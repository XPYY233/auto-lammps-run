"""Real ASE geometry checks, with physical evaluation methods forbidden."""
from copy import deepcopy
from io import StringIO
import unittest
from unittest.mock import patch

import ase
from ase.io.lammpsdata import read_lammps_data

from auto_lammps.structures import StructureError, build_structure, validate_structure

SPEC = {'crystal': 'fcc', 'elements': ['Cu'], 'a_angstrom': 4.0, 'repeat': [2, 3, 4],
        'orientation': 'cubic_axes', 'boundary': ['p', 'p', 'p'], 'vacancies': [], 'substitutions': [],
        'type_elements': ['Cu'], 'masses_amu': [63.5]}


class StructureTests(unittest.TestCase):
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
