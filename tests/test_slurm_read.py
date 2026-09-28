import base64
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from auto_lammps.slurm_read import SlurmReader, _capture, identity, interpret

REQUEST = 'a'*32
MANIFEST = 'b'*64
NAME, COMMENT = identity(REQUEST, MANIFEST)
SINCE = '2026-01-01T00:00:00'


def queue(state='RUNNING', job='123', comment=COMMENT):
    return f'{job}|{state}|{NAME}|{comment}\n'


def account(state='COMPLETED', job='123', comment=COMMENT, cores='8', elapsed='60', code='0:0', restarts='0'):
    return f'{job}|{state}|{NAME}|{comment}|{cores}|{elapsed}|{code}|{restarts}\n'


class ParserTests(unittest.TestCase):
    def test_empty_is_unknown_not_rejected(self):
        self.assertEqual(interpret('', '', REQUEST, MANIFEST).reason, 'not_visible')

    def test_complete_root_only_allocation_cost(self):
        result = interpret('', account() + account(job='123.batch') + account(job='123.extern'), REQUEST, MANIFEST)
        self.assertEqual((result.state, result.job_id, result.allocated_core_seconds), ('completed', '123', 480))

    def test_active_queue_and_lagging_accounting(self):
        result = interpret(queue(), account(state='PENDING'), REQUEST, MANIFEST)
        self.assertEqual((result.state, result.allocated_core_seconds), ('running', None))

    def test_timeout_cancellation_and_failure_remain_distinct(self):
        for raw, expected in [('TIMEOUT', 'timeout'), ('CANCELLED by 42', 'cancelled'), ('NODE_FAIL', 'failed')]:
            self.assertEqual(interpret('', account(state=raw, code='0:9'), REQUEST, MANIFEST).state, expected)

    def test_truncation_bad_identity_and_malformed_rows(self):
        for q, a in [(queue().rstrip('\n'), ''), ('', account(comment='')), (queue(job='123_1'), ''),
                     ('garbage\n', ''), ('', account(state='COMPLETED+')), ('', account(code='1:0'))]:
            with self.subTest(q=q, a=a):
                self.assertEqual(interpret(q, a, REQUEST, MANIFEST).state, 'unknown')

    def test_requeues_duplicates_and_conflicts_never_finalize(self):
        for q, a in [('', account(restarts='1')), ('', account()+account()), ('', account()+account(job='124')),
                     (queue(), account()), (queue(job='124'), account(state='RUNNING')),
                     ('', account(cores='0', elapsed='10', state='FAILED'))]:
            with self.subTest(q=q, a=a):
                result = interpret(q, a, REQUEST, MANIFEST)
                self.assertEqual(result.state, 'unknown')
                self.assertIsNone(result.allocated_core_seconds)

    def test_unallocated_pending_cancellation(self):
        result = interpret('', account(state='CANCELLED', cores='0', elapsed='0'), REQUEST, MANIFEST)
        self.assertEqual(result.allocated_core_seconds, 0)


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.reader = SlurmReader('test-login', self.root / 'audit')

    def fake_capture(self, argv, **kwargs):
        self.assertIn('StrictHostKeyChecking=yes', argv)
        self.assertIn('BatchMode=yes', argv)
        self.assertIn('ForwardAgent=no', argv)
        self.assertIn('PermitLocalCommand=no', argv)
        self.assertTrue(list((self.root / 'audit').glob('*/intent.json')))
        command = argv[-1]
        self.assertNotIn('sbatch', command)
        self.assertNotIn('scancel', command)
        output = queue() if 'squeue' in command else account(state='RUNNING')
        return dict(returncode=0, failure='', stdout=base64.b64encode(output.encode()).decode(), stderr='')

    def test_fixed_queries_and_durable_private_evidence(self):
        with patch('auto_lammps.slurm_read._capture', side_effect=self.fake_capture) as capture:
            result = self.reader.lookup(REQUEST, MANIFEST, since_utc=SINCE)
        self.assertEqual(capture.call_count, 2)
        self.assertEqual(result.state, 'running')
        self.assertEqual(len(result.evidence_sha256), 64)
        self.assertEqual(len(list((self.root / 'audit').glob('*/result.json'))), 2)
        self.assertEqual(len(list((self.root / 'audit').glob('*.json'))), 1)
        for path in (self.root / 'audit').rglob('*.json'):
            self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_failed_command_is_unknown_with_raw_receipt(self):
        failure = dict(returncode=255, failure='command_failed', stdout='', stderr=base64.b64encode(b'synthetic failure').decode())
        with patch('auto_lammps.slurm_read._capture', return_value=failure):
            result = self.reader.lookup(REQUEST, MANIFEST, since_utc=SINCE)
        self.assertEqual((result.state, result.reason), ('unknown', 'query_failed'))
        self.assertTrue(result.evidence_sha256)

    def test_audit_failure_blocks_network(self):
        with patch('auto_lammps.slurm_read._write_new', side_effect=OSError('disk full')):
            with patch('auto_lammps.slurm_read._capture') as capture, self.assertRaises(OSError):
                self.reader.lookup(REQUEST, MANIFEST, since_utc=SINCE)
            capture.assert_not_called()

    def test_invalid_identifiers_and_dates_block_network(self):
        with patch('auto_lammps.slurm_read._capture') as capture:
            for request, since in [('x;id', SINCE), (REQUEST, '2026-01-01;id'), (REQUEST, '2026-02-30T00:00:00')]:
                with self.assertRaises(ValueError):
                    self.reader.lookup(request, MANIFEST, since_utc=since)
            capture.assert_not_called()
        with self.assertRaises(ValueError):
            SlurmReader('-oProxyCommand=bad', self.root / 'audit')

    def test_real_subprocess_bounds_without_any_simulation(self):
        result = _capture([sys.executable, '-c', 'import time; time.sleep(5)'], timeout=.1, max_bytes=1024)
        self.assertEqual(result['failure'], 'timeout')
        result = _capture([sys.executable, '-c', 'print("x" * 10000)'], timeout=5, max_bytes=1024)
        self.assertEqual(result['failure'], 'output_limit')
        self.assertLessEqual(len(base64.b64decode(result['stdout'])), 1025)
        result = _capture([sys.executable, '-c', 'print("ok")'], timeout=5, max_bytes=1024)
        self.assertEqual(base64.b64decode(result['stdout']), b'ok\n')
        self.assertEqual(result['returncode'], 0)


if __name__ == '__main__':
    unittest.main()
