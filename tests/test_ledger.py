"""Synthetic accounting tests. No SSH, LAMMPS or model requests."""
from contextlib import closing
from dataclasses import replace
import multiprocessing as mp
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from auto_lammps.ledger import Conflict, Ledger, LedgerError, LimitExceeded, Policy, Resources
from auto_lammps.submission import SchedulerRejected, SubmissionService

H1 = 'a' * 64
H2 = 'b' * 64
H3 = 'c' * 64
POLICY = Policy(1000, 8, 100, 1024, 10000, 2, H1)
RESOURCE = Resources(2, 10, 100, 100)


class FakeScheduler:
    def __init__(self, reply='101', error=None):
        self.reply, self.error, self.calls = reply, error, []

    def submit(self, request):
        self.calls.append(request)
        if self.error:
            raise self.error
        return self.reply


def reserve_worker(path, evaluation, key, barrier, queue):
    ledger = Ledger(Path(path))
    barrier.wait(timeout=10)
    try:
        row = ledger.reserve(evaluation, key, H2, RESOURCE)
        queue.put(('reserved', row['id']))
    except (Conflict, LimitExceeded) as exc:
        queue.put((type(exc).__name__, None))


def dispatch_worker(path, evaluation, barrier, count, queue):
    class CounterScheduler:
        def submit(self, request):
            with count.get_lock():
                count.value += 1
            return '909'
    ledger = Ledger(Path(path))
    barrier.wait(timeout=10)
    row = SubmissionService(ledger, CounterScheduler()).submit(evaluation, 'same-key', H2, RESOURCE)
    queue.put(row['id'])


def crash_after_fake_acceptance(path, evaluation, receipt_path):
    class CrashScheduler:
        def submit(self, request):
            # Stand in for a scheduler accepting the job, then the worker dying
            # before its receipt reaches the ledger. No external command runs.
            Path(receipt_path).write_text(request.request_id)
            os._exit(17)
    SubmissionService(Ledger(Path(path)), CrashScheduler()).submit(evaluation, 'crash', H2, RESOURCE)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'ledger.sqlite'
        self.ledger = Ledger(self.path)
        self.ledger.create_campaign('campaign', POLICY)
        self.evaluation = self.register()

    def tearDown(self):
        self.tmp.cleanup()

    def register(self, repetition=0, campaign='campaign', role='agent'):
        return self.ledger.register_evaluation(campaign, task_sha256=H1, repetition=repetition,
                                               role=role, system_sha256=H2)

    def reserve(self, key='request', evaluation=None, resources=RESOURCE):
        return self.ledger.reserve(evaluation or self.evaluation, key, H2, resources)

    def rejected_attempt(self, key):
        row = self.reserve(key)
        self.assertTrue(self.ledger.begin_dispatch(row['id']))
        self.ledger.rejected(row['id'], {'synthetic': True})
        return row

    def accepted_job(self, key='request', job_id='101'):
        row = self.reserve(key)
        self.ledger.begin_dispatch(row['id'])
        self.ledger.accepted(row['id'], job_id, {'synthetic': True})
        return row

    def test_durable_policy_identity_and_permission(self):
        same = Ledger(self.path)
        same.create_campaign('campaign', POLICY)
        with self.assertRaises(Conflict):
            same.create_campaign('campaign', replace(POLICY, total_core_seconds=2000))
        self.assertEqual(self.register(), self.evaluation)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(Conflict):
            same.register_evaluation('campaign', task_sha256=H1, repetition=0,
                                     role='agent', system_sha256=H2, max_attempts=1)

    def test_cannot_modify_unrelated_database(self):
        other = Path(self.tmp.name) / 'other.sqlite'
        with closing(sqlite3.connect(other)) as db:
            db.execute('CREATE TABLE research(value INTEGER)')
        other.chmod(0o600)
        before = other.read_bytes()
        with self.assertRaises(LedgerError):
            Ledger(other)
        self.assertEqual(before, other.read_bytes())

    def test_symlink_public_mode_and_git_rejected(self):
        link = Path(self.tmp.name) / 'alias.sqlite'
        link.symlink_to(self.path)
        with self.assertRaises(ValueError):
            Ledger(link)
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            Ledger(self.path)
        (Path(self.tmp.name) / '.git').mkdir()
        with self.assertRaises(ValueError):
            Ledger(Path(self.tmp.name) / 'new.sqlite')

    def test_idempotency_conflicts_and_restart(self):
        row = self.reserve()
        fresh = Ledger(self.path)
        self.assertEqual(row['id'], fresh.reserve(self.evaluation, 'request', H2, RESOURCE)['id'])
        for digest, resource in [(H3, RESOURCE), (H2, replace(RESOURCE, cores=3))]:
            with self.assertRaises(Conflict):
                fresh.reserve(self.evaluation, 'request', digest, resource)
        self.assertEqual(len(fresh.recoverable()), 1)
        self.assertEqual([x['kind'] for x in fresh.events(row['id'])], ['reserved'])

    def test_two_rejections_count_and_cannot_rename_evaluation(self):
        self.rejected_attempt('first')
        self.rejected_attempt('second')
        same_evaluation = self.register()
        with self.assertRaises(LimitExceeded):
            self.reserve('new-name', same_evaluation)
        self.assertEqual(len(self.ledger.recoverable()), 0)

    def test_unknown_dispatch_holds_attempt_concurrency_and_cost(self):
        row = self.reserve()
        self.ledger.begin_dispatch(row['id'])
        self.ledger.uncertain(row['id'], {'reason': 'lost connection'})
        fresh = Ledger(self.path)
        self.assertFalse(fresh.begin_dispatch(row['id']))
        self.assertEqual(fresh.get(row['id'])['charge_core_seconds'], RESOURCE.core_seconds)
        with self.assertRaises(Conflict):
            self.reserve('again')
        with self.assertRaises(Conflict):
            fresh.cancel_intent(row['id'])
        fresh.accepted(row['id'], '88', {'lookup': 'exact_request_id'})
        self.assertEqual(fresh.get(row['id'])['job_id'], '88')

    def test_intent_without_receipt_is_not_automatically_retried(self):
        row = self.reserve()
        self.ledger.begin_dispatch(row['id'])
        fresh = Ledger(self.path)
        self.assertEqual(fresh.recoverable()[0]['state'], 'dispatching')
        scheduler = FakeScheduler()
        SubmissionService(fresh, scheduler).submit(self.evaluation, 'request', H2, RESOURCE)
        self.assertEqual(scheduler.calls, [])
        summary = fresh.summary('campaign')
        self.assertEqual(summary['dispatch_intents'], 1)
        self.assertEqual(summary['accepted_jobs'], 0)
        self.assertEqual(summary['unresolved_dispatches'], 1)
        self.assertEqual(summary['accounted_core_seconds'], 0)
        self.assertEqual(summary['charged_or_reserved_core_seconds'], RESOURCE.core_seconds)

    def test_per_job_limits_and_integer_validation(self):
        for resource in [replace(RESOURCE, cores=9), replace(RESOURCE, wall_seconds=101),
                         replace(RESOURCE, memory_bytes=1025), replace(RESOURCE, storage_bytes=10001)]:
            with self.subTest(resource=resource), self.assertRaises(LimitExceeded):
                self.reserve(resources=resource)
        for value in [0, -1, True, 1.5, '2']:
            with self.assertRaises(ValueError):
                Resources(value, 10, 10, 10)

    def test_cancel_before_dispatch_is_not_scheduler_request(self):
        row = self.reserve()
        self.ledger.cancel_intent(row['id'])
        self.assertFalse(self.ledger.begin_dispatch(row['id']))
        self.assertEqual(self.ledger.get(row['id'])['dispatch_claimed'], 0)
        self.assertEqual(self.ledger.get(row['id'])['charge_core_seconds'], 0)
        self.reserve('replacement')

    def test_cancel_after_acceptance_waits_for_scheduler(self):
        row = self.accepted_job()
        self.ledger.cancel_intent(row['id'])
        self.ledger.observe(row['id'], '101', 'running', {'synthetic': True})
        self.assertEqual(self.ledger.get(row['id'])['state'], 'cancelling')
        with self.assertRaises(Conflict):
            self.reserve('too-early')
        self.ledger.observe(row['id'], '101', 'cancelled', {'synthetic': True})
        self.reserve('second')
        self.assertEqual(self.ledger.get(row['id'])['charge_core_seconds'], RESOURCE.core_seconds)

    def test_rejection_keeps_staged_storage(self):
        row = self.rejected_attempt('first')
        self.assertEqual(self.ledger.get(row['id'])['charge_storage_bytes'], RESOURCE.storage_bytes)
        self.assertEqual(self.ledger.get(row['id'])['charge_core_seconds'], 0)

    def test_final_accounting_records_overrun_without_hiding_it(self):
        row = self.accepted_job()
        with self.assertRaises(Conflict):
            self.ledger.account(row['id'], 10, H3)
        self.ledger.observe(row['id'], '101', 'completed', {'synthetic': True})
        self.ledger.account(row['id'], 1001, H3)
        before = len(self.ledger.events(row['id']))
        self.ledger.account(row['id'], 1001, H3)
        self.assertEqual(before, len(self.ledger.events(row['id'])))
        self.assertEqual(self.ledger.get(row['id'])['charge_core_seconds'], 1001)
        with self.assertRaises(LimitExceeded):
            self.reserve('second')
        with self.assertRaises(Conflict):
            self.ledger.account(row['id'], 0, H3)

    def test_completed_scheduler_job_is_not_scientific_success(self):
        row = self.accepted_job()
        self.ledger.observe(row['id'], '101', 'completed', {'synthetic': True})
        result = self.ledger.get(row['id'])
        self.assertEqual(result['state'], 'completed')
        self.assertNotIn('scientific_success', result)
        self.assertEqual(result['accounted'], 0)

    def test_prepared_job_cannot_dispatch_after_another_job_overruns(self):
        first = self.accepted_job()
        second = self.reserve('second', self.register(repetition=1))
        self.ledger.observe(first['id'], '101', 'completed', {})
        self.ledger.account(first['id'], POLICY.total_core_seconds + 1, H3)
        with self.assertRaises(LimitExceeded):
            self.ledger.begin_dispatch(second['id'])
        self.assertEqual(self.ledger.get(second['id'])['state'], 'prepared')

    def test_stale_or_requeued_terminal_job_blocks_until_reconciled(self):
        row = self.accepted_job()
        self.ledger.observe(row['id'], '101', 'completed', {'synthetic': True})
        self.ledger.account(row['id'], 1, H3)
        self.ledger.observe(row['id'], '101', 'running', {'restart_count': 1})
        self.assertEqual(self.ledger.get(row['id'])['state'], 'reconcile_required')
        self.assertEqual(self.ledger.get(row['id'])['charge_core_seconds'], RESOURCE.core_seconds + 1)
        self.ledger.observe(row['id'], '101', 'running', {'restart_count': 1})
        self.assertEqual(self.ledger.get(row['id'])['charge_core_seconds'], RESOURCE.core_seconds + 1)
        self.assertEqual(self.ledger.summary('campaign')['accounted_core_seconds'], 1)
        self.assertEqual(self.ledger.events(row['id'])[-1]['kind'], 'scheduler_observation_conflict')
        with self.assertRaises(Conflict):
            self.reserve('again')

    def test_events_are_append_only(self):
        row = self.reserve()
        with closing(sqlite3.connect(self.path)) as db:
            for sql in ['DELETE FROM events', "UPDATE events SET kind='edited'"]:
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(sql)
        self.assertEqual(len(self.ledger.events(row['id'])), 1)

    def test_audit_failure_rolls_back_reservation_and_dispatch(self):
        with patch.object(self.ledger, '_event', side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError):
                self.reserve()
        self.assertEqual(self.ledger.recoverable(), [])
        row = self.reserve()
        scheduler = FakeScheduler()
        with patch.object(self.ledger, '_event', side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError):
                SubmissionService(self.ledger, scheduler).submit(self.evaluation, 'request', H2, RESOURCE)
        self.assertEqual(scheduler.calls, [])
        self.assertEqual(self.ledger.get(row['id'])['state'], 'prepared')

    def test_controller_dispatches_once_and_classifies_outcomes(self):
        for index, (response, error, expected) in enumerate([('22', None, 'queued'), ('bad', None, 'unknown'),
                                          ('22', TimeoutError(), 'unknown'),
                                          ('22', SchedulerRejected(), 'rejected')]):
            with self.subTest(expected=expected):
                role_eval = self.register(repetition=index + 1)
                key = f'case-{response}-{type(error).__name__}'
                scheduler = FakeScheduler(response, error)
                service = SubmissionService(self.ledger, scheduler)
                row = service.submit(role_eval, key, H2, RESOURCE)
                self.assertEqual(row['state'], expected)
                service.submit(role_eval, key, H2, RESOURCE)
                self.assertEqual(len(scheduler.calls), 1)
                if expected == 'queued':
                    self.ledger.observe(row['id'], response, 'completed', {})
                elif expected == 'unknown':
                    self.ledger.rejected(row['id'], {'synthetic_reconciliation': True})

    def run_processes(self, target, args):
        context = mp.get_context('spawn')
        queue = context.Queue()
        barrier = context.Barrier(len(args))
        children = [context.Process(target=target, args=(*a, barrier, queue)) for a in args]
        for child in children:
            child.start()
        try:
            results = [queue.get(timeout=20) for _ in children]
        finally:
            for child in children:
                child.join(timeout=5)
                if child.is_alive():
                    child.terminate()
                    child.join()
                self.assertEqual(child.exitcode, 0)
        return results

    def test_processes_compete_for_final_attempt(self):
        self.rejected_attempt('first')
        results = self.run_processes(reserve_worker,
            [(str(self.path), self.evaluation, f'worker-{i}') for i in range(3)])
        self.assertEqual(sum(r[0] == 'reserved' for r in results), 1)

    def test_processes_compete_for_final_budget(self):
        self.ledger.create_campaign('small', replace(POLICY, total_core_seconds=RESOURCE.core_seconds))
        evaluations = [self.register(i, 'small') for i in range(2)]
        results = self.run_processes(reserve_worker,
            [(str(self.path), e, f'worker-{i}') for i, e in enumerate(evaluations)])
        self.assertEqual(sum(r[0] == 'reserved' for r in results), 1)
        self.assertEqual(sum(r[0] == 'LimitExceeded' for r in results), 1)

    def test_processes_compete_for_concurrency(self):
        self.ledger.create_campaign('serial', replace(POLICY, concurrency=1))
        evaluations = [self.register(i, 'serial') for i in range(2)]
        results = self.run_processes(reserve_worker,
            [(str(self.path), e, f'worker-{i}') for i, e in enumerate(evaluations)])
        self.assertEqual(sum(r[0] == 'reserved' for r in results), 1)

    def test_duplicate_concurrent_call_invokes_scheduler_once(self):
        context = mp.get_context('spawn')
        barrier, counter, queue = context.Barrier(3), context.Value('i', 0), context.Queue()
        children = [context.Process(target=dispatch_worker,
                    args=(str(self.path), self.evaluation, barrier, counter, queue)) for _ in range(3)]
        for child in children:
            child.start()
        ids = [queue.get(timeout=20) for _ in children]
        for child in children:
            child.join(timeout=10)
            self.assertEqual(child.exitcode, 0)
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(counter.value, 1)
        self.assertEqual(self.ledger.get(ids[0])['state'], 'queued')

    def test_actual_worker_exit_after_fake_acceptance_recovers_without_resubmit(self):
        receipt = Path(self.tmp.name) / 'synthetic-receipt'
        child = mp.get_context('spawn').Process(target=crash_after_fake_acceptance,
                    args=(str(self.path), self.evaluation, str(receipt)))
        child.start()
        child.join(timeout=10)
        if child.is_alive():
            child.terminate()
            child.join()
        self.assertEqual(child.exitcode, 17)
        request_id = receipt.read_text()
        fresh = Ledger(self.path)
        self.assertEqual(fresh.get(request_id)['state'], 'dispatching')
        scheduler = FakeScheduler()
        service = SubmissionService(fresh, scheduler)
        service.submit(self.evaluation, 'crash', H2, RESOURCE)
        self.assertEqual(scheduler.calls, [])
        fresh.accepted(request_id, '777', {'synthetic_exact_request_lookup': True})
        self.assertEqual(service.submit(self.evaluation, 'crash', H2, RESOURCE)['job_id'], '777')
        self.assertEqual(scheduler.calls, [])


if __name__ == '__main__':
    unittest.main()
