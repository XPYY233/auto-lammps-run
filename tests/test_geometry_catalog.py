"""Trusted geometry metadata and file-only HPC protocol, with synthetic inputs."""
import base64
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from auto_lammps.geometry_catalog import (GeometryCatalogClient, GeometryCatalogError,
    register_atomic_geometry, validate_entry, validate_view)
from auto_lammps import remote_stage
from auto_lammps.ledger import Resources
from auto_lammps.manifest import canonical, freeze, sha256
from auto_lammps.staging import StageClient, StageEndpoint, upload_chunks
from test_atomic_structure_data import DATA, OPTIONS


class GeometryCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root/'source'; self.source.mkdir()
        (self.source/'initial.data').write_bytes(DATA)
        self.directory = self.root/'geometry-catalog'
        self.asset = self.register()

    def register(self, **changes):
        return register_atomic_geometry(self.source, 'initial.data', self.directory, **{**OPTIONS, **changes})

    def view(self):
        return remote_stage.inspect_geometry_catalog(_geometry_catalog=self.directory)

    def test_register_preserves_bytes_and_returns_only_bounded_geometry_metadata(self):
        view = validate_view(self.view())
        self.assertEqual(view['catalog_sha256'], self.asset['catalog_sha256'])
        entry = view['entries'][0]
        self.assertNotIn('path', entry)
        self.assertNotIn('positions', entry['summary'])
        self.assertEqual((self.directory/'data'/(sha256(DATA)+'.data')).read_bytes(), DATA)
        self.assertFalse(entry['summary']['physical_evaluation_performed'])
        self.assertFalse(entry['summary']['scientifically_verified'])
        self.assertEqual(entry['summary']['composition'], {'Ni': 1, 'Cu': 1})
        self.assertEqual((self.directory/'data'/(sha256(DATA)+'.data')).stat().st_mode & 0o777, 0o400)

    def test_same_input_is_idempotent_and_old_catalog_version_survives_new_input(self):
        self.assertEqual(self.asset, self.register())
        original = (self.directory/'catalog.json').read_bytes()
        data = DATA.replace(b'2 1 2.2 3.1 6.8', b'2 1 2.1 3.1 6.8')
        (self.source/'initial.data').write_bytes(data)
        second = self.register()
        self.assertNotEqual(second['pin'], self.asset['pin'])
        self.assertNotEqual(second['catalog_sha256'], self.asset['catalog_sha256'])
        self.assertEqual((self.directory/'versions'/(self.asset['catalog_sha256']+'.json')).read_bytes(), original)
        self.assertEqual(len(validate_view(self.view())['entries']), 2)

    def test_old_pin_stages_after_new_catalog_without_local_structure_copy(self):
        old = dict(self.asset)
        (self.source/'initial.data').write_bytes(DATA.replace(b'2 1 2.2 3.1 6.8', b'2 1 2.1 3.1 6.8'))
        self.register()
        (self.source/'input.in').write_bytes(b'# Synthetic inert input; never executed\n')
        resources = Resources(2, 60, 2097152, 65536)
        record = dict(path='structure.data', role='structure', size=old['size'], sha256=old['sha256'],
                      external_source=dict(catalog_sha256=old['catalog_sha256'], pin=old['pin']))
        snapshot = freeze(self.source, self.root/'snapshots', files={'input.in':'lammps_input'},
            external_files={'structure.data':record}, entrypoint='input.in', resources=resources,
            provenance=dict(task_sha256='a'*64, analysis_sha256='b'*64, software_sha256='c'*64))
        self.assertFalse((snapshot.path/'structure.data').exists())
        target = self.root/'requests'; target.mkdir(mode=0o700)
        (target/'policy.json').write_bytes(canonical(dict(schema_version=1, max_total_bytes=131072,
                                                        approval_sha256='d'*64)))
        receipt = remote_stage.receive(str(target), 'e'*32, snapshot.digest,
            io.BytesIO(b''.join(upload_chunks(snapshot))), _geometry_catalog=self.directory)
        self.assertEqual((target/('e'*32)/'structure.data').read_bytes(), DATA)
        self.assertEqual(receipt['input_bytes'], len(DATA) + (self.source/'input.in').stat().st_size)
        self.assertEqual(sha256((target/('e'*32)/'manifest.json').read_bytes()), snapshot.digest)

    def test_wrong_elements_and_simulation_script_cannot_be_registered(self):
        with self.assertRaises(ValueError): self.register(type_elements=['NotAnElement', 'Cu'])
        (self.source/'initial.data').write_bytes(b'run 1000\n')
        with self.assertRaises(ValueError): self.register()
        self.assertEqual(len(self.view()['entries']), 1)

    def test_links_and_changed_existing_data_preserve_catalog(self):
        initial = (self.directory/'catalog.json').read_bytes()
        datafile = self.directory/'data'/(sha256(DATA)+'.data')
        datafile.chmod(0o600); datafile.write_bytes(b'altered')
        with self.assertRaises(ValueError): self.register()
        self.assertEqual((self.directory/'catalog.json').read_bytes(), initial)
        (self.source/'initial.data').unlink(); (self.source/'initial.data').symlink_to(datafile)
        with self.assertRaises(ValueError): self.register()

    def test_view_rejects_answers_raw_paths_and_modified_pin(self):
        view = self.view()
        for mutation in ('extra', 'pin', 'composition', 'bool_composition', 'physical', 'path', 'huge'):
            item = deepcopy(view['entries'][0])
            if mutation=='extra': item['summary']['author_answer']='not allowed'
            if mutation=='pin': item['pin']='f'*64
            if mutation=='composition': item['summary']['composition']={'Fe': 2}
            if mutation=='bool_composition': item['summary']['composition']={'Ni': True, 'Cu': True}
            if mutation=='physical': item['summary']['scientifically_verified']=True
            if mutation=='path': item['path']='any-source.data'
            if mutation=='huge': item['summary']['masses_amu']=[10**1000, 1]
            with self.subTest(mutation=mutation), self.assertRaises(GeometryCatalogError): validate_entry(item)

    def test_client_uses_fixed_helper_and_returns_metadata_only(self):
        endpoint = StageEndpoint('synthetic-hpc', '/usr/bin/python3', '/trusted/receiver.py', 'a'*64, '/private/requests')
        client = GeometryCatalogClient(StageClient(endpoint, self.root/'audit'))
        reply = dict(returncode=0, failure='', stdout=base64.b64encode(canonical(self.view())).decode(), stderr='')
        with patch('auto_lammps.geometry_catalog._capture', return_value=reply) as capture:
            value = client.list()
        command = capture.call_args.args[0]
        self.assertIn('--list-geometry', command[-1])
        self.assertNotIn(str(self.source), ' '.join(command))
        self.assertEqual(value, self.view())
        self.assertNotIn('input_chunks', capture.call_args.kwargs)

    def test_failed_metadata_read_does_not_choose_default_or_return_old_resource(self):
        endpoint = StageEndpoint('synthetic-hpc', '/usr/bin/python3', '/trusted/receiver.py', 'a'*64, '/private/requests')
        client = GeometryCatalogClient(StageClient(endpoint, self.root/'audit'))
        reply = dict(returncode=None, failure='timeout', stdout='', stderr='')
        with patch('auto_lammps.geometry_catalog._capture', return_value=reply), self.assertRaises(GeometryCatalogError):
            client.list()

    def test_large_finite_edges_produce_controlled_catalog_and_client_errors(self):
        view = deepcopy(self.view())
        item = view['entries'][0]
        item['summary']['cell_angstrom'] = [[10**300, 0, 0], [0, 10**300, 0], [0, 0, 10**300]]
        item['summary']['tilt_angstrom'] = [0, 0, 0]
        item['pin'] = sha256(canonical({k: item[k] for k in ('size', 'sha256', 'summary')}))
        with self.assertRaises(GeometryCatalogError):
            validate_entry(item)
        endpoint = StageEndpoint('synthetic-hpc', '/usr/bin/python3', '/trusted/receiver.py', 'a'*64, '/private/requests')
        client = GeometryCatalogClient(StageClient(endpoint, self.root/'audit'))
        reply = dict(returncode=0, failure='', stdout=base64.b64encode(canonical(view)).decode(), stderr='')
        with patch('auto_lammps.geometry_catalog._capture', return_value=reply), self.assertRaises(GeometryCatalogError):
            client.list()
