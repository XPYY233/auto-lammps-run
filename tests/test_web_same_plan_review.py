"""Ordinary explicit review action with synthetic model receipts only."""
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from auto_lammps.web import create_app
import test_same_plan_review as fixture
from test_web import ORIGIN, HEADERS


class WebSamePlanReviewTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.SamePlanReviewTests()
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.web=TestClient(create_app(self.f.tasks,model_client=self.f.client,candidate_service=self.f.service),base_url=ORIGIN)
        self.addCleanup(self.web.close)
        self.base='/api/tasks/'+self.f.doc['id']+'/plan'
        self.body={'revision':self.f.doc['revision'],'request_id':'d'*32}

    def test_read_is_free_and_explicit_recheck_preserves_six_rounds_and_approval_gate(self):
        view=self.web.get(self.base).json()
        self.assertTrue(view['same_plan_review']['can_recheck'])
        self.assertEqual(self.f.transport.call_count,0)
        with patch.object(self.f.service.pool,'submit'):
            for _ in range(2):
                reply=self.web.post(self.base+'/recheck',json=self.body,headers=HEADERS)
                self.assertEqual(reply.status_code,202,reply.text)
        self.assertEqual(self.f.transport.call_count,0)
        self.f.service.run(self.f.doc['id'])
        view=self.web.get(self.base).json()
        self.assertEqual(view['state'],'prepared')
        self.assertEqual(view['proposal_rounds']['used'],6)
        self.assertEqual(view['review_rounds']['used'],1)
        self.assertFalse(view['approved'])
        self.assertFalse(view['execution_authorized'])
        self.assertEqual(self.f.transport.call_count,1)
        self.assertEqual(view['workspace']['current']['version'],6)

    def test_revision_origin_and_request_identity_rejected_without_model(self):
        for body,headers in (({**self.body,'request_id':'INVALID'},HEADERS),
                             ({**self.body,'revision':self.body['revision']+1},HEADERS),
                             (self.body,{})):
            with patch.object(self.f.service.pool,'submit'):
                reply=self.web.post(self.base+'/recheck',json=body,headers=headers)
            self.assertIn(reply.status_code,(403,422),reply.text)
        self.assertEqual(self.f.transport.call_count,0)
        self.assertEqual(self.f.service.history.get(self.f.doc['id'])['state'],'failed')

    def test_paused_task_blocks_recheck(self):
        self.f.tasks.set_paused(self.f.doc['id'],self.f.doc['revision'],True)
        reply=self.web.post(self.base+'/recheck',json=self.body,headers=HEADERS)
        self.assertEqual(reply.status_code,422,reply.text)
        self.assertEqual(self.f.transport.call_count,0)
