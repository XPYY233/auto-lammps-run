"""Synthetic candidate to real file-only adapters and analysis; no engine or SSH."""
import base64
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from auto_lammps import outputs, remote_stage, remote_submit, runtime_launcher as runtime
from auto_lammps.analysis import AnalysisService
from auto_lammps.batch_plan import BatchEnvironment
from auto_lammps.candidate_jobs import CandidateService
from auto_lammps.execution import CandidateExecution, ExistingAuthorization
from auto_lammps.following import FollowingService
from auto_lammps.ledger import Conflict, Ledger, Policy
from auto_lammps.manifest import canonical, sha256
from auto_lammps.reconciliation import ReconciliationService
from auto_lammps.slurm_read import Observation, SlurmReader
from auto_lammps.slurm_submit import SlurmSubmitter
from auto_lammps.staging import StageClient, StageEndpoint, StagingService
from auto_lammps.submission import SubmissionService
from auto_lammps.tasks import TaskStore
import test_agent_candidates as candidates
import test_runtime_launcher as runtime_fixtures
from test_candidate_jobs import frozen_research
from test_analysis import DATA, PLAN


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        f=candidates.AgentCandidateTests();f.setUp();self.addCleanup(f.doCleanups);self.f=f
        remote=runtime_fixtures.RuntimeTests();remote.setUp();self.addCleanup(remote.doCleanups);self.remote=remote
        self.root=f.root.resolve();self.tasks=TaskStore(self.root/'tasks.sqlite');self.doc=frozen_research(self.tasks)
        f.value['analysis']={'quantity':'synthetic curve','method':'synthetic arithmetic','files':['trajectory.dump'],'plan':deepcopy(PLAN)}
        f.value['workflow']='run 0\nprint "# columns: strain stress" file /output/trajectory.dump'
        service=CandidateService(self.tasks,f.client,f.adapter,resources=runtime_fixtures.RESOURCES,
                                 snapshots=self.root/'snapshots')
        service.enqueue(self.doc['id'],self.doc['revision']);service.close(wait=True)
        self.assertEqual(service.history.get(self.doc['id'])['state'],'prepared')
        self.ledger=Ledger(self.root/'ledger.sqlite')
        self.ledger.create_campaign('synthetic',Policy(1000,8,120,128*1024*1024,16*1024*1024,1,'a'*64))
        self.condition_hash=sha256(self.tasks.export(self.doc['id']))
        self.evaluation=self.ledger.register_evaluation('synthetic',task_sha256=self.condition_hash,
            repetition=0,role='agent',system_sha256='a'*64)
        requests=Path(remote.profile['requests_root'])
        (requests/'policy.json').write_bytes(canonical(dict(schema_version=1,max_total_bytes=4*1024*1024,approval_sha256='a'*64)))
        self.helper=remote.root/'runtime_launcher.py';self.helper.write_bytes(Path(runtime.__file__).read_bytes())
        common=dict(host_alias='fixture-login',python_path=str(Path(sys.executable).resolve()),root_path=str(requests))
        stage_endpoint=StageEndpoint(**common,helper_path=str(Path(remote_stage.__file__).resolve()),helper_sha256=sha256(Path(remote_stage.__file__).read_bytes()))
        submit_endpoint=StageEndpoint(**common,helper_path=str(Path(remote_submit.__file__).resolve()),helper_sha256=sha256(Path(remote_submit.__file__).read_bytes()))
        collect_endpoint=StageEndpoint(**common,helper_path=str(self.helper),helper_sha256=sha256(self.helper.read_bytes()))
        staging=StagingService(self.ledger,StageClient(stage_endpoint,self.root/'uploads'))
        submission=SubmissionService(self.ledger,SlurmSubmitter(self.ledger,submit_endpoint,self.root/'dispatch'))
        collector=outputs.OutputCollector(self.ledger,collect_endpoint,self.root/'collected')
        reader=SlurmReader('fixture-login',self.root/'queries',max_bytes=1024)
        self.following=FollowingService(self.ledger,ReconciliationService(self.ledger,reader),
            AnalysisService(collector,self.root/'reports'),self.root/'snapshots',max_polls=3)
        self.pins=dict(profile_sha256=sha256(remote.profile_bytes),reviewed_commit='c'*40,
            approval_sha256='a'*64,scoring_sha256='d'*64,static_check_sha256='e'*64,software_sha256='b'*64)
        auth=ExistingAuthorization(remote.control,**self.pins)
        environment=BatchEnvironment('synthetic',None,str(requests),common['python_path'],str(self.helper),collect_endpoint.helper_sha256)
        self.controller=CandidateExecution(self.tasks,self.ledger,self.root/'snapshots',staging,submission,self.following,auth,environment)
        self.plan=self.controller.prepare(self.doc['id'],self.evaluation);self.request_id=self.plan['row']['id']
        self.outputs=json.loads((self.plan['snapshot'].path/'analysis.json').read_bytes())['outputs']
        self.payload=dict(request_id=self.request_id,manifest_sha256=self.plan['snapshot'].digest,
            **{k:v for k,v in self.pins.items() if k!='software_sha256'},expires_at=4102444800,
            task_sha256=self.plan['snapshot'].verify()['provenance']['task_sha256'],
            resources=asdict(runtime_fixtures.RESOURCES),outputs=self.outputs,batch_sha256=self.plan['batch'].sha256)
        self.scheduler_binary=remote.root/'not-a-scheduler';self.scheduler_binary.write_bytes(b'never executed')
        self.submit_config=remote.root/'submission.json'
        remote.private(self.submit_config,canonical(dict(runtime_path=str(self.helper),runtime_sha256=sha256(self.helper.read_bytes()),
            sbatch_path=str(self.scheduler_binary),sbatch_sha256=sha256(self.scheduler_binary.read_bytes()))))

    def authorize_fixture(self):
        self.remote.private(self.remote.control/(self.request_id+'.json'),canonical(runtime_fixtures.signed(self.payload)))
        self.remote.private(self.remote.control/(self.request_id+'.sh'),self.plan['batch'].script)

    def stage_transport(self,argv,**kwargs):
        receipt=remote_stage.receive(self.remote.profile['requests_root'],self.request_id,self.plan['snapshot'].digest,
                                    io.BytesIO(b''.join(kwargs['input_chunks'])))
        return dict(returncode=0,failure='',stdout=base64.b64encode(canonical(receipt)).decode(),stderr='')

    def scheduler_transport(self,argv,**kwargs):
        # The actual helper verifies grant/stage/script and records an intent;
        # only sbatch is substituted. Generated LAMMPS text is never executed.
        with (patch.object(remote_submit.sys,'platform','linux'),
              patch.object(remote_submit,'capture_sbatch',return_value=dict(returncode=0,failure='',stdout=base64.b64encode(b'123\n').decode(),stderr=''))):
            receipt=remote_submit.submit(self.submit_config,self.request_id,self.plan['snapshot'].digest,self.remote.profile['requests_root'])
        self.seed_synthetic_output()
        return dict(returncode=0,failure='',stdout=base64.b64encode(canonical(receipt)).decode(),stderr='')

    def seed_synthetic_output(self):
        case=Path(self.remote.profile['requests_root'])/self.request_id
        self.remote.private(case/'execution-intent.json',canonical(dict(request_id=self.request_id,
            manifest_sha256=self.plan['snapshot'].digest,job_id='123',outputs=self.outputs)))
        self.remote.private(case/'execution-result.json',canonical(dict(request_id=self.request_id,
            job_id='123',returncode=0,timed_out=False,scientific_status='not_evaluated')))
        (case/'output').mkdir(mode=0o700)
        for name in self.outputs:self.remote.private(case/'output'/name,DATA if name=='trajectory.dump' else b'synthetic only\n')
        self.remote.private(case/'scheduler.stdout',b'synthetic only\n');self.remote.private(case/'scheduler.stderr',b'')

    def transports(self):
        stack=ExitStack()
        self.upload=stack.enter_context(patch('auto_lammps.staging._capture',side_effect=self.stage_transport))
        self.dispatch=stack.enter_context(patch('auto_lammps.slurm_submit._capture',side_effect=self.scheduler_transport))
        self.query=stack.enter_context(patch.object(self.following.reconciliation.reader,'lookup',
            return_value=Observation('completed','123',1,evidence_sha256='a'*64)))
        transfer=outputs.transfer
        command=[sys.executable,'-I',str(self.helper),'--collect','--root',self.remote.profile['requests_root'],
                 '--request-id',self.request_id,'--manifest-sha256',self.plan['snapshot'].digest,'--job-id','123']
        self.download=stack.enter_context(patch.object(outputs,'transfer',side_effect=lambda argv,receiver,**kw:transfer(command,receiver,**kw)))
        return stack

    def execute(self):return self.controller.run(self.doc['id'],self.evaluation)

    def test_prepared_candidate_through_real_adapters_to_report_once(self):
        self.authorize_fixture()
        with self.transports():
            first=self.execute();second=self.execute()
        self.assertEqual(first,second);self.assertEqual(first['state'],'analyzed')
        self.assertEqual((self.upload.call_count,self.dispatch.call_count,self.download.call_count),(1,1,1))
        self.assertEqual(self.f.transport.call_count,1)  # Synthetic model response, not a paid call.
        self.assertEqual(self.ledger.evaluation_snapshot(self.evaluation)['dispatch_claims'],1)
        events=self.ledger.events(self.request_id)
        binding=[json.loads(e['payload']) for e in events if e['kind']=='execution_authorized']
        self.assertEqual(len(binding),1)
        self.assertEqual(binding[0]['batch_sha256'],self.plan['batch'].sha256)
        self.assertEqual(binding[0]['grant_sha256'],sha256(canonical(self.payload)))
        self.assertLess([e['kind'] for e in events].index('execution_authorized'),[e['kind'] for e in events].index('dispatch_intent'))
        with self.assertRaises(Conflict):
            self.ledger.bind_execution_authorization(self.request_id,'0'*64,self.plan['batch'].sha256,binding[0]['policy_sha256'])
        report=json.loads(next((self.root/'reports').glob('*.json')).read_bytes())['report']
        self.assertEqual(report['results'][2]['values']['slope'],2)
        self.assertEqual(report['scientific_status'],'not_evaluated')

    def test_missing_grant_prevents_upload_and_dispatch(self):
        with self.transports(),self.assertRaises(FileNotFoundError):self.execute()
        self.upload.assert_not_called();self.dispatch.assert_not_called()
        self.assertFalse(self.ledger.get(self.request_id)['dispatch_claimed'])

    def test_signed_but_wrong_evidence_resources_outputs_or_batch_are_rejected(self):
        original=deepcopy(self.payload)
        for field,value in [('static_check_sha256','f'*64),('reviewed_commit','f'*40),('resources',dict(asdict(runtime_fixtures.RESOURCES),cores=2)),
                            ('outputs',['other.dat']),('batch_sha256','f'*64),('expires_at',0)]:
            self.payload={**original,field:value};self.authorize_fixture()
            with self.subTest(field=field),self.transports(),self.assertRaises((ValueError,Conflict,runtime.ExecutionDenied)):
                self.execute()
            self.upload.assert_not_called();self.dispatch.assert_not_called()

    def test_grant_expiry_during_upload_prevents_dispatch(self):
        self.authorize_fixture();original=self.stage_transport
        def expiring(argv,**kwargs):
            reply=original(argv,**kwargs);self.payload['expires_at']=0;self.authorize_fixture();return reply
        with self.transports():
            self.upload.side_effect=expiring
            with self.assertRaises(runtime.ExecutionDenied):self.execute()
        self.dispatch.assert_not_called()
        self.assertFalse(self.ledger.get(self.request_id)['dispatch_claimed'])

    def test_unknown_upload_cannot_be_retried_or_submitted(self):
        self.authorize_fixture()
        with self.transports():
            self.upload.side_effect=OSError('synthetic disconnected')
            with self.assertRaises(RuntimeError):self.execute()
            result=self.execute()
        self.assertEqual(result['reason'],'upload_unresolved')
        self.assertEqual(self.upload.call_count,1);self.dispatch.assert_not_called()

    def test_ambiguous_dispatch_uses_existing_identity_and_never_repeats(self):
        self.authorize_fixture()
        with self.transports():
            self.dispatch.side_effect=OSError('synthetic acceptance unknown')
            self.query.return_value=Observation('unknown',reason='not_visible',evidence_sha256='a'*64)
            first=self.controller.advance(self.doc['id'],self.evaluation)
            second=self.controller.advance(self.doc['id'],self.evaluation)
        self.assertEqual(first['state'],'waiting');self.assertEqual(second['state'],'waiting')
        self.assertEqual(self.dispatch.call_count,1);self.assertEqual(self.upload.call_count,1)
        self.download.assert_not_called()
        self.assertEqual(self.ledger.evaluation_snapshot(self.evaluation)['dispatch_claims'],1)

    def test_expired_authorization_does_not_block_completed_request_recovery(self):
        self.authorize_fixture()
        with self.transports():
            first=self.execute();self.payload['expires_at']=0;self.authorize_fixture();second=self.execute()
        self.assertEqual(first,second);self.assertEqual(self.dispatch.call_count,1)

    def test_wrong_role_or_condition_identity_cannot_reserve_another_attempt(self):
        for role,task in [('reference',self.condition_hash),('agent','f'*64)]:
            evaluation=self.ledger.register_evaluation('synthetic',task_sha256=task,repetition=0,role=role,system_sha256='a'*64)
            with self.assertRaises(Conflict):self.controller.prepare(self.doc['id'],evaluation)
            self.assertEqual(self.ledger.evaluation_snapshot(evaluation)['reserved_attempts'],0)

    def test_changed_candidate_and_pre_dispatch_cancellation_do_not_submit(self):
        self.authorize_fixture();self.ledger.cancel_intent(self.request_id)
        with self.transports():result=self.execute()
        self.assertEqual(result['reason'],'request_not_prepared');self.upload.assert_not_called()
        path=self.plan['snapshot'].path/'in.lammps';path.chmod(0o600);path.write_bytes(b'changed')
        with self.assertRaises(ValueError):self.controller.prepare(self.doc['id'],self.evaluation)


if __name__=='__main__':unittest.main()
