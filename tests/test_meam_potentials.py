"""Synthetic MEAM resources only; no physical evaluation or upstream model data."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from auto_lammps.potentials import PotentialAdapter, PotentialCatalog, PotentialError
from auto_lammps.manifest import sha256


LIBRARY = (b"# Synthetic nonphysical library; deliberately reverse row order\n"
           b"'Ni' 'fcc' 12 28 58.69\n4 2 2 2 2 3.5 4 1\n1 1 1 1 1 3\n"
           b"'Cu' 'fcc' 12 29 63.546\n4 2 2 2 2 3.6 4 1\n1 1 1 1 1 3\n")
PARAMETERS = b"rc = 5\ndelr = 0.1\naugt1 = 0\nEc(1,2) = 3\nCmin(1,2,1) = 1\nlattce(1,2) = 'fcc'\n"
FILES = {'library': 'test.library', 'parameters': 'test.parameters', 'license': 'LICENSE'}
METADATA = {'name': 'Synthetic MEAM', 'format': 'meam', 'elements': ['Cu', 'Ni'], 'units': 'metal',
            'source': {'url': 'https://example.org/synthetic', 'revision': 'a' * 40, 'locator': 'test'},
            'license': 'Apache-2.0', 'applicability': 'Synthetic only; not a physical model',
            'usage_evidence': 'No scientific validation', 'interaction': 'standalone'}


class MeamPotentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.library = self.source / FILES['library']
        self.parameters = self.source / FILES['parameters']
        self.library.write_bytes(LIBRARY)
        self.parameters.write_bytes(PARAMETERS)
        (self.source / 'LICENSE').write_text('Synthetic fixture; Apache-2.0')
        self.metadata = copy.deepcopy(METADATA)
        self.catalog = PotentialCatalog(self.root / 'catalog')

    def ingest(self):
        return self.catalog.import_model(self.source, metadata=self.metadata, files=FILES)

    def adapter(self, pin, **kwargs):
        options = dict(allowed_pins=[pin], software_sha256='b' * 64, packages=['MEAM'])
        options.update(kwargs)
        return PotentialAdapter(self.catalog, **options)

    def test_library_index_order_is_independent_of_rows_and_atom_types(self):
        pin = self.ingest()
        with patch('subprocess.Popen', side_effect=AssertionError('no evaluation')):
            binding = self.adapter(pin).resolve_potential(pin, type_elements=['Ni', 'Cu', 'Ni'], units='metal')
        self.assertEqual(binding.commands, ('pair_style meam',
            f'pair_coeff * * potentials/{pin}/library.meam Cu Ni potentials/{pin}/model.meam Ni Cu Ni'))
        self.assertEqual(binding.receipt['library_index_elements'], ['Cu', 'Ni'])
        self.assertEqual(binding.receipt['atom_type_elements'], ['Ni', 'Cu', 'Ni'])
        self.assertEqual(binding.files[f'potentials/{pin}/library.meam'], LIBRARY)
        self.assertEqual(binding.files[f'potentials/{pin}/model.meam'], PARAMETERS)
        self.assertEqual(binding.receipt['files'], {p: sha256(b) for p, b in binding.files.items()})
        for key in ('execution_authorized', 'environment_verified', 'scientifically_verified'):
            self.assertFalse(binding.receipt[key])
        self.assertNotIn('compatibility_conversion', binding.receipt)

    def test_immutable_import_reopen_and_selected_order_changes_pin(self):
        pin = self.ingest()
        self.assertEqual(pin, self.ingest())
        record, data = PotentialCatalog(self.root / 'catalog').read(pin)
        self.assertEqual(data['library'], LIBRARY)
        self.assertEqual(record['inspection']['library_entries']['Cu']['entry'], 2)
        self.metadata['elements'].reverse()
        self.assertNotEqual(pin, self.ingest())
        self.assertEqual(self.catalog.read(pin)[0]['metadata']['elements'], ['Cu', 'Ni'])

    def test_unavailable_package_units_policy_or_mapping_cannot_bind(self):
        pin = self.ingest()
        for options in ({'packages': ['ML-SNAP']}, {'allowed_pins': []}, {'legacy_snap_pins': [pin]}):
            with self.subTest(options=options), self.assertRaises(PotentialError):
                self.adapter(pin, **options).resolve_potential(pin, type_elements=['Cu'], units='metal')
            self.assertEqual(self.adapter(pin, **options).compatible_models(), [])
        self.assertEqual(self.adapter(pin).compatible_models(units='real'), [])
        for mapping in ([], ['NULL'], ['Sn'], ['Cu\nrun 0']):
            with self.subTest(mapping=mapping), self.assertRaises(PotentialError):
                self.adapter(pin).resolve_potential(pin, type_elements=mapping, units='metal')
        with self.assertRaises(PotentialError):
            self.adapter(pin).resolve_potential(pin, type_elements=['Cu'], units='real')
        self.metadata['units'] = 'real'
        with self.assertRaises(PotentialError):
            self.ingest()

    def test_missing_and_duplicate_selected_entries_do_not_silently_bind(self):
        self.metadata['elements'] = ['Sn']
        with self.assertRaisesRegex(PotentialError, 'missing'):
            self.ingest()
        self.metadata['elements'] = ['Cu', 'Ni']
        self.library.write_bytes(LIBRARY + LIBRARY)
        pin = self.ingest()
        self.assertIn('duplicate_selected_library_element:Cu', self.catalog.read(pin)[0]['inspection']['blockers'])
        self.assertEqual(self.adapter(pin).compatible_models(), [])

    def test_malformed_library_and_nonfinite_numbers_rejected(self):
        for raw in (LIBRARY + b'1', LIBRARY.replace(b'63.546', b'NaN'),
                    LIBRARY.replace(b'63.546', b'1e999'), LIBRARY.replace(b'63.546', b'-1'),
                    LIBRARY.replace(b' 29 ', b' 29.5 '), b"'Cu fcc 12", LIBRARY + b'\xff'):
            with self.subTest(raw=raw), self.assertRaises(PotentialError):
                self.library.write_bytes(raw)
                self.ingest()

    def test_nonunit_t0_and_unimplemented_ibar_are_blocked(self):
        for tail in (b'2 1 1 1 1 3', b'1 1 1 1 1 2'):
            self.library.write_bytes(LIBRARY.replace(b'1 1 1 1 1 3', tail))
            pin = self.ingest()
            self.assertEqual(self.adapter(pin).compatible_models(), [])

    def test_parameter_indices_cardinality_duplicates_and_values_rejected(self):
        for extra in (b'Ec(0,1)=1', b'Ec(3,1)=1', b'Ec(1)=1', b'rc(1)=5',
                      b'Ec(1, 2)=4', b'rc=6', b'rho0(1)=NaN', b'rc=1e999',
                      b'nn2(1,1)=2', b'ialloy=3', b're(1,1)=-1',
                      b"lattce(1,1)='unknown'", b'run 0', b'theta(1,1)=180 2'):
            with self.subTest(extra=extra), self.assertRaises(PotentialError):
                self.parameters.write_bytes(PARAMETERS + extra + b'\n')
                self.ingest()
        self.parameters.write_bytes(b'# no assignments\n')
        with self.assertRaises(PotentialError):
            self.ingest()

    def test_unknown_parameter_retained_but_not_offered(self):
        self.parameters.write_bytes(PARAMETERS + b'futureflag(1)=1\n')
        pin = self.ingest()
        self.assertEqual(self.catalog.read(pin)[1]['parameters'], PARAMETERS + b'futureflag(1)=1\n')
        self.assertEqual(self.adapter(pin).compatible_models(), [])

    def test_hybrid_unresolved_and_more_than_eight_elements_are_not_supported(self):
        for interaction in ('hybrid', 'unresolved'):
            self.metadata['interaction'] = interaction
            pin = self.ingest()
            self.assertEqual(self.adapter(pin).compatible_models(), [])
        self.metadata['elements'] = ['H', 'He', 'Li', 'Be', 'B', 'C', 'N', 'O', 'F']
        with self.assertRaisesRegex(PotentialError, 'eight'):
            self.ingest()

    def test_original_file_tamper_and_wrong_roles_rejected(self):
        pin = self.ingest()
        path = self.catalog.directory / pin / FILES['library']
        path.chmod(0o600)
        path.write_bytes(LIBRARY.replace(b'63.546', b'63.000'))
        with self.assertRaisesRegex(PotentialError, 'hash mismatch'):
            self.adapter(pin).resolve_potential(pin, type_elements=['Cu'], units='metal')
        with self.assertRaises(PotentialError):
            self.catalog.import_model(self.source, metadata=self.metadata,
                files={'coefficients': FILES['library'], 'parameters': FILES['parameters'], 'license': 'LICENSE'})


if __name__ == '__main__':
    unittest.main()
