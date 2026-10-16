"""Synthetic author-plan quota, dispatch and retained-answer recovery checks."""
from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import patch

from auto_lammps.deepseek import request_body
from auto_lammps.manifest import canonical, sha256
from auto_lammps.tasks import TaskError
import test_author_reference_plan as fixture


class AuthorPlanRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.AuthorReferencePlanTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.request = '4' * 32

    def assert_recoverable(self, record):
        case = self.fx
        self.assertEqual(record['state'], 'preparation_paused')
        self.assertEqual((record['request_id'], record['round']), (self.request, 1))
        self.assertIsNone(record['receipt'])
        self.assertIsNone(record['report'])
        self.assertIsNone(case.service._stored(case.task['id'], self.request)[2])
        self.assertEqual(case.service.status(case.task['id'])['used_rounds'], 1)

    def test_01_zero_allowance_creates_no_author_intent_or_round(self):
        case = self.fx
        # Spend only synthetic local model-intent slots, with no transport.
        allowance = case.calls.status()['max_requests']
        for index in range(allowance):
            case.calls.reserve(format(index, '032x'), b'synthetic unrelated prior intent')
        previous = case.calls.history()
        self.assertEqual(case.calls.status()['remaining_requests'], 0)
        for request in ('4' * 32, '5' * 32, '6' * 32):
            with self.assertRaises(TaskError):
                case.prepare(request)
        self.assertEqual(case.service.history(case.task['id']), [])
        status = case.service.status(case.task['id'])
        self.assertEqual((status['used_rounds'], status['remaining_rounds']), (0, 3))
        with case.tasks.transaction() as database:
            self.assertEqual(database.execute('SELECT count(*) FROM author_plan_intents WHERE task_id=?',
                                              (case.task['id'],)).fetchone()[0], 0)
            self.assertEqual(database.execute('SELECT count(*) FROM author_plan_events').fetchone()[0], 0)
        self.assertEqual(case.sent, [])
        self.assertEqual(case.calls.history(), previous)
        case.calls.amend_allowance(allowance + 1, reason_sha256='c' * 64)
        recovered = case.prepare(service=case.make_service())
        self.assertEqual((recovered['state'], recovered['round']), ('prepared', 1))
        self.assertEqual(len(case.sent), 1)
        self.assertEqual(case.calls.status()['used_requests'], allowance + 1)
        remaining_old = [row for row in case.calls.history() if row['request_id'] != self.request]
        self.assertEqual(remaining_old, previous)

    def test_02_dispatch_body_guard_pauses_without_send_then_recovers_original_id(self):
        case = self.fx
        event = case.service._event
        original_config = case.calls.config
        changed = []
        original_records = case.fixture.snapshot_old_records()

        def change_after_preflight(database, request, state, message=''):
            event(database, request, state, message)
            if state == 'dispatching':
                case.calls.amend_limits(replace(original_config,
                    max_output_tokens=original_config.max_output_tokens + 1), reason_sha256='d' * 64)
                changed.append(True)

        with patch.object(case.service, '_event', side_effect=change_after_preflight):
            paused = case.prepare()
        self.assertEqual(changed, [True])
        self.assert_recoverable(paused)
        context = case.service._stored(case.task['id'], self.request)[1]
        changed_body = request_body(case.calls.config, context['messages'], model=case.client.model,
                                    thinking=case.client.thinking)
        self.assertNotEqual(sha256(changed_body), context['request_sha256'])
        self.assertIsNone(case.calls.lookup(self.request))
        self.assertEqual(case.calls.status()['used_requests'], 0)
        self.assertEqual(case.sent, [])
        case.calls.amend_limits(original_config, reason_sha256='e' * 64)
        restarted = case.make_service()
        recovered = case.prepare(service=restarted)
        self.assertEqual((recovered['state'], recovered['request_id'], recovered['round']),
                         ('prepared', self.request, 1))
        self.assertEqual(recovered['context_sha256'], paused['context_sha256'])
        self.assertIn('preparation_paused', [event['state'] for event in recovered['events']])
        self.assertEqual(len(case.sent), 1)
        self.assertEqual(case.calls.status()['used_requests'], 1)
        self.assertEqual(restarted.status(case.task['id'])['used_rounds'], 1)
        self.assertEqual(case.fixture.snapshot_old_records(), original_records)

    def test_03_transient_source_preflight_keeps_unsent_context_recoverable(self):
        case = self.fx
        original_records = case.fixture.snapshot_old_records()
        case.provider.side_effect = [deepcopy(case.input), TaskError('PRIVATE_SYNTHETIC_SOURCE_CANARY')]
        paused = case.prepare()
        self.assert_recoverable(paused)
        self.assertEqual(case.sent, [])
        self.assertEqual(case.calls.status()['used_requests'], 0)
        self.assertNotIn('PRIVATE_SYNTHETIC_SOURCE_CANARY', canonical(paused).decode())
        history = case.service.history(case.task['id'])
        self.assertEqual(len(history), 1)
        self.assertFalse(paused['recoverable'])  # prepare still held the lease at return time
        self.assertTrue(history[0]['recoverable'])  # history sees the released lease
        self.assertEqual({key: value for key, value in history[0].items() if key != 'recoverable'},
                         {key: value for key, value in paused.items() if key != 'recoverable'})
        case.provider.side_effect = lambda _: deepcopy(case.input)
        restarted = case.make_service()
        recovered = case.prepare(service=restarted)
        self.assertEqual((recovered['state'], recovered['request_id'], recovered['round']),
                         ('prepared', self.request, 1))
        self.assertEqual(recovered['context_sha256'], paused['context_sha256'])
        self.assertIn('preparation_paused', [event['state'] for event in recovered['events']])
        self.assertEqual(len(case.sent), 1)
        self.assertEqual(case.calls.status()['used_requests'], 1)
        self.assertEqual(case.fixture.snapshot_old_records(), original_records)

    def test_04_completed_reply_survives_repeated_source_outage_without_new_model_call(self):
        case = self.fx
        transport = case.transport
        original_records = case.fixture.snapshot_old_records()

        def reply_then_lose_source(body, key, timeout):
            returned = transport(body, key, timeout)
            case.provider.side_effect = TaskError('PRIVATE_SYNTHETIC_RETURN_CANARY')
            return returned

        case.client.transport = reply_then_lose_source
        paused = case.prepare()
        self.assert_recoverable(paused)
        paid_call = case.calls.lookup(self.request)
        self.assertEqual(paid_call['receipt']['state'], 'completed')
        self.assertEqual(paid_call['receipt']['structured_output'], case.value)
        self.assertEqual(len(case.sent), 1)
        self.assertEqual(case.calls.status()['used_requests'], 1)
        # A browser may retry while the same temporary outage is still active.
        # This must stay recoverable rather than create a terminal paused receipt.
        restarted = case.make_service()
        still_paused = case.prepare(service=restarted)
        self.assert_recoverable(still_paused)
        self.assertGreater(len(still_paused['events']), len(paused['events']))
        self.assertEqual(case.calls.lookup(self.request), paid_call)
        self.assertEqual(len(case.sent), 1)
        case.provider.side_effect = lambda _: deepcopy(case.input)
        recovered = case.prepare(service=case.make_service())
        self.assertEqual((recovered['state'], recovered['request_id'], recovered['round']),
                         ('prepared', self.request, 1))
        self.assertEqual(recovered['context_sha256'], paused['context_sha256'])
        self.assertEqual(case.calls.lookup(self.request), paid_call)
        self.assertEqual(len(case.sent), 1)
        self.assertEqual(case.calls.status()['used_requests'], 1)
        self.assertEqual(case.service.status(case.task['id'])['used_rounds'], 1)
        self.assertEqual(case.fixture.snapshot_old_records(), original_records)


if __name__ == '__main__':
    unittest.main()
