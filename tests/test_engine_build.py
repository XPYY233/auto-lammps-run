"""Synthetic build metadata, grants, budgets and file transfer; no compiler."""
from dataclasses import asdict
import base64
import hmac
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from auto_lammps import engine_build_worker as worker, engine_source_worker as source, runtime_launcher as runtime
from auto_lammps.batch_plan import BatchEnvironment
from auto_lammps.engine_build import (BuildAuthorization, EngineBuildExecution, build_manifest, freeze_build)
from auto_lammps.ledger import Ledger, Policy, Resources, Conflict, LimitExceeded
from auto_lammps.manifest import canonical,sha256
from auto_lammps.slurm_submit import SlurmSubmitter
from auto_lammps.staging import StageEndpoint, StageClient, StagingService, upload_chunks
from auto_lammps.submission import SubmissionService

H='a'*64


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve()
        self.req=source.requirements_from_models({'state':'finished','bindings':[{'required_package':'MEAM','pair_style':'meam'}]},release='2 Aug 2023',cores=2)
        self.spec=dict(kind='engine_build',source_directory=str(self.root/'source'),source_result_sha256=H,
            source_inventory_sha256=H,source_commit='c'*40,requirements=self.req,
            tools={k:dict(path='/usr/bin/'+v,sha256=H) for k,v in [('cmake','cmake'),('compiler','g++'),('mpi_compiler','mpicxx'),('make','make')]})
        self.resources=Resources(2,60,128*1024**2,32*1024**2)
        self.ledger=Ledger(self.root/'ledger.sqlite')
        self.ledger.create_campaign('shared',Policy(240,2,60,128*1024**2,128*1024**2,1,H))
        self.evaluation=self.ledger.register_evaluation('shared',task_sha256=sha256(canonical(self.spec)),
            repetition=0,role='development',system_sha256=H)
        self.requests=self.root/'requests';self.requests.mkdir(mode=0o700)
        self.control=self.root/'control';self.control.mkdir(mode=0o700)
        self.profile=dict(requests_root=str(self.requests),control_root=str(self.control),runtime_tree=str(self.root/'runtime'),
            output_volume=dict(kind='ext2-fuse',max_image_bytes=16*1024**2,mkfs_path='/usr/bin/mkfs',mkfs_sha256=H,
                fuse2fs_path='/usr/bin/fuse2fs',fuse2fs_sha256=H,fusermount_path='/usr/bin/fusermount',fusermount_sha256=H,
                helper_directory=str(self.root/'helpers')))
        self.raw=canonical(self.profile);self.profile_sha=sha256(self.raw)
        self.auth=BuildAuthorization(self.control,profile_sha256=self.profile_sha,reviewed_commit='e'*40,approval_sha256=H)
        common=dict(host_alias='fixture-hpc',python_path=str(Path(sys.executable).resolve()),root_path=str(self.requests))
        self.endpoint=StageEndpoint(**common,helper_path=str(Path(worker.__file__).resolve()),helper_sha256=sha256(Path(worker.__file__).read_bytes()))
        submit=StageEndpoint(**common,helper_path=str(self.root/'installed-submit.py'),helper_sha256=H)
        self.stager=StagingService(self.ledger,StageClient(self.endpoint,self.root/'stage-audit'))
        self.submitter=SlurmSubmitter(self.ledger,submit,self.root/'submit-audit')
        self.env=BatchEnvironment('fixture',None,str(self.requests),common['python_path'],self.endpoint.helper_path,self.endpoint.helper_sha256)
        self.service=EngineBuildExecution(self.ledger,self.root/'snapshots',self.stager,
            SubmissionService(self.ledger,self.submitter),self.auth,self.env)

    def sign(self,plan,**changes):
        key=b'x'*32;p=self.control/'grant.key'
        if not p.exists():p.write_bytes(key);p.chmod(0o600)
        request=plan['submission'];spec=self.spec
        grant=dict(purpose='engine_build',request_id=request.request_id,manifest_sha256=request.manifest_sha256,
            profile_sha256=self.profile_sha,reviewed_commit='e'*40,approval_sha256=H,expires_at=int(time.time())+600,
            resources=asdict(self.resources),task_sha256=sha256(canonical(spec)),
            scoring_sha256=sha256(canonical(spec['requirements'])),static_check_sha256=spec['source_inventory_sha256'],
            batch_sha256=plan['batch'].sha256)|changes
        p=self.control/(request.request_id+'.json');p.write_bytes(canonical(dict(payload=grant,hmac_sha256=hmac.new(key,canonical(grant),'sha256').hexdigest())));p.chmod(0o600)
        p=self.control/(request.request_id+'.sh');p.write_bytes(plan['batch'].script);p.chmod(0o600)

    def stage_capture(self,argv,**options):
        submission=self.plan['submission']
        with patch.object(worker.sys,'platform','linux'),patch.object(worker,'load_profile',return_value=(self.raw,self.profile,runtime,source)):
            result=worker.stage('/private/runtime.json',submission.request_id,submission.manifest_sha256,
                                str(self.requests),io.BytesIO(b''.join(options['input_chunks'])))
        return dict(returncode=0,failure='',stdout=base64.b64encode(canonical(result)).decode(),stderr='')

    def test_typed_snapshot_has_no_target_input_and_reserves_shared_budget(self):
        plan=self.service.prepare(self.evaluation,self.spec,self.resources)
        self.assertEqual(plan['snapshot'].verify()['kind'],'engine_build')
        self.assertEqual(set(p.name for p in plan['snapshot'].path.iterdir()),{'manifest.json','build.json'})
        self.assertEqual(plan['row']['charge_core_seconds'],120)
        other=self.ledger.register_evaluation('shared',task_sha256='b'*64,repetition=0,role='agent',system_sha256=H)
        with self.assertRaises(LimitExceeded):self.ledger.reserve(other,'candidate','c'*64,self.resources)
        self.assertEqual(self.ledger.evaluation_snapshot(other)['requests'],[])

    def test_unapproved_or_wrong_purpose_never_stages_or_dispatches(self):
        plan=self.service.prepare(self.evaluation,self.spec,self.resources)
        with patch.object(self.stager,'stage') as stage,patch.object(self.submitter,'submit') as submit:
            with self.assertRaises((OSError,runtime.ExecutionDenied)):self.service.advance(self.evaluation,self.spec,self.resources)
            self.sign(plan,purpose='simulation')
            with self.assertRaises(Conflict):self.service.advance(self.evaluation,self.spec,self.resources)
            stage.assert_not_called();submit.assert_not_called()
        self.assertFalse(self.ledger.get(plan['row']['id'])['dispatch_claimed'])

    def test_real_metadata_stage_and_unknown_submit_do_not_repeat(self):
        self.plan=self.service.prepare(self.evaluation,self.spec,self.resources);self.sign(self.plan)
        with patch('auto_lammps.staging._capture',side_effect=self.stage_capture) as stage,patch(
                'auto_lammps.slurm_submit._capture',return_value=dict(returncode=None,failure='timeout',stdout='',stderr='')) as submit:
            result=self.service.advance(self.evaluation,self.spec,self.resources)
            self.assertEqual(result['state'],'unknown')
            self.service.advance(self.evaluation,self.spec,self.resources)
            self.assertEqual(stage.call_count,1);self.assertEqual(submit.call_count,1)
        self.assertTrue((self.requests/result['id']/'stage.json').exists())
        self.assertEqual(self.ledger.get(result['id'])['charge_core_seconds'],120)

    def test_changed_snapshot_extra_file_and_agent_role_rejected(self):
        snap=freeze_build(self.root/'snapshots',self.spec,self.resources)
        (snap.path/'in.lammps').write_text('run 0')
        with self.assertRaises(ValueError):snap.verify()
        other=self.ledger.register_evaluation('shared',task_sha256=sha256(canonical(self.spec)),repetition=0,role='agent',system_sha256=H)
        with self.assertRaises((ValueError,Conflict)):self.service.prepare(other,self.spec,self.resources)
        for invalid in (self.resources.__class__(2,61,128*1024**2,32*1024**2),Resources(1,60,128*1024**2,32*1024**2)):
            with self.assertRaises(ValueError):build_manifest(self.spec,invalid)

    def test_worker_requires_linux_allocation_before_compiler(self):
        with patch.object(worker.sys,'platform','darwin'),patch.object(worker.subprocess,'Popen') as popen:
            with self.assertRaises(ValueError):worker.execute('/private/profile','b'*32,H)
            popen.assert_not_called()
        with patch.object(worker.sys,'platform','linux'),patch.object(worker,'load_profile',return_value=(self.raw,self.profile,runtime,source)),patch.dict(worker.os.environ,{'SLURM_JOB_ID':''}),patch.object(worker.subprocess,'Popen') as popen:
            with self.assertRaises(ValueError):worker.execute('/private/profile','b'*32,H)
            popen.assert_not_called()

    def test_source_file_and_tool_tampering_detected_before_build(self):
        root=self.root/'source';(root/'source/src').mkdir(parents=True,mode=0o700)
        (root/'source/src/.test-header').write_bytes(b'synthetic')
        inventory={'files':[{'path':'src/.test-header','size':9,'sha256':sha256(b'synthetic')}]}
        (root/'inventory.json').write_bytes(canonical(inventory))
        result=dict(state='source_ready',commit=self.spec['source_commit'],requirements=self.req)
        (root/'result.json').write_bytes(canonical(result))
        tool=self.root/'fake-tool';tool.write_bytes(b'not executable')
        worker.os.link(tool,self.root/'tool-hardlink')
        spec=self.spec|dict(source_result_sha256=sha256(canonical(result)),source_inventory_sha256=sha256(canonical(inventory)),
                           tools={k:dict(path=str(tool),sha256=sha256(tool.read_bytes())) for k in self.spec['tools']})
        self.assertEqual(worker.verify_source(runtime,source,spec),root/'source')
        tool.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'tool changed'):worker.verify_source(runtime,source,spec)
        (root/'source/src/.test-header').write_bytes(b'corrupted')
        with self.assertRaisesRegex(ValueError,'Source file changed'):worker.verify_source(runtime,source,spec)

    def test_compute_worker_records_build_success_without_executing_engine(self):
        from contextlib import contextmanager,ExitStack
        from types import SimpleNamespace
        self.profile.update(scontrol_path=str(self.root/'scontrol'),scontrol_sha256=sha256(b'synthetic scheduler'))
        (self.root/'scontrol').write_bytes(b'synthetic scheduler')
        self.raw=canonical(self.profile);self.profile_sha=sha256(self.raw)
        self.auth=BuildAuthorization(self.control,profile_sha256=self.profile_sha,reviewed_commit='e'*40,approval_sha256=H)
        self.service.authorization=self.auth
        self.plan=self.service.prepare(self.evaluation,self.spec,self.resources);self.sign(self.plan)
        self.stage_capture([],input_chunks=list(upload_chunks(self.plan['snapshot'])))
        request=self.plan['submission'];case=self.requests/request.request_id
        (case/'job.sh').write_bytes(self.plan['batch'].script)
        def compile_fixture(commands,output,**limits):
            self.assertEqual([name for name,argv in commands],['configure','build'])
            self.assertIn('BUILD_TESTING=OFF',commands[0][1])
            self.assertIn('CMAKE_MAKE_PROGRAM=/usr/bin/make',commands[0][1])
            self.assertNotIn('-in',commands[0][1]+commands[1][1])
            (output/'build').mkdir();(output/'build/lmp').write_bytes(b'\x7fELFsynthetic-not-executable')
            return [dict(stage=n,returncode=0,timed_out=False) for n,a in commands]
        @contextmanager
        def volume(profile,directory,allocation):
            output=directory/'output';output.mkdir(mode=0o700);yield output
        allocation=f'JobId=123 JobName=al-{request.request_id} Comment=al:{request.manifest_sha256}:{request.request_id} JobState=RUNNING NumNodes=1 NumCPUs=2 NodeList=fixture BatchHost=fixture Restarts=0 UserId=fixture({worker.os.getuid()}) TimeLimit=00:01:00 RunTime=00:00:00'
        with ExitStack() as stack:
            for target,value in [('sys.platform','linux')]:stack.enter_context(patch.object(worker.sys,'platform',value))
            stack.enter_context(patch.dict(worker.os.environ,{'SLURM_JOB_ID':'123'}))
            stack.enter_context(patch.object(worker,'load_profile',return_value=(self.raw,self.profile,runtime,source)))
            stack.enter_context(patch.object(runtime,'cgroup_cpu_set',return_value={1,2}))
            stack.enter_context(patch.object(worker.os,'sched_getaffinity',return_value={1,2},create=True))
            stack.enter_context(patch.object(runtime,'cgroup_memory_limit',return_value=self.resources.memory_bytes))
            stack.enter_context(patch.object(worker.socket,'gethostname',return_value='fixture'))
            stack.enter_context(patch.object(worker.subprocess,'run',return_value=SimpleNamespace(stdout=allocation.encode(),stderr=b'')))
            stack.enter_context(patch.object(worker,'verify_source',return_value=self.root/'source/source'))
            stack.enter_context(patch.object(runtime,'mounted_output_volume',side_effect=volume))
            compiler=stack.enter_context(patch.object(worker,'compile_commands',side_effect=compile_fixture))
            result=worker.execute('/private/runtime.json',request.request_id,request.manifest_sha256)
            self.assertTrue(result['built']);self.assertFalse(result['environment_verified']);self.assertFalse(result['scientific_validation'])
            self.assertEqual(result['engine_sha256'],sha256(b'\x7fELFsynthetic-not-executable'))
            with self.assertRaises(FileExistsError):worker.execute('/private/runtime.json',request.request_id,request.manifest_sha256)
            self.assertEqual(compiler.call_count,1)
        self.assertTrue((case/'execution-result.json').exists())
