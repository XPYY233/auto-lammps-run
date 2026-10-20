"""Ordinary HTTP recovery stays explicit; synthetic providers only."""
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from auto_lammps.web import create_app
import test_supplier_checked_candidate_review as fixtures
from test_web import ORIGIN, HEADERS

class WebSupplierCheckedReviewTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.SupplierCheckedCandidateReviewTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.web=TestClient(create_app(self.f.tasks,model_client=self.f.client,candidate_service=self.f.service),base_url=ORIGIN)
        self.addCleanup(self.web.close)
        self.url=f"/api/tasks/{self.f.doc['id']}/plan/recheck-after-supplier-check"
        self.body=dict(revision=self.f.doc['revision'],request_id='a'*32,original_request_id=self.f.original,
            supplier_checked=True,note='Supplier record checked; retain unknown costs.')

    def test_explicit_same_task_http_only_enqueues_and_keeps_original_unknown(self):
        old=self.f.client.calls.lookup(self.f.original)
        with patch.object(self.f.service.pool,'submit') as queue:
            reply=self.web.post(self.url,json=self.body,headers=HEADERS)
        self.assertEqual(reply.status_code,202,reply.text)
        queue.assert_called_once()
        self.f.transport.assert_not_called()
        self.assertEqual(self.f.client.calls.lookup(self.f.original),old)
        self.assertEqual(reply.json()['proposal_rounds']['used'],5)

    def test_missing_check_wrong_origin_and_paused_do_not_enqueue(self):
        with patch.object(self.f.service.pool,'submit') as queue:
            for body,headers in [(dict(self.body,supplier_checked=False),HEADERS),
                                 (dict(self.body,supplier_checked='yes'),HEADERS),
                                 (self.body,dict(HEADERS,Origin='http://invalid.example'))]:
                with self.subTest(body=body,headers=headers):
                    self.assertGreaterEqual(self.web.post(self.url,json=body,headers=headers).status_code,400)
            self.f.tasks.set_paused(self.f.doc['id'],self.f.doc['revision'],True)
            reply=self.web.post(self.url,json=self.body,headers=HEADERS)
            self.assertGreaterEqual(reply.status_code,400,reply.text)
            queue.assert_not_called()
