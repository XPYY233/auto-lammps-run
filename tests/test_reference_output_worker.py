import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from auto_lammps import reference_output_worker as worker


class ReferenceOutputWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.request = dict(version=1, request_id='a' * 32, job_id='12345',
                            manifest_sha256='b' * 64, root=str(self.root), files=[])

    def add_file(self, path='nested/output.bin', data=b'raw\x00\xff\r\n'):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)
        item = dict(path=path, name=file.name, size=len(data),
                    sha256=hashlib.sha256(data).hexdigest())
        self.request['files'].append(item)
        return file

    def assert_rejected(self, request=...):
        with self.assertRaises(worker.ReferenceOutputError):
            worker.read_outputs(self.request if request is ... else request)

    def run_main(self, raw):
        source = Path(worker.__file__).read_bytes()
        bootstrap = "import base64; exec(compile(base64.b64decode(%r), '<trusted-output-reader>', 'exec'))" % base64.b64encode(source)
        return subprocess.run([sys.executable, '-I', '-c', bootstrap], input=raw,
                              capture_output=True, timeout=10, check=False)

    def test_exact_binary_and_empty_files_with_identity_and_no_root(self):
        file = self.add_file()
        self.add_file('empty.dat', b'')
        original = copy.deepcopy(self.request)
        before = file.stat()
        result = worker.read_outputs(self.request)
        self.assertEqual(set(result), {'version', 'request_id', 'job_id', 'manifest_sha256', 'files'})
        self.assertEqual(self.request, original)
        self.assertEqual(result['request_id'], self.request['request_id'])
        self.assertEqual(result['job_id'], self.request['job_id'])
        self.assertEqual(result['manifest_sha256'], self.request['manifest_sha256'])
        for actual, expected in zip(result['files'], self.request['files']):
            self.assertEqual({key: actual[key] for key in expected}, expected)
            self.assertEqual(base64.b64decode(actual['data']), (self.root / expected['path']).read_bytes())
        self.assertEqual(result['files'][1]['data'], '')
        self.assertEqual(worker._stamp(before), worker._stamp(file.stat()))
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_author_python_is_returned_as_bytes_and_never_executed(self):
        canary = self.root / 'must-not-exist'
        data = ("from pathlib import Path\nPath(%r).touch()\n" % str(canary)).encode()
        self.add_file('author.py', data)
        result = worker.read_outputs(self.request)
        self.assertEqual(base64.b64decode(result['files'][0]['data']), data)
        self.assertFalse(canary.exists())
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ['author.py'])

    def test_duplicate_path_and_name_are_rejected(self):
        self.add_file('one.dat')
        for key in ('path', 'name'):
            request = copy.deepcopy(self.request)
            second = dict(request['files'][0], path='two.dat', name='two.dat')
            second[key] = request['files'][0][key]
            request['files'].append(second)
            with self.subTest(key=key):
                self.assert_rejected(request)

    def test_unsafe_paths_and_flat_names_are_rejected(self):
        self.add_file()
        for path in ('', '.', '..', '/output', '../output', 'a/../output', './output',
                     'a//output', 'a/./output', 'a/', 'a\\output', 'a\x00b', 'a\nb',
                     'a b', 'a/' + 'b' * 256, 'a/' * 600):
            request = copy.deepcopy(self.request)
            request['files'][0]['path'] = path
            with self.subTest(path=repr(path)):
                self.assert_rejected(request)
        for name in ('', '.', '..', '/output', '../output', 'a/b', 'a b', 'a\nb', 'a' * 161):
            request = copy.deepcopy(self.request)
            request['files'][0]['name'] = name
            with self.subTest(name=repr(name)):
                self.assert_rejected(request)

    def test_invalid_roots_are_rejected(self):
        self.add_file()
        for root in ('relative', '', '/a/../b', '/a/./b', '//a', '/a//b', '/a/',
                     '/a\x00b', '/a\nb', '/a\ud800', '/' + 'a' * 2048, None):
            request = copy.deepcopy(self.request)
            request['root'] = root
            with self.subTest(root=repr(root)):
                self.assert_rejected(request)

    def test_schema_identity_and_type_rejections(self):
        self.add_file()
        for value in (None, [], True, 'request', 1):
            with self.subTest(value=value):
                self.assert_rejected(value)
        for field, values in (
                ('version', [True, 1.0, 2, '1']),
                ('request_id', [True, 1, 'a' * 31, 'g' * 32]),
                ('job_id', [123, True, '', '-1', '1;id', '١٢', '1' * 33]),
                ('manifest_sha256', [None, 1, 'b' * 63, 'g' * 64]),
                ('files', [None, {}, (), []])):
            for value in values:
                request = copy.deepcopy(self.request)
                request[field] = value
                with self.subTest(field=field, value=value):
                    self.assert_rejected(request)
        for request in (dict(self.request, extra=1), {key: value for key, value in self.request.items() if key != 'root'}):
            self.assert_rejected(request)

    def test_file_metadata_types_and_unknown_fields_are_rejected(self):
        self.add_file()
        for field, values in (('size', [True, -1, 1.5, '5', worker.MAX_FILE_BYTES + 1]),
                              ('sha256', [None, 42, 'g' * 64, 'c' * 63]),
                              ('path', [None, 42]), ('name', [None, 42])):
            for value in values:
                request = copy.deepcopy(self.request)
                request['files'][0][field] = value
                with self.subTest(field=field, value=value):
                    self.assert_rejected(request)
        for item in (None, [], dict(self.request['files'][0], data='untrusted'),
                     {key: value for key, value in self.request['files'][0].items() if key != 'sha256'}):
            request = copy.deepcopy(self.request)
            request['files'] = [item]
            self.assert_rejected(request)

    def test_file_count_and_total_byte_limits(self):
        self.add_file()
        item = self.request['files'][0]
        self.request['files'] = [dict(item, path=str(i), name=str(i)) for i in range(worker.MAX_FILES)]
        worker.validate_request(self.request)
        self.request['files'].append(dict(item, path='extra', name='extra'))
        self.assert_rejected()
        self.request['files'] = [dict(item, path=str(i), name=str(i), size=worker.MAX_FILE_BYTES) for i in range(4)]
        worker.validate_request(self.request)
        self.request['files'].append(dict(item, path='fifth', name='fifth', size=1))
        self.assert_rejected()

    def test_exact_per_file_limit_is_readable(self):
        self.add_file('maximum.bin', b'x' * worker.MAX_FILE_BYTES)
        result = worker.read_outputs(self.request)
        self.assertEqual(len(base64.b64decode(result['files'][0]['data'])), worker.MAX_FILE_BYTES)

    def test_symlink_file_and_hardlink_are_rejected(self):
        file = self.add_file('original.dat')
        file.rename(self.root / 'target.dat')
        file.symlink_to('target.dat')
        self.assert_rejected()
        file.unlink()
        os.link(self.root / 'target.dat', file)
        self.assert_rejected()

    def test_symlink_relative_parent_is_rejected(self):
        self.add_file()
        (self.root / 'nested').rename(self.root / 'real')
        (self.root / 'nested').symlink_to('real', target_is_directory=True)
        self.assert_rejected()

    def test_symlink_root_or_absolute_ancestor_is_rejected(self):
        self.add_file()
        link = self.root / 'root-link'
        link.symlink_to(self.root, target_is_directory=True)
        self.request['root'] = str(link)
        self.assert_rejected()
        self.request['root'] = str(link / 'nested')
        self.request['files'][0]['path'] = 'output.bin'
        self.assert_rejected()

    def test_nonregular_file_and_missing_file_are_rejected(self):
        file = self.add_file()
        file.unlink()
        self.assert_rejected()
        file.mkdir()
        self.assert_rejected()
        file.rmdir()
        os.mkfifo(file)
        self.assert_rejected()

    def test_file_and_root_must_be_owned_by_current_user(self):
        self.add_file('owned.dat')
        with patch.object(worker.os, 'getuid', return_value=os.getuid() + 1):
            self.assert_rejected()
        real_fstat = os.fstat

        def other_file_owner(descriptor):
            info = real_fstat(descriptor)
            if stat.S_ISREG(info.st_mode):
                return SimpleNamespace(st_mode=info.st_mode, st_nlink=info.st_nlink,
                                       st_uid=info.st_uid + 1, st_size=info.st_size)
            return info

        with patch.object(worker.os, 'fstat', side_effect=other_file_owner):
            self.assert_rejected()

    def test_wrong_digest_and_changed_size_are_rejected(self):
        file = self.add_file()
        self.request['files'][0]['sha256'] = '0' * 64
        self.assert_rejected()
        self.request['files'][0]['sha256'] = hashlib.sha256(file.read_bytes()).hexdigest()
        file.write_bytes(b'longer than original')
        self.assert_rejected()

    def test_mtime_changes_during_read_are_rejected(self):
        file = self.add_file()
        before = file.stat()
        real_read = os.read
        changed = False

        def change_mtime(descriptor, size):
            nonlocal changed
            data = real_read(descriptor, size)
            if data and not changed:
                changed = True
                os.utime(file, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
            return data

        with patch.object(worker.os, 'read', side_effect=change_mtime):
            self.assert_rejected()
        self.assertTrue(changed)

    def test_ctime_change_with_restored_mtime_is_rejected(self):
        file = self.add_file('output.dat', b'original')
        before = file.stat()
        real_read = os.read
        changed = False

        def change_content(descriptor, size):
            nonlocal changed
            data = real_read(descriptor, size)
            if data and not changed:
                changed = True
                file.write_bytes(b'modified')
                os.utime(file, ns=(before.st_atime_ns, before.st_mtime_ns))
            return data

        with patch.object(worker.os, 'read', side_effect=change_content):
            self.assert_rejected()

    def test_second_digest_pass_rejects_changed_bytes(self):
        file = self.add_file('output.dat', b'original')
        real_lseek = os.lseek

        def change_between_reads(descriptor, offset, whence):
            file.write_bytes(b'modified')
            return real_lseek(descriptor, offset, whence)

        with patch.object(worker.os, 'lseek', side_effect=change_between_reads):
            self.assert_rejected()

    def test_io_errors_expose_only_fixed_message(self):
        self.add_file()
        with patch.object(worker.os, 'open', side_effect=PermissionError('private location and author text')):
            with self.assertRaisesRegex(worker.ReferenceOutputError, '^reference_output_unavailable$') as caught:
                worker.read_outputs(self.request)
        self.assertIsNone(caught.exception.__cause__)

    def test_main_standalone_base64_canonical_response(self):
        self.add_file()
        result = self.run_main(worker.canonical(self.request))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b'')
        data = base64.b64decode(result.stdout, validate=False)
        decoded = json.loads(data)
        self.assertEqual(worker.canonical(decoded), data)
        self.assertEqual(decoded, worker.read_outputs(self.request))

    def test_main_bounded_invalid_and_duplicate_json_are_safe(self):
        self.add_file()
        raw = worker.canonical(self.request)
        duplicate = raw[:-1] + b',"root":"/private-sensitive-location"}'
        for payload in (b' ' * (worker.MAX_REQUEST_BYTES + 1), b'not-json',
                        b'\xff', duplicate, b'[' * 2000):
            with self.subTest(payload_length=len(payload)):
                result = self.run_main(payload)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, b'')
                error = json.loads(result.stderr)
                self.assertEqual(error['error_type'], 'ReferenceOutputError')
                self.assertIn(error['message'], {'reference_output_request_limit', 'reference_output_request_invalid'})
                self.assertNotIn(str(self.root), result.stderr.decode())
                self.assertNotIn('private-sensitive-location', result.stderr.decode())
                self.assertNotIn('Traceback', result.stderr.decode())


if __name__ == '__main__':
    unittest.main()
