"""Author A conversations stay separate from research B; synthetic transport only."""
from datetime import datetime, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from auto_lammps.deepseek import DeepSeekConfig, ModelCalls
from auto_lammps.manifest import canonical, sha256
from auto_lammps.scientific_adapters import MANDATORY_INSTRUCTION
from auto_lammps.tasks import TaskError
import test_model_connections as model_fixture


class ReferenceDiscussionTests(unittest.TestCase):
    def setUp(self):
        model_fixture.ModelConnectionTests.setUp(self)
        self.calls = ModelCalls(Path(self.tmp.name)/'models.sqlite', DeepSeekConfig('synthetic-model'), max_requests=20)
        self.connections.calls = self.calls
        self.connections.save('deepseek-official', 'synthetic-model', 'synthetic-secret')
        self.task = self.store.create('Synthetic B task', 'B_CONDITION_CANARY', 'research')
        self.source = 'a'*64

    def context(self, source=None, **extra):
        return dict(source_sha256=source or self.source, role='author_reference_A_human_only',
                    tables=[{'A_TABLE_CANARY':'synthetic retained A observation'}], **extra)

    def discuss_A(self, rid, question='A_QUESTION_CANARY', source=None, context=None, provider='deepseek-official'):
        source = source or self.source
        return self.connections.discuss(self.task['id'], rid, provider, question,
            context if context is not None else self.context(source), channel='author_reference', evidence_sha256=source)

    def payload(self):
        return self.transport.call_args.args[4]

    def test_A_and_B_history_and_payload_are_disjoint_without_creating_tasks(self):
        original = self.store.list(); original_history = self.store.history(self.task['id'])
        self.transport.return_value['choices'][0]['message']['content'] = 'A_ANSWER_CANARY'
        self.discuss_A('1'*32)
        self.assertEqual(self.connections.history(self.task['id']), [])
        self.transport.return_value['choices'][0]['message']['content'] = 'B_ANSWER_CANARY'
        self.connections.discuss(self.task['id'], '2'*32, 'deepseek-official', 'B_QUESTION_CANARY',
                                  {'retained_B':'B_TABLE_CANARY'})
        payload = json.dumps(self.payload())
        for canary in ('A_QUESTION_CANARY','A_ANSWER_CANARY','A_TABLE_CANARY'):
            self.assertNotIn(canary, payload)
        self.discuss_A('3'*32, 'A_SECOND_QUESTION_CANARY')
        payload = json.dumps(self.payload())
        for canary in ('B_QUESTION_CANARY','B_ANSWER_CANARY','B_TABLE_CANARY','B_CONDITION_CANARY'):
            self.assertNotIn(canary, payload)
        self.assertIn('A_QUESTION_CANARY', payload); self.assertIn('A_ANSWER_CANARY', payload)
        self.assertEqual(len(self.connections.history(self.task['id'])), 1)
        A = self.connections.history(self.task['id'], channel='author_reference', evidence_sha256=self.source)
        self.assertEqual(len(A), 2); self.assertTrue(all(row['task_id']==self.task['id'] for row in A))
        self.assertEqual(self.store.list(), original); self.assertEqual(self.store.history(self.task['id']), original_history)
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)

    def test_same_request_cannot_switch_channels_or_reference_sources(self):
        self.discuss_A('1'*32)
        with self.assertRaises(TaskError):
            self.connections.discuss(self.task['id'], '1'*32, 'deepseek-official', 'A_QUESTION_CANARY', {'synthetic_B':True})
        with self.assertRaises(TaskError): self.discuss_A('1'*32, source='b'*64)
        self.connections.discuss(self.task['id'], '2'*32, 'deepseek-official', 'B_FIRST', {})
        with self.assertRaises(TaskError): self.discuss_A('2'*32, 'B_FIRST')
        self.assertEqual(self.transport.call_count, 2); self.assertEqual(self.calls.status()['used_requests'], 2)

    def test_source_change_clears_only_active_reference_conversation(self):
        self.transport.return_value['choices'][0]['message']['content'] = 'OLD_SOURCE_ANSWER_CANARY'
        self.discuss_A('1'*32, 'OLD_SOURCE_QUESTION_CANARY')
        new_source = 'b'*64
        self.assertEqual(self.connections.history(self.task['id'], channel='author_reference', evidence_sha256=new_source), [])
        self.discuss_A('2'*32, 'NEW_SOURCE_QUESTION', source=new_source)
        payload = json.dumps(self.payload())
        self.assertNotIn('OLD_SOURCE_QUESTION_CANARY', payload); self.assertNotIn('OLD_SOURCE_ANSWER_CANARY', payload)
        old = self.connections.history(self.task['id'], channel='author_reference', evidence_sha256=self.source)
        self.assertEqual(old[0]['answer'], 'OLD_SOURCE_ANSWER_CANARY')
        self.assertEqual(self.connections.history(self.task['id']), [])

    def test_completed_duplicate_and_failed_transport_never_resend(self):
        rid = '1'*32
        answer = self.discuss_A(rid)
        self.assertEqual(self.discuss_A(rid), answer)
        self.transport.assert_called_once(); self.assertEqual(self.calls.status()['used_requests'], 1)
        self.transport.side_effect = RuntimeError('synthetic-secret')
        failed = self.discuss_A('2'*32, 'FAILED_A_REQUEST')
        self.assertEqual(failed['state'], 'failed_or_unknown')
        self.assertEqual(self.discuss_A('2'*32, 'FAILED_A_REQUEST'), failed)
        self.assertEqual(self.transport.call_count, 2); self.assertEqual(self.calls.status()['used_requests'], 2)
        self.assertNotIn('synthetic-secret', json.dumps(failed))

    def test_unknown_intent_without_answer_is_not_resent_after_restart(self):
        rid = '1'*32
        namespace = sha256(canonical({'task':self.task['id'], 'role':'author_reference', 'source_sha256':self.source}))[:32]
        with self.store.transaction() as db:
            db.execute('INSERT INTO reference_result_questions VALUES (?,?,?,?,?,?,?)',
                (rid, namespace, 'deepseek-official', 'synthetic-model', 'UNKNOWN_A', 'c'*64,
                 datetime.now(timezone.utc).isoformat()))
        self.calls.reserve(rid, canonical({'synthetic_unknown_intent':True}))
        result = self.discuss_A(rid, 'UNKNOWN_A')
        self.assertEqual(result['state'], 'unknown'); self.assertIsNone(result['answer'])
        self.transport.assert_not_called(); self.assertEqual(self.calls.status()['used_requests'], 1)
        self.assertIsNone(self.calls.lookup(rid)['receipt'])

    def test_invalid_reference_context_rejected_before_payment_or_history(self):
        bad_contexts = [None, 'invalid', {}, self.context('b'*64),
                        {**self.context(), 'role':'independent_B'},
                        self.context(frozen_scientific_conditions={'B_SECRET_CANARY':True})]
        for index, context in enumerate(bad_contexts):
            with self.subTest(index=index), self.assertRaises(TaskError):
                self.connections.discuss(self.task['id'], format(index+1, '032x'), 'deepseek-official', 'invalid',
                    context, channel='author_reference', evidence_sha256=self.source)
        for channel, source in (('author_reference', None), ('author_reference', 'A'*64),
                                ('research', self.source), ('invented', self.source)):
            with self.subTest(channel=channel, source=source), self.assertRaises(TaskError):
                self.connections.discuss(self.task['id'], 'f'*32, 'deepseek-official', 'invalid', self.context(),
                                           channel=channel, evidence_sha256=source)
        self.transport.assert_not_called(); self.assertEqual(self.calls.status()['used_requests'], 0)
        self.assertEqual(self.connections.history(self.task['id'], channel='author_reference', evidence_sha256=self.source), [])
        self.assertEqual(self.connections.history(self.task['id']), [])

    def test_mandatory_adapter_in_actual_payload_and_accounted_receipt(self):
        rid = '1'*32
        self.discuss_A(rid)
        payload = self.payload(); messages = payload['messages']
        self.assertIn(MANDATORY_INSTRUCTION, messages[0]['content'])
        data = json.loads(messages[-1]['content']); adapter = data['scientific_adapter']
        self.assertEqual(adapter['stage'], 'result_discussion')
        self.assertFalse(adapter['rules']['model_may_disable_checks'])
        self.assertEqual(data['verified_results'], self.context())
        receipt = self.calls.lookup(rid)['receipt']
        self.assertEqual(receipt['purpose'], 'author_reference_discussion')
        self.assertEqual(receipt['request_sha256'], sha256(canonical(payload)))
        self.assertEqual(receipt['scientific_adapter']['stage'], 'result_discussion')
        self.assertEqual(receipt['scientific_adapter']['output_check'], 'text_shape_only_prose_claims_not_verified')
        self.assertEqual(receipt['usage']['total_tokens'], 12)

    def test_missing_mandatory_adapter_blocks_reference_dispatch(self):
        with patch('auto_lammps.scientific_adapters.stage_context', return_value=None):
            with self.assertRaises(TaskError): self.discuss_A('1'*32)
        self.transport.assert_not_called(); self.assertEqual(self.calls.status()['used_requests'], 0)
        self.assertEqual(self.connections.history(self.task['id'], channel='author_reference', evidence_sha256=self.source), [])

    def test_all_supported_protocols_receive_reference_role_and_mandatory_adapter(self):
        for index, provider in enumerate(('deepseek-official','glm','openai','anthropic')):
            self.connections.save(provider, 'synthetic-model', 'synthetic-secret')
            self.transport.return_value = ({'content':[{'type':'text','text':'合成作者参考解释'}], 'usage':{'input_tokens':4}}
                if provider == 'anthropic' else {'choices':[{'message':{'content':'合成作者参考解释'}}], 'usage':{'total_tokens':12}})
            rid = format(index+1, '032x')
            result = self.discuss_A(rid, '仅解释 A', provider=provider)
            self.assertEqual(result['state'], 'completed')
            payload = self.payload(); system = payload['system'] if provider=='anthropic' else payload['messages'][0]['content']
            self.assertIn(MANDATORY_INSTRUCTION, system); self.assertIn('author reference A analysis', system)
            self.assertEqual(json.loads(payload['messages'][-1]['content'])['scientific_adapter']['stage'], 'result_discussion')
            count = self.transport.call_count
            self.discuss_A(rid, '仅解释 A', provider=provider)
            self.assertEqual(self.transport.call_count, count)
        self.assertEqual(self.connections.history(self.task['id']), [])
        self.assertEqual(len(self.store.list()), 1)
