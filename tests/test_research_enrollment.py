"""Automatic research identity before generation; no model API or physics."""
from contextlib import ExitStack
import json
import threading
import time
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient

from auto_lammps.candidate_jobs import CandidateService
from auto_lammps.deepseek import DeepSeekClient,ModelCalls
from auto_lammps.execution_jobs import ExecutionJobs
from auto_lammps.ledger import Policy
from auto_lammps.manifest import canonical,sha256
from auto_lammps.research_enrollment import ResearchEnrollment
from auto_lammps.tasks import TaskError
from auto_lammps.agent_candidates import CandidateError
from auto_lammps.web import create_app
from test_candidate_jobs import frozen_research
from test_execution_jobs import HEADERS,ORIGIN
import test_execution as execution


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.f=execution.ExecutionTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        with self.f.ledger._transaction() as db:policy=self.f.ledger._policy(db,'synthetic')
        self.scope=dict(campaign='synthetic',system_sha256='a'*64,policy_sha256=sha256(canonical(policy)))
        self.enrollment=self.open();self.doc=frozen_research(self.f.tasks)

    def open(self,**changes):return ResearchEnrollment(self.f.tasks,self.f.ledger,**(self.scope|changes))
    def register(self):return self.enrollment.register(self.doc['id'],self.doc['revision'])

    def test_registration_and_restart_keep_identity_and_two_attempts(self):
        one=self.register();two=self.open().register(self.doc['id'],self.doc['revision'])
        self.assertEqual(one,two)
        summary=self.f.ledger.evaluation_snapshot(one)
        self.assertEqual(summary['max_attempts'],2);self.assertEqual(summary['identity']['repetition'],0)
        self.assertEqual(summary['identity']['role'],'agent');self.assertEqual(summary['dispatch_claims'],0)
        with self.f.tasks.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM research_execution_bindings').fetchone()[0],1)
        self.assertEqual(self.f.f.transport.call_count,1)  # Existing fixture only.

    def test_concurrent_registration_has_one_binding_and_evaluation(self):
        results=[];errors=[]
        def run():
            try:results.append(self.open().register(self.doc['id'],self.doc['revision']))
            except Exception as exc:errors.append(exc)
        workers=[threading.Thread(target=run) for _ in range(3)]
        for w in workers:w.start()
        for w in workers:w.join(5)
        self.assertFalse(errors);self.assertEqual(len(results),3);self.assertEqual(len(set(results)),1)

    def test_draft_reference_and_late_enrollment_are_rejected(self):
        draft=self.f.tasks.create('Synthetic','request','research')
        reference=self.f.tasks.create('Synthetic reference','request','reproduction')
        for doc in (draft,reference):
            with self.assertRaises(CandidateError):self.enrollment.register(doc['id'],doc['revision'])
        with self.assertRaises(TaskError):self.enrollment.register(self.f.doc['id'],self.f.doc['revision'])
        self.assertIsNone(self.enrollment.get(self.f.doc['id']))

    def test_changed_policy_or_system_does_not_reset_identity(self):
        one=self.register()
        with self.assertRaises(TaskError):self.open(system_sha256='c'*64).register(self.doc['id'],self.doc['revision'])
        with self.assertRaises(TaskError):self.open(policy_sha256='c'*64)
        self.assertEqual(self.enrollment.get(self.doc['id'])['evaluation'],one)

    def test_policy_amendment_requires_reconciliation_before_reusing_binding(self):
        self.register();self.f.ledger.cancel_intent(self.f.request_id)
        with self.f.ledger._transaction() as db:policy=self.f.ledger._policy(db,'synthetic')
        changed=Policy(**{**policy,'approval_sha256':'f'*64})
        self.f.ledger.amend_campaign_policy('synthetic',changed,expected_previous_sha256=self.scope['policy_sha256'])
        with self.assertRaises(TaskError):self.enrollment.get(self.doc['id'])
        with self.assertRaises(TaskError):self.register()

    def test_read_only_status_does_not_register_or_reserve(self):
        jobs=ExecutionJobs(self.f.controller,{},enrollment=self.enrollment);self.addCleanup(jobs.close)
        before=self.f.ledger.events(self.f.request_id)
        status=jobs.status(self.doc['id']);self.assertTrue(status['configured']);self.assertFalse(status['can_start'])
        self.assertIsNone(self.enrollment.get(self.doc['id']));self.assertEqual(self.f.ledger.events(self.f.request_id),before)

    def test_http_generation_enrolls_before_model_and_execution_needs_no_manual_binding(self):
        self.f.ledger.cancel_intent(self.f.request_id)
        self.f.doc=self.doc
        def model(*args):
            registered=self.enrollment.get(self.doc['id']);self.assertIsNotNone(registered)
            self.assertEqual(self.f.ledger.evaluation_snapshot(registered['evaluation'])['max_attempts'],2)
            return self.f.f.transport(*args)
        client=DeepSeekClient(ModelCalls(self.f.root/'enrolled-models.sqlite',self.f.f.calls.config,max_requests=1),
                              transport=model,key_reader=lambda:'synthetic-key')
        candidates=CandidateService(self.f.tasks,client,self.f.f.adapter,resources=self.f.resources,snapshots=self.f.root/'snapshots')
        self.addCleanup(lambda:candidates.close(wait=True))
        jobs=ExecutionJobs(self.f.controller,{},enrollment=self.enrollment);self.addCleanup(jobs.close)
        url='/api/tasks/'+self.doc['id']
        with ExitStack() as stack,TestClient(create_app(self.f.tasks,model_client=client,candidate_service=candidates,execution_jobs=jobs),base_url=ORIGIN) as web:
            before=web.get(url+'/execution').json();self.assertFalse(before['can_start'])
            # 方案准备好之后仍不允许直接开工：必须先由用户批准。
            bad=web.post(url+'/candidate',headers=HEADERS,json={'revision':self.doc['revision'],'repetition':1})
            self.assertEqual(bad.status_code,422);self.assertIsNone(self.enrollment.get(self.doc['id']))
            reply=web.post(url+'/candidate',headers=HEADERS,json={'revision':self.doc['revision']})
            self.assertEqual(reply.status_code,202,reply.text)
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                candidate=web.get(url+'/candidate').json()['candidate']
                if candidate['state']=='prepared':break
                time.sleep(.01)
            self.assertEqual(candidate['state'],'prepared',candidate)
            self.assertTrue(web.get(url+'/execution').json()['can_start'])
            # 第一道人工关卡：未批准不能开工；批准后按原流程继续。
            self.assertEqual(web.post(url+'/execution',headers=HEADERS,
                                      json={'revision':self.doc['revision']}).status_code,409)
            self.assertEqual(web.post(url+'/plan/approve',headers=HEADERS,
                                      json={'revision':self.doc['revision']}).status_code,200)
            self.assertEqual(jobs.bindings,{})
            self.f.evaluation=jobs.evaluation_for(self.doc['id'])
            self.f.plan=self.f.controller.prepare(self.doc['id'],self.f.evaluation);self.f.request_id=self.f.plan['row']['id']
            self.f.payload.update(request_id=self.f.request_id,manifest_sha256=self.f.plan['snapshot'].digest,
                task_sha256=self.f.plan['snapshot'].verify()['provenance']['task_sha256'],batch_sha256=self.f.plan['batch'].sha256)
            # Existing signed-grant fixture, not automatic approval or a real run.
            self.f.authorize_fixture()
            stack.enter_context(self.f.transports())
            self.assertEqual(web.post(url+'/execution',headers=HEADERS,json={'revision':self.doc['revision']}).status_code,202)
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                status=web.get(url+'/execution').json()
                if status.get('job',{}).get('state')=='analyzed':break
                time.sleep(.01)
            self.assertEqual(status['job']['state'],'analyzed',status)
            self.assertEqual(status['job']['dispatch_count'],1)
            self.assertEqual(len(web.get(url+'/raw-files').json()['files']),4)
            restarted=ExecutionJobs(self.f.controller,{},enrollment=self.open())
            self.assertEqual(restarted.evaluation_for(self.doc['id']),self.f.evaluation)
            self.assertEqual(restarted.advance(self.doc['id'])['job']['dispatch_count'],1)
            self.assertEqual(self.f.dispatch.call_count,1)
