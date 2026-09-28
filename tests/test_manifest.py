import os
from pathlib import Path
import tempfile
import unittest

from auto_lammps.ledger import Resources
from auto_lammps.manifest import ManifestError, Snapshot, canonical, freeze, relative_name, sha256


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'input.in').write_text('# Synthetic text; never executed.\n')
        (self.source / 'potential').mkdir()
        (self.source / 'potential' / 'test.table').write_bytes(b'synthetic potential placeholder')
        self.store = self.root / 'private'
        self.resources = Resources(1, 60, 1024, 4096)
        self.provenance = dict(task_sha256='a'*64, analysis_sha256='b'*64, software_sha256='c'*64)
        self.files = {'input.in': 'lammps_input', 'potential/test.table': 'potential'}

    def freeze(self, **overrides):
        options = dict(files=self.files, entrypoint='input.in', resources=self.resources, provenance=self.provenance)
        options.update(overrides)
        return freeze(self.source, self.store, **options)

    def test_content_addressed_repeat_and_source_independence(self):
        first = self.freeze()
        second = self.freeze()
        self.assertEqual(first, second)
        self.assertEqual(first.verify()['resources']['cores'], 1)
        (self.source / 'input.in').write_text('changed source')
        self.assertEqual(len(first.verify()['files']), 2)
        self.assertNotEqual(self.freeze().digest, first.digest)
        self.assertEqual((first.path / 'input.in').stat().st_mode & 0o777, 0o400)

    def test_path_traversal_shell_and_reserved_names(self):
        for name in ('../x', '/tmp/x', 'a/../x', 'a//x', './x', '.hidden', 'a\\b',
                     'x;id', 'x\nfoo', 'x$(id)', 'manifest.json', 'job.sh', ''):
            with self.subTest(name=name), self.assertRaises(ManifestError):
                relative_name(name)

    def test_symlink_file_directory_and_source_root(self):
        (self.source / 'link').symlink_to(self.source / 'input.in')
        with self.assertRaises(ManifestError):
            self.freeze(files={'link': 'lammps_input'}, entrypoint='link')
        (self.source / 'linked').symlink_to(self.source / 'potential', target_is_directory=True)
        with self.assertRaises(ManifestError):
            self.freeze(files={'input.in': 'lammps_input', 'linked/test.table': 'potential'})
        linked_root = self.root / 'linked-root'
        linked_root.symlink_to(self.source, target_is_directory=True)
        self.source = linked_root
        with self.assertRaises(ManifestError):
            self.freeze()

    def test_hardlink_and_fifo_rejected(self):
        os.link(self.source / 'input.in', self.source / 'alias')
        with self.assertRaises(ManifestError):
            self.freeze()
        (self.source / 'alias').unlink()
        (self.source / 'input.in').unlink()
        os.mkfifo(self.source / 'input.in')
        with self.assertRaises(ManifestError):
            self.freeze()

    def test_file_and_manifest_storage_limits(self):
        for size in (1, 100):
            with self.subTest(size=size), self.assertRaises(ManifestError):
                self.freeze(resources=Resources(1, 60, 1024, size))
        self.assertEqual(list(self.store.glob('.freeze-*')), [])

    def test_integrity_and_undeclared_files(self):
        snapshot = self.freeze()
        target = snapshot.path / 'input.in'
        target.chmod(0o600)
        target.write_text('changed frozen content')
        with self.assertRaises(ManifestError):
            snapshot.verify()
        # Repeating the original request must fail rather than overwrite damage.
        with self.assertRaises(ManifestError):
            self.freeze()

    def test_extra_file_and_extra_symlink_directory(self):
        snapshot = self.freeze()
        extra = snapshot.path / 'extra'
        extra.write_text('not declared')
        with self.assertRaises(ManifestError):
            snapshot.verify()
        extra.unlink()
        extra.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(ManifestError):
            snapshot.verify()

    def test_store_must_be_private_and_outside_git(self):
        self.store.mkdir(mode=0o755)
        with self.assertRaises(ManifestError):
            self.freeze()
        self.store.chmod(0o700)
        (self.root / '.git').mkdir()
        with self.assertRaises(ManifestError):
            self.freeze()

    def test_provenance_and_one_entrypoint_required(self):
        for provenance in ({}, {**self.provenance, 'answer': 'not allowed'}):
            with self.assertRaises(ManifestError):
                self.freeze(provenance=provenance)
        with self.assertRaises(ManifestError):
            self.freeze(files={'input.in': 'lammps_input', 'other.in': 'lammps_input'})

    def test_invalid_json_schema_and_resources_fail_closed(self):
        snapshot = self.freeze()
        original = snapshot.verify()
        cases = [b'{', b'[]', canonical({**original, 'resources': {'cores': 1}}),
                 canonical({**original, 'files': [{}]}), canonical({**original, 'provenance': {}})]
        path = snapshot.path / 'manifest.json'
        path.chmod(0o600)
        for encoded in cases:
            path.write_bytes(encoded)
            with self.subTest(encoded=encoded), self.assertRaises(ManifestError):
                Snapshot(snapshot.path, sha256(encoded)).verify()


if __name__ == '__main__':
    unittest.main()
