"""Synthetic runtime guard tests. No real engine, scheduler or namespace call."""
from contextlib import ExitStack, nullcontext
from dataclasses import asdict
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from auto_lammps import runtime_launcher as runtime
from auto_lammps.ledger import Resources
from auto_lammps.manifest import freeze
from auto_lammps.remote_stage import receive
from auto_lammps.staging import upload_chunks

H='a'*64
REQUEST='b'*32
KEY=b'x'*32  # Synthetic fixture, never a deployment credential.
RESOURCES=Resources(1,60,64*1024*1024,1024*1024)
OUTPUTS=['stdout.txt','stderr.txt','log.lammps','trajectory.dump']


def signed(payload):
    return dict(payload=payload,hmac_sha256=hmac.new(KEY,runtime.canonical(payload),hashlib.sha256).hexdigest())


class FakeProcess:
    pid=999999
    def __init__(self,argv,**kwargs):
        self.argv=argv
        self.returncode=None
    def wait(self,timeout=None):
        self.returncode=0
        return 0
    def poll(self):
        return self.returncode


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.source=self.root/'source'
        self.source.mkdir()
        (self.source/'input.in').write_text('# Synthetic placeholder, never executed\n')
        snapshot=freeze(self.source,self.root/'snapshots',files={'input.in':'lammps_input'},entrypoint='input.in',
                        resources=RESOURCES,provenance=dict(task_sha256=H,analysis_sha256=H,software_sha256=H))
        self.digest=snapshot.digest
        requests=self.root/'requests'
        requests.mkdir(mode=0o700)
        (requests/'policy.json').write_bytes(runtime.canonical(dict(schema_version=1,max_total_bytes=2*1024*1024,approval_sha256=H)))
        receive(str(requests),REQUEST,self.digest,io.BytesIO(b''.join(upload_chunks(snapshot))))
        self.case=requests/REQUEST
        self.control=self.root/'control'
        self.control.mkdir(mode=0o700)
        self.private(self.control/'grant.key',KEY)
        self.tree=self.root/'runtime-tree'
        (self.tree/'bin').mkdir(parents=True)
        (self.tree/'bin/lmp').write_bytes(b'not an engine; test inventory only')
        for folder in ('work','output','proc','dev','tmp'):
            (self.tree/folder).mkdir()
        self.bwrap=self.root/'bwrap-placeholder'
        self.scontrol=self.root/'scontrol-placeholder'
        self.bwrap.write_bytes(b'not executed')
        self.scontrol.write_bytes(b'not executed either')
        self.profile=dict(requests_root=str(requests),control_root=str(self.control),runtime_tree=str(self.tree),
            runtime_files={'bin/lmp':runtime.digest((self.tree/'bin/lmp').read_bytes())},engine_relative='bin/lmp',
            bwrap_path=str(self.bwrap),bwrap_sha256=runtime.digest(self.bwrap.read_bytes()),
            scontrol_path=str(self.scontrol),scontrol_sha256=runtime.digest(self.scontrol.read_bytes()))
        self.profile_path=self.root/'runtime.json'
        self.profile_bytes=runtime.canonical(self.profile)
        self.private(self.profile_path,self.profile_bytes)
        self.payload=dict(request_id=REQUEST,manifest_sha256=self.digest,profile_sha256=runtime.digest(self.profile_bytes),
            expires_at=4102444800,approval_sha256=H,task_sha256=H,scoring_sha256=H,static_check_sha256=H,
            reviewed_commit='c'*40,resources=asdict(RESOURCES),outputs=OUTPUTS)
        self.private(self.control/(REQUEST+'.json'),runtime.canonical(signed(self.payload)))

    def private(self,path,data):
        path.write_bytes(data)
        path.chmod(0o600)

    def allocation(self,**overrides):
        values=dict(JobId='123',JobName='al-'+REQUEST,Comment=f'al:{self.digest}:{REQUEST}',JobState='RUNNING',
                    NumNodes='1',NumCPUs='1',NodeList='compute-fixture',BatchHost='compute-fixture',Restarts='0',
                    UserId=f'synthetic({os.getuid()})',TimeLimit='00:01:00',RunTime='00:00:01')
        values.update(overrides)
        return ' '.join(f'{key}={value}' for key,value in values.items())+'\n'

    def guards(self,process=FakeProcess):
        stack=ExitStack()
        stack.enter_context(patch.object(runtime.sys,'platform','linux'))
        stack.enter_context(patch.dict(os.environ,{'SLURM_JOB_ID':'123'},clear=True))
        stack.enter_context(patch.object(runtime.os,'sched_getaffinity',return_value={0},create=True))
        stack.enter_context(patch.object(runtime.socket,'gethostname',return_value='compute-fixture'))
        stack.enter_context(patch.object(runtime,'cgroup_memory_limit',return_value=RESOURCES.memory_bytes))
        stack.enter_context(patch.object(runtime.subprocess,'run',return_value=subprocess.CompletedProcess([],0,self.allocation().encode(),b'')))
        stack.enter_context(patch.object(runtime,'seccomp_fd',side_effect=lambda profile: nullcontext(42)))
        popen=stack.enter_context(patch.object(runtime.subprocess,'Popen',side_effect=process))
        return stack,popen

    def test_signed_grant_rejects_tamper_expiry_and_wrong_request(self):
        envelope=signed(self.payload)
        args=dict(request_id=REQUEST,manifest_sha256=self.digest,profile_sha256=runtime.digest(self.profile_bytes),now=1)
        self.assertEqual(runtime.verify_grant(envelope,KEY,**args),self.payload)
        for change in ('tamper','expiry','identity'):
            altered=json.loads(json.dumps(envelope))
            if change=='tamper': altered['payload']['resources']['cores']=8
            elif change=='expiry': altered=signed({**self.payload,'expires_at':0})
            else: altered=signed({**self.payload,'request_id':'d'*32})
            with self.subTest(change=change),self.assertRaises(runtime.ExecutionDenied):
                runtime.verify_grant(altered,KEY,**args)

    def test_actual_platform_and_missing_job_block_before_process(self):
        with patch.object(runtime.sys,'platform','darwin'),patch.object(runtime.subprocess,'Popen') as popen:
            with self.assertRaises(runtime.ExecutionDenied): runtime.execute(self.profile_path,REQUEST,self.digest)
            popen.assert_not_called()
        with patch.object(runtime.sys,'platform','linux'),patch.dict(os.environ,{},clear=True):
            with self.assertRaises(runtime.ExecutionDenied): runtime.execute(self.profile_path,REQUEST,self.digest)

    def test_scheduler_identity_owner_cpu_restart_and_time_must_match(self):
        args=dict(request_id=REQUEST,manifest_sha256=self.digest,job_id='123',uid=os.getuid(),host='compute-fixture',resources=asdict(RESOURCES))
        self.assertEqual(runtime.parse_allocation(self.allocation(),**args),1)
        for change in [dict(Restarts='1'),dict(NumCPUs='2'),dict(NodeList='login-fixture'),dict(JobState='PENDING'),
                       dict(Comment=''),dict(UserId='synthetic(999999)'),dict(TimeLimit='00:02:00'),dict(RunTime='00:01:00')]:
            with self.subTest(change=change),self.assertRaises(runtime.ExecutionDenied):
                runtime.parse_allocation(self.allocation(**change),**args)
        with self.assertRaises(runtime.ExecutionDenied):
            runtime.parse_allocation(self.allocation()+' JobId=123',**args)

    def test_cgroup_hierarchical_minimum_and_unbounded_denial(self):
        mount=self.root/'cgroup'
        (mount/'parent/child').mkdir(parents=True)
        (mount/'memory.max').write_text('max')
        (mount/'parent/memory.max').write_text('1024')
        (mount/'parent/child/memory.max').write_text('2048')
        proc=self.root/'cgroup-membership'
        proc.write_text('0::/parent/child\n')
        self.assertEqual(runtime.cgroup_memory_limit(proc,mount),1024)
        (mount/'parent/memory.max').write_text('max')
        (mount/'parent/child/memory.max').write_text('max')
        with self.assertRaises(runtime.ExecutionDenied): runtime.cgroup_memory_limit(proc,mount)
        proc.write_text('0::/../escape\n')
        with self.assertRaises(runtime.ExecutionDenied): runtime.cgroup_memory_limit(proc,mount)

    def test_only_declared_outputs_get_writable_mounts(self):
        args=runtime.sandbox_command(self.profile,case=self.case,outputs=OUTPUTS,entrypoint='input.in',filter_fd=42)
        for flag in ('--unshare-user','--unshare-net','--seccomp','--new-session'):
            if flag=='--unshare-net': self.assertIn('--unshare-all',args)
            else: self.assertIn(flag,args)
        writable=[args[i+1:i+3] for i,arg in enumerate(args) if arg=='--bind']
        self.assertEqual(writable,[[str(self.case/'output'/name),'/output/'+name] for name in OUTPUTS])
        self.assertNotIn(str(self.control),args)
        self.assertNotIn('--share-net',args)
        limit=runtime.validate_outputs(OUTPUTS,storage_bytes=1024*1024,input_bytes=1024)
        self.assertLessEqual(limit*(len(OUTPUTS)+2)+1024+262144,1024*1024)
        with self.assertRaises(runtime.ExecutionDenied):
            runtime.validate_outputs(OUTPUTS+['../leak'],storage_bytes=1024*1024,input_bytes=1024)

    def test_runtime_tree_rejects_changed_extra_and_linked_files(self):
        runtime.verify_runtime_tree(self.profile)
        extra=self.tree/'extra'
        extra.write_text('undeclared')
        with self.assertRaises(runtime.ExecutionDenied): runtime.verify_runtime_tree(self.profile)
        extra.unlink()
        (self.tree/'link').symlink_to(self.control,target_is_directory=True)
        with self.assertRaises(runtime.ExecutionDenied): runtime.verify_runtime_tree(self.profile)

    def test_full_guard_path_records_once_without_running_engine(self):
        stack,popen=self.guards()
        with stack:
            result=runtime.execute(self.profile_path,REQUEST,self.digest)
            self.assertEqual(result['scientific_status'],'not_evaluated')
            self.assertEqual(popen.call_count,1)
            self.assertEqual(popen.call_args.kwargs['env'], {'PATH':'/usr/bin:/bin','LC_ALL':'C'})
            self.assertEqual(popen.call_args.kwargs['pass_fds'], (42,))
            self.assertTrue((self.case/'execution-intent.json').exists())
            self.assertTrue((self.case/'execution-result.json').exists())
            with self.assertRaises(FileExistsError): runtime.execute(self.profile_path,REQUEST,self.digest)
            self.assertEqual(popen.call_count,1)

    def test_wrong_memory_limit_blocks_engine(self):
        stack,popen=self.guards()
        with stack,patch.object(runtime,'cgroup_memory_limit',return_value=RESOURCES.memory_bytes+1):
            with self.assertRaises(runtime.ExecutionDenied): runtime.execute(self.profile_path,REQUEST,self.digest)
            popen.assert_not_called()

    def test_staged_input_tamper_blocks_engine(self):
        (self.case/'input.in').chmod(0o600)
        (self.case/'input.in').write_text('changed')
        stack,popen=self.guards()
        with stack:
            with self.assertRaises(runtime.ExecutionDenied): runtime.execute(self.profile_path,REQUEST,self.digest)
            popen.assert_not_called()

    def test_intent_disk_error_blocks_engine(self):
        stack,popen=self.guards()
        with stack,patch.object(runtime,'write_once',side_effect=OSError('synthetic disk full')):
            with self.assertRaises(OSError): runtime.execute(self.profile_path,REQUEST,self.digest)
            popen.assert_not_called()

    def test_unavailable_filter_and_overlapping_private_storage_block_engine(self):
        stack,popen=self.guards()
        with stack,patch.object(runtime,'seccomp_fd',side_effect=runtime.ExecutionDenied('Unavailable filter')):
            with self.assertRaises(runtime.ExecutionDenied): runtime.execute(self.profile_path,REQUEST,self.digest)
            popen.assert_not_called()
        with self.assertRaises(runtime.ExecutionDenied):
            runtime.validate_deployment_paths({**self.profile, 'control_root':str(self.case/'control')})

    def test_timeout_kills_process_and_retains_failed_receipt(self):
        class TimeoutProcess(FakeProcess):
            def wait(self,timeout=None):
                if timeout is not None: raise subprocess.TimeoutExpired('synthetic',timeout)
                self.returncode=-9
                return -9
        stack,popen=self.guards(TimeoutProcess)
        with stack,patch.object(runtime.os,'killpg') as kill:
            result=runtime.execute(self.profile_path,REQUEST,self.digest)
            self.assertTrue(result['timed_out'])
            self.assertEqual(result['returncode'],-9)
            self.assertEqual(result['scientific_status'],'not_evaluated')
            kill.assert_called_once_with(FakeProcess.pid,runtime.signal.SIGKILL)

    def test_real_os_file_size_limit_on_synthetic_bytes(self):
        # This subprocess only writes bytes; it does not load an engine or model.
        target=self.root/'limited-output'
        code='import resource,sys\nresource.setrlimit(resource.RLIMIT_FSIZE,(1024,1024))\nwith open(sys.argv[1],"wb") as f:\n f.write(b"x"*4096)\n f.flush()\n'
        result=subprocess.run([sys.executable,'-c',code,str(target)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=5)
        self.assertNotEqual(result.returncode,0)
        self.assertLessEqual(target.stat().st_size,1024)


if __name__=='__main__':
    unittest.main()
