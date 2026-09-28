"""Synthetic scheduler acceptance tests. No SSH, Slurm or physics invocation."""
import base64
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_runtime_launcher as fixtures
from auto_lammps import remote_submit, runtime_launcher as runtime
from auto_lammps.ledger import Conflict, Ledger, Policy
from auto_lammps.slurm_submit import SlurmSubmitter
from auto_lammps.staging import StageEndpoint
from auto_lammps.submission import Submission, SubmissionService


class RemoteSubmitTests(unittest.TestCase):
    private = fixtures.RuntimeTests.private

    def setUp(self):
        fixtures.RuntimeTests.setUp(self)
        self.launcher = self.root/'runtime_launcher.py'
        self.launcher.write_bytes(Path(runtime.__file__).read_bytes())
        self.sbatch = self.root/'scheduler-placeholder'
        self.sbatch.write_bytes(b'not an executable; never invoked')
        self.script = b'#!/bin/sh\n# Synthetic, not executed\n'
        self.private(self.control/(fixtures.REQUEST+'.sh'), self.script)
        self.payload['batch_sha256'] = runtime.digest(self.script)
        self.private(self.control/(fixtures.REQUEST+'.json'), runtime.canonical(fixtures.signed(self.payload)))
        self.config_path = self.root/'submission.json'
        self.config = dict(runtime_path=str(self.launcher), runtime_sha256=runtime.digest(self.launcher.read_bytes()),
                           sbatch_path=str(self.sbatch), sbatch_sha256=runtime.digest(self.sbatch.read_bytes()))
        self.private(self.config_path, runtime.canonical(self.config))

    def invoke(self):
        with patch.object(remote_submit.sys, 'platform', 'linux'):
            return remote_submit.submit(self.config_path, fixtures.REQUEST, self.digest, self.profile['requests_root'])

    def result(self, value=b'123\n', code=0, failure=''):
        return dict(returncode=code, failure=failure, stdout=base64.b64encode(value).decode(), stderr='')

    def test_one_acceptance_repeated_request_returns_saved_receipt(self):
        with patch.object(remote_submit, 'capture_sbatch', return_value=self.result()) as submit:
            first, second = self.invoke(), self.invoke()
        self.assertEqual(first, second)
        self.assertEqual(first['job_id'], '123')
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(submit.call_args.args[0], [str(self.sbatch), '--parsable', '--export=NIL', str(self.case/'job.sh')])
        self.assertEqual((self.case/'job.sh').read_bytes(), self.script)

    def test_ambiguous_acceptance_is_never_retried(self):
        with patch.object(remote_submit, 'capture_sbatch', return_value=self.result(failure='timeout')) as submit:
            self.assertEqual(self.invoke()['state'], 'unknown')
            self.assertEqual(self.invoke()['state'], 'unknown')
            self.assertEqual(submit.call_count, 1)

    def test_crash_after_intent_leaves_no_resubmit_route(self):
        with patch.object(remote_submit, 'capture_sbatch', side_effect=KeyboardInterrupt) as submit:
            with self.assertRaises(KeyboardInterrupt): self.invoke()
            self.assertTrue((self.case/'scheduler-intent.json').exists())
            self.assertFalse((self.case/'scheduler-result.json').exists())
            self.assertEqual(self.invoke()['state'], 'unknown')
            self.assertEqual(submit.call_count, 1)

    def test_tampered_batch_and_runtime_do_not_reach_scheduler(self):
        for target in (self.control/(fixtures.REQUEST+'.sh'), self.launcher):
            original = target.read_bytes()
            target.write_bytes(b'changed')
            with patch.object(remote_submit, 'capture_sbatch') as submit:
                with self.assertRaises(ValueError): self.invoke()
                submit.assert_not_called()
            target.write_bytes(original)

    def test_missing_private_grant_blocks_scheduler(self):
        (self.control/(fixtures.REQUEST+'.json')).unlink()
        with patch.object(remote_submit, 'capture_sbatch') as submit:
            with self.assertRaises(FileNotFoundError): self.invoke()
            submit.assert_not_called()

    def test_wrong_stage_and_existing_script_block_scheduler(self):
        (self.case/'job.sh').write_bytes(b'not the approved script')
        with patch.object(remote_submit, 'capture_sbatch') as submit:
            with self.assertRaises(ValueError): self.invoke()
            submit.assert_not_called()

    def test_bounded_capture_only_synthetic_output(self):
        # Actual process-control test; the child emits bytes, never loads LAMMPS.
        reply = remote_submit.capture_sbatch([sys.executable, '-c', 'print("123")'], self.root)
        self.assertEqual(base64.b64decode(reply['stdout']), b'123\n')
        self.assertEqual(reply['returncode'], 0)
        reply = remote_submit.capture_sbatch([sys.executable, '-c', 'print("x"*100000)'], self.root, limit=1024)
        self.assertEqual(reply['failure'], 'output_limit')
        self.assertLessEqual(len(base64.b64decode(reply['stdout'])), 1025)


class SubmitAdapterTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.ledger = Ledger(self.root/'ledger.sqlite')
        self.ledger.create_campaign('test', Policy(1000, 8, 120, 128*1024*1024, 4*1024*1024, 1, fixtures.H))
        self.evaluation = self.ledger.register_evaluation('test', task_sha256=fixtures.H, repetition=0,
                                                         role='agent', system_sha256=fixtures.H)
        self.row = self.ledger.reserve(self.evaluation, 'one', fixtures.H, fixtures.RESOURCES)
        self.endpoint = StageEndpoint('synthetic-login', '/usr/bin/python3', '/approved/remote_submit.py',
                                       fixtures.H, '/approved/requests')
        self.adapter = SlurmSubmitter(self.ledger, self.endpoint, self.root/'audit')
        self.service = SubmissionService(self.ledger, self.adapter)
        self.submission = Submission(self.row['id'], fixtures.H, fixtures.RESOURCES)

    def staged(self):
        self.ledger.begin_staging(self.row['id'])
        self.ledger.staging_result(self.row['id'], evidence_sha256=fixtures.H)

    def invoke(self):
        return self.service.submit(self.evaluation, 'one', fixtures.H, fixtures.RESOURCES)

    def response(self, **overrides):
        receipt = dict(state='accepted', job_id='123', request_id=self.row['id'],
                       manifest_sha256=fixtures.H, evidence_sha256=fixtures.H)
        receipt.update(overrides)
        return dict(returncode=0, failure='', stdout=base64.b64encode(json.dumps(receipt).encode()).decode(), stderr='')

    def test_missing_staging_does_not_consume_dispatch_or_call_network(self):
        with patch('auto_lammps.slurm_submit._capture') as capture:
            with self.assertRaises(Conflict): self.invoke()
            capture.assert_not_called()
        row = self.ledger.get(self.row['id'])
        self.assertEqual(row['state'], 'prepared')
        self.assertEqual(row['dispatch_claimed'], 0)

    def test_accepted_receipt_links_private_evidence_and_repeats_never_dispatch(self):
        self.staged()
        with patch('auto_lammps.slurm_submit._capture', return_value=self.response()) as capture:
            self.assertEqual(self.invoke()['job_id'], '123')
            self.assertEqual(self.invoke()['job_id'], '123')
            self.assertEqual(capture.call_count, 1)
        events = self.ledger.events(self.row['id'])
        self.assertTrue(any('evidence_sha256' in event['payload'] for event in events if event['kind']=='scheduler_accepted'))
        intent = json.loads((self.root/'audit'/self.row['id']/'intent.json').read_text())
        self.assertIn('StrictHostKeyChecking=yes', intent['argv'])
        self.assertNotIn('shell', intent)

    def test_wrong_receipt_stays_unknown_and_is_not_retried(self):
        self.staged()
        with patch('auto_lammps.slurm_submit._capture', return_value=self.response(request_id='f'*32)) as capture:
            self.assertEqual(self.invoke()['state'], 'unknown')
            self.assertEqual(self.invoke()['state'], 'unknown')
            self.assertEqual(capture.call_count, 1)

    def test_direct_adapter_requires_ledger_dispatch_intent(self):
        self.staged()
        with patch('auto_lammps.slurm_submit._capture') as capture:
            with self.assertRaises(Conflict): self.adapter.submit(self.submission)
            capture.assert_not_called()

    def test_audit_write_failure_prevents_network_call(self):
        self.staged()
        with patch('auto_lammps.slurm_submit._write_new', side_effect=OSError), patch('auto_lammps.slurm_submit._capture') as capture:
            self.assertEqual(self.invoke()['state'], 'unknown')
            capture.assert_not_called()
