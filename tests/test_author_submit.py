"""Fixed-author sbatch handoff with a mocked scheduler and synthetic grants."""
import base64
import hashlib
import hmac
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from auto_lammps import remote_submit
from auto_lammps.manifest import canonical, sha256
import test_author_native_runtime as runtime_fixtures


class AuthorSubmitTests(unittest.TestCase):
    def setUp(self):
        self.fixture=runtime_fixtures.AuthorNativeRuntimeTests()
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.request=self.fixture.request_id; self.checksum=self.fixture.package['package_sha256']
        self.root=self.fixture.fixture.requests
        self.batch=b'#!/bin/sh\n# Inert original-reference test batch; never executed.\n'
        self.fixture.payload['batch_sha256']=sha256(self.batch); self.fixture.sign()
        self.fixture.private(self.fixture.control/(self.request+'.sh'),self.batch)
        self.sbatch=self.fixture.fixture.deployment_root/'sbatch'
        self.sbatch.write_bytes(b'inert scheduler binary'); self.sbatch.chmod(0o400)
        runtime_path=self.fixture.fixture.deployment_root/'runtime_launcher.py'
        self.config=self.fixture.fixture.deployment_root/'submission.json'
        self.fixture.private(self.config,canonical(dict(runtime_path=str(runtime_path),runtime_sha256=sha256(runtime_path.read_bytes()),
            sbatch_path=str(self.sbatch),sbatch_sha256=sha256(self.sbatch.read_bytes()))))

    def submit(self,**kwargs):
        return remote_submit.submit(self.config,self.request,self.checksum,str(self.root),author_reference=True,**kwargs)

    def accepted(self):
        return dict(returncode=0,failure='',stdout=base64.b64encode(b'123\n').decode(),stderr='')

    def test_original_reference_submits_once_and_never_needs_an_agent_manifest(self):
        with patch.object(remote_submit.sys,'platform','linux'),patch.object(remote_submit,'capture_sbatch',return_value=self.accepted()) as capture:
            result=self.submit(); again=self.submit()
        self.assertEqual(result['state'],'accepted'); self.assertEqual(result['job_id'],'123')
        self.assertEqual(again['job_id'],'123'); self.assertEqual(capture.call_count,1)
        self.assertEqual(capture.call_args.args[0],[str(self.sbatch),'--parsable','--export=NIL',str(self.fixture.case/'job.sh')])
        self.assertFalse((self.fixture.case/'manifest.json').exists()); self.assertFalse((self.fixture.case/'generation.json').exists())
        self.assertTrue((self.fixture.case/'scheduler-intent.json').exists())
        self.assertTrue((self.fixture.case/'scheduler-result.json').exists())

    def test_role_original_and_stage_mismatch_cannot_reach_scheduler(self):
        with patch.object(remote_submit.sys,'platform','linux'),patch.object(remote_submit,'capture_sbatch') as capture:
            with self.assertRaises(ValueError):
                remote_submit.submit(self.config,self.request,self.checksum,str(self.root))
            self.fixture.payload['role']='agent'; self.fixture.sign()
            with self.assertRaises((ValueError,RuntimeError)): self.submit()
            self.fixture.payload['role']='author_reference'; self.fixture.sign()
            path=self.fixture.case/'workflow/example.txt'; raw=path.read_bytes()
            self.fixture.private(path,raw+b'changed')
            with self.assertRaises((ValueError,RuntimeError)): self.submit()
            self.fixture.private(path,raw)
            stage=self.fixture.case/'author-stage.json'; value=json.loads(stage.read_bytes())
            value['command_plan_sha256']='0'*64; self.fixture.private(stage,canonical(value))
            with self.assertRaises(ValueError): self.submit()
        capture.assert_not_called()

    def test_uncertain_scheduler_result_retained_without_resubmission(self):
        failed=dict(returncode=None,failure='timeout',stdout='',stderr='')
        with patch.object(remote_submit.sys,'platform','linux'),patch.object(remote_submit,'capture_sbatch',return_value=failed) as capture:
            result=self.submit(); again=self.submit()
        self.assertEqual(result['state'],'unknown'); self.assertEqual(again['state'],'unknown')
        self.assertEqual(capture.call_count,1)

    def test_reference_authorization_install_has_explicit_role_and_no_scheduler(self):
        envelope=dict(payload=self.fixture.payload,hmac_sha256=hmac.new(runtime_fixtures.KEY,
            canonical(self.fixture.payload),hashlib.sha256).hexdigest())
        raw=canonical(dict(envelope=envelope,batch_base64=base64.b64encode(self.batch).decode()))
        with patch.object(remote_submit,'capture_sbatch') as capture:
            with self.assertRaises(ValueError):
                remote_submit.install_authorization(self.config,self.request,self.checksum,str(self.root),io.BytesIO(raw))
            result=remote_submit.install_authorization(self.config,self.request,self.checksum,str(self.root),io.BytesIO(raw),author_reference=True)
        capture.assert_not_called(); self.assertEqual(result['state'],'authorization_installed')

    def test_large_reference_envelope_installs_and_submits_but_agent_path_rejects(self):
        self.fixture.payload['synthetic_reference_metadata']='x'*20000
        self.fixture.sign()
        envelope=dict(payload=self.fixture.payload,hmac_sha256=hmac.new(runtime_fixtures.KEY,
            canonical(self.fixture.payload),hashlib.sha256).hexdigest())
        self.assertGreater(len(canonical(envelope)),16384)
        raw=canonical(dict(envelope=envelope,batch_base64=base64.b64encode(self.batch).decode()))
        with patch.object(remote_submit,'capture_sbatch') as capture:
            with self.assertRaises(ValueError):
                remote_submit.install_authorization(self.config,self.request,self.checksum,str(self.root),io.BytesIO(raw))
            result=remote_submit.install_authorization(self.config,self.request,self.checksum,str(self.root),io.BytesIO(raw),author_reference=True)
        capture.assert_not_called(); self.assertEqual(result['state'],'authorization_installed')
        with patch.object(remote_submit.sys,'platform','linux'),patch.object(remote_submit,'capture_sbatch',return_value=self.accepted()) as capture:
            with self.assertRaises(RuntimeError):
                remote_submit.submit(self.config,self.request,self.checksum,str(self.root))
            capture.assert_not_called()
            result=self.submit()
        self.assertEqual(result['state'],'accepted'); self.assertEqual(capture.call_count,1)

    def test_reference_install_envelope_over_1MiB_is_rejected_without_writes(self):
        self.fixture.payload['synthetic_reference_metadata']='x'*(1024*1024)
        envelope=dict(payload=self.fixture.payload,hmac_sha256=hmac.new(runtime_fixtures.KEY,
            canonical(self.fixture.payload),hashlib.sha256).hexdigest())
        raw=canonical(dict(envelope=envelope,batch_base64=base64.b64encode(self.batch).decode()))
        self.assertLess(len(raw),1600000)
        with patch.object(remote_submit,'private_bytes_once') as write:
            with self.assertRaisesRegex(ValueError,'capacity'):
                remote_submit.install_authorization(self.config,self.request,self.checksum,str(self.root),io.BytesIO(raw),author_reference=True)
        write.assert_not_called()


if __name__=='__main__': unittest.main()
