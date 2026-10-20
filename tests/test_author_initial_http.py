"""Ordinary initial resource routes over real controllers, synthetic transport."""
import unittest
from fastapi.testclient import TestClient

from auto_lammps.web import create_app
import test_author_initial_workspace as fixture
from test_web import ORIGIN, HEADERS


class InitialAuthorHTTPTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.InitialAuthorWorkspaceTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.http=TestClient(create_app(self.f.tasks,papers=self.f.papers,
            author_workspace_service=self.f.workspace,hpc_connections=self.f.fx.hpc,
            discovery_library=self.f.fx.f.library),base_url=ORIGIN)
        self.addCleanup(self.http.close)
        self.initial=self.f.workspace.initial
        self.initial.bootstrap.capture=self.f.fx.capture
        self.url=f'/api/tasks/{self.f.task}/author-resources'

    def test_browser_inspects_and_confirms_displayed_receipt_without_binding_or_execution(self):
        view=self.http.get(self.url)
        self.assertEqual(view.status_code,200,view.text)
        self.assertTrue(view.json()['initial']['enabled'])
        self.f.fx.capture.assert_not_called();self.f.model.assert_not_called()
        result=self.http.post(self.url+'/bootstrap',json={'request_id':self.f.fx.request},headers=HEADERS)
        self.assertEqual(result.status_code,200,result.text)
        value=result.json();self.assertEqual(value['latest']['state'],'metadata_ready')
        self.assertFalse(value['reading_approved']);self.assertFalse(value['binding_ready'])
        approved=self.http.post(self.url+'/bootstrap/read-approval',json={
            'request_id':self.f.fx.request,'receipt_sha256':value['receipt_sha256'],
            'note':'Synthetic project reading acknowledgement'},headers=HEADERS)
        self.assertEqual(approved.status_code,200,approved.text)
        self.assertTrue(approved.json()['reading_approved'])
        self.assertFalse(approved.json()['execution_authorized'])
        self.assertFalse(approved.json()['use_approved'])
        self.assertFalse(approved.json()['binding_ready'])
        self.assertEqual(self.f.workspace.bindings,{})
        self.f.model.assert_not_called()

    def test_browser_cannot_supply_paths_source_execution_or_license_claim(self):
        for extra in ({'repository':'attacker/other'},{'path':'input.lmp'},
                      {'execution_authorized':True},{'use_approved':True}):
            result=self.http.post(self.url+'/bootstrap',json={'request_id':self.f.fx.request,**extra},headers=HEADERS)
            self.assertEqual(result.status_code,422,result.text)
        self.f.fx.capture.assert_not_called()

    def test_browser_unknown_reconciliation_keeps_original_identity_and_does_not_repeat_http(self):
        self.f.fx.after_worker_unknown=True
        first=self.http.post(self.url+'/bootstrap',json={'request_id':self.f.fx.request},headers=HEADERS)
        self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(first.json()['latest']['state'],'unknown')
        count=len(self.f.fx.repository.calls);self.f.fx.after_worker_unknown=False
        result=self.http.post(self.url+'/bootstrap/reconcile',json={'request_id':self.f.fx.request},headers=HEADERS)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['latest']['state'],'metadata_ready')
        self.assertEqual(len(self.f.fx.repository.calls),count)
        self.assertEqual(len(result.json()['history']),1)

    def test_origin_protection_rejects_mutation_before_any_transport(self):
        response=self.http.post(self.url+'/bootstrap',json={'request_id':self.f.fx.request},
            headers={**HEADERS,'Origin':'https://untrusted.example'})
        self.assertEqual(response.status_code,403,response.text)
        self.f.fx.capture.assert_not_called();self.f.model.assert_not_called()
