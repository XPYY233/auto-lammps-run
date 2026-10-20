"""User-only additive budget intent preserves the existing accounted B request."""
from copy import deepcopy
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient

from auto_lammps.web import create_app
import test_execution_jobs as fixture
import test_execution as execution
from test_results import ORIGIN


class FormalBSupplementHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixture.ExecutionJobTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.fx.enqueue();self.f=self.fx.f;self.ledger=self.f.ledger;parent=self.f.request_id
        self.ledger.begin_dispatch(parent);self.ledger.accepted(parent,'123',{})
        self.ledger.observe(parent,'123','cancelled',{});self.ledger.account(parent,1,'a'*64)
        job_id=self.fx.jobs.get(self.fx.task)['id']
        with self.f.tasks.transaction() as db:self.fx.jobs._event(db,job_id,'attention','synthetic_accounted_cancellation')
        with patch.object(self.ledger,'approve_formal_b_development_supplement',return_value=None):
            execution.grant_cancelled_development(self.f)
        self.http=TestClient(create_app(self.f.tasks,execution_jobs=self.fx.jobs),base_url=ORIGIN)
        self.addCleanup(self.http.close);self.url=self.fx.url+'/development-supplement'
        self.payload=dict(revision=self.fx.revision,parent_request_id=parent,
            additional_core_seconds=506016,note='Synthetic explicit second-run development approval')

    def test_ordinary_budget_intent_is_append_only_idempotent_and_does_not_submit(self):
        before=self.ledger.get(self.f.request_id);view=self.http.get(self.fx.url).json()
        self.assertTrue(view['formal_b_retest']['can_add_supplement'])
        first=self.http.post(self.url,json=self.payload,headers=fixture.HEADERS)
        self.assertEqual(first.status_code,200,first.text)
        self.assertFalse(first.json()['calls_model']);self.assertFalse(first.json()['submits_HPC'])
        self.assertEqual(self.http.post(self.url,json=self.payload,headers=fixture.HEADERS).json(),first.json())
        self.assertEqual(self.ledger.get(self.f.request_id),before)
        self.assertEqual(self.ledger.evaluation_snapshot(self.f.evaluation)['dispatch_claims'],1)
        after=self.http.get(self.fx.url).json();self.assertFalse(after['formal_b_retest']['can_add_supplement'])
        self.assertEqual(after['formal_b_retest']['grant']['additional_core_seconds'],506016)
        evidence=next((self.f.tasks.path.parent/'execution-development-approvals').glob('*.json'))
        self.assertIn(self.payload['note'],evidence.read_text())
        self.assertFalse(self.fx.jobs.thread and self.fx.jobs.thread.is_alive())

    def test_stale_foreign_or_expanded_repeat_cannot_change_the_grant(self):
        for changed in ({'revision':self.fx.revision+1},{'parent_request_id':'f'*32},
                        {'execution_authorized':True},{'additional_core_seconds':0}):
            value={**self.payload,**changed};response=self.http.post(self.url,json=value,headers=fixture.HEADERS)
            self.assertGreaterEqual(response.status_code,400,response.text)
        self.assertIsNone(self.ledger.formal_b_development_supplement(self.f.evaluation))
        result=self.http.post(self.url,json=self.payload,headers=fixture.HEADERS);self.assertEqual(result.status_code,200,result.text)
        grant=deepcopy(result.json()['grant'])
        response=self.http.post(self.url,json={**self.payload,'additional_core_seconds':506017},headers=fixture.HEADERS)
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(self.ledger.formal_b_development_supplement(self.f.evaluation),grant)
        self.assertEqual(self.ledger.evaluation_snapshot(self.f.evaluation)['dispatch_claims'],1)
        self.assertFalse(self.fx.jobs.thread and self.fx.jobs.thread.is_alive())
