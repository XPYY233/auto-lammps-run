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
        self.assertEqual(first.verify()['schema_version'], 1)
        self.assertEqual(first, self.freeze(external_files={}))
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

    def external_record(self, *, size=37):
        return dict(path='structure.data', role='structure', size=size, sha256='d'*64,
                    external_source=dict(catalog_sha256='e'*64, pin='f'*64))

    def test_external_structure_freezes_identity_without_local_geometry(self):
        record = self.external_record()
        snapshot = self.freeze(external_files={record['path']: record})
        document = snapshot.verify()
        self.assertEqual(document['schema_version'], 2)
        self.assertIn(record, document['files'])
        self.assertFalse((snapshot.path/'structure.data').exists())
        self.assertEqual(snapshot, self.freeze(external_files={record['path']: record}))
        record['external_source']['pin'] = '0'*64
        self.assertEqual(snapshot.verify(), document)
        self.assertNotEqual(snapshot.digest, self.freeze(external_files={record['path']: record}).digest)

    def test_materialized_external_structure_is_verified_and_optional(self):
        data = b'Synthetic inert geometry bytes; never executed.\n'
        record = self.external_record(size=len(data)); record['sha256'] = sha256(data)
        snapshot = self.freeze(external_files={record['path']: record})
        path = snapshot.path/'structure.data'; path.write_bytes(data)
        snapshot.verify()
        path.write_bytes(data[:-1])
        with self.assertRaises(ManifestError):
            snapshot.verify()
        path.unlink(); snapshot.verify()
        path.symlink_to(self.source/'input.in')
        with self.assertRaises(ManifestError):
            snapshot.verify()

    def test_external_structure_fields_roles_and_schema_are_strict(self):
        original = self.external_record()
        bad = []
        for role in ('potential', 'analysis_spec', 'lammps_input', 'unknown'):
            bad.append({**original, 'role': role})
        bad.extend([{**original, 'size': True}, {**original, 'size': -1},
                    {**original, 'sha256': 'not-a-digest'}, {**original, 'extra': 'forbidden'},
                    {**original, 'external_source': {'catalog_sha256': 'e'*64}},
                    {**original, 'external_source': {'catalog_sha256': 'e'*64, 'pin': '../escape'}},
                    {**original, 'external_source': {'catalog_sha256': 'e'*64, 'pin': 'f'*64, 'path': '/private'}}])
        for record in bad:
            with self.subTest(record=record), self.assertRaises(ManifestError):
                self.freeze(external_files={'structure.data': record})
        snapshot = self.freeze(external_files={'structure.data': original})
        document = snapshot.verify(); document['schema_version'] = 1
        encoded = canonical(document); manifest = snapshot.path/'manifest.json'
        manifest.chmod(0o600); manifest.write_bytes(encoded)
        with self.assertRaises(ManifestError):
            Snapshot(snapshot.path, sha256(encoded)).verify()

    def test_external_storage_count_duplicate_and_path_overlap(self):
        record = self.external_record(size=4096)
        with self.assertRaises(ManifestError):
            self.freeze(external_files={'structure.data': record})
        with self.assertRaises(ManifestError):
            self.freeze(external_files={'input.in': {**self.external_record(), 'path': 'input.in'}})
        with self.assertRaises(ManifestError):
            self.freeze(external_files={'structure.data': {**self.external_record(), 'path': 'different.data'}})
        with self.assertRaises(ManifestError):
            self.freeze(external_files={'potential': {**self.external_record(), 'path': 'potential'}})
        record = self.external_record()
        with self.assertRaises(ManifestError):
            self.freeze(external_files={f's{i}.data': {**record, 'path': f's{i}.data'} for i in range(127)})
        snapshot = self.freeze(external_files={'structure.data': record})
        document = snapshot.verify(); document['files'].append(record)
        encoded = canonical(document); manifest = snapshot.path/'manifest.json'
        manifest.chmod(0o600); manifest.write_bytes(encoded)
        with self.assertRaises(ManifestError):
            Snapshot(snapshot.path, sha256(encoded)).verify()

    def test_external_materialization_rejects_hard_links_and_linked_parents(self):
        data = (self.source/'input.in').read_bytes()
        record = self.external_record(size=len(data)); record['sha256'] = sha256(data)
        snapshot = self.freeze(external_files={'structure.data': record})
        os.link(self.source/'input.in', snapshot.path/'structure.data')
        with self.assertRaises(ManifestError):
            snapshot.verify()
        (snapshot.path/'structure.data').unlink()
        nested = {**record, 'path': 'geometry/structure.data'}
        snapshot = self.freeze(external_files={nested['path']: nested})
        (snapshot.path/'geometry').symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(ManifestError):
            snapshot.verify()


if __name__ == '__main__':
    unittest.main()
