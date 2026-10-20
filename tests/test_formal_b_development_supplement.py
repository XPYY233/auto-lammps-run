"""Explicit additive formal-B development budgets; no model, SSH or physics."""
from pathlib import Path
import sqlite3
import tempfile
import unittest

from auto_lammps.ledger import Conflict, Ledger, LimitExceeded, Policy, Resources


H, S, T, A, APPROVAL = (letter * 64 for letter in 'abcdf')


class FormalBDevelopmentSupplementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ledger = Ledger(Path(self.tmp.name) / 'ledger.sqlite')
        self.ledger.create_campaign('synthetic', Policy(10**9,32,604800,8<<30,1<<34,3,H))
        self.author = self.register(A, 'reference')
        self.candidate = self.register(T, 'agent')
        self.job = 100
        self.source = self.finish(self.author, 'author', 115200)
        self.parent = self.finish(self.candidate, 'first', 200000, 'cancelled')
        self.original = self.pair()

    def register(self, task, role='agent', repetition=0):
        return self.ledger.register_evaluation('synthetic', task_sha256=task,
            repetition=repetition, role=role, system_sha256=S)

    def reserve(self, key, *, evaluation=None, seconds=3600, cores=32):
        return self.ledger.reserve(evaluation or self.candidate,key,H,
            Resources(cores,seconds,1024,1024))

    def finish(self, evaluation, key, actual, state='completed'):
        row = self.reserve(key,evaluation=evaluation)
        self.job += 1; job = str(self.job)
        self.ledger.begin_dispatch(row['id']); self.ledger.accepted(row['id'],job,{})
        self.ledger.observe(row['id'],job,state,{})
        self.ledger.account(row['id'],actual,H)
        return row['id']

    def pair(self, evaluation=None):
        return self.ledger.approve_reference_derived_b_limit(evaluation or self.candidate,
            self.author,[self.source],scope_sha256=H,evidence_sha256=H,approval_sha256=H)

    def approve(self, *, evaluation=None, parent=None, seconds=506016, approval=APPROVAL):
        return self.ledger.approve_formal_b_development_supplement(evaluation or self.candidate,
            parent or self.parent,additional_core_seconds=seconds,approval_sha256=approval)

    def test_authorization_retains_original_limit_state_costs_and_two_attempts(self):
        old = self.ledger.get(self.parent)
        grant = self.approve()
        snapshot = self.ledger.evaluation_snapshot(self.candidate)
        budget = snapshot['task_resource_limit']
        self.assertEqual(self.ledger.get(self.parent),old)
        self.assertEqual(self.pair(),self.original)
        self.assertEqual(snapshot['dispatch_claims'],1)
        self.assertEqual(snapshot['max_attempts'],2)
        self.assertEqual(budget['remaining_core_seconds'],506016)
        self.assertEqual(budget['limit_core_seconds'],706016)
        self.assertEqual(budget['original_formal_limit_core_seconds'],118800)
        self.assertEqual(budget['original_formal_overrun_core_seconds'],81200)
        self.assertEqual(budget['development_supplement'],grant)
        self.assertFalse(budget['gpu_allowed'])

    def test_same_grant_idempotent_even_after_second_run_and_never_a_third(self):
        grant = self.approve()
        second = self.finish(self.candidate,'second',1000,'failed')
        self.assertEqual(self.approve(),grant)
        self.assertEqual(self.ledger.formal_b_development_supplement(self.candidate,self.parent),grant)
        self.assertEqual(self.ledger.evaluation_snapshot(self.candidate)['task_resource_limit']['remaining_core_seconds'],505016)
        self.assertIsNone(self.ledger.formal_b_development_supplement(self.candidate,second))
        with self.assertRaises(LimitExceeded): self.reserve('third',seconds=1)
        with self.assertRaises(Conflict): self.approve(parent=second)
        events = [e for e in self.ledger.events(self.parent)
                  if e['kind'] == 'formal_b_development_supplement_approved']
        self.assertEqual(len(events),1)

    def test_changed_amount_or_approval_cannot_replace_grant(self):
        self.approve()
        for changes in ({'seconds':506017},{'approval':S}):
            with self.subTest(changes=changes),self.assertRaises(Conflict): self.approve(**changes)
        for bad in (0,-1,True,1.2,'506016'):
            with self.subTest(bad=bad),self.assertRaises(ValueError): self.approve(seconds=bad)

    def test_only_remaining_second_run_budget_can_be_reserved(self):
        self.approve(seconds=100)
        with self.assertRaisesRegex(LimitExceeded,'Task cumulative'):
            self.reserve('too-large',cores=1,seconds=101)
        row = self.reserve('exact',cores=1,seconds=100)
        self.assertEqual(self.ledger.evaluation_snapshot(self.candidate)['task_resource_limit']['remaining_core_seconds'],0)
        self.assertEqual(self.reserve('exact',cores=1,seconds=100)['id'],row['id'])
        self.assertTrue(self.ledger.begin_dispatch(row['id']))

    def test_global_task_and_campaign_caps_still_apply(self):
        self.approve()
        self.ledger.approve_task_resource_limit('synthetic',200010,approval_sha256=S)
        with self.assertRaisesRegex(LimitExceeded,'Task cumulative'):
            self.reserve('over-task-cap',cores=1,seconds=11)

    def test_dispatch_rechecks_later_stricter_task_budget(self):
        self.approve()
        row = self.reserve('before-policy-change',cores=1,seconds=100)
        self.ledger.approve_task_resource_limit('synthetic',200099,approval_sha256=S)
        with self.assertRaisesRegex(LimitExceeded,'dispatch blocked'):
            self.ledger.begin_dispatch(row['id'])
        self.assertFalse(self.ledger.get(row['id'])['dispatch_claimed'])

    def test_no_identity_reset_and_no_budget_for_ordinary_or_reference(self):
        self.approve()
        alternate = self.register(T,'agent',1)
        with self.assertRaises(LimitExceeded): self.reserve('alternate',evaluation=alternate,seconds=1)
        self.assertIsNone(self.ledger.formal_b_development_supplement(alternate))
        self.assertIsNone(self.ledger.formal_b_development_supplement(self.author))
        ordinary = self.register(S)
        self.assertIsNone(self.ledger.evaluation_snapshot(ordinary)['task_resource_limit'])
        with self.assertRaises(Conflict): self.approve(evaluation=ordinary)
        with self.assertRaises(Conflict): self.approve(evaluation=self.author,parent=self.source)

    def test_parent_must_be_original_cancelled_accounted_and_dispatched(self):
        for state in ('failed','completed','timeout'):
            with self.subTest(state=state):
                case = FormalBDevelopmentSupplementTests('test_authorization_retains_original_limit_state_costs_and_two_attempts')
                case.setUp()
                try:
                    # Synthetic changed scheduler evidence is deliberately quarantined.
                    row = case.ledger.get(case.parent)
                    case.ledger.observe(case.parent,row['job_id'],state,{})
                    with self.assertRaises(Conflict): case.approve()
                finally: case.doCleanups()
        with self.assertRaises(Conflict): self.approve(parent=self.source)
        with self.assertRaises(Conflict): self.approve(approval=H)

    def test_unknown_and_undispatched_parent_are_rejected(self):
        other = self.register(S)
        row = self.reserve('unknown',evaluation=other,seconds=1)
        self.ledger.begin_dispatch(row['id']); self.ledger.uncertain(row['id'],{})
        self.pair(other)
        with self.assertRaises(Conflict): self.approve(evaluation=other,parent=row['id'])
        ordinary = self.register('1'*64)
        pending = self.reserve('never-dispatched',evaluation=ordinary,seconds=1)
        self.ledger.cancel_prepared(pending['id']); self.pair(ordinary)
        with self.assertRaises(Conflict): self.approve(evaluation=ordinary,parent=pending['id'])

    def test_cancelled_but_unaccounted_parent_is_not_authorizable(self):
        other = self.register(S)
        row = self.reserve('unaccounted',evaluation=other,seconds=1)
        self.ledger.begin_dispatch(row['id']); self.ledger.accepted(row['id'],'900',{})
        self.ledger.observe(row['id'],'900','cancelled',{})
        self.pair(other)
        with self.assertRaises(Conflict): self.approve(evaluation=other,parent=row['id'])

    def test_a_second_already_submitted_attempt_cannot_receive_a_new_grant(self):
        other = self.register(S)
        parent = self.finish(other,'cancelled',1000,'cancelled')
        self.finish(other,'already-second',1000,'failed')
        self.pair(other)
        with self.assertRaises(Conflict): self.approve(evaluation=other,parent=parent)

    def test_prior_active_reservation_blocks_authorization(self):
        # A budget binding can arrive after an earlier preparation; it must not
        # make that old preparation an implicitly authorized development retry.
        other = self.register(S)
        parent = self.finish(other,'cancelled',1000,'cancelled')
        self.reserve('active',evaluation=other,seconds=1)
        self.pair(other)
        with self.assertRaises(Conflict): self.approve(evaluation=other,parent=parent)

    def test_proof_tamper_and_duplicate_event_fail_closed(self):
        grant = self.approve()
        with self.ledger._transaction() as db,self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE events SET payload='{}' WHERE kind='formal_b_development_supplement_approved'")
        # A duplicate append cannot be interpreted as two separate allowances.
        with self.ledger._transaction() as db:
            self.ledger._event(db,self.parent,'formal_b_development_supplement_approved',grant)
        with self.assertRaisesRegex(Conflict,'conflicting records'):
            self.ledger.formal_b_development_supplement(self.candidate)
        with self.assertRaises(Conflict): self.reserve('blocked-after-tamper',seconds=1)

    def test_reopened_ledger_preserves_binding_and_cost(self):
        grant = self.approve()
        reopened = Ledger(self.ledger.path)
        self.assertEqual(reopened.formal_b_development_supplement(self.candidate),grant)
        self.assertEqual(reopened.get(self.parent)['charge_core_seconds'],200000)
        self.assertEqual(reopened.evaluation_snapshot(self.candidate)['task_resource_limit']['remaining_core_seconds'],506016)

