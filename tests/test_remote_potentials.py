"""Synthetic HPC file-store fixtures only; no model, network or physics evaluation."""
import base64
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from auto_lammps import remote_stage
from auto_lammps.ledger import Resources
from auto_lammps.manifest import (ManifestError, canonical, freeze, sha256,
                                 validate_file_record)
from auto_lammps.potentials import PotentialAdapter, PotentialCatalog, PotentialError
from auto_lammps.remote_potentials import (CombinedPotentialCatalog,
    PotentialCatalogClient, RemotePotentialCatalog, fixed_binding,
    register_catalog_version, save_metadata_view, validate_entry, validate_view)
from auto_lammps.staging import StageClient, StageEndpoint, upload_chunks
from test_eam_potentials import FILES as EAM_FILES, METADATA as EAM_META, MODEL
from test_meam_potentials import (FILES as MEAM_FILES, LIBRARY, METADATA as MEAM_META,
                                 PARAMETERS)


LICENSE = b'Apache-2.0; synthetic fixture only\n'
INPUT = b'# Synthetic inert input, never executed\n'
SNAP_META = dict(name='Synthetic remote SNAP', format='snap', elements=['Cu'],
    units='metal', source=dict(url='https://example.org/synthetic', revision='a'*40,
                             locator='models/synthetic'),
    license='Apache-2.0', applicability='File protocol tests only',
    usage_evidence='No physical validation', interaction='standalone')
SNAP_FILES = dict(coefficients='synthetic.snapcoeff', parameters='synthetic.snapparam',
                  license='LICENSE')
SNAP_COEFFICIENTS = b'# Synthetic nonphysical coefficients\n1 2\nCu 0.5 1\n0\n0\n'
SNAP_PARAMETERS = b'rcutfac 4\ntwojmax 0\n'
FIXTURES = {
    'eam/alloy': (EAM_META, EAM_FILES, dict(model=MODEL, license=LICENSE)),
    'meam': (MEAM_META, MEAM_FILES,
             dict(library=LIBRARY, parameters=PARAMETERS, license=LICENSE)),
    'snap': (SNAP_META, SNAP_FILES,
             dict(coefficients=SNAP_COEFFICIENTS, parameters=SNAP_PARAMETERS, license=LICENSE)),
}


class RemotePotentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        # Even accidental spawning fails: fixtures imitate only an HPC file store.
        no_process = patch('subprocess.Popen', side_effect=AssertionError('No physics or network'))
        no_process.start(); self.addCleanup(no_process.stop)
        self.hpc = PotentialCatalog(self.root/'hpc-potentials')
        self.pin = self.import_fixture('eam/alloy')
        self.refresh()
        self.case = self.root/'case'; self.case.mkdir()
        (self.case/'input.in').write_bytes(INPUT)
        self.serial = 0

    def import_fixture(self, fmt, *, catalog=None, name=None):
        metadata, files, content = deepcopy(FIXTURES[fmt])
        if name is not None:
            metadata['name'] = name
        source = self.root/('source-'+fmt.replace('/', '-'))
        source.mkdir(exist_ok=True)
        for role, value in content.items():
            (source/files[role]).write_bytes(value)
        return (catalog or self.hpc).import_model(source, metadata=metadata, files=files)

    def refresh(self):
        self.view = register_catalog_version(self.hpc.directory)
        self.cache = save_metadata_view(self.view, self.root/'metadata-cache')
        self.remote = RemotePotentialCatalog(self.cache)

    def adapter(self, catalog=None, **changes):
        options = dict(allowed_pins=[self.pin], software_sha256='b'*64,
                       packages=['MANYBODY', 'MEAM', 'ML-SNAP'])
        options.update(changes)
        return PotentialAdapter(catalog or self.remote, **options)

    def binding(self):
        return self.adapter().resolve_potential(self.pin, type_elements=['Co', 'Cr', 'Ni', 'Ni'],
                                               units='metal')

    def snapshot(self, external=None):
        return freeze(self.case, self.root/'snapshots', files={'input.in':'lammps_input'},
            external_files=self.binding().remote_files if external is None else external,
            entrypoint='input.in', resources=Resources(1, 60, 2097152, 65536),
            provenance={key:'b'*64 for key in ('task_sha256', 'analysis_sha256', 'software_sha256')})

    def target(self):
        self.serial += 1
        target = self.root/('requests-'+str(self.serial)); target.mkdir(mode=0o700)
        (target/'policy.json').write_bytes(canonical(dict(schema_version=1,
            max_total_bytes=1048576, approval_sha256='d'*64)))
        return target

    def receive(self, snapshot, target):
        return remote_stage.receive(str(target), 'e'*32, snapshot.digest,
            io.BytesIO(b''.join(upload_chunks(snapshot))), _potential_catalog=self.hpc.directory)

    def assert_stage_rejected(self, snapshot):
        target = self.target()
        with self.assertRaises((remote_stage.StageError, OSError)):
            self.receive(snapshot, target)
        self.assertFalse((target/('e'*32)/'stage.json').exists())
        return target

    def repin(self, entry):
        entry['pin'] = sha256(canonical(entry['record']))
        return entry

    def test_catalog_exports_metadata_only_and_preserves_immutable_versions(self):
        inspected = remote_stage.inspect_potential_catalog(_potential_catalog=self.hpc.directory)
        self.assertEqual(inspected, self.view)
        entry = inspected['entries'][0]
        self.assertEqual(set(entry), {'pin', 'record'})
        self.assertEqual(entry['record']['files']['model'],
            dict(name=EAM_FILES['model'], size=len(MODEL), sha256=sha256(MODEL)))
        self.assertEqual(self.remote.read(self.pin), (self.hpc.read(self.pin)[0], {}))
        self.assertEqual(set(path.name for path in self.cache.iterdir()), {'catalog-view.json'})
        self.assertEqual((self.cache/'catalog-view.json').stat().st_mode & 0o777, 0o400)
        self.assertNotIn('paths', canonical(inspected).decode())
        old_version = self.hpc.directory/'versions'/(self.view['catalog_sha256']+'.json')
        old_bytes = old_version.read_bytes()
        self.assertEqual(self.view, register_catalog_version(self.hpc.directory))
        self.import_fixture('eam/alloy', name='Synthetic later version')
        newer = register_catalog_version(self.hpc.directory)
        self.assertNotEqual(newer['catalog_sha256'], self.view['catalog_sha256'])
        self.assertEqual(old_version.read_bytes(), old_bytes)
        self.assertEqual(old_version.stat().st_mode & 0o777, 0o400)

    def test_returned_records_cannot_change_later_binding_or_catalog(self):
        original = self.remote.read(self.pin)[0]
        record, _ = self.remote.read(self.pin)
        record['metadata']['elements'] = ['Fe']
        record['files']['model']['sha256'] = 'f'*64
        listed = self.remote.list_models()
        listed[0]['record']['inspection']['blockers'].append('changed by caller')
        self.assertEqual(self.remote.read(self.pin)[0], original)
        self.assertEqual(self.binding().receipt['atom_type_elements'], ['Co', 'Cr', 'Ni', 'Ni'])
        # The public metadata view is not an authority after construction either.
        self.remote.view['entries'][0]['record']['metadata']['elements'] = ['Fe']
        self.assertEqual(self.remote.read(self.pin)[0], original)

    def test_three_formats_have_fixed_names_commands_and_explicit_type_order(self):
        pins = {'eam/alloy':self.pin, 'meam':self.import_fixture('meam'),
                'snap':self.import_fixture('snap')}
        self.refresh()
        cases = [('eam/alloy', ['Co', 'Cr', 'Ni', 'Ni'], {'model':'model.eam.alloy'}),
                 ('meam', ['Ni', 'Cu', 'Ni'], {'library':'library.meam', 'parameters':'model.meam'}),
                 ('snap', ['Cu', 'Cu'], {'coefficients':'model.snapcoeff', 'parameters':'model.snapparam'})]
        for fmt, mapping, names in cases:
            with self.subTest(format=fmt):
                pin = pins[fmt]
                binding = self.adapter(allowed_pins=[pin]).resolve_potential(pin,
                    type_elements=mapping, units='metal')
                prefix = 'potentials/'+pin
                self.assertEqual(binding.files, {})
                self.assertEqual(set(binding.remote_files),
                                 {prefix+'/'+name for name in [*names.values(), 'LICENSE.txt']})
                commands = [f'pair_style {fmt}']
                if fmt == 'meam':
                    commands.append(f'pair_coeff * * {prefix}/library.meam Cu Ni {prefix}/model.meam Ni Cu Ni')
                    self.assertEqual(binding.receipt['library_index_elements'], ['Cu', 'Ni'])
                elif fmt == 'snap':
                    commands.append(f'pair_coeff * * {prefix}/model.snapcoeff {prefix}/model.snapparam Cu Cu')
                else:
                    commands.append(f'pair_coeff * * {prefix}/model.eam.alloy Co Cr Ni Ni')
                    self.assertEqual(binding.receipt['model_element_order'], ['Ni', 'Co', 'Cr'])
                self.assertEqual(binding.commands, tuple(commands))
                self.assertEqual(binding.receipt['atom_type_elements'], mapping)
                self.assertEqual(binding.receipt['files'],
                    {name:item['sha256'] for name,item in binding.remote_files.items()})
                for key in ('execution_authorized', 'environment_verified', 'scientifically_verified'):
                    self.assertFalse(binding.receipt[key])
                for name,item in binding.remote_files.items():
                    self.assertEqual(item['path'], name)
                    self.assertEqual(item['external_source']['kind'], 'potential')
                    self.assertEqual(item['external_source']['catalog_sha256'], self.view['catalog_sha256'])

    def test_remote_binding_enforces_allowlist_units_packages_and_legacy_policy(self):
        for changes in [dict(allowed_pins=[]), dict(packages=[]), dict(legacy_snap_pins=[self.pin])]:
            with self.subTest(changes=changes), self.assertRaises(PotentialError):
                self.adapter(**changes).resolve_potential(self.pin, type_elements=['Ni'], units='metal')
        with self.assertRaises(PotentialError):
            self.adapter().resolve_potential(self.pin, type_elements=['Ni'], units='real')
        entry = deepcopy(self.view['entries'][0])
        for mapping in ([], 'Ni', [None], [[]], ['NULL'], ['Fe'], ['Ni\nrun 0'], ['Ni']*119):
            with self.subTest(mapping=mapping), self.assertRaises(PotentialError):
                fixed_binding(entry, self.view['catalog_sha256'], type_elements=mapping, software_sha256='b'*64)
        for interaction in ('hybrid', 'unresolved'):
            item = deepcopy(entry); item['record']['metadata']['interaction'] = interaction
            with self.subTest(interaction=interaction), self.assertRaises(PotentialError):
                fixed_binding(self.repin(item), self.view['catalog_sha256'],
                              type_elements=['Ni'], software_sha256='b'*64)
        entry['record']['inspection']['blockers'] = ['synthetic unresolved capability']
        with self.assertRaises(PotentialError):
            fixed_binding(self.repin(entry), self.view['catalog_sha256'], type_elements=['Ni'], software_sha256='b'*64)

    def test_combined_catalog_keeps_local_bindings_and_adds_remote_without_bytes(self):
        local = PotentialCatalog(self.root/'local-potentials')
        local_pin = self.import_fixture('meam', catalog=local)
        combined = CombinedPotentialCatalog(local, self.remote)
        self.assertEqual(combined.directory, local.directory)
        self.assertFalse(combined.is_remote(local_pin)); self.assertTrue(combined.is_remote(self.pin))
        self.assertEqual(combined.read(local_pin), local.read(local_pin))
        self.assertEqual(combined.read(self.pin)[1], {})
        self.assertEqual({item['pin'] for item in combined.list_models()}, {local_pin, self.pin})
        old_binding = self.adapter(local, allowed_pins=[local_pin]).resolve_potential(local_pin,
            type_elements=['Ni', 'Cu'], units='metal')
        mixed_binding = self.adapter(combined, allowed_pins=[local_pin, self.pin]).resolve_potential(local_pin,
            type_elements=['Ni', 'Cu'], units='metal')
        self.assertEqual(mixed_binding, old_binding)
        self.assertEqual(mixed_binding.remote_files, {})
        self.assertEqual(mixed_binding.files['potentials/'+local_pin+'/library.meam'], LIBRARY)
        self.assertEqual(self.adapter(combined).resolve_potential(self.pin,
            type_elements=['Ni'], units='metal').files, {})
        with self.assertRaises(PotentialError):
            combined.binding(local_pin, type_elements=['Ni'], software_sha256='b'*64)

    def test_combined_catalog_rejects_ambiguous_duplicate_pin(self):
        local = PotentialCatalog(self.root/'local-potentials')
        self.assertEqual(self.import_fixture('eam/alloy', catalog=local), self.pin)
        with self.assertRaises(PotentialError): CombinedPotentialCatalog(local, self.remote)

    def test_metadata_validation_rejects_missing_license_roles_and_extra_answers(self):
        original = self.view['entries'][0]
        for mutation in ('model', 'license', 'extra_role', 'empty_license', 'answer', 'path'):
            item = deepcopy(original)
            if mutation in ('model', 'license'): del item['record']['files'][mutation]
            elif mutation == 'extra_role': item['record']['files']['parameters'] = deepcopy(item['record']['files']['model'])
            elif mutation == 'empty_license': item['record']['metadata']['license'] = ''
            elif mutation == 'answer': item['record']['metadata']['hidden_answer'] = 'not permitted'
            else: item['path'] = 'arbitrary-source'
            self.repin(item)
            with self.subTest(mutation=mutation), self.assertRaises(PotentialError): validate_entry(item)
        for fmt in ('meam', 'snap'):
            pin = self.import_fixture(fmt); self.refresh()
            entry = next(x for x in self.view['entries'] if x['pin'] == pin)
            for role in entry['record']['files']:
                item = deepcopy(entry); del item['record']['files'][role]
                with self.subTest(format=fmt, role=role), self.assertRaises(PotentialError):
                    validate_entry(self.repin(item))

    def test_metadata_rejects_unsafe_names_invalid_hashes_sizes_and_types(self):
        original = self.view['entries'][0]
        cases = [('name',value) for value in ('../model', '/model', 'a/b', 'record.json', 'bad;run', 'model\nrun 0')]
        cases += [('size',value) for value in (0, -1, True, 16*1024*1024+1, '10')]
        cases += [('sha256',value) for value in ('main', 'F'*64, [], None)]
        for key,value in cases:
            item = deepcopy(original); item['record']['files']['model'][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(PotentialError):
                validate_entry(self.repin(item))
        item = deepcopy(original)
        item['record']['files']['license']['name'] = item['record']['files']['model']['name']
        with self.assertRaises(PotentialError): validate_entry(self.repin(item))
        for field,value in (('format', []), ('units', {}), ('interaction', [])):
            item = deepcopy(original); item['record']['metadata'][field] = value
            with self.subTest(field=field), self.assertRaises(PotentialError): validate_entry(self.repin(item))
        item = deepcopy(original); item['pin'] = 'f'*64
        with self.assertRaises(PotentialError): validate_entry(item)

    def test_metadata_view_rejects_duplicate_pins_bad_versions_and_arbitrary_fields(self):
        for changes in (dict(schema_version=True), dict(catalog_sha256='main'),
                        dict(entries=[self.view['entries'][0]]*2), dict(paths=['arbitrary'])):
            with self.subTest(changes=changes), self.assertRaises(PotentialError):
                validate_view({**deepcopy(self.view), **changes})
        with self.assertRaises(PotentialError): self.remote.read('f'*64)

    def test_cache_tampering_is_rechecked_for_read_list_and_binding(self):
        cache_file = self.cache/'catalog-view.json'
        cache_file.chmod(0o600); cache_file.write_bytes(cache_file.read_bytes()+b' ')
        for action in (lambda:self.remote.read(self.pin), self.remote.list_models, self.binding):
            with self.subTest(action=action), self.assertRaises(PotentialError): action()
        with self.assertRaises(PotentialError): RemotePotentialCatalog(self.cache)

    def test_cache_symlinks_and_hardlinks_are_rejected(self):
        source = self.cache/'catalog-view.json'
        moved = self.root/'saved-view.json'; source.rename(moved); source.symlink_to(moved)
        with self.assertRaises(ManifestError): self.remote.read(self.pin)
        source.unlink(); os.link(moved, source)
        with self.assertRaises(ManifestError): RemotePotentialCatalog(self.cache)

    def test_external_snapshot_reserves_original_bytes_without_local_model_copy(self):
        binding = self.binding(); snapshot = self.snapshot(binding.remote_files)
        document = snapshot.verify()
        self.assertEqual(document['schema_version'], 2)
        for path,record in binding.remote_files.items():
            self.assertFalse((snapshot.path/path).exists())
            self.assertEqual(next(x for x in document['files'] if x['path']==path), record)
        payload = b''.join(upload_chunks(snapshot))
        length = struct.unpack('!I', payload[:4])[0]
        self.assertEqual(payload[4+length:], INPUT)
        binding.remote_files[next(iter(binding.remote_files))]['sha256'] = 'f'*64
        self.assertEqual(snapshot.verify(), document)
        ordinary = self.snapshot({})
        self.assertEqual(ordinary.verify()['schema_version'], 1)
        self.assertEqual(remote_stage.validate_manifest((ordinary.path/'manifest.json').read_bytes(),
                                                      ordinary.digest)['schema_version'], 1)

    def test_external_manifest_validation_matches_receiver_and_rejects_unsafe_sources(self):
        original = next(iter(self.binding().remote_files.values()))
        changes = [dict(kind='geometry'), dict(pin='main'), dict(catalog_sha256=None),
                   dict(role='unknown'), dict(role=[]), dict(path='arbitrary-source')]
        for changeset in changes:
            item = deepcopy(original); item['external_source'].update(changeset)
            with self.subTest(changes=changeset), self.assertRaises(ManifestError): validate_file_record(item, 2)
            document = self.snapshot().verify()
            document['files'] = [x for x in document['files'] if x['role']=='lammps_input']+[item]
            raw = canonical(document)
            with self.subTest(receiver=changeset), self.assertRaises(remote_stage.StageError):
                remote_stage.validate_manifest(raw, sha256(raw))
        for path in ('../model', '/model', 'potentials/../model', 'potentials/model\nrun0'):
            item = deepcopy(original); item['path'] = path
            with self.subTest(path=path), self.assertRaises(ManifestError): validate_file_record(item, 2)
        with self.assertRaises(ManifestError): validate_file_record(original, 1)
        item = deepcopy(original); item['role'] = 'structure'
        with self.assertRaises(ManifestError): validate_file_record(item, 2)

    def test_staging_copies_exact_pinned_bytes_license_and_old_catalog_version(self):
        snapshot = self.snapshot()
        old_sha = self.view['catalog_sha256']
        self.import_fixture('eam/alloy', name='Later synthetic EAM metadata')
        new = register_catalog_version(self.hpc.directory)
        self.assertNotEqual(new['catalog_sha256'], old_sha)
        target = self.target(); receipt = self.receive(snapshot, target)
        folder = target/('e'*32)
        self.assertEqual((folder/('potentials/'+self.pin+'/model.eam.alloy')).read_bytes(), MODEL)
        self.assertEqual((folder/('potentials/'+self.pin+'/LICENSE.txt')).read_bytes(), LICENSE)
        self.assertEqual(receipt['input_bytes'], len(INPUT)+len(MODEL)+len(LICENSE))
        self.assertEqual(receipt['storage_bytes'], snapshot.verify()['resources']['storage_bytes'])
        self.assertEqual(sha256((folder/'manifest.json').read_bytes()), snapshot.digest)
        self.assertEqual(receipt['state'], 'staged')
        self.assertEqual((folder/('potentials/'+self.pin+'/model.eam.alloy')).stat().st_mode & 0o777, 0o400)

    def test_meam_and_snap_staging_preserves_each_original_file_role(self):
        for fmt in ('meam', 'snap'):
            pin = self.import_fixture(fmt); self.refresh()
            metadata, _, content = FIXTURES[fmt]
            binding = self.adapter(allowed_pins=[pin]).resolve_potential(pin,
                type_elements=list(reversed(metadata['elements'])), units='metal')
            target = self.target(); receipt = self.receive(self.snapshot(binding.remote_files), target)
            for path,item in binding.remote_files.items():
                original = content[item['external_source']['role']]
                with self.subTest(format=fmt, path=path):
                    self.assertEqual((target/('e'*32)/path).read_bytes(), original)
                    self.assertEqual(sha256(original), item['sha256'])
            self.assertEqual(receipt['input_bytes'], len(INPUT)+sum(map(len, content.values())))

    def test_staging_requires_every_registered_role_including_license(self):
        for fmt in ('eam/alloy', 'meam', 'snap'):
            pin = self.pin if fmt=='eam/alloy' else self.import_fixture(fmt)
            self.refresh()
            elements = FIXTURES[fmt][0]['elements']
            external = self.adapter(allowed_pins=[pin]).resolve_potential(pin,
                type_elements=elements, units='metal').remote_files
            for path in external:
                missing = {key:value for key,value in external.items() if key!=path}
                with self.subTest(format=fmt, missing=path): self.assert_stage_rejected(self.snapshot(missing))

    def test_staging_rejects_wrong_pin_catalog_hash_file_hash_size_and_binding_name(self):
        external = self.binding().remote_files
        model = next(path for path in external if path.endswith('model.eam.alloy'))
        for mutation in ('pin', 'catalog', 'hash', 'size', 'name', 'role'):
            changed = deepcopy(external)
            if mutation=='pin': changed[model]['external_source']['pin'] = 'f'*64
            elif mutation=='catalog': changed[model]['external_source']['catalog_sha256'] = 'f'*64
            elif mutation=='hash': changed[model]['sha256'] = 'f'*64
            elif mutation=='size': changed[model]['size'] += 1
            elif mutation=='role': changed[model]['external_source']['role'] = 'license'
            else:
                new = 'potentials/'+self.pin+'/renamed.model'
                changed[new] = changed.pop(model); changed[new]['path'] = new
            with self.subTest(mutation=mutation): self.assert_stage_rejected(self.snapshot(changed))

    def test_source_tampering_never_produces_a_staged_receipt(self):
        snapshot = self.snapshot()
        source = self.hpc.directory/self.pin/EAM_FILES['model']
        source.chmod(0o600); source.write_bytes(MODEL.replace(b'58.69', b'58.00'))
        target = self.assert_stage_rejected(snapshot)
        self.assertTrue((target/('e'*32)/'allocation.json').exists())
        self.assertEqual((snapshot.path/'manifest.json').read_bytes(), canonical(snapshot.verify()))

    def test_source_symlink_hardlink_and_public_permissions_are_rejected(self):
        snapshot = self.snapshot(); source = self.hpc.directory/self.pin/EAM_FILES['model']
        moved = self.root/'outside.model'; source.rename(moved)
        source.symlink_to(moved); self.assert_stage_rejected(snapshot)
        source.unlink(); os.link(moved, source); self.assert_stage_rejected(snapshot)
        source.unlink(); source.write_bytes(MODEL); source.chmod(0o444)
        self.assert_stage_rejected(snapshot)

    def test_source_directory_symlink_and_changed_catalog_version_are_rejected(self):
        snapshot = self.snapshot(); source = self.hpc.directory/self.pin
        moved = self.root/'outside-entry'; source.rename(moved); source.symlink_to(moved, target_is_directory=True)
        self.assert_stage_rejected(snapshot)
        source.unlink(); moved.rename(source)
        version = self.hpc.directory/'versions'/(self.view['catalog_sha256']+'.json')
        version.chmod(0o600); version.write_bytes(version.read_bytes()+b' ')
        self.assert_stage_rejected(snapshot)

    def test_receiver_catalog_rejects_incomplete_roles_unsafe_paths_and_duplicates(self):
        original = json.loads((self.hpc.directory/'catalog.json').read_bytes())
        for mutation in ('license', 'path', 'basename', 'duplicate', 'format_type'):
            value = deepcopy(original); entry = value['entries'][0]
            if mutation=='license':
                del entry['record']['files']['license']; del entry['paths']['license']
            elif mutation=='path': entry['paths']['model'] = 'outside/arbitrary-model'
            elif mutation=='basename': entry['record']['files']['model']['name'] = '../model'
            elif mutation=='duplicate': value['entries'].append(deepcopy(entry))
            else: entry['record']['metadata']['format'] = []
            if mutation in ('license', 'basename', 'format_type'):
                entry['pin'] = sha256(canonical(entry['record']))
                entry['paths'] = {role:entry['pin']+'/'+item['name'] for role,item in entry['record']['files'].items()}
            with self.subTest(mutation=mutation), self.assertRaises(remote_stage.StageError):
                remote_stage.validate_potential_catalog(canonical(value))

    def test_catalog_client_uses_pinned_helper_and_only_reads_metadata(self):
        endpoint = StageEndpoint('synthetic-hpc', '/usr/bin/python3', '/trusted/receiver.py',
                                 'a'*64, '/private/requests')
        client = PotentialCatalogClient(StageClient(endpoint, self.root/'audit'))
        result = dict(returncode=0, failure='', stdout=base64.b64encode(canonical(self.view)).decode(), stderr='')
        with patch('auto_lammps.remote_potentials._capture', return_value=result) as capture:
            self.assertEqual(client.list(), self.view)
        command = capture.call_args.args[0]
        self.assertIn('--list-potentials', command[-1]); self.assertIn(endpoint.helper_sha256, command[-1])
        self.assertNotIn(str(self.hpc.directory), ' '.join(command))
        self.assertNotIn('input_chunks', capture.call_args.kwargs)
        for result in (dict(returncode=None, failure='timeout', stdout='', stderr=''),
                       dict(returncode=0, failure='', stdout='invalid-base64', stderr='')):
            with self.subTest(result=result), patch('auto_lammps.remote_potentials._capture', return_value=result):
                with self.assertRaises(PotentialError): client.list()
        self.assertEqual(len(list((self.root/'audit').glob('*/intent.json'))), 3)


if __name__ == '__main__':
    unittest.main()
