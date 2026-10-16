"""Real author signing/delivery files with synthetic data and mocked transport."""
import base64
from copy import deepcopy
from dataclasses import asdict
import io
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from auto_lammps import remote_submit, runtime_launcher as runtime
from auto_lammps.author_authorization import AuthorAuthorization, BUNDLE_LIMIT
from auto_lammps.author_dispatch import AuthorSnapshot, render_author_batch
from auto_lammps.author_reference_plan import validate_author_plan
from auto_lammps.batch_plan import BatchEnvironment, BatchPlan
from auto_lammps.ledger import Conflict, Ledger, Policy, Resources
from auto_lammps.manifest import canonical, private_directory, sha256
from auto_lammps.staging import StageEndpoint
from auto_lammps.submission import Submission
import test_author_submit as remote_fixtures


class AuthorAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=remote_fixtures.AuthorSubmitTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.remote=self.fixture.fixture
        self.package=self.remote.package['package']; self.digest=self.remote.package['package_sha256']
        self.root=self.remote.base
        package_fixture=self.remote.package_fixture
        self.tasks=package_fixture.fixture.tasks; self.task=package_fixture.fixture.task
        self.ledger=Ledger(self.root/'author-authorization-ledger.sqlite')
        self.ledger.create_campaign('author-auth',Policy(10000000,32,604800,8*1024**3,1024**3,1,'a'*64))
        self.evaluation=self.ledger.register_evaluation('author-auth',task_sha256='1'*64,
            repetition=0,role='reference',system_sha256='3'*64)
        row=self.ledger.reserve(self.evaluation,'synthetic-original',self.digest,Resources(**self.package['resources']))
        self.submission=Submission(row['id'],self.digest,Resources(**self.package['resources']))
        path=private_directory(self.root/'author-auth-snapshot')
        self.remote.private(path/'author-package.json',canonical(self.package))
        self.snapshot=AuthorSnapshot(path,self.digest)
        self.endpoint=StageEndpoint('synthetic-reference','/synthetic/python',
            str(self.fixture.config.parent/'remote_submit.py'),'a'*64,str(self.remote.fixture.requests))
        environment=BatchEnvironment('cpu',None,self.endpoint.root_path,self.endpoint.python_path,
            str(self.fixture.config.parent/'runtime_launcher.py'),sha256(runtime_source()))
        self.batch=render_author_batch(self.submission,environment)
        self.local=private_directory(self.root/'author-local-authorization')
        self.remote.private(self.local/'grant.key',remote_fixtures.runtime_fixtures.KEY)
        self.args=dict(tasks=self.tasks,task_id=self.task['id'],ledger=self.ledger,endpoint=self.endpoint,
            audit_directory=self.root/'author-auth-audit',profile_sha256=self.package['deployment']['profile_sha256'],
            reviewed_commit='4'*40,approval_sha256=self.package['deployment']['approval_sha256'],
            scoring_sha256='2'*64,software_sha256='3'*64,lifetime_seconds=3600)
        self.authorization=AuthorAuthorization(self.local,**self.args)
        self.install_calls=0

    def approve(self,note='A real synthetic user approval event.'):
        self.tasks.approve_plan(self.task['id'],self.tasks.get(self.task['id'])['revision'],
            scope='author:'+self.digest,note=note)

    def ensure(self): return self.authorization.ensure(self.submission,self.snapshot,self.batch)

    def install(self,command,**kwargs):
        self.install_calls+=1
        self.assertIn('--install-authorization',command[-1]); self.assertIn('--author-reference',command[-1])
        raw=b''.join(kwargs['input_chunks'])
        self.assertNotIn(remote_fixtures.runtime_fixtures.KEY,raw)
        with patch.object(remote_submit,'capture_sbatch',side_effect=AssertionError('authorization cannot submit')):
            receipt=remote_submit.install_authorization(self.fixture.config,self.submission.request_id,
                self.digest,self.endpoint.root_path,io.BytesIO(raw),author_reference=True)
        return dict(returncode=0,failure='',stdout=base64.b64encode(canonical(receipt)).decode(),stderr='')

    def test_first_real_user_approval_creates_reference_grant_and_reuses_delivery(self):
        self.approve()
        first=next(row for row in self.tasks.approvals(self.task['id']) if row['scope']=='author:'+self.digest)
        before=self.ledger.evaluation_snapshot(self.evaluation)
        with patch('auto_lammps.authorization._capture',side_effect=self.install):
            self.assertTrue(self.ensure()); self.assertTrue(self.ensure())
            reloaded=AuthorAuthorization(self.local,**self.args)
            self.assertTrue(reloaded.ensure(self.submission,self.snapshot,self.batch))
        self.assertEqual(self.install_calls,1)
        self.assertEqual(self.ledger.evaluation_snapshot(self.evaluation)['dispatch_claims'],0)
        self.assertEqual(before['reserved_attempts'],1)
        self.assertEqual(len(self.tasks.approvals(self.task['id'])),1)
        envelope=json.loads((self.local/(self.submission.request_id+'.json')).read_bytes())
        payload=envelope['payload']
        self.assertEqual(payload['role'],'author_reference')
        self.assertEqual(payload['approval_sha256'],self.args['approval_sha256'])
        actual=sha256(canonical(dict(task_id=self.task['id'],**first)))
        self.assertEqual(payload['user_plan_approval_sha256'],actual)
        self.assertNotEqual(payload['user_plan_approval_sha256'],self.digest)
        self.assertFalse(payload['formal_isolation']); self.assertEqual(payload['execution_scope'],'trusted_research')
        self.assertEqual(set(payload['outputs']),{'stdout.txt','stderr.txt','values.csv'})
        self.assertEqual(self.authorization.verify(self.submission,self.snapshot,self.batch),sha256(canonical(payload)))
        bundle=json.loads((self.local/(self.submission.request_id+'.bundle.json')).read_bytes())
        self.assertNotIn('package',bundle['report']); self.assertNotIn('invocations',bundle['report'])
        self.assertLess(len(canonical(bundle)),BUNDLE_LIMIT)

    def test_no_approval_no_key_or_altered_package_never_delivers(self):
        with patch('auto_lammps.authorization._capture') as capture:
            with self.assertRaises(Conflict): self.ensure()
            self.assertEqual(self.tasks.approvals(self.task['id']),[])
            self.approve()
            (self.local/'grant.key').unlink()
            with self.assertRaises(FileNotFoundError): self.ensure()
            self.assertFalse((self.local/'grant.key').exists())
        capture.assert_not_called()
        self.assertFalse((self.local/(self.submission.request_id+'.json')).exists())

    def test_unknown_or_already_claimed_reference_cannot_mint_a_grant(self):
        self.approve(); self.ledger.begin_dispatch(self.submission.request_id)
        with patch('auto_lammps.authorization._capture') as capture:
            with self.assertRaises(Conflict): self.ensure()
            self.ledger.uncertain(self.submission.request_id,{'synthetic':True})
            with self.assertRaises(Conflict): self.ensure()
        capture.assert_not_called()
        self.assertFalse((self.local/(self.submission.request_id+'.bundle.json')).exists())

    def test_agent_role_cannot_receive_author_authorization(self):
        self.approve()
        other=Ledger(self.root/'agent-auth.sqlite')
        other.create_campaign('agent',Policy(10000000,32,604800,8*1024**3,1024**3,1,'a'*64))
        evaluation=other.register_evaluation('agent',task_sha256='1'*64,repetition=0,role='agent',system_sha256='3'*64)
        row=other.reserve(evaluation,'synthetic-agent',self.digest,Resources(**self.package['resources']))
        auth=AuthorAuthorization(self.local,**dict(self.args,ledger=other))
        submission=Submission(row['id'],self.digest,Resources(**self.package['resources']))
        with patch('auto_lammps.authorization._capture') as capture,self.assertRaises(Conflict):
            auth.ensure(submission,self.snapshot,self.batch)
        capture.assert_not_called()

    def test_delivery_loss_keeps_one_immutable_grant_and_reuses_original_install(self):
        self.approve(); initial=time.time(); failed=True
        def lose(command,**kwargs):
            nonlocal failed
            result=self.install(command,**kwargs)
            if failed: failed=False; raise OSError('synthetic lost installation reply')
            return result
        with patch('auto_lammps.authorization._capture',side_effect=lose):
            self.assertFalse(self.ensure())
            raw=(self.local/(self.submission.request_id+'.bundle.json')).read_bytes()
            self.assertFalse(self.ensure()); self.assertEqual(self.install_calls,1)
            with patch('auto_lammps.authorization.time.time',return_value=initial+61): self.assertTrue(self.ensure())
        self.assertEqual(raw,(self.local/(self.submission.request_id+'.bundle.json')).read_bytes())
        self.assertEqual(self.install_calls,2)
        self.assertEqual(self.ledger.evaluation_snapshot(self.evaluation)['dispatch_claims'],0)

    def test_changed_policy_batch_or_approval_receipt_cannot_reissue(self):
        self.approve()
        with patch('auto_lammps.authorization._capture',side_effect=self.install): self.assertTrue(self.ensure())
        bundle_path=self.local/(self.submission.request_id+'.bundle.json'); raw=bundle_path.read_bytes()
        original=self.authorization.policy_sha256; self.authorization.policy_sha256='0'*64
        with self.assertRaises(Conflict): self.ensure()
        self.authorization.policy_sha256=original
        other=BatchPlan(self.batch.script+b'# modified\n',sha256(self.batch.script+b'# modified\n'))
        with self.assertRaises(Conflict): self.authorization.verify(self.submission,self.snapshot,other)
        self.approve('Later duplicate approval does not replace the first.')
        with patch('auto_lammps.authorization._capture',side_effect=self.install): self.assertTrue(self.ensure())
        self.assertEqual(raw,bundle_path.read_bytes()); self.assertEqual(self.install_calls,1)

    def test_expired_grant_and_corrupt_mirror_are_not_regenerated(self):
        self.approve()
        with patch('auto_lammps.authorization._capture',side_effect=self.install): self.assertTrue(self.ensure())
        path=self.local/(self.submission.request_id+'.bundle.json'); raw=path.read_bytes()
        expires=json.loads(raw)['envelope']['payload']['expires_at']
        with patch('auto_lammps.author_authorization.time.time',return_value=expires+1),self.assertRaises(runtime.ExecutionDenied):
            self.ensure()
        self.assertEqual(path.read_bytes(),raw)
        grant=self.local/(self.submission.request_id+'.json')
        value=json.loads(grant.read_bytes()); value['payload']['role']='agent'
        self.remote.private(grant,canonical(value))
        with self.assertRaises(runtime.ExecutionDenied): self.authorization.verify(self.submission,self.snapshot,self.batch)

    def test_changed_software_or_deployment_approval_never_receives_grant(self):
        self.approve()
        for field in ('software_sha256','profile_sha256','approval_sha256'):
            args=dict(self.args,**{field:'0'*64})
            auth=AuthorAuthorization(self.local,**args)
            with self.subTest(field=field),patch('auto_lammps.authorization._capture') as capture,self.assertRaises(Conflict):
                auth.ensure(self.submission,self.snapshot,self.batch)
            capture.assert_not_called()

    def test_large_output_grant_uses_compact_static_report_and_real_installation(self):
        fixture=self.remote.package_fixture
        outputs=fixture.raw['invocations'][0]['output_files']; template=deepcopy(outputs[0])
        for index in range(100):
            row=deepcopy(template);row['path']='artifact-'+str(index)+'-'+'x'*175+'.csv';outputs.append(row)
        fixture.report=validate_author_plan(fixture.evidence,fixture.raw)
        compiled=fixture.compile();self.package=compiled['package'];self.digest=compiled['package_sha256']
        self.ledger=Ledger(self.root/'large-author-auth.sqlite')
        self.ledger.create_campaign('author-auth',Policy(10000000,32,604800,8*1024**3,1024**3,1,'a'*64))
        self.evaluation=self.ledger.register_evaluation('author-auth',task_sha256='1'*64,
            repetition=0,role='reference',system_sha256='3'*64)
        row=self.ledger.reserve(self.evaluation,'large-original',self.digest,Resources(**self.package['resources']))
        self.submission=Submission(row['id'],self.digest,Resources(**self.package['resources']))
        path=private_directory(self.root/'large-author-snapshot');self.remote.private(path/'author-package.json',canonical(self.package))
        self.snapshot=AuthorSnapshot(path,self.digest)
        environment=BatchEnvironment('cpu',None,self.endpoint.root_path,self.endpoint.python_path,
            str(self.fixture.config.parent/'runtime_launcher.py'),sha256(runtime_source()))
        self.batch=render_author_batch(self.submission,environment)
        self.authorization=AuthorAuthorization(self.local,**dict(self.args,ledger=self.ledger))
        self.approve()
        with patch('auto_lammps.authorization._capture',side_effect=self.install):self.assertTrue(self.ensure())
        envelope=(self.local/(self.submission.request_id+'.json')).read_bytes()
        bundle=json.loads((self.local/(self.submission.request_id+'.bundle.json')).read_bytes())
        self.assertGreater(len(envelope),16384);self.assertLess(len(envelope),1024**2)
        self.assertLess(len(canonical(bundle['report'])),8192)
        self.assertEqual(len(bundle['envelope']['payload']['outputs']),103)
        self.assertEqual(self.install_calls,1)


def runtime_source(): return Path(runtime.__file__).read_bytes()


if __name__=='__main__': unittest.main()
