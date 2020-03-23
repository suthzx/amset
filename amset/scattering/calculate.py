"""
This module implements methods to calculate electron scattering based on an
AmsetData object.
"""

import logging
import time
from multiprocessing import cpu_count

from quadpy import nsimplex, ncube
from typing import Any, Dict, List, Optional, Union

import numpy as np
from scipy.interpolate import griddata

from amset.constants import hbar, small_val, spin_name
from amset.core.data import AmsetData
from amset.electronic_structure.kpoints import kpoints_to_first_bz
from amset.electronic_structure.tetrahedron import (
    get_cross_section_values,
    get_projected_intersections,
)
from amset.electronic_structure.tetrahedron import numerical_integration_defaults as ni
from amset.log import log_list, log_time_taken
from amset.scattering.basic import AbstractBasicScattering
from amset.scattering.elastic import AbstractElasticScattering
from amset.scattering.inelastic import AbstractInelasticScattering
from amset.util import get_progress_bar
from BoltzTraP2 import units
from BoltzTraP2.fd import FD
from pymatgen import Spin
from pymatgen.util.coord import pbc_diff

__author__ = "Alex Ganose"
__maintainer__ = "Alex Ganose"
__email__ = "aganose@lbl.gov"

logger = logging.getLogger(__name__)

_all_scatterers = (
    AbstractElasticScattering.__subclasses__()
    + AbstractInelasticScattering.__subclasses__()
    + AbstractBasicScattering.__subclasses__()
)
_scattering_mechanisms = {m.name: m for m in _all_scatterers}


class ScatteringCalculator(object):
    def __init__(
        self,
        settings: Dict[str, float],
        amset_data: AmsetData,
        cutoff_pad: float,
        scattering_type: Union[str, List[str], float] = "auto",
        nworkers: int = -1,
    ):
        if amset_data.temperatures is None or amset_data.doping is None:
            raise RuntimeError(
                "AmsetData doesn't contain doping levels or temperatures"
            )

        self.scattering_type = scattering_type
        self.settings = settings
        self.nworkers = nworkers if nworkers != -1 else cpu_count()
        self.scatterers = self.get_scatterers(scattering_type, settings, amset_data)
        self.amset_data = amset_data

        buf = 0.05 * units.eV
        if self.amset_data.fd_cutoffs:
            self.scattering_energy_cutoffs = (
                self.amset_data.fd_cutoffs[0] - buf,
                self.amset_data.fd_cutoffs[1] + buf,
            )
        else:
            self.scattering_energy_cutoffs = (
                min(self.amset_data.dos.energies) - buf,
                max(self.amset_data.dos.energies) + buf,
            )

        # precompute the coefficients we will need to for calculating overlaps
        # could do this on the fly but caching will really speed things up.
        # we need to interpolate as the wavefunction coefficients were calculated on a
        # dense mesh but we calculate the orbital overlap on a fine mesh.
        self._coeffs = {}
        self._coeffs_mapping = {}
        tbs = self.amset_data.tetrahedral_band_structure
        for spin in amset_data.spins:
            spin_b_idxs = []
            spin_k_idxs = []
            for b_idx, b_energies in enumerate(self.amset_data.energies[spin]):
                # find all k-points that fall inside Fermi cutoffs
                k_idxs = np.where(
                    (b_energies > self.scattering_energy_cutoffs[0] - cutoff_pad) &
                    (b_energies < self.scattering_energy_cutoffs[1] + cutoff_pad)
                )[0]

                # find k-points connected to the k-points inside Fermi cutoffs
                k_idxs = tbs.get_connected_kpoints(k_idxs)

                spin_k_idxs.extend(k_idxs.tolist())
                spin_b_idxs.extend([b_idx] * len(k_idxs))

            # calculate the coefficients for all bands and k-point simultaneously
            self._coeffs[spin] = self.amset_data.overlap_calculator.get_coefficients(
                spin, spin_b_idxs, self.amset_data.kpoints[spin_k_idxs],
            )

            # because we are only storing the coefficients for the band/k-points we
            # want, we need a way of mapping from the original band/k-point indices to
            # the reduced indices. I.e., it allows us to get the coefficients for
            # band b_idx, and k-point k_idx using:
            # self._coeffs[spin][self._coeffs_mapping[b_idx, k_idx]]
            # use a default value of 100000 as this was it will throw an error
            # if we don't precache the correct values
            mapping = np.full_like(self.amset_data.energies[spin], 100000, dtype=int)
            mapping[spin_b_idxs, spin_k_idxs] = np.arange(len(spin_b_idxs)).astype(int)
            self._coeffs_mapping[spin] = mapping

    @property
    def basic_scatterers(self):
        return [s for s in self.scatterers if isinstance(s, AbstractBasicScattering)]

    @property
    def inelastic_scatterers(self):
        return [
            s for s in self.scatterers if isinstance(s, AbstractInelasticScattering)
        ]

    @property
    def elastic_scatterers(self):
        return [s for s in self.scatterers if isinstance(s, AbstractElasticScattering)]

    @property
    def scatterer_labels(self):
        basic_names = [s.name for s in self.basic_scatterers]
        elastic_names = [s.name for s in self.elastic_scatterers]
        inelastic_names = [s.name for s in self.inelastic_scatterers]

        return basic_names + elastic_names + inelastic_names

    @staticmethod
    def get_scatterers(
        scattering_type: Union[str, List[str], float],
        settings: Dict[str, Any],
        amset_data: AmsetData,
    ) -> List[Union[AbstractElasticScattering, AbstractInelasticScattering]]:
        if scattering_type == "auto":
            # dynamically determine the available scattering mechanism subclasses
            logger.info(
                "Examining material properties to determine possible "
                "scattering mechanisms"
            )

            scattering_type = []
            for name, mechanism in _scattering_mechanisms.items():
                if all([settings.get(x, False) for x in mechanism.required_properties]):
                    scattering_type.append(name)

            if not scattering_type:
                raise ValueError(
                    "No scattering mechanisms possible with " "material properties"
                )

        else:
            for name in scattering_type:
                missing_properties = [
                    p
                    for p in _scattering_mechanisms[name].required_properties
                    if not settings.get(p, False)
                ]

                if missing_properties:
                    str_missing = ", ".join(missing_properties)
                    raise ValueError(
                        "{} scattering mechanism specified but the following "
                        "material properties are missing: {}".format(name, str_missing)
                    )

        str_scats = ", ".join(scattering_type)
        logger.info("Scattering mechanisms to be calculated: {}".format(str_scats))

        return [
            _scattering_mechanisms[name](settings, amset_data)
            for name in scattering_type
        ]

    def calculate_scattering_rates(self):
        spins = self.amset_data.spins
        kpoints = self.amset_data.kpoints
        f_shape = self.amset_data.fermi_levels.shape
        scattering_shape = (len(self.scatterer_labels),) + f_shape

        # rates has shape (spin, nscatterers, ndoping, ntemp, nbands, nkpoints)
        rates = {
            s: np.zeros(scattering_shape + self.amset_data.energies[s].shape)
            for s in spins
        }
        masks = {
            s: np.full(scattering_shape + self.amset_data.energies[s].shape, True)
            for s in spins
        }

        nkpoints = len(self.amset_data.ir_kpoints_idx)

        logger.info("Scattering information:")
        log_list(["# ir k-points: {}".format(nkpoints)])

        for spin in spins:
            for b_idx in range(len(self.amset_data.energies[spin])):
                str_b = "Calculating rates for {} band {}"
                logger.info(str_b.format(spin_name[spin], b_idx + 1))

                t0 = time.perf_counter()
                (
                    rates[spin][..., b_idx, :],
                    masks[spin][..., b_idx, :],
                ) = self.calculate_band_rates(spin, b_idx)

                info = [
                    "max rate: {:.4g}".format(rates[spin][..., b_idx, :].max()),
                    "min rate: {:.4g}".format(rates[spin][..., b_idx, :].min()),
                    "time: {:.4f} s".format(time.perf_counter() - t0),
                ]
                log_list(info)

            # fill in k-points outside Fermi-Dirac cutoffs with a default value
            rates[spin][masks[spin]] = 1e14

        # if the k-point density is low, some k-points may not have other k-points
        # within the energy tolerance leading to zero rates
        rates = _interpolate_zero_rates(rates, kpoints, masks)

        return rates

    def calculate_band_rates(self, spin: Spin, b_idx: int):
        conversion = self.amset_data.structure.lattice.reciprocal_lattice.volume
        kpoints_idx = self.amset_data.ir_kpoints_idx
        nkpoints = len(kpoints_idx)

        band_energies = self.amset_data.energies[spin][b_idx, kpoints_idx]
        mask = band_energies < self.scattering_energy_cutoffs[0]
        mask |= band_energies > self.scattering_energy_cutoffs[1]
        fill_mask = mask[self.amset_data.ir_to_full_kpoint_mapping]

        n = np.sum(~fill_mask)
        logger.debug("  ├── # k-points within Fermi–Dirac cut-offs: {}".format(n))

        k_idx_in_cutoff = kpoints_idx[~mask]
        ir_idx_in_cutoff = np.arange(nkpoints)[~mask]
        iterable = list(zip(k_idx_in_cutoff, ir_idx_in_cutoff))

        to_stack = []
        if len(self.basic_scatterers) > 0:
            basic_rates = np.array(
                [m.rates[spin][:, :, b_idx, kpoints_idx] for m in self.basic_scatterers]
            )
            to_stack.append(basic_rates)

        if len(self.elastic_scatterers) > 0:
            elastic_prefactors = conversion * np.array(
                [m.prefactor(spin, b_idx) for m in self.elastic_scatterers]
            )
            elastic_rates = np.zeros(elastic_prefactors.shape + (nkpoints,))

            if len(k_idx_in_cutoff) > 0:
                pbar = get_progress_bar(iterable, desc="elastic")
                for k_idx, ir_idx, in pbar:
                    elastic_rates[..., ir_idx] = self.calculate_rate(spin, b_idx, k_idx)

            elastic_rates *= elastic_prefactors[..., None]
            to_stack.append(elastic_rates)

        if len(self.inelastic_scatterers) > 0:
            inelastic_prefactors = conversion * np.array(
                [m.prefactor(spin, b_idx) for m in self.inelastic_scatterers]
            )
            inelastic_rates = np.zeros(inelastic_prefactors.shape + (nkpoints,))
            f_pop = self.settings["pop_frequency"]
            energy_diff = f_pop * 1e12 * 2 * np.pi * hbar * units.eV

            if len(k_idx_in_cutoff) > 0:
                pbar = get_progress_bar(iterable, desc="inelastic")
                inelastic_rates[:, :, :, ir_idx_in_cutoff] = 0
                for k_idx, ir_idx in pbar:
                    for ediff in [energy_diff, -energy_diff]:
                        inelastic_rates[:, :, :, ir_idx] += self.calculate_rate(
                            spin, b_idx, k_idx, energy_diff=ediff
                        )

            inelastic_rates *= inelastic_prefactors[..., None]
            to_stack.append(inelastic_rates)

        all_band_rates = np.vstack(to_stack)

        return all_band_rates[..., self.amset_data.ir_to_full_kpoint_mapping], fill_mask

    def calculate_rate(self, spin, b_idx, k_idx, energy_diff=None):
        rlat = self.amset_data.structure.lattice.reciprocal_lattice.matrix
        energy = self.amset_data.energies[spin][b_idx, k_idx]

        if energy_diff:
            energy += energy_diff

        tbs = self.amset_data.tetrahedral_band_structure

        (
            tet_dos,
            tet_mask,
            cs_weights,
            tet_contributions,
        ) = tbs.get_tetrahedra_density_of_states(
            spin,
            energy,
            return_contributions=True,
            symmetry_reduce=False,
            # band_idx=b_idx,  # turn this on to disable interband scattering
        )

        if len(tet_dos) == 0:
            return 0

        # next, get k-point indices and band_indices
        property_mask, band_kpoint_mask, band_mask, kpoint_mask = tbs.get_masks(
            spin, tet_mask
        )
        k = self.amset_data.kpoints[k_idx]

        # use cached coefficients to calculate the overlap on the fine mesh
        # tetrahedron vertices
        p1 = self._coeffs[spin][self._coeffs_mapping[spin][b_idx, k_idx]]
        p2 = self._coeffs[spin][self._coeffs_mapping[spin][band_mask, kpoint_mask]]
        braket = np.abs(np.dot(np.conj(p1), np.asarray(p2).T))
        overlap = braket ** 2

        # put overlap back in array with shape (nbands, nkpoints)
        all_overlap = np.zeros(self.amset_data.energies[spin].shape)
        all_overlap[band_kpoint_mask] = overlap

        # now select the properties at the tetrahedron vertices
        vert_overlap = all_overlap[property_mask]

        # get interpolated overlap at centre of tetrahedra cross sections
        tet_overlap = get_cross_section_values(vert_overlap, *tet_contributions)
        tetrahedra = tbs.tetrahedra[spin][tet_mask]

        # have to deal with the case where the tetrahedron cross section crosses the
        # zone boundary. This is a slight inaccuracy but we just treat the
        # cross section as if it is on one side of the boundary
        tet_kpoints = self.amset_data.kpoints[tetrahedra]
        base_kpoints = tet_kpoints[:, 0][:, None, :]
        k_diff = pbc_diff(tet_kpoints, base_kpoints) + pbc_diff(base_kpoints, k)

        k_diff = np.dot(k_diff, rlat)
        intersections = get_cross_section_values(
            k_diff, *tet_contributions, average=False
        )
        projected_intersections, basis = get_projected_intersections(intersections)

        k_spacing = np.linalg.norm(np.dot(rlat, 1 / self.amset_data.kpoint_mesh))
        qpoints, weights, mapping = get_fine_mesh_qpoints(
            projected_intersections,
            basis,
            *tet_contributions[0:3],
            high_tol=k_spacing * 0.5,
            med_tol=k_spacing * 2,
            cross_section_weights=cs_weights
        )
        qpoint_norm_sq = np.sum(qpoints ** 2, axis=-1)

        k_primes = np.dot(qpoints, np.linalg.inv(rlat)) + k
        k_primes = kpoints_to_first_bz(k_primes)
        if energy_diff:
            fd = _get_fd(energy, self.amset_data)
            emission = energy_diff <= 0
            rates = [
                s.factor(qpoint_norm_sq, emission, fd)
                for s in self.inelastic_scatterers
            ]
            mrta_factor = 1
        else:
            mrta_factor = self.amset_data.mrta_calculator.get_mrta_factor(
                spin, b_idx, k, tet_mask[0][mapping], k_primes
            )
            rates = [s.factor(qpoint_norm_sq) for s in self.elastic_scatterers]

        rates = np.array(rates)

        # sometimes the projected intersections can be nan when the density of states
        # contribution is infinitesimally small; this catches those errors
        rates[np.isnan(rates)] = 0

        # this is too expensive vs tetrahedron integration and doesn't add much more
        # accuracy; could offer this as an option
        # overlap = self.amset_data.overlap_calculator.get_overlap(
        #     spin, b_idx, k, tet_mask[0][mapping], k_primes
        # )

        rates /= self.amset_data.structure.lattice.reciprocal_lattice.volume
        # rates *= overlap * weights * mrta_factor
        rates *= tet_overlap[mapping] * weights * mrta_factor

        return np.sum(rates, axis=-1)


def _interpolate_zero_rates(rates, kpoints, masks: Optional = None):
    # loop over all scattering types, doping, temps, and bands and interpolate
    # zero scattering rates based on the nearest k-point
    logger.info("Interpolating missing scattering rates")
    n_rates = sum([np.product(rates[spin].shape[:-1]) for spin in rates])
    pbar = get_progress_bar(total=n_rates, desc="progress")

    t0 = time.perf_counter()
    k_idx = np.arange(len(kpoints))
    for spin in rates:
        for s, d, t, b in np.ndindex(rates[spin].shape[:-1]):

            if masks is not None:
                mask = np.invert(masks[spin][s, d, t, b])
            else:
                mask = [True] * len(rates[spin][s, d, t, b])

            non_zero_rates = rates[spin][s, d, t, b, mask] > 1e7
            # non_zero_rates = rates[spin][s, d, t, b, mask] != 0
            zero_rate_idx = k_idx[mask][~non_zero_rates]
            non_zero_rate_idx = k_idx[mask][non_zero_rates]

            if not np.any(non_zero_rates):
                # all scattering rates are zero so cannot interpolate
                # generally this means the scattering prefactor is zero. E.g.
                # for POP when studying non polar materials
                rates[spin][s, d, t, b, mask] += small_val

            elif np.sum(non_zero_rates) != np.sum(mask):
                # electronic_structure seems to work best when all the kpoints are +ve
                # therefore add 0.5
                # Todo: Use cartesian coordinates (will be more robust to
                #  oddly shaped cells)
                rates[spin][s, d, t, b, zero_rate_idx] = griddata(
                    points=kpoints[non_zero_rate_idx] + 0.5,
                    values=rates[spin][s, d, t, b, non_zero_rate_idx],
                    xi=kpoints[zero_rate_idx] + 0.5,
                    method="nearest",
                )
                # rates[spin][s, d, t, b, zero_rate_idx] = 1e15

            pbar.update()
    pbar.close()
    log_time_taken(t0)

    return rates


def get_fine_mesh_qpoints(
    intersections,
    basis,
    cond_a_mask,
    cond_b_mask,
    cond_c_mask,
    high_tol=0.1,
    med_tol=0.2,
    cross_section_weights=None,
):
    if cross_section_weights is None:
        cross_section_weights = np.ones(len(intersections))

    # minimum norm in each intersection
    all_norms = np.linalg.norm(intersections, axis=-1)

    # intersections now has shape nvert, ntet, 2 (i.e., x, y coords)
    intersection_idxs = np.arange(len(intersections))
    z_coords = intersections[:, 0, 2]
    intersections = intersections[:, :, :2].transpose(1, 0, 2)
    triangle_mask = cond_a_mask | cond_c_mask

    # have to do these separately as the triangle intersections always have [0, 0, 0]
    # as the last coordinate
    norms = np.ones(len(all_norms))
    norms[triangle_mask] = np.min(all_norms[:, :3][triangle_mask], axis=-1)
    norms[cond_b_mask] = np.min(all_norms[cond_b_mask], axis=-1)

    qpoints = []
    qweights = []
    mapping = []  # maps a qpoint to an intersection index

    def _get_tri_mesh(prec, min_norm, max_norm):
        scheme = ni[prec]["triangle"]
        mask = (min_norm <= norms) & (norms < max_norm) & triangle_mask
        if not np.any(mask):
            return

        simplex = intersections[:3, mask]
        vol = nsimplex.get_vol(simplex)
        xy_coords = nsimplex.transform(scheme.points.T, simplex.T)
        weights = (
            scheme.weights[None] * vol[:, None] * cross_section_weights[mask][:, None]
        )

        qpoints.append(get_q(xy_coords, z_coords[mask]))
        qweights.append(weights.reshape(-1))
        mapping.append(np.repeat(intersection_idxs[mask], len(scheme.weights)))

    def _get_quad_mesh(prec, min_norm, max_norm):
        scheme = ni[prec]["quad"]
        mask = (min_norm <= norms) & (norms < max_norm) & cond_b_mask
        if not np.any(mask):
            return

        cube = intersections.reshape((2, 2, -1, 2))[:, :, mask]
        vol = np.abs(ncube._helpers.get_detJ(scheme.points.T, cube))
        xy_coords = ncube.transform(scheme.points.T, cube).T
        weights = scheme.weights[None] * vol * cross_section_weights[mask][:, None]

        qpoints.append(get_q(xy_coords, z_coords[mask]))
        qweights.append(weights.reshape(-1))
        mapping.append(np.repeat(intersection_idxs[mask], len(scheme.weights)))

    _get_tri_mesh("high", 0, high_tol)
    _get_tri_mesh("medium", high_tol, med_tol)
    _get_tri_mesh("low", med_tol, np.Inf)
    _get_quad_mesh("high", 0, high_tol)
    _get_quad_mesh("medium", high_tol, med_tol)
    _get_quad_mesh("low", med_tol, np.Inf)

    qpoints = np.concatenate(qpoints)
    qweights = np.concatenate(qweights)
    mapping = np.concatenate(mapping)

    return get_kpoints_in_original_basis(qpoints, basis[mapping]), qweights, mapping


def get_kpoints_in_original_basis(q, basis):
    # transform k back to original lattice basis in cartesian coords
    return np.einsum("ikj,ij->ik", basis, q)


def get_q(x, z_coords):
    z = np.repeat(z_coords[:, None], len(x[0][0]), axis=-1)
    return np.stack([x[0], x[1], z], axis=-1).reshape(-1, 3)


def _get_fd(energy, amset_data):
    f = np.zeros(amset_data.fermi_levels.shape)

    for n, t in np.ndindex(amset_data.fermi_levels.shape):
        f[n, t] = FD(
            energy,
            amset_data.fermi_levels[n, t],
            amset_data.temperatures[t] * units.BOLTZMANN,
        )
    return f
