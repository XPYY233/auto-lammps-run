"""Synthetic HPC-to-HPC original-byte staging, never a real author source/run."""
from copy import deepcopy
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import struct
import unittest
from unittest.mock import patch

from auto_lammps import remote_stage, runtime_launcher
from auto_lammps.manifest import canonical, sha256
import test_author_execution_package as package_fixtures


class AuthorStageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = package_fixtures.AuthorExecutionPackageTests()
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.base = self.fixture.fixture.fixture.root.resolve()
        self.original = self.base/'synthetic-hpc-originals'; self.original.mkdir(mode=0o700)
        for name, contents in self.fixture.fixture.reader.texts.items():
            path = self.original/name; path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_bytes(contents); path.chmod(0o400)
        self.requests = self.base/'synthetic-hpc-requests'; self.requests.mkdir(mode=0o700)
        self.deployment_root = self.base/'synthetic-deployment'; self.deployment_root.mkdir(mode=0o700)
        self.runtime_raw = Path(runtime_launcher.__file__).read_bytes()
        helper = self.deployment_root/'runtime_launcher.py'; helper.write_bytes(self.runtime_raw); helper.chmod(0o400)
        self.profile = self.fixture.runtime_profile()
        self.profile.update(requests_root=str(self.requests), control_root=str(self.base/'synthetic-control'),
                            author_reference_runtime_sha256=sha256(self.runtime_raw))
        self.profile['author_reference']['source_roots'][0]['path'] = str(self.original)
        self.profile_path = self.deployment_root/'runtime.json'
        self.rebind()
        self.request_id = 'e'*32
        self.policy(2*self.fixture.resources.storage_bytes + remote_stage.ROOT_ALLOWANCE)

    def rebind(self):
        raw = canonical(self.profile)
        if self.profile_path.exists(): self.profile_path.chmod(0o600)
        self.profile_path.write_bytes(raw); self.profile_path.chmod(0o400)
        self.fixture.deployment = dict(deepcopy(self.profile['author_reference']), profile_sha256=sha256(raw))
        self.package = self.fixture.compile()

    def policy(self, storage):
        path = self.requests/'policy.json'
        if path.exists(): path.chmod(0o600)
        path.write_bytes(canonical(dict(schema_version=1,max_total_bytes=storage,approval_sha256='a'*64)))
        path.chmod(0o400)

    def receive(self, package=None, request_id=None, extra=b''):
        package = self.package if package is None else package
        raw = canonical(package['package'])
        return remote_stage.receive_author(str(self.requests), request_id or self.request_id,
            package['package_sha256'], io.BytesIO(struct.pack('!I',len(raw))+raw+extra), _profile_path=self.profile_path)

    def test_fixed_originals_copy_with_relative_tree_and_shared_allocation_identity(self):
        with patch('subprocess.run') as run, patch('subprocess.Popen') as popen:
            receipt = self.receive()
        run.assert_not_called(); popen.assert_not_called()
        case = self.requests/self.request_id
        self.assertEqual(receipt['state'], 'staged'); self.assertEqual(receipt['role'], 'author_reference')
        self.assertFalse(receipt['physical_evaluation_performed']); self.assertFalse(receipt['execution_authorized'])
        allocation = json.loads((case/'allocation.json').read_bytes())
        self.assertEqual(allocation['manifest_sha256'], self.package['package_sha256'])
        self.assertEqual(allocation['storage_bytes'], self.fixture.resources.storage_bytes)
        self.assertEqual(json.loads((case/'author-stage.json').read_bytes()), receipt)
        self.assertFalse((case/'manifest.json').exists())  # Not a forged agent snapshot.
        self.assertFalse((case/'generation.json').exists())
        for row in self.package['package']['files']:
            staged = case/row['path']
            self.assertEqual(sha256(staged.read_bytes()), row['sha256'])
            self.assertEqual(staged.stat().st_mode & 0o777, 0o400)
            self.assertEqual(staged.read_bytes(), (self.original/row['source_path']).read_bytes())
        with self.assertRaises(remote_stage.StageError): self.receive()

    def test_partial_copy_preserves_allocation_and_blocks_restage(self):
        path = self.original/'workflow/example.txt'
        path.chmod(0o600)
        path.write_bytes(b'changed source bytes'); path.chmod(0o400)
        with self.assertRaises(remote_stage.StageError): self.receive()
        case = self.requests/self.request_id
        self.assertTrue((case/'allocation.json').exists()); self.assertTrue((case/'author-stage-intent.json').exists())
        self.assertFalse((case/'author-stage.json').exists())
        with self.assertRaisesRegex(remote_stage.StageError, 'reconcile'): self.receive()
        # Failed byte verification retains its entire allocation in aggregate use.
        self.policy(remote_stage.ROOT_ALLOWANCE+self.fixture.resources.storage_bytes)
        with self.assertRaisesRegex(remote_stage.StageError, 'aggregate storage'):
            self.receive(request_id='f'*32)
        self.assertFalse((self.requests/('f'*32)).exists())

    def test_package_and_trusted_profile_identity_fail_before_reservation(self):
        for change in ('role','digest','argv','trailing'):
            altered = deepcopy(self.package)
            if change == 'role': altered['package']['role'] = 'agent'
            elif change == 'digest': altered['package_sha256'] = '0'*64
            elif change == 'argv': altered['package']['invocations'][0]['argv'] = ['-c','hidden']
            if change != 'digest': altered['package_sha256'] = sha256(canonical(altered['package']))
            with self.subTest(change=change), self.assertRaises(remote_stage.StageError):
                self.receive(altered, extra=b'extra' if change=='trailing' else b'')
            self.assertFalse((self.requests/self.request_id).exists())
        self.profile['native_slurm']['modules'] = ['changed/1']
        self.profile_path.chmod(0o600)
        self.profile_path.write_bytes(canonical(self.profile)); self.profile_path.chmod(0o400)
        with self.assertRaises(remote_stage.StageError): self.receive()
        self.assertFalse((self.requests/self.request_id).exists())

    def test_unregistered_original_source_cannot_be_copied(self):
        altered = deepcopy(self.package)
        altered['package']['files'][0]['binding_sha256'] = '0'*64
        altered['package_sha256'] = sha256(canonical(altered['package']))
        with self.assertRaisesRegex(remote_stage.StageError, 'unregistered'): self.receive(altered)
        self.assertFalse((self.requests/self.request_id).exists())

    def test_original_symlink_hardlink_public_file_and_nested_symlink_are_denied(self):
        path = self.original/'workflow/example.txt'; raw = path.read_bytes()
        for index, change in enumerate(('symlink','hardlink','public','directory')):
            path.unlink()
            if change == 'symlink':
                outside = self.base/'outside'; outside.write_bytes(raw); outside.chmod(0o400)
                path.symlink_to(outside)
            elif change == 'hardlink':
                outside = self.base/'outside-hardlink'; outside.write_bytes(raw); outside.chmod(0o400)
                os.link(outside,path)
            elif change == 'public': path.write_bytes(raw); path.chmod(0o644)
            else: path.mkdir(mode=0o700)
            request = f'{index:032x}'
            self.policy(remote_stage.ROOT_ALLOWANCE+10*self.fixture.resources.storage_bytes)
            with self.subTest(change=change), self.assertRaises((remote_stage.StageError,OSError)):
                self.receive(request_id=request)
            self.assertFalse((self.requests/request/'author-stage.json').exists())
            if path.is_dir(): path.rmdir(); path.write_bytes(raw)
        path.chmod(0o400)
        folder = self.original/'workflow'; moved = self.original/'workflow-fixed'
        folder.rename(moved); folder.symlink_to(moved,target_is_directory=True)
        with self.assertRaises((remote_stage.StageError,OSError)):
            self.receive(request_id='b'*32)

    def test_nested_group_write_inside_private_root_matches_existing_reader(self):
        (self.original/'workflow').chmod(0o770)
        self.assertEqual(self.receive()['state'],'staged')

    def test_overlap_unpinned_runtime_and_capacity_fail_without_new_request(self):
        helper = self.deployment_root/'runtime_launcher.py'
        helper.chmod(0o600)
        helper.write_bytes(self.runtime_raw+b'\n# modified'); helper.chmod(0o400)
        with self.assertRaisesRegex(remote_stage.StageError,'helper changed'): self.receive()
        self.assertFalse((self.requests/self.request_id).exists())
        helper.chmod(0o600); helper.write_bytes(self.runtime_raw); helper.chmod(0o400)
        self.policy(remote_stage.ROOT_ALLOWANCE+self.fixture.resources.storage_bytes-1)
        with self.assertRaisesRegex(remote_stage.StageError,'aggregate storage'): self.receive()
        self.assertFalse((self.requests/self.request_id).exists())
        self.profile['author_reference']['source_roots'][0]['path'] = str(self.requests)
        self.rebind()
        with self.assertRaisesRegex(remote_stage.StageError,'disjoint'): self.receive()
        self.assertFalse((self.requests/self.request_id).exists())


if __name__ == '__main__': unittest.main()
