"""停止器：身份判据与"宁可不关也不误杀"的离线测试。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import stop_local


CONFIG = {'port': 8799, 'python': '/opt/example/bin/python', 'data_directory': '/private/example-app',
          'args': ['--data-directory', '/private/example-app', '--port', '8799'],
          'release': 'test-release', 'state_directory': ''}


class StopperTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.state=Path(self.temp.name)
        self.config=dict(CONFIG, state_directory=str(self.state))

    @staticmethod
    def free_sequence(sequence):
        counter = {'index': 0}

        def free(port):
            index = counter['index']
            counter['index'] += 1
            return sequence[min(index, len(sequence) - 1)]
        return free

    def run_stop(self, *, free, pid, command, port_free_after=True):
        sequence = [True] if free else [False, port_free_after, port_free_after, port_free_after]
        with mock.patch.object(stop_local, 'port_is_free', side_effect=self.free_sequence(sequence)), \
             mock.patch.object(stop_local, 'listening_pid', return_value=([pid] if not free else [])), \
             mock.patch.object(stop_local, 'command_of', return_value=command), \
             mock.patch('subprocess.run') as runner, \
             mock.patch.object(stop_local.os, 'kill') as killer, \
             mock.patch.object(stop_local.sys, 'argv', ['stop_local.py', '--config', '/tmp/current.json']), \
             mock.patch.object(stop_local, 'read_config', return_value=self.config):
            runner.return_value = mock.Mock(stdout='/opt/example/bin/python\n', returncode=0)
            return stop_local.main(), killer, json.loads((self.state/'last-stop.json').read_text())

    def test_refuses_to_kill_a_mismatched_service(self):
        code, killer, receipt = self.run_stop(free=False, pid=4321,
            command='/usr/bin/python -m other.service --data-directory /somewhere/else --port 8799')
        self.assertEqual(code, 1)
        killer.assert_not_called()
        self.assertEqual(receipt['reason'], 'identity_mismatch')

    def test_stops_the_matching_service_and_records_what_it_kept(self):
        code, killer, receipt = self.run_stop(free=False, pid=4321,
            command='/opt/example/bin/python -m auto_lammps.web --data-directory /private/example-app --port 8799')
        self.assertEqual(code, 0)
        killer.assert_called_once()
        self.assertTrue(receipt['stopped'])
        self.assertEqual(receipt['terminated'], ['4321'])
        self.assertIn('ledger', receipt['data_kept'])
        self.assertEqual(receipt['remote_jobs'], 'untouched')

    def test_reports_when_nothing_is_running(self):
        code, killer, receipt = self.run_stop(free=True, pid=None, command='')
        self.assertEqual(code, 0)
        killer.assert_not_called()
        self.assertFalse(receipt['stopped'])
        self.assertEqual(receipt['reason'], 'not_running')


if __name__ == '__main__':
    unittest.main()
