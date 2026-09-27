"""Synthetic scheduler evidence plus durable worker recovery; no target simulation."""
import base64
import json
import multiprocessing as mp
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from auto_lammps.ledger import Ledger, Policy, Resources, Conflict
from auto_lammps.monitoring import MonitoringService
from auto_lammps.reconciliation import ReconciliationService
from auto_lammps.slurm_read import SlurmReader, identity

HASH = 'b' * 64
REQUEST = 'a' * 32
NAME, COMMENT = identity(REQUEST, HASH)
SINCE = '2026-09-01T00:00:00'


def capture_rows(queue, account):
    def capture(argv, **kwargs):
        raw = queue if 'squeue' in argv[-1] else account
        return dict(returncode=0, failure='', stdout=base64.b64encode(raw.encode()).decode(), stderr='')
    return capture


def q(request=REQUEST, manifest=HASH, job='123'):
    name, comment = identity(request, manifest)
    return f'{job}|RUNNING|{name}|{comment}\n'


def a(request=REQUEST, state='RUNNING', job='123', comment='', restarts='0', code='0:0'):
    return f'{job}|{state}|al-{request}|{comment}|32|100|{code}|{restarts}\n'


def worker_once(path, request, audit):
    reader = SlurmReader('synthetic-login', audit, retain_queue_identity=True)
    service = MonitoringService(ReconciliationService(Ledger(Path(path)), reader), interval_seconds=15)
    with patch('auto_lammps.slurm_read._capture', side_effect=capture_rows(q(request=request), a(request=request))):
        service.advance(request)


class RetainedIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.reader = SlurmReader('synthetic-login', self.root/'audit', retain_queue_identity=True)

    def lookup(self, queue, account, reader=None):
        with patch('auto_lammps.slurm_read._capture', side_effect=capture_rows(queue, account)):
            return (reader or self.reader).lookup(REQUEST, HASH, since_utc=SINCE)

    def test_queue_identity_survives_reader_restart_and_queue_expiry(self):
        self.assertEqual(self.lookup(q(), a()).state, 'running')
        restarted = SlurmReader('synthetic-login', self.root/'audit', retain_queue_identity=True)
        result = self.lookup('', a(state='COMPLETED'), restarted)
        self.assertEqual((result.state, result.allocated_core_seconds), ('completed', 3200))
        receipts = [json.loads(p.read_text()) for p in (self.root/'audit').glob('*/result.json')]
        self.assertTrue(any('||32|' in base64.b64decode(r['stdout']).decode() for r in receipts))

    def test_no_queue_identity_means_no_inferred_binding(self):
        self.assertEqual(self.lookup('', a(state='COMPLETED')).reason, 'identity_unverified')
        self.assertFalse((self.root/'audit/identities'/f'{REQUEST}.json').exists())

    def test_default_remains_strict(self):
        strict = SlurmReader('synthetic-login', self.root/'strict')
        self.assertEqual(self.lookup(q(), a(), strict).reason, 'identity_unverified')

    def test_wrong_nonempty_comment_and_duplicates_and_restart_rejected(self):
        self.lookup(q(), a())
        for account in (a(state='COMPLETED', comment='wrong'), a(state='COMPLETED')*2,
                        a(state='COMPLETED', job='124'), a(state='COMPLETED', restarts='1'),
                        a(state='COMPLETED', code='1:0'), a(state='COMPLETED').rstrip('\n')):
            with self.subTest(account=account):
                result = self.lookup('', account)
                self.assertEqual(result.state, 'unknown')
                self.assertIsNone(result.allocated_core_seconds)

    def test_invalid_queue_cannot_create_binding(self):
        for queue in (q().replace(COMMENT, 'wrong'), q()*2, q().rstrip('\n')):
            self.assertEqual(self.lookup(queue, a()).state, 'unknown')
        self.assertFalse((self.root/'audit/identities'/f'{REQUEST}.json').exists())

    def test_modified_retained_receipt_cannot_finalize(self):
        self.lookup(q(), a())
        binding = json.loads((self.root/'audit/identities'/f'{REQUEST}.json').read_text())
        receipt = self.root/'audit'/binding['receipt_directory']/'result.json'
        receipt.write_text('{}')
        self.assertEqual(self.lookup('', a(state='COMPLETED')).state, 'unknown')

    def test_other_manifest_cannot_reuse_binding(self):
        self.lookup(q(), a())
        with patch('auto_lammps.slurm_read._capture', side_effect=capture_rows('', a(state='COMPLETED'))):
            result = self.reader.lookup(REQUEST, 'c'*64, since_utc=SINCE)
        self.assertEqual(result.state, 'unknown')


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = Ledger(self.root/'ledger.sqlite')
        self.ledger.create_campaign('test', Policy(100000, 32, 1000, 1024, 100000000, 1, HASH))
        self.evaluation = self.ledger.register_evaluation('test', task_sha256=HASH, repetition=0,
                                                         role='development', system_sha256=HASH)
        self.row = self.ledger.reserve(self.evaluation, 'first', HASH, Resources(32, 1000, 1024, 100))
        self.request = self.row['id']
        self.ledger.begin_dispatch(self.request)
        self.ledger.accepted(self.request, '123', {})
        self.reader = SlurmReader('synthetic-login', self.root/'audit', retain_queue_identity=True)
        self.service = MonitoringService(ReconciliationService(self.ledger, self.reader), interval_seconds=15)

    def advance(self, state='RUNNING', clock=1000):
        queue = q(request=self.request) if state=='RUNNING' else ''
        with patch('auto_lammps.slurm_read._capture', side_effect=capture_rows(queue, a(request=self.request,state=state))), \
                patch('auto_lammps.ledger.time.time', return_value=clock):
            return self.service.advance(self.request)

    def test_reopen_keeps_poll_delay_and_accounting_and_transition(self):
        self.assertEqual(self.advance()['state'], 'running')
        restarted = MonitoringService(ReconciliationService(Ledger(self.ledger.path), self.reader), interval_seconds=15)
        with patch('auto_lammps.slurm_read._capture') as capture, patch('auto_lammps.ledger.time.time', return_value=1001):
            self.assertEqual(restarted.advance(self.request)['reason'], 'poll_not_due')
            capture.assert_not_called()
        result = self.advance('COMPLETED', 1016)
        self.assertTrue(result['terminal'])
        self.assertEqual(self.ledger.get(self.request)['actual_core_seconds'], 3200)
        self.assertFalse(self.advance('COMPLETED', 1050)['changed'])
        self.assertEqual(self.ledger.summary('test')['dispatch_intents'], 1)
        self.assertEqual(sum(e['kind']=='accounting_final' for e in self.ledger.events(self.request)), 1)

    def test_connection_failure_backs_off_without_new_submission(self):
        self.advance()
        with patch('auto_lammps.slurm_read._capture', side_effect=OSError('temporary disconnect')), \
                patch('auto_lammps.ledger.time.time', return_value=1016):
            result = self.service.advance(self.request)
        self.assertFalse(result['terminal'])
        with patch('auto_lammps.slurm_read._capture') as capture, patch('auto_lammps.ledger.time.time', return_value=1032):
            self.assertEqual(self.service.advance(self.request)['reason'], 'poll_not_due')
            capture.assert_not_called()
        self.assertEqual(self.advance(clock=1047)['state'], 'running')
        self.assertEqual(self.ledger.summary('test')['dispatch_intents'], 1)

    def test_failure_terminal_is_not_success_and_duplicate_transition_suppressed(self):
        self.advance()
        result = self.advance('FAILED', 1016)
        self.assertEqual(result['state'], 'failed')
        self.assertTrue(result['terminal'])
        self.assertEqual(result['scientific_status'], 'not_evaluated')
        self.assertFalse(self.advance('FAILED', 1040)['changed'])

    def test_prepared_request_never_polled(self):
        self.advance('COMPLETED')  # No identity yet: remains unknown, same request reserved.
        with self.assertRaises(Conflict):
            self.ledger.reserve(self.evaluation, 'second', HASH, Resources(32, 1000, 1024, 100))
        self.assertEqual(self.ledger.summary('test')['dispatch_intents'], 1)

    def test_real_process_exit_and_restart_do_not_reset_timing(self):
        ctx = mp.get_context('spawn')
        worker = ctx.Process(target=worker_once, args=(str(self.ledger.path), self.request, str(self.root/'audit')))
        worker.start(); worker.join(15)
        if worker.is_alive(): worker.kill(); worker.join()
        self.assertEqual(worker.exitcode, 0)
        with patch('auto_lammps.slurm_read._capture') as capture:
            self.assertEqual(self.service.advance(self.request)['reason'], 'poll_not_due')
            capture.assert_not_called()
        self.assertEqual(sum(e['kind']=='monitor_poll' for e in self.ledger.events(self.request)), 1)

    def test_changed_policy_rejected_and_stop_does_not_cancel(self):
        self.advance()
        other = MonitoringService(ReconciliationService(self.ledger,self.reader),interval_seconds=30)
        with self.assertRaises(Conflict): other.advance(self.request)
        stop=threading.Event();stop.set()
        self.assertEqual(self.service.run(self.request,stop=stop)['state'],'stopped')
        self.assertEqual(self.ledger.get(self.request)['state'],'running')

    def test_two_processes_share_one_poll_and_monitor_projection_is_safe(self):
        ctx = mp.get_context('spawn')
        workers = [ctx.Process(target=worker_once, args=(str(self.ledger.path), self.request, str(self.root/'audit'))) for _ in range(2)]
        for worker in workers: worker.start()
        for worker in workers:
            worker.join(15)
            if worker.is_alive(): worker.kill(); worker.join()
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual(sum(e['kind']=='monitor_poll' for e in self.ledger.events(self.request)),1)
        snapshot = self.ledger.evaluation_snapshot(self.evaluation)
        self.assertEqual(snapshot['dispatch_claims'],1)
        self.assertIsNotNone(snapshot['requests'][0]['monitoring']['last_checked'])
        self.assertNotIn(str(self.root),json.dumps(snapshot))

    def test_genuinely_prepared_job_cannot_register_a_monitor(self):
        self.ledger.create_campaign('other', Policy(100000,32,1000,1024,100000000,1,HASH))
        ev=self.ledger.register_evaluation('other',task_sha256=HASH,repetition=0,role='development',system_sha256=HASH)
        request=self.ledger.reserve(ev,'first',HASH,Resources(32,1000,1024,100))
        with patch('auto_lammps.slurm_read._capture') as capture, self.assertRaises(Conflict):
            self.service.advance(request['id'])
        capture.assert_not_called()
        self.assertEqual(self.ledger.get(request['id'])['dispatch_claimed'],0)
