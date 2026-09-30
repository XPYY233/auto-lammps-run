"""Native product execution/collection contracts using synthetic process output only."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from auto_lammps import runtime_launcher as runtime
from auto_lammps.agent_candidates import CandidateError, generate_candidate_draft
from auto_lammps.ledger import Conflict
from auto_lammps.manifest import canonical, sha256
from auto_lammps.staging import upload_chunks
from auto_lammps.slurm_read import Observation
import test_agent_candidates as candidates
import test_execution as execution
from test_analysis import DATA


def native_profile(remote):
    files={}
    native=dict(scope='trusted_research',formal_isolation=False,storage_enforcement='collection_bound_only',
                max_ranks=32,modules=['compiler/synthetic','lammps/synthetic'])
    for key in ('shell_path','bootstrap_path','mpi_path','engine_path'):
        path=remote.root/('native-'+key)
        path.write_bytes(('not executable; synthetic '+key).encode())
        native[key]=str(path);files[str(path)]=sha256(path.read_bytes())
    native['files']=files
    remote.profile.update(execution_mode='native_slurm',native_slurm=native)
    remote.profile_bytes=canonical(remote.profile)
    remote.private(remote.profile_path,remote.profile_bytes)


class NativeSlurmTests(unittest.TestCase):
    def setUp(self):
        f=execution.ExecutionTests();f.output_layout='working_directory';f.native_setup=native_profile;f.cores=4
        f.setUp();self.addCleanup(f.doCleanups);self.f=f
        f.seed_synthetic_output=self.execute_worker
        self.argv=None;self.process_options=None

    def execute_worker(self):
        f=self.f;r=f.remote;r.digest=f.plan['snapshot'].digest
        r.case=Path(r.profile['requests_root'])/f.request_id
        class SyntheticProcess:
            pid=999999
            returncode=None
            def __init__(inner,argv,**kwargs):
                self.argv=argv;self.process_options=kwargs
                self.assertEqual(Path(kwargs['cwd']),r.case/'output')
                for item in f.plan['snapshot'].verify()['files']:
                    self.assertEqual((Path(kwargs['cwd'])/item['path']).read_bytes(),(f.plan['snapshot'].path/item['path']).read_bytes())
                (Path(kwargs['cwd'])/'trajectory.dump').write_bytes(DATA)
                (Path(kwargs['cwd'])/'log.lammps').write_bytes(b'synthetic output only')
            def wait(inner,timeout=None):
                if timeout is not None and getattr(self,'timeout',False):
                    raise subprocess.TimeoutExpired(inner.argv if hasattr(inner,'argv') else [],timeout)
                inner.returncode=getattr(self,'exit_code',0);return inner.returncode
            def poll(inner):return inner.returncode
        stack,popen=r.guards(process=SyntheticProcess)
        allocation=r.allocation(NumCPUs='4',JobName='al-'+f.request_id,Comment=f'al:{r.digest}:{f.request_id}')
        with stack,patch.object(runtime,'cgroup_cpu_set',return_value={0,1,2,3}), \
                patch.object(runtime.os,'sched_getaffinity',return_value={0,1,2,3}), \
                patch.object(runtime.subprocess,'run',return_value=subprocess.CompletedProcess([],0,allocation.encode(),b'')), \
                patch.object(runtime.os,'killpg') as kill, \
                patch.object(runtime,'seccomp_fd',side_effect=AssertionError('native does not invoke sandbox')), \
                patch.object(runtime,'mounted_output_volume',side_effect=AssertionError('native does not invoke FUSE')):
            result=runtime.execute(r.profile_path,f.request_id,r.digest)
        self.assertEqual(result['returncode'],getattr(self,'exit_code',0))
        self.assertEqual(kill.call_count, int(getattr(self,'timeout',False)))
        self.assertFalse(result['formal_isolation'])
        return result

    def test_generation_native_worker_collection_analysis_and_restart_share_identity(self):
        f=self.f;f.authorize_fixture()
        with f.transports():
            first=f.execute();second=f.execute()
        self.assertEqual(first,second);self.assertEqual(first['state'],'analyzed')
        self.assertEqual((f.upload.call_count,f.dispatch.call_count,f.download.call_count),(1,1,1))
        self.assertEqual(f.ledger.evaluation_snapshot(f.evaluation)['dispatch_claims'],1)
        self.assertIn('-np',self.argv);self.assertIn('4',self.argv)
        self.assertNotIn('fork',self.argv);self.assertNotIn('127.0.0.1',self.argv)
        self.assertIn('I_MPI_HYDRA_BOOTSTRAP=slurm',self.argv[4])
        self.assertEqual(self.process_options['env']['SLURM_JOB_ID'],'123')
        intent=json.loads((f.remote.case/'execution-intent.json').read_bytes())
        self.assertEqual(intent['execution_mode'],'native_slurm');self.assertFalse(intent['formal_isolation'])
        self.assertEqual(intent['storage_enforcement'],'collection_bound_only')
        report=json.loads(next((f.root/'reports').glob('*.json')).read_bytes())['report']
        self.assertEqual(report['results'][2]['values']['slope'],2)
        self.assertEqual(report['scientific_status'],'not_evaluated')
        # A compute worker invoked again cannot start another target process.
        with self.assertRaises(FileExistsError):self.execute_worker()

    def test_failed_calculation_retains_diagnostics_and_does_not_analyze_or_retry(self):
        f=self.f;self.exit_code=7;f.authorize_fixture()
        with f.transports():
            f.query.return_value=Observation('failed','123',1,evidence_sha256='a'*64)
            first=f.execute();second=f.execute()
        self.assertEqual(first,second);self.assertEqual(first['state'],'diagnostics_saved')
        self.assertEqual(f.dispatch.call_count,1)
        self.assertFalse(list((f.root/'reports').glob('*.json')))
        self.assertEqual(f.ledger.evaluation_snapshot(f.evaluation)['dispatch_claims'],1)

    def test_native_timeout_saves_terminal_receipt_without_restarting(self):
        f=self.f;self.timeout=True;self.exit_code=-9;f.authorize_fixture()
        with f.transports():
            f.query.return_value=Observation('timeout','123',1,evidence_sha256='a'*64)
            result=f.execute()
        self.assertEqual(result['state'],'diagnostics_saved')
        receipt=json.loads((f.remote.case/'execution-result.json').read_bytes())
        self.assertTrue(receipt['timed_out']);self.assertEqual(receipt['returncode'],-9)
        self.assertEqual(f.dispatch.call_count,1)

    def test_scheduler_completion_does_not_override_failed_process(self):
        f=self.f;self.exit_code=7;f.authorize_fixture()
        with f.transports():result=f.execute()
        self.assertEqual(result['state'],'analysis_failed')
        report=json.loads(next((f.root/'reports').glob('*.json')).read_bytes())['report']
        self.assertEqual(report['scientific_status'],'not_evaluated')
        self.assertNotIn('results',report)

    def test_missing_native_scope_and_formal_claim_block_before_upload(self):
        f=self.f
        for scope,isolated in [(None,False),('formal_evaluation',False),('trusted_research',True)]:
            f.payload.update(execution_scope=scope,formal_isolation=isolated);f.authorize_fixture()
            with f.transports(),self.assertRaises(Conflict):f.execute()
            f.upload.assert_not_called();f.dispatch.assert_not_called()
        self.assertEqual(f.ledger.evaluation_snapshot(f.evaluation)['dispatch_claims'],0)

    def test_runtime_requires_explicit_scope_even_if_controller_is_not_used(self):
        f=self.f;f.authorize_fixture()
        f.stage_transport([],input_chunks=upload_chunks(f.plan['snapshot']))
        f.payload.pop('execution_scope');f.authorize_fixture()
        with self.assertRaisesRegex(runtime.ExecutionDenied,'non-isolated research grant'):self.execute_worker()
        self.assertIsNone(self.argv)

    def test_changed_native_binary_denied_and_no_fallback(self):
        f=self.f;f.authorize_fixture()
        f.stage_transport([],input_chunks=upload_chunks(f.plan['snapshot']))
        Path(f.remote.profile['native_slurm']['mpi_path']).write_bytes(b'changed')
        with self.assertRaisesRegex(runtime.ExecutionDenied,'environment file changed'):self.execute_worker()
        self.assertIsNone(self.argv)
        self.assertFalse((f.remote.case/'execution-intent.json').exists())

    def test_layout_mismatch_cannot_reserve_another_attempt(self):
        f=self.f
        # No profile means the original isolated default; it cannot accept native output paths.
        f.controller.runtime_profile_path=None
        with self.assertRaises(runtime.ExecutionDenied):f.controller.prepare(f.doc['id'],f.evaluation)
        self.assertEqual(f.ledger.evaluation_snapshot(f.evaluation)['reserved_attempts'],1)

    def test_native_profile_rejects_unknown_mode_shell_injection_and_hidden_quota_claims(self):
        profile=self.f.remote.profile
        for change in ({'execution_mode':'automatic_fallback'},):
            with self.assertRaises(runtime.ExecutionDenied):runtime.deployment_parallelism({**profile,**change},4)
        for key,value in [('modules',['compiler;echo pwn']),('formal_isolation',True),('max_ranks',True),
                          ('storage_enforcement','hard_quota'),('scope','blind_test')]:
            altered=deepcopy(profile);altered['native_slurm'][key]=value
            with self.subTest(key=key),self.assertRaises(runtime.ExecutionDenied):runtime.deployment_parallelism(altered,4)


class NativeCandidateTests(unittest.TestCase):
    def setUp(self):
        f=candidates.AgentCandidateTests();f.setUp();self.addCleanup(f.doCleanups);self.f=f
    def generate(self,layout):
        f=self.f
        return generate_candidate_draft(f.client,f.adapter,task_text='synthetic confirmed task',units='metal',
            resources=f.resources,store=f.root/'snapshots',output_layout=layout)
    def test_relative_output_frozen_without_rewriting_model_input(self):
        f=self.f;f.value['workflow']=f.value['workflow'].replace('/output/','')
        result=self.generate('working_directory')
        self.assertEqual(result['generation']['input']['output_layout'],'working_directory')
        self.assertIn(b'write_data final.data',(result['snapshot'].path/'in.lammps').read_bytes())
        self.assertEqual(result['generation']['proposal']['workflow'],f.value['workflow'])
    def test_invalid_layout_rejected_before_model_request(self):
        with self.assertRaises(CandidateError):self.generate('auto')
        self.f.transport.assert_not_called()
    def test_frozen_input_cannot_be_overwritten_by_analysis_output(self):
        f=self.f;f.value['workflow']='run 0\nwrite_data structure.data';f.value['analysis']['files']=['structure.data'];f.value['analysis']['plan']['tables'][0]['file']='structure.data';f.value['analysis']['plan']['operations'][0]['file']='structure.data'
        with self.assertRaisesRegex(CandidateError,'collides'):self.generate('working_directory')
        self.assertFalse(list((f.root/'snapshots').glob('*/manifest.json')))
    def test_absolute_output_not_silently_rewritten_for_native_mode(self):
        with self.assertRaises(CandidateError):self.generate('working_directory')


if __name__=='__main__':unittest.main()
