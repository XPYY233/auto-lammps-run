"""Original synthetic setfl fixtures only; no engine, network or model requests."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from auto_lammps.ledger import Resources
from auto_lammps.manifest import ManifestError, freeze, sha256
from auto_lammps.potentials import PotentialAdapter, PotentialCatalog, PotentialError, inspect_eam_alloy


MODEL = (b'# Synthetic fixture, not a physical model\n# UNITS: metal\n# original test data\n'
         b'3 Ni Co Cr\n3 0.1 4 0.2 0.6\n'
         b'28 58.69 0 dummy\n0 1 2\n0 1\n2 3\n'
         b'27 58.93 3.5 FCC\n0 1 2\n0 1 2 3\n'
         b'24 52.00 2.8 BCC\n0 1 2\n0 1 2 3\n'
         + b'0 1 2 3\n' * 6)
FILES = dict(model='synthetic.setfl', license='LICENSE')
METADATA = dict(name='Synthetic Ni-Co-Cr EAM/alloy', format='eam/alloy', elements=['Ni', 'Co', 'Cr'],
                units='metal', source=dict(url='https://example.org/synthetic', revision='c' * 40,
                                          locator='synthetic.setfl'),
                license='Apache-2.0 synthetic fixture', applicability='Parser tests only',
                usage_evidence='No physical validation', interaction='standalone')


class EAMPotentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'; self.source.mkdir()
        (self.source / FILES['model']).write_bytes(MODEL)
        (self.source / FILES['license']).write_text('Apache-2.0; synthetic fixture only\n')
        self.catalog = PotentialCatalog(self.root / 'catalog')

    def ingest(self, **changes):
        return self.catalog.import_model(self.source, metadata={**deepcopy(METADATA), **changes}, files=FILES)

    def adapter(self, pin, **changes):
        return PotentialAdapter(self.catalog, **dict(allowed_pins=[pin], software_sha256='b' * 64,
                                                    packages=['MANYBODY']) | changes)

    def test_array_boundaries_header_order_and_dummy_lattice_are_preserved(self):
        result = inspect_eam_alloy(MODEL, METADATA['elements'])
        self.assertEqual(result['elements'], ['Ni', 'Co', 'Cr'])
        self.assertEqual(result['pair_tables'], 6)
        self.assertEqual(result['element_headers'][0]['lattice_constant'], 0)
        self.assertEqual(result['element_headers'][0]['lattice_type'], 'dummy')
        self.assertEqual(result['blockers'], [])

    def test_original_bytes_restart_and_reordered_repeated_atom_types(self):
        pin = self.ingest()
        self.assertEqual(pin, self.ingest())
        self.assertEqual(PotentialCatalog(self.catalog.directory).read(pin)[1]['model'], MODEL)
        binding = self.adapter(pin).resolve_potential(pin, type_elements=['Co', 'Cr', 'Ni', 'Ni'], units='metal')
        self.assertEqual(binding.commands, ('pair_style eam/alloy',
                         f'pair_coeff * * potentials/{pin}/model.eam.alloy Co Cr Ni Ni'))
        self.assertEqual(binding.files[f'potentials/{pin}/model.eam.alloy'], MODEL)
        self.assertEqual(binding.receipt['model_element_order'], ['Ni', 'Co', 'Cr'])
        self.assertEqual(binding.receipt['atom_type_elements'], ['Co', 'Cr', 'Ni', 'Ni'])
        for key in ('execution_authorized', 'environment_verified', 'scientifically_verified'):
            self.assertFalse(binding.receipt[key])
        case = self.root / 'case'; case.mkdir()
        for name, value in binding.files.items():
            target = case / name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(value)
        (case / 'in.lammps').write_text('units metal\n' + '\n'.join(binding.commands) + '\n')
        snapshot = freeze(case, self.root / 'snapshots',
                          files={**{name:'potential' for name in binding.files}, 'in.lammps':'lammps_input'},
                          entrypoint='in.lammps', resources=Resources(1,60,256000000,100000),
                          provenance={key:'b'*64 for key in ('task_sha256','analysis_sha256','software_sha256')})
        manifest = snapshot.verify()
        self.assertEqual(next(row['sha256'] for row in manifest['files']
                             if row['path'].endswith('model.eam.alloy')), sha256(MODEL))

    def test_invalid_setfl_rejected_without_catalog_records(self):
        cases = [MODEL[:-4], MODEL + b'0\n', MODEL.replace(b'3 Ni Co Cr', b'3 Ni Co Ni'),
                 MODEL.replace(b'3 Ni Co Cr', b'2 Ni Co Cr'), MODEL.replace(b'3 0.1 4', b'3 -0.1 4'),
                 MODEL.replace(b'0 1 2\n', b'nan 1 2\n', 1),
                 MODEL.replace(b'0 1 2\n', b'1e999 1 2\n', 1),
                 MODEL.replace(b'0 1 2\n', b'1D+00 1 2\n', 1),
                 MODEL.replace(b'28 58.69', b'28 0'), MODEL.replace(b'UNITS: metal', b'UNITS: real'),
                 MODEL.replace(b'dummy', b'bad;command'), MODEL.replace(b'0 1 2\n', b'0 1\n', 1),
                 MODEL.replace(b'# original test data', b'#' + b'x'*1023),
                 MODEL.replace(b'0 1 2\n', b'0\v1 2\n', 1)]
        for data in cases:
            with self.subTest(data=data[:110]), self.assertRaises(PotentialError):
                (self.source / FILES['model']).write_bytes(data)
                self.ingest()
        self.assertEqual(self.catalog.list_models(), [])
        with self.assertRaises(PotentialError):inspect_eam_alloy(MODEL, ['Co','Cr','Ni'])

    def test_array_last_line_extra_value_matches_engine_reading_and_is_reported(self):
        # Embedding has three values; the finite fourth value is discarded by
        # next_dvector. Density and subsequent element headers must not shift.
        padded = MODEL.replace(b'0 1 2\n', b'0 1 2 999\n', 1)
        result = inspect_eam_alloy(padded, METADATA['elements'])
        self.assertEqual(result['warnings'], ['array_line_tail_ignored'])
        self.assertEqual(result['ignored_line_tails'],
                         [dict(array='embedding:Ni', line=7, count=1)])
        self.assertEqual(result['element_headers'][1]['element'], 'Co')
        self.assertEqual(result['pair_tables'], 6)
        (self.source / FILES['model']).write_bytes(padded)
        pin = self.ingest()
        binding = self.adapter(pin).resolve_potential(pin, type_elements=['Co','Cr','Ni'], units='metal')
        self.assertEqual(binding.files[f'potentials/{pin}/model.eam.alloy'], padded)
        self.assertEqual(binding.receipt['potential_warnings'], result['warnings'])
        self.assertEqual(binding.receipt['ignored_line_tails'], result['ignored_line_tails'])
        for value in (b'nan', b'1e999', b'bad'):
            with self.subTest(value=value), self.assertRaises(PotentialError):
                inspect_eam_alloy(padded.replace(b'999\n', value + b'\n'), METADATA['elements'])

    def test_binding_rejects_missing_capability_units_allowlist_hybrid_and_legacy_conversion(self):
        pin = self.ingest()
        for overrides in [dict(packages=[]), dict(allowed_pins=[]), dict(legacy_snap_pins=[pin])]:
            adapter = self.adapter(pin, **overrides)
            with self.assertRaises(PotentialError):adapter.resolve_potential(pin, type_elements=['Ni'], units='metal')
            self.assertEqual(adapter.compatible_models(), [])
        with self.assertRaises(PotentialError):self.ingest(units='real')
        for elements in [['NULL'],['Fe']]:
            with self.assertRaises(PotentialError):
                self.adapter(pin).resolve_potential(pin,type_elements=elements,units='metal')
        with self.assertRaises(PotentialError):
            self.adapter(pin).resolve_potential(pin,type_elements=['Ni'],units='real')
        for interaction in ['hybrid','unresolved']:
            other=self.ingest(interaction=interaction)
            with self.assertRaises(PotentialError):self.adapter(other).resolve_potential(other,type_elements=['Ni'],units='metal')

    def test_catalog_tampering_license_and_element_declaration_are_not_bypassed(self):
        with self.assertRaises(PotentialError):self.ingest(elements=['Co','Cr','Ni'])
        (self.source / FILES['license']).write_text('')
        with self.assertRaises(PotentialError):self.ingest()
        (self.source / FILES['license']).write_text('Apache-2.0; synthetic fixture only\n')
        pin = self.ingest()
        path=self.catalog.directory / pin / FILES['model']; path.chmod(0o600); path.write_bytes(MODEL+b'0\n')
        with self.assertRaises((PotentialError, ManifestError)):self.catalog.read(pin)
