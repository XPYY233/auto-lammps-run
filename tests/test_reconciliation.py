"""Synthetic scheduler-to-ledger recovery; no network, engine or model calls."""
from datetime import datetime, timezone
from contextlib import redirect_stdout
from io import StringIO
import json
import multiprocessing as mp
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from auto_lammps.ledger import Conflict, Ledger, Policy, Resources
from auto_lammps.reconciliation import ReconciliationService
from auto_lammps.slurm_read import Observation
from auto_lammps.submission import SubmissionService

HASH = 'a'*64
OTHER = 'b'*64
RESOURCES = Resources(2, 10, 100, 100)
POLICY = Policy(1000, 8, 100, 1024, 10000, 3, HASH)


class Reader:
    def __init__(self, observation):
        self.observation, self.calls = observation, []

    def lookup(self, request, manifest, *, since_utc):
        self.calls.append((request, manifest, since_utc))
        return self.observation


def apply_worker(path, request, ticket, barrier, replies):
    ledger = Ledger(Path(path))
    barrier.wait(timeout=10)
    try:
        row = ledger.apply_reconciliation(request, ticket, state='completed', job_id='123',
                                          core_seconds=8, reason='', evidence_sha256=HASH)
        replies.put(row['actual_core_seconds'])
    except Exception as exc:
        replies.put(type(exc).__name__)


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'ledger.sqlite'
        self.ledger = Ledger(self.path)
        self.ledger.create_campaign('test', POLICY)
        self.evaluation = self.register(0)
        self.row = self.ledger.reserve(self.evaluation, 'first', HASH, RESOURCES)
        self.id = self.row['id']
        self.ledger.begin_dispatch(self.id)

    def register(self, repetition):
        return self.ledger.register_evaluation('test', task_sha256=HASH, repetition=repetition,
                                                role='development', system_sha256=HASH)

    def refresh(self, state='completed', job='123', cost=8, reason='', proof=HASH):
        reader = Reader(Observation(state, job, cost, reason, proof))
        return ReconciliationService(self.ledger, reader).refresh(self.id)

    def apply(self, ticket, state='completed', job='123', cost=8, proof=HASH):
        return self.ledger.apply_reconciliation(self.id, ticket, state=state, job_id=job,
                                                core_seconds=cost, reason='', evidence_sha256=proof)

    def test_lost_acceptance_receipt_recovers_once_after_restart(self):
        self.ledger.uncertain(self.id, {'synthetic': True})
        self.ledger = Ledger(self.path)
        reader = Reader(Observation('completed', '123', 8, evidence_sha256=HASH))
        recovered = ReconciliationService(self.ledger, reader).recover_once()
        self.assertEqual((recovered[0]['job_id'], recovered[0]['actual_core_seconds']), ('123', 8))
        intent = next(e for e in self.ledger.events(self.id) if e['kind'] == 'dispatch_intent')
        expected = datetime.fromtimestamp(intent['at'] - 60, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
        self.assertEqual(reader.calls, [(self.id, HASH, expected)])
        class NeverSubmit:
            def submit(self, request):
                raise AssertionError('Recovery must not submit')
        row = SubmissionService(self.ledger, NeverSubmit()).submit(self.evaluation, 'first', HASH, RESOURCES)
        self.assertEqual(row['state'], 'completed')
        self.assertEqual(self.ledger.summary('test')['dispatch_intents'], 1)

    def test_terminal_without_accounting_remains_recoverable(self):
        self.ledger.accepted(self.id, '123', {})
        self.ledger.observe(self.id, '123', 'completed', {})
        self.assertIn(self.id, [r['id'] for r in self.ledger.recoverable()])
        reader = Reader(Observation('completed', '123', 8, evidence_sha256=HASH))
        ReconciliationService(Ledger(self.path), reader).recover_once()
        self.assertNotIn(self.id, [r['id'] for r in self.ledger.recoverable()])

    def test_missing_query_keeps_budget_and_attempt(self):
        row = self.refresh('unknown', None, None, 'not_visible')
        self.assertEqual((row['state'], row['charge_core_seconds'], row['dispatch_claimed']), ('unknown', 20, 1))
        with self.assertRaises(Conflict):
            self.ledger.reserve(self.evaluation, 'second', HASH, RESOURCES)

    def test_older_query_cannot_overwrite_newer_completion(self):
        old = self.ledger.begin_reconciliation(self.id)['ticket']
        new = self.ledger.begin_reconciliation(self.id)['ticket']
        self.apply(new)
        self.apply(old, state='running', cost=None)
        row = self.ledger.get(self.id)
        self.assertEqual((row['state'], row['charge_core_seconds']), ('completed', 8))
        last = json.loads(self.ledger.events(self.id)[-1]['payload'])
        self.assertEqual(last['outcome'], 'stale')

    def test_intervening_cancel_and_pending_query_do_not_erase_cancel(self):
        self.ledger.accepted(self.id, '123', {})
        ticket = self.ledger.begin_reconciliation(self.id)['ticket']
        self.ledger.cancel_intent(self.id)
        self.apply(ticket, state='running', cost=None)
        self.assertEqual(self.ledger.get(self.id)['state'], 'cancelling')
        self.refresh('running', cost=None)
        self.assertEqual(self.ledger.get(self.id)['state'], 'cancelling')
        self.refresh('cancelled', cost=4)
        self.assertEqual(self.ledger.get(self.id)['actual_core_seconds'], 4)

    def test_new_receipt_same_final_usage_is_valid_without_reaccounting(self):
        self.refresh()
        self.refresh(proof=OTHER)
        events = self.ledger.events(self.id)
        self.assertEqual(sum(e['kind'] == 'accounting_final' for e in events), 1)
        self.assertEqual(sum(e['kind'] == 'reconciliation_finished' for e in events), 2)

    def test_changed_final_usage_quarantines_campaign_preserving_history(self):
        self.refresh()
        row = self.refresh(cost=9, proof=OTHER)
        self.assertEqual((row['state'], row['actual_core_seconds']), ('reconcile_required', 8))
        charge = row['charge_core_seconds']
        self.refresh(cost=9)
        self.assertEqual(self.ledger.get(self.id)['charge_core_seconds'], charge)
        with self.assertRaises(Conflict):
            self.ledger.reserve(self.register(1), 'new', HASH, RESOURCES)

    def test_restarted_job_blocks_previously_prepared_other_request(self):
        other = self.ledger.reserve(self.register(1), 'prepared', HASH, RESOURCES)
        row = self.refresh('unknown', '123', None, 'restarted_allocation')
        self.assertEqual(row['state'], 'reconcile_required')
        with self.assertRaises(Conflict):
            self.ledger.begin_dispatch(other['id'])

    def test_different_job_and_terminal_state_conflict(self):
        self.refresh()
        self.assertEqual(self.refresh(job='124')['state'], 'reconcile_required')

    def test_conflict_cancellation_cannot_reopen_campaign(self):
        self.refresh()
        self.refresh('running', cost=None)
        self.assertEqual(self.ledger.get(self.id)['state'], 'reconcile_required')
        self.ledger.cancel_intent(self.id)
        self.assertEqual(self.ledger.get(self.id)['state'], 'reconcile_required')
        with self.assertRaises(Conflict):
            self.ledger.reserve(self.register(1), 'another', HASH, RESOURCES)

    def test_final_overrun_is_recorded_without_clamping(self):
        row = self.refresh(cost=1200)
        self.assertEqual(row['actual_core_seconds'], 1200)
        self.assertEqual(self.ledger.summary('test')['overrun_core_seconds'], 200)

    def test_reader_failure_keeps_intent_recoverable(self):
        reader = Reader(None)
        with patch.object(reader, 'lookup', side_effect=OSError('synthetic transport error')):
            with self.assertRaises(OSError):
                ReconciliationService(self.ledger, reader).refresh(self.id)
        self.assertEqual(self.ledger.get(self.id)['charge_core_seconds'], 20)
        self.assertEqual(self.ledger.events(self.id)[-1]['kind'], 'reconciliation_started')
        self.refresh()
        self.assertEqual(self.ledger.get(self.id)['actual_core_seconds'], 8)

    def test_raw_scheduler_receipt_through_reader_to_reopened_ledger(self):
        import base64
        from auto_lammps.slurm_read import SlurmReader, identity
        name, comment = identity(self.id, HASH)
        def capture(argv, **kwargs):
            stdout = '' if 'squeue' in argv[-1] else f'123|COMPLETED|{name}|{comment}|2|4|0:0|0\n'
            return dict(returncode=0, failure='', stdout=base64.b64encode(stdout.encode()).decode(), stderr='')
        reader = SlurmReader('synthetic-login', self.path.parent / 'audit')
        with patch('auto_lammps.slurm_read._capture', side_effect=capture):
            rows = ReconciliationService(Ledger(self.path), reader).recover_once()
        self.assertEqual((rows[0]['state'], rows[0]['actual_core_seconds']), ('completed', 8))
        self.assertEqual(len(list((self.path.parent / 'audit').glob('*/result.json'))), 2)
        proof = next(e for e in self.ledger.events(self.id) if e['kind'] == 'accounting_final')
        digest = json.loads(proof['payload'])['evidence_sha256']
        import hashlib
        self.assertTrue(any(hashlib.sha256(p.read_bytes()).hexdigest() == digest
                            for p in (self.path.parent / 'audit').glob('*.json')))

    def test_admin_command_recovers_existing_ledger_using_reader(self):
        from auto_lammps.worker import main
        reader = Reader(Observation('completed', '123', 8, evidence_sha256=HASH))
        output = StringIO()
        with patch('auto_lammps.worker.SlurmReader', return_value=reader), redirect_stdout(output):
            result = main(['--ledger', str(self.path), '--ssh-alias', 'synthetic-login',
                           '--audit-directory', str(self.path.parent / 'audit')])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())['requests'][0]['state'], 'completed')
        self.assertEqual(self.ledger.get(self.id)['actual_core_seconds'], 8)

    def test_transaction_failure_rolls_back_binding_status_and_accounting(self):
        ticket = self.ledger.begin_reconciliation(self.id)['ticket']
        original = self.ledger._event
        def fail(db, request, kind, payload):
            if kind == 'accounting_final':
                raise OSError('synthetic disk failure')
            return original(db, request, kind, payload)
        with patch.object(self.ledger, '_event', side_effect=fail), self.assertRaises(OSError):
            self.apply(ticket)
        row = self.ledger.get(self.id)
        self.assertEqual((row['state'], row['job_id'], row['accounted']), ('dispatching', None, 0))
        self.apply(ticket)
        self.assertEqual(self.ledger.get(self.id)['actual_core_seconds'], 8)

    def test_duplicate_ticket_is_idempotent_and_cannot_replace_proof(self):
        ticket = self.ledger.begin_reconciliation(self.id)['ticket']
        self.apply(ticket)
        count = len(self.ledger.events(self.id))
        self.apply(ticket)
        self.assertEqual(len(self.ledger.events(self.id)), count)
        with self.assertRaises(Conflict):
            self.apply(ticket, proof=OTHER)

    def test_prepared_is_never_dispatched_by_recovery(self):
        self.refresh()
        other = self.ledger.reserve(self.register(1), 'prepared', HASH, RESOURCES)
        reader = Reader(Observation('unknown', reason='not_visible', evidence_sha256=HASH))
        self.assertEqual(ReconciliationService(self.ledger, reader).recover_once(), [])
        self.assertEqual(reader.calls, [])
        self.assertEqual(self.ledger.get(other['id'])['dispatch_claimed'], 0)
        with self.assertRaises(Conflict):
            self.ledger.begin_reconciliation(other['id'])

    def test_ticket_from_other_request_is_rejected(self):
        other = self.ledger.reserve(self.register(1), 'prepared', HASH, RESOURCES)
        self.ledger.begin_dispatch(other['id'])
        ticket = self.ledger.begin_reconciliation(other['id'])['ticket']
        with self.assertRaises(Conflict):
            self.apply(ticket)

    def test_parallel_workers_apply_one_final_receipt_once(self):
        ticket = self.ledger.begin_reconciliation(self.id)['ticket']
        context = mp.get_context('spawn')
        barrier, replies = context.Barrier(2), context.Queue()
        workers = [context.Process(target=apply_worker, args=(str(self.path), self.id, ticket, barrier, replies)) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(15)
            if worker.is_alive():
                worker.kill()
                worker.join()
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual([replies.get(timeout=2) for _ in workers], [8, 8])
        self.assertEqual(sum(e['kind'] == 'accounting_final' for e in self.ledger.events(self.id)), 1)
        replies.close()
        replies.join_thread()


if __name__ == '__main__':
    unittest.main()
