"""Synthetic author-reference approval tests; no real ledger, SSH or models."""
from contextlib import closing
from dataclasses import asdict, replace
import hashlib
import json
import multiprocessing as mp
from pathlib import Path
import sqlite3
import tempfile
import unittest

from auto_lammps.ledger import Conflict, Ledger, LimitExceeded, Policy, Resources


H1, H2, H3, H4 = ('a' * 64, 'b' * 64, 'c' * 64, 'd' * 64)
POLICY = Policy(1000, 8, 100, 1024, 10000, 1, H1)
RESOURCE = Resources(2, 10, 100, 100)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def reserve_parallel_worker(path, evaluation, barrier, queue):
    ledger = Ledger(Path(path))
    barrier.wait(timeout=20)
    try:
        row = ledger.reserve(evaluation, 'parallel', H2, RESOURCE)
        queue.put(('reserved', row['id']))
    except (Conflict, LimitExceeded) as exc:
        queue.put((type(exc).__name__, None))


class ReferenceParallelPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'ledger.sqlite'
        self.ledger = Ledger(self.path)
        self.ledger.create_campaign('campaign', POLICY)

    def tearDown(self):
        self.tmp.cleanup()

    def register(self, repetition, role='reference'):
        return self.ledger.register_evaluation('campaign', task_sha256=H1,
                    repetition=repetition, role=role, system_sha256=H2)

    def reserve(self, evaluation, key='request'):
        return self.ledger.reserve(evaluation, key, H2, RESOURCE)

    def approve(self, **kwargs):
        return self.ledger.approve_reference_parallelism('campaign', max_active=3,
                    approval_sha256=H3, expected_policy_sha256=digest(asdict(POLICY)), **kwargs)

    def raise_totals(self, **kwargs):
        return self.ledger.raise_campaign_resource_totals('campaign',
                    total_core_seconds=kwargs.pop('total_core_seconds', 2000),
                    total_storage_bytes=kwargs.pop('total_storage_bytes', 20000),
                    approval_sha256=kwargs.pop('approval_sha256', H4),
                    expected_previous_sha256=kwargs.pop('expected_previous_sha256', digest(asdict(POLICY))),
                    **kwargs)

    def running(self, evaluation, job='101'):
        row = self.reserve(evaluation)
        self.ledger.begin_dispatch(row['id'])
        self.ledger.accepted(row['id'], job, {'synthetic': True})
        self.ledger.observe(row['id'], job, 'running', {'synthetic': True})
        return self.ledger.get(row['id'])

    def test_default_concurrency_still_counts_all_roles_together(self):
        self.reserve(self.register(0))
        for role in ('reference', 'agent', 'analysis', 'development'):
            with self.subTest(role=role), self.assertRaisesRegex(LimitExceeded, 'concurrency'):
                self.reserve(self.register(1, role=role))

    def test_explicit_approval_preserves_running_reference_and_default_policy(self):
        ev = self.register(0)
        before = self.running(ev)
        approval = self.approve()
        self.assertEqual(approval['max_active'], 3)
        self.assertEqual(self.ledger.get(before['id']), before)
        self.assertEqual(self.ledger.register_evaluation('campaign', task_sha256=H1,
                    repetition=0, role='reference', system_sha256=H2), ev)
        self.assertEqual(self.ledger.evaluation_snapshot(ev)['dispatch_claims'], 1)
        self.assertEqual(self.ledger.evaluation_snapshot(ev)['max_attempts'], 2)
        with closing(sqlite3.connect(self.path)) as db:
            original = json.loads(db.execute('SELECT policy FROM campaigns').fetchone()[0])
            self.assertEqual(original, asdict(POLICY))
        self.assertEqual(self.ledger.approve_reference_parallelism('campaign', **{
            key: approval[key] for key in ('max_active', 'approval_sha256')},
            expected_policy_sha256=approval['policy_sha256']), approval)

    def test_three_reference_and_one_ordinary_requests_can_reserve_and_dispatch(self):
        self.approve()
        references = [self.reserve(self.register(i)) for i in range(3)]
        ordinary = self.reserve(self.register(0, 'agent'))
        for row in [*references, ordinary]:
            self.assertTrue(self.ledger.begin_dispatch(row['id']))
            self.assertFalse(self.ledger.begin_dispatch(row['id']))
        with self.assertRaisesRegex(LimitExceeded, 'concurrency'):
            self.reserve(self.register(3))
        for role in ('agent', 'analysis', 'development'):
            with self.subTest(role=role), self.assertRaisesRegex(LimitExceeded, 'concurrency'):
                self.reserve(self.register(1, role))
        self.assertEqual(self.ledger.summary('campaign')['dispatch_intents'], 4)

    def test_concurrent_transactions_do_not_overbook_either_pool(self):
        self.approve()
        evaluations = [self.register(i) for i in range(5)]
        evaluations += [self.register(i, 'agent') for i in range(2)]
        ctx = mp.get_context('spawn')
        barrier, queue = ctx.Barrier(len(evaluations)), ctx.Queue()
        processes = [ctx.Process(target=reserve_parallel_worker,
                    args=(str(self.path), evaluation, barrier, queue)) for evaluation in evaluations]
        for process in processes:
            process.start()
        replies = [queue.get(timeout=30) for _ in processes]
        for process in processes:
            process.join(timeout=30)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(sum(result[0] == 'reserved' for result in replies), 4)
        with self.ledger._transaction() as db:
            rows = self.ledger._campaign_requests(db, 'campaign')
            role_counts = {role: sum(json.loads(row['evaluation_identity'])['role'] == role for row in rows)
                           for role in ('reference', 'agent')}
        self.assertEqual(role_counts, {'reference': 3, 'agent': 1})

    def test_same_evaluation_unknown_dispatch_never_gets_another_attempt(self):
        self.approve()
        ev = self.register(0)
        row = self.reserve(ev)
        self.ledger.begin_dispatch(row['id'])
        self.ledger.uncertain(row['id'], {'synthetic': True})
        self.assertEqual(self.reserve(ev)['id'], row['id'])
        with self.assertRaisesRegex(Conflict, 'finish or be reconciled'):
            self.reserve(ev, 'different')
        self.assertEqual(self.ledger.evaluation_snapshot(ev)['dispatch_claims'], 1)
        self.assertFalse(self.ledger.begin_dispatch(row['id']))

    def test_reference_pool_does_not_extend_candidate_attempts(self):
        self.approve()
        ev = self.register(0, 'agent')
        for key in ('first', 'second'):
            row = self.reserve(ev, key)
            self.ledger.begin_dispatch(row['id'])
            self.ledger.rejected(row['id'], {'synthetic': True})
        with self.assertRaisesRegex(LimitExceeded, 'allowance'):
            self.reserve(ev, 'third')
        self.assertEqual(self.ledger.evaluation_snapshot(ev)['max_attempts'], 2)

    def test_parallel_approval_requires_new_evidence_correct_policy_and_fixed_limit(self):
        for bad in (dict(max_active=2, approval_sha256=H3,
                         expected_policy_sha256=digest(asdict(POLICY))),
                    dict(max_active=True, approval_sha256=H3,
                         expected_policy_sha256=digest(asdict(POLICY)))):
            with self.assertRaises(ValueError):
                self.ledger.approve_reference_parallelism('campaign', **bad)
        for approval, previous in ((H1, digest(asdict(POLICY))), (H3, H2)):
            with self.assertRaises(Conflict):
                self.ledger.approve_reference_parallelism('campaign', max_active=3,
                    approval_sha256=approval, expected_policy_sha256=previous)
        self.approve()
        with self.assertRaises(Conflict):
            self.ledger.approve_reference_parallelism('campaign', max_active=3,
                approval_sha256=H4, expected_policy_sha256=digest(asdict(POLICY)))

    def test_parallel_approval_is_immutable(self):
        self.approve()
        with closing(sqlite3.connect(self.path)) as db:
            for sql in ('DELETE FROM reference_parallelism',
                        "UPDATE reference_parallelism SET approval_sha256='changed'"):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(sql)

    def test_approval_does_not_silently_lower_limit_below_active_references(self):
        larger = replace(POLICY, concurrency=5)
        self.ledger.create_campaign('larger', larger)
        for repetition in range(4):
            ev = self.ledger.register_evaluation('larger', task_sha256=H1,
                    repetition=repetition, role='reference', system_sha256=H2)
            self.reserve(ev)
        with self.assertRaisesRegex(Conflict, 'exceed'):
            self.ledger.approve_reference_parallelism('larger', max_active=3,
                approval_sha256=H3, expected_policy_sha256=digest(asdict(larger)))
        self.assertEqual(self.ledger.summary('larger')['active_reservations'], 4)

    def test_authorization_does_not_affect_other_campaigns(self):
        self.approve()
        self.ledger.create_campaign('other', POLICY)
        for repetition in range(2):
            ev = self.ledger.register_evaluation('other', task_sha256=H1,
                repetition=repetition, role='reference', system_sha256=H2)
            if repetition == 0:
                self.reserve(ev)
            else:
                with self.assertRaisesRegex(LimitExceeded, 'concurrency'):
                    self.reserve(ev)

    def test_resource_revision_keeps_reference_approval_and_ordinary_one_limit(self):
        self.approve()
        updated = self.raise_totals()
        for repetition in range(3):
            self.reserve(self.register(repetition))
        self.reserve(self.register(0, 'agent'))
        with self.assertRaisesRegex(LimitExceeded, 'concurrency'):
            self.reserve(self.register(1, 'agent'))
        self.assertEqual(updated['concurrency'], 1)

    def test_active_total_increase_preserves_charges_identity_and_job_limits(self):
        failed_ev = self.register(0)
        failed = self.running(failed_ev)
        self.ledger.observe(failed['id'], '101', 'failed', {'synthetic': True})
        self.ledger.account(failed['id'], 11, H2)
        active_ev = self.register(1)
        active = self.running(active_ev, '102')
        before = [self.ledger.get(row['id']) for row in (failed, active)]
        with self.assertRaisesRegex(Conflict, 'active or unaccounted'):
            self.ledger.amend_campaign_policy('campaign', replace(POLICY,
                total_core_seconds=2000, approval_sha256=H4),
                expected_previous_sha256=digest(asdict(POLICY)))
        result = self.raise_totals()
        self.assertEqual(result, dict(asdict(POLICY), total_core_seconds=2000,
                                     total_storage_bytes=20000, approval_sha256=H4))
        self.assertEqual([self.ledger.get(row['id']) for row in (failed, active)], before)
        summary = Ledger(self.path).summary('campaign')
        self.assertEqual(summary['accounted_core_seconds'], 11)
        self.assertEqual(summary['charged_or_reserved_core_seconds'], 31)
        self.assertEqual(summary['dispatch_intents'], 2)
        self.assertEqual(summary['reserved_storage_bytes'], 200)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(json.loads(db.execute('SELECT policy FROM campaigns').fetchone()[0]), asdict(POLICY))
            revision = db.execute('SELECT * FROM campaign_policy_revisions').fetchone()
            self.assertEqual(revision[3], digest(asdict(POLICY)))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('DELETE FROM campaign_policy_revisions')

    def test_total_increase_allows_known_terminal_unaccounted_job(self):
        ev = self.register(0)
        row = self.running(ev)
        self.ledger.observe(row['id'], '101', 'completed', {'synthetic': True})
        before = self.ledger.get(row['id'])
        self.raise_totals()
        self.assertEqual(self.ledger.get(row['id']), before)
        self.assertEqual(before['accounted'], 0)

    def test_total_increase_cannot_lower_totals_reuse_evidence_or_change_limits(self):
        for changes in ({'total_core_seconds': 999}, {'total_storage_bytes': 9999},
                        {'total_core_seconds': 1000, 'total_storage_bytes': 10000},
                        {'approval_sha256': H1}, {'expected_previous_sha256': H2}):
            with self.subTest(changes=changes), self.assertRaises(Conflict):
                self.raise_totals(**changes)
        with self.assertRaises(TypeError):
            self.raise_totals(max_cores=32)
        with self.assertRaises(ValueError):
            self.raise_totals(total_core_seconds=True)
        self.raise_totals()
        with self.assertRaisesRegex(Conflict, 'changed'):
            self.raise_totals()

    def test_unknown_or_scheduler_conflict_blocks_new_authorizations(self):
        ev = self.register(0)
        row = self.reserve(ev)
        self.ledger.begin_dispatch(row['id'])
        for state in ('dispatching', 'unknown', 'reconcile_required'):
            if state == 'unknown':
                self.ledger.uncertain(row['id'], {'synthetic': True})
            if state == 'reconcile_required':
                self.ledger.accepted(row['id'], '101', {'synthetic': True})
                self.ledger.observe(row['id'], '101', 'completed', {'synthetic': True})
                self.ledger.observe(row['id'], '101', 'running', {'synthetic': True})
            with self.subTest(state=state):
                with self.assertRaisesRegex(Conflict, 'Reconcile'):
                    self.approve()
                with self.assertRaisesRegex(Conflict, 'Reconcile'):
                    self.raise_totals()
        with self.assertRaisesRegex(Conflict, 'scheduler conflict'):
            self.reserve(self.register(1))

    def test_per_task_cap_is_not_reset_by_parallelism_or_total_increase(self):
        self.ledger.approve_task_resource_limit('campaign', 30, approval_sha256=H2)
        self.approve()
        self.raise_totals()
        self.reserve(self.register(0))
        for role in ('reference', 'agent'):
            with self.subTest(role=role), self.assertRaisesRegex(LimitExceeded, 'Task cumulative'):
                self.reserve(self.register(1, role))
        ev = self.register(0)
        self.assertEqual(self.ledger.evaluation_snapshot(ev)['task_resource_limit']['limit_core_seconds'], 30)

    def test_raise_does_not_hide_recorded_compute_overrun(self):
        ev = self.register(0)
        row = self.running(ev)
        self.ledger.observe(row['id'], '101', 'failed', {'synthetic': True})
        self.ledger.account(row['id'], 2500, H2)
        with self.assertRaisesRegex(LimitExceeded, 'charged'):
            self.raise_totals()
        self.raise_totals(total_core_seconds=3000)
        self.assertEqual(self.ledger.get(row['id'])['actual_core_seconds'], 2500)


if __name__ == '__main__':
    unittest.main()
