"""Pure geometry and malformed-resource checks; no engine, API or HPC calls."""
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import importlib.util
from io import StringIO
import json
import unittest
from unittest.mock import patch

from auto_lammps.atomic_structure_data import AtomicStructureDataError, inspect_atomic_data


DATA = b'''Synthetic atomic resource

2 atoms
2 atom types

-1 3 xlo xhi
2 7 ylo yhi
-3 9 zlo zhi
-3 2 -1 xy xz yz

Masses

1 58.7 # deliberately not used to infer an element
2 63.5

Atoms # atomic

1 2 0.4 0.7 3.6
2 1 2.2 3.1 6.8
'''
OPTIONS = {'units': 'metal', 'boundary': ['p', 'p', 'f'], 'type_elements': ['Ni', 'Cu'], 'max_atoms': 100000}


def inspect(data=DATA, **changes):
    return inspect_atomic_data(data, **{**deepcopy(OPTIONS), **changes})


class AtomicStructureDataTests(unittest.TestCase):
    def reject(self, data=DATA, **changes):
        with self.assertRaises(AtomicStructureDataError):
            inspect(data, **changes)

    def test_summary_preserves_source_frame_identity_and_declared_mapping(self):
        before = bytes(DATA)
        options = deepcopy(OPTIONS)
        result = inspect_atomic_data(DATA, **options)
        self.assertEqual(DATA, before)
        self.assertEqual(options, OPTIONS)
        self.assertEqual(result['data_sha256'], hashlib.sha256(DATA).hexdigest())
        self.assertEqual(result['size'], len(DATA))
        self.assertEqual(result['atom_count'], 2)
        self.assertEqual(result['type_counts'], {'1': 1, '2': 1})
        self.assertEqual(result['composition'], {'Ni': 1, 'Cu': 1})
        self.assertEqual(result['cell_angstrom'], [[4., 0., 0.], [-3., 5., 0.], [2., -1., 12.]])
        self.assertEqual(result['origin_angstrom'], [-1., 2., -3.])
        self.assertEqual(result['tilt_angstrom'], [-3., 2., -1.])
        self.assertEqual(result['masses_amu'], [58.7, 63.5])
        self.assertEqual(result['boundary'], OPTIONS['boundary'])
        self.assertFalse(result['physical_evaluation_performed'])
        self.assertFalse(result['scientifically_verified'])
        self.assertFalse(result['elements_inferred'])
        self.assertNotIn('positions', result)
        json.dumps(result, allow_nan=False)

    def test_mass_is_optional_and_never_supplied_by_element_defaults(self):
        data = DATA.replace(b'Masses\n\n1 58.7 # deliberately not used to infer an element\n2 63.5\n\n', b'')
        result = inspect(data)
        self.assertIsNone(result['masses_amu'])
        self.assertEqual(result['mass_source'], 'not_in_file')
        self.assertEqual(result['composition'], {'Ni': 1, 'Cu': 1})

    def test_masses_do_not_override_explicit_element_mapping(self):
        result = inspect(type_elements=['Cu', 'Ni'])
        self.assertEqual(result['type_elements'], ['Cu', 'Ni'])
        self.assertEqual(result['masses_amu'], [58.7, 63.5])

    def test_orthogonal_box_and_both_supported_units(self):
        data = DATA.replace(b'-3 2 -1 xy xz yz\n', b'')
        for units in ('metal', 'real'):
            with self.subTest(units=units):
                result = inspect(data, units=units)
                self.assertEqual(result['cell_angstrom'], [[4., 0., 0.], [0., 5., 0.], [0., 0., 12.]])
                self.assertEqual(result['tilt_angstrom'], [0., 0., 0.])
                self.assertEqual(result['units'], units)

    def test_integer_image_flags_and_exponent_numbers(self):
        data = DATA.replace(b'1 2 0.4 0.7 3.6', b'+1 2 4.e-1 +.7 3.6e0 0 -1 0').replace(
            b'2 1 2.2 3.1 6.8', b'2 1 2.2 3.1 6.8 1 0 0')
        result = inspect(data)
        self.assertEqual(result['atom_record_columns'], 8)
        self.assertEqual(result['type_counts'], inspect()['type_counts'])
        self.assertNotEqual(result['coordinate_content_sha256'], inspect()['coordinate_content_sha256'])

    def test_unannotated_atoms_and_crlf_are_supported(self):
        result = inspect(DATA.replace(b'Atoms # atomic', b'Atoms').replace(b'\n', b'\r\n'))
        self.assertEqual(result['coordinate_content_sha256'], inspect()['coordinate_content_sha256'])
        self.reject(DATA.replace(b'\n', b'\r'))

    def test_continuous_id_set_can_have_unsorted_rows_without_reordering(self):
        first, second = b'1 2 0.4 0.7 3.6', b'2 1 2.2 3.1 6.8'
        result = inspect(DATA.replace(first + b'\n' + second, second + b'\n' + first))
        self.assertEqual(result['id_policy'], 'continuous_1_to_N_preserve_input_order')
        self.assertEqual(result['composition'], inspect()['composition'])
        self.assertNotEqual(result['particle_id_order_sha256'], inspect()['particle_id_order_sha256'])
        self.assertNotEqual(result['coordinate_content_sha256'], inspect()['coordinate_content_sha256'])

    def test_raw_identity_includes_comments_but_coordinate_digest_does_not(self):
        result = inspect(DATA + b'\n# preserved resource comment\n')
        self.assertNotEqual(result['data_sha256'], inspect()['data_sha256'])
        self.assertEqual(result['coordinate_content_sha256'], inspect()['coordinate_content_sha256'])

    def test_declared_unused_types_are_explicit_zero_counts(self):
        result = inspect(DATA.replace(b'2 1 2.2', b'2 2 2.2'))
        self.assertEqual(result['type_counts'], {'1': 0, '2': 2})
        self.assertEqual(result['composition'], {'Cu': 2})

    def test_mass_section_after_atoms_is_not_lost(self):
        masses = b'Masses\n\n1 58.7 # deliberately not used to infer an element\n2 63.5\n\n'
        result = inspect(DATA.replace(masses, b'') + b'\n' + masses)
        self.assertEqual(result['masses_amu'], [58.7, 63.5])

    def test_atom_limit_and_byte_limit_are_enforced(self):
        self.reject(max_atoms=1)
        self.reject(max_bytes=len(DATA) - 1)
        self.assertEqual(inspect(max_atoms=2, max_bytes=len(DATA))['atom_count'], 2)
        self.reject(DATA.replace(b'2 atoms', b'1000001 atoms'), max_atoms=1000000)

    def test_invalid_explicit_options_are_rejected(self):
        changes = ({'units': None}, {'units': 'lj'}, {'boundary': None}, {'boundary': ['p', 'p']},
                   {'boundary': ['p', 'p', 's']}, {'boundary': ['p', 'p', True]},
                   {'type_elements': None}, {'type_elements': ['Cu', 'Cu']},
                   {'type_elements': ['Cu', 'Xx']}, {'type_elements': ['Cu', 1]},
                   {'max_atoms': True}, {'max_atoms': 0}, {'max_atoms': 1000001},
                   {'max_bytes': True}, {'max_bytes': 0}, {'max_bytes': 512*1024*1024+1})
        for change in changes:
            with self.subTest(change=change):
                self.reject(**change)

    def test_nonbytes_binary_and_controls_are_rejected(self):
        for data in ('plain text', bytearray(DATA), b'', DATA + b'\0', DATA + b'\xff', DATA + b'\x7f'):
            with self.subTest(kind=type(data).__name__):
                self.reject(data)

    def test_script_and_unknown_sections_are_rejected(self):
        for prefix in (b'units metal', b'shell echo unsafe', b'python unsafe'):
            with self.subTest(prefix=prefix):
                self.reject(prefix + b'\n' + DATA.partition(b'\n')[2])
        for section in (b'Velocities\n1 0 0 0', b'Pair Coeffs\n1 2', b'Bonds\n1 1 1 2',
                        b'run 0', b'include other.data', b'AtomsExtra\n1 2 3'):
            with self.subTest(section=section):
                self.reject(DATA + b'\n' + section + b'\n')

    def test_unknown_and_duplicate_headers_are_rejected(self):
        for header in (b'0 bonds', b'1 bond types', b'1 0 0 avec', b'run 0',
                       b'2 atoms', b'-1 3 xlo xhi', b'-3 2 -1 xy xz yz'):
            with self.subTest(header=header):
                self.reject(DATA.replace(b'\nMasses', b'\n' + header + b'\nMasses'))

    def test_missing_and_contradictory_counts_or_bounds_are_rejected(self):
        for before, after in ((b'2 atoms\n', b''), (b'2 atom types\n', b''),
                              (b'-1 3 xlo xhi\n', b''), (b'2 atoms', b'0 atoms'),
                              (b'2 atoms', b'3 atoms'), (b'2 atom types', b'1 atom types'),
                              (b'2 atom types', b'3 atom types')):
            with self.subTest(before=before, after=after):
                self.reject(DATA.replace(before, after))
        self.reject(DATA.partition(b'\n')[2].lstrip(b'\n'))  # First count cannot double as title.
        self.reject(DATA.partition(b'Atoms')[0])

    def test_duplicate_or_incomplete_sections_are_rejected(self):
        for data in (DATA + b'\nAtoms # atomic\n', DATA + b'\nMasses\n',
                     DATA.replace(b'2 63.5\n', b''), DATA.replace(b'1 58.7 # deliberately not used to infer an element\n', b''),
                     DATA.replace(b'1 2 0.4 0.7 3.6\n', b'')):
            with self.subTest(data=data[-40:]):
                self.reject(data)

    def test_duplicate_noncontinuous_and_nonpositive_ids_are_rejected(self):
        for identifier in (b'1', b'0', b'-1', b'3', b'1.5', b'nan'):
            with self.subTest(identifier=identifier):
                self.reject(DATA.replace(b'2 1 2.2', identifier + b' 1 2.2'))

    def test_invalid_types_are_rejected(self):
        for kind in (b'0', b'-1', b'3', b'1.5', b'inf'):
            with self.subTest(kind=kind):
                self.reject(DATA.replace(b'2 1 2.2', b'2 ' + kind + b' 2.2'))

    def test_nonfinite_numeric_data_is_rejected(self):
        for token in (b'nan', b'NaN', b'inf', b'-inf', b'1e999', b'1_000', b'0x1'):
            for before, after in ((b'0.4 0.7', token + b' 0.7'), (b'-1 3 xlo', b'-1 ' + token + b' xlo'),
                                  (b'-3 2 -1 xy', token + b' 2 -1 xy'), (b'2 63.5', b'2 ' + token)):
                with self.subTest(token=token, before=before):
                    self.reject(DATA.replace(before, after))

    def test_invalid_masses_are_rejected(self):
        for line in (b'1 63.5', b'0 63.5', b'3 63.5', b'2 0', b'2 -1', b'2 63.5 1'):
            with self.subTest(line=line):
                self.reject(DATA.replace(b'2 63.5', line))

    def test_wrong_styles_columns_or_image_flags_are_rejected(self):
        self.reject(DATA.replace(b'Atoms # atomic', b'Atoms # full'))
        self.reject(DATA.replace(b'Atoms # atomic', b'Atoms # charge'))
        self.reject(DATA.replace(b'1 2 0.4 0.7 3.6', b'1 2 0.4 0.7'))
        self.reject(DATA.replace(b'1 2 0.4 0.7 3.6', b'1 2 0.4 0.7 3.6 0 0'))
        self.reject(DATA.replace(b'1 2 0.4 0.7 3.6', b'1 2 0.4 0.7 3.6 0 0 0'))
        for token in (b'0.5', b'nan', b'512', b'-513', b'2147483648'):
            with self.subTest(token=token):
                data = DATA.replace(b'1 2 0.4 0.7 3.6', b'1 2 0.4 0.7 3.6 ' + token + b' 0 0').replace(
                    b'2 1 2.2 3.1 6.8', b'2 1 2.2 3.1 6.8 0 0 0')
                self.reject(data)

    def test_comments_cannot_be_glued_to_numeric_records_and_counts_are_unsigned(self):
        self.reject(DATA.replace(b'3.6\n', b'3.6#glued\n'))
        self.reject(DATA.replace(b'2 63.5\n', b'2 63.5#glued\n'))
        self.reject(DATA.replace(b'2 atoms', b'+2 atoms'))
        self.reject(DATA.replace(b'2 atom types', b'+2 atom types'))

    def test_negative_degenerate_overflow_boxes_and_malformed_tilts_are_rejected(self):
        for before, after in ((b'-1 3 xlo xhi', b'3 -1 xlo xhi'), (b'-1 3 xlo xhi', b'3 3 xlo xhi'),
                              (b'-1 3 xlo xhi', b'-1e308 1e308 xlo xhi'),
                              (b'-3 2 -1 xy xz yz', b'1e30 2 -1 xy xz yz'),
                              (b'-3 2 -1 xy xz yz', b'-3 2 xy xz yz'),
                              (b'-3 2 -1 xy xz yz', b'-3 2 -1 yz xz xy')):
            with self.subTest(after=after):
                self.reject(DATA.replace(before, after))
        self.reject(DATA.replace(b'-1 3 xlo xhi', b'0 1e-9 xlo xhi').replace(
            b'2 7 ylo yhi', b'0 1e-9 ylo yhi').replace(b'-3 9 zlo zhi', b'0 1e-9 zlo zhi'))

    def test_line_bound_prevents_unbounded_integer_or_comment_tokens(self):
        self.reject(b'x' * 65537 + b'\n' + DATA)
        self.reject(DATA + b'\n#' + b'x' * 65536 + b'\n')
        self.reject(DATA.replace(b'2 1 2.2', b'9' * 5000 + b' 1 2.2'))

    def test_engine_line_capacity_rejects_truncated_records_but_retains_long_comments(self):
        self.reject(DATA.replace(b'1 2 0.4 0.7 3.6', b'1 2 ' + b' ' * 254 + b'0.4 0.7 3.6'))
        long_comment = DATA.replace(b'1 2 0.4 0.7 3.6', b'1 2 0.4 0.7 3.6 #' + b'x' * 300)
        self.assertEqual(inspect(long_comment)['coordinate_content_sha256'], inspect()['coordinate_content_sha256'])

    def test_section_separator_and_contiguous_physical_record_contract(self):
        for before, after in ((b'Atoms # atomic\n\n', b'Atoms # atomic\n'),
                              (b'Masses\n\n', b'Masses\n'),
                              (b'1 2 0.4 0.7 3.6\n', b'1 2 0.4 0.7 3.6\n\n'),
                              (b'1 2 0.4 0.7 3.6\n', b'1 2 0.4 0.7 3.6\n# comment-only record\n'),
                              (b'2 63.5\n', b'\n2 63.5\n')):
            with self.subTest(before=before, after=after):
                self.reject(DATA.replace(before, after))
        self.assertEqual(inspect(DATA.replace(b'Atoms # atomic\n\n', b'Atoms # atomic\n# ignored separator\n'))[
            'coordinate_content_sha256'], inspect()['coordinate_content_sha256'])

    def test_fixed_box_membership_uses_triclinic_fractional_coordinates(self):
        self.reject(DATA.replace(b'2 1 2.2 3.1 6.8', b'2 1 2.2 3.1 10.8'))
        self.reject(DATA.replace(b'2 1 2.2 3.1 6.8', b'2 1 2.2 3.1 -3.1'))
        self.reject(DATA.replace(b'2 1 2.2 3.1 6.8', b'2 1 2.2 3.1 9'))
        self.assertTrue(inspect()['fixed_boundary_geometry_verified'])
        self.reject(DATA.replace(b'1 2 0.4 0.7 3.6', b'1 2 0.4 0.7 3.6 0 0 1').replace(
            b'2 1 2.2 3.1 6.8', b'2 1 2.2 3.1 6.8 0 0 0'))
        # Periodic out-of-box positions can be remapped by read_data; report it.
        periodic = inspect(DATA.replace(b'2 1 2.2 3.1 6.8', b'2 1 2.2 3.1 10.8'), boundary=['p', 'p', 'p'])
        self.assertTrue(periodic['periodic_remapping_may_occur'])

    def test_parser_has_no_file_network_subprocess_or_physical_interface(self):
        with patch('builtins.open', side_effect=AssertionError('no file access')), \
                patch('subprocess.Popen', side_effect=AssertionError('no process')), \
                patch('socket.socket', side_effect=AssertionError('no network')):
            self.assertEqual(inspect()['atom_count'], 2)

    @unittest.skipUnless(importlib.util.find_spec('ase'), 'optional existing geometry dependency unavailable')
    def test_real_existing_ase_output_matches_ase_readback_without_physics(self):
        import ase
        from ase.io.lammpsdata import read_lammps_data
        from auto_lammps.structures import build_structure
        spec = {'crystal': 'explicit_cell', 'cell_angstrom': [[4., 0., 0.], [-3., 5., 0.], [2., -1., 12.]],
                'site_elements': ['Cu', 'Ni'], 'scaled_positions': [[.1, .2, .3], [.7, .8, .9]],
                'repeat': [1, 1, 1], 'orientation': 'provided_axes', 'boundary': ['p', 'p', 'f'],
                'vacancies': [], 'substitutions': [], 'type_elements': ['Ni', 'Cu'], 'masses_amu': [58.7, 63.5]}
        with ExitStack() as stack:
            for method in ('get_potential_energy', 'get_forces', 'get_stress'):
                stack.enter_context(patch.object(ase.Atoms, method, side_effect=AssertionError('no physics')))
            stack.enter_context(patch('subprocess.Popen', side_effect=AssertionError('no process')))
            geometry = build_structure(spec, units='metal')
            result = inspect(geometry.data)
            atoms = read_lammps_data(StringIO(geometry.data.decode('ascii')), Z_of_type={1: 28, 2: 29},
                                     atom_style='atomic', units='metal')
        self.assertEqual(result['data_sha256'], geometry.receipt['data_sha256'])
        self.assertEqual(result['cell_angstrom'], atoms.cell.tolist())
        self.assertEqual(result['composition'], geometry.receipt['composition'])
        # ASE converts the data-file mass unit; the audit retains original values.
        for actual, read_back in zip(result['masses_amu'], [atoms.get_masses()[1], atoms.get_masses()[0]]):
            self.assertAlmostEqual(actual, float(read_back), delta=2e-8)
        self.assertLess(len(json.dumps(result)), 2500)


if __name__ == '__main__':
    unittest.main()
