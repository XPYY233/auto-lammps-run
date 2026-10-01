"""Automatic grant delivery uses real local files, with synthetic remote scheduler only."""
import base64
from dataclasses import asdict
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from auto_lammps import remote_submit, runtime_launcher as runtime
from auto_lammps.authorization import AutomaticAuthorization
from auto_lammps.execution_jobs import ExecutionJobs, load_execution_jobs
from auto_lammps.ledger import Conflict
from auto_lammps.manifest import canonical
import test_execution as execution
import test_native_slurm as native


class AuthorizationTests(unittest.TestCase):
    native_mode=False
    def setUp(self):
        f=execution.ExecutionTests()
        if self.native_mode:
            f.output_layout='working_directory';f.native_setup=native.native_profile
        f.setUp();self.addCleanup(f.doCleanups);self.f=f
        # Separate controller and remote control stores. Only one-time key setup
        # is a fixture; no request grant or batch is provided by the test.
        local=f.root/'local-authorization';local.mkdir(mode=0o700)
        f.remote.private(local/'grant.key',b'x'*32)
        pins={k:v for k,v in f.pins.items() if k!='static_check_sha256'}
        self.auth=AutomaticAuthorization(local,ledger=f.ledger,endpoint=f.controller.submission.scheduler.endpoint,
            audit_directory=f.root/'auth-audit',max_atoms=10000,lifetime_seconds=3600,**pins)
        f.controller.authorization=self.auth
        self.install_calls=0
    def install(self,argv,**kw):
        self.install_calls+=1;f=self.f
        self.assertIn('--install-authorization',argv[-1])
        package=b''.join(kw['input_chunks'])
        self.assertNotIn(b'grant.key',package);self.assertNotIn(b'x'*32,package)
        with patch.object(remote_submit,'capture_sbatch',side_effect=AssertionError('installer cannot submit')):
            try:
                receipt=remote_submit.install_authorization(f.submit_config,f.request_id,f.plan['snapshot'].digest,
                    f.remote.profile['requests_root'],io.BytesIO(package))
            except RuntimeError as error:
                return dict(returncode=1,failure=type(error).__name__,stdout='',stderr='')
        return dict(returncode=0,failure='',stdout=base64.b64encode(canonical(receipt)).decode(),stderr='')
    def patches(self):return patch('auto_lammps.authorization._capture',side_effect=self.install)
    def ensure(self):
        f=self.f
        return self.auth.ensure(f.plan['submission'],f.plan['snapshot'],f.plan['batch'])

    def test_blank_grant_stores_to_result_and_restart_without_manual_request_files(self):
        f=self.f
        self.assertFalse((f.remote.control/(f.request_id+'.json')).exists())
        jobs=ExecutionJobs(f.controller,{f.doc['id']:f.evaluation})
        with f.transports(),self.patches():
            jobs.enqueue(f.doc['id'],f.doc['revision']);first=jobs.advance(f.doc['id'])
            second=ExecutionJobs(f.controller,{f.doc['id']:f.evaluation}).advance(f.doc['id'])
        self.assertEqual(first['job']['state'],'analyzed');self.assertEqual(first,second)
        self.assertEqual(self.install_calls,1);self.assertEqual(f.dispatch.call_count,1)
        self.assertEqual(first['job']['dispatch_count'],1)
        self.assertEqual(first['job']['max_attempts'],2)
        receipt=f.controller.submission.scheduler.accepted_identity(f.request_id,f.plan['snapshot'].digest)
        self.assertEqual(receipt['job_id'],first['job']['job_id'])
        trace=f.controller.submission.scheduler.audit_directory/f.request_id/'result.json'
        saved_trace=trace.read_bytes();trace.write_bytes(saved_trace+b' ')
        with self.assertRaises(ValueError):f.controller.submission.scheduler.accepted_identity(f.request_id,f.plan['snapshot'].digest)
        trace.write_bytes(saved_trace)
        payload=json.loads((self.auth.directory/(f.request_id+'.json')).read_bytes())['payload']
        self.assertEqual(payload['scientific_status'],'not_evaluated')
        self.assertEqual(payload['reviewed_commit'],f.pins['reviewed_commit'])
        self.assertEqual((f.remote.control/(f.request_id+'.sh')).read_bytes(),f.plan['batch'].script)
        self.assertEqual((f.remote.control/(f.request_id+'.json')).read_bytes(),(self.auth.directory/(f.request_id+'.json')).read_bytes())
        if self.native_mode:self.assertFalse(payload['formal_isolation'])

    def test_explicit_development_exception_authorizes_same_plan_third_attempt(self):
        f=self.f
        for jid in ('123','124'):
            rid=f.plan['row']['id'];f.ledger.begin_dispatch(rid);f.ledger.accepted(rid,jid,{})
            f.ledger.observe(rid,jid,'failed',{});f.ledger.account(rid,0,'a'*64)
            if jid=='123':f.plan=f.controller.prepare(f.doc['id'],f.evaluation,retry_after=rid)
        f.ledger.approve_development_third_attempt(f.evaluation,approval_sha256='e'*64)
        f.plan=f.controller.prepare(f.doc['id'],f.evaluation,retry_after=rid)
        f.request_id=f.plan['row']['id']
        with self.patches():self.assertTrue(self.ensure())
        self.assertEqual(f.ledger.evaluation_snapshot(f.evaluation)['dispatch_claims'],2)
        f.ledger.begin_dispatch(f.request_id);f.ledger.accepted(f.request_id,'125',{})
        f.ledger.observe(f.request_id,'125','failed',{});f.ledger.account(f.request_id,0,'a'*64)
        f.ledger.approve_development_fourth_attempt(f.evaluation,approval_sha256='f'*64)
        f.plan=f.controller.prepare(f.doc['id'],f.evaluation,retry_after=f.request_id)
        f.request_id=f.plan['row']['id']
        with self.patches():self.assertTrue(self.ensure())
        snap=f.ledger.evaluation_snapshot(f.evaluation)
        self.assertEqual((snap['dispatch_claims'],snap['original_max_attempts'],snap['max_attempts']),(3,2,4))

    def test_lost_receipt_retries_identical_installation_before_any_submission(self):
        f=self.f;first=True
        def lost(argv,**kw):
            nonlocal first
            result=self.install(argv,**kw)
            if first:first=False;raise OSError('synthetic reply loss')
            return result
        with f.transports(),patch('auto_lammps.authorization._capture',side_effect=lost):
            result=f.controller.advance(f.doc['id'],f.evaluation)
            self.assertEqual(result['reason'],'authorization_delivery_unconfirmed')
            f.dispatch.assert_not_called();f.upload.assert_not_called()
            saved=(self.auth.directory/(f.request_id+'.bundle.json')).read_bytes()
            self.assertFalse(self.ensure());self.assertEqual(self.install_calls,1) # backoff, no SSH
            import time
            now=time.time()
            with patch('auto_lammps.authorization.time.time',return_value=now+61):
                result=f.controller.advance(f.doc['id'],f.evaluation)
            self.assertEqual(result['state'],'analyzed')
        self.assertEqual(saved,(self.auth.directory/(f.request_id+'.bundle.json')).read_bytes())
        self.assertEqual(self.install_calls,2);self.assertEqual(f.dispatch.call_count,1)

    def test_partial_remote_batch_installation_resumes_without_overwrite(self):
        f=self.f;original=remote_submit.private_bytes_once
        def partial(path,data):
            if str(path).endswith('.json'):raise OSError('synthetic interruption after batch')
            return original(path,data)
        with self.patches(),patch.object(remote_submit,'private_bytes_once',side_effect=partial):
            self.assertFalse(self.ensure())
        self.assertTrue((f.remote.control/(f.request_id+'.sh')).exists())
        self.assertFalse((f.remote.control/(f.request_id+'.json')).exists())
        import time
        now=time.time()
        with self.patches(),patch('auto_lammps.authorization.time.time',return_value=now+61):self.assertTrue(self.ensure())

    def test_conflicting_remote_batch_is_retained_and_cannot_dispatch(self):
        f=self.f;path=f.remote.control/(f.request_id+'.sh');f.remote.private(path,b'conflict')
        with f.transports(),self.patches():
            # Real helper process would return nonzero; this direct transport raises
            # ValueError, which is likewise recorded as unconfirmed delivery.
            result=f.controller.advance(f.doc['id'],f.evaluation)
        self.assertEqual(result['state'],'waiting');f.dispatch.assert_not_called()
        self.assertEqual(path.read_bytes(),b'conflict')

    def test_expired_grant_is_not_regenerated(self):
        f=self.f
        with self.patches():self.assertTrue(self.ensure())
        path=self.auth.directory/(f.request_id+'.bundle.json');saved=path.read_bytes()
        expires=json.loads(saved)['envelope']['payload']['expires_at']
        with self.patches(),patch('auto_lammps.authorization.time.time',return_value=expires+1),self.assertRaises(runtime.ExecutionDenied):self.ensure()
        self.assertEqual(path.read_bytes(),saved);self.assertEqual(self.install_calls,1)

    def test_changed_policy_and_remote_signing_key_do_not_authorize(self):
        f=self.f;f.remote.private(f.remote.control/'grant.key',b'y'*32)
        with self.patches():
            # Direct helper rejects signature before writing either request file.
            self.assertFalse(self.ensure())
        self.assertFalse((f.remote.control/(f.request_id+'.json')).exists())
        original=self.auth.policy_sha256;self.auth.policy_sha256='f'*64
        with self.patches(),self.assertRaises(Conflict):self.ensure()
        self.auth.policy_sha256=original

    def test_delivery_retries_are_bounded_without_consuming_a_submission(self):
        f=self.f
        with patch('auto_lammps.authorization._capture',side_effect=OSError('synthetic unavailable')) as call:
            import time
            start=time.time()
            for seconds in (0,61,122):
                with patch('auto_lammps.authorization.time.time',return_value=start+seconds):
                    self.assertFalse(self.ensure())
            with patch('auto_lammps.authorization.time.time',return_value=start+183),self.assertRaises(Conflict):self.ensure()
        self.assertEqual(call.call_count,3)
        self.assertEqual(f.ledger.evaluation_snapshot(f.evaluation)['dispatch_claims'],0)
        self.assertEqual(f.ledger.evaluation_snapshot(f.evaluation)['reserved_attempts'],1)

    def test_private_auto_configuration_load_has_no_signing_or_network_effects(self):
        f=self.f;c=f.controller
        value=dict(snapshots_directory=str(c.snapshots),collections_directory=str(c.following.analysis.collector.directory),
            reports_directory=str(c.following.analysis.directory),audit_directory=str(f.root/'service-audit'),
            stage_endpoint=asdict(c.staging.client.endpoint),submit_endpoint=asdict(c.submission.scheduler.endpoint),
            collect_endpoint=asdict(c.following.analysis.collector.endpoint),environment=asdict(c.environment),
            authorization_mode='automatic',authorization=dict(directory=str(self.auth.directory),**self.auth.pins,
                software_sha256=self.auth.software_sha256,max_atoms=10000,lifetime_seconds=3600),
            runtime_profile_path=str(f.remote.profile_path) if self.native_mode else None,
            max_polls=3,interval_seconds=15,query_max_bytes=1024,task_evaluations={f.doc['id']:f.evaluation})
        path=f.root/'execution-auto.json';path.write_bytes(canonical(value));path.chmod(0o600)
        before=f.ledger.events(f.request_id)
        with patch('auto_lammps.authorization._capture',side_effect=AssertionError('loading cannot install')):
            loaded=load_execution_jobs(f.tasks,f.ledger,path)
        self.assertIsInstance(loaded.controller.authorization,AutomaticAuthorization)
        self.assertEqual(f.ledger.events(f.request_id),before)
        self.assertFalse((self.auth.directory/(f.request_id+'.json')).exists())
        value['authorization_mode']='auto_fallback';path.write_bytes(canonical(value))
        with self.assertRaises(ValueError):load_execution_jobs(f.tasks,f.ledger,path)

    def test_installer_rejects_tampered_package_without_scheduler(self):
        f=self.f
        with self.patches():self.assertTrue(self.ensure())
        bundle=json.loads((self.auth.directory/(f.request_id+'.bundle.json')).read_bytes())
        package={k:bundle[k] for k in ('envelope','batch_base64')};package['batch_base64']=base64.b64encode(b'tampered').decode()
        with self.assertRaises(ValueError):
            remote_submit.install_authorization(f.submit_config,f.request_id,f.plan['snapshot'].digest,
                f.remote.profile['requests_root'],io.BytesIO(canonical(package)))


class NativeAuthorizationTests(AuthorizationTests):
    native_mode=True


if __name__=='__main__':unittest.main()
