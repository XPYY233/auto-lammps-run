"""Ordinary author control and raw collection using only synthetic adapters."""
import base64
from copy import deepcopy
from dataclasses import asdict
import io
import json
from pathlib import Path
import shlex
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from auto_lammps.author_dispatch import (AuthorDispatch, AuthorFollowingService,
    AuthorOutputCollector, AuthorReceiver, AuthorSlurmSubmitter, AuthorSnapshot,
    AuthorStageClient, render_author_batch, validate_author_header, verify_author_payload)
from auto_lammps.batch_plan import BatchEnvironment
from auto_lammps.ledger import Conflict, Ledger, Policy, Resources
from auto_lammps.manifest import canonical, private_directory, sha256
from auto_lammps.papers import PaperStore
from auto_lammps.reconciliation import ReconciliationService
from auto_lammps.slurm_read import Observation
from auto_lammps.staging import StageEndpoint, StagingService, UploadUncertain
from auto_lammps.submission import SchedulerReceipt, Submission, SubmissionService
from auto_lammps.tasks import TaskError
import test_author_execution_package as fixtures


def collection(package, request, job='321', *, nested=False, name=None):
    name = name or ('nested/values.csv' if nested else 'values.csv')
    names = ['stdout.txt', 'stderr.txt', name]
    intent = dict(request_id=request, manifest_sha256=sha256(canonical(package)), job_id=job,
        role='author_reference', author_package_sha256=sha256(canonical(package)), outputs=names)
    result = dict(request_id=request, job_id=job, returncode=0, timed_out=False, scientific_status='not_evaluated')
    payload = {'execution-intent.json': canonical(intent), 'execution-result.json': canonical(result),
        'output/stdout.txt': b'', 'output/stderr.txt': b'', 'output/'+name: b'x,y\n1,2\n'}
    header = dict(schema_version=1, request_id=request, manifest_sha256=sha256(canonical(package)), job_id=job,
        execution=dict(state='finished', returncode=0, timed_out=False), scientific_status='not_evaluated',
        missing_outputs=[], files=[dict(path=path, size=len(raw), sha256=sha256(raw)) for path, raw in payload.items()])
    raw = canonical(header)
    return header, payload, struct.pack('!I', len(raw))+raw+b''.join(payload.values())


class SyntheticClient:
    transport = None
    def __init__(self, endpoint): self.endpoint=endpoint; self.uploads=0
    def upload(self, submission, snapshot):
        self.uploads += 1
        assert snapshot.verify()['role']=='author_reference'
        return '5'*64


class SyntheticScheduler:
    transport = None
    def __init__(self, ledger, endpoint): self.ledger=ledger; self.endpoint=endpoint; self.submits=0; self.fail=False
    def prepare(self, submission):
        assert any(event['kind']=='inputs_staged' for event in self.ledger.events(submission.request_id))
    def submit(self, submission):
        self.submits += 1
        if self.fail: raise OSError('synthetic uncertain submit')
        return SchedulerReceipt('321', '6'*64)


class SyntheticAuthorization:
    transport=None
    policy_sha256='7'*64
    def __init__(self, ledger): self.ledger=ledger; self.calls=0; self.delivered=True
    def ensure(self, submission, snapshot, batch):
        self.calls += 1
        assert '--author-reference' in batch.script.decode()
        assert snapshot.verify()['role']=='author_reference'
        return self.delivered
    def verify(self, submission, snapshot, batch): return '8'*64


class SyntheticReader:
    transport=None
    max_bytes=65536
    def __init__(self): self.calls=0; self.fail=False
    def lookup(self, request, manifest, **kwargs):
        self.calls += 1
        if self.fail: raise OSError('synthetic query unavailable')
        return Observation('completed', '321', 320, evidence_sha256='9'*64)


class AuthorDispatchTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.AuthorExecutionPackageTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.plan_fixture=self.fixture.fixture
        self.tasks=self.plan_fixture.tasks; self.task=self.plan_fixture.task
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.ledger=Ledger(self.root/'execution.sqlite')
        self.ledger.create_campaign('synthetic-reference', Policy(10000000,32,604800,8*1024**3,1024**3,1,'9'*64))
        self.evaluation=self.ledger.register_evaluation('synthetic-reference', task_sha256='1'*64,
            repetition=0, role='reference', system_sha256='3'*64)
        self.papers=PaperStore(self.tasks, ledger=self.ledger)
        self.binding=dict(report=self.fixture.report,evidence=self.fixture.evidence,source_files=self.fixture.files,
            deployment=self.fixture.deployment,resources=asdict(self.fixture.resources),provenance=self.fixture.provenance,
            evaluation=self.evaluation,task_revision=self.tasks.get(self.task['id'])['revision'],transport_identity=None)
        self.snapshots=private_directory(self.root/'snapshots')
        root='/synthetic-hpc/requests'
        self.stage_endpoint=StageEndpoint('synthetic','/synthetic/python','/synthetic/receiver.py','a'*64,root)
        self.submit_endpoint=StageEndpoint('synthetic','/synthetic/python','/synthetic/submit.py','b'*64,root)
        self.collect_endpoint=StageEndpoint('synthetic','/synthetic/python','/synthetic/launcher.py','c'*64,root)
        self.environment=BatchEnvironment('cpu',None,root,'/synthetic/python','/synthetic/launcher.py','c'*64)
        self.client=SyntheticClient(self.stage_endpoint)
        self.scheduler=SyntheticScheduler(self.ledger,self.submit_endpoint)
        self.authorization=SyntheticAuthorization(self.ledger)
        self.reader=SyntheticReader(); self.reconciliation=ReconciliationService(self.ledger,self.reader)
        self.transfers=0
        self.collector=AuthorOutputCollector(self.ledger,self.collect_endpoint,self.root/'collections',
            snapshots=self.snapshots,transfer_call=self.transfer)
        self.following=AuthorFollowingService(self.ledger,self.reconciliation,self.collector,interval_seconds=3600)
        self.runtime=dict(staging=StagingService(self.ledger,self.client),
            submission=SubmissionService(self.ledger,self.scheduler),authorization=self.authorization,
            following=self.following,environment=self.environment)
        self.service=AuthorDispatch(self.tasks,self.papers,self.ledger,binding_provider=lambda _:deepcopy(self.binding),
            runtime_provider=lambda binding,package:self.runtime,snapshots=self.snapshots)

    def transfer(self, command, receiver, **kwargs):
        self.transfers += 1
        self.assertIn('--author-reference', command[-1])
        _,_,raw=collection(receiver.package,receiver.context['request_id'])
        for index in range(0,len(raw),79): receiver.feed(raw[index:index+79])
        return dict(returncode=0,failure='',stdout_bytes=len(raw),stdout_sha256=sha256(raw),stderr='')

    def prepared(self):
        value=self.service.prepare(self.task['id'])
        return value['latest']['package_sha256']

    def approve(self):
        digest=self.prepared()
        self.service.approve(self.task['id'],self.binding['task_revision'],digest)
        return digest

    def test_full_ordinary_control_path_reuses_ledger_then_collects_real_fixture_bytes(self):
        before=self.ledger.evaluation_snapshot(self.evaluation)
        digest=self.prepared()
        self.assertEqual(self.ledger.evaluation_snapshot(self.evaluation),before)
        self.assertEqual(self.client.uploads,self.scheduler.submits,0)
        self.service.approve(self.task['id'],self.binding['task_revision'],digest)
        result=self.service.dispatch(self.task['id'],digest)
        request=result['latest']['requests'][0]
        self.assertEqual(request['job_id'],'321'); self.assertEqual(self.scheduler.submits,1)
        self.assertEqual(self.client.uploads,1)
        self.assertTrue(self.papers.reference_progress(self.task['id'])['entries'])
        result=self.service.follow(self.task['id'])
        self.assertEqual(result['following']['state'],'collected')
        self.assertTrue(result['following']['ready_for_analysis'])
        row=self.ledger.get(request['id'])
        self.assertEqual(row['actual_core_seconds'],320); self.assertEqual(row['accounted'],1)
        self.assertEqual(len(self.ledger.evaluation_snapshot(self.evaluation)['requests']),1)
        self.service.dispatch(self.task['id'],digest)
        self.service.follow(self.task['id'])
        self.assertEqual((self.client.uploads,self.scheduler.submits,self.transfers),(1,1,1))
        saved=next((self.root/'collections').glob('*/payload/output/values.csv'))
        self.assertEqual(saved.read_bytes(),b'x,y\n1,2\n')

    def test_approval_is_required_and_bound_to_exact_package(self):
        digest=self.prepared()
        with self.assertRaises(TaskError): self.service.dispatch(self.task['id'],digest)
        with self.assertRaises(TaskError): self.service.approve(self.task['id'],999,digest)
        self.assertEqual(self.scheduler.submits,0)
        self.assertEqual(self.ledger.evaluation_snapshot(self.evaluation)['requests'],[])
        self.service.approve(self.task['id'],self.binding['task_revision'],digest)
        self.service.approve(self.task['id'],self.binding['task_revision'],digest)
        self.assertEqual(len(self.tasks.approvals(self.task['id'])),1)
        self.binding['resources']['wall_seconds']=3660
        with self.assertRaises((TaskError,ValueError)): self.service.dispatch(self.task['id'],digest)
        self.assertEqual(self.scheduler.submits,0)

    def test_unknown_submission_is_never_retried_or_renamed(self):
        digest=self.approve(); self.scheduler.fail=True
        result=self.service.dispatch(self.task['id'],digest)
        self.assertEqual(result['latest']['requests'][0]['state'],'unknown')
        self.service.dispatch(self.task['id'],digest)
        self.assertEqual(self.scheduler.submits,1)
        self.assertEqual(len(self.ledger.evaluation_snapshot(self.evaluation)['requests']),1)

    def test_unconfirmed_grant_does_not_stage_or_submit(self):
        digest=self.approve(); self.authorization.delivered=False
        result=self.service.dispatch(self.task['id'],digest)
        self.assertEqual(result['waiting_reason'],'authorization_delivery_unconfirmed')
        self.assertEqual((self.client.uploads,self.scheduler.submits),(0,0))
        self.authorization.delivered=True
        self.service.dispatch(self.task['id'],digest)
        self.assertEqual(len(self.ledger.evaluation_snapshot(self.evaluation)['requests']),1)

    def test_staging_failure_keeps_original_reservation_and_prevents_submit(self):
        digest=self.approve()
        def unavailable(*args): raise UploadUncertain('synthetic uncertain staging')
        self.client.upload=unavailable
        with self.assertRaises(UploadUncertain): self.service.dispatch(self.task['id'],digest)
        self.assertEqual(self.scheduler.submits,0)
        snapshot=self.ledger.evaluation_snapshot(self.evaluation)
        self.assertEqual(len(snapshot['requests']),1); self.assertEqual(snapshot['requests'][0]['dispatch_claimed'],0)

    def test_agent_identity_missing_runtime_and_changed_transport_rejected(self):
        digest=self.approve()
        self.runtime.pop('following')
        with self.assertRaises(TaskError): self.service.dispatch(self.task['id'],digest)
        self.assertEqual(self.ledger.evaluation_snapshot(self.evaluation)['requests'],[])
        self.runtime['following']=self.following
        self.binding['transport_identity']={'changed':True}
        with self.assertRaises(TaskError): self.service.dispatch(self.task['id'],digest)
        self.assertEqual(self.scheduler.submits,0)

    def test_follow_survives_newer_preparation_and_cannot_submit(self):
        digest=self.approve(); self.service.dispatch(self.task['id'],digest)
        self.binding['deployment']['environments'][0]['executables'][0]['path']='/changed/runtime'
        newer=self.prepared()
        self.assertNotEqual(newer,digest)
        result=self.service.follow(self.task['id'])
        self.assertEqual(result['following']['state'],'collected')
        self.assertEqual(self.scheduler.submits,1)

    def test_saved_snapshot_mutation_blocks_dispatch(self):
        digest=self.approve(); path=self.snapshots/digest/'author-package.json'
        path.chmod(0o600); path.write_bytes(path.read_bytes()+b' ')
        with self.assertRaises(Conflict): self.service.dispatch(self.task['id'],digest)
        self.assertEqual(self.scheduler.submits,0)

    def test_query_failure_does_not_claim_scheduler_failure_or_resubmit(self):
        digest=self.approve(); self.service.dispatch(self.task['id'],digest); self.reader.fail=True
        result=self.service.follow(self.task['id'])
        self.assertEqual(result['following']['state'],'waiting')
        self.assertEqual(self.scheduler.submits,1)

    def test_native_submitter_uses_reference_flag_and_retains_acceptance_evidence(self):
        captures=[]
        def capture(command,**kwargs):
            args=shlex.split(command[-1]); self.assertIn('--author-reference',args)
            captures.append(args)
            receipt=dict(state='accepted',request_id=args[args.index('--request-id')+1],
                manifest_sha256=args[args.index('--manifest-sha256')+1],job_id='321',evidence_sha256='e'*64)
            return dict(returncode=0,failure='',stdout=base64.b64encode(canonical(receipt)).decode(),stderr='')
        scheduler=AuthorSlurmSubmitter(self.ledger,self.submit_endpoint,self.root/'submit-audit',capture=capture)
        self.runtime['submission']=SubmissionService(self.ledger,scheduler)
        digest=self.approve(); result=self.service.dispatch(self.task['id'],digest)
        request=result['latest']['requests'][0]
        self.assertEqual(request['job_id'],'321')
        self.assertEqual(scheduler.accepted_identity(request['id'],digest)['job_id'],'321')
        self.service.dispatch(self.task['id'],digest)
        self.assertEqual(len(captures),1)

    def test_bad_native_receipt_remains_unknown_and_is_not_resent(self):
        calls=[]
        def capture(*args,**kwargs):
            calls.append(True)
            return dict(returncode=0,failure='',stdout=base64.b64encode(b'{}').decode(),stderr='')
        scheduler=AuthorSlurmSubmitter(self.ledger,self.submit_endpoint,self.root/'submit-audit',capture=capture)
        self.runtime['submission']=SubmissionService(self.ledger,scheduler)
        digest=self.approve(); result=self.service.dispatch(self.task['id'],digest)
        self.assertEqual(result['latest']['requests'][0]['state'],'unknown')
        self.service.dispatch(self.task['id'],digest)
        self.assertEqual(len(calls),1)

    def test_failed_collection_is_not_repeated_by_supervisor_follow(self):
        digest=self.approve(); self.service.dispatch(self.task['id'],digest)
        calls=[]
        def failed(*args,**kwargs):
            calls.append(True)
            return dict(returncode=None,failure='timeout',stdout_bytes=0,stdout_sha256='a'*64,stderr='')
        self.collector.transfer=failed
        first=self.service.follow(self.task['id'])
        self.assertEqual(first['following']['state'],'collection_failed')
        again=self.service.follow(self.task['id'])
        self.assertEqual(again['following']['reason'],'collection_recovery_required')
        self.assertEqual(len(calls),1)

    def test_author_stage_wire_contains_only_package_not_local_source(self):
        digest=self.prepared(); package=AuthorSnapshot(self.snapshots/digest,digest).verify()
        submission=Submission('d'*32,digest,self.fixture.resources)
        def capture(command,**kwargs):
            self.assertIn('--author-reference',command[-1])
            raw=b''.join(kwargs['input_chunks']); size=struct.unpack('!I',raw[:4])[0]
            self.assertEqual(size,len(raw)-4); self.assertEqual(json.loads(raw[4:]),package)
            receipt=dict(schema_version=1,state='staged',role='author_reference',request_id=submission.request_id,
                manifest_sha256=digest,package_sha256=digest,profile_sha256=package['deployment']['profile_sha256'],
                storage_bytes=submission.resources.storage_bytes,input_bytes=sum(row['size'] for row in package['files']),
                original_files_rewritten=False,physical_evaluation_performed=False,execution_authorized=False,
                policy_sha256='e'*64,command_plan_sha256='f'*64)
            return dict(returncode=0,failure='',stdout=base64.b64encode(canonical(receipt)).decode(),stderr='')
        client=AuthorStageClient(self.stage_endpoint,self.root/'stage-audit',capture=capture)
        proof=client.upload(submission,AuthorSnapshot(self.snapshots/digest,digest))
        self.assertEqual(len(proof),64)


class AuthorReceiverTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.package=dict(outputs=[dict(path='nested/values.csv',kind='numeric_table')])
        self.header,self.payload,self.raw=collection(self.package,'a'*32,nested=True)
        self.context=dict(request_id='a'*32,manifest_sha256=sha256(canonical(self.package)),job_id='321',payload_bytes=1024**2)

    def test_nested_outputs_are_verified_without_log_lammps_or_scientific_claim(self):
        receiver=AuthorReceiver(self.root/'payload',self.context,self.package)
        for offset in range(0,len(self.raw),17): receiver.feed(self.raw[offset:offset+17])
        header=receiver.finish(); receiver.close()
        self.assertEqual(header,self.header)
        self.assertEqual((self.root/'payload/output/nested/values.csv').read_bytes(),b'x,y\n1,2\n')

    def test_deeper_output_parents_stay_private_and_cannot_follow_links(self):
        name='first/second/third/values.csv'
        package=dict(outputs=[dict(path=name,kind='numeric_table')])
        header,_,raw=collection(package,'a'*32,name=name)
        context=dict(self.context,manifest_sha256=sha256(canonical(package)))
        receiver=AuthorReceiver(self.root/'deep',context,package)
        receiver.feed(raw); receiver.finish(); receiver.close()
        for path in (self.root/'deep/output').rglob('*'):
            if path.is_dir(): self.assertEqual(path.stat().st_mode&0o077,0)
        receiver=AuthorReceiver(self.root/'linked',context,package)
        (self.root/'outside').mkdir()
        (self.root/'linked/output/first').symlink_to(self.root/'outside',target_is_directory=True)
        with self.assertRaises((ValueError,OSError)): receiver.feed(raw)
        receiver.close()
        self.assertEqual(list((self.root/'outside').iterdir()),[])

    def test_paths_overlap_unknown_outputs_and_budget_fail_before_payload(self):
        for change in ('traversal','absolute','overlap','unknown','budget','identity','role'):
            header=deepcopy(self.header); context=deepcopy(self.context)
            if change=='traversal': header['files'][-1]['path']='output/../secret'
            elif change=='absolute': header['files'][-1]['path']='/secret'
            elif change=='overlap': header['files'].append(dict(header['files'][-1],path='output/nested'))
            elif change=='unknown': header['files'][-1]['path']='output/unapproved.csv'
            elif change=='budget': context['payload_bytes']=1
            elif change=='identity': header['request_id']='b'*32
            else: header['scientific_status']='passed'
            with self.subTest(change=change),self.assertRaises(ValueError):
                validate_author_header(header,context,package=self.package)

    def test_corrupt_truncated_or_extra_bytes_are_not_valid_results(self):
        for index, raw in enumerate((self.raw[:-1],self.raw+b'extra',self.raw[:-1]+b'X')):
            receiver=AuthorReceiver(self.root/('payload'+str(index)),self.context,self.package)
            with self.assertRaises(ValueError):
                receiver.feed(raw); receiver.finish()
            receiver.close()

    def test_wrong_reference_role_in_execution_intent_is_rejected(self):
        intent=json.loads(self.payload['execution-intent.json']); intent['role']='agent'
        self.payload['execution-intent.json']=canonical(intent)
        for row in self.header['files']:
            row.update(size=len(self.payload[row['path']]),sha256=sha256(self.payload[row['path']]))
        raw=canonical(self.header)
        raw=struct.pack('!I',len(raw))+raw+b''.join(self.payload.values())
        receiver=AuthorReceiver(self.root/'wrong-role',self.context,self.package)
        receiver.feed(raw)
        with self.assertRaises(ValueError): receiver.finish()
        receiver.close()

    def test_reference_execution_controls_can_exceed_old_agent_16KiB_limit(self):
        names=['value-'+str(index)+'x'*180+'.csv' for index in range(101)]
        package=dict(outputs=[dict(path=name,kind='numeric_table') for name in names])
        digest=sha256(canonical(package))
        intent=dict(request_id='a'*32,manifest_sha256=digest,job_id='321',role='author_reference',
            author_package_sha256=digest,outputs=['stdout.txt','stderr.txt',*names])
        payload=dict(self.payload)
        payload.pop('output/nested/values.csv')
        payload['execution-intent.json']=canonical(intent)
        for name in names: payload['output/'+name]=b'1,2\n'
        header=dict(self.header,manifest_sha256=digest,files=[dict(path=path,size=len(raw),sha256=sha256(raw)) for path,raw in payload.items()])
        raw=canonical(header); raw=struct.pack('!I',len(raw))+raw+b''.join(payload.values())
        self.assertGreater(len(payload['execution-intent.json']),16384)
        receiver=AuthorReceiver(self.root/'large-controls',dict(self.context,manifest_sha256=digest),package)
        for offset in range(0,len(raw),65536): receiver.feed(raw[offset:offset+65536])
        receiver.finish();receiver.close()


if __name__=='__main__': unittest.main()
