"""Ordinary HTTP target suggestions with synthetic P and the native PDF reader.

Only the model transport is replaced in successful calls. The package reader,
native source reader, stage adapter, accounting client and HTTP routes are real.
No author input, target physics, network request or private research is used.
"""
from copy import deepcopy
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from auto_lammps.manifest import canonical, sha256
from auto_lammps.model_connections import ModelConnections
from auto_lammps.paper_evidence import PaperEvidenceViews
from auto_lammps.scientific_adapters import validate_prepared_messages
from auto_lammps.tasks import FIELDS
from auto_lammps.web import create_app
from test_tasks import evidence, target_inventory
import test_workbench_recovery as fixture


ORIGIN = 'http://127.0.0.1:8765'
HEADERS = {'Origin': ORIGIN, 'X-Task-Review': '1'}


@unittest.skipUnless(importlib.util.find_spec('auto_research') is not None,
    'separately installed literature workbench is optional')
class ReferenceTargetAnalysisWebTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.NativeWorkbenchRecoveryTests()
        self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.tasks, self.papers = self.f.tasks, self.f.papers
        self.paper, self.task = self.f.paper, self.f.task
        self.bridge, self.binding = self.f.bridge, self.f.binding
        self.directory = self.f.root/'retained-P'; self.directory.mkdir(mode=0o700)
        folder = self.directory/self.task['id']; folder.mkdir(mode=0o700)
        self.folder = folder/'paper-evidence'; self.folder.mkdir(mode=0o700)
        receipt = canonical({'paper': {'title': self.paper['title'], 'doi': self.paper['doi']},
            'modules': ['synthetic-retained-workbench-export'], 'pdf_sha256': self.f.source_sha})
        self.contents = {'source.json': receipt, 'original.png': b'synthetic-original-image',
            'points.csv': b'x,y\n0,1\n1,2\n'}
        for name, content in self.contents.items():
            path = self.folder/name; path.write_bytes(content); path.chmod(0o600)
        inventory = target_inventory()
        inventory.update(paper={'title': self.paper['title'], 'doi': self.paper['doi']}, source_sha256=sha256(receipt))
        inventory['targets'][0].update(locator='SYNTHETIC_TARGET_LABEL_IS_NOT_P_SOURCE',
            label='Synthetic retained target 1', criterion='SYNTHETIC_P_VALUE_ONLY')
        inventory['targets'].append({**inventory['targets'][0], 'id': 'table-2', 'label': 'Synthetic retained target 2'})
        self.document = dict(version=1, task_id=self.task['id'], paper_id=self.paper['id'],
            title=self.paper['title'], doi=self.paper['doi'], role='paper_evidence_human_only',
            source_sha256=sha256(receipt), source_receipt_file='source.json',
            source_note='Retained synthetic native export; no scientific verification',
            limitations=['Synthetic fixture'], target_inventory=inventory, priorities={},
            files=[dict(name=name, label=name, size=len(content), sha256=sha256(content))
                for name, content in self.contents.items()],
            figures=[dict(name='original.png', label='Retained P', caption='Synthetic original image')],
            views=[dict(id='figure-1', title='Synthetic P view', description='Reference only',
                figures=[dict(name='original.png', role='paper')],
                tables=[dict(name='points.csv', role='paper', label='Synthetic P data',
                    columns=[dict(key='x', label='x'), dict(key='y', label='y')])])])
        self.save_package()
        self.views = PaperEvidenceViews(self.directory, self.papers)
        self.connections = ModelConnections(self.tasks, calls=self.f.calls, assistant_enabled=True)
        self.connections.save('deepseek-official', 'synthetic-model', 'synthetic-project-key')
        self.sent = []
        self.transport = patch('auto_lammps.model_connections.https_transport', self.response)
        self.transport.start(); self.addCleanup(self.transport.stop)
        self.client = self.new_client()
        self.base = '/api/tasks/'+self.task['id']+'/reference-target-analysis'
        self.request_id = 'd'*32

    def save_package(self):
        path = self.folder/'manifest.json'
        path.write_bytes(canonical(self.document)); path.chmod(0o600)

    def new_client(self, *, bridge=None):
        client = TestClient(create_app(self.tasks, papers=self.papers,
            model_client=self.f.client, model_connections=self.connections,
            paper_evidence_views=self.views, workbench_bridge=bridge or self.bridge,
            workbench_bindings={self.paper['id']: self.binding}), base_url=ORIGIN)
        self.addCleanup(client.close)
        return client

    def response(self, body, key, timeout):
        payload = json.loads(body); self.sent.append(payload)
        self.assertEqual(key, 'synthetic-project-key')
        context = json.loads(payload['messages'][-1]['content'])
        cited = dict(source_id='paper-page-6', quote='Synthetic page 6.')
        targets = [dict(target_id=row['id'], data_class='raw_lammps', priority=1,
            reason='合成建议，检验 HTTP 接线。', citations=[cited], dependency_ids=['shared'], missing=[])
            for row in context['inventory']['targets']]
        value = dict(targets=targets, dependencies=[dict(id='shared', data_class='raw_lammps',
            label='Synthetic shared source', description='Both synthetic targets use retained source data.',
            requires=[], citations=[cited], missing=[])], limitations=['Not verified science.'])
        return 200, canonical({'choices':[{'finish_reason':'stop','message':{'role':'assistant',
            'content':json.dumps(value)}}], 'usage':{'prompt_tokens':8,'completion_tokens':5,'total_tokens':13}})

    def post(self, request_id=None, *, client=None, **extra):
        return (client or self.client).post(self.base, json={'request_id':request_id or self.request_id, **extra},
            headers=HEADERS)

    def test_normal_http_action_uses_native_all_pages_real_adapter_and_existing_accounting(self):
        before = self.tasks.get(self.task['id'])
        history_before = self.tasks.history(self.task['id'])
        native = self.bridge.runtime.native_paper_sources_reader(self.binding.workbench_paper_id)
        self.assertEqual(native['pdf_sha256'], self.f.source_sha)
        self.assertEqual(len(native['sources']), 6)
        self.assertEqual([row['id'] for row in native['sources']], ['paper-page-'+str(i) for i in range(1,7)])
        for number, row in enumerate(native['sources'],1):
            self.assertIn('Synthetic page '+str(number)+'.', row['text'])
            self.assertNotIn('SYNTHETIC_TARGET_LABEL_IS_NOT_P_SOURCE', row['text'])
        response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json(); self.assertEqual(result['state'], 'completed', result)
        self.assertEqual(result['usage']['total_tokens'], 13)
        self.assertFalse(result['execution_authorized']); self.assertFalse(result['B_input'])
        payload = self.sent[0]
        permitted = json.loads(payload['messages'][-1]['content'])
        self.assertEqual(permitted['sources'], native['sources'])
        self.assertEqual(permitted['scientific_adapter']['stage'], 'reference_target_analysis')
        self.assertEqual(permitted['scientific_adapter']['rules']['role'], 'reference_only')
        self.assertEqual(permitted['reference_target_dependency_menu']['target_ids'],
            [row['id'] for row in self.document['target_inventory']['targets']])
        expected = {key: permitted[key] for key in ('task_id','paper_id','paper','role','scope',
            'inventory','sources','source_binding')}
        proof = validate_prepared_messages('reference_target_analysis', payload['messages'], expected)
        self.assertEqual(proof['input_check'], 'passed')
        self.assertEqual(result['report']['inventory'], self.document['target_inventory'])
        self.assertEqual(result['source_binding']['source_receipt_sha256'], self.document['source_sha256'])
        self.assertEqual(result['source_binding']['pdf_sha256'], self.f.source_sha)
        self.assertEqual(result['source_binding']['package_manifest_sha256'], sha256((self.folder/'manifest.json').read_bytes()))
        self.assertNotIn('synthetic-project-key', response.text)
        self.assertNotIn(str(self.f.root), response.text)
        self.assertEqual(self.tasks.get(self.task['id']), before)
        self.assertEqual(self.tasks.history(self.task['id']), history_before)
        self.assertEqual(self.bridge.history(self.task['id']), [])
        self.assertEqual(self.bridge.runtime.jobs._jobs, {})
        self.assertEqual(self.f.calls.status()['used_requests'], 1)

    def test_read_only_http_history_and_restart_do_not_need_credentials_or_call_model(self):
        self.assertEqual(self.client.get(self.base).json()['history'], [])
        self.assertEqual(self.f.calls.status()['used_requests'], 0)
        first = self.post().json()
        self.assertEqual(first['state'], 'completed', first)
        with patch.object(self.connections, '_read', side_effect=AssertionError('No credentials for history')):
            history = self.client.get(self.base)
            self.assertEqual(history.status_code, 200, history.text)
            self.assertEqual(history.json()['history'], [first])
            restarted = self.new_client()
            self.assertEqual(restarted.get(self.base).json()['history'], [first])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.f.calls.status()['used_requests'], 1)

    def test_duplicate_http_request_keeps_the_same_report_and_cost_after_restart(self):
        first = self.post()
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(self.post().json(), first.json())
        restarted = self.new_client()
        self.assertEqual(self.post(client=restarted).json(), first.json())
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.f.calls.status()['used_requests'], 1)

    def test_saved_answer_review_route_uses_same_task_and_project_binding_without_generation(self):
        url = self.base+'/'+self.request_id+'/review'
        with patch('auto_lammps.reference_targets.ReferenceTargetAnalysis.review_existing',
                   return_value={'state':'completed','new_model_calls':0}) as review:
            reply = self.client.post(url, json={}, headers=HEADERS)
            self.assertEqual(reply.status_code, 200, reply.text)
            self.assertEqual(reply.json()['new_model_calls'], 0)
            args, kwargs = review.call_args
            self.assertEqual(args[:2], (self.task['id'], self.request_id))
            self.assertEqual(args[2].model, self.f.client.model)
            self.assertGreaterEqual(kwargs['connection_revision'], 1)
            self.assertEqual(kwargs['credential_generation'], kwargs['connection_revision'])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.f.calls.status()['used_requests'], 0)

    def test_saved_answer_review_route_rejects_browser_science_missing_origin_and_finished_task(self):
        url = self.base+'/'+self.request_id+'/review'
        with patch('auto_lammps.reference_targets.ReferenceTargetAnalysis.review_existing') as review:
            self.assertEqual(self.client.post(url, json={}, headers={}).status_code, 403)
            self.assertEqual(self.client.post(url, json={'report':{'targets':[]}}, headers=HEADERS).status_code, 422)
            self.tasks.manage_lifecycle(self.task['id'], self.task['revision'], 0, 'finish')
            self.assertEqual(self.client.post(url, json={}, headers=HEADERS).status_code, 422)
            review.assert_not_called()
        self.assertEqual(self.sent, [])

    def test_pdf_bytes_change_blocks_classification_without_replacing_retained_P(self):
        original_package = (self.folder/'manifest.json').read_bytes()
        import fitz
        pdf = fitz.open(); page = pdf.new_page(); page.insert_text((72,72), 'Different synthetic PDF bytes.')
        pdf.save(self.f.source); pdf.close()
        response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['state'], 'unavailable', response.json())
        self.assertIsNone(response.json()['report'])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.f.calls.status()['used_requests'], 0)
        self.assertEqual((self.folder/'manifest.json').read_bytes(), original_package)

    def test_missing_old_target_inventory_is_unavailable_and_never_inferred_from_repository_defaults(self):
        del self.document['target_inventory']; self.save_package()
        response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['state'], 'unavailable')
        self.assertIsNone(response.json()['report'])
        self.assertEqual(self.f.calls.status()['used_requests'], 0)
        self.assertEqual(self.sent, [])
        self.assertNotIn('target_inventory', self.tasks.get(self.task['id']))

    def test_missing_native_reader_never_substitutes_target_labels_or_mocked_source_text(self):
        no_reader = replace(self.bridge.runtime, native_paper_sources_reader=None)
        from auto_lammps.workbench_bridge import WorkbenchExtractionBridge
        bridge = WorkbenchExtractionBridge(self.papers, no_reader)
        client = self.new_client(bridge=bridge)
        response = self.post(client=client)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['state'], 'unavailable')
        self.assertEqual(self.sent, [])
        self.assertEqual(self.f.calls.status()['used_requests'], 0)

    def test_removing_connection_while_model_returns_pauses_report_and_preserves_usage(self):
        def removed(body, key, timeout):
            result = self.response(body, key, timeout)
            self.connections.save('deepseek-official', '', remove=True)
            return result
        with patch('auto_lammps.model_connections.https_transport', removed):
            response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json(); self.assertEqual(result['state'], 'paused', result)
        self.assertIsNone(result['report'])
        self.assertEqual(result['usage']['total_tokens'], 13)
        self.assertEqual(self.f.calls.lookup(self.request_id)['receipt']['state'], 'completed')
        with patch.object(self.connections, '_read', side_effect=AssertionError('No credentials for history')):
            self.assertEqual(self.client.get(self.base).json()['history'], [result])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.f.calls.status()['used_requests'], 1)

    def test_model_cannot_cite_inventory_label_as_original_paper_text(self):
        def fabricated(body, key, timeout):
            status, raw = self.response(body, key, timeout)
            response = json.loads(raw)
            value = json.loads(response['choices'][0]['message']['content'])
            value['targets'][0]['citations'][0]['quote'] = 'SYNTHETIC_TARGET_LABEL_IS_NOT_P_SOURCE'
            response['choices'][0]['message']['content'] = json.dumps(value)
            return status, canonical(response)
        with patch('auto_lammps.model_connections.https_transport', fabricated):
            response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json(); self.assertEqual(result['state'], 'unavailable', result)
        self.assertEqual(result['receipt']['error_code'], 'citation_unavailable')
        self.assertIn('逐字匹配', result['receipt']['message'])
        self.assertIsNone(result['report'])
        self.assertEqual(result['usage']['total_tokens'], 13)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.tasks.get(self.task['id']), self.task)

    def test_frozen_conditions_B_package_and_execution_history_are_unchanged(self):
        doc = self.tasks.import_target_inventory(self.task['id'], self.task['revision'], self.document['target_inventory'])
        for field in FIELDS: doc = self.tasks.add_candidate(doc['id'], doc['revision'], field,
            evidence('SYNTHETIC_B_CONDITION_CANARY'))
        doc = self.tasks.confirm(doc['id'], doc['revision'], list(FIELDS))
        doc = self.tasks.select_targets(doc['id'], doc['revision'], [row['id'] for row in self.document['target_inventory']['targets']], '')
        doc = self.tasks.freeze(doc['id'], doc['revision'])
        packages, revisions = self.tasks.export_packages(doc['id']), self.tasks.history(doc['id'])
        with self.tasks.transaction() as db:
            counts = {name:db.execute('SELECT count(*) FROM '+name).fetchone()[0]
                for name in ('reference_intents','frozen','task_approvals','condition_requests')}
        response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['state'], 'completed', response.json())
        self.assertFalse(response.json()['can_select'])
        self.assertEqual(self.tasks.export_packages(doc['id']), packages)
        self.assertEqual(self.tasks.history(doc['id']), revisions)
        with self.tasks.transaction() as db:
            self.assertEqual({name:db.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in counts}, counts)
        self.assertNotIn('SYNTHETIC_B_CONDITION_CANARY', json.dumps(self.sent))
        self.assertEqual(self.bridge.history(doc['id']), [])

    def test_browser_cannot_supply_P_or_B_answers_context_or_source_paths(self):
        for extra in ({'context':{'sources':[]}}, {'inventory':self.document['target_inventory']},
                {'B_answer':'forbidden'}, {'pdf_path':'forged.pdf'}, {'model':'forged-model'}):
            with self.subTest(extra=extra):
                self.assertEqual(self.post(**extra).status_code, 422)
        self.assertEqual(self.client.post(self.base, json={'request_id':self.request_id}).status_code, 403)
        self.assertEqual(self.post(request_id='bad').status_code, 422)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.f.calls.status()['used_requests'], 0)


if __name__ == '__main__': unittest.main()
