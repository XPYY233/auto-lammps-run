"""Local HTTP and process tests; no model, scheduler, or simulation."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

spec = importlib.util.spec_from_file_location('local_launcher', Path(__file__).parents[1] / 'scripts/launch_local.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class DesktopLauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = dict(python=sys.executable, args=['--port', '8787'], port=8787,
                           state_directory=self.tmp.name, release='synthetic',
                           asset_sha256={x: hashlib.sha256(x.encode()).hexdigest() for x in ('app.js', 'app.css')})

    def test_existing_match_never_starts_another_process(self):
        with patch.object(launcher, 'matching_service', return_value=True), patch.object(launcher.subprocess, 'Popen') as process:
            result = launcher.launch(self.config, False)
        process.assert_not_called()
        self.assertFalse(result['started'])
        self.assertEqual(json.loads((Path(self.tmp.name) / 'last-launch.json').read_text())['release'], 'synthetic')

    def test_foreign_or_old_service_is_not_killed_or_replaced(self):
        with patch.object(launcher, 'matching_service', return_value=False), patch.object(launcher, 'occupied', return_value=True), patch.object(launcher.subprocess, 'Popen') as process:
            with self.assertRaises(launcher.LaunchError): launcher.launch(self.config, False)
        process.assert_not_called()

    def test_stopped_service_uses_exact_runtime_and_private_configuration(self):
        with patch.object(launcher, 'matching_service', side_effect=[False, True]), patch.object(launcher, 'occupied', return_value=False), patch.object(launcher.subprocess, 'Popen', return_value=Mock()) as process:
            result = launcher.launch(self.config, False)
        self.assertTrue(result['started'])
        self.assertEqual(process.call_args.args[0], [sys.executable, '-I', '-m', 'auto_lammps.web', '--port', '8787'])
        self.assertTrue(process.call_args.kwargs['start_new_session'])

    def test_mismatched_port_configuration_rejected(self):
        path = Path(self.tmp.name) / 'config.json'
        self.config['args'] = ['--port', '8785']
        path.write_text(json.dumps(self.config))
        with self.assertRaises(launcher.LaunchError): launcher.read_config(path)
