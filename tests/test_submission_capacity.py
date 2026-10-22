"""Approved queue admission changes; synthetic ledgers, no models or HPC."""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from auto_lammps.ledger import Conflict, Ledger, LimitExceeded, Policy, Resources


H1, H2, H3 = ('a' * 64, 'b' * 64, 'c' * 64)
POLICY = Policy(1000, 8, 100, 1024, 10000, 1, H1)
RESOURCE = Resources(2, 10, 100, 100)


def digest(policy):
    return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class SubmissionCapacityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'ledger.sqlite'
        self.ledger = Ledger(self.path)
        self.ledger.create_campaign('campaign', POLICY)
        self.evaluations = [self.ledger.register_evaluation('campaign', task_sha256=H1,
                            repetition=i, role='agent', system_sha256=H2) for i in range(3)]

    def tearDown(self):
        self.tmp.cleanup()

    def approve(self, **kwargs):
        return self.ledger.raise_campaign_submission_capacity('campaign',
                    max_active=kwargs.pop('max_active', 3),
                    approval_sha256=kwargs.pop('approval_sha256', H3),
                    expected_previous_sha256=kwargs.pop('expected_previous_sha256', digest(asdict(POLICY))))

    def test_known_running_job_preserved_and_new_jobs_can_queue(self):
        first = self.ledger.reserve(self.evaluations[0], 'once', H2, RESOURCE)
        self.ledger.begin_dispatch(first['id'])
        self.ledger.accepted(first['id'], '101', {'synthetic': True})
        self.ledger.observe(first['id'], '101', 'running', {'synthetic': True})
        before = self.ledger.get(first['id'])
        updated = self.approve()
        self.assertEqual({k: v for k, v in updated.items() if k not in ('concurrency', 'approval_sha256')},
                         {k: v for k, v in asdict(POLICY).items() if k not in ('concurrency', 'approval_sha256')})
        self.assertEqual(Ledger(self.path).get(first['id']), before)
        for i in (1, 2):
            row = self.ledger.reserve(self.evaluations[i], 'once', H2, RESOURCE)
            self.assertTrue(self.ledger.begin_dispatch(row['id']))
            self.ledger.accepted(row['id'], str(101+i), {'synthetic': True})
        self.assertEqual(self.ledger.summary('campaign')['active_reservations'], 3)
        self.assertEqual(self.ledger.evaluation_snapshot(self.evaluations[0])['dispatch_claims'], 1)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(json.loads(db.execute('SELECT policy FROM campaigns').fetchone()[0]), asdict(POLICY))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('DELETE FROM campaign_policy_revisions')

    def test_stale_policy_and_reused_approval_rejected(self):
        with self.assertRaises(Conflict): self.approve(expected_previous_sha256=H2)
        with self.assertRaises(Conflict): self.approve(approval_sha256=H1)
        self.approve()
        with self.assertRaises(Conflict): self.approve()

    def test_reduction_and_no_change_rejected(self):
        for n in (1,):
            with self.assertRaises(Conflict): self.approve(max_active=n)
        for n in (0, -1, True):
            with self.assertRaises(ValueError): self.approve(max_active=n)

    def test_unknown_dispatch_blocks_increase(self):
        row = self.ledger.reserve(self.evaluations[0], 'once', H2, RESOURCE)
        self.ledger.begin_dispatch(row['id'])
        with self.assertRaises(Conflict): self.approve()
        self.ledger.uncertain(row['id'], {'synthetic': True})
        with self.assertRaises(Conflict): self.approve()

    def test_capacity_does_not_expand_resources_or_attempts(self):
        self.approve()
        for resource in (replace(RESOURCE, cores=9), replace(RESOURCE, wall_seconds=101),
                         replace(RESOURCE, memory_bytes=1025), replace(RESOURCE, storage_bytes=10001)):
            with self.subTest(resource=resource), self.assertRaises(LimitExceeded):
                self.ledger.reserve(self.evaluations[0], 'once', H2, resource)
        before = self.ledger.evaluation_snapshot(self.evaluations[0])
        self.assertEqual(before['max_attempts'], 2)
        for key in ('one', 'two'):
            row = self.ledger.reserve(self.evaluations[0], key, H2, RESOURCE)
            self.ledger.begin_dispatch(row['id'])
            self.ledger.rejected(row['id'], {'synthetic': True})
        with self.assertRaises(LimitExceeded):
            self.ledger.reserve(self.evaluations[0], 'three', H2, RESOURCE)

    def test_budget_still_blocks_more_reserved_wall_time(self):
        self.approve()
        self.ledger.reserve(self.evaluations[0], 'once', H2, replace(RESOURCE, cores=8, wall_seconds=100))
        with self.assertRaisesRegex(LimitExceeded, 'compute budget'):
            self.ledger.reserve(self.evaluations[1], 'once', H2, replace(RESOURCE, cores=8, wall_seconds=100))
