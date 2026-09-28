"""Synthetic transport and accounting checks; no API credential or live request."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from auto_lammps.deepseek import (DeepSeekClient, DeepSeekConfig, MAX_RESPONSE_BYTES, ModelCalls,
                                  ModelError, https_transport, parse_completion, request_body, usage_from_response)
from auto_lammps.manifest import canonical

MESSAGES = [{'role': 'system', 'content': 'Return JSON: {"conditions": [], "questions": []}'}]


def response(value=None, **changes):
    doc = dict(model='synthetic-model', choices=[dict(finish_reason='stop', message=dict(
        role='assistant', content=json.dumps(value if value is not None else {'conditions': [], 'questions': []})))],
        usage=dict(prompt_tokens=20, completion_tokens=10, total_tokens=30))
    doc.update(changes)
    return canonical(doc)


class DeepSeekTests(unittest.TestCase):
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

    def test_fixed_https_transport_does_not_follow_redirects(self):
        with patch('auto_lammps.deepseek.http.client.HTTPSConnection') as connection:
            stream = connection.return_value.getresponse.return_value
            stream.status = 307
            stream.read.return_value = b'redirect'
            self.assertEqual(https_transport(b'{}', 'synthetic-key', 30), (307, b'redirect'))
            connection.assert_called_once_with('api.deepseek.com', timeout=30)
            stream.read.assert_called_once_with(MAX_RESPONSE_BYTES+1)
            connection.return_value.close.assert_called_once()
