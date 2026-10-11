"""Protocol tests use synthetic evidence and transports, never paid calls."""
from copy import deepcopy
from dataclasses import replace
import json
import importlib.util
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from auto_lammps.deepseek import DeepSeekClient, DeepSeekConfig, ModelCalls
from auto_lammps.manifest import canonical, sha256
from auto_lammps.papers import PaperStore
from auto_lammps.tasks import TaskError, TaskStore
from auto_lammps.workbench_bridge import (HUMAN_ROLE, WorkbenchExtractionBridge,
    WorkbenchModelClient, WorkbenchPaperBinding, WorkbenchRuntime,
    installed_workbench_runtime)
from auto_lammps.workbench_adapter import (WorkbenchScientificAdapter, native_digest)


def synthetic_capabilities():
    return {'planner': {'id': 'synthetic_planner', 'version': 'synthetic-v1'},
        'executor': {'id': 'synthetic_executor', 'version': 'synthetic-v1'},
        'runtime_tasks': ['analysis', 'extraction'], 'model_stages': ['initial_focus'],
        'operations': {key: 'synthetic_executor.' + key for key in
            ('assemble', 'execute', 'project', 'plan_next', 'finalize')},
        'modules': {'synthetic_module': 'e'*64}}


def synthetic_stage(messages, options):
    call = {'call_id': 'synthetic-1', 'task': options['task'], 'messages': deepcopy(messages),
        'max_tokens': options['max_tokens'],
        'options': {'thinking': options.get('thinking'), 'temperature': options.get('temperature')}}
    call['call_digest'] = native_digest(call)
    stage = {'name': 'initial_focus', 'input_fingerprint': 'b'*64, 'calls': [call]}
    stage['stage_fingerprint'] = native_digest({'name': stage['name'],
        'input_fingerprint': stage['input_fingerprint'], 'calls': [call['call_digest']]})
    return stage


def response(value):
    return 200, canonical({'choices': [{'finish_reason': 'stop', 'message': {
        'role': 'assistant', 'content': json.dumps(value)}}],
        'usage': {'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6}})


class WorkbenchBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.tasks = TaskStore(self.root / 'tasks.sqlite')
        self.task = self.tasks.create('Synthetic workflow', 'No physics', 'reproduction')
        self.papers = PaperStore(self.tasks)
        paper = self.papers.add('Synthetic article', '10.1234/synthetic', 'Scope', 'Test')
        paper = self.papers.select(paper['id'], paper['revision'])
        self.paper = self.papers.link_task(paper['id'], paper['revision'], self.task['id'])
        self.binding = WorkbenchPaperBinding(self.paper['id'], 7, 'a' * 64)
        self.source = {'title': self.paper['title'], 'doi': self.paper['doi'],
            'pdf_sha256': self.binding.pdf_sha256, 'pdf_path': 'private-synthetic.pdf'}
        self.calls = ModelCalls(self.root/'models.sqlite',
            DeepSeekConfig('synthetic-model', max_output_tokens=16000), max_requests=100)
        self.outgoing = []
        def transport(body, key, timeout):
            self.outgoing.append(json.loads(body))
            return response({'verified': True})
        self.client = DeepSeekClient(self.calls, transport=transport,
            key_reader=lambda: 'synthetic-project-key')
        self.preparations = []
        self.actions = []
        self.summary = {'schema_version': 'literature-extraction-commit-result-v2',
            'status': 'completed', 'paper': {'title': self.paper['title'], 'doi': self.paper['doi']},
            'candidate_count': 1, 'published_item_count': 1, 'existing_item_count': 0,
            'manual_review_count': 0, 'table_candidate_count': 0, 'figure_candidate_count': 1,
            'table_structure_candidate_count': 0, 'table_structure_manual_review_count': 0,
            'table_structure_unavailable_count': 0, 'visual_evidence_ready': True,
            'visual_stage_status': 'ready', 'private_receipt_field': '/private/source.pdf'}
        self.messages = [{'role': 'system', 'content': 'Extract source facts as JSON.'},
            {'role': 'user', 'content': 'Synthetic registered P source only.'}]
        plan = {'method': 'json', 'task': 'extraction', 'messages': self.messages,
            'max_tokens': 4000, 'tools': [], 'options': {'thinking': False, 'temperature': 0.45}}
        self.draft = SimpleNamespace(outbound={'job_handle': 'synthetic-job',
            'stage_fingerprint': 'b'*64}, estimated_calls=1, max_calls=4,
            max_tokens=16000, content_units=(),
            call_plan=(SimpleNamespace(canonical_dict=lambda: deepcopy(plan)),))
        assembler = SimpleNamespace(preflight=lambda req: self.preparations.append(deepcopy(req)),
            assemble=lambda req: self.draft)
        def execute(*, action, ai_client):
            self.actions.append(action)
            ai_client.request_json(self.messages, task='extraction', max_tokens=4000,
                thinking=False, temperature=0.45)
            return {'summary': self.summary}
        ports = SimpleNamespace(assembler=assembler,
            executor=SimpleNamespace(execute=execute),
            projector=SimpleNamespace(project=lambda result: result['summary']))
        self.exports = []
        self.runtime = WorkbenchRuntime(
            SimpleNamespace(get_paper=lambda identifier: deepcopy(self.source)),
            SimpleNamespace(export_private_state=lambda token, **kwargs: b'synthetic-state'),
            ports, 'synthetic-human-session', SimpleNamespace,
            lambda *, client, action: client,
            lambda data: SimpleNamespace(snapshot={'pdf_sha256': self.binding.pdf_sha256}),
            SimpleNamespace(export=lambda **kwargs: self.exports.append(kwargs) or b'synthetic-csv'),
            lambda db, paper_id: [{'item_id': 11}],
            lambda db, **kwargs: [{'id': 22}], native_capability_reader=synthetic_capabilities,
            native_stage_reader=lambda token: {'paper_id': 7,
                'paper': {'title': self.paper['title'], 'doi': self.paper['doi']},
                'pdf_sha256': self.binding.pdf_sha256, 'runtime_task': lambda value: value,
                'stage': synthetic_stage(self.messages, {'task': 'extraction', 'max_tokens': 4000,
                    'thinking': False, 'temperature': 0.45})})
        self.bridge = WorkbenchExtractionBridge(self.papers, self.runtime)
        self.identifier = 'c' * 32

    def extract(self, **kwargs):
        return self.bridge.extract(self.task['id'], self.identifier, self.binding,
            self.client, connection_revision=1, credential_generation=1, **kwargs)

    def direct_adapter(self, **options):
        return WorkbenchScientificAdapter({'task_id': self.task['id'], 'paper_id': self.paper['id'],
            'request_id': self.identifier, 'role': HUMAN_ROLE, 'source_sha256': self.binding.pdf_sha256,
            'title': self.paper['title'], 'doi': self.paper['doi'], 'workbench_paper_id': 7},
            capability_reader=synthetic_capabilities,
            stage_reader=lambda: {'paper_id': 7, 'paper': {'title': self.paper['title'], 'doi': self.paper['doi']},
                'pdf_sha256': self.binding.pdf_sha256, 'runtime_task': lambda value: value,
                'stage': synthetic_stage(self.messages, options)})

    def test_extract_calls_existing_ports_and_project_model_without_B_context(self):
        before = canonical(self.tasks.get(self.task['id']))
        result = self.extract()
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['role'], HUMAN_ROLE)
        self.assertEqual(result['scientific_status'], 'not_evaluated')
        self.assertEqual(before, canonical(self.tasks.get(self.task['id'])))
        self.assertEqual(self.preparations, [{'paper_id': 7, 'force_rescan': False}])
        self.assertEqual(self.outgoing[0]['max_tokens'], 4000)
        self.assertEqual(self.outgoing[0]['temperature'], 0.45)
        self.assertEqual(self.outgoing[0]['thinking'], {'type': 'disabled'})
        self.assertEqual(self.actions[0].provider_id, 'deepseek')
        self.assertNotIn('frozen_scientific_conditions', json.dumps(self.actions[0].outbound))
        self.assertNotIn('/private', json.dumps(result))
        self.assertNotIn('private_receipt_field', result)
        ledger = self.calls.lookup(result['model_request_ids'][0])
        self.assertEqual(ledger['receipt']['transport_attempts'], 1)
        self.assertEqual(ledger['receipt']['purpose'], 'literature_workbench_extraction')
        self.assertEqual(ledger['receipt']['scientific_adapter']['name'], 'literature_workbench_scientific_adapter')
        self.assertTrue(self.outgoing[0]['messages'][0]['content'].startswith(self.messages[0]['content']))
        self.assertEqual(self.outgoing[0]['messages'][1:], self.messages[1:])

    def test_replay_and_restart_never_repeat_provider(self):
        result = self.extract()
        self.assertEqual(self.extract(), result)
        restarted = WorkbenchExtractionBridge(self.papers, self.runtime)
        self.assertEqual(restarted.extract(self.task['id'], self.identifier, self.binding,
            self.client, connection_revision=1, credential_generation=1), result)
        self.assertEqual(len(self.outgoing), 1)
        self.assertEqual(len(self.preparations), 1)

    def test_request_cannot_move_paper_source_or_connection(self):
        self.extract()
        with self.assertRaises(TaskError):
            self.bridge.extract(self.task['id'], self.identifier, self.binding,
                self.client, connection_revision=2, credential_generation=1)
        self.assertEqual(len(self.outgoing), 1)

    def test_foreign_task_and_paper_are_rejected_before_prepare(self):
        other = self.tasks.create('Other task', 'No physics', 'reproduction')
        with self.assertRaises(TaskError):
            self.bridge.extract(other['id'], self.identifier, self.binding,
                self.client, connection_revision=1, credential_generation=1)
        self.source['doi'] = '10.1234/foreign'
        with self.assertRaises(TaskError):
            self.extract()
        self.assertEqual(self.preparations, [])
        self.assertEqual(self.calls.status()['used_requests'], 0)

    def test_frozen_B_document_is_never_loaded_or_changed(self):
        from auto_lammps.tasks import FIELDS
        from test_tasks import target_inventory, evidence
        inv = target_inventory()
        doc = self.tasks.import_target_inventory(self.task['id'], self.task['revision'], inv)
        for field in FIELDS:
            doc = self.tasks.add_candidate(doc['id'], doc['revision'], field, evidence())
        doc = self.tasks.confirm(doc['id'], doc['revision'], list(FIELDS))
        doc = self.tasks.select_targets(doc['id'], doc['revision'], [inv['targets'][0]['id']], '')
        doc = self.tasks.freeze(doc['id'], doc['revision'])
        before = canonical(self.tasks.get(doc['id']))
        with patch.object(self.tasks, 'get', side_effect=AssertionError('frozen task document must not be read')):
            result = self.extract()
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(canonical(self.tasks.get(doc['id'])), before)
        self.assertNotIn('frozen_scientific_conditions', json.dumps(self.outgoing))

    def test_B_role_is_not_a_valid_binding(self):
        with self.assertRaises(TaskError):
            replace(self.binding, role='independent_B')

    def test_changed_captured_PDF_never_sends(self):
        self.runtime = replace(self.runtime,
            decode_job_state=lambda data: SimpleNamespace(snapshot={'pdf_sha256': 'd'*64}))
        self.bridge = WorkbenchExtractionBridge(self.papers, self.runtime)
        result = self.extract()
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['model_request_ids'], [])
        self.assertEqual(self.outgoing, [])

    def test_unfinished_stage_is_not_extraction_success(self):
        self.summary['schema_version'] = 'literature-extraction-stage-summary-v1'
        result = self.extract()
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(len(result['model_request_ids']), 1)
        self.assertEqual(self.extract(), result)

    def test_empty_publication_and_unresolved_coverage_remain_explicit(self):
        self.summary.update(published_item_count=0, figure_candidate_count=0)
        result = self.extract()
        self.assertEqual(result['state'], 'no_evidence_published')
        self.identifier = 'd' * 32
        self.summary.update(published_item_count=1, manual_review_count=1)
        result = self.extract(force_rescan=True)
        self.assertEqual(result['state'], 'completed_with_limitations')
        self.assertEqual(result['counts']['manual_review_count'], 1)

    def test_transport_unknown_preserves_accounting_and_never_retries(self):
        def fail(*args):
            raise RuntimeError('synthetic-project-key private upstream body')
        self.client.transport = fail
        result = self.extract()
        self.assertEqual(result['state'], 'failed_or_unknown')
        receipt = self.calls.lookup(result['model_request_ids'][0])['receipt']
        self.assertEqual(receipt['state'], 'unknown')
        self.assertEqual(receipt['transport_attempts'], 1)
        self.assertNotIn('synthetic-project-key', json.dumps(result))
        self.extract()
        self.assertEqual(self.calls.status()['used_requests'], 1)

    def test_unknown_source_blocks_new_request_even_explicit_rescan(self):
        self.client.transport = lambda *args: (_ for _ in ()).throw(RuntimeError('timeout'))
        self.assertEqual(self.extract()['state'], 'failed_or_unknown')
        self.identifier = 'd'*32
        for options in ({}, {'force_rescan': True}, {'repair_visuals': True}):
            with self.assertRaises(TaskError):
                self.extract(**options)
        self.assertEqual(self.calls.status()['used_requests'], 1)
        self.assertEqual(len(self.preparations), 1)

    def test_unknown_unfinished_intent_cannot_be_replaced_with_new_identity(self):
        with self.papers.tasks.transaction() as db:
            db.execute('INSERT INTO workbench_extraction_intents VALUES (?,?,?,?,?)',
                ('d'*32, self.task['id'], self.paper['id'], 'b'*64,
                 canonical({'source_sha256': self.binding.pdf_sha256}).decode()))
        with self.assertRaises(TaskError):
            self.extract(force_rescan=True)
        self.assertEqual(self.calls.status()['used_requests'], 0)

    def test_completed_source_requires_explicit_rescan_for_new_identity(self):
        self.extract()
        self.identifier = 'd'*32
        with self.assertRaises(TaskError):
            self.extract()
        self.assertEqual(self.extract(force_rescan=True)['state'], 'completed')
        self.assertEqual(self.calls.status()['used_requests'], 2)

    def test_unknown_source_is_not_recharged_through_other_linked_task(self):
        self.client.transport = lambda *args: (_ for _ in ()).throw(RuntimeError('timeout'))
        self.extract()
        other = self.tasks.create('Same paper other task', 'No physics', 'reproduction')
        paper = self.papers.get(self.paper['id'])
        self.papers.link_task(paper['id'], paper['revision'], other['id'])
        with self.assertRaises(TaskError):
            self.bridge.extract(other['id'], 'd'*32, self.binding,
                self.client, connection_revision=1, credential_generation=1)
        self.assertEqual(self.calls.status()['used_requests'], 1)

    def test_missing_native_capability_contract_never_sends(self):
        self.runtime = replace(self.runtime, native_capability_reader=None)
        self.bridge = WorkbenchExtractionBridge(self.papers, self.runtime)
        result = self.extract()
        self.assertEqual(result['error_code'], 'workbench_scientific_adapter_invalid')
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(self.calls.status()['used_requests'], 0)

    def test_tampered_adapter_proof_never_sends_or_reserves(self):
        prepare = WorkbenchScientificAdapter.prepare
        def changed(adapter, messages, options):
            prepared, proof = prepare(adapter, messages, options)
            proof['native_call_digest'] = 'f'*64
            return prepared, proof
        with patch.object(WorkbenchScientificAdapter, 'prepare', changed):
            result = self.extract()
        self.assertEqual(result['error_code'], 'workbench_scientific_adapter_invalid')
        self.assertEqual(self.outgoing, [])
        self.assertEqual(self.calls.status()['used_requests'], 0)

    def test_foreign_valid_adapter_cannot_be_attached_to_model_identity(self):
        foreign = self.direct_adapter(task='extraction', max_tokens=4000,
            thinking=None, temperature=None)
        foreign.identity['task_id'] = 'f'*32
        adapter = WorkbenchModelClient(self.client, task=self.task['id'],
            paper=self.paper['id'], source_sha256=self.binding.pdf_sha256,
            request_id=self.identifier, adapter=foreign)
        with self.assertRaises(TaskError):
            adapter.request_json(self.messages, task='extraction', max_tokens=4000)
        self.assertEqual(self.outgoing, [])
        self.assertEqual(self.calls.status()['used_requests'], 0)

    def test_model_policy_options_cannot_increase_or_accept_nonfinite(self):
        adapter = WorkbenchModelClient(self.client, task=self.task['id'],
            paper=self.paper['id'], source_sha256=self.binding.pdf_sha256,
            request_id=self.identifier)
        for options in ({'max_tokens': 16001}, {'max_tokens': True},
                        {'max_tokens': 4000, 'thinking': 1},
                        {'max_tokens': 4000, 'temperature': float('nan')}):
            with self.assertRaises(TaskError):
                adapter.request_json(self.messages, task='extraction', **options)
        self.assertEqual(self.calls.status()['used_requests'], 0)

    def test_key_rotation_stops_before_another_network_request(self):
        adapter = WorkbenchModelClient(self.client, task=self.task['id'],
            paper=self.paper['id'], source_sha256=self.binding.pdf_sha256,
            request_id=self.identifier, adapter=self.direct_adapter(task='extraction', max_tokens=4000,
                thinking=None, temperature=None))
        adapter.request_json(self.messages, task='extraction', max_tokens=4000)
        self.client.key_reader = lambda: 'changed-synthetic-project-key'
        adapter.adapter = self.direct_adapter(task='analysis', max_tokens=4000, thinking=None, temperature=None)
        with self.assertRaises(TaskError):
            adapter.request_json(self.messages, task='analysis', max_tokens=4000)
        self.assertEqual(len(self.outgoing), 1)
        self.assertEqual(self.calls.lookup(adapter.requests[1])['receipt']['state'], 'not_sent')

    def test_export_reuses_upstream_CSV_and_cannot_export_foreign_entity(self):
        data = self.bridge.export(self.task['id'], self.binding, entity_type='item', entity_uid='11')
        self.assertEqual(data, b'synthetic-csv')
        self.assertEqual(self.exports[0], {'source_scope': 'workspace', 'source_id': 'workspace',
            'entity_type': 'item', 'entity_uid': '11', 'format': 'csv'})
        with self.assertRaises(TaskError):
            self.bridge.export(self.task['id'], self.binding, entity_type='item', entity_uid='12')
        with self.assertRaises(TaskError):
            self.bridge.export(self.task['id'], self.binding, entity_type='source', entity_uid='11')

    def test_intents_and_receipts_are_append_only(self):
        self.extract()
        for table in ('workbench_extraction_intents', 'workbench_extraction_receipts'):
            with self.assertRaises(sqlite3.DatabaseError):
                with self.tasks.transaction() as db:
                    db.execute('DELETE FROM ' + table)

    def test_missing_dependency_or_durable_service_never_enables_facade(self):
        with self.assertRaises(TaskError):
            installed_workbench_runtime(database=object(), snapshot_blobs=None,
                checkpoint_runtime=None, session_id='human')
        with patch('auto_lammps.workbench_bridge.importlib.import_module', side_effect=ImportError):
            with self.assertRaises(TaskError):
                installed_workbench_runtime(database=object(), snapshot_blobs=object(),
                    checkpoint_runtime=object(), session_id='human')

    def test_evidence_keeps_values_units_missing_cells_and_hides_local_paths(self):
        self.runtime = replace(self.runtime,
            data_items=lambda db, paper_id: [{'item_id': 11, 'value_text': None,
                'meaning': '数值缺失', 'unit': 'GPa', 'pdf_path': '/private/generated.pdf'}],
            visual_assets=lambda db, **kwargs: [{'id': 22, 'asset_type': 'figure',
                'caption': 'Original caption', 'image_path': '/private/original.png'}])
        self.bridge = WorkbenchExtractionBridge(self.papers, self.runtime)
        result = self.bridge.evidence(self.task['id'], self.binding)
        self.assertIsNone(result['items'][0]['value_text'])
        self.assertEqual(result['items'][0]['unit'], 'GPa')
        self.assertEqual(result['visuals'][0]['caption'], 'Original caption')
        self.assertNotIn('/private', json.dumps(result))
        self.assertEqual(self.calls.status()['used_requests'], 0)

    def test_image_requires_same_paper_and_stored_digest(self):
        path = self.root/'original.png'; content = b'\x89PNG\r\n\x1a\nsynthetic'
        path.write_bytes(content); path.chmod(0o600)
        self.runtime = replace(self.runtime,
            visual_assets=lambda db, **kwargs: [{'id': 22, 'image_sha256': sha256(content)}],
            visual_image=lambda db, asset_id: path)
        self.bridge = WorkbenchExtractionBridge(self.papers, self.runtime)
        self.assertEqual(self.bridge.image(self.task['id'], self.binding, entity_uid='22'), content)
        with self.assertRaises(TaskError):
            self.bridge.image(self.task['id'], self.binding, entity_uid='23')
        path.write_bytes(b'\x89PNG\r\n\x1a\nchanged')
        with self.assertRaises(TaskError):
            self.bridge.image(self.task['id'], self.binding, entity_uid='22')


@unittest.skipUnless(importlib.util.find_spec('auto_research') is not None,
    'separately installed literature workbench is optional')
class InstalledWorkbenchBridgeTests(unittest.TestCase):
    def test_original_PDF_stages_verification_finalizer_and_export(self):
        import fitz
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from auto_research.evidence.db import EvidenceDB
        from auto_research.evidence.literature_checkpoint_runtime import LiteratureCheckpointRuntime
        from auto_research.evidence.literature_task_checkpoint_service import LiteratureTaskCheckpointService
        from auto_research.evidence.literature_task_checkpoint_store import SealedSQLiteLiteratureCheckpointStore
        from auto_research.evidence.literature_snapshot_blob import SealedImmutablePDFBlobStore

        class Sealer:
            cipher = AESGCM(AESGCM.generate_key(bit_length=256))
            def seal(self, plaintext, *, associated_data):
                nonce = os.urandom(12)
                return nonce + self.cipher.encrypt(nonce, plaintext, associated_data)
            def open(self, ciphertext, *, associated_data):
                return self.cipher.decrypt(ciphertext[:12], ciphertext[12:], associated_data)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            source = root/'generated.pdf'
            doc = fitz.open(); page = doc.new_page()
            page.insert_text((72, 72), 'Results. Sample C has hardness 4.0 GPa at 400 K.')
            page.draw_rect(fitz.Rect(72, 115, 320, 250), fill=(0.85, 0.9, 1))
            page.draw_line((90, 230), (300, 140), color=(0.1, 0.3, 0.7))
            page.insert_text((72, 270), 'Figure 1. Hardness of sample C.')
            doc.save(source); doc.close()
            tasks = TaskStore(root/'tasks.sqlite')
            task = tasks.create('Generated PDF source', 'No physics', 'reproduction')
            papers = PaperStore(tasks)
            paper = papers.add('Generated workbench article', '10.1234/generated', 'Scope', 'Test')
            paper = papers.select(paper['id'], paper['revision'])
            paper = papers.link_task(paper['id'], paper['revision'], task['id'])
            database = EvidenceDB(root/'workbench.sqlite')
            source_sha = sha256(source.read_bytes())
            upstream_id = database.upsert_paper(title=paper['title'], doi=paper['doi'],
                pdf_path=str(source), pdf_sha256=source_sha)
            sealer = Sealer()
            persistence = SealedSQLiteLiteratureCheckpointStore(data_root=root/'checkpoints', sealer=sealer)
            checkpoints = LiteratureCheckpointRuntime(LiteratureTaskCheckpointService(store=persistence))
            blobs = SealedImmutablePDFBlobStore(data_root=root/'snapshots', sealer=sealer)
            runtime = installed_workbench_runtime(database=database, snapshot_blobs=blobs,
                checkpoint_runtime=checkpoints, session_id='synthetic-installed-session')
            provider_calls = []
            def transport(body, key, timeout):
                payload = json.loads(body); provider_calls.append(payload)
                content = payload['messages'][1]['content']
                if 'Verify these candidates:' in content:
                    candidates = json.loads(content.split('Verify these candidates:\n', 1)[1]
                        .split('\n\nSource pages:', 1)[0])
                    value = {'verdicts': [{'candidate_id': row['candidate_id'],
                        'verdict': 'supported', 'reason': 'Exact source text.'} for row in candidates]}
                elif 'original_caption' in content:
                    assets = json.loads(content)['visual_evidence']
                    value = {'assets': [{'asset_id': a['asset_id'],
                        'display_name': '样品硬度', 'context_explanation': '来源图呈现样品硬度。',
                        'physical_quantities': ['hardness'], 'materials': [],
                        'variables': {}, 'tags': ['hardness']} for a in assets]}
                else:
                    value = {'data': [{'value_text': '4.0', 'meaning': '样品硬度', 'unit': 'GPa',
                        'context_explanation': '样品 C 在 400 K 条件下的测量', 'source_page': 1,
                        'source_locator': 'Results', 'source_excerpt': 'Sample C has hardness 4.0 GPa at 400 K',
                        'evidence_type': 'measured', 'source_precision': 'exact_text'}],
                        'findings': [], 'pending_tasks': []}
                return response(value)
            calls = ModelCalls(root/'calls.sqlite', DeepSeekConfig('synthetic-model',
                max_output_tokens=16000, max_input_bytes=262144), max_requests=100)
            client = DeepSeekClient(calls, transport=transport, key_reader=lambda:'synthetic-project-key')
            bridge = WorkbenchExtractionBridge(papers, runtime)
            binding = WorkbenchPaperBinding(paper['id'], upstream_id, source_sha)
            request = 'f'*32
            result = bridge.extract(task['id'], request, binding, client,
                connection_revision=1, credential_generation=1)
            self.assertIn(result['state'], {'completed', 'completed_with_limitations'}, result)
            self.assertGreater(result['counts']['published_item_count'], 0)
            self.assertGreater(result['counts']['figure_candidate_count'], 0)
            self.assertTrue(result['visual_evidence_ready'])
            self.assertEqual(len(result['model_request_ids']), len(provider_calls))
            self.assertGreater(len(provider_calls), 2)
            for identifier in result['model_request_ids']:
                proof = calls.lookup(identifier)['receipt']['scientific_adapter']
                self.assertEqual(proof['name'], 'literature_workbench_scientific_adapter')
                self.assertEqual(proof['output_check'], 'native_workbench_stage_validation_and_finalizer')
                self.assertEqual(len(proof['contract_sha256']), 64)
            rows = runtime.data_items(database, upstream_id)
            artifact = bridge.export(task['id'], binding, entity_type='item', entity_uid=str(rows[0]['item_id']))
            self.assertIn(b'4.0', artifact.content)
            self.assertIn(b'GPa', artifact.content)
            evidence = bridge.evidence(task['id'], binding)
            self.assertEqual(evidence['items'][0]['value_text'], '4.0')
            self.assertEqual(evidence['items'][0]['unit'], 'GPa')
            image = bridge.image(task['id'], binding, entity_uid=evidence['visuals'][0]['entity_uid'])
            self.assertTrue(image.startswith(b'\x89PNG\r\n\x1a\n'))
            self.assertEqual(bridge.extract(task['id'], request, binding, client,
                connection_revision=1, credential_generation=1), result)
            self.assertEqual(len(provider_calls), len(result['model_request_ids']))


if __name__ == '__main__':
    unittest.main()
