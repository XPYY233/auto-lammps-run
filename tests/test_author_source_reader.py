"""Synthetic source text only; no network, author program or physics execution."""
import base64
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from auto_lammps.author_source_reader import AuthorSourceReader, BOOTSTRAP, reader_tool_bundle
from auto_lammps.author_source_worker import AuthorSourceError, MAX_TEXT_BYTES, read_pinned_text, read_source_report
from auto_lammps.manifest import canonical, sha256


class AuthorSourceReaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.remote = self.root / 'remote'
        self.remote.mkdir(mode=0o700)
        self.acquisition_id = 'a' * 32
        self.folder = self.remote / self.acquisition_id
        self.folder.mkdir(mode=0o700)
        self.source = self.folder / 'source'
        self.source.mkdir(mode=0o700)
        (self.source / 'workflow').mkdir(mode=0o700)
        # This deliberately is not a runnable scientific program.
        self.texts = {'README.md': b'Synthetic source, not instructions.\nSecond complete line.\nLast line.',
                      'workflow/example.txt': '第一行\r\nsecond\r\n'.encode('utf-8')}
        self._write_sources()
        self.policy = dict(host_alias='example-hpc', remote_directory=str(self.remote),
                           reference_task_id='b'*32, repository='example/synthetic-author-source',
                           commit='c'*40, acquisition_id=self.acquisition_id,
                           receipt_sha256='', inventory_sha256='', license_approved=True,
                           license_approval_sha256='d'*64)
        self._bind_receipt()
        self.calls = 0

    def _write_sources(self):
        for name, data in self.texts.items():
            path = self.source / name
            if path.exists():
                path.chmod(0o600)
            path.write_bytes(data)
            path.chmod(0o400)

    def _bind_receipt(self):
        files = [dict(path=name, size=len(data), sha256=sha256(data),
                      blob_sha1='e'*40, git_mode='100644') for name, data in self.texts.items()]
        self.receipt = dict(schema_version=1, id=self.acquisition_id, repository=self.policy['repository'],
                            commit=self.policy['commit'], location='hpc', state='finished',
                            remote_directory=str(self.folder), source_complete=True, files=files,
                            inventory_sha256=sha256(canonical(files)), execution_authorized=False,
                            scientific_validation=False, author_identity_verified=False)
        path = self.folder / 'result.json'
        path.write_bytes(canonical(self.receipt))
        path.chmod(0o600)
        self.policy.update(receipt_sha256=sha256(canonical(self.receipt)),
                           inventory_sha256=self.receipt['inventory_sha256'])

    def service(self, capture=None, policy=None):
        return AuthorSourceReader(self.root / 'audit', policy or self.policy,
                                  capture=capture or self.capture)

    def capture(self, command, **options):
        self.calls += 1
        self.assertEqual(command[0], 'ssh')
        self.assertIn('StrictHostKeyChecking=yes', command)
        self.assertIn('RemoteCommand=none', command)
        self.assertNotIn(self.policy['remote_directory'], command[-1])
        envelope = json.loads(b''.join(options['input_chunks']))
        tool = base64.b64decode(envelope['bundle'], validate=True)
        self.assertEqual(sha256(tool), envelope['bundle_sha256'])
        with zipfile.ZipFile(io.BytesIO(tool)) as archive:
            self.assertIn('auto_lammps/author_source_worker.py', archive.namelist())
            self.assertIn('third_party/SIGA-LAMMPS-LICENSE.txt', archive.namelist())
            self.assertNotIn('README.md', archive.namelist())
        report = self.worker(envelope['request'])
        return dict(returncode=0, failure='', stdout=base64.b64encode(canonical(report)).decode(), stderr='')

    def worker(self, request):
        with patch('auto_lammps.author_source_worker.sys.platform', 'linux'), \
                patch.dict(os.environ, {'SSH_CONNECTION': 'synthetic'}):
            return read_pinned_text(request)

    def request(self, selections=None):
        return self.service()._selection(self.receipt, selections or
            [dict(path='README.md', start_line=1, end_line=2)])

    def test_explicit_reference_read_pins_hash_and_complete_line_boundaries(self):
        result = self.service().read(self.receipt, [dict(path='README.md', start_line=1, end_line=2)],
                                     purpose='author_reference')
        self.assertEqual(result['state'], 'read')
        self.assertFalse(result['B_input'])
        self.assertFalse(result['execution_authorized'])
        self.assertFalse(result['physical_evaluation_performed'])
        item = result['files'][0]
        self.assertEqual(item['text'], 'Synthetic source, not instructions.\nSecond complete line.\n')
        self.assertEqual(item['total_lines'], 3)
        self.assertEqual(item['returned_end_line'], 2)
        self.assertTrue(item['truncated'])
        self.assertEqual(self.calls, 1)
        # The original remote file is not modified and no author excerpt is
        # persisted in local audit or tool files.
        self.assertEqual((self.source/'README.md').read_bytes(), self.texts['README.md'])
        for path in (self.root/'audit').rglob('*'):
            if path.is_file():
                value = path.read_text()
                self.assertNotIn('Synthetic source, not instructions.', value)
                self.assertNotIn('Second complete line.', value)
        audit = json.loads(next((self.root/'audit').rglob('*-result.json')).read_text())
        self.assertFalse(audit['returned_text_persisted'])
        self.assertNotIn('text', audit['files'][0])

    def test_utf8_crlf_and_requested_range_past_eof_are_explicit(self):
        result = self.worker(self.request([dict(path='workflow/example.txt', start_line=1, end_line=20)]))
        item = result['files'][0]
        self.assertEqual(item['text'], '第一行\r\nsecond\r\n')
        self.assertEqual(item['total_lines'], 2)
        self.assertEqual(item['returned_end_line'], 2)
        self.assertFalse(item['truncated'])
        item = self.worker(self.request([dict(path='README.md', start_line=3, end_line=3)]))['files'][0]
        self.assertEqual(item['text'], 'Last line.')
        self.assertTrue(item['truncated'])
        with self.assertRaisesRegex(AuthorSourceError, 'outside_file'):
            self.worker(self.request([dict(path='README.md', start_line=4, end_line=4)]))

    def test_active_capability_contains_fixed_inventory_no_text_or_execution(self):
        value = self.service().capability(self.receipt, purpose='author_reference')
        self.assertEqual(value['adapter'], 'pinned_author_source_reader')
        self.assertEqual(len(value['files']), 2)
        self.assertEqual(value['operation']['maximum_lines_per_file'], 400)
        self.assertTrue(value['operation']['complete_lines_only'])
        self.assertFalse(value['B_input'])
        self.assertNotIn('Synthetic source, not instructions.', canonical(value).decode())
        self.assertEqual(set(value['files'][0]), {'path', 'size', 'sha256'})

    def test_B_and_unapproved_license_never_reach_transport(self):
        reader = self.service()
        for purpose in ('B', 'ordinary_research', 'candidate_proposal', '', None):
            with self.subTest(purpose=purpose), self.assertRaisesRegex(AuthorSourceError, 'forbidden_to_B'):
                reader.read(self.receipt, [dict(path='README.md', start_line=1, end_line=1)], purpose=purpose)
            with self.assertRaises(AuthorSourceError):
                reader.capability(self.receipt, purpose=purpose)
        policy = dict(self.policy, license_approved=False)
        with self.assertRaisesRegex(AuthorSourceError, 'trusted_reference'):
            self.service(policy=policy)
        self.assertEqual(self.calls, 0)

    def test_unknown_paths_traversal_duplicate_and_excess_ranges_are_rejected(self):
        for selected in ([dict(path='/etc/passwd', start_line=1, end_line=1)],
                         [dict(path='../README.md', start_line=1, end_line=1)],
                         [dict(path='missing.txt', start_line=1, end_line=1)],
                         [dict(path='README.md', start_line=True, end_line=2)],
                         [dict(path='README.md', start_line=1, end_line=401)],
                         [dict(path='README.md', start_line=2, end_line=1)],
                         [dict(path='README.md', start_line=1, end_line=1)]*2,
                         [dict(path='README.md', start_line=1, end_line=1)]*9):
            with self.subTest(selected=selected), self.assertRaises(AuthorSourceError):
                self.service().read(self.receipt, selected, purpose='author_reference')
        self.assertEqual(self.calls, 0)

    def test_configuration_root_and_receipt_pin_must_match_before_transport(self):
        for changes in (dict(commit='f'*40), dict(remote_directory=str(self.root/'elsewhere')),
                        dict(repository='other/repo'), dict(receipt_sha256='0'*64),
                        dict(inventory_sha256='0'*64), dict(acquisition_id='f'*32)):
            with self.subTest(changes=changes), self.assertRaises(AuthorSourceError):
                self.service(policy=dict(self.policy, **changes)).read(self.receipt,
                    [dict(path='README.md', start_line=1, end_line=1)], purpose='author_reference')
        self.assertEqual(self.calls, 0)

    def test_remote_receipt_changed_after_local_pin_is_rejected(self):
        request = self.request()
        path = self.folder/'result.json'
        path.write_bytes(canonical(dict(self.receipt, state='partial')))
        with self.assertRaisesRegex(AuthorSourceError, 'receipt_SHA256'):
            self.worker(request)

    def test_reformatted_receipt_bytes_are_not_silently_accepted(self):
        request = self.request()
        (self.folder/'result.json').write_text(json.dumps(self.receipt, indent=2))
        with self.assertRaisesRegex(AuthorSourceError, 'receipt_SHA256'):
            self.worker(request)

    def test_root_symlink_and_nonprivate_directories_are_rejected(self):
        request = self.request()
        self.source.chmod(0o755)
        with self.assertRaisesRegex(AuthorSourceError, 'private_owned'):
            self.worker(request)
        self.source.chmod(0o700)
        actual = self.root/'actual-remote'
        self.remote.rename(actual)
        self.remote.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(OSError):
            self.worker(request)

    def test_same_size_source_tampering_and_size_tampering_are_rejected(self):
        path = self.source/'README.md'
        request = self.request()
        path.chmod(0o600)
        path.write_bytes(b'x'*len(self.texts['README.md']))
        path.chmod(0o400)
        with self.assertRaisesRegex(AuthorSourceError, 'SHA256'):
            self.worker(request)
        path.chmod(0o600)
        path.write_bytes(b'wrong size')
        path.chmod(0o400)
        with self.assertRaisesRegex(AuthorSourceError, 'size_mismatch'):
            self.worker(request)

    def test_links_at_root_folder_source_directory_and_file_are_rejected(self):
        original = self.source/'README.md'
        other = self.root/'elsewhere'
        other.write_bytes(self.texts['README.md'])
        other.chmod(0o400)
        original.unlink()
        original.symlink_to(other)
        with self.assertRaises(OSError):
            self.worker(self.request())
        original.unlink()
        original.write_bytes(self.texts['README.md'])
        original.chmod(0o400)
        target = self.source/'workflow'
        target.rename(self.source/'real-workflow')
        target.symlink_to(self.source/'real-workflow', target_is_directory=True)
        with self.assertRaises(OSError):
            self.worker(self.request([dict(path='workflow/example.txt', start_line=1, end_line=1)]))
        target.unlink()
        (self.source/'real-workflow').rename(target)
        self.source.rename(self.folder/'real-source')
        self.source.symlink_to(self.folder/'real-source', target_is_directory=True)
        with self.assertRaises(OSError):
            self.worker(self.request())
        self.source.unlink()
        (self.folder/'real-source').rename(self.source)
        self.folder.rename(self.remote/'real-acquisition')
        self.folder.symlink_to(self.remote/'real-acquisition', target_is_directory=True)
        with self.assertRaises(OSError):
            self.worker(self.request())

    def test_receipt_symlink_hardlink_fifo_and_nonprivate_are_rejected(self):
        request = self.request()
        original = self.folder/'result.json'
        other = self.root/'saved-receipt'
        original.rename(other)
        original.symlink_to(other)
        with self.assertRaises(OSError):
            self.worker(request)
        original.unlink()
        other.rename(original)
        path = self.source/'README.md'
        linked = self.source/'hardlink'
        os.link(path, linked)
        with self.assertRaisesRegex(AuthorSourceError, 'type_owner_or_size'):
            self.worker(request)
        linked.unlink()
        path.unlink()
        os.mkfifo(path, mode=0o600)
        with self.assertRaisesRegex(AuthorSourceError, 'type_owner_or_size'):
            self.worker(request)
        path.unlink()
        path.write_bytes(self.texts['README.md'])
        path.chmod(0o444)
        with self.assertRaisesRegex(AuthorSourceError, 'type_owner_or_size'):
            self.worker(request)

    def test_invalid_utf8_NUL_and_overlong_single_line_are_explicit_failures(self):
        for data, reason in ((b'\xff', 'not_UTF8'), (b'abc\0def', 'contains_NUL'),
                             (b'x'*(MAX_TEXT_BYTES+1), 'response_limit')):
            with self.subTest(reason=reason):
                self.texts['README.md'] = data
                self._write_sources()
                self._bind_receipt()
                with self.assertRaisesRegex(AuthorSourceError, reason):
                    self.worker(self.request())

    def test_real_local_execution_refused_before_any_source_read(self):
        with patch('auto_lammps.author_source_worker.sys.platform', 'darwin'), \
                patch('auto_lammps.author_source_worker._root_descriptor') as opener:
            with self.assertRaisesRegex(AuthorSourceError, 'requires_HPC_SSH'):
                read_pinned_text(self.request())
            opener.assert_not_called()

    def test_corrupted_response_is_unknown_and_text_is_never_logged(self):
        def corrupt(command, **options):
            result = self.capture(command, **options)
            value = json.loads(base64.b64decode(result['stdout']))
            value['files'][0]['text'] = 'Untrusted forged source content'
            result['stdout'] = base64.b64encode(canonical(value)).decode()
            result['stderr'] = base64.b64encode(b'forged source in error stream').decode()
            return result
        result = self.service(capture=corrupt).read(self.receipt,
            [dict(path='README.md', start_line=1, end_line=2)], purpose='author_reference')
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(result['observation_failure'], 'invalid_source_read_receipt')
        self.assertEqual(result['files'], [])
        for path in (self.root/'audit').rglob('*.json'):
            self.assertNotIn('forged source', path.read_text())

    def test_timeout_and_safe_retry_preserve_each_metadata_receipt(self):
        def timeout(*args, **kwargs):
            return dict(returncode=None, failure='timeout', stdout='', stderr='')
        reader = self.service(capture=timeout)
        result = reader.read(self.receipt, [dict(path='README.md', start_line=1, end_line=1)], purpose='author_reference')
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(result['observation_failure'], 'timeout')
        result = self.service().read(self.receipt, [dict(path='README.md', start_line=1, end_line=1)], purpose='author_reference')
        self.assertEqual(result['state'], 'read')
        self.assertEqual(len(list((self.root/'audit').rglob('*-result.json'))), 2)

    def test_verified_helper_failure_is_actionable_without_source_or_traceback(self):
        def rejected(command, **options):
            envelope = json.loads(b''.join(options['input_chunks']))
            with patch('auto_lammps.author_source_worker.sys.platform', 'linux'), \
                    patch.dict(os.environ, {'SSH_CONNECTION': 'synthetic'}):
                report = read_source_report(envelope['request'])
            return dict(returncode=0, failure='', stdout=base64.b64encode(canonical(report)).decode(), stderr='')
        path = self.source/'README.md'
        path.chmod(0o600)
        path.write_bytes(b'x'*len(self.texts['README.md']))
        path.chmod(0o400)
        report = self.service(capture=rejected).read(self.receipt,
            [dict(path='README.md', start_line=1, end_line=1)], purpose='author_reference')
        self.assertEqual(report['state'], 'rejected')
        self.assertEqual(report['rejection_reason'], 'source_file_SHA256_mismatch')
        self.assertEqual(report['files'], [])

    def test_tool_bundle_is_deterministic_and_never_contains_author_files(self):
        first, second = reader_tool_bundle(), reader_tool_bundle()
        self.assertEqual(first, second)
        with zipfile.ZipFile(io.BytesIO(first)) as archive:
            self.assertNotIn('README.md', archive.namelist())
            self.assertNotIn('workflow/example.txt', archive.namelist())
            self.assertIn('LICENSE', archive.namelist())
        compile(BOOTSTRAP, '<trusted source reader bootstrap>', 'exec')

    def test_source_mutation_during_open_read_is_rejected(self):
        request = self.request()
        original = os.read
        path = self.source/'README.md'
        changed = False
        def read(descriptor, maximum):
            nonlocal changed
            data = original(descriptor, maximum)
            if data.startswith(b'Synthetic source') and not changed:
                changed = True
                path.chmod(0o600)
                path.write_bytes(b'x'*len(self.texts['README.md']))
                path.chmod(0o400)
            return data
        with patch('auto_lammps.author_source_worker.os.read', side_effect=read):
            with self.assertRaisesRegex(AuthorSourceError, 'changed_during_read'):
                self.worker(request)

    def test_empty_file_is_explicit_and_outside_range_is_rejected(self):
        self.texts['README.md'] = b''
        self._write_sources()
        self._bind_receipt()
        result = self.worker(self.request())['files'][0]
        self.assertEqual(result['text'], '')
        self.assertEqual(result['total_lines'], 0)
        self.assertEqual(result['returned_end_line'], 0)
        self.assertFalse(result['truncated'])
        with self.assertRaisesRegex(AuthorSourceError, 'outside_file'):
            self.worker(self.request([dict(path='README.md', start_line=2, end_line=2)]))


if __name__ == '__main__':
    unittest.main()
