"""Real ASE geometry checks, with physical evaluation methods forbidden."""
from copy import deepcopy
from contextlib import ExitStack
from io import StringIO
import unittest
from unittest.mock import patch

import ase
import numpy as np
from ase.io.lammpsdata import read_lammps_data

from auto_lammps.structures import StructureError, build_structure, validate_structure

SPEC = {'crystal': 'fcc', 'elements': ['Cu'], 'a_angstrom': 4.0, 'repeat': [2, 3, 4],
        'orientation': 'cubic_axes', 'boundary': ['p', 'p', 'p'], 'vacancies': [], 'substitutions': [],
        'type_elements': ['Cu'], 'masses_amu': [63.5]}

# Deliberately synthetic basis, not a published material or reference input.
EXPLICIT = {'crystal': 'explicit_cell', 'cell_angstrom': [[4., 0., 0.], [0., 4., 0.], [0., 0., 7.]],
            'site_elements': ['Cu', 'Cu', 'Cu'],
            'scaled_positions': [[0.1, 0.2, 0.3], [0.5, 0.5, 0.5], [0.8, 0.6, 0.7]],
            'repeat': [1, 1, 1], 'orientation': 'provided_axes', 'boundary': ['p', 'p', 'p'],
            'vacancies': [], 'substitutions': [], 'type_elements': ['Cu'], 'masses_amu': [63.5]}


class StructureTests(unittest.TestCase):
    def build_without_physics(self, spec, units='metal'):
        with ExitStack() as stack:
            for method in ('get_potential_energy', 'get_forces', 'get_stress'):
                stack.enter_context(patch.object(ase.Atoms, method, side_effect=AssertionError('no physical evaluation')))
            stack.enter_context(patch('subprocess.Popen', side_effect=AssertionError('no process')))
            return build_structure(spec, units=units)

    def test_explicit_tetragonal_cell_and_basis_roundtrip(self):
        before = deepcopy(EXPLICIT)
        result = self.build_without_physics(EXPLICIT)
        atoms = read_lammps_data(StringIO(result.data.decode()), Z_of_type={1: 29}, atom_style='atomic', units='metal')
        np.testing.assert_allclose(atoms.cell, EXPLICIT['cell_angstrom'], rtol=0, atol=1e-14)
        np.testing.assert_allclose(atoms.get_scaled_positions(wrap=False), EXPLICIT['scaled_positions'], rtol=0, atol=1e-14)
        np.testing.assert_allclose(atoms.get_masses(), [63.5]*3, rtol=0, atol=1e-10)
        self.assertEqual(EXPLICIT, before)
        self.assertEqual(result.data, self.build_without_physics(EXPLICIT).data)
        self.assertEqual(result.receipt['builder'], 'ase.Atoms.explicit_cell')
        self.assertFalse(result.receipt['automatic_rotation_performed'])
        self.assertFalse(result.receipt['physical_evaluation_performed'])

    def test_triclinic_coordinates_and_supplied_frame_are_not_rotated_or_reduced(self):
        spec = deepcopy(EXPLICIT)
        spec.update(cell_angstrom=[[4., 0, 0], [-3., 5., 0], [2., -1., 12.]],
                    boundary=['p', 'p', 'f'])
        expected = np.array(spec['scaled_positions']) @ np.array(spec['cell_angstrom'])
        for units in ('metal', 'real'):
            with self.subTest(units=units):
                result = self.build_without_physics(spec, units)
                atoms = read_lammps_data(StringIO(result.data.decode()), Z_of_type={1: 29}, atom_style='atomic', units=units)
                np.testing.assert_allclose(atoms.cell, spec['cell_angstrom'], rtol=0, atol=1e-13)
                np.testing.assert_allclose(atoms.positions, expected, rtol=0, atol=1e-13)
                self.assertEqual(result.receipt['boundary'], ['p', 'p', 'f'])
                self.assertIn(b'xy xz yz', result.data)
                self.assertFalse(result.receipt['automatic_wrapping_performed'])

    def test_explicit_replication_site_order_and_defects_preserved(self):
        spec = deepcopy(EXPLICIT)
        spec.update(repeat=[2, 2, 2], site_elements=['Cu', 'Ni', 'Cu'],
                    type_elements=['Ni', 'Cu'], masses_amu=[58.7, 63.5],
                    vacancies=[0, 8], substitutions=[{'site': 3, 'element': 'Ni'}])
        expected_positions = []
        expected_species = []
        cell = np.array(spec['cell_angstrom'])
        for x in range(2):
            for y in range(2):
                for z in range(2):
                    for species, point in zip(spec['site_elements'], spec['scaled_positions']):
                        expected_positions.append((np.array(point) + [x, y, z]) @ cell)
                        expected_species.append(species)
        expected_species[3] = 'Ni'
        for i in sorted(spec['vacancies'], reverse=True):
            del expected_positions[i], expected_species[i]
        result = self.build_without_physics(spec)
        atoms = read_lammps_data(StringIO(result.data.decode()), Z_of_type={1: 28, 2: 29}, atom_style='atomic', units='metal')
        self.assertEqual(atoms.get_chemical_symbols(), expected_species)
        np.testing.assert_allclose(atoms.positions, expected_positions, rtol=0, atol=1e-13)
        np.testing.assert_allclose(atoms.cell, 2*cell, rtol=0, atol=1e-13)
        self.assertEqual(result.receipt['original_site_count'], 24)
        self.assertEqual(result.receipt['atom_count'], 22)
        self.assertEqual(result.receipt['type_elements'], ['Ni', 'Cu'])

    def test_small_explicit_tilt_is_not_lost_to_writer_tolerance(self):
        spec = deepcopy(EXPLICIT)
        spec['cell_angstrom'][1][0] = 1e-10
        result = self.build_without_physics(spec)
        self.assertIn(b'xy xz yz', result.data)
        atoms = read_lammps_data(StringIO(result.data.decode()), Z_of_type={1: 29}, atom_style='atomic', units='metal')
        self.assertAlmostEqual(atoms.cell[1, 0], 1e-10, delta=1e-24)

    def test_invalid_explicit_cells_rejected_without_constructing_atoms(self):
        cells = ([], [[4, 0], [0, 4], [0, 0]], [[4, 0, 0], [0, 4, 0], [0, 0, 0]],
                 [[-4, 0, 0], [0, 4, 0], [0, 0, 7]], [[0, 4, 0], [-4, 0, 0], [0, 0, 7]],
                 [[4, 0.1, 0], [0, 4, 0], [0, 0, 7]], [[4, 0, 0], [0, 4, 0.1], [0, 0, 7]],
                 [[True, 0, 0], [0, 4, 0], [0, 0, 7]], [[4, 0, 0], [0, float('inf'), 0], [0, 0, 7]],
                 [[1e-9, 0, 0], [0, 1e-9, 0], [0, 0, 1e-9]],
                 [[1e-7, 0, 0], [1000, 1e-7, 0], [1000, 1000, 1000]])
        with patch('ase.Atoms', side_effect=AssertionError('must validate first')):
            for cell in cells:
                with self.subTest(cell=cell), self.assertRaises(StructureError):
                    build_structure({**EXPLICIT, 'cell_angstrom': cell}, units='metal')

    def test_invalid_basis_and_schema_rejected_without_wrapping_or_defaults(self):
        for change in ({'scaled_positions': []}, {'site_elements': ['Cu']},
                       {'site_elements': ['Cu', 'Xx', 'Cu']}, {'orientation': 'cubic_axes'},
                       {'a_angstrom': 4}, {'elements': ['Cu']}, {'calculator': 'EMT'},
                       {'scaled_positions': [[0, 0, 0]]*3}):
            with self.subTest(change=change), self.assertRaises(StructureError):
                validate_structure({**EXPLICIT, **change})
        for value in (True, float('nan'), float('inf'), -0.1, 1.0, 10**400):
            spec = deepcopy(EXPLICIT)
            spec['scaled_positions'][0][0] = value
            with self.subTest(value=str(value)[:20]), self.assertRaises(StructureError):
                validate_structure(spec)
        for field in ('cell_angstrom', 'scaled_positions', 'site_elements'):
            spec = deepcopy(EXPLICIT)
            del spec[field]
            with self.subTest(missing=field), self.assertRaises(StructureError):
                validate_structure(spec)

    def test_explicit_atom_limit_is_enforced_before_allocation(self):
        with patch('ase.Atoms', side_effect=AssertionError('no allocation')):
            with self.assertRaises(StructureError):
                build_structure({**EXPLICIT, 'repeat': [1000, 1000, 1000]}, units='metal')
            with self.assertRaises(StructureError):
                build_structure(EXPLICIT, units='metal', max_atoms=2)

    def test_fcc_geometry_roundtrip_without_any_physical_evaluation(self):
        with patch.object(ase.Atoms, 'get_potential_energy', side_effect=AssertionError('no local evaluation')), \
                patch.object(ase.Atoms, 'get_forces', side_effect=AssertionError('no local evaluation')), \
                patch.object(ase.Atoms, 'get_stress', side_effect=AssertionError('no local evaluation')):
            result = build_structure(SPEC, units='metal')
        atoms = read_lammps_data(StringIO(result.data.decode()), Z_of_type={1: 29}, atom_style='atomic', units='metal')
        self.assertEqual(len(atoms), 96)
        self.assertEqual(atoms.cell.tolist(), [[8., 0., 0.], [0., 12., 0.], [0., 0., 16.]])
        self.assertAlmostEqual(atoms.get_masses()[0], 63.5, places=10)
        self.assertEqual(atoms.positions[0].tolist(), [0., 0., 0.])
        self.assertEqual(result.receipt['composition'], {'Cu': 96})
        self.assertFalse(result.receipt['physical_evaluation_performed'])
        self.assertEqual(result.data, build_structure(SPEC, units='metal').data)

    def test_defects_use_original_indices_and_preserve_type_order(self):
        spec = deepcopy(SPEC)
        spec.update(vacancies=[0, 5], substitutions=[{'site': 1, 'element': 'Ni'}],
                    type_elements=['Ni', 'Cu'], masses_amu=[58.7, 63.5], boundary=['p', 'p', 'f'])
        result = build_structure(spec, units='real')
        atoms = read_lammps_data(StringIO(result.data.decode()), Z_of_type={1: 28, 2: 29}, atom_style='atomic', units='real')
        self.assertEqual(len(atoms), 94)
        self.assertEqual(atoms[0].symbol, 'Ni')
        self.assertEqual(atoms.positions[0].tolist(), [0., 2., 2.])
        self.assertEqual(result.receipt['composition'], {'Ni': 1, 'Cu': 93})
        self.assertEqual(result.receipt['boundary'], ['p', 'p', 'f'])

    def test_bcc_and_binary_conventional_cells(self):
        for crystal, elements, count in [('bcc', ['W'], 2), ('diamond', ['Si'], 8),
                                          ('rocksalt', ['Na', 'Cl'], 8), ('zincblende', ['Si', 'C'], 8)]:
            spec = deepcopy(SPEC)
            spec.update(crystal=crystal, elements=elements, repeat=[1, 1, 1],
                        type_elements=elements, masses_amu=[20.] * len(elements))
            with self.subTest(crystal=crystal):
                result = build_structure(spec, units='metal')
                self.assertEqual(result.receipt['atom_count'], count)
                self.assertEqual(set(result.receipt['composition']), set(elements))

    def test_unsafe_or_implicit_geometry_rejected_before_build(self):
        for change in ({'a_angstrom': float('nan')}, {'a_angstrom': True}, {'repeat': [1000, 1000, 1000]},
                       {'repeat': [1, True, 2]}, {'orientation': 'arbitrary'}, {'vacancies': [0, 0]},
                       {'vacancies': [96]}, {'substitutions': [{'site': -1, 'element': 'Ni'}]},
                       {'vacancies': [1], 'substitutions': [{'site': 1, 'element': 'Ni'}]},
                       {'masses_amu': []}, {'calculator': 'EMT'}, {'boundary': ['s', 'p', 'p']}):
            with self.subTest(change=change), self.assertRaises(StructureError):
                validate_structure({**SPEC, **change})
        missing = deepcopy(SPEC)
        del missing['a_angstrom']
        with self.assertRaises(StructureError): build_structure(missing, units='metal')
        with self.assertRaises(StructureError): build_structure({**SPEC, 'type_elements': ['Ni']}, units='metal')

    def test_pinned_dependency_version_is_checked(self):
        with patch.object(ase, '__version__', 'not-the-pinned-version'), self.assertRaises(StructureError):
            build_structure(SPEC, units='metal')


if __name__ == '__main__':
    unittest.main()
