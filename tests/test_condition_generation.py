"""Synthetic NLP outputs traverse the real service and persistent task store."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import json
import sqlite3

from auto_lammps.condition_generation import (condition_evidence_context, condition_evidence_spans,
    condition_messages, condition_request_identity, generate_condition_draft, legacy_condition_request_status,
    recover_condition_request, validate_conditions)
from auto_lammps.deepseek import DeepSeekClient, DeepSeekConfig, ModelCalls, ModelError, request_body
from auto_lammps.manifest import canonical, sha256
from auto_lammps.tasks import FIELDS, TaskError, TaskStore
from test_deepseek import response
from test_tasks import evidence

SOURCES = [dict(id='user-request', origin='user', locator='用户原始任务描述', text='希望研究铜在 300 K 下的性质。')]
OUTPUT = dict(conditions=[dict(field='temperature', value='300', unit='K', source_id='user-request', quote='300 K')],
              questions=[dict(field='quantity', question='需要计算哪项物理量？')])


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TaskStore(Path(self.tmp.name)/'tasks.sqlite')
        self.doc = self.store.create('合成自然语言测试', SOURCES[0]['text'], 'research')
        self.calls = ModelCalls(Path(self.tmp.name)/'models.sqlite', DeepSeekConfig('synthetic-model'), max_requests=2)
        self.transport = Mock(return_value=(200, response(OUTPUT)))
        self.client = DeepSeekClient(self.calls, transport=self.transport, key_reader=lambda:'synthetic-key')

    def generate(self, request_id='a'*32):
        return generate_condition_draft(self.client, self.store, self.doc['id'], self.doc['revision'], SOURCES, request_id)

    def test_free_prose_becomes_unconfirmed_sourced_conditions_and_questions(self):
        doc = self.generate()
        field = doc['fields']['temperature']
        self.assertEqual(field['candidates'][0]['value'], '300')
        self.assertFalse(field['confirmed'])
        self.assertEqual(field['candidates'][0]['generated_evidence']['quote'], '300 K')
        self.assertEqual(doc['generated_batches']['a'*32]['questions'], OUTPUT['questions'])
        self.assertEqual(self.store.history(doc['id'])[-1]['event'], 'conditions_generated')
        self.assertEqual(len(self.calls.history()), 1)
        self.assertNotIn('reference', [item['field'] for item in doc['issues']])
        self.assertEqual(TaskStore(self.store.path).get(doc['id']), doc)

    def test_source_only_generation_derives_value_and_remains_unconfirmed(self):
        span = condition_evidence_spans(SOURCES)[0]
        output = dict(conditions=[dict(field='temperature', source_id='user-request',
                                       evidence_span_id=span['id'])], questions=OUTPUT['questions'])
        self.transport.return_value = (200, response(output))
        doc = self.generate()
        choice = doc['fields']['temperature']['candidates'][0]
        self.assertEqual(choice['value'], SOURCES[0]['text'])
        self.assertEqual(choice['unit'], '')
        self.assertEqual(choice['generated_evidence']['quote'], span['quote'])
        self.assertEqual(choice['generated_evidence']['semantic_verification'], 'not_performed')
        self.assertFalse(doc['fields']['temperature']['confirmed'])
        self.assertEqual(doc['generated_batches']['a'*32]['questions'], OUTPUT['questions'])
        self.assertEqual(self.calls.history()[0]['receipt']['structured_output'], output)
        self.assertEqual(self.transport.call_count, 1)

    def test_invented_source_quote_unit_or_extra_authority_is_rejected(self):
        for changes in ({'source_id':'not-provided'}, {'quote':'400 K'}, {'value':'400'},
                        {'unit':'bar'}, {'field':'execute'}, {'confirmed':True}):
            result = deepcopy(OUTPUT)
            result['conditions'][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(TaskError):
                validate_conditions(SOURCES, result)

    def test_invalid_generation_changes_no_conditions_but_retains_model_output(self):
        bad = deepcopy(OUTPUT)
        bad['conditions'].append({**bad['conditions'][0], 'quote':'invented quote'})
        self.transport.return_value = (200, response(bad))
        with self.assertRaises(TaskError): self.generate()
        self.assertEqual(self.store.get(self.doc['id']), self.doc)
        self.assertEqual(self.calls.history()[0]['receipt']['structured_output'], bad)
        # 不合法输出会触发一次有界修复（共 2 次调用），但仍不改变任何条件。
        self.assertEqual(self.transport.call_count, 2)

    def test_generated_conflict_preserves_manual_evidence_and_revokes_confirmation(self):
        self.doc = self.store.add_candidate(self.doc['id'], self.doc['revision'], 'temperature', evidence('400'))
        self.doc = self.store.confirm(self.doc['id'], self.doc['revision'], ['temperature'])
        doc = self.generate()
        field = doc['fields']['temperature']
        self.assertIsNone(field['selected'])
        self.assertFalse(field['confirmed'])
        self.assertEqual([item['value'] for item in field['candidates']], ['400','300'])

    def test_update_during_model_call_rejects_stale_import_without_losing_new_edit(self):
        def transport(*args):
            self.store.add_candidate(self.doc['id'], self.doc['revision'], 'material', evidence('Cu'))
            return 200, response(OUTPUT)
        self.client.transport = transport
        with self.assertRaises(TaskError): self.generate()
        latest = self.store.get(self.doc['id'])
        self.assertEqual(latest['fields']['material']['candidates'][0]['value'], 'Cu')
        self.assertEqual(latest['fields']['temperature']['candidates'], [])
        self.assertEqual(len(self.calls.history()), 1)

    def test_stale_request_is_rejected_before_spending(self):
        self.store.add_candidate(self.doc['id'], self.doc['revision'], 'material', evidence('Cu'))
        with self.assertRaises(TaskError): self.generate()
        self.transport.assert_not_called()
        self.assertEqual(self.calls.history(), [])

    def test_research_freezes_and_exports_without_any_paper(self):
        for field in FIELDS:
            if field != 'reference':
                self.doc = self.store.add_candidate(self.doc['id'], self.doc['revision'], field, evidence('synthetic input'))
        self.doc = self.store.confirm(self.doc['id'], self.doc['revision'], [f for f in FIELDS if f != 'reference'])
        self.doc = self.store.freeze(self.doc['id'], self.doc['revision'])
        self.assertEqual(self.doc['fields']['reference']['candidates'], [])
        self.assertIn('execution', self.store.export_packages(self.doc['id']))
        with self.assertRaises(TaskError): self.generate()
        self.transport.assert_not_called()

    def test_quote_match_does_not_claim_scientific_or_answer_classification(self):
        sources = [{**SOURCES[0], 'text':'计算得到的结果是 300 K。'}]
        choices, _, _ = validate_conditions(sources, OUTPUT)
        self.assertEqual(choices[0]['candidate']['generated_evidence']['semantic_verification'], 'not_performed')
        # The quote exists but is a result, not an input. This check is not a
        # semantic verifier and must not auto-confirm or release Agent tasks.

    def test_repeated_evidence_does_not_duplicate_or_clear_existing_confirmation(self):
        self.doc = self.generate()
        self.doc = self.store.confirm(self.doc['id'], self.doc['revision'], ['temperature'])
        self.doc = self.generate('b'*32)
        self.assertEqual(len(self.doc['fields']['temperature']['candidates']), 1)
        self.assertTrue(self.doc['fields']['temperature']['confirmed'])
        self.assertGreater(self.doc['generated_batches']['b'*32]['revision'], self.doc['generated_batches']['a'*32]['revision'])

    def test_research_does_not_accept_model_request_for_paper(self):
        output = deepcopy(OUTPUT)
        output['questions'].append(dict(field='reference',question='请提供论文'))
        self.transport.return_value = (200,response(output))
        with self.assertRaisesRegex(TaskError, '不要求论文'): self.generate()
        self.assertEqual(self.store.get(self.doc['id']), self.doc)

    def test_active_locator_selects_exact_unicode_slice_and_still_rejects_invented_values(self):
        spans = condition_evidence_spans(SOURCES)
        span = spans[0]
        self.assertEqual(SOURCES[0]['text'][span['start']:span['end']], span['quote'])
        output = deepcopy(OUTPUT)
        output['conditions'][0].pop('quote')
        output['conditions'][0]['evidence_span_id'] = span['id']
        choices, _, _ = validate_conditions(SOURCES, output)
        evidence = choices[0]['candidate']['generated_evidence']
        self.assertEqual(evidence['quote'], span['quote'])
        self.assertEqual(evidence['source_start'], span['start'])
        self.assertEqual(evidence['semantic_verification'], 'not_performed')
        for key, value in [('evidence_span_id', 'f'*24), ('value', '400'), ('unit', 'bar'),
                           ('source_id', 'unknown'), ('quote', '温度300K')]:
            changed = deepcopy(output)
            changed['conditions'][0][key] = value
            with self.subTest(key=key), self.assertRaises(TaskError):
                validate_conditions(SOURCES, changed)
        payload = json.loads(condition_messages(SOURCES)[1]['content'])
        self.assertEqual(payload['source_locator_adapter']['spans'], spans)
        self.assertEqual(payload['source_locator_adapter']['matching'], 'exact_only')

    def test_source_changed_or_author_code_cannot_reuse_locator_identity(self):
        span = condition_evidence_spans(SOURCES)[0]
        output = deepcopy(OUTPUT)
        output['conditions'][0].pop('quote')
        output['conditions'][0]['evidence_span_id'] = span['id']
        for source in [{**SOURCES[0], 'text': SOURCES[0]['text'] + ' 新版本。'},
                       {**SOURCES[0], 'origin': 'code'}]:
            with self.subTest(source=source['origin']), self.assertRaises(TaskError):
                validate_conditions([source], output)
        self.assertEqual(condition_evidence_spans([{**SOURCES[0], 'origin': 'code'}]), [])

    def test_locator_preserves_long_request_once_within_default_input_limit(self):
        for source_text in ('铜温度参数说明'*750, '铜。'*2625):
            source = {**SOURCES[0], 'text': source_text}
            messages = condition_messages([source])
            body = request_body(DeepSeekConfig('synthetic-model'), messages)
            self.assertLess(len(body), 65536)
            payload = json.loads(messages[1]['content'])
            self.assertNotIn('text', payload['sources'][0])
            self.assertEqual(''.join(span['quote'] for span in payload['source_locator_adapter']['spans']), source['text'])
            self.assertEqual(payload['sources'][0]['origin'], 'user')
            self.assertEqual(payload['sources'][0]['source_sha256'], sha256(canonical(source)))

    def test_full_source_with_whitespace_and_paper_origin_is_retained_in_locator(self):
        source = dict(id='paper-section', origin='paper', locator='方法第2段', text='温度300 K。\n\n压力0 bar；\n采样5 ps。')
        payload = json.loads(condition_messages([source], 'reproduction')[1]['content'])
        self.assertEqual(payload['sources'][0]['origin'], 'paper')
        spans = payload['source_locator_adapter']['spans']
        self.assertEqual(''.join(span['quote'] for span in spans), source['text'])
        self.assertEqual([(span['start'], span['end']) for span in spans][0][0], 0)
        self.assertEqual(spans[-1]['end'], len(source['text']))
        output = dict(conditions=[dict(field='temperature', value='300', unit='K', source_id='paper-section',
                                       evidence_span_id=spans[0]['id'])], questions=[])
        self.assertEqual(validate_conditions([source], output)[0][0]['candidate']['origin'], 'paper')

    def test_failure_progress_survives_restart_without_answers_quotes_or_private_paths(self):
        bad = deepcopy(OUTPUT)
        bad['conditions'][0]['quote'] = '/private/secret-path 不在原文'
        self.transport.return_value = (200, response(bad))
        with self.assertRaises(TaskError):
            self.generate()
        restarted = TaskStore(self.store.path)
        requests = restarted.condition_requests(self.doc['id'])
        self.assertEqual(len(requests), 1)
        latest = requests[0]
        self.assertEqual(latest['state'], 'failed')
        self.assertEqual(latest['error_code'], 'source_quote_mismatch')
        self.assertEqual(len(latest['calls']), 2)
        self.assertEqual([call['state'] for call in latest['calls']], ['completed', 'completed'])
        self.assertNotIn('/private/secret-path', json.dumps(requests))
        self.assertNotIn('structured_output', json.dumps(requests))
        self.assertEqual(restarted.get(self.doc['id']), self.doc)
        with self.assertRaisesRegex(TaskError, '原调用'):
            self.generate()
        self.assertEqual(self.transport.call_count, 2)
        self.assertEqual(len(self.calls.history()), 2)

    def completed_failure(self, request_id='a'*32):
        messages = condition_messages(SOURCES)
        self.store.begin_condition_request(self.doc['id'], self.doc['revision'], request_id,
                                          sha256(canonical(SOURCES)), sha256(canonical(messages)))
        self.store.record_condition_request_event(self.doc['id'], request_id, 'call_started', call_id=request_id)
        completion = self.client.complete_json(request_id, messages)
        self.store.record_condition_request_event(self.doc['id'], request_id, 'model_completed',
                                                 call_id=request_id, receipt=completion['receipt'])
        self.store.record_condition_request_event(self.doc['id'], request_id, 'failed',
                                                 call_id=request_id, error_code='import_rejected',
                                                 receipt=completion['receipt'])
        return request_id

    def test_explicit_retry_after_adapter_change_preserves_failure_and_accounting(self):
        old_id = self.completed_failure()
        old_requests = self.store.condition_requests(self.doc['id'])
        old_calls = self.calls.history()
        old_history = self.store.history(self.doc['id'])
        changed_messages = condition_messages(SOURCES)
        changed_messages[0]['content'] += ' synthetic adapter contract change'
        with patch('auto_lammps.condition_generation.condition_messages', return_value=changed_messages):
            result = generate_condition_draft(self.client, TaskStore(self.store.path), self.doc['id'],
                self.doc['revision'], SOURCES, old_id, retry_of=old_id)
            selected = condition_request_identity(self.client, self.store, self.doc['id'], self.doc['revision'],
                                                  SOURCES, old_id, retry_of=old_id)
        requests = self.store.condition_requests(self.doc['id'])
        self.assertEqual(requests[0], old_requests[0])
        self.assertEqual(requests[1]['request_id'], selected)
        self.assertNotEqual(selected, old_id)
        self.assertEqual(requests[1]['messages_sha256'], sha256(canonical(changed_messages)))
        self.assertEqual(requests[1]['revision'], self.doc['revision'])
        self.assertEqual(requests[1]['state'], 'imported')
        self.assertEqual(self.calls.history()[:1], old_calls)
        self.assertEqual(self.store.history(self.doc['id'])[:1], old_history)
        self.assertEqual(len(self.calls.history()), 2)
        self.assertEqual(result['revision'], self.doc['revision'] + 1)

    def test_explicit_retry_with_same_adapter_and_duplicate_token_never_resends(self):
        old_id = self.completed_failure()
        self.transport.side_effect = RuntimeError('synthetic interrupted model call')
        with self.assertRaises(ModelError):
            generate_condition_draft(self.client, self.store, self.doc['id'], self.doc['revision'],
                                     SOURCES, old_id, retry_of=old_id)
        before = self.store.condition_requests(self.doc['id'])
        for changed in (False, True):
            messages = condition_messages(SOURCES)
            if changed:
                messages[0]['content'] += ' another synthetic contract version'
            with self.subTest(changed=changed), patch('auto_lammps.condition_generation.condition_messages',
                                                    return_value=messages), self.assertRaises(TaskError):
                generate_condition_draft(self.client, TaskStore(self.store.path), self.doc['id'],
                    self.doc['revision'], SOURCES, old_id, retry_of=old_id)
        self.assertEqual(self.store.condition_requests(self.doc['id']), before)
        self.assertEqual(len(self.calls.history()), 2)
        self.assertEqual(self.transport.call_count, 2)

    def test_completed_failed_retry_requires_its_own_token_for_another_explicit_attempt(self):
        parent = 'a'*32
        messages = condition_messages(SOURCES)
        self.store.begin_condition_request(self.doc['id'], self.doc['revision'], parent,
                                          sha256(canonical(SOURCES)), sha256(canonical(messages)))
        self.store.record_condition_request_event(self.doc['id'], parent, 'failed', error_code='import_rejected')
        bad = deepcopy(OUTPUT)
        bad['conditions'][0]['quote'] = 'synthetic quote absent from source'
        self.transport.return_value = (200, response(bad))
        with self.assertRaises(TaskError):
            generate_condition_draft(self.client, self.store, self.doc['id'], self.doc['revision'],
                                     SOURCES, parent, retry_of=parent)
        saved = self.store.condition_requests(self.doc['id'])
        child = saved[-1]['request_id']
        self.assertEqual(saved[-1]['state'], 'failed')
        self.assertEqual([call['state'] for call in saved[-1]['calls']], ['completed', 'completed'])
        with self.assertRaisesRegex(TaskError, '原调用'):
            generate_condition_draft(self.client, self.store, self.doc['id'], self.doc['revision'],
                                     SOURCES, parent, retry_of=parent)
        next_id = condition_request_identity(self.client, self.store, self.doc['id'], self.doc['revision'],
                                             SOURCES, parent, retry_of=child)
        self.assertNotIn(next_id, {parent, child})
        self.assertEqual(self.store.condition_requests(self.doc['id']), saved)
        self.assertEqual(self.transport.call_count, 2)
        self.assertEqual(len(self.calls.history()), 2)

    def test_uncertain_failed_call_blocks_retry_and_new_identity(self):
        self.transport.side_effect = RuntimeError('synthetic network disconnect')
        with self.assertRaises(ModelError):
            self.generate()
        before = self.store.condition_requests(self.doc['id'])
        for request_id, retry_of in [('a'*32, 'a'*32), ('b'*32, None)]:
            with self.subTest(retry_of=retry_of), self.assertRaisesRegex(TaskError, '状态未知'):
                generate_condition_draft(self.client, self.store, self.doc['id'], self.doc['revision'],
                                         SOURCES, request_id, retry_of=retry_of)
        self.assertEqual(self.store.condition_requests(self.doc['id']), before)
        self.assertEqual(len(self.calls.history()), 1)
        self.assertEqual(self.transport.call_count, 1)

    def test_uncertain_ledger_without_call_event_cannot_enable_retry(self):
        request_id = 'a'*32
        messages = condition_messages(SOURCES)
        self.store.begin_condition_request(self.doc['id'], self.doc['revision'], request_id,
                                          sha256(canonical(SOURCES)), sha256(canonical(messages)))
        self.calls.reserve(request_id, request_body(self.calls.config, messages))
        self.store.record_condition_request_event(self.doc['id'], request_id, 'failed', error_code='import_rejected')
        before = self.store.condition_requests(self.doc['id'])
        with self.assertRaisesRegex(TaskError, '状态未知'):
            generate_condition_draft(self.client, self.store, self.doc['id'], self.doc['revision'],
                                     SOURCES, request_id, retry_of=request_id)
        self.assertEqual(self.store.condition_requests(self.doc['id']), before)
        self.assertEqual(len(self.calls.history()), 1)
        self.transport.assert_not_called()

    def test_retry_receipt_after_process_restart_is_recovered_from_same_identity(self):
        old_id = self.completed_failure()
        retry_id = condition_request_identity(self.client, self.store, self.doc['id'], self.doc['revision'],
                                              SOURCES, old_id, retry_of=old_id)
        messages = condition_messages(SOURCES)
        self.store.begin_condition_request(self.doc['id'], self.doc['revision'], retry_id,
                                          sha256(canonical(SOURCES)), sha256(canonical(messages)))
        self.store.record_condition_request_event(self.doc['id'], retry_id, 'call_started', call_id=retry_id)
        self.client.complete_json(retry_id, messages)
        result = generate_condition_draft(self.client, TaskStore(self.store.path), self.doc['id'],
            self.doc['revision'], SOURCES, old_id)
        self.assertEqual(self.store.condition_requests(self.doc['id'])[-1]['request_id'], retry_id)
        self.assertEqual(self.store.condition_requests(self.doc['id'])[-1]['state'], 'imported')
        self.assertEqual(result['fields']['temperature']['candidates'][0]['value'], '300')
        self.assertEqual(len(self.calls.history()), 2)
        self.assertEqual(self.transport.call_count, 2)

    def test_concurrent_same_retry_token_uses_one_reserved_model_call(self):
        old_id = self.completed_failure()
        started, release = Event(), Event()
        def transport(*args):
            started.set()
            if not release.wait(5):
                raise RuntimeError('synthetic test synchronization timeout')
            return 200, response(OUTPUT)
        self.client.transport = transport
        def retry():
            return generate_condition_draft(self.client, TaskStore(self.store.path), self.doc['id'],
                self.doc['revision'], SOURCES, old_id, retry_of=old_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(retry)
            try:
                self.assertTrue(started.wait(5))
                second = pool.submit(retry)
                with self.assertRaisesRegex(TaskError, '尚无完整回执'):
                    second.result(timeout=5)
            finally:
                release.set()
            result = first.result(timeout=5)
        self.assertEqual(len(self.store.condition_requests(self.doc['id'])), 2)
        self.assertEqual(len(self.calls.history()), 2)
        self.assertEqual(len(result['generated_batches']), 1)

    def test_recovery_before_repair_reservation_cannot_fail_or_start_another_request(self):
        for paused_event in ('validation_failed', 'repair_started'):
            with self.subTest(paused_event=paused_event), tempfile.TemporaryDirectory() as folder:
                store = TaskStore(Path(folder)/'tasks.sqlite')
                document = store.create('合成修复并发', SOURCES[0]['text'], 'research')
                calls = ModelCalls(Path(folder)/'models.sqlite', DeepSeekConfig('synthetic-model'), max_requests=4)
                bad = deepcopy(OUTPUT)
                bad['conditions'][0]['quote'] = 'synthetic absent quote'
                transport = Mock(side_effect=[(200, response(bad)), (200, response(OUTPUT))])
                client = DeepSeekClient(calls, transport=transport, key_reader=lambda: 'synthetic-key')
                reached, release = Event(), Event()
                record = store.record_condition_request_event
                def paused_record(identifier, request_id, kind, **data):
                    record(identifier, request_id, kind, **data)
                    if kind == paused_event:
                        reached.set()
                        if not release.wait(5):
                            raise RuntimeError('synthetic test synchronization timeout')
                store.record_condition_request_event = paused_record
                with ThreadPoolExecutor(max_workers=1) as pool:
                    worker = pool.submit(generate_condition_draft, client, store, document['id'],
                                         document['revision'], SOURCES, 'a'*32)
                    try:
                        self.assertTrue(reached.wait(5))
                        before = store.condition_requests(document['id'])
                        with self.assertRaisesRegex(TaskError, '尚无完整回执|修复进度'):
                            recover_condition_request(client, TaskStore(store.path), document['id'], 'a'*32, SOURCES)
                        self.assertEqual(store.condition_requests(document['id']), before)
                        with self.assertRaisesRegex(TaskError, '已有的失败请求'):
                            generate_condition_draft(client, store, document['id'], document['revision'],
                                                     SOURCES, 'a'*32, retry_of='a'*32)
                        self.assertEqual(len(calls.history()), 1)
                        self.assertEqual(transport.call_count, 1)
                    finally:
                        release.set()
                    result = worker.result(timeout=5)
                self.assertEqual(store.condition_requests(document['id'])[-1]['state'], 'imported')
                self.assertEqual(len(store.condition_requests(document['id'])), 1)
                self.assertEqual(len(calls.history()), 2)
                self.assertEqual(transport.call_count, 2)
                self.assertEqual(len(result['generated_batches']), 1)

    def test_recovery_with_stale_first_snapshot_cannot_terminate_declared_repair(self):
        request_id = 'a'*32
        repair_id = sha256(canonical({'base': request_id, 'repair': 1}))[:32]
        messages = condition_messages(SOURCES)
        self.store.begin_condition_request(self.doc['id'], self.doc['revision'], request_id,
                                          sha256(canonical(SOURCES)), sha256(canonical(messages)))
        self.store.record_condition_request_event(self.doc['id'], request_id, 'call_started', call_id=request_id)
        bad = deepcopy(OUTPUT)
        bad['conditions'][0]['quote'] = 'synthetic absent quote'
        self.transport.return_value = (200, response(bad))
        completion = self.client.complete_json(request_id, messages)
        self.store.record_condition_request_event(self.doc['id'], request_id, 'model_completed',
                                                 call_id=request_id, receipt=completion['receipt'])
        lookup = self.calls.lookup
        def declare_repair_during_lookup(call_id):
            found = lookup(call_id)
            if call_id == request_id:
                self.store.record_condition_request_event(self.doc['id'], request_id, 'validation_failed',
                                                         call_id=request_id, error_code='source_quote_mismatch')
                self.store.record_condition_request_event(self.doc['id'], request_id, 'repair_started', call_id=repair_id)
            return found
        self.calls.lookup = declare_repair_during_lookup
        with self.assertRaisesRegex(TaskError, '修复进度'):
            recover_condition_request(self.client, self.store, self.doc['id'], request_id, SOURCES)
        self.assertEqual(self.store.condition_requests(self.doc['id'])[0]['state'], 'repairing')
        self.assertFalse(any(event['kind'] == 'failed' for event in self.store.condition_requests(self.doc['id'])[0]['events']))
        self.assertEqual(len(self.calls.history()), 1)

    def test_terminal_request_is_checked_before_sending_a_declared_repair(self):
        bad = deepcopy(OUTPUT)
        bad['conditions'][0]['quote'] = 'synthetic absent quote'
        self.transport.return_value = (200, response(bad))
        record = self.store.record_condition_request_event
        def terminate_after_declaration(identifier, request_id, kind, **data):
            record(identifier, request_id, kind, **data)
            if kind == 'repair_started':
                record(identifier, request_id, 'failed', call_id=data['call_id'],
                       error_code='import_rejected', reserved=False)
        self.store.record_condition_request_event = terminate_after_declaration
        with self.assertRaisesRegex(TaskError, '请求已结束'):
            self.generate()
        latest = self.store.condition_requests(self.doc['id'])[0]
        self.assertEqual(latest['state'], 'failed')
        self.assertEqual(len(self.calls.history()), 1)
        self.assertEqual(self.transport.call_count, 1)
        self.assertFalse(latest['calls'][-1]['reserved'])

    def test_retry_token_must_name_this_tasks_failed_request(self):
        other = self.store.create('另一个合成任务', SOURCES[0]['text'], 'research')
        old_id = self.completed_failure()
        for identifier, retry_of in [(self.doc['id'], 'b'*32), (other['id'], old_id)]:
            with self.subTest(identifier=identifier), self.assertRaisesRegex(TaskError, '此任务已有的失败请求'):
                generate_condition_draft(self.client, self.store, identifier, 1, SOURCES, 'c'*32,
                                         retry_of=retry_of)
        self.assertEqual(len(self.calls.history()), 1)

    def test_preparation_blocks_concurrent_request_before_spending_and_is_immutable(self):
        def begin(identifier):
            try:
                return self.store.begin_condition_request(self.doc['id'], self.doc['revision'],
                    identifier, sha256(canonical(SOURCES)), 'c'*64)
            except TaskError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(begin, ['a'*32, 'b'*32]))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(self.store.condition_requests(self.doc['id'])), 1)
        self.assertEqual(self.calls.history(), [])
        with self.store.transaction() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('DELETE FROM condition_requests')

    def test_import_and_success_progress_commit_together(self):
        self.generate()
        request = self.store.condition_requests(self.doc['id'])[0]
        self.assertEqual(request['state'], 'imported')
        self.assertEqual(request['calls'][0]['state'], 'completed')
        self.assertEqual(self.store.history(self.doc['id'])[-1]['event'], 'conditions_generated')
        with self.assertRaisesRegex(TaskError, '终态'):
            self.store.record_condition_request_event(self.doc['id'], 'a'*32, 'failed')

    def test_progress_commit_failure_rolls_back_all_condition_import(self):
        request_id = 'a'*32
        messages = condition_messages(SOURCES)
        self.store.begin_condition_request(self.doc['id'], self.doc['revision'], request_id,
                                          sha256(canonical(SOURCES)), sha256(canonical(messages)))
        completion = self.client.complete_json(request_id, messages)
        self.store.record_condition_request_event(self.doc['id'], request_id, 'failed',
                                                 error_code='import_rejected')
        with self.assertRaisesRegex(TaskError, '终态'):
            self.store.import_generated_conditions(self.doc['id'], self.doc['revision'], SOURCES, completion,
                                                   condition_request_id=request_id)
        self.assertEqual(self.store.get(self.doc['id']), self.doc)
        self.assertEqual(len(self.store.history(self.doc['id'])), 1)

    def test_transport_failure_has_accounted_model_state_and_safe_reason(self):
        self.client.transport = Mock(side_effect=RuntimeError('/private/secret-path'))
        with self.assertRaises(ModelError):
            self.generate()
        latest = self.store.condition_requests(self.doc['id'])[0]
        self.assertEqual(latest['state'], 'failed')
        self.assertEqual(latest['error_code'], 'model_state_unknown')
        self.assertEqual(latest['calls'][0]['state'], 'unknown')
        self.assertNotIn('/private/secret-path', json.dumps(latest))
        self.assertEqual(len(self.calls.history()), 1)

    def test_budget_or_input_rejection_keeps_intent_but_counts_zero_reserved_calls(self):
        for quota, input_limit, reason in [(0, 65536, 'model_budget_exhausted'),
                                           (2, 1024, 'model_input_too_large')]:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as folder:
                store = TaskStore(Path(folder)/'tasks.sqlite')
                document = store.create('调用前拒绝', SOURCES[0]['text'], 'research')
                calls = ModelCalls(Path(folder)/'models.sqlite', DeepSeekConfig('synthetic-model', max_input_bytes=input_limit),
                                   max_requests=quota)
                transport = Mock()
                reader = Mock(return_value='synthetic-key')
                client = DeepSeekClient(calls, transport=transport, key_reader=reader)
                with self.assertRaises(ModelError):
                    generate_condition_draft(client, store, document['id'], document['revision'], SOURCES, 'a'*32)
                self.assertEqual(calls.history(), [])
                transport.assert_not_called()
                reader.assert_not_called()
                request = legacy_condition_request_status(client, store, document['id'])[-1]
                self.assertEqual(request['state'], 'failed')
                self.assertEqual(request['error_code'], reason)
                self.assertEqual(request['call_count'], 0)
                self.assertEqual(len(request['calls']), 1)  # identity intent is retained
                self.assertFalse(request['calls'][0]['reserved'])
                self.assertEqual(request['calls'][0]['state'], 'not_reserved')
                self.assertEqual(store.get(document['id']), document)

    def test_explicit_recovery_uses_completed_receipt_without_second_call(self):
        request_id = 'a'*32
        messages = condition_messages(SOURCES, self.doc['mode'])
        self.store.begin_condition_request(self.doc['id'], self.doc['revision'], request_id,
                                          sha256(canonical(SOURCES)), sha256(canonical(messages)))
        self.store.record_condition_request_event(self.doc['id'], request_id, 'call_started', call_id=request_id)
        self.client.complete_json(request_id, messages)
        # Simulate process death after the response is saved but before import.
        status = legacy_condition_request_status(self.client, self.store, self.doc['id'])
        self.assertEqual(status[-1]['state'], 'awaiting_import')
        result = recover_condition_request(self.client, TaskStore(self.store.path), self.doc['id'], request_id, SOURCES)
        self.assertEqual(result['fields']['temperature']['candidates'][0]['value'], '300')
        self.assertEqual(self.store.condition_requests(self.doc['id'])[0]['state'], 'imported')
        self.assertEqual(self.transport.call_count, 1)
        self.assertEqual(len(self.calls.history()), 1)

    def test_simultaneous_recovery_before_originating_call_returns_same_imported_result(self):
        complete = self.client.complete_json
        recovered = []
        def complete_then_recover(request_id, messages):
            completion = complete(request_id, messages)
            # Receipt is durable, but the original generation worker has not
            # yet received it or written model_completed. A second same-ID
            # POST recovers the receipt and wins the single import transaction.
            recovered.append(recover_condition_request(self.client, self.store, self.doc['id'], request_id, SOURCES))
            return completion
        self.client.complete_json = complete_then_recover
        result = self.generate()
        self.assertEqual(result, recovered[0])
        self.assertEqual(result['revision'], self.doc['revision'] + 1)
        self.assertEqual(len(result['generated_batches']), 1)
        request = self.store.condition_requests(self.doc['id'])[0]
        self.assertEqual(request['state'], 'imported')
        self.assertEqual(sum(event['kind'] == 'imported' for event in request['events']), 1)
        self.assertFalse(any(event['kind'] == 'failed' for event in request['events']))
        self.assertEqual(len(self.calls.history()), 1)
        self.assertEqual(self.transport.call_count, 1)

    def test_completed_persisted_first_or_repair_reply_is_visible_without_import(self):
        for repair in (False, True):
            with self.subTest(repair=repair), tempfile.TemporaryDirectory() as folder:
                store = TaskStore(Path(folder)/'tasks.sqlite')
                document = store.create('进程边界', SOURCES[0]['text'], 'research')
                calls = ModelCalls(Path(folder)/'models.sqlite', DeepSeekConfig('synthetic-model'), max_requests=2)
                client = DeepSeekClient(calls, transport=Mock(return_value=(200, response(OUTPUT))),
                                       key_reader=lambda: 'synthetic-key')
                request_id = 'a'*32
                messages = condition_messages(SOURCES)
                store.begin_condition_request(document['id'], document['revision'], request_id,
                                              sha256(canonical(SOURCES)), sha256(canonical(messages)))
                store.record_condition_request_event(document['id'], request_id, 'call_started', call_id=request_id)
                first = client.complete_json(request_id, messages)
                store.record_condition_request_event(document['id'], request_id, 'model_completed',
                                                     call_id=request_id, receipt=first['receipt'])
                if repair:
                    repair_id = sha256(canonical({'base': request_id, 'repair': 1}))[:32]
                    store.record_condition_request_event(document['id'], request_id, 'validation_failed',
                                                         call_id=request_id, error_code='source_quote_mismatch')
                    store.record_condition_request_event(document['id'], request_id, 'repair_started', call_id=repair_id)
                    second = client.complete_json(repair_id, messages)
                    store.record_condition_request_event(document['id'], request_id, 'model_completed',
                                                         call_id=repair_id, receipt=second['receipt'])
                # The durable model_completed event was written; import never ran.
                before = store.condition_requests(document['id'])
                history = store.history(document['id'])
                status = legacy_condition_request_status(client, TaskStore(store.path), document['id'])[-1]
                self.assertEqual(status['state'], 'awaiting_import')
                self.assertTrue(status['recovery_required'])
                self.assertEqual(len(calls.history()), 2 if repair else 1)
                self.assertEqual(store.condition_requests(document['id']), before)
                self.assertEqual(store.history(document['id']), history)
                self.assertEqual(store.get(document['id']), document)
                self.assertEqual(client.transport.call_count, 2 if repair else 1)

    def test_unknown_recovery_is_read_only_and_task_revision_change_is_not_overwritten(self):
        messages = condition_messages(SOURCES, self.doc['mode'])
        self.store.begin_condition_request(self.doc['id'], self.doc['revision'], 'a'*32,
                                          sha256(canonical(SOURCES)), sha256(canonical(messages)))
        self.store.record_condition_request_event(self.doc['id'], 'a'*32, 'call_started', call_id='a'*32)
        events = self.store.condition_requests(self.doc['id'])
        with self.assertRaisesRegex(TaskError, '尚无完整回执'):
            recover_condition_request(self.client, self.store, self.doc['id'], 'a'*32, SOURCES)
        self.assertEqual(self.store.condition_requests(self.doc['id']), events)
        self.assertEqual(self.calls.history(), [])
        self.client.complete_json('a'*32, messages)
        status = legacy_condition_request_status(self.client, self.store, self.doc['id'])[-1]
        self.assertTrue(status['recovery_required'])
        self.store.add_candidate(self.doc['id'], self.doc['revision'], 'material', evidence('Cu'))
        history = self.store.history(self.doc['id'])
        status = legacy_condition_request_status(self.client, self.store, self.doc['id'])[-1]
        self.assertEqual(status['state'], 'needs_reconciliation')
        self.assertEqual(status['error_code'], 'task_changed')
        self.assertFalse(status['recovery_required'])
        self.assertEqual(self.store.history(self.doc['id']), history)
        self.assertEqual(self.store.condition_requests(self.doc['id']), events)
        with self.assertRaisesRegex(TaskError, '任务或定位工具已改变'):
            recover_condition_request(self.client, self.store, self.doc['id'], 'a'*32, SOURCES)
        self.assertEqual(self.store.get(self.doc['id'])['fields']['temperature']['candidates'], [])

    def test_completed_old_source_or_tool_does_not_offer_current_request_recovery(self):
        messages = condition_messages(SOURCES, self.doc['mode'])
        self.store.begin_condition_request(self.doc['id'], self.doc['revision'], 'a'*32,
                                          sha256(canonical(SOURCES)), sha256(canonical(messages)))
        self.store.record_condition_request_event(self.doc['id'], 'a'*32, 'call_started', call_id='a'*32)
        completion = self.client.complete_json('a'*32, messages)
        self.store.record_condition_request_event(self.doc['id'], 'a'*32, 'model_completed',
                                                 call_id='a'*32, receipt=completion['receipt'])
        saved = self.store.condition_requests(self.doc['id'])
        history = self.store.history(self.doc['id'])
        original_calls = self.calls.history()
        changed_sources = [{**SOURCES[0], 'text': SOURCES[0]['text'] + ' 修改需求。'}]
        source_status = legacy_condition_request_status(self.client, self.store, self.doc['id'], changed_sources)[-1]
        changed_messages = deepcopy(messages)
        changed_messages[0]['content'] += ' synthetic locator contract change'
        with patch('auto_lammps.condition_generation.condition_messages', return_value=changed_messages):
            tool_status = legacy_condition_request_status(self.client, self.store, self.doc['id'])[-1]
        for status in (source_status, tool_status):
            self.assertEqual(status['state'], 'needs_reconciliation')
            self.assertEqual(status['error_code'], 'task_changed')
            self.assertFalse(status['recovery_required'])
            self.assertEqual(status['call_count'], 1)
        self.assertEqual(self.store.condition_requests(self.doc['id']), saved)
        self.assertEqual(self.store.history(self.doc['id']), history)
        self.assertEqual(self.calls.history(), original_calls)
        self.assertEqual(self.store.get(self.doc['id']), self.doc)
        self.assertEqual(self.transport.call_count, 1)

    def test_legacy_failed_calls_are_reconstructed_read_only_with_real_call_times(self):
        request_id = sha256(canonical(dict(task_id=self.doc['id'], revision=self.doc['revision'], sources=SOURCES,
                                         operation='generate-conditions-v1')))[:32]
        bad = deepcopy(OUTPUT)
        bad['conditions'][0]['quote'] = '原文被改写'
        self.transport.return_value = (200, response(bad))
        self.client.complete_json(request_id, condition_messages(SOURCES))
        repair_id = sha256(canonical({'base': request_id, 'repair': 1}))[:32]
        self.client.complete_json(repair_id, condition_messages(SOURCES))
        before = self.store.history(self.doc['id'])
        reconstructed = legacy_condition_request_status(self.client, self.store, self.doc['id'])
        self.assertEqual(len(reconstructed), 1)
        self.assertTrue(reconstructed[0]['reconstructed'])
        self.assertEqual(reconstructed[0]['state'], 'failed')
        self.assertEqual(reconstructed[0]['error_code'], 'source_quote_mismatch')
        self.assertEqual(reconstructed[0]['timestamp_kind'], 'model_call')
        self.assertEqual([event['at'] for event in reconstructed[0]['events']],
                         [item['at'] for item in self.calls.history()])
        self.assertEqual(len(reconstructed[0]['calls']), 2)
        self.assertEqual(self.store.history(self.doc['id']), before)
        self.assertEqual(self.store.condition_requests(self.doc['id']), [])
        self.assertEqual(self.store.get(self.doc['id']), self.doc)
        self.assertEqual(self.transport.call_count, 2)


class SourceOnlyConditionTests(unittest.TestCase):
    def output(self, source, *, field='sampling'):
        span = condition_evidence_spans([source])[0]
        return dict(conditions=[dict(field=field, source_id=source['id'], evidence_span_id=span['id'])],
                    questions=[])

    def test_exact_source_is_derived_without_unit_rewriting_or_semantic_clearance(self):
        for origin in ('user', 'paper'):
            source = dict(id='permitted-source', origin=origin, locator='合成来源',
                          text='完整60000步，温度为300 K；这段原文可能包含待判断的结果。')
            output = self.output(source)
            output['questions'] = [dict(field='analysis', question='需要确认原文的科学用途。')]
            before = deepcopy(output)
            choices, questions, clean = validate_conditions([source], output)
            choice = choices[0]['candidate']
            self.assertEqual(choice['value'], source['text'])
            self.assertEqual(choice['unit'], '')
            self.assertEqual(choice['origin'], origin)
            self.assertEqual(choice['generated_evidence']['semantic_verification'], 'not_performed')
            self.assertNotIn('confirmed', choice)
            self.assertEqual(questions, output['questions'])
            self.assertEqual(clean, [source])
            self.assertEqual(output, before)

    def test_source_only_rejects_forged_ids_wrong_source_and_partial_legacy_fields(self):
        source = {**SOURCES[0], 'text':'完整60000步，温度为300 K。'}
        output = self.output(source)
        other = dict(id='other', origin='user', locator='另一合成来源', text=source['text'])
        changes = [dict(evidence_span_id='f'*24), dict(evidence_span_id=[]), dict(source_id='unknown'),
                   dict(source_id='other'), dict(field='execute'), dict(value='完成60000'), dict(unit='K'),
                   dict(quote=source['text']), dict(confirmed=True), dict(start=0), dict(end=len(source['text']))]
        for change in changes:
            value = deepcopy(output); value['conditions'][0].update(change)
            with self.subTest(change=change), self.assertRaises(TaskError):
                validate_conditions([source, other], value)

    def test_old_explicit_values_are_never_corrected_by_source_only_derivation(self):
        source = {**SOURCES[0], 'text':'完整60000步，温度为300 K。'}
        output = self.output(source)
        for value, unit in (('完成60000', ''), ('400', 'K')):
            old = deepcopy(output); old['conditions'][0].update(value=value, unit=unit)
            with self.subTest(value=value), self.assertRaisesRegex(TaskError, 'value 不在其 quote'):
                validate_conditions([source], old)
        old = deepcopy(output); old['conditions'][0].update(value='300', unit='K')
        choice = validate_conditions([source], old)[0][0]['candidate']
        self.assertEqual((choice['value'], choice['unit']), ('300', 'K'))

    def test_units_source_token_reaches_research_and_potential_compatibility(self):
        from auto_lammps.agent_candidates import research_inputs
        from auto_lammps.potentials import PotentialAdapter, inspect_eam_alloy
        from test_eam_potentials import METADATA, MODEL

        source = dict(id='user-request', origin='user', locator='合成单位制来源',
                      text='采用 metal 单位制。')
        choice = validate_conditions([source], self.output(source, field='units'))[0][0]['candidate']
        self.assertEqual((choice['value'], choice['unit']), ('metal', ''))
        self.assertEqual(choice['generated_evidence']['quote'], source['text'])
        self.assertEqual(choice['generated_evidence']['semantic_verification'], 'not_performed')
        fields = {}
        for field in FIELDS:
            selected = {**(choice if field == 'units' else evidence('synthetic-'+field)), 'id':'a'*32}
            fields[field] = dict(candidates=[selected], selected=selected['id'], confirmed=True, resolution='')
        record = canonical(dict(schema_version=1, purpose='condition_review_record', mode='research',
                                execution_authorized=False, conditions=fields))
        tasks = Mock()
        tasks.get.return_value = dict(revision=1, status='conditions_frozen', mode='research')
        tasks.export.return_value = record
        inputs = research_inputs(tasks, 'b'*32, 1)
        self.assertEqual(inputs['units'], 'metal')
        catalog = Mock()
        catalog.read.return_value = (
            dict(metadata=deepcopy(METADATA), inspection=inspect_eam_alloy(MODEL, METADATA['elements'])),
            dict(model=MODEL, license=b'Synthetic fixture only'))
        adapter = PotentialAdapter(catalog, allowed_pins=['c'*64], software_sha256='d'*64, packages=['MANYBODY'])
        self.assertEqual([model['pin'] for model in adapter.compatible_models(units=inputs['units'])], ['c'*64])
        self.assertEqual(adapter.compatible_models(units=source['text']), [])

    def test_units_source_only_requires_one_supported_original_token(self):
        for value in ('metal', 'real'):
            source = dict(id='units-source', origin='user', locator='合成单位制来源',
                          text=f'采用 {value} 单位制；所有阶段均采用 {value}。')
            choice = validate_conditions([source], self.output(source, field='units'))[0][0]['candidate']
            self.assertEqual(choice['value'], value)
            self.assertEqual(choice['generated_evidence']['quote'], source['text'])
        for prose in ('尚未决定单位制。', '采用金属单位制。', '建议以后明确单位制。',
                      '采用 Metal 单位制。', '采用 REAL 单位制。', '采用 metal 或 real 单位制。',
                      '使用 metallic 和 unreal 参数。', '参数名为 metal_mode。'):
            source = dict(id='units-source', origin='user', locator='合成单位制来源', text=prose)
            with self.subTest(prose=prose), self.assertRaisesRegex(TaskError, '唯一明确'):
                validate_conditions([source], self.output(source, field='units'))

    def test_units_legacy_explicit_value_is_not_changed_or_filled(self):
        source = dict(id='units-source', origin='user', locator='合成单位制来源',
                      text='讨论 metal 与 real；本需求明确使用 metal。')
        output = self.output(source, field='units')
        output['conditions'][0].update(value='metal', unit='')
        before = deepcopy(output)
        choice = validate_conditions([source], output)[0][0]['candidate']
        self.assertEqual(choice['value'], 'metal')
        self.assertEqual(output, before)
        output['conditions'][0]['value'] = 'METAL'
        with self.assertRaisesRegex(TaskError, 'value 不在其 quote'):
            validate_conditions([source], output)

    def test_source_changes_and_code_do_not_reuse_source_only_identity(self):
        source = SOURCES[0]
        output = self.output(source)
        for change in (dict(text=source['text']+'新版本。'), dict(locator='新的合成位置'),
                       dict(origin='paper'), dict(origin='code')):
            with self.subTest(change=change), self.assertRaises(TaskError):
                validate_conditions([{**source, **change}], output)
        self.assertEqual(condition_evidence_spans([{**source, 'origin':'code'}]), [])
        with self.assertRaises(TaskError):
            validate_conditions([source, dict(source)], output)

    def test_duplicate_references_are_deduplicated_without_claiming_classification(self):
        source = {**SOURCES[0], 'text':'计算结果为300 K。'}
        output = self.output(source, field='temperature')
        output['conditions'] *= 2
        choices = validate_conditions([source], output)[0]
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0]['candidate']['value'], source['text'])
        self.assertEqual(choices[0]['candidate']['generated_evidence']['semantic_verification'], 'not_performed')

    def test_active_span_boundaries_fit_full_candidate_values_and_keep_all_source_text(self):
        for length in (3999, 4000, 4001, 6000, 24000):
            source = {**SOURCES[0], 'text':'铜'*length}
            spans = condition_evidence_spans([source])
            self.assertEqual(''.join(span['quote'] for span in spans), source['text'])
            self.assertTrue(all(0 < len(span['quote']) <= 4000 for span in spans))
            output = dict(conditions=[dict(field='material', source_id=source['id'], evidence_span_id=span['id'])
                                      for span in spans], questions=[])
            choices = validate_conditions([source], output)[0]
            self.assertTrue(all(len(choice['candidate']['value']) <= 4000 for choice in choices))
        with self.assertRaises(TaskError):
            condition_messages([{**SOURCES[0], 'text':'铜'*24001}])
        for version in (0, 3, True):
            with self.subTest(version=version), self.assertRaises(TaskError):
                condition_evidence_spans(SOURCES, version=version)

    def test_v1_long_locator_still_validates_legacy_receipt_but_not_source_only(self):
        source = {**SOURCES[0], 'text':'铜'*3990+'300 K'+'铜'*2010}
        old_span = condition_evidence_spans([source], version=1)[0]
        self.assertEqual(len(old_span['quote']), 6000)
        self.assertNotIn(old_span['id'], {span['id'] for span in condition_evidence_spans([source])})
        output = dict(conditions=[dict(field='temperature', source_id=source['id'],
                                       evidence_span_id=old_span['id'], value='300', unit='K',
                                       quote=old_span['quote'])], questions=[])
        before = deepcopy(output)
        choice = validate_conditions([source], output)[0][0]['candidate']
        self.assertEqual(choice['value'], '300')
        self.assertEqual(choice['generated_evidence']['quote'], old_span['quote'])
        self.assertEqual(output, before)
        source_only = deepcopy(output)
        for key in ('value', 'unit', 'quote'): source_only['conditions'][0].pop(key)
        with self.assertRaises(TaskError): validate_conditions([source], source_only)

    def test_active_context_advertises_source_only_v2_without_duplicate_source_text(self):
        payload = json.loads(condition_messages(SOURCES)[1]['content'])
        tool = payload['source_locator_adapter']
        self.assertEqual(tool['version'], 2)
        self.assertEqual(tool['preferred_condition_fields'], ['field', 'source_id', 'evidence_span_id'])
        self.assertEqual(tool['value_derivation'], 'complete_source_span')
        self.assertEqual(tool['unit_system_derivation'], dict(field='units', supported_tokens=['metal', 'real'],
            matching='case_sensitive_complete_token', required='one_distinct_explicit_token',
            value='original_token', quote='complete_source_span', otherwise='legacy_explicit_value_required'))
        self.assertEqual(tool['max_value_characters'], 4000)
        self.assertNotIn('text', payload['sources'][0])
        self.assertEqual(''.join(span['quote'] for span in tool['spans']), SOURCES[0]['text'])
