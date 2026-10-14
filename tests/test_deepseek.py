"""Synthetic transport and accounting checks; no API credential or live request."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import ssl
import unittest
from unittest.mock import Mock, patch

from auto_lammps.deepseek import (DeepSeekClient, DeepSeekConfig, MAX_RESPONSE_BYTES, ModelCalls,
                                  ModelError, https_transport, parse_completion, request_body, usage_from_response)
from auto_lammps.manifest import canonical, sha256

MESSAGES = [{'role': 'system', 'content': 'Return JSON: {"conditions": [], "questions": []}'}]


def response(value=None, **changes):
    doc = dict(model='synthetic-model', choices=[dict(finish_reason='stop', message=dict(
        role='assistant', content=json.dumps(value if value is not None else {'conditions': [], 'questions': []})))],
        usage=dict(prompt_tokens=20, completion_tokens=10, total_tokens=30))
    doc.update(changes)
    return canonical(doc)


class DeepSeekTests(unittest.TestCase):
    def test_full_input_capacity_is_bounded_and_old_default_is_preserved(self):
        from auto_lammps.deepseek import MAX_INPUT_BYTES
        self.assertEqual(DeepSeekConfig('synthetic-model').max_input_bytes, 65536)
        config=DeepSeekConfig('synthetic-model', max_input_bytes=524288)
        body=request_body(config,[{'role':'user','content':'Return json. '+('x'*300000)}])
        self.assertGreater(len(body),262144)
        with self.assertRaisesRegex(ModelError,'invalid_model_limits'):
            DeepSeekConfig('synthetic-model',max_input_bytes=MAX_INPUT_BYTES+1)
        with self.assertRaisesRegex(ModelError,'input_too_large'):
            request_body(config,[{'role':'user','content':'Return json. '+('x'*524288)}])

    def test_limit_amendment_is_append_only_and_keeps_costs(self):
        from dataclasses import replace
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'model.sqlite'
            calls=ModelCalls(path,DeepSeekConfig('synthetic-model'),max_requests=2)
            calls.reserve('a'*32,b'first request')
            calls.amend_limits(replace(calls.config,max_output_tokens=16384),reason_sha256='b'*64)
            reopened=ModelCalls.open_existing(path)
            self.assertEqual(reopened.config.max_output_tokens,16384)
            self.assertEqual(reopened.status()['remaining_requests'],1)
            with reopened.transaction() as db:
                base=json.loads(db.execute('SELECT document FROM policy').fetchone()[0])
                self.assertEqual(base['config']['max_output_tokens'],4096)
                self.assertEqual(db.execute('SELECT count(*) FROM calls').fetchone()[0],1)
                with self.assertRaises(sqlite3.IntegrityError):db.execute('DELETE FROM policy_revisions')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'models.sqlite'
        self.config = DeepSeekConfig('synthetic-model')

    def client(self, limit=1, transport=None, key_reader=None):
        self.calls = ModelCalls(self.path, self.config, max_requests=limit)
        self.transport = transport or Mock(return_value=(200, response()))
        return DeepSeekClient(self.calls, transport=self.transport,
                              key_reader=key_reader or (lambda: 'synthetic-not-a-credential'))

    def test_zero_budget_never_reads_key_or_sends(self):
        key = Mock(side_effect=AssertionError('key should not be read'))
        client = self.client(0, key_reader=key)
        with self.assertRaisesRegex(ModelError, 'model_budget_exhausted'):
            client.complete_json('a'*32, MESSAGES)
        key.assert_not_called()
        self.transport.assert_not_called()
        self.assertEqual(self.calls.history(), [])

    def test_accounted_json_success_and_restart_cannot_repeat_id(self):
        client = self.client(2)
        result = client.complete_json('a'*32, MESSAGES)
        body, key, timeout = self.transport.call_args.args
        payload = json.loads(body)
        self.assertFalse(payload['stream'])
        self.assertEqual(payload['response_format'], {'type': 'json_object'})
        self.assertEqual(payload['thinking'], {'type': 'disabled'})
        self.assertEqual(payload['max_tokens'], 4096)
        self.assertNotIn('synthetic-not-a-credential', body.decode())
        self.assertEqual(result['receipt']['usage']['total_tokens'], 30)
        reopened = ModelCalls.open_existing(self.path)
        self.assertEqual(reopened.status()['remaining_requests'], 1)
        self.assertEqual(reopened.history()[0]['receipt']['structured_output'], result['value'])
        with self.assertRaisesRegex(ModelError, 'request_already_reserved'):
            DeepSeekClient(reopened, transport=self.transport).complete_json('a'*32, MESSAGES)
        self.assertEqual(self.transport.call_count, 1)

    def test_explicit_thinking_mode_preserves_accounting_and_json(self):
        client=self.client(1)
        client.thinking=True
        result=client.complete_json('b'*32, MESSAGES)
        body=json.loads(self.transport.call_args.args[0])
        self.assertEqual(body['thinking'], {'type':'enabled'})
        self.assertEqual(body['response_format'], {'type':'json_object'})
        self.assertTrue(result['receipt']['thinking'])
        self.assertEqual(self.calls.status()['remaining_requests'],0)

    def test_per_request_thinking_override_does_not_change_shared_default(self):
        client = self.client(2)
        client.thinking = True
        first = client.complete_json('a'*32, MESSAGES, thinking=False, reasoning_effort='low')
        self.assertTrue(client.thinking)
        body = json.loads(self.transport.call_args.args[0])
        self.assertEqual(body['thinking'], {'type': 'disabled'})
        self.assertNotIn('reasoning_effort', body)
        self.assertFalse(first['receipt']['thinking'])
        self.assertIsNone(first['receipt']['reasoning_effort'])
        second = client.complete_json('b'*32, MESSAGES)
        self.assertTrue(second['receipt']['thinking'])
        self.assertEqual(self.calls.status()['remaining_requests'], 0)

    def test_invalid_thinking_override_is_rejected_before_accounting(self):
        client = self.client(1)
        for value in (0, 1, 'false', [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ModelError, 'invalid_thinking_mode'):
                client.complete_json('a'*32, MESSAGES, thinking=value)
        self.assertEqual(self.calls.history(), [])
        self.transport.assert_not_called()

    def test_incomplete_response_keeps_safe_finish_reason_without_upstream_text(self):
        envelope = json.loads(response())
        envelope['choices'][0]['finish_reason'] = 'length'
        envelope['choices'][0]['message']['reasoning_content'] = 'SECRET_CANARY'
        envelope['usage']['completion_tokens_details'] = {'reasoning_tokens': 9}
        client = self.client(1, transport=Mock(return_value=(200, canonical(envelope))))
        with self.assertRaisesRegex(ModelError, 'incomplete_generation'):
            client.complete_json('a'*32, MESSAGES)
        receipt = self.calls.history()[0]['receipt']
        self.assertEqual(receipt['finish_reason'], 'length')
        self.assertEqual(receipt['reasoning_tokens'], 9)
        self.assertEqual(receipt['max_output_tokens'], 4096)
        self.assertNotIn('SECRET_CANARY', json.dumps(receipt))
        self.assertNotIn('structured_output', receipt)

    def test_unknown_finish_reason_and_invalid_reasoning_counts_do_not_leak(self):
        from auto_lammps.deepseek import response_diagnostics
        envelope = json.loads(response())
        envelope['choices'][0]['finish_reason'] = 'SECRET_CANARY'
        for value in (True, -1, 11, '9'):
            envelope['usage']['completion_tokens_details'] = {'reasoning_tokens': value}
            self.assertEqual(response_diagnostics(canonical(envelope)), {'finish_reason': 'unrecognized'})

    def test_concurrent_workers_share_last_request_slot(self):
        self.client(1)
        def execute(index):
            client = DeepSeekClient(ModelCalls.open_existing(self.path), transport=self.transport,
                                    key_reader=lambda: 'synthetic-key')
            try:
                client.complete_json(f'{index:032x}', MESSAGES)
                return 'sent'
            except ModelError as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(execute, range(6)))
        self.assertEqual(results.count('sent'), 1)
        self.assertEqual(results.count('model_budget_exhausted'), 5)
        self.assertEqual(self.transport.call_count, 1)

    def test_unknown_transport_is_sanitized_spent_and_not_retried(self):
        client = self.client(2, transport=Mock(side_effect=TimeoutError('SECRET_CANARY request body')))
        with self.assertRaisesRegex(ModelError, '^model_transport_unknown$'):
            client.complete_json('a'*32, MESSAGES)
        history = self.calls.history()
        self.assertEqual(history[0]['receipt']['state'], 'unknown')
        self.assertNotIn('SECRET_CANARY', json.dumps(history))
        with self.assertRaisesRegex(ModelError, 'request_already_reserved'):
            client.complete_json('a'*32, MESSAGES)
        self.assertEqual(self.transport.call_count, 1)
        self.assertEqual(history[0]['receipt']['transport_attempts'], 1)

    def test_rejection_body_and_missing_key_do_not_leak(self):
        client = self.client(2, transport=Mock(return_value=(401, b'SECRET_CANARY')))
        with self.assertRaisesRegex(ModelError, 'provider_request_failed'):
            client.complete_json('a'*32, MESSAGES)
        self.assertNotIn('SECRET_CANARY', json.dumps(self.calls.history()))
        self.assertEqual(self.calls.history()[0]['receipt']['state'], 'rejected')
        client.key_reader = lambda: None
        with self.assertRaisesRegex(ModelError, 'model_key_missing_or_invalid'):
            client.complete_json('b'*32, MESSAGES)
        self.assertEqual(self.transport.call_count, 1)
        self.assertEqual(self.calls.history()[1]['receipt']['state'], 'not_sent')

    def test_invalid_empty_truncated_and_tool_responses_rejected(self):
        valid = json.loads(response())
        for change in ({'finish_reason': 'length'}, {'finish_reason': 'tool_calls'},
                       {'message': {'role': 'assistant', 'content': ''}},
                       {'message': {'role': 'assistant', 'content': '{}', 'tool_calls': [{}]}},
                       {'message': {'role': 'assistant', 'content': '{"x":1,"x":2}'}},
                       {'message': {'role': 'assistant', 'content': '{"x":NaN}'}},
                       {'message': {'role': 'assistant', 'content': '[]'}}):
            data = {**valid, 'choices': [{**valid['choices'][0], **change}]}
            with self.subTest(change=change), self.assertRaises(ModelError):
                parse_completion(canonical(data))
        client = self.client(1, transport=Mock(return_value=(200, b'broken-json')))
        with self.assertRaises(ModelError): client.complete_json('c'*32, MESSAGES)
        self.assertEqual(self.calls.history()[0]['receipt']['state'], 'response_invalid')

    def test_missing_or_inconsistent_usage_is_unknown_not_zero(self):
        self.assertIsNone(usage_from_response(response(usage=None)))
        self.assertIsNone(usage_from_response(response(usage=dict(prompt_tokens=1, completion_tokens=1, total_tokens=1))))
        self.assertIsNone(usage_from_response(response(usage=dict(prompt_tokens=True, completion_tokens=1, total_tokens=2))))

    def test_policy_and_history_cannot_be_silently_changed(self):
        client = self.client(1)
        client.complete_json('a'*32, MESSAGES)
        with self.assertRaisesRegex(ModelError, 'model_policy_mismatch'):
            ModelCalls(self.path, self.config, max_requests=2)
        with closing(sqlite3.connect(self.path)) as db:
            for table in ('policy', 'calls', 'receipts'):
                with self.assertRaises(sqlite3.IntegrityError): db.execute('DELETE FROM '+table)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ModelError): ModelCalls.open_existing(Path(self.tmp.name)/'missing.sqlite')

    def test_preflight_does_not_spend_a_request(self):
        client = self.client(1)
        for messages in ([], [{'role': 'tool', 'content': 'JSON'}],
                         [{'role': 'user', 'content': 'missing format instruction'}],
                         [{'role': 'user', 'content': 'JSON'+'x'*70000}]):
            with self.assertRaises(ModelError): client.complete_json('a'*32, messages)
        self.assertEqual(self.calls.history(), [])
        self.transport.assert_not_called()

    def test_keepalive_cannot_extend_total_response_deadline(self):
        with patch('auto_lammps.deepseek.http.client.HTTPSConnection') as connection, patch('auto_lammps.deepseek.time.monotonic',side_effect=[0,1,2,31]):
            stream=connection.return_value.getresponse.return_value
            stream.isclosed.return_value=False
            stream.read1.return_value=b' '
            with self.assertRaises(TimeoutError):https_transport(b'{}','synthetic-key',30)
            self.assertEqual(stream.read1.call_count,1)
            connection.return_value.close.assert_called_once()

    def test_fixed_https_transport_does_not_follow_redirects(self):
        with patch('auto_lammps.deepseek.http.client.HTTPSConnection') as connection:
            stream = connection.return_value.getresponse.return_value
            stream.status = 307
            stream.isclosed.return_value = False
            stream.read1.side_effect = [b'redirect',b'']
            self.assertEqual(https_transport(b'{}', 'synthetic-key', 30), (307, b'redirect'))
            # The fixed host is still the only target, and the connection now carries
            # a verifying TLS context instead of relying on an implicit default.
            connection.assert_called_once_with('api.deepseek.com', timeout=30,
                                               context=connection.call_args.kwargs['context'])
            self.assertEqual(connection.call_args.kwargs['context'].verify_mode, ssl.CERT_REQUIRED)
            self.assertEqual(stream.read1.call_count,2)
            connection.return_value.close.assert_called_once()

class AllowanceAmendmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'models.sqlite'
        self.config=DeepSeekConfig('synthetic-model')
        self.calls=ModelCalls(self.path,self.config,max_requests=2)

    def revisions(self):
        with self.calls.transaction() as db:
            return db.execute('SELECT * FROM policy_revisions ORDER BY seq').fetchall()

    def test_allowance_increase_keeps_full_history_costs_unknowns_and_original_policy(self):
        transport=Mock(return_value=(200,response()))
        client=DeepSeekClient(self.calls,transport=transport,key_reader=lambda:'synthetic-key')
        client.complete_json('a'*32,MESSAGES)
        transport.side_effect=TimeoutError('Synthetic unknown request')
        with self.assertRaisesRegex(ModelError,'model_transport_unknown'):
            client.complete_json('b'*32,MESSAGES)
        before=self.calls.history()
        with self.calls.transaction() as db:
            base=db.execute('SELECT document FROM policy').fetchone()[0]
        self.calls.amend_allowance(5,reason_sha256='c'*64)
        reopened=ModelCalls.open_existing(self.path)
        self.assertEqual(reopened.history(),before)
        self.assertEqual(reopened.status()['used_requests'],2)
        self.assertEqual(reopened.status()['remaining_requests'],3)
        self.assertEqual(reopened.config,self.config)
        with reopened.transaction() as db:
            self.assertEqual(db.execute('SELECT document FROM policy').fetchone()[0],base)
        for identifier in ('a'*32,'b'*32):
            with self.assertRaisesRegex(ModelError,'request_already_reserved'):
                reopened.reserve(identifier,b'No retransmission')
        self.assertEqual(transport.call_count,2)

    def test_live_and_reopened_workers_reserve_using_latest_allowance(self):
        old_worker=ModelCalls.open_existing(self.path)
        self.calls.reserve('a'*32,b'first');self.calls.reserve('b'*32,b'second')
        with self.assertRaisesRegex(ModelError,'model_budget_exhausted'):
            old_worker.reserve('c'*32,b'before explicit amendment')
        self.calls.amend_allowance(3,reason_sha256='d'*64)
        self.assertEqual(old_worker.status()['max_requests'],3)
        old_worker.reserve('c'*32,b'after explicit amendment')
        self.assertEqual(ModelCalls.open_existing(self.path).status()['remaining_requests'],0)
        with self.assertRaisesRegex(ModelError,'model_budget_exhausted'):
            self.calls.reserve('d'*32,b'fourth')

    def test_per_call_amendments_preserve_latest_allowance_and_chain_of_authorizations(self):
        from dataclasses import replace
        with self.calls.transaction() as db:
            base=db.execute('SELECT document FROM policy').fetchone()[0]
        self.calls.amend_allowance(5,reason_sha256='a'*64)
        self.calls.amend_limits(replace(self.config,max_output_tokens=16384),reason_sha256='b'*64)
        self.calls.amend_allowance(8,reason_sha256='c'*64)
        self.calls.amend_limits(replace(self.calls.config,timeout_seconds=60),reason_sha256='d'*64)
        reopened=ModelCalls.open_existing(self.path)
        self.assertEqual(reopened.status()['max_requests'],8)
        self.assertEqual(reopened.config.max_output_tokens,16384)
        self.assertEqual(reopened.config.timeout_seconds,60)
        previous=base
        for index,row in enumerate(self.revisions(),1):
            sequence,document,previous_sha,reason,at=row
            self.assertEqual(sequence,index)
            self.assertEqual(previous_sha,sha256(previous.encode()))
            self.assertEqual(reason,('abcd'[index-1])*64)
            self.assertTrue(at)
            previous=document
        self.assertEqual(json.loads(previous)['provider'],'deepseek')
        self.assertEqual(json.loads(previous)['protocol'],json.loads(base)['protocol'])

    def test_allowance_amendment_from_stale_worker_preserves_newer_per_call_policy(self):
        from dataclasses import replace
        stale=ModelCalls.open_existing(self.path)
        self.calls.amend_limits(replace(self.config,max_output_tokens=16384),reason_sha256='b'*64)
        stale.amend_allowance(5,reason_sha256='c'*64)
        reopened=ModelCalls.open_existing(self.path)
        self.assertEqual(reopened.config.max_output_tokens,16384)
        self.assertEqual(reopened.status()['max_requests'],5)
        with self.assertRaisesRegex(ModelError,'model_policy_changed_reload_before_amendment'):
            stale.amend_limits(replace(stale.config,timeout_seconds=60),reason_sha256='d'*64)
        self.assertEqual(len(self.revisions()),2)

    def test_original_constructor_identity_survives_allowance_and_limit_amendments(self):
        from dataclasses import replace
        self.calls.amend_allowance(5,reason_sha256='a'*64)
        self.calls.amend_limits(replace(self.config,max_output_tokens=16384),reason_sha256='b'*64)
        original=ModelCalls(self.path,self.config,max_requests=2)
        self.assertEqual(original.status()['max_requests'],5)
        self.assertEqual(original.config.max_output_tokens,16384)
        with self.assertRaisesRegex(ModelError,'model_policy_mismatch'):
            ModelCalls(self.path,self.config,max_requests=5)
        with self.assertRaisesRegex(ModelError,'model_policy_mismatch'):
            ModelCalls(self.path,original.config,max_requests=2)

    def test_same_allowance_is_idempotent_and_invalid_or_reduced_allowances_never_append(self):
        self.calls.reserve('a'*32,b'first');self.calls.reserve('b'*32,b'second')
        self.calls.amend_allowance(5,reason_sha256='a'*64)
        self.calls.amend_allowance(5,reason_sha256='a'*64)
        before=self.revisions()
        for value in (True,False,0,-1,100001,2.0,'5',None):
            with self.subTest(value=value),self.assertRaisesRegex(ModelError,'invalid_request_limit'):
                self.calls.amend_allowance(value,reason_sha256='b'*64)
        for value in (1,2,4):
            with self.subTest(value=value),self.assertRaisesRegex(ModelError,'cannot_reduce_limit_or_usage'):
                self.calls.amend_allowance(value,reason_sha256='b'*64)
        for reason in (None,'','a'*63,'A'*64,'g'*64):
            with self.subTest(reason=reason),self.assertRaisesRegex(ModelError,'requires_evidence'):
                self.calls.amend_allowance(8,reason_sha256=reason)
        self.assertEqual(self.revisions(),before)
        self.assertEqual(self.calls.status()['max_requests'],5)
        self.assertEqual(self.calls.status()['used_requests'],2)

    def test_concurrent_duplicate_amendments_append_once_and_workers_share_new_total(self):
        workers=[ModelCalls.open_existing(self.path) for _ in range(8)]
        self.calls.reserve('a'*32,b'original')
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda worker:worker.amend_allowance(4,reason_sha256='b'*64),workers))
        self.assertEqual(len(self.revisions()),1)
        def reserve(item):
            index,worker=item
            try:
                worker.reserve(f'{index:032x}',b'new accounted intent')
                return 'reserved'
            except ModelError as error:
                return str(error)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(reserve,enumerate(workers)))
        self.assertEqual(results.count('reserved'),3)
        self.assertEqual(results.count('model_budget_exhausted'),5)
        self.assertEqual(self.calls.status()['used_requests'],4)
        self.assertEqual(self.calls.status()['remaining_requests'],0)

    def test_zero_base_allowance_requires_explicit_increase_and_maximum_bound_is_preserved(self):
        path=Path(self.tmp.name)/'zero.sqlite'
        calls=ModelCalls(path,self.config,max_requests=0)
        with self.assertRaisesRegex(ModelError,'model_budget_exhausted'):
            calls.reserve('a'*32,b'not authorized')
        calls.amend_allowance(1,reason_sha256='a'*64)
        calls.reserve('a'*32,b'explicitly authorized synthetic intent')
        calls.amend_allowance(100000,reason_sha256='b'*64)
        self.assertEqual(calls.status()['max_requests'],100000)
        self.assertEqual(calls.status()['used_requests'],1)
        with calls.transaction() as db:
            self.assertEqual(json.loads(db.execute('SELECT document FROM policy').fetchone()[0])['max_requests'],0)
            for table in ('policy','policy_revisions','calls'):
                with self.assertRaises(sqlite3.IntegrityError):db.execute('DELETE FROM '+table)


class LenientJsonTests(unittest.TestCase):
    """字符串内的裸换行（模型写多行脚本时常见）应被容错解析，其他错误不放宽。"""

    def test_raw_newline_inside_string_is_escaped(self):
        from auto_lammps.deepseek import lenient_json
        text = '{"workflow": "minimize 1e-10 1e-10\nrun 0", "ok": 1}'
        # 上面的字面量本就是合法转义；这里构造真正的裸换行
        raw = '{"workflow": "line one' + chr(10) + 'line two", "ok": 1}'
        self.assertEqual(lenient_json(raw)['workflow'], 'line one' + chr(10) + 'line two')

    def test_other_malformed_json_still_raises(self):
        from auto_lammps.deepseek import ModelError, lenient_json
        with self.assertRaises(ModelError):
            lenient_json('{"a": ')

