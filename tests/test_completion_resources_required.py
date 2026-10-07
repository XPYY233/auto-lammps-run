"""Ordinary completion route rejects missing verified resources before model cost."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from auto_lammps.candidate_jobs import CandidateService
from auto_lammps.deepseek import DeepSeekClient, DeepSeekConfig, ModelCalls
from auto_lammps.ledger import Resources
from auto_lammps.potentials import PotentialAdapter, PotentialCatalog, PotentialError
from auto_lammps.tasks import TaskStore
from auto_lammps.web import create_app
from test_deepseek import response
from test_meam_potentials import FILES, LIBRARY, METADATA, PARAMETERS
from test_web import HEADERS, ORIGIN


class CompletionResourcesRequiredTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        guard = patch('subprocess.Popen', side_effect=AssertionError('No engine or network'))
        guard.start(); self.addCleanup(guard.stop)
        self.store = TaskStore(self.root/'tasks.sqlite')
        created = self.store.create('合成资源补全检查', '研究 Cu-Ni 的静态弹性常数。', 'research')
        self.doc = self.store.get(created['id'])
        self.url = f"/api/tasks/{created['id']}/complete-conditions"
        self.payload = dict(revision=self.doc['revision'])
        self.calls = ModelCalls(self.root/'models.sqlite', DeepSeekConfig('synthetic-model'), max_requests=4)
        self.transport = Mock(return_value=(200, response(dict(proposals=[
            dict(field='units', value='metal', unit='', basis='合成建议，待确认'),
            dict(field='potential', value='MEAM Cu Ni', unit='', basis='已登记的合成资源，待确认')
        ]))))
        self.key_reader = Mock(return_value='synthetic-key')
        self.model = DeepSeekClient(self.calls, transport=self.transport, key_reader=self.key_reader)
        source = self.root/'synthetic-source'; source.mkdir()
        (source/FILES['library']).write_bytes(LIBRARY)
        (source/FILES['parameters']).write_bytes(PARAMETERS)
        (source/FILES['license']).write_text('Apache-2.0; synthetic fixture only')
        self.catalog = PotentialCatalog(self.root/'potentials')
        self.pin = self.catalog.import_model(source, metadata=deepcopy(METADATA), files=FILES)
        self.adapter = PotentialAdapter(self.catalog, allowed_pins=[self.pin],
                                        software_sha256='b'*64, packages=['MEAM'])
        self.service = self.make_service(self.adapter)

    def make_service(self, adapter):
        service = CandidateService(self.store, self.model, adapter,
            resources=Resources(1, 60, 2097152, 65536), snapshots=self.root/'snapshots')
        self.addCleanup(lambda:service.close(wait=True))
        return service

    def app_client(self, service):
        return TestClient(create_app(self.store, model_client=self.model, candidate_service=service),
                          base_url=ORIGIN)

    def assert_refused_without_model_or_task_change(self, reply, history):
        self.assertEqual(reply.status_code, 422, reply.text)
        self.assertIn('势函数', reply.json()['detail'])
        self.assertIn('未发送', reply.json()['detail'])
        self.assertNotIn('PRIVATE_RESOURCE_CANARY', reply.text)
        self.transport.assert_not_called(); self.key_reader.assert_not_called()
        self.assertEqual(self.calls.history(), [])
        self.assertEqual(self.calls.status()['used_requests'], 0)
        self.assertEqual(self.store.get(self.doc['id']), self.doc)
        self.assertEqual(self.store.history(self.doc['id']), history)
        self.assertIsNone(self.service.history.get(self.doc['id']))

    def test_missing_candidate_service_refuses_before_reserving_or_calling_model(self):
        history = self.store.history(self.doc['id'])
        with self.app_client(None) as client:
            reply = client.post(self.url, json=self.payload, headers=HEADERS)
        self.assert_refused_without_model_or_task_change(reply, history)

    def test_catalog_read_errors_refuse_and_hide_private_diagnostics_before_model(self):
        history = self.store.history(self.doc['id'])
        errors = [PotentialError('PRIVATE_RESOURCE_CANARY'), OSError('PRIVATE_RESOURCE_CANARY'),
                  KeyError('PRIVATE_RESOURCE_CANARY'), TypeError('PRIVATE_RESOURCE_CANARY')]
        with self.app_client(self.service) as client:
            for error in errors:
                with self.subTest(error=type(error).__name__), patch.object(self.catalog, 'read', side_effect=error):
                    reply = client.post(self.url, json=self.payload, headers=HEADERS)
                    self.assert_refused_without_model_or_task_change(reply, history)

    def test_empty_registered_catalog_refuses_before_model(self):
        empty = PotentialCatalog(self.root/'empty-potentials')
        adapter = PotentialAdapter(empty, allowed_pins=[], software_sha256='b'*64, packages=['MEAM'])
        service = self.make_service(adapter)
        history = self.store.history(self.doc['id'])
        with self.app_client(service) as client:
            reply = client.post(self.url, json=self.payload, headers=HEADERS)
        self.assertEqual(empty.list_models(), [])
        self.assert_refused_without_model_or_task_change(reply, history)

    def test_registered_resource_without_required_package_is_not_usable(self):
        adapter = PotentialAdapter(self.catalog, allowed_pins=[self.pin],
                                   software_sha256='b'*64, packages=[])
        service = self.make_service(adapter)
        history = self.store.history(self.doc['id'])
        with self.app_client(service) as client:
            reply = client.post(self.url, json=self.payload, headers=HEADERS)
        self.assertEqual(len(self.catalog.list_models()), 1)
        self.assert_refused_without_model_or_task_change(reply, history)

    def test_incomplete_adapter_inventory_refuses_before_model(self):
        history = self.store.history(self.doc['id'])
        with self.app_client(self.service) as client, patch.object(self.adapter, 'compatible_models',
                return_value=[dict(pin=self.pin, elements=['Cu', 'Ni'], units='metal')]):
            reply = client.post(self.url, json=self.payload, headers=HEADERS)
        self.assert_refused_without_model_or_task_change(reply, history)

    def test_usable_resource_pin_format_elements_reach_real_accounted_model_context(self):
        expected = [dict(pin=self.pin, elements=['Cu', 'Ni'], format='meam', units='metal',
                         applicability=METADATA['applicability'])]
        with self.app_client(self.service) as client:
            reply = client.post(self.url, json=self.payload, headers=HEADERS)
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(self.transport.call_count, 1)
        self.assertEqual(len(self.calls.history()), 1)
        request = json.loads(self.transport.call_args.args[0])
        user = json.loads(request['messages'][1]['content'])
        self.assertEqual(user['available_resources'], expected)
        self.assertEqual(user['user_request'], self.doc['prompt'])
        system = request['messages'][0]['content']
        self.assertIn(self.pin, system); self.assertIn('meam', system)
        self.assertIn('不得提出未在此列表中的势函数格式', system)
        self.assertNotIn(str(self.catalog.directory), json.dumps(request))
        self.assertNotIn('usage_evidence', json.dumps(user['available_resources']))
        self.assertEqual(self.calls.history()[0]['receipt']['state'], 'completed')
        saved = self.store.get(self.doc['id'])
        for field in ('units', 'potential'):
            self.assertEqual(saved['fields'][field]['candidates'][0]['origin'], 'proposed')
            self.assertFalse(saved['fields'][field]['confirmed'])
        self.assertIsNone(self.service.history.get(self.doc['id']))


if __name__ == '__main__':
    unittest.main()
