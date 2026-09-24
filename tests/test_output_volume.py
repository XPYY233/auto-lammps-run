"""Storage allocation and execution/collection wiring; no physics or FUSE in CI."""
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import types
import unittest
from unittest.mock import patch

import test_runtime_launcher as fixture
from auto_lammps import runtime_launcher as runtime
from auto_lammps.manifest import relative_name, ManifestError
from auto_lammps.remote_stage import safe_name, StageError, approved_root


class VolumeTests(unittest.TestCase):
    private = fixture.RuntimeTests.private
    allocation = fixture.RuntimeTests.allocation
    guards = fixture.RuntimeTests.guards

    def setUp(self):
        fixture.RuntimeTests.setUp(self)
        helper = self.control/'helpers'
        helper.mkdir(mode=0o700)
        config = dict(kind='ext2-fuse', max_image_bytes=16*1024*1024, helper_directory=str(helper))
        for prefix in ('mkfs', 'fuse2fs', 'fusermount'):
            path = self.control/prefix
            self.private(path, ('synthetic tool '+prefix).encode())
            config[prefix+'_path'] = str(path)
            config[prefix+'_sha256'] = runtime.digest(path.read_bytes())
        (helper/'fusermount').symlink_to(config['fusermount_path'])
        self.profile['output_volume'] = config
        self.config = config
        self.profile_bytes = runtime.canonical(self.profile)
        self.private(self.profile_path, self.profile_bytes)
        manifest = json.loads((self.case/'manifest.json').read_bytes())
        manifest['resources']['storage_bytes'] = 20*1024*1024
        data = runtime.canonical(manifest)
        self.digest = runtime.digest(data)
        (self.case/'manifest.json').chmod(0o600)
        self.private(self.case/'manifest.json', data)
        stage = json.loads((self.case/'stage.json').read_bytes())
        stage['manifest_sha256'] = self.digest
        (self.case/'stage.json').chmod(0o600)
        self.private(self.case/'stage.json', runtime.canonical(stage))
        self.payload.update(manifest_sha256=self.digest, profile_sha256=runtime.digest(self.profile_bytes),
                            resources=manifest['resources'])
        self.private(self.control/(fixture.REQUEST+'.json'), runtime.canonical(fixture.signed(self.payload)))

    def test_image_shares_capacity_and_preserves_external_reserve(self):
        size = 20*1024*1024
        allocation = runtime.volume_allocation(self.profile, storage_bytes=size, input_bytes=10000)
        self.assertEqual(allocation['image_bytes'], 16*1024*1024)
        self.assertLess(allocation['image_bytes']+10000+262144+2*65536, size)
        self.assertGreater(allocation['image_bytes'], runtime.validate_outputs(
            fixture.OUTPUTS, storage_bytes=size, input_bytes=10000))
        with self.assertRaises(runtime.ExecutionDenied):
            runtime.volume_allocation(self.profile, storage_bytes=8*1024*1024, input_bytes=0)
        for value in (True, -1, '10000000'):
            with self.assertRaises(runtime.ExecutionDenied):
                runtime.volume_allocation(self.profile, storage_bytes=value, input_bytes=0)
        self.assertIsNone(runtime.volume_allocation({}, storage_bytes=size, input_bytes=0))

    def test_changed_tool_or_helper_is_rejected(self):
        # CI hosts may have a system fusermount; isolate only that lookup.
        with patch.object(runtime.Path, 'exists', return_value=False):
            self.assertEqual(runtime.verify_volume_tools(self.profile), self.config)
            self.private(Path(self.config['fuse2fs_path']), b'changed')
            with self.assertRaisesRegex(runtime.ExecutionDenied, 'version mismatch'):
                runtime.verify_volume_tools(self.profile)
        self.config['fuse2fs_sha256'] = runtime.digest(b'changed')
        helper = Path(self.config['helper_directory'])/'fusermount'
        helper.unlink(); helper.symlink_to('/unapproved/helper')
        with self.assertRaisesRegex(runtime.ExecutionDenied, 'Unverified'):
            runtime.verify_volume_tools(self.profile)

    def test_mount_identity_capacity_readonly_and_owner(self):
        mount, image = self.case/'output', self.case/'output-volume.ext2'
        line = f'123 10 0:99 / {mount} rw,nosuid,nodev - fuse.ext4 {image} rw,user_id={os.getuid()}\n'
        def check(value, readonly=False, blocks=2048):
            with patch.object(runtime.Path, 'read_text', return_value=value), patch.object(
                    runtime.os, 'statvfs', return_value=types.SimpleNamespace(f_blocks=blocks, f_frsize=4096)):
                runtime.verify_volume_mount(mount, image, 8*1024*1024, readonly=readonly)
        check(line)
        for bad in (line.replace('fuse.ext4', 'ext4'), line.replace(str(image), '/wrong/image'),
                    line.replace(f'user_id={os.getuid()}', 'user_id=99999999'), line+line, ''):
            with self.assertRaises(runtime.ExecutionDenied): check(bad)
        with self.assertRaises(runtime.ExecutionDenied): check(line, readonly=True)
        with self.assertRaises(runtime.ExecutionDenied): check(line, blocks=2049)
        check(line.replace(' rw,', ' ro,'), readonly=True)

    def test_execution_and_readonly_collection_share_frozen_allocation(self):
        modes = []
        @contextmanager
        def fake_volume(profile, case, allocation, *, readonly=False):
            modes.append(readonly)
            if not readonly:
                (case/'output').mkdir(mode=0o700)
            yield case/'output'
        stack, popen = self.guards()
        with stack, patch.object(runtime, 'mounted_output_volume', side_effect=fake_volume):
            result = runtime.execute(self.profile_path, fixture.REQUEST, self.digest)
            self.assertFalse(result['timed_out'])
            self.assertEqual(popen.call_count, 1)
            intent = json.loads((self.case/'execution-intent.json').read_text())
            self.assertEqual(intent['output_storage']['image_bytes'], 16*1024*1024)
            with self.assertRaises(FileExistsError):
                runtime.execute(self.profile_path, fixture.REQUEST, self.digest)
        with patch.object(runtime, 'mounted_output_volume', side_effect=fake_volume):
            header = runtime.collect(self.profile_path, fixture.REQUEST, self.digest, '123',
                                     self.profile['requests_root'], io.BytesIO())
        self.assertEqual(modes, [False, True])
        self.assertEqual(header['scientific_status'], 'not_evaluated')
        self.assertEqual(header['missing_outputs'], [])
        # An altered recorded capacity cannot reopen another volume allocation.
        intent['output_storage']['image_bytes'] = 8*1024*1024
        (self.case/'execution-intent.json').chmod(0o600)
        self.private(self.case/'execution-intent.json', runtime.canonical(intent))
        with self.assertRaisesRegex(runtime.ExecutionDenied, 'allocation'):
            runtime.collect(self.profile_path, fixture.REQUEST, self.digest, '123',
                            self.profile['requests_root'], io.BytesIO())

    def test_image_name_is_reserved_in_both_staging_boundaries(self):
        with self.assertRaises(ManifestError): relative_name('output-volume.ext2')
        with self.assertRaises(StageError): safe_name('output-volume.ext2')

    @unittest.skipUnless(hasattr(os, 'O_PATH') and os.geteuid() != 0,
                         'Requires Linux O_PATH and a non-root permission check')
    def test_traversal_only_ancestors_do_not_require_directory_listing(self):
        ancestor = self.root/'traverse-only'
        ancestor.mkdir(mode=0o700)
        leaf = ancestor/'owned'
        leaf.mkdir(mode=0o700)
        ancestor.chmod(0o111)
        try:
            with self.assertRaises(PermissionError): list(ancestor.iterdir())
            fd = runtime.directory(leaf, private=True)
            try: os.fsync(fd)
            finally: os.close(fd)
            with approved_root(str(leaf)) as fd: os.fsync(fd)
            alias = self.root/'alias'
            alias.symlink_to(ancestor, target_is_directory=True)
            with self.assertRaises(OSError): runtime.directory(alias/'owned')
        finally:
            ancestor.chmod(0o700)
