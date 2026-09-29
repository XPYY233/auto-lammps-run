"""One explicit start across real services, synthetic model/scheduler/physics only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import sys
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from auto_lammps import outputs
from auto_lammps.candidate_jobs import CandidateService
from auto_lammps.deepseek import DeepSeekClient, ModelCalls
from auto_lammps.execution_jobs import ExecutionJobs
from auto_lammps.manifest import canonical, sha256
from auto_lammps.research_enrollment import ResearchEnrollment
from auto_lammps.research_workflow import ResearchWorkflow
from auto_lammps.tasks import TaskError
from auto_lammps.web import create_app
from test_candidate_jobs import frozen_research
from test_execution_jobs import HEADERS, ORIGIN
import test_authorization as authorization


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.fixture=authorization.AuthorizationTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups);self.f=self.fixture.f
        self.f.ledger.cancel_intent(self.f.request_id)
        self.doc=frozen_research(self.f.tasks)
        with self.f.ledger._transaction() as db:policy=self.f.ledger._policy(db,'synthetic')
        self.enrollment=ResearchEnrollment(self.f.tasks,self.f.ledger,campaign='synthetic',
            system_sha256='a'*64,policy_sha256=sha256(canonical(policy)))
        self.client=DeepSeekClient(ModelCalls(self.f.root/'workflow-models.sqlite',self.f.f.calls.config,max_requests=1),
            transport=self.f.f.transport,key_reader=lambda:'synthetic-key')
        self.candidates=CandidateService(self.f.tasks,self.client,self.f.f.adapter,resources=self.f.resources,snapshots=self.f.root/'snapshots')
        self.addCleanup(lambda:self.candidates.close(wait=True))
        self.jobs=ExecutionJobs(self.f.controller,{},enrollment=self.enrollment);self.addCleanup(self.jobs.close)
        self.flow=ResearchWorkflow(self.candidates,self.jobs);self.addCleanup(self.flow.close)
        self.url='/api/tasks/'+self.doc['id']

    def enqueue(self):return self.flow.enqueue(self.doc['id'],self.doc['revision'])
    def advance(self):return self.flow.advance(self.doc['id'])['workflow']
    def wait_for(self,read,predicate):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            value=read()
            if predicate(value):return value
            time.sleep(.02)
        self.fail('Timed out waiting for synthetic workflow: '+str(value))

    def transports(self):
        from auto_lammps.outputs import transfer
        stack=ExitStack();stack.enter_context(self.f.transports())
        original_install=self.fixture.install
        def install(argv,**kw):
            # Read the identity already registered by the product, no per-task
            # grant, binding or batch is written by this fixture.
            self.f.doc=self.doc;self.f.evaluation=self.jobs.evaluation_for(self.doc['id'])
            self.f.plan=self.f.controller.prepare(self.doc['id'],self.f.evaluation)
            self.f.request_id=self.f.plan['row']['id']
            return original_install(argv,**kw)
        stack.enter_context(patch('auto_lammps.authorization._capture',side_effect=install))
        # Keep the real file-only helper subprocess; replace SSH transport only.
        def download(argv,receiver,**kw):
            command=[sys.executable,'-I',str(self.f.helper),'--collect','--root',self.f.remote.profile['requests_root'],
                '--request-id',self.f.request_id,'--manifest-sha256',self.f.plan['snapshot'].digest,'--job-id','123']
            return transfer(command,receiver,**kw)
        stack.enter_context(patch.object(outputs,'transfer',side_effect=download))
        return stack

    def approve_plan(self):
        job=self.candidates.history.get(self.doc['id'])
        self.f.tasks.approve_plan(self.doc['id'],self.doc['revision'],
                                  scope='plan:'+job['result']['snapshot_sha256'])

    def test_single_http_start_reaches_analysis_and_download_without_second_click(self):
        app=create_app(self.f.tasks,model_client=self.client,candidate_service=self.candidates,execution_jobs=self.jobs)
        with self.transports(),TestClient(app,base_url=ORIGIN) as web:
            self.assertTrue(web.get('/api/schema').json()['automatic_workflow']['configured'])
            self.assertIsNone(web.get(self.url+'/execution').json()['automatic_workflow']['workflow'])
            self.assertIsNone(self.enrollment.get(self.doc['id']))
            # 第一道人工关卡：未批准方案时不允许提交（这里先准备方案，再断言被拒）。
            reply=web.post(self.url+'/workflow',headers=HEADERS,json={'revision':self.doc['revision']})
            self.assertEqual(reply.status_code,202,reply.text)
            self.wait_for(lambda:self.candidates.history.get(self.doc['id']),lambda x:bool(x) and x.get('state')=='prepared')
            refused=web.post(self.url+'/execution',headers=HEADERS,json={'revision':self.doc['revision']})
            self.assertEqual(refused.status_code,409,refused.text)
            self.approve_plan()
            reply=web.post(self.url+'/workflow',headers=HEADERS,json={'revision':self.doc['revision']})
            self.assertEqual(reply.status_code,202,reply.text)
            for _ in range(3):
                self.assertEqual(web.post(self.url+'/workflow',headers=HEADERS,json={'revision':self.doc['revision']}).status_code,202)
            result=self.wait_for(lambda:web.get(self.url+'/execution').json(),lambda x:(x.get('job') or {}).get('state')=='analyzed')
            self.assertEqual(result['job']['dispatch_count'],1)
            self.assertEqual(result['job']['max_attempts'],2)
            self.assertEqual(result['job']['scientific_status'],'not_evaluated')
            files=web.get(self.url+'/raw-files').json()['files'];self.assertEqual(len(files),4)
            self.assertEqual(self.fixture.install_calls,1);self.assertEqual(self.f.dispatch.call_count,1)
            self.assertEqual(self.client.calls.status()['remaining_requests'],0)
            self.assertEqual(self.jobs.bindings,{})
        restarted=ResearchWorkflow(self.candidates,self.jobs)
        self.assertEqual(restarted.advance(self.doc['id'])['workflow']['state'],'handed_off')
        self.assertEqual(self.f.dispatch.call_count,1)

    def test_queued_start_survives_service_restart_without_browser(self):
        self.enqueue()
        restarted=ResearchWorkflow(self.candidates,self.jobs);self.addCleanup(restarted.close)
        self.assertIsNone(self.candidates.history.get(self.doc['id']))
        with self.transports():
            self.candidates.start();self.jobs.start();restarted.start()
            # 方案准备完成后由用户批准（新的产品契约），随后才允许派发。
            self.wait_for(lambda:self.candidates.history.get(self.doc['id']),lambda x:bool(x) and x.get('state')=='prepared')
            self.approve_plan()
            result=self.wait_for(lambda:self.jobs.status(self.doc['id']),lambda x:(x.get('job') or {}).get('state')=='analyzed')
        self.assertEqual(result['job']['dispatch_count'],1)
        self.assertEqual(self.fixture.install_calls,1)

    def test_read_only_restart_never_runs_unstarted_prepared_task(self):
        self.enrollment.register(self.doc['id'],self.doc['revision'])
        self.candidates.enqueue(self.doc['id'],self.doc['revision'])
        self.wait_for(lambda:self.candidates.history.get(self.doc['id']),lambda x:bool(x) and x.get('state')=='prepared')
        with self.transports():
            self.flow.start();time.sleep(.05);self.flow.close()
            self.assertIsNone(self.flow.status(self.doc['id'])['workflow'])
            self.assertIsNone(self.jobs.get(self.doc['id']))
            self.f.dispatch.assert_not_called()

    def test_concurrent_starts_have_one_immutable_intent(self):
        with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(lambda _:self.enqueue(),range(8)))
        with self.f.tasks.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM research_workflows').fetchone()[0],1)
        self.assertIsNone(self.enrollment.get(self.doc['id']))

    def test_service_change_stops_without_generation_or_enrollment(self):
        self.enqueue();self.flow.identity='f'*64
        step=self.advance();self.assertEqual(step['reason'],'configuration_changed')
        self.assertIsNone(self.candidates.history.get(self.doc['id']))
        self.assertIsNone(self.enrollment.get(self.doc['id']))

    def test_unknown_model_call_is_not_regenerated_or_submitted(self):
        self.enqueue()
        with patch.object(self.candidates.pool,'submit'):
            self.advance()
        row=self.candidates.history.get(self.doc['id'])
        with self.f.tasks.transaction() as db:self.candidates.history._event(db,row['id'],'model_requested')
        step=self.advance()
        self.assertEqual(step['reason'],'candidate_interrupted')
        self.advance();self.assertIsNone(self.jobs.get(self.doc['id']))
        self.assertEqual(self.client.calls.status()['remaining_requests'],1)

    def test_candidate_clarification_stops_before_execution(self):
        self.enqueue()
        with patch.object(self.candidates.pool,'submit'):self.advance()
        row=self.candidates.history.get(self.doc['id'])
        with self.f.tasks.transaction() as db:self.candidates.history._event(db,row['id'],'clarification',{'questions':['Which temperature?']})
        self.assertEqual(self.advance()['reason'],'candidate_clarification')
        self.assertIsNone(self.jobs.get(self.doc['id']))

    def test_endpoint_rejects_extra_authorization_and_unfrozen_inputs(self):
        with TestClient(create_app(self.f.tasks,model_client=self.client,candidate_service=self.candidates,execution_jobs=self.jobs),base_url=ORIGIN) as web:
            reply=web.post(self.url+'/workflow',headers=HEADERS,json={'revision':self.doc['revision'],'max_attempts':9})
            self.assertEqual(reply.status_code,422);self.assertIsNone(self.flow.get(self.doc['id']))
            draft=self.f.tasks.create('Synthetic draft','request','research')
            reply=web.post('/api/tasks/'+draft['id']+'/workflow',headers=HEADERS,json={'revision':draft['revision']})
            self.assertEqual(reply.status_code,422)
