"""Private configuration and ordinary HTTP author A actions, synthetic I/O only."""
import base64
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import shlex
import struct
import tempfile
from unittest.mock import patch
import unittest
from fastapi.testclient import TestClient

from auto_lammps.author_dispatch import AuthorStageClient, AuthorSlurmSubmitter, AuthorOutputCollector
from auto_lammps.author_dispatch_config import load_author_dispatch
from auto_lammps.ledger import Ledger, Policy, Resources
from auto_lammps.manifest import canonical, sha256
from auto_lammps.slurm_read import Observation
from auto_lammps.tasks import TaskError
from auto_lammps.web import create_app
from test_author_dispatch import collection
from test_web import ORIGIN, HEADERS
import test_author_reference_workspace as workspace_fixtures


class OfflineTransport:
    def __init__(self, connections, revision, host_alias):
        self.host_alias=host_alias
        self.identity=dict(revision=revision,host_alias=host_alias,profile=dict(work_directory='/synthetic-hpc'))
    def _identity(self): return deepcopy(self.identity)
    def command(self,host,remote):
        if host!=self.host_alias: raise ValueError('wrong host')
        return ['ssh','synthetic-offline',shlex.join(remote)]


class AuthorDispatchConfigTests(unittest.TestCase):
    def setUp(self):
        self.fixture=workspace_fixtures.AuthorWorkspaceTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.workspace=self.fixture.workspace; self.tasks=self.fixture.fx.tasks; self.task=self.fixture.task
        result=self.fixture.post(); self.assertEqual(result.json()['state'],'prepared')
        self.saved=self.workspace.planner._stored(self.task,'4'*32)
        self.report=result.json()['report']; self.evidence=self.saved[1]['input']
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.ledger=Ledger(self.root/'ledger.sqlite')
        self.ledger.create_campaign('author-config',Policy(10000000,32,604800,8*1024**3,1024**3,1,'9'*64))
        self.evaluation=self.ledger.register_evaluation('author-config',task_sha256='1'*64,repetition=0,
            role='reference',system_sha256='3'*64)
        self.workspace.papers.ledger=self.ledger
        self.alias=self.fixture.source.policy['host_alias']
        self.transport=OfflineTransport(None,1,self.alias)
        self.fixture.source.transport=self.transport
        resource=dict(cores=32,wall_seconds=3600,memory_bytes=8*1024**3,storage_bytes=4*1024**2)
        source_files=[dict(**{key:row[key] for key in ('path','size','sha256')},
            binding_sha256='4'*64,source_path=row['path']) for row in self.evidence['author_source']['files']]
        deployment=dict(schema_version=1,role='author_reference',profile_sha256='5'*64,approval_sha256='6'*64,
            execution_mode='native_slurm',formal_isolation=False,gpu_allowed=False,resources=resource,
            source_roots=[dict(binding_sha256='4'*64,path='/synthetic-hpc/original')],
            environments=[dict(id='synthetic-env',sha256='a'*64,executables=[dict(id='synthetic-lmp',sha256='b'*64,
                kind='lammps',path='/synthetic-runtime/lmp',launch_mode='mpi',child_process_policy='no_children',
                review_sha256='7'*64,entrypoints=[deepcopy(self.report['invocations'][0]['entrypoint'])])])])
        native=dict(scope='trusted_research',formal_isolation=False,storage_enforcement='collection_bound_only',
            max_ranks=32,shell_path='/synthetic-runtime/bash',bootstrap_path='/synthetic-runtime/bootstrap',
            modules=['synthetic-module'],mpi_path='/synthetic-runtime/mpirun',engine_path='/synthetic-runtime/lmp',
            files={path:'b'*64 for path in ['/synthetic-runtime/bash','/synthetic-runtime/bootstrap',
                '/synthetic-runtime/mpirun','/synthetic-runtime/lmp']})
        profile=dict(requests_root='/synthetic-hpc/requests',control_root='/synthetic-hpc/control',
            runtime_tree='/synthetic-runtime',execution_mode='native_slurm',native_slurm=native,
            author_reference={key:value for key,value in deployment.items() if key!='profile_sha256'},
            author_reference_runtime_sha256='c'*64)
        self.profile=self.root/'profile.json'; self.private(self.profile,canonical(profile))
        deployment['profile_sha256']=sha256(self.profile.read_bytes())
        self.grants=self.root/'grants'; self.grants.mkdir(mode=0o700)
        self.private(self.grants/'grant.key',b's'*32)
        def endpoint(helper,checksum):
            return dict(host_alias=self.alias,python_path='/synthetic/python',helper_path='/synthetic/'+helper,
                helper_sha256=checksum,root_path='/synthetic-hpc/requests')
        self.row=dict(task_id=self.task,connection_revision=1,evaluation=self.evaluation,resources=resource,
            source_files=source_files,deployment=deployment,
            provenance=dict(task_sha256='1'*64,analysis_sha256='2'*64,software_sha256='3'*64),
            profile=dict(path=str(self.profile),sha256=deployment['profile_sha256']),
            stage_endpoint=endpoint('receiver.py','a'*64),submission_endpoint=endpoint('submit.py','b'*64),
            collection_endpoint=endpoint('launcher.py','c'*64),
            authorization=dict(directory=str(self.grants),reviewed_commit='4'*40,scoring_sha256='2'*64,lifetime_seconds=3600),
            batch=dict(partition='cpu',account=None),following=dict(max_polls=240,interval_seconds=3600,
                query_timeout=20,query_max_bytes=65536,retain_queue_identity=True))
        self.config=dict(version=1,audit_directory=str(self.root/'audit'),snapshots_directory=str(self.root/'snapshots'),
            collections_directory=str(self.root/'collections'),registrations=[self.row])
        for name in ('audit','snapshots','collections'): (self.root/name).mkdir(mode=0o700)
        self.path=self.root/'author-config.json'; self.private(self.path,canonical(self.config))
        self.io=[]; self.posts=[]
        self.addCleanup(patch.stopall)
        patch('auto_lammps.author_dispatch_config.SavedHPCTransport',OfflineTransport).start()
        stage_type,submit_type,collector_type=AuthorStageClient,AuthorSlurmSubmitter,AuthorOutputCollector
        patch('auto_lammps.author_dispatch_config.AuthorStageClient',side_effect=lambda *args,**kwargs:
            stage_type(*args,capture=self.stage,**kwargs)).start()
        patch('auto_lammps.author_dispatch_config.AuthorSlurmSubmitter',side_effect=lambda *args,**kwargs:
            submit_type(*args,capture=self.submit,**kwargs)).start()
        patch('auto_lammps.author_dispatch_config.AuthorOutputCollector',side_effect=lambda *args,**kwargs:
            collector_type(*args,transfer_call=self.collect,**kwargs)).start()
        patch('auto_lammps.author_dispatch_config.SlurmReader',side_effect=self.reader).start()
        patch('auto_lammps.authorization._capture',side_effect=self.authorize).start()

    def private(self,path,raw):
        if path.exists(): path.chmod(0o600)
        path.write_bytes(raw); path.chmod(0o400)

    def reply(self,value): return dict(returncode=0,failure='',stdout=base64.b64encode(canonical(value)).decode(),stderr='')

    def stage(self,command,**kwargs):
        self.io.append('stage'); args=shlex.split(command[-1]); self.assertIn('--author-reference',args)
        raw=b''.join(kwargs['input_chunks']); size=struct.unpack('!I',raw[:4])[0]
        self.assertEqual(size,len(raw)-4); package=json.loads(raw[4:]); digest=sha256(raw[4:])
        return self.reply(dict(schema_version=1,state='staged',role='author_reference',
            request_id=args[args.index('--request-id')+1],manifest_sha256=digest,package_sha256=digest,
            profile_sha256=self.row['deployment']['profile_sha256'],storage_bytes=package['resources']['storage_bytes'],
            input_bytes=sum(row['size'] for row in package['files']),policy_sha256='e'*64,command_plan_sha256='f'*64,
            original_files_rewritten=False,physical_evaluation_performed=False,execution_authorized=False))

    def submit(self,command,**kwargs):
        self.io.append('submit'); args=shlex.split(command[-1]); self.assertIn('--author-reference',args)
        return self.reply(dict(state='accepted',request_id=args[args.index('--request-id')+1],
            manifest_sha256=args[args.index('--manifest-sha256')+1],job_id='321',evidence_sha256='d'*64))

    def authorize(self,command,**kwargs):
        self.io.append('authorize'); args=shlex.split(command[-1]); self.assertIn('--author-reference',args)
        self.assertIn('--install-authorization',args)
        payload=json.loads(b''.join(kwargs['input_chunks']))['envelope']['payload']
        return self.reply(dict(state='authorization_installed',request_id=payload['request_id'],
            manifest_sha256=payload['manifest_sha256'],grant_sha256=sha256(canonical(payload)),
            batch_sha256=payload['batch_sha256'],profile_sha256=payload['profile_sha256']))

    def reader(self,host,audit_directory,**kwargs):
        class Reader:
            def lookup(inner,*args,**kw):
                self.io.append('query')
                return Observation('completed','321',320,evidence_sha256='d'*64)
        value=Reader(); value.host_alias=host; value.transport=kwargs['transport']; value.max_bytes=kwargs['max_bytes']
        return value

    def collect(self,command,receiver,**kwargs):
        self.io.append('collect'); self.assertIn('--author-reference',command[-1])
        _,_,raw=collection(receiver.package,receiver.context['request_id'])
        for offset in range(0,len(raw),71): receiver.feed(raw[offset:offset+71])
        return dict(returncode=0,failure='',stdout_bytes=len(raw),stdout_sha256=sha256(raw),stderr='')

    def http(self):
        app=create_app(self.tasks,papers=self.workspace.papers,author_workspace_service=self.workspace,
            collections_directory=self.config['collections_directory'],author_execution_config=self.path)
        client=TestClient(app,base_url=ORIGIN); self.addCleanup(client.close)
        return client

    def test_ordinary_HTTP_prepares_approves_submits_follows_and_reuses_original_model_output(self):
        client=self.http(); url='/api/tasks/'+self.task+'/author-execution'
        # Real workspace preparation persists raw reader pairs separately, while
        # planner._context stores author_reference_input's verified projection.
        with self.tasks.transaction() as db:
            source=json.loads(db.execute('SELECT document FROM author_source_evidence').fetchone()[0])
        self.assertEqual(set(source),{'capability','acquisition_receipt','reads'})
        self.assertEqual(set(self.evidence['author_source']),{'capability','files','reads'})
        self.assertEqual(self.evidence['author_source']['files'],source['capability']['files'])
        source_reads=self.fixture.fx.reader.calls; model_calls=len(self.fixture.fx.sent)
        status=client.get(url).json(); self.assertTrue(status['can_prepare']); self.assertEqual(self.io,[])
        prepared=client.post(url+'/prepare',json={},headers=HEADERS)
        self.assertEqual(prepared.status_code,200,prepared.text); digest=prepared.json()['latest']['package_sha256']
        self.assertEqual(self.io,[]); self.assertEqual(self.ledger.evaluation_snapshot(self.evaluation)['requests'],[])
        self.assertEqual(client.post(url+'/submit',json=dict(package_sha256=digest),headers=HEADERS).status_code,422)
        approval=client.post(url+'/approve',json=dict(package_sha256=digest,revision=status['task_revision']),headers=HEADERS)
        self.assertEqual(approval.status_code,200,approval.text); self.assertTrue(approval.json()['can_submit']); self.assertEqual(self.io,[])
        submitted=client.post(url+'/submit',json=dict(package_sha256=digest),headers=HEADERS)
        self.assertEqual(submitted.status_code,200,submitted.text)
        self.assertEqual(submitted.json()['latest']['requests'][0]['job_id'],'321')
        self.assertEqual(self.io,['authorize','stage','submit'])
        self.assertEqual(client.post(url+'/submit',json=dict(package_sha256=digest),headers=HEADERS).status_code,200)
        follow=client.post(url+'/follow',json={},headers=HEADERS)
        self.assertEqual(follow.status_code,200,follow.text); self.assertEqual(follow.json()['following']['state'],'collected')
        self.assertEqual(self.io,['authorize','stage','submit','query','collect'])
        self.assertEqual(self.fixture.fx.reader.calls,source_reads); self.assertEqual(len(self.fixture.fx.sent),model_calls)
        visible=client.get('/api/tasks/'+self.task+'/author-plan').json()
        self.assertTrue(visible['execution']['latest']['approved'])
        self.assertEqual(visible['execution']['latest']['requests'][0]['actual_core_seconds'],320)

    def test_browser_cannot_supply_configuration_paths_commands_or_evaluation(self):
        client=self.http(); url='/api/tasks/'+self.task+'/author-execution'
        for action,body in [('prepare',dict(profile={'path':'/tmp/other','sha256':'a'*64})),
                ('approve',dict(revision=1,package_sha256='a'*64,source_files=[])),
                ('submit',dict(package_sha256='a'*64,evaluation=self.evaluation)),
                ('follow',dict(command=['sbatch']))]:
            with self.subTest(action=action):
                self.assertEqual(client.post(url+'/'+action,json=body,headers=HEADERS).status_code,422)
        self.assertEqual(self.io,[])

    def test_old_package_runtime_cannot_silently_change_registered_execution_binding(self):
        service=load_author_dispatch(self.workspace,self.path,None)
        binding=service.binding_provider(self.task)
        binding['deployment']['profile_sha256']='f'*64
        with self.assertRaisesRegex(TaskError,'原作业的部署'):
            service.runtime_provider(binding,{})
        self.assertEqual(self.io,[])

    def test_missing_registration_and_changed_local_config_are_explicit_not_remote_retries(self):
        service=load_author_dispatch(self.workspace,self.path,None)
        self.workspace.execution=service
        config=deepcopy(self.config); config['registrations']=[]
        self.private(self.path,canonical(config))
        status=service.status(self.task)
        self.assertFalse(status['can_prepare']); self.assertIn('配置已改变',status['disabled_reason'])
        self.assertEqual(self.io,[])

    def test_no_execution_configuration_keeps_preparation_but_cannot_submit(self):
        status=self.fixture.http.get('/api/tasks/'+self.task+'/author-execution')
        self.assertEqual(status.status_code,200,status.text)
        self.assertFalse(status.json()['can_submit']); self.assertIn('尚未登记',status.json()['disabled_reason'])
        post=self.fixture.http.post('/api/tasks/'+self.task+'/author-execution/prepare',json={},headers=HEADERS)
        self.assertEqual(post.status_code,422,post.text); self.assertEqual(self.io,[])

    def test_invalid_identity_profile_private_file_and_missing_key_reject_at_load(self):
        original=deepcopy(self.config)
        for change in ('role','profile','field','key','permissions'):
            with self.subTest(change=change):
                config=deepcopy(original)
                if change=='role':
                    agent=self.ledger.register_evaluation('author-config',task_sha256='1'*64,repetition=1,role='agent',system_sha256='3'*64)
                    config['registrations'][0]['evaluation']=agent
                elif change=='profile': config['registrations'][0]['profile']['sha256']='f'*64
                elif change=='field': config['registrations'][0].pop('source_files')
                elif change=='key': (self.grants/'grant.key').unlink()
                self.private(self.path,canonical(config))
                if change=='permissions': self.path.chmod(0o644)
                with self.assertRaises((ValueError,OSError,RuntimeError)):
                    load_author_dispatch(self.workspace,self.path,None)
                if change=='key': self.private(self.grants/'grant.key',b's'*32)
        self.assertEqual(self.io,[])


if __name__=='__main__': unittest.main()
