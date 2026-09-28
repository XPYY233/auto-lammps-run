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

OWNED = Path('/opt/example-runtime/lib/python3.14/site-packages/auto_lammps/__init__.py')


class DesktopLauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = dict(python=sys.executable, args=['--port', '8787'], port=8787,
                           state_directory=self.tmp.name, release='synthetic',
                           asset_sha256={x: hashlib.sha256(x.encode()).hexdigest() for x in ('app.js', 'app.css')})

    def installed(self):
        """Skip the interpreter probe in contract tests; the probe has its own tests below."""
        return patch.object(launcher, 'verify_installation', return_value=OWNED.parent)

    def test_existing_match_never_starts_another_process(self):
        with self.installed(), patch.object(launcher, 'matching_service', return_value=True), patch.object(launcher.subprocess, 'Popen') as process:
            result = launcher.launch(self.config, False)
        process.assert_not_called()
        self.assertFalse(result['started'])
        self.assertEqual(json.loads((Path(self.tmp.name) / 'last-launch.json').read_text())['release'], 'synthetic')

    def test_the_receipt_names_the_interpreter_and_the_package_it_owns(self):
        with self.installed(), patch.object(launcher, 'matching_service', return_value=True):
            result = launcher.launch(self.config, False)
        self.assertEqual(result['package'], str(OWNED.parent))
        self.assertEqual(result['interpreter'], sys.executable)

    def test_foreign_or_old_service_is_not_killed_or_replaced(self):
        with self.installed(), patch.object(launcher, 'matching_service', return_value=False), patch.object(launcher, 'occupied', return_value=True), patch.object(launcher.subprocess, 'Popen') as process:
            with self.assertRaises(launcher.LaunchError): launcher.launch(self.config, False)
        process.assert_not_called()

    def test_a_service_serving_another_copy_of_the_application_is_reported(self):
        with self.installed(), patch.object(launcher, 'matching_service', return_value=False), \
             patch.object(launcher, 'occupied', return_value=True), \
             patch.object(launcher, 'served_installation', return_value='/elsewhere/auto_lammps'), \
             patch.object(launcher.subprocess, 'Popen') as process:
            with self.assertRaises(launcher.LaunchError) as raised:
                launcher.launch(self.config, False)
        self.assertIn('/elsewhere/auto_lammps', str(raised.exception))
        self.assertIn(str(OWNED.parent), str(raised.exception))
        process.assert_not_called()

    def test_stopped_service_uses_exact_runtime_and_private_configuration(self):
        with self.installed(), patch.object(launcher, 'matching_service', side_effect=[False, True]), patch.object(launcher, 'occupied', return_value=False), patch.object(launcher.subprocess, 'Popen', return_value=Mock()) as process:
            result = launcher.launch(self.config, False)
        self.assertTrue(result['started'])
        self.assertEqual(process.call_args.args[0], [sys.executable, '-I', '-m', 'auto_lammps.web', '--port', '8787'])
        self.assertTrue(process.call_args.kwargs['start_new_session'])

    def test_mismatched_port_configuration_rejected(self):
        path = Path(self.tmp.name) / 'config.json'
        self.config['args'] = ['--port', '8785']
        path.write_text(json.dumps(self.config))
        with self.assertRaises(launcher.LaunchError): launcher.read_config(path)


class InstallationCheckTests(unittest.TestCase):
    """The configured interpreter must load the application it claims to serve."""

    def setUp(self):
        self.config = dict(python=sys.executable, state_directory=tempfile.gettempdir(), port=8798)

    def test_a_package_outside_the_configured_environment_is_refused(self):
        with patch.object(launcher, 'installed_package', return_value=Path('/opt/example-checkout/auto_lammps/__init__.py')), \
             patch.object(launcher, 'interpreter_environment', return_value=[Path('/opt/example-runtime/lib/python3.14/site-packages')]):
            with self.assertRaises(launcher.LaunchError) as raised:
                launcher.verify_installation(self.config)
        self.assertIn('不在该环境内', str(raised.exception))

    def test_a_package_inside_the_configured_environment_is_accepted(self):
        with patch.object(launcher, 'installed_package', return_value=OWNED), \
             patch.object(launcher, 'interpreter_environment', return_value=[Path('/opt/example-runtime/lib/python3.14/site-packages')]):
            self.assertEqual(launcher.verify_installation(self.config), OWNED)

    def test_an_interpreter_that_cannot_report_its_paths_stops_the_launch(self):
        with patch.object(launcher, 'installed_package', return_value=OWNED), \
             patch.object(launcher.subprocess, 'run', return_value=Mock(returncode=1, stderr='boom')):
            with self.assertRaises(launcher.LaunchError):
                launcher.verify_installation(self.config)

    def test_a_service_that_reports_nothing_is_tolerated(self):
        broken = Mock()
        broken.open.side_effect = OSError('no service here')
        with patch.object(launcher.urllib.request, 'build_opener', return_value=broken):
            self.assertEqual(launcher.served_installation(self.config), '')
