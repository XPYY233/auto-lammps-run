"""Synthetic P suggestions only; no paper classification, paid API or physics."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from auto_lammps.deepseek import DeepSeekClient, DeepSeekConfig, ModelCalls, request_body
from auto_lammps.manifest import canonical, sha256
from auto_lammps.papers import PaperStore
from auto_lammps.reference_targets import (DATA_CLASSES, ReferenceTargetAnalysis, ReferenceTargetUnavailable,
    model_ledger_identity, reference_target_input, target_analysis_context, validate_target_analysis)
from auto_lammps.tasks import FIELDS, TaskError, TaskStore
from auto_lammps.scientific_adapters import ScientificAdapterError, validate_prepared_messages
from test_tasks import evidence, target_inventory


class ReferenceTargetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.tasks = TaskStore(self.root/'tasks.sqlite')
        self.task = self.tasks.create('Synthetic P target scope', 'B_CONDITION_SECRET', 'reproduction')
        self.papers = PaperStore(self.tasks)
        paper = self.papers.add('Synthetic dependency article', '10.1234/dependency', 'Scope', 'No physics')
        paper = self.papers.select(paper['id'], paper['revision'])
        self.paper = self.papers.link_task(paper['id'], paper['revision'], self.task['id'])
        self.inv = target_inventory()
        self.inv['paper'] = {key: self.paper[key] for key in ('title', 'doi')}
        original = self.inv['targets'][0]
        self.inv['targets'] = [{**original, 'id': 'target-'+kind, 'label': 'Synthetic '+kind,
            'locator': 'Synthetic P page 1', 'criterion': 'P_ONLY_VALUE'} for kind in sorted(DATA_CLASSES)]
        self.source = dict(id='paper-source', origin='paper', locator='Synthetic P page 1',
            text='Synthetic P evidence. These sentences are fixture data, not research results.')
        self.inputs = dict(task_id=self.task['id'], paper_id=self.paper['id'], paper=deepcopy(self.inv['paper']),
            role='paper_reference_human_only', inventory=deepcopy(self.inv), sources=[self.source],
            source_binding=dict(inventory_source_sha256='a'*64, source_receipt_sha256='a'*64,
                                pdf_sha256='b'*64, package_manifest_sha256='c'*64))
        citation = [dict(source_id=self.source['id'], quote='Synthetic P evidence.')]
        self.value = dict(targets=[], dependencies=[], limitations=['Synthetic suggestions; not verified science.'])
        for kind in sorted(DATA_CLASSES):
            missing = ['Synthetic method link missing.'] if kind == 'unclear' else []
            self.value['targets'].append(dict(target_id='target-'+kind, data_class=kind, priority=2,
                reason='合成建议，用于工程测试。', citations=deepcopy(citation), dependency_ids=[kind], missing=missing))
            self.value['dependencies'].append(dict(id=kind, data_class=kind, label='Synthetic '+kind,
                description='Synthetic retained P dependency.', requires=['raw_lammps'] if kind == 'postprocessing' else [],
                citations=deepcopy(citation), missing=missing))
        self.calls = ModelCalls(self.root/'models.sqlite',
            DeepSeekConfig('synthetic-reference-model', max_output_tokens=16000, max_input_bytes=262144), max_requests=100)
        self.sent = []
        def transport(body, key, timeout):
            self.sent.append(json.loads(body))
            return 200, canonical({'choices': [{'finish_reason': 'stop', 'message': {
                'role': 'assistant', 'content': json.dumps(self.value)}}],
                'usage': {'prompt_tokens': 9, 'completion_tokens': 4, 'total_tokens': 13}})
        self.key = Mock(return_value='synthetic-project-key')
        self.client = DeepSeekClient(self.calls, transport=transport, key_reader=self.key)
        self.connection = dict(enabled=True, model=self.client.model, connection_revision=1,
            credential_generation=1, model_ledger_sha256=model_ledger_identity(self.client))
        self.provider = Mock(side_effect=lambda _:deepcopy(self.inputs))
        self.connection_provider = Mock(side_effect=lambda _:dict(self.connection))
        self.service = self.make_service()
        self.request_id = 'd'*32

    def make_service(self, **kwargs):
        return ReferenceTargetAnalysis(self.tasks, self.papers, self.provider,
            connection_provider=self.connection_provider, **kwargs)

    def analyze(self, service=None, request_id=None, client=None, **kwargs):
        return (service or self.service).analyze(self.task['id'], request_id or self.request_id,
            client or self.client, connection_revision=kwargs.get('connection_revision', 1),
            credential_generation=kwargs.get('credential_generation', 1))

    def interrupted_before_reserve(self):
        with patch.object(self.client, 'complete_json', side_effect=KeyboardInterrupt('Synthetic unsent exit')):
            with self.assertRaises(KeyboardInterrupt): self.analyze()
        self.assertEqual(self.calls.status()['used_requests'], 0)

    def test_fixed_menu_and_validation_preserve_every_target_and_source_identity(self):
        original = deepcopy(self.inputs)
        menu = target_analysis_context(self.inputs)
        self.assertEqual(menu['target_ids'], [row['id'] for row in self.inv['targets']])
        self.assertEqual(set(menu['data_classes']), DATA_CLASSES)
        value = deepcopy(self.value); value['targets'].reverse()
        report = validate_target_analysis(self.inputs, value)
        self.assertEqual(report['inventory'], self.inv)
        self.assertEqual([row['target_id'] for row in report['targets']], menu['target_ids'])
        self.assertEqual(report['source_binding']['pdf_sha256'], 'b'*64)
        self.assertEqual(report['source_binding']['source_receipt_sha256'], 'a'*64)
        self.assertEqual(report['classification_status'], 'model_proposed_not_verified')
        self.assertEqual(report['selection_status'], 'user_selection_required')
        self.assertFalse(report['execution_authorized']); self.assertFalse(report['B_input'])
        self.assertEqual(self.inputs, original)

    def test_all_target_coverage_rejects_omissions_duplicates_new_ids_and_actions(self):
        changes = [lambda v:v['targets'].pop(), lambda v:v['targets'].append(deepcopy(v['targets'][0])),
            lambda v:v['targets'][1].update(target_id=v['targets'][0]['target_id']),
            lambda v:v['targets'][1].update(target_id='not-in-inventory'), lambda v:v.update(execution_authorized=True)]
        for change in changes:
            with self.subTest(change=change):
                value = deepcopy(self.value); change(value)
                with self.assertRaises(TaskError): validate_target_analysis(self.inputs, value)

    def test_dependency_graph_rejects_cycles_dangling_duplicates_and_unreachable_nodes(self):
        changes = [lambda v:v['dependencies'][0].update(requires=['absent']),
            lambda v:v['dependencies'][0].update(requires=[v['dependencies'][0]['id']]),
            lambda v:v['dependencies'][0].update(requires=[v['dependencies'][1]['id']]),
            lambda v:v['targets'][0].update(dependency_ids=['absent']),
            lambda v:v['dependencies'].append(deepcopy(v['dependencies'][0])),
            lambda v:v['dependencies'].append({**v['dependencies'][0], 'id':'orphan'}),
            lambda v:v['targets'][0].update(dependency_ids=[v['dependencies'][0]['id']]*2)]
        for change in changes:
            value = deepcopy(self.value); change(value)
            # The third mutation alone is acyclic; close its reciprocal edge.
            if change is changes[2]: value['dependencies'][1]['requires'] = [value['dependencies'][0]['id']]
            with self.subTest(change=change), self.assertRaises(TaskError):
                validate_target_analysis(self.inputs, value)

    def test_literal_citations_and_unclear_gaps_are_required_per_target(self):
        mutations = [lambda v:v['targets'][0]['citations'][0].update(quote='Invented quote'),
            lambda v:v['targets'][0]['citations'][0].update(source_id='another-paper'),
            lambda v:v['targets'][0].update(citations=[]),
            lambda v:v['targets'][-1].update(missing=[]),
            lambda v:v['targets'][0].update(priority=True),
            lambda v:v['targets'][0].update(data_class='all-paper-lammps')]
        for mutation in mutations:
            value = deepcopy(self.value); mutation(value)
            with self.subTest(mutation=mutation), self.assertRaises(TaskError):
                validate_target_analysis(self.inputs, value)

    def test_reference_input_requires_paper_only_sources_and_separate_provenance_hashes(self):
        changes = [lambda v:v.update(role='independent_B'), lambda v:v['sources'][0].update(origin='code'),
            lambda v:v['sources'][0].update(origin='user'), lambda v:v['sources'].clear(),
            lambda v:v['source_binding'].update(source_receipt_sha256='b'*64),
            lambda v:v['source_binding'].update(inventory_source_sha256='d'*64),
            lambda v:v['source_binding'].update(pdf_sha256=None, package_manifest_sha256=None),
            lambda v:v.update(A_report='Forbidden result')]
        for change in changes:
            value = deepcopy(self.inputs); change(value)
            with self.subTest(change=change), self.assertRaises(ReferenceTargetUnavailable):
                reference_target_input(value)
        native = deepcopy(self.inputs)
        native['source_binding']['package_manifest_sha256'] = None
        native['inventory']['source_sha256'] = native['source_binding']['inventory_source_sha256'] = 'b'*64
        native['source_binding']['source_receipt_sha256'] = 'e'*64
        self.assertEqual(reference_target_input(native)['source_binding']['source_receipt_sha256'], 'e'*64)

    def test_accounted_request_has_mandatory_adapter_and_never_loads_task_conditions(self):
        before = self.tasks.get(self.task['id'])
        with patch.object(self.tasks, 'get', side_effect=AssertionError('No B document read')):
            result = self.analyze()
            self.assertEqual(self.service.history(self.task['id']), [result])
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual(result['usage'], {'prompt_tokens':9, 'completion_tokens':4, 'total_tokens':13})
        payload = json.loads(self.sent[0]['messages'][-1]['content'])
        self.assertEqual(payload['scientific_adapter']['stage'], 'reference_target_analysis')
        self.assertEqual(payload['scientific_adapter']['rules']['role'], 'reference_only')
        self.assertEqual(payload['reference_target_dependency_menu'], target_analysis_context(self.inputs))
        self.assertEqual(result['scientific_adapter']['input_check'], 'passed')
        self.assertNotIn('B_CONDITION_SECRET', json.dumps(self.sent))
        self.assertNotIn('synthetic-project-key', json.dumps(self.service.history(self.task['id'])))
        self.assertNotIn(str(self.root), json.dumps(self.service.history(self.task['id'])))
        self.assertEqual(self.tasks.get(self.task['id']), before)
        self.assertEqual(self.calls.status()['used_requests'], 1)

    def test_existing_frozen_task_gets_only_suggestions_without_retroactive_selection(self):
        doc = self.tasks.import_target_inventory(self.task['id'], self.task['revision'], self.inv)
        for field in FIELDS: doc = self.tasks.add_candidate(doc['id'], doc['revision'], field, evidence())
        doc = self.tasks.confirm(doc['id'], doc['revision'], list(FIELDS))
        doc = self.tasks.select_targets(doc['id'], doc['revision'], [row['id'] for row in self.inv['targets']], '')
        doc = self.tasks.freeze(doc['id'], doc['revision'])
        before, exported = self.tasks.history(doc['id']), self.tasks.export_packages(doc['id'])
        result = self.analyze()
        self.assertEqual(result['state'], 'completed', result)
        self.assertFalse(result['can_select'])
        self.assertEqual(self.tasks.history(doc['id']), before)
        self.assertEqual(self.tasks.export_packages(doc['id']), exported)

    def test_get_history_and_duplicate_completed_action_never_read_credentials(self):
        result = self.analyze()
        self.connection_provider.side_effect = AssertionError('No connection read')
        self.key.side_effect = AssertionError('No credential read')
        self.assertEqual(self.service.get(self.task['id'], self.request_id), result)
        self.assertEqual(self.service.history(self.task['id']), [result])
        self.assertEqual(self.analyze(), result)
        self.assertEqual(self.calls.status()['used_requests'], 1)

    def test_duplicate_click_during_transport_returns_same_intent_and_new_id_is_blocked(self):
        entered, release = threading.Event(), threading.Event(); self.addCleanup(release.set)
        transport = self.client.transport
        def slow(*args):
            entered.set()
            if not release.wait(5): raise TimeoutError('Synthetic timeout')
            return transport(*args)
        self.client.transport = slow
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(self.analyze)
            self.assertTrue(entered.wait(3))
            duplicate = self.analyze()
            self.assertEqual(duplicate['state'], 'dispatching')
            with self.assertRaises(TaskError): self.analyze(request_id='e'*32)
            release.set(); result = pending.result(timeout=5)
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual(len(self.sent), 1)

    def test_persistent_three_suggestion_limit_keeps_invalid_outputs_and_paid_usage(self):
        self.value['targets'].pop()
        for request_id in ('d'*32, 'e'*32, 'f'*32):
            result = self.analyze(self.make_service(), request_id)
            self.assertEqual(result['state'], 'invalid_output', result)
            self.assertEqual(result['usage']['total_tokens'], 13)
        with self.assertRaisesRegex(TaskError, '三次'): self.analyze(self.make_service(), 'a'*32)
        self.assertEqual(self.calls.status()['used_requests'], 3)
        self.assertEqual(len(self.service.history(self.task['id'])), 3)
        for item in self.service.history(self.task['id']):
            self.assertTrue(self.calls.lookup(item['request_id'])['receipt']['structured_output'])
        with self.tasks.transaction() as db:
            for table in ('reference_target_scopes','reference_target_intents','reference_target_events','reference_target_receipts'):
                with self.assertRaises(sqlite3.IntegrityError): db.execute('DELETE FROM '+table)

    def test_unmatchable_pending_citation_is_specific_unavailable_without_fabricated_source(self):
        self.value['targets'][0]['citations'][0]['quote'] = 'Pending source is not retained.'
        result = self.analyze()
        self.assertEqual(result['state'], 'unavailable', result)
        self.assertEqual(result['receipt']['error_code'], 'citation_unavailable')
        self.assertIn(self.value['targets'][0]['target_id'], result['receipt']['message'])
        self.assertIn('逐字匹配', result['receipt']['message'])
        self.assertIsNone(result['report'])
        self.assertEqual(result['usage']['total_tokens'], 13)
        self.assertEqual(self.calls.lookup(self.request_id)['receipt']['structured_output'], self.value)

    def test_missing_trusted_input_is_recorded_unavailable_without_model_call(self):
        self.provider.side_effect = lambda _:None
        result = self.analyze()
        self.assertEqual(result['state'], 'unavailable')
        self.assertEqual(result['receipt']['error_code'], 'reference_input_missing')
        self.assertEqual(self.analyze(), result)
        self.assertEqual(self.calls.status()['used_requests'], 0)
        self.key.assert_not_called()

    def test_unavailable_preflight_crash_reconciles_same_intent_without_credentials(self):
        self.provider.side_effect = lambda _:None
        with patch.object(self.service, '_finish', side_effect=KeyboardInterrupt('Synthetic local exit')):
            with self.assertRaises(KeyboardInterrupt): self.analyze()
        self.provider.side_effect = AssertionError('Do not invent a fresh source')
        result = self.analyze(self.make_service())
        self.assertEqual(result['state'], 'unavailable')
        self.assertEqual(self.calls.status()['used_requests'], 0)
        self.key.assert_not_called()

    def test_unreserved_same_context_intent_resumes_once_without_new_suggestion(self):
        self.interrupted_before_reserve()
        result = self.analyze(self.make_service())
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.service.history(self.task['id'])), 1)

    def test_reserved_unknown_crash_is_never_replayed_after_restart_or_under_new_id(self):
        def reserve_then_exit(request_id, messages):
            self.calls.reserve(request_id, request_body(self.calls.config, messages, model=self.client.model))
            raise KeyboardInterrupt('Synthetic exit after durable reservation')
        with patch.object(self.client, 'complete_json', side_effect=reserve_then_exit):
            with self.assertRaises(KeyboardInterrupt): self.analyze()
        result = self.analyze(self.make_service())
        self.assertEqual(result['state'], 'unknown')
        with self.assertRaisesRegex(TaskError, '待核对'): self.analyze(request_id='e'*32)
        self.assertEqual(self.calls.status()['used_requests'], 1)
        self.assertEqual(self.sent, [])
        self.key.assert_not_called()

    def test_completed_model_receipt_before_local_crash_is_reconciled_without_resend(self):
        with patch.object(self.service, '_accept', side_effect=KeyboardInterrupt('Synthetic exit before service receipt')):
            with self.assertRaises(KeyboardInterrupt): self.analyze()
        original = self.calls.lookup(self.request_id)
        self.key.side_effect = AssertionError('No replay key read')
        result = self.analyze(self.make_service())
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual(self.calls.lookup(self.request_id), original)
        self.assertEqual(len(self.sent), 1)

    def test_unknown_transport_is_retained_and_blocks_new_identity_even_when_source_changes(self):
        self.client.transport = Mock(side_effect=TimeoutError('PRIVATE transport detail'))
        result = self.analyze()
        self.assertEqual(result['state'], 'unknown', result)
        self.inputs['source_binding']['pdf_sha256'] = 'f'*64
        self.assertEqual(self.analyze(), result)
        with self.assertRaisesRegex(TaskError, '待核对'): self.analyze(request_id='e'*32)
        self.assertEqual(self.calls.status()['used_requests'], 1)
        self.client.transport.assert_called_once()
        self.assertNotIn('PRIVATE transport detail', json.dumps(result))

    def test_truncated_known_response_keeps_usage_and_duplicate_does_not_resend(self):
        def truncated(body, key, timeout):
            self.sent.append(json.loads(body))
            return 200, canonical({'choices':[{'finish_reason':'length','message':{'role':'assistant',
                'content':'{"targets":['}}], 'usage':{'prompt_tokens':9,'completion_tokens':16000,'total_tokens':16009}})
        self.client.transport = truncated
        result = self.analyze()
        self.assertEqual(result['state'], 'failed', result)
        self.assertEqual(result['usage']['total_tokens'], 16009)
        self.assertEqual(result['receipt']['model_receipt']['finish_reason'], 'length')
        self.assertEqual(self.calls.lookup(self.request_id)['receipt']['state'], 'response_invalid')
        self.assertEqual(self.analyze(self.make_service()), result)
        self.assertEqual(len(self.sent), 1)

    def test_serialized_tool_contract_cannot_be_omitted_or_changed(self):
        context = self.service._context(self.task['id'], self.client, 1, 1)
        for mutation in ('omit', 'change'):
            messages = deepcopy(context['messages'])
            payload = json.loads(messages[-1]['content'])
            if mutation == 'omit': del payload['reference_target_dependency_menu']
            else: payload['reference_target_dependency_menu']['target_ids'].pop()
            messages[-1]['content'] = canonical(payload).decode()
            with self.subTest(mutation=mutation), self.assertRaises(ScientificAdapterError):
                validate_prepared_messages('reference_target_analysis', messages, context['input'])
        self.assertEqual(self.calls.status()['used_requests'], 0)
        self.key.assert_not_called()

    def test_changed_source_or_connection_pauses_unreserved_intent_without_dispatch(self):
        for change in ('source', 'connection'):
            with self.subTest(change=change):
                self.setUp()
                self.interrupted_before_reserve()
                if change == 'source': self.inputs['source_binding']['pdf_sha256'] = 'f'*64
                else: self.connection['credential_generation'] = 2
                result = self.analyze(self.make_service())
                self.assertEqual(result['state'], 'paused', result)
                self.assertEqual(self.calls.status()['used_requests'], 0)
                self.assertEqual(self.sent, [])

    def test_external_allowance_amendment_pauses_unreserved_intent_without_reset(self):
        self.interrupted_before_reserve()
        ModelCalls.open_existing(self.calls.path).amend_allowance(101, reason_sha256='f'*64)
        result = self.analyze()
        self.assertEqual(result['state'], 'paused', result)
        self.assertEqual(self.calls.status()['used_requests'], 0)
        self.assertEqual(len(self.service.history(self.task['id'])), 1)

    def test_model_thinking_change_pauses_original_unreserved_intent(self):
        self.interrupted_before_reserve()
        self.client.thinking = True
        result = self.analyze()
        self.assertEqual(result['state'], 'paused', result)
        self.assertEqual(self.calls.status()['used_requests'], 0)
        self.assertEqual(self.sent, [])

    def test_explicit_new_source_does_not_reset_the_task_suggestion_limit(self):
        for number, request_id in enumerate(('d'*32, 'e'*32, 'f'*32), 1):
            self.inputs['sources'][0]['text'] += '\nSynthetic retained text revision '+str(number)+'.'
            result = self.analyze(self.make_service(), request_id)
            self.assertEqual(result['state'], 'completed', result)
        with self.assertRaisesRegex(TaskError, '三次'): self.analyze(self.make_service(), 'a'*32)
        self.assertEqual(self.calls.status()['used_requests'], 3)
        self.assertEqual(len(self.service.history(self.task['id'])), 3)

    def test_changed_ledger_cannot_displace_unresolved_original_intent(self):
        self.interrupted_before_reserve()
        other = DeepSeekClient(ModelCalls(self.root/'other-models.sqlite', self.calls.config, max_requests=100),
            key_reader=Mock(side_effect=AssertionError('No second ledger key read')))
        with self.assertRaisesRegex(TaskError, '原模型账本'): self.analyze(client=other)
        self.assertEqual(other.calls.status()['used_requests'], 0)
        with self.assertRaisesRegex(TaskError, '待核对'): self.analyze(request_id='e'*32, client=other)
        self.assertEqual(self.sent, [])

    def test_source_or_authorization_changes_while_model_returns_pause_but_keep_fees(self):
        for change in ('source', 'authorization'):
            with self.subTest(change=change):
                self.setUp()
                transport = self.client.transport
                def changed(*args):
                    if change == 'source': self.inputs['source_binding']['pdf_sha256'] = 'f'*64
                    else: self.connection['enabled'] = False
                    return transport(*args)
                self.client.transport = changed
                result = self.analyze()
                self.assertEqual(result['state'], 'paused', result)
                self.assertEqual(result['usage']['total_tokens'], 13)
                self.assertEqual(self.calls.lookup(self.request_id)['receipt']['state'], 'completed')
                self.assertIsNone(result['report'])
                self.assertEqual(len(self.sent), 1)

    def test_later_p_change_marks_original_completed_report_stale_without_reading_connection(self):
        original = self.analyze()
        self.inputs['source_binding']['pdf_sha256'] = 'f'*64
        self.connection_provider.side_effect = AssertionError('No credentials for history')
        retained = self.service.get(self.task['id'], self.request_id)
        self.assertEqual(retained['state'], 'source_unavailable')
        self.assertEqual(retained['original_state'], 'completed')
        self.assertEqual(retained['report'], original['report'])
        self.assertEqual(retained['receipt'], original['receipt'])
        self.assertEqual(len(self.sent), 1)

    def test_broken_mandatory_adapter_never_dispatches(self):
        with patch('auto_lammps.reference_targets.prepare_stage_messages', side_effect=ValueError('Synthetic contract missing')):
            result = self.analyze()
        self.assertEqual(result['state'], 'unavailable')
        self.assertEqual(self.calls.status()['used_requests'], 0)
        self.key.assert_not_called()

    def test_source_task_paper_and_scope_identity_fail_before_any_model_request(self):
        for change in ('task', 'paper', 'scope'):
            with self.subTest(change=change):
                self.setUp()
                if change == 'task': self.inputs['task_id'] = 'a'*32
                elif change == 'paper': self.inputs['paper_id'] = 'b'*32
                else: self.inputs['scope'] = 'independent_B'
                result = self.analyze()
                self.assertEqual(result['state'], 'unavailable', result)
                self.assertEqual(self.calls.status()['used_requests'], 0)
                self.key.assert_not_called()


if __name__ == '__main__': unittest.main()
