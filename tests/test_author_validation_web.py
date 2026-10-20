"""A validation intent is previewed and pinned through ordinary UI routes."""
import unittest
from unittest.mock import Mock
from fastapi.testclient import TestClient
from auto_lammps.author_reference_workspace import AuthorReferenceWorkspace
from auto_lammps.web import create_app
from test_web import ORIGIN,HEADERS
import test_author_validation_subset as fixtures

class AuthorValidationWebTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.AuthorValidationScopeTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.fx=self.f.fx;self.task=self.f.task
        workspace=AuthorReferenceWorkspace(self.fx.tasks,self.fx.papers,self.f.service,{},
            model_provider=Mock(),connection_provider=Mock())
        self.web=TestClient(create_app(self.fx.tasks,papers=self.fx.papers,author_workspace_service=workspace),base_url=ORIGIN)
        self.addCleanup(self.web.close)
        self.url=f'/api/tasks/{self.task}/author-validation'
        self.body=dict(scope_id=self.f.scope['id'],unit_count=10,user_intent='Explicit ten-state development validation only.')

    def test_preview_and_explicit_save_keep_full_P_and_do_not_run_AI_or_HPC(self):
        original=self.f.service.status(self.task)
        status=self.web.get(self.url);self.assertEqual(status.status_code,200,status.text)
        self.assertIsNone(status.json()['latest'])
        preview=self.web.post(self.url+'/preview',json=self.body,headers=HEADERS)
        self.assertEqual(preview.status_code,200,preview.text)
        body=dict(self.body,revision=status.json()['task_revision'],validation_id='f'*32,
            expected_validation_sha256=preview.json()['validation_sha256'])
        saved=self.web.post(self.url,json=body,headers=HEADERS)
        self.assertEqual(saved.status_code,200,saved.text)
        self.assertEqual(self.web.post(self.url,json=body,headers=HEADERS).json(),saved.json())
        self.assertEqual(saved.json()['contract']['unit_count'],10)
        self.assertEqual(self.f.service.status(self.task),original)
        self.assertFalse(saved.json()['execution_authorized'])
        self.fx.transport.assert_not_called();self.fx.key.assert_not_called()

    def test_unreviewed_digest_extra_scientific_code_and_cross_origin_rejected(self):
        status=self.web.get(self.url).json()
        for changes in ({'unit_count':True},{'invocations':[]},{'source_code':'forbidden'}):
            reply=self.web.post(self.url+'/preview',json=dict(self.body,**changes),headers=HEADERS)
            self.assertEqual(reply.status_code,422,reply.text)
        reply=self.web.post(self.url,json=dict(self.body,revision=status['task_revision'],validation_id='f'*32,
            expected_validation_sha256='e'*64),headers=HEADERS)
        self.assertEqual(reply.status_code,409,reply.text)
        self.assertEqual(self.web.post(self.url+'/preview',json=self.body,headers={}).status_code,403)
        self.assertIsNone(self.web.get(self.url).json()['latest'])
