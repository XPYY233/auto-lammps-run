"""Researcher HTTP intents use real file adapters; never execute target physics."""
from dataclasses import asdict
import json
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from auto_lammps.execution_jobs import ExecutionJobs, load_execution_jobs
from auto_lammps.manifest import canonical
from auto_lammps.slurm_read import Observation
from auto_lammps.web import create_app
from auto_lammps.tasks import TaskError, StaleTask
import test_execution as execution
from test_results import ORIGIN
from test_candidate_jobs import frozen_research

HEADERS={'Origin':ORIGIN,'X-Task-Review':'1'}


class ExecutionJobTests(unittest.TestCase):
    def setUp(self):
        self.f=execution.ExecutionTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.task=self.f.doc['id'];self.revision=self.f.doc['revision'];self.url='/api/tasks/'+self.task+'/execution'
        self.jobs=self.service();self.addCleanup(self.jobs.close)

    def service(self):return ExecutionJobs(self.f.controller,{self.task:self.f.evaluation})

    def enqueue(self):return self.jobs.enqueue(self.task,self.revision)

    def await_state(self,client,state):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            response=client.get(self.url);self.assertEqual(response.status_code,200,response.text)
            result=response.json()
            if result.get('job',{}).get('state')==state:return result
            time.sleep(.02)
        self.fail('Worker did not reach '+state+': '+str(result))

    def test_http_background_execution_to_reports_and_raw_download_once(self):
        self.f.authorize_fixture()
        with self.f.transports(),TestClient(create_app(self.f.tasks,execution_jobs=self.jobs),base_url=ORIGIN) as client:
            before=self.f.ledger.events(self.f.request_id)
            ready=client.get(self.url).json();self.assertTrue(ready['can_start']);self.assertTrue(ready['worker_alive'])
            self.assertEqual(self.f.ledger.events(self.f.request_id),before)
            for _ in range(2):self.assertEqual(client.post(self.url,json={'revision':self.revision},headers=HEADERS).status_code,202)
            result=self.await_state(client,'analyzed');self.assertEqual(result['job']['dispatch_count'],1)
            self.assertEqual(result['job']['scientific_status'],'not_evaluated')
            reports=client.get('/api/tasks/'+self.task+'/results').json()['evaluations'][0]['requests'][0]['reports']
            self.assertTrue(reports)
            raw=client.get('/api/tasks/'+self.task+'/raw-files').json()['files'];self.assertEqual(len(raw),4)
            curve=next(f for f in raw if f['name']=='trajectory.dump')
            self.assertEqual(client.get('/api/tasks/'+self.task+'/raw-files/'+curve['id']).content,execution.DATA)
            self.assertNotIn(str(self.f.root),json.dumps(result))
            self.assertEqual((self.f.upload.call_count,self.f.dispatch.call_count,self.f.download.call_count),(1,1,1))
        self.assertFalse(self.jobs.thread.is_alive())

    def fail_first(self):
        self.enqueue()
        ledger=self.f.ledger;rid=self.f.request_id
        ledger.begin_dispatch(rid);ledger.accepted(rid,'123',{})
        ledger.observe(rid,'123','failed',{})
        ledger.account(rid,core_seconds=0,evidence_sha256='a'*64)
        job_id=self.jobs.get(self.task)['id']
        with self.f.tasks.transaction() as db:self.jobs._event(db,job_id,'attention','test_failure')

    def test_explicit_retry_preserves_plan_identity_and_survives_restart(self):
        self.fail_first()
        self.assertTrue(self.jobs.status(self.task)['job']['can_retry'])
        first=self.jobs.retry(self.task,self.revision)
        second=self.jobs.retry(self.task,self.revision)
        rid=first['job']['request_id'];self.assertEqual(rid,second['job']['request_id'])
        self.assertNotEqual(rid,self.f.request_id)
        self.assertEqual(first['job']['dispatch_count'],1)
        reopened=self.service()
        plan=self.f.controller.prepare(self.task,self.f.evaluation,retry_after=reopened.retry_parent(self.task))
        self.assertEqual(plan['row']['id'],rid)
        self.assertEqual(plan['snapshot'].digest,self.f.plan['snapshot'].digest)
        self.f.ledger.begin_dispatch(rid);self.f.ledger.accepted(rid,'124',{})
        self.f.ledger.observe(rid,'124','failed',{});self.f.ledger.account(rid,core_seconds=0,evidence_sha256='a'*64)
        job_id=self.jobs.get(self.task)['id']
        with self.f.tasks.transaction() as db:self.jobs._event(db,job_id,'attention','test_failure')
        self.assertFalse(self.jobs.status(self.task)['job']['can_retry'])
        with self.assertRaises(TaskError):self.jobs.retry(self.task,self.revision)

    def test_retry_rejects_unknown_or_unaccounted_and_stale_actions(self):
        self.enqueue()
        job_id=self.jobs.get(self.task)['id']
        with self.f.tasks.transaction() as db:self.jobs._event(db,job_id,'attention','test_unknown')
        with self.assertRaises(TaskError):self.jobs.retry(self.task,self.revision)
        with self.assertRaises(StaleTask):self.jobs.retry(self.task,self.revision-1)

    def test_restart_resumes_existing_queued_intent(self):
        self.f.authorize_fixture();self.enqueue();reopened=self.service()
        with self.f.transports():
            result=reopened.advance(self.task);again=reopened.advance(self.task)
        self.assertEqual(result,again);self.assertEqual(result['job']['state'],'analyzed');self.assertEqual(self.f.dispatch.call_count,1)
        self.assertEqual(reopened.get(self.task)['id'],self.jobs.get(self.task)['id'])

    def test_unknown_dispatch_keeps_same_request_and_waits(self):
        self.f.authorize_fixture();self.enqueue()
        with self.f.transports():
            self.f.dispatch.side_effect=OSError('synthetic acceptance unknown')
            self.f.query.return_value=Observation('unknown',reason='not_visible',evidence_sha256='a'*64)
            one=self.jobs.advance(self.task);two=self.service().advance(self.task)
            self.assertEqual(one['job']['request_id'],two['job']['request_id'])
            self.assertEqual(two['job']['state'],'waiting');self.assertEqual(two['job']['dispatch_count'],1)
            self.assertEqual(self.f.dispatch.call_count,1)

    def test_missing_grant_records_attention_without_upload(self):
        self.enqueue()
        with self.f.transports():
            result=self.jobs.advance(self.task);self.jobs.advance(self.task)
            self.f.upload.assert_not_called();self.f.dispatch.assert_not_called()
        self.assertEqual(result['job']['reason'],'deployment_file_missing')
        self.assertEqual(result['job']['dispatch_count'],0)
        self.f.authorize_fixture()
        self.assertEqual(self.service().advance(self.task)['job']['state'],'attention')

    def test_invalid_grant_does_not_dispatch(self):
        self.f.payload['expires_at']=0;self.f.authorize_fixture();self.enqueue()
        with self.f.transports():
            result=self.jobs.advance(self.task);self.f.dispatch.assert_not_called();self.f.upload.assert_not_called()
        self.assertEqual(result['job']['state'],'attention')

    def test_deployment_changes_cannot_silently_resume(self):
        self.f.authorize_fixture();self.enqueue();reopened=self.service();reopened.config_sha256='f'*64
        with self.f.transports():
            result=reopened.advance(self.task);self.f.upload.assert_not_called();self.f.dispatch.assert_not_called()
        self.assertEqual(result['job']['reason'],'deployment_changed')

    def test_concurrent_workers_have_one_dispatch(self):
        self.f.authorize_fixture();self.enqueue();other=self.service();entered=threading.Event();release=threading.Event()
        original=self.f.controller.advance
        def delayed(*args):entered.set();release.wait(5);return original(*args)
        with self.f.transports(),patch.object(self.f.controller,'advance',side_effect=delayed):
            thread=threading.Thread(target=self.jobs.advance,args=(self.task,));thread.start()
            try:
                self.assertTrue(entered.wait(5));other.advance(self.task)
            finally:release.set();thread.join(10)
            self.assertFalse(thread.is_alive());self.assertEqual(self.f.dispatch.call_count,1)

    def test_crash_after_accepted_dispatch_does_not_resubmit_on_restart(self):
        self.f.authorize_fixture();self.enqueue()
        with self.f.transports():
            with patch.object(self.f.following,'advance',side_effect=KeyboardInterrupt),self.assertRaises(KeyboardInterrupt):
                self.jobs.advance(self.task)
            self.assertEqual(self.jobs.get(self.task)['state'],'running')
            result=self.service().advance(self.task)
            self.assertEqual(result['job']['state'],'analyzed');self.assertEqual(self.f.dispatch.call_count,1)

    def test_unbound_task_stale_revision_and_extra_browser_fields_rejected(self):
        other=frozen_research(self.f.tasks)
        with self.assertRaises(TaskError):self.jobs.enqueue(other['id'],other['revision'])
        with self.assertRaises(StaleTask):self.jobs.enqueue(self.task,self.revision-1)
        with TestClient(create_app(self.f.tasks,execution_jobs=self.jobs),base_url=ORIGIN) as client:
            result=client.post(self.url,json={'revision':self.revision,'evaluation':self.f.evaluation},headers=HEADERS)
            self.assertEqual(result.status_code,422)
        self.assertIsNone(self.jobs.get(self.task))
        with self.f.tasks.transaction() as db:self.assertEqual(db.execute('SELECT count(*) FROM execution_jobs').fetchone()[0],0)

    def test_private_configuration_load_uses_same_pipeline_without_side_effects(self):
        c=self.f.controller
        value=dict(snapshots_directory=str(c.snapshots),collections_directory=str(c.following.analysis.collector.directory),
            reports_directory=str(c.following.analysis.directory),audit_directory=str(self.f.root/'service-audit'),
            stage_endpoint=asdict(c.staging.client.endpoint),submit_endpoint=asdict(c.submission.scheduler.endpoint),
            collect_endpoint=asdict(c.following.analysis.collector.endpoint),environment=asdict(c.environment),
            authorization=dict(directory=str(c.authorization.directory),**self.f.pins),runtime_profile_path=None,
            max_polls=3,interval_seconds=15,query_max_bytes=1024,task_evaluations={self.task:self.f.evaluation})
        path=self.f.root/'execution.json';path.write_bytes(canonical(value));path.chmod(0o600)
        before=self.f.ledger.events(self.f.request_id)
        loaded=load_execution_jobs(self.f.tasks,self.f.ledger,path)
        self.assertEqual(loaded.bindings,self.jobs.bindings);self.assertEqual(self.f.ledger.events(self.f.request_id),before)
        self.assertIsNone(loaded.thread)
        path.chmod(0o644)
        with self.assertRaises(Exception):load_execution_jobs(self.f.tasks,self.f.ledger,path)
