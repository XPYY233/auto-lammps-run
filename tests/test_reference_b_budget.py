"""Synthetic formal-B accounting; no scheduler, model or physics."""
import sqlite3
import tempfile
from pathlib import Path
import unittest

from auto_lammps.ledger import Ledger, Policy, Resources, Conflict, LedgerError, LimitExceeded

A, B, S, H = ('a'*64, 'b'*64, 'c'*64, 'd'*64)


class ReferenceBBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ledger = Ledger(Path(self.tmp.name)/'ledger.sqlite')
        self.ledger.create_campaign('synthetic', Policy(100000000,32,604800,8589934592,1<<34,3,H))
        self.author = self.register(A, 'reference')
        self.candidate = self.register(B, 'agent')
        self.next_job = 100
        self.source = self.finish(self.author, 'source', 32, 3600, 115200)

    def register(self, task, role, repetition=0):
        return self.ledger.register_evaluation('synthetic', task_sha256=task, repetition=repetition,
            role=role, system_sha256=S)

    def reserve(self, evaluation, key, cores=32, seconds=3600):
        return self.ledger.reserve(evaluation,key,H,Resources(cores,seconds,1024,1024))

    def finish(self, evaluation, key, cores, seconds, actual, state='completed'):
        row=self.reserve(evaluation,key,cores,seconds)
        self.next_job += 1; job = str(self.next_job)
        self.ledger.begin_dispatch(row['id']); self.ledger.accepted(row['id'],job,{})
        self.ledger.observe(row['id'],job,state,{})
        self.ledger.account(row['id'],actual,H)
        return row['id']

    def pair(self, requests=None, **kwargs):
        return self.ledger.approve_reference_derived_b_limit(self.candidate,self.author,
            requests if requests is not None else [self.source],scope_sha256=H,
            evidence_sha256=H,approval_sha256=kwargs.get('approval_sha256',H))

    def test_exact_derived_cap_counts_failure_and_keeps_two_attempts(self):
        proof=self.pair()
        self.assertEqual(proof['reference_core_seconds'],115200)
        self.assertEqual(proof['core_seconds'],118800)  # 32*1 + 1^2 core-hours
        failed=self.finish(self.candidate,'first',32,3600,115200,'failed')
        with self.assertRaisesRegex(LimitExceeded,'Task cumulative'):
            self.reserve(self.candidate,'too-large-retry',seconds=120)
        state=self.ledger.evaluation_snapshot(self.candidate)
        self.assertEqual(state['task_resource_limit']['remaining_core_seconds'],3600)
        self.assertEqual(state['dispatch_claims'],1)
        self.assertEqual(self.ledger.get(failed)['actual_core_seconds'],115200)

    def test_existing_overrun_is_retained_and_another_version_cannot_reset_it(self):
        old=self.finish(self.candidate,'old',32,7200,200000,'cancelled')
        self.pair()
        self.assertEqual(self.ledger.get(old)['actual_core_seconds'],200000)
        for evaluation in (self.candidate,self.register(B,'agent',1)):
            with self.assertRaises(LimitExceeded):self.reserve(evaluation,'new',seconds=60)
        self.assertEqual(self.ledger.evaluation_snapshot(self.candidate)['dispatch_claims'],1)

    def test_old_prepared_reservation_cannot_dispatch_after_new_binding(self):
        old=self.reserve(self.candidate,'old',seconds=604800)
        self.pair()
        with self.assertRaises(LimitExceeded):self.ledger.begin_dispatch(old['id'])
        self.assertFalse(self.ledger.get(old['id'])['dispatch_claimed'])

    def test_binding_never_adds_reference_requirement_to_ordinary_or_author_tasks(self):
        self.pair()
        ordinary=self.register(H,'agent')
        self.reserve(ordinary,'ordinary',seconds=604800)
        self.reserve(self.author,'author-second',seconds=604800)
        self.assertIsNone(self.ledger.evaluation_snapshot(ordinary)['task_resource_limit'])
        self.assertIsNone(self.ledger.evaluation_snapshot(self.author)['task_resource_limit'])

    def test_failed_unaccounted_wrong_core_or_wrong_role_A_cannot_set_b(self):
        pending=self.reserve(self.author,'pending',seconds=60)
        for requests in ([pending['id']],[],[{}],[self.source,self.source]):
            with self.subTest(requests=requests),self.assertRaises((Conflict,LedgerError)):
                self.pair(requests)
        with self.assertRaises(Conflict):
            self.ledger.approve_reference_derived_b_limit(self.author,self.author,[self.source],
                scope_sha256=H,evidence_sha256=H,approval_sha256=H)

    def test_approval_is_idempotent_immutable_and_cannot_replace_scope_or_source(self):
        proof=self.pair();self.assertEqual(self.pair(),proof)
        with self.assertRaises(Conflict):self.pair(approval_sha256=S)
        with self.ledger._transaction() as db,self.assertRaises(sqlite3.IntegrityError):
            db.execute('DELETE FROM formal_b_resource_limits')

    def test_global_and_reference_b_caps_both_apply(self):
        self.pair();self.ledger.approve_task_resource_limit('synthetic',1000,approval_sha256=H)
        with self.assertRaises(LimitExceeded):self.reserve(self.candidate,'B',seconds=60)

    def test_failed_or_non_32_core_A_cannot_define_reference_hours(self):
        for repetition,cores,state in ((1,32,'failed'),(2,16,'completed')):
            author=self.register(A,'reference',repetition)
            source=self.finish(author,'source',cores,3600,cores*3600,state)
            with self.subTest(cores=cores,state=state),self.assertRaises(Conflict):
                self.ledger.approve_reference_derived_b_limit(self.candidate,author,[source],
                    scope_sha256=H,evidence_sha256=H,approval_sha256=H)
