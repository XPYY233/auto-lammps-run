"""Accounted author launcher guard paths with inert bytes and mocked processes."""
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import unittest
from unittest.mock import patch

from auto_lammps import runtime_launcher as runtime
from auto_lammps.author_reference_plan import validate_author_plan
from auto_lammps.manifest import canonical, sha256
import test_author_stage as stage_fixtures


KEY = b's'*32  # Synthetic only, never a deployment key.


class InertProcess:
    pid = 999999
    def __init__(self, argv, **kwargs):
        self.argv = argv; self.returncode = None
        (Path(kwargs['cwd'])/'values.csv').write_bytes(b'fixture,x\n1,2\n')
    def wait(self, timeout=None): self.returncode = 0; return 0
    def poll(self): return self.returncode


class AuthorNativeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = stage_fixtures.AuthorStageTests()
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.package_fixture = self.fixture.fixture
        self.base = self.fixture.base; self.request_id = self.fixture.request_id
        self.profile = self.fixture.profile; native = self.profile['native_slurm']
        inventory = {}
        for key in ('shell_path','bootstrap_path','mpi_path','engine_path'):
            path = self.fixture.deployment_root/key
            path.write_bytes(('inert synthetic '+key).encode()); path.chmod(0o400)
            native[key] = str(path); inventory[str(path)] = sha256(path.read_bytes())
        native['files'] = inventory
        program = self.profile['author_reference']['environments'][0]['executables'][0]
        program.update(path=native['engine_path'], sha256=inventory[native['engine_path']])
        self.package_fixture.evidence['deployment']['environments'][0]['executables'][0]['sha256'] = program['sha256']
        self.package_fixture.report = validate_author_plan(self.package_fixture.evidence,self.package_fixture.raw)
        self.scontrol = self.fixture.deployment_root/'scontrol'
        self.scontrol.write_bytes(b'inert scheduler query fixture'); self.scontrol.chmod(0o400)
        self.profile.update(scontrol_path=str(self.scontrol),scontrol_sha256=sha256(self.scontrol.read_bytes()))
        self.control = self.base/'synthetic-control'; self.control.mkdir(mode=0o700)
        self.private(self.control/'grant.key',KEY)
        self.fixture.rebind(); self.package = self.fixture.package
        self.fixture.receive(); self.case = self.fixture.requests/self.request_id
        self.payload = dict(request_id=self.request_id,manifest_sha256=self.package['package_sha256'],
            profile_sha256=sha256(self.fixture.profile_path.read_bytes()),expires_at=4102444800,
            approval_sha256=self.package['package']['deployment']['approval_sha256'],user_plan_approval_sha256='1'*64,
            task_sha256='1'*64,scoring_sha256='2'*64,static_check_sha256='3'*64,
            reviewed_commit='4'*40,resources=asdict(self.package_fixture.resources),
            outputs=['stdout.txt','stderr.txt','values.csv'],role='author_reference',
            author_package_sha256=self.package['package_sha256'],execution_scope='trusted_research',formal_isolation=False)
        self.sign()

    def private(self,path,raw):
        if path.exists(): path.chmod(0o600)
        path.write_bytes(raw); path.chmod(0o400)

    def sign(self):
        self.private(self.control/(self.request_id+'.json'),canonical(dict(payload=self.payload,
            hmac_sha256=hmac.new(KEY,canonical(self.payload),hashlib.sha256).hexdigest())))

    def allocation(self):
        return (f'JobId=123 JobName=al-{self.request_id} Comment=al:{self.package["package_sha256"]}:{self.request_id} '
            f'JobState=RUNNING NumNodes=1 NumCPUs=32 NodeList=compute-fixture BatchHost=compute-fixture Restarts=0 '
            f'UserId=synthetic({os.getuid()}) TimeLimit=01:00:00 RunTime=00:00:01\n').encode()

    def guards(self, process=InertProcess):
        stack = ExitStack()
        stack.enter_context(patch.object(runtime.sys,'platform','linux'))
        stack.enter_context(patch.dict(os.environ,{'SLURM_JOB_ID':'123'},clear=True))
        stack.enter_context(patch.object(runtime.os,'sched_getaffinity',return_value=set(range(32)),create=True))
        stack.enter_context(patch.object(runtime,'cgroup_cpu_set',return_value=set(range(32))))
        stack.enter_context(patch.object(runtime,'cgroup_memory_limit',return_value=8*1024**3))
        stack.enter_context(patch.object(runtime.socket,'gethostname',return_value='compute-fixture'))
        stack.enter_context(patch.object(runtime.subprocess,'run',return_value=subprocess.CompletedProcess([],0,self.allocation(),b'')))
        popen = stack.enter_context(patch.object(runtime.subprocess,'Popen',side_effect=process))
        return stack,popen

    def execute(self):
        return runtime.execute(self.fixture.profile_path,self.request_id,self.package['package_sha256'],author_reference=True)

    def test_real_common_allocation_guards_execute_one_original_without_agent_files(self):
        stack,popen = self.guards()
        with stack: result = self.execute()
        self.assertEqual(popen.call_count,1)
        self.assertEqual(result['returncode'],0); self.assertEqual(result['completed_action_count'],1)
        self.assertEqual(result['role'],'author_reference'); self.assertEqual(result['scientific_status'],'not_evaluated')
        self.assertTrue((self.case/'author-actions/original-call/intent.json').exists())
        self.assertTrue((self.case/'author-actions/original-call/result.json').exists())
        self.assertFalse((self.case/'generation.json').exists()); self.assertFalse((self.case/'analysis.json').exists())
        self.assertEqual(popen.call_args.args[0][-6:],[self.profile['native_slurm']['mpi_path'],'-np','32',
            self.profile['native_slurm']['engine_path'],'-in','workflow/example.txt'])
        stack,popen = self.guards()
        with stack,self.assertRaises((FileExistsError,runtime.ExecutionDenied)): self.execute()
        popen.assert_not_called()

    def test_reference_grant_cannot_cross_to_agent_and_wrong_role_cannot_launch(self):
        stack,popen = self.guards()
        with stack,self.assertRaises(runtime.ExecutionDenied):
            runtime.execute(self.fixture.profile_path,self.request_id,self.package['package_sha256'])
        popen.assert_not_called()
        self.payload['role'] = 'agent'; self.sign()
        stack,popen = self.guards()
        with stack,self.assertRaises(runtime.ExecutionDenied): self.execute()
        popen.assert_not_called()

    def test_large_reference_grant_has_1MiB_capacity_while_agent_remains_16KiB(self):
        self.payload['synthetic_reference_metadata']='x'*20000
        self.sign()
        envelope=(self.control/(self.request_id+'.json')).read_bytes()
        self.assertGreater(len(envelope),16384); self.assertLess(len(envelope),1024*1024)
        self.assertEqual(runtime.grant_envelope_limit(),16384)
        self.assertEqual(runtime.grant_envelope_limit(author_reference=True),1024*1024)
        stack,popen=self.guards()
        with stack,self.assertRaises(runtime.ExecutionDenied):
            runtime.execute(self.fixture.profile_path,self.request_id,self.package['package_sha256'])
        popen.assert_not_called()
        stack,popen=self.guards()
        with stack: result=self.execute()
        self.assertEqual(result['returncode'],0); self.assertEqual(popen.call_count,1)

    def test_reference_grant_over_1MiB_is_rejected_before_process(self):
        self.payload['synthetic_reference_metadata']='x'*(1024*1024)
        self.sign()
        stack,popen=self.guards()
        with stack,self.assertRaises(runtime.ExecutionDenied): self.execute()
        popen.assert_not_called()

    def test_changed_original_or_engine_denied_before_process(self):
        original = self.case/'workflow/example.txt'
        raw = original.read_bytes(); self.private(original,raw+b'\nchanged')
        stack,popen = self.guards()
        with stack,self.assertRaises(runtime.ExecutionDenied): self.execute()
        popen.assert_not_called(); self.private(original,raw)
        path = Path(self.profile['native_slurm']['engine_path']); self.private(path,b'changed inert engine')
        stack,popen = self.guards()
        with stack,self.assertRaises(runtime.ExecutionDenied): self.execute()
        popen.assert_not_called()

    def test_output_binding_must_match_original_artifacts(self):
        self.payload['outputs'].append('invented.csv'); self.sign()
        stack,popen = self.guards()
        with stack,self.assertRaises(runtime.ExecutionDenied): self.execute()
        popen.assert_not_called()

    def test_failed_or_timed_out_original_keeps_one_attempt_receipt(self):
        class Failed(InertProcess):
            def wait(self,timeout=None): self.returncode=2; return 2
        stack,popen = self.guards(Failed)
        with stack: result=self.execute()
        self.assertEqual(result['returncode'],2); self.assertEqual(popen.call_count,1)
        self.assertEqual(result['scientific_status'],'not_evaluated')
        self.assertTrue((self.case/'execution-result.json').exists())

    def test_timeout_kills_the_one_action_and_does_not_restart_it(self):
        class Timeout(InertProcess):
            waits=0
            def wait(self,timeout=None):
                self.waits+=1
                if self.waits==1: raise subprocess.TimeoutExpired('inert original fixture',timeout)
                self.returncode=-9; return -9
        stack,popen=self.guards(Timeout)
        with stack,patch.object(runtime.os,'killpg') as kill: result=self.execute()
        kill.assert_called_once_with(999999,runtime.signal.SIGKILL)
        self.assertTrue(result['timed_out']); self.assertEqual(result['returncode'],-9)
        self.assertEqual(popen.call_count,1)

    def test_original_actions_share_one_allocation_and_stop_at_first_failure(self):
        raw=self.package_fixture.raw
        for index in range(2):
            call=deepcopy(raw['invocations'][0]); call['id']='original-'+str(index)
            call['argv'].append('synthetic-argument-'+str(index)); raw['invocations'].append(call)
            raw['coverage'][0]['invocation_ids'].append(call['id'])
        self.package_fixture.report=validate_author_plan(self.package_fixture.evidence,raw)
        self.fixture.rebind(); self.package=self.fixture.package
        self.request_id='f'*32; self.fixture.receive(request_id=self.request_id)
        self.case=self.fixture.requests/self.request_id
        self.payload.update(request_id=self.request_id,manifest_sha256=self.package['package_sha256'],
            author_package_sha256=self.package['package_sha256'])
        self.sign()
        class FailSecond(InertProcess):
            count=0
            def wait(self,timeout=None):
                FailSecond.count+=1
                self.returncode=2 if FailSecond.count==2 else 0
                return self.returncode
        stack,popen=self.guards(FailSecond)
        with stack: result=self.execute()
        self.assertEqual(popen.call_count,2)
        self.assertEqual(result['action_count'],3); self.assertEqual(result['completed_action_count'],2)
        self.assertEqual(result['returncode'],2)
        self.assertFalse((self.case/'author-actions/original-1/intent.json').exists())

    def test_changed_stage_command_binding_denies_before_original_process(self):
        stage=self.case/'author-stage.json'; document=json.loads(stage.read_bytes())
        document['command_plan_sha256']='0'*64; self.private(stage,canonical(document))
        stack,popen=self.guards()
        with stack,self.assertRaisesRegex(runtime.ExecutionDenied,'stage receipt'): self.execute()
        popen.assert_not_called()

    def test_collection_uses_original_nested_paths_and_never_launches(self):
        stack,popen=self.guards()
        with stack: self.execute()
        stream=io.BytesIO()
        with patch.object(runtime.subprocess,'Popen') as popen:
            header=runtime.collect(self.fixture.profile_path,self.request_id,self.package['package_sha256'],'123',
                str(self.fixture.requests),stream,author_reference=True)
        popen.assert_not_called()
        self.assertIn('output/values.csv',[row['path'] for row in header['files']])
        self.assertEqual(header['execution']['returncode'],0)
        size=struct.unpack('!I',stream.getvalue()[:4])[0]
        self.assertEqual(json.loads(stream.getvalue()[4:4+size]),header)
        with self.assertRaises((FileNotFoundError,runtime.ExecutionDenied)):
            runtime.collect(self.fixture.profile_path,self.request_id,self.package['package_sha256'],'123',
                str(self.fixture.requests),io.BytesIO())

    def test_nested_original_directory_survives_stage_launch_and_collection(self):
        self.package_fixture.raw['invocations'][0].update(working_directory='workflow',argv=['-in','example.txt'])
        self.package_fixture.report=validate_author_plan(self.package_fixture.evidence,self.package_fixture.raw)
        self.fixture.rebind(); self.package=self.fixture.package
        self.request_id='f'*32; self.fixture.receive(request_id=self.request_id)
        self.case=self.fixture.requests/self.request_id
        self.payload.update(request_id=self.request_id,manifest_sha256=self.package['package_sha256'],
            author_package_sha256=self.package['package_sha256'],outputs=['stdout.txt','stderr.txt','workflow/values.csv'])
        self.sign()
        stack,popen=self.guards()
        with stack: result=self.execute()
        self.assertEqual(result['returncode'],0)
        self.assertEqual(popen.call_args.kwargs['cwd'],str(self.case/'output/workflow'))
        header=runtime.collect(self.fixture.profile_path,self.request_id,self.package['package_sha256'],'123',
            str(self.fixture.requests),io.BytesIO(),author_reference=True)
        self.assertIn('output/workflow/values.csv',[row['path'] for row in header['files']])

    def test_large_reference_output_intent_and_collection_keep_agent_receipt_bound(self):
        outputs=self.package_fixture.raw['invocations'][0]['output_files']
        template=deepcopy(outputs[0])
        for index in range(100):
            row=deepcopy(template); row['path']='artifact-'+str(index)+'-'+'x'*175+'.csv'
            outputs.append(row)
        self.package_fixture.report=validate_author_plan(self.package_fixture.evidence,self.package_fixture.raw)
        self.fixture.rebind(); self.package=self.fixture.package
        self.request_id='f'*32; self.fixture.receive(request_id=self.request_id)
        self.case=self.fixture.requests/self.request_id
        self.payload.update(request_id=self.request_id,manifest_sha256=self.package['package_sha256'],
            author_package_sha256=self.package['package_sha256'],
            outputs=['stdout.txt','stderr.txt',*[row['path'] for row in self.package['package']['outputs']]])
        self.sign(); declared=self.package['package']['outputs']
        class ManyInertOutputs(InertProcess):
            def __init__(self,argv,**kwargs):
                super().__init__(argv,**kwargs)
                for row in declared: (Path(kwargs['cwd'])/row['path']).write_bytes(b'inert output\n')
        stack,popen=self.guards(ManyInertOutputs)
        with stack: result=self.execute()
        self.assertEqual(result['returncode'],0); self.assertEqual(popen.call_count,1)
        intent=(self.case/'execution-intent.json').read_bytes()
        self.assertGreater(len(intent),16384); self.assertLess(len(intent),1024*1024)
        with self.assertRaises(runtime.ExecutionDenied):
            runtime.write_once(self.base/'old-agent-receipt.json',json.loads(intent))
        header=runtime.collect(self.fixture.profile_path,self.request_id,self.package['package_sha256'],'123',
            str(self.fixture.requests),io.BytesIO(),author_reference=True)
        self.assertEqual(header['missing_outputs'],[])
        self.assertEqual(len([row for row in header['files'] if row['path'].startswith('output/')]),103)


if __name__=='__main__': unittest.main()
