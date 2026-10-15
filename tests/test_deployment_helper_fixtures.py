"""Installed package paths are not synthetic remote deployment locations.

Full helper bytes are copied from a deliberately non-ASCII installed path.
File-only adapters run locally; no SSH, scheduler, compiler or engine runs.
"""
from contextlib import contextmanager, ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from auto_lammps import remote_stage, remote_submit, runtime_launcher, engine_build_worker
from auto_lammps.manifest import sha256
from auto_lammps.staging import StageEndpoint
import test_execution as execution
import test_engine_build as build
import test_staging as staging


class DeploymentHelperFixtureTests(unittest.TestCase):
    @contextmanager
    def installed_modules(self, *modules):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)/'中文安装目录'/'auto_lammps'
            root.mkdir(parents=True)
            originals = {}
            for module in modules:
                raw = Path(module.__file__).read_bytes()
                source = root/(module.__name__.rsplit('.', 1)[-1]+'.py')
                source.write_bytes(raw)
                source.chmod(0o400)
                originals[module] = raw
                stack.enter_context(patch.object(module, '__file__', str(source)))
            yield originals

    def assert_deployed(self, endpoint, expected):
        deployed = Path(endpoint.helper_path)
        self.assertTrue(str(deployed).isascii())
        self.assertEqual(deployed.read_bytes(), expected)
        self.assertEqual(endpoint.helper_sha256, sha256(expected))
        self.assertFalse(deployed.is_symlink())
        self.assertEqual(deployed.stat().st_nlink, 1)
        self.assertFalse(deployed.stat().st_mode & 0o022)

    def test_execution_deploys_all_helpers_outside_non_ascii_installation(self):
        with self.installed_modules(remote_stage, remote_submit, runtime_launcher) as originals:
            fixture = execution.ExecutionTests()
            fixture.setUp()
            self.addCleanup(fixture.doCleanups)
            self.assert_deployed(fixture.controller.staging.client.endpoint, originals[remote_stage])
            self.assert_deployed(fixture.controller.submission.scheduler.endpoint, originals[remote_submit])
            self.assertEqual(fixture.helper.read_bytes(), originals[runtime_launcher])
            fixture.test_prepared_candidate_through_real_adapters_to_report_once()

    def test_streaming_receiver_deploys_exact_bytes_and_retains_path_rejection(self):
        with self.installed_modules(remote_stage) as originals:
            fixture = staging.StagingTests()
            fixture.setUp()
            self.addCleanup(fixture.doCleanups)
            self.assert_deployed(fixture.endpoint, originals[remote_stage])
            with self.assertRaisesRegex(ValueError, 'safe absolute deployment path'):
                StageEndpoint('fixture-login', '/fixture/bin/python3', remote_stage.__file__,
                    sha256(originals[remote_stage]), str(fixture.remote))
            fixture.test_full_streaming_client_to_pinned_receiver_and_reserved_ledger()

    def test_build_metadata_deploys_worker_outside_non_ascii_installation(self):
        with self.installed_modules(engine_build_worker) as originals:
            fixture = build.BuildTests()
            fixture.setUp()
            self.addCleanup(fixture.doCleanups)
            self.assert_deployed(fixture.endpoint, originals[engine_build_worker])
            fixture.test_real_metadata_stage_and_unknown_submit_do_not_repeat()


if __name__ == '__main__':
    unittest.main()
