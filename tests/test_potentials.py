"""Synthetic format/resource checks only; no engine or physical evaluation."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import unittest

from auto_lammps.ledger import Resources
from auto_lammps.manifest import ManifestError, freeze, sha256
from auto_lammps.potentials import PotentialAdapter, PotentialCatalog, PotentialError


class PotentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.files = {'coefficients': 'test.snapcoeff', 'parameters': 'test.snapparam', 'license': 'LICENSE'}
        self.coefficients = b'# Synthetic, not a physical model\n1 2\nCu 0.5 1\n0\n0\n'
        self.parameters = b'rcutfac 4\ntwojmax 0\n'
        (self.source / self.files['coefficients']).write_bytes(self.coefficients)
        (self.source / self.files['parameters']).write_bytes(self.parameters)
        (self.source / 'LICENSE').write_text('Synthetic test fixture; Apache-2.0\n')
        self.metadata = {'name': 'Synthetic Cu resource', 'format': 'snap', 'elements': ['Cu'],
                         'units': 'metal', 'source': {'url': 'https://example.org/synthetic',
                         'revision': 'a' * 40, 'locator': 'models/synthetic'},
                         'license': 'Apache-2.0', 'applicability': 'Synthetic resource checks only',
                         'usage_evidence': 'No scientific evidence; synthetic fixture', 'interaction': 'standalone'}
        self.catalog = PotentialCatalog(self.root / 'catalog')

    def ingest(self):
        return self.catalog.import_model(self.source, metadata=self.metadata, files=self.files)

    def adapter(self, pin, **overrides):
        args = dict(allowed_pins=[pin], software_sha256='b' * 64, packages=['ML-SNAP'])
        args.update(overrides)
        return PotentialAdapter(self.catalog, **args)

    def test_pin_survives_restart_and_duplicate_import_preserves_bytes(self):
        pin = self.ingest()
        self.assertEqual(pin, self.ingest())
        catalog = PotentialCatalog(self.root / 'catalog')
        record, content = catalog.read(pin)
        self.assertEqual(content['coefficients'], self.coefficients)
        self.assertEqual(record['status'], 'collected')
        self.assertEqual([x['pin'] for x in catalog.list_models()], [pin])
        self.assertEqual((self.root / 'catalog' / pin / 'record.json').stat().st_mode & 0o777, 0o400)
        self.metadata['name'] = 'New catalog revision'
        new_pin = self.ingest()
        self.assertNotEqual(pin, new_pin)
        self.assertEqual(catalog.read(pin)[0]['metadata']['name'], 'Synthetic Cu resource')

    def test_ordered_mapping_repeated_types_and_receipt_are_deterministic(self):
        pin = self.ingest()
        a = self.adapter(pin).resolve_potential(pin, type_elements=['Cu', 'Cu'], units='metal')
        b = self.adapter(pin).resolve_potential(pin, type_elements=['Cu', 'Cu'], units='metal')
        self.assertEqual(a, b)
        self.assertTrue(a.commands[1].endswith(' Cu Cu'))
        self.assertFalse(a.receipt['scientifically_verified'])
        self.assertFalse(a.receipt['execution_authorized'])
        self.assertFalse(a.receipt['environment_verified'])
        self.assertEqual(a.receipt['files'], {name: sha256(data) for name, data in a.files.items()})

    def test_binding_enters_existing_snapshot_with_license_and_hashes(self):
        pin = self.ingest()
        binding = self.adapter(pin).resolve_potential(pin, type_elements=['Cu'], units='metal')
        case = self.root / 'case'
        case.mkdir()
        for name, data in binding.files.items():
            path = case / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (case / 'in.lammps').write_text('units metal\n' + '\n'.join(binding.commands) + '\n')
        (case / 'binding.json').write_text(json.dumps(binding.receipt))
        snapshot = freeze(case, self.root / 'snapshots',
                          files={**{x: 'potential' for x in binding.files}, 'in.lammps': 'lammps_input',
                                 'binding.json': 'analysis_spec'}, entrypoint='in.lammps',
                          resources=Resources(cores=1, wall_seconds=60, memory_bytes=256000000, storage_bytes=100000),
                          provenance={x: 'b' * 64 for x in ('task_sha256', 'analysis_sha256', 'software_sha256')})
        manifest = snapshot.verify()
        self.assertEqual(len(manifest['files']), 5)
        for item in manifest['files']:
            if item['path'] in binding.files:
                self.assertEqual(item['sha256'], binding.receipt['files'][item['path']])

    def test_disallowed_pin_cannot_be_resolved(self):
        pin = self.ingest()
        with self.assertRaisesRegex(PotentialError, 'allowlist'):
            self.adapter(pin, allowed_pins=[]).resolve_potential(pin, type_elements=['Cu'], units='metal')

    def legacy_parameters(self):
        return (b'# preserved comment\r\nrcutfac 4\r\ntwojmax 0\r\nrfac0 0.99363\r\n'
                b'rmin0 0\r\n  diagonalstyle 3 # obsolete\r\nquadraticflag 0\r\nbzeroflag 0')

    def test_explicit_legacy_binding_preserves_original_and_records_exact_conversion(self):
        original = self.legacy_parameters()
        (self.source / self.files['parameters']).write_bytes(original)
        pin = self.ingest()
        self.assertEqual(self.adapter(pin).compatible_models(), [])
        with self.assertRaisesRegex(PotentialError, 'compatibility blocked'):
            self.adapter(pin).resolve_potential(pin, type_elements=['Cu'], units='metal')
        adapter = self.adapter(pin, legacy_snap_pins=[pin])
        binding = adapter.resolve_potential(pin, type_elements=['Cu', 'Cu'], units='metal')
        expected = original.replace(b'  diagonalstyle 3 # obsolete\r\n', b'')
        self.assertEqual(binding.files[f'potentials/{pin}/model.snapparam'], expected)
        self.assertEqual(self.catalog.read(pin)[1]['parameters'], original)
        self.assertEqual(binding.files[f'potentials/{pin}/model.snapcoeff'], self.coefficients)
        self.assertEqual(binding.files[f'potentials/{pin}/LICENSE.txt'], self.catalog.read(pin)[1]['license'])
        receipt = binding.receipt['compatibility_conversion']
        self.assertEqual(receipt['original_parameter_sha256'], sha256(original))
        self.assertEqual(receipt['bound_parameter_sha256'], sha256(expected))
        self.assertEqual(receipt['removed_line'], 6)
        self.assertEqual(receipt['unwritten_defaults_requiring_environment_review'], {'switchflag': 1})
        self.assertFalse(receipt['numerical_equivalence_verified'])
        self.assertEqual(adapter.compatible_models()[0]['pin'], pin)
        self.assertEqual(adapter.compatible_models(units='real'), [])
        self.assertEqual(binding, adapter.resolve_potential(pin, type_elements=['Cu', 'Cu'], units='metal'))

    def test_legacy_policy_cannot_broaden_resource_allowlist_or_guess_parameters(self):
        pin = self.ingest()
        with self.assertRaisesRegex(PotentialError, 'subset'):
            self.adapter(pin, legacy_snap_pins=['f' * 64])
        for value in (pin, None, {pin: True}):
            with self.subTest(value=value), self.assertRaises(PotentialError):
                self.adapter(pin, legacy_snap_pins=value)
        for parameters in (self.parameters, self.legacy_parameters().replace(b'diagonalstyle 3', b'diagonalstyle 2'),
                           self.legacy_parameters().replace(b'bzeroflag 0', b''),
                           self.legacy_parameters() + b'\nchemflag 0\n',
                           self.legacy_parameters() + b'\nunknown 1\n'):
            with self.subTest(parameters=parameters):
                (self.source / self.files['parameters']).write_bytes(parameters)
                pin = self.ingest()
                adapter = self.adapter(pin, legacy_snap_pins=[pin])
                with self.assertRaisesRegex(PotentialError, 'reviewed legacy'):
                    adapter.resolve_potential(pin, type_elements=['Cu'], units='metal')
                self.assertEqual(adapter.compatible_models(), [])

    def test_legacy_rule_does_not_bypass_units_packages_or_interaction_checks(self):
        (self.source / self.files['parameters']).write_bytes(self.legacy_parameters())
        pin = self.ingest()
        for kwargs, units in (({'packages': []}, 'metal'), ({}, 'real')):
            with self.assertRaises(PotentialError):
                self.adapter(pin, legacy_snap_pins=[pin], **kwargs).resolve_potential(pin, type_elements=['Cu'], units=units)
        self.metadata['interaction'] = 'unresolved'
        pin = self.ingest()
        with self.assertRaisesRegex(PotentialError, 'unresolved'):
            self.adapter(pin, legacy_snap_pins=[pin]).resolve_potential(pin, type_elements=['Cu'], units='metal')

    def test_explicit_switch_and_quadratic_coefficients_are_not_changed(self):
        original = self.legacy_parameters().replace(b'quadraticflag 0', b'quadraticflag 1') + b'\nswitchflag 0\n'
        coefficients = b'1 3\nCu 0.5 1\n0\n0\n0\n'
        (self.source / self.files['parameters']).write_bytes(original)
        (self.source / self.files['coefficients']).write_bytes(coefficients)
        pin = self.ingest()
        binding = self.adapter(pin, legacy_snap_pins=[pin]).resolve_potential(pin, type_elements=['Cu'], units='metal')
        self.assertEqual(binding.files[f'potentials/{pin}/model.snapcoeff'], coefficients)
        self.assertEqual(binding.files[f'potentials/{pin}/model.snapparam'], original.replace(b'  diagonalstyle 3 # obsolete\r\n', b''))
        self.assertEqual(binding.receipt['compatibility_conversion']['unwritten_defaults_requiring_environment_review'], {})

    def test_concurrent_duplicate_import_has_one_immutable_result(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            pins = list(pool.map(lambda _: self.ingest(), range(8)))
        self.assertEqual(len(set(pins)), 1)
        self.assertEqual(len(self.catalog.list_models()), 1)
        self.assertEqual(list(self.catalog.directory.glob('.import-*')), [])

    def test_multielement_type_order_is_not_sorted_or_inferred(self):
        (self.source / self.files['coefficients']).write_bytes(b'2 2\nCu 0.5 1\n0\n0\nNi 0.5 1\n0\n0\n')
        self.metadata['elements'] = ['Cu', 'Ni']
        pin = self.ingest()
        binding = self.adapter(pin).resolve_potential(pin, type_elements=['Ni', 'Cu', 'Ni'], units='metal')
        self.assertTrue(binding.commands[1].endswith(' Ni Cu Ni'))
        self.assertEqual(binding.receipt['atom_type_elements'], ['Ni', 'Cu', 'Ni'])

    def test_mapping_units_and_packages_fail_closed(self):
        pin = self.ingest()
        for elements in ([], ['NULL'], ['Fe'], ['Cu\nrun 0'], 'Cu', [None]):
            with self.subTest(elements=elements), self.assertRaises(PotentialError):
                self.adapter(pin).resolve_potential(pin, type_elements=elements, units='metal')
        with self.assertRaisesRegex(PotentialError, 'units'):
            self.adapter(pin).resolve_potential(pin, type_elements=['Cu'], units='real')
        with self.assertRaisesRegex(PotentialError, 'ML-SNAP'):
            self.adapter(pin, packages=[]).resolve_potential(pin, type_elements=['Cu'], units='metal')

    def test_legacy_file_is_preserved_but_never_silently_adapted(self):
        legacy = self.parameters + b'diagonalstyle 3\n'
        (self.source / self.files['parameters']).write_bytes(legacy)
        pin = self.ingest()
        record, content = self.catalog.read(pin)
        self.assertEqual(content['parameters'], legacy)
        self.assertIn('legacy_diagonalstyle_requires_version_review', record['inspection']['blockers'])
        with self.assertRaisesRegex(PotentialError, 'compatibility blocked'):
            self.adapter(pin).resolve_potential(pin, type_elements=['Cu'], units='metal')

    def test_unsupported_variants_are_not_presented_as_standalone(self):
        for suffix in (b'chemflag 1\n', b'switchinnerflag 1\n', b'futureflag 1\n'):
            with self.subTest(suffix=suffix):
                (self.source / self.files['parameters']).write_bytes(self.parameters + suffix)
                pin = self.ingest()
                with self.assertRaisesRegex(PotentialError, 'compatibility blocked'):
                    self.adapter(pin).resolve_potential(pin, type_elements=['Cu'], units='metal')
        (self.source / self.files['parameters']).write_bytes(self.parameters)
        for interaction in ('hybrid', 'unresolved'):
            self.metadata['interaction'] = interaction
            pin = self.ingest()
            with self.assertRaisesRegex(PotentialError, 'interactions'):
                self.adapter(pin).resolve_potential(pin, type_elements=['Cu'], units='metal')

    def test_corrupt_coefficients_and_element_claims_rejected(self):
        for data in (b'1 3\nCu 0.5 1\n0\n0\n', self.coefficients.replace(b'0.5', b'NaN'),
                     self.coefficients.replace(b'Cu', b'Fe'), b'1 0\nCu 0.5 1\n'):
            with self.subTest(data=data), self.assertRaises(PotentialError):
                (self.source / self.files['coefficients']).write_bytes(data)
                self.ingest()

    def test_invalid_parameters_rejected(self):
        for data in (b'twojmax 0\n', b'rcutfac 4\ntwojmax 0\ntwojmax 2\n',
                     b'rcutfac inf\ntwojmax 0\n', b'rcutfac -1\ntwojmax 0\n',
                     b'rcutfac 4\ntwojmax 0.5\n', self.parameters + b'chemflag 2\n'):
            with self.subTest(data=data), self.assertRaises(PotentialError):
                (self.source / self.files['parameters']).write_bytes(data)
                self.ingest()

    def test_model_and_record_tampering_rejected(self):
        pin = self.ingest()
        path = self.catalog.directory / pin / self.files['coefficients']
        path.chmod(0o600)
        path.write_bytes(self.coefficients.replace(b'0.5', b'0.6'))
        with self.assertRaisesRegex(PotentialError, 'hash mismatch'):
            self.adapter(pin).resolve_potential(pin, type_elements=['Cu'], units='metal')
        path.write_bytes(self.coefficients)
        record = self.catalog.directory / pin / 'record.json'
        record.chmod(0o600)
        record.write_bytes(record.read_bytes() + b' ')
        with self.assertRaisesRegex(PotentialError, 'hash mismatch'):
            self.catalog.read(pin)

    def test_descriptor_dimensions_and_quadratic_count(self):
        for angular, count in ((0, 2), (1, 3), (2, 6), (3, 9), (6, 31), (8, 56)):
            (self.source / self.files['coefficients']).write_text(f'1 {count}\nCu 0.5 1\n' + '0\n' * count)
            (self.source / self.files['parameters']).write_text(f'rcutfac 4\ntwojmax {angular}\n')
            self.ingest()
            (self.source / self.files['parameters']).write_text(f'rcutfac 4\ntwojmax {angular + 1}\n')
            with self.assertRaisesRegex(PotentialError, 'descriptor parameters'):
                self.ingest()
        (self.source / self.files['coefficients']).write_bytes(b'1 3\nCu 0.5 1\n0\n0\n0\n')
        (self.source / self.files['parameters']).write_bytes(self.parameters + b'quadraticflag 1\n')
        self.ingest()

    def test_extra_file_and_symlink_catalog_entry_rejected(self):
        pin = self.ingest()
        path = self.catalog.directory / pin
        (path / 'author-target.py').write_text('not allowed')
        with self.assertRaisesRegex(PotentialError, 'Undeclared'):
            self.catalog.read(pin)
        (path / 'author-target.py').unlink()
        moved = self.root / 'moved'
        path.rename(moved)
        path.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(ManifestError):
            self.catalog.read(pin)

    def test_source_symlink_and_hardlink_rejected(self):
        path = self.source / self.files['coefficients']
        moved = self.root / 'external'
        path.rename(moved)
        path.symlink_to(moved)
        with self.assertRaises(ManifestError):
            self.ingest()
        path.unlink()
        os.link(moved, path)
        with self.assertRaises(ManifestError):
            self.ingest()

    def test_unsafe_filenames_metadata_and_unpinned_sources_rejected(self):
        for name in ('../LICENSE', 'a/b', '$(run)', 'record.json', '/tmp/model'):
            with self.subTest(name=name), self.assertRaises(PotentialError):
                self.catalog.import_model(self.source, metadata=self.metadata,
                                          files={**self.files, 'license': name})
        for url in ('http://example.org/model', 'https://user:secret@example.org/model',
                    'https://example.org/model?token=secret'):
            metadata = copy.deepcopy(self.metadata)
            metadata['source']['url'] = url
            with self.assertRaises(PotentialError):
                self.catalog.import_model(self.source, metadata=metadata, files=self.files)
        self.metadata['source']['revision'] = 'main'
        with self.assertRaises(PotentialError):
            self.ingest()


if __name__ == '__main__':
    unittest.main()
