"""Pinned HPC metadata diagnostics: synthetic cache only, no SSH/network/physics."""
import base64
from copy import deepcopy
import io
import json
import os
from pathlib import Path
from contextlib import redirect_stdout
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from auto_lammps.author_resource_inventory_worker import (
    CACHE_KEYS, LIMITS, MAX_BLOB, classify_entries, inspect_cached_tree, inventory_summary)
from auto_lammps.manifest import canonical, sha256
from auto_lammps.potential_acquisition import AcquisitionError
from auto_lammps.reference_resource_worker import acquire
from auto_lammps.remote_resources import INVENTORY_BOOTSTRAP, RemoteResources, tool_bundle
from test_potential_acquisition import Reader, TREE
from test_source_discovery import COMMIT, NAME, response


def entry(path='input.lmp', *, size=20, kind='blob', mode='100644', identity='f'*40):
    row = dict(path=path, sha=identity, type=kind, mode=mode)
    if size is not None:
        row['size'] = size
    return row


class MetadataReader(Reader):
    def __init__(self, entries):
        super().__init__()
        self.entries = entries

    def get(self, endpoint):
        if '/git/trees/' in endpoint:
            self.calls.append(endpoint)
            return response(dict(sha=TREE, truncated=False, tree=self.entries))
        if '/git/blobs/' in endpoint:
            raise AssertionError('This fixture must never download source contents')
        return super().get(endpoint)


class CachedTreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.policy = dict(host_alias='synthetic-hpc', remote_directory=str(self.root/'hpc'),
                           operation_id='c'*32, storage_bytes=512*1024**2)
        self.entries = [
            entry('input.lmp'), entry('scripts', size=None, kind='tree', mode='040000'),
            entry('large.data', size=MAX_BLOB+1), entry('linked', mode='120000'),
            entry('submodule', size=None, kind='commit', mode='160000'),
            entry('../escape'), entry('bad-sha', identity='not-a-sha'),
            entry('bad-size', size=-1), entry('input.lmp'), entry('.git/config'),
            'invalid metadata',
        ]
        self.reader = MetadataReader(self.entries)
        self.transport_calls = []
        self.service = RemoteResources(self.root/'audit', self.policy, capture=self.capture)
        self.receipt = self.service.acquire(NAME, COMMIT)
        self.folder = Path(self.receipt['remote_directory'])
        original = json.loads((self.folder/'intent.json').read_bytes())
        self.request = dict(version=1, acquisition_request=original,
                            receipt_sha256=sha256(canonical(self.receipt)), tree_sha1=TREE)

    def capture(self, command, **options):
        self.transport_calls.append((command, options))
        envelope = json.loads(b''.join(options['input_chunks']))
        if 'version' not in envelope['request']:
            report = acquire(envelope['request'], transport=self.reader)
        else:
            self.assertEqual(command[0], 'ssh')
            with patch('auto_lammps.author_resource_inventory_worker.sys.platform', 'linux'), \
                    patch.dict(os.environ, {'SSH_CONNECTION': 'synthetic offline fixture'}):
                report = inspect_cached_tree(envelope['request'])
        return dict(returncode=0, failure='', stderr='',
                    stdout=base64.b64encode(canonical(report)).decode())

    def inspect(self, request=None):
        with patch('auto_lammps.author_resource_inventory_worker.sys.platform', 'linux'), \
                patch.dict(os.environ, {'SSH_CONNECTION': 'synthetic offline fixture'}):
            return inspect_cached_tree(request or self.request)

    def write(self, name, value):
        path = self.folder/name
        path.write_bytes(canonical(value))
        path.chmod(0o600)

    def cache_json(self, name):
        return json.loads((self.folder/name).read_bytes())

    def alter_response(self, name, change):
        value = self.cache_json(name)
        body = json.loads(base64.b64decode(value['stdout']))
        change(body)
        value['stdout'] = base64.b64encode(canonical(body)).decode()
        self.write(name, value)

    def rewrite_receipt(self, change):
        value = deepcopy(self.receipt)
        change(value)
        self.write('result.json', value)
        self.request['receipt_sha256'] = sha256(canonical(value))

    def test_cache_only_classifies_every_member_and_preserves_request(self):
        before = deepcopy(self.request)
        remote_calls = len(self.reader.calls)
        from auto_lammps.author_resource_inventory_worker import _read
        with patch('auto_lammps.author_resource_inventory_worker._read', wraps=_read) as reads, \
                patch('urllib.request.build_opener', side_effect=AssertionError('no network')), \
                patch('subprocess.Popen', side_effect=AssertionError('no execution')):
            report = self.inspect()
        self.assertEqual(self.request, before)
        self.assertEqual(len(self.reader.calls), remote_calls)
        self.assertEqual([call.args[1] for call in reads.call_args_list], [
            'c'*32+'.lock.json', 'intent.json', 'result.json', '01-request.json',
            '01-response.json', '02-request.json', '02-response.json'])
        self.assertEqual([row['status'] for row in report['entries']], [
            'supported_blob', 'directory', 'oversized_blob', 'nonregular_member',
            'nonregular_member', 'unsafe_path', 'invalid_metadata', 'invalid_metadata',
            'unsafe_path', 'unsafe_path', 'invalid_metadata'])
        self.assertEqual(report['summary']['supported_blob_bytes'], 20)
        self.assertEqual(report['summary']['blocking_entry_count'], 9)
        self.assertEqual(report['summary']['acquisition_limit_reasons'], ['unsupported_members'])
        self.assertEqual(set(report['summary']['cache_sha256']), set(CACHE_KEYS))
        self.assertEqual(RemoteResources.validate_tree_report(self.request, report), report)
        for key in ('B_input', 'execution_authorized', 'physical_evaluation_performed', 'scientific_validation'):
            self.assertIs(report[key], False)
        self.assertFalse(report['summary']['file_contents_read'])
        self.assertFalse(report['summary']['source_complete'])
        self.assertFalse(report['summary']['execution_ready'])

    def test_service_round_trip_keeps_original_acquisition_and_network_counts(self):
        report = self.service.inspect_tree(self.receipt)
        self.assertEqual(report['state'], 'inspected')
        self.assertEqual(len(self.reader.calls), 2)
        self.assertEqual(self.cache_json('result.json'), self.receipt)
        self.assertEqual(self.service.acquire(NAME, COMMIT), self.receipt)
        self.assertEqual(len(self.reader.calls), 2)
        intent = next((self.service.root/self.receipt['id']/'tree-inspections').glob('*-intent.json'))
        self.assertEqual(json.loads(intent.read_bytes())['request'], self.request)

    def test_bundle_contains_the_worker_and_only_trusted_tools(self):
        with zipfile.ZipFile(io.BytesIO(tool_bundle())) as archive:
            self.assertIn('auto_lammps/author_resource_inventory_worker.py', archive.namelist())
            self.assertFalse(any(name.endswith('input.lmp') for name in archive.namelist()))

    def bootstrap(self, request=None):
        import sys
        bundle = tool_bundle()
        envelope = dict(request=request or self.request, bundle_sha256=sha256(bundle),
                        bundle=base64.b64encode(bundle).decode())
        output = io.StringIO()
        opened = []
        original_open = os.open
        original_path = list(sys.path)
        def tracked_open(*args, **kwargs):
            descriptor = original_open(*args, **kwargs)
            opened.append(descriptor)
            return descriptor
        try:
            with patch('sys.platform', 'linux'), \
                    patch.dict(os.environ, {'SSH_CONNECTION': 'synthetic offline fixture'}), \
                    patch('sys.stdin', SimpleNamespace(buffer=io.BytesIO(canonical(envelope)))), \
                    patch('os.open', side_effect=tracked_open), redirect_stdout(output):
                exec(INVENTORY_BOOTSTRAP, {})
            return json.loads(output.getvalue())
        finally:
            sys.path[:] = original_path
            for descriptor in reversed(opened):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def test_bootstrap_rechecks_binding_before_loading_bundle(self):
        report = self.bootstrap()
        self.assertEqual(report['state'], 'inspected')
        self.assertEqual(len(list(self.folder.parent.glob('tools-*.zip'))), 1)
        for request in (dict(self.request, receipt_sha256='e'*64),
                        dict(self.request, version=True)):
            with self.subTest(request=request):
                with self.assertRaises(ValueError):
                    self.bootstrap(request)

    def test_bootstrap_refuses_symlinked_bundle_even_with_correct_digest(self):
        bundle = tool_bundle()
        real = self.folder/'untrusted-tools.zip'
        real.write_bytes(bundle)
        real.chmod(0o400)
        target = self.folder.parent/('tools-'+sha256(bundle)+'.zip')
        target.symlink_to(real)
        with self.assertRaises(OSError):
            self.bootstrap()

    def test_maximum_tree_with_long_paths_survives_controller_transport(self):
        self.reader.entries = [entry('a'*490+str(index).zfill(10), size=0) for index in range(2000)]
        policy = dict(self.policy, operation_id='d'*32)
        service = RemoteResources(self.root/'audit', policy, capture=self.capture)
        receipt = service.acquire(NAME, COMMIT)
        report = service.inspect_tree(receipt)
        self.assertEqual(report['state'], 'inspected')
        self.assertEqual(len(report['entries']), 2000)
        self.assertEqual(report['summary']['acquisition_limit_reasons'], ['source_file_count_limit'])
        self.assertLess(len(canonical(report)), self.transport_calls[-1][1]['max_bytes'])

    def test_service_rejects_changed_original_identities_before_transport(self):
        calls = len(self.transport_calls)
        for key, value in (('id', 'd'*32), ('repository', 'other/repository'),
                           ('commit', 'f'*40), ('bundle_sha256', 'd'*64),
                           ('remote_directory', self.receipt['remote_directory']+'/other'),
                           ('request_sha256', 'e'*64)):
            with self.subTest(key=key):
                receipt = dict(self.receipt, **{key: value})
                with self.assertRaises(AcquisitionError):
                    self.service.inspect_tree(receipt)
        self.assertEqual(len(self.transport_calls), calls)

    def test_service_requires_original_local_intent(self):
        intent = self.service.root/self.receipt['id']/'intent.json'
        intent.unlink()
        with self.assertRaisesRegex(AcquisitionError, 'intent_unavailable'):
            self.service.inspect_tree(self.receipt)
        self.assertEqual(len(self.transport_calls), 1)

    def test_service_rejects_changed_local_transport_binding(self):
        intent = self.service.root/self.receipt['id']/'intent.json'
        value = json.loads(intent.read_bytes())
        value['host_alias'] = 'another-host'
        intent.write_bytes(canonical(value))
        with self.assertRaisesRegex(AcquisitionError, 'intent_mismatch'):
            self.service.inspect_tree(self.receipt)
        self.assertEqual(len(self.transport_calls), 1)

    def test_remote_rejects_changed_request_and_receipt_digest(self):
        for key, value in (('receipt_sha256', 'e'*64), ('tree_sha1', 'f'*40), ('version', True)):
            with self.subTest(key=key):
                request = dict(self.request, **{key: value})
                with self.assertRaises(AcquisitionError):
                    self.inspect(request)
        for key, value in (('id', 'd'*32), ('commit', 'd'*40), ('repository', 'other/repo'),
                           ('bundle_sha256', 'e'*64), ('storage_bytes', 1024**3)):
            with self.subTest(original=key):
                request = deepcopy(self.request)
                request['acquisition_request'][key] = value
                with self.assertRaises(AcquisitionError):
                    self.inspect(request)

    def test_intent_and_lock_are_exact_not_adopted(self):
        original = self.cache_json('intent.json')
        altered = dict(original, commit='d'*40)
        self.write('intent.json', altered)
        with self.assertRaisesRegex(AcquisitionError, 'intent_mismatch'):
            self.inspect()
        self.write('intent.json', original)
        lock = self.folder.parent/(original['id']+'.lock.json')
        lock.write_bytes(canonical(altered))
        with self.assertRaisesRegex(AcquisitionError, 'lock_mismatch'):
            self.inspect()

    def test_receipt_cannot_rebind_original_commit_or_flags(self):
        for key, value in (('commit', 'e'*40), ('request_sha256', 'e'*64),
                           ('source_bytes', 1), ('request_count', True),
                           ('scientific_validation', 0), ('location', 'local')):
            with self.subTest(key=key):
                self.rewrite_receipt(lambda receipt: receipt.update({key: value}))
                with self.assertRaises(AcquisitionError):
                    self.inspect()

    def test_cache_requests_cannot_name_other_repositories_or_blobs(self):
        for name, endpoint in (('01-request.json', 'repos/other/repo/git/commits/'+COMMIT),
                                ('02-request.json', 'repos/'+NAME+'/git/blobs/'+'f'*40)):
            with self.subTest(name=name):
                original = self.cache_json(name)
                self.write(name, dict(endpoint=endpoint))
                with self.assertRaises(AcquisitionError):
                    self.inspect()
                self.write(name, original)

    def test_commit_and_tree_hashes_must_match_the_original_receipt(self):
        for name, change in (
                ('01-response.json', lambda value: value.update(sha='e'*40)),
                ('01-response.json', lambda value: value.update(tree={'sha': 'e'*40})),
                ('02-response.json', lambda value: value.update(sha='e'*40)),
                ('02-response.json', lambda value: value.update(truncated=True))):
            with self.subTest(name=name):
                original = self.cache_json(name)
                self.alter_response(name, change)
                with self.assertRaises(AcquisitionError):
                    self.inspect()
                self.write(name, original)

    def test_corrupt_base64_and_failed_response_never_become_an_inventory(self):
        original = self.cache_json('02-response.json')
        for change in ({'stdout': '!not-base64!'}, {'stdout': 'e31='},
                       {'returncode': 1}, {'failure': 'timeout'}, {'stderr': 'eA=='},
                       {'stdout': base64.b64encode(b'{"sha":1,"sha":2}').decode()}):
            with self.subTest(change=change):
                self.write('02-response.json', dict(original, **change))
                with self.assertRaises(AcquisitionError):
                    self.inspect()

    def test_transfer_statistics_cannot_be_less_than_inspected_metadata(self):
        self.rewrite_receipt(lambda receipt: receipt.update(received_bytes=1))
        with self.assertRaisesRegex(AcquisitionError, 'accounting_mismatch'):
            self.inspect()

    def test_symlink_cache_and_hardlink_receipt_rejected(self):
        cache = self.folder/'02-response.json'
        saved = self.folder/'saved-response.json'
        cache.rename(saved)
        cache.symlink_to(saved)
        with self.assertRaises(AcquisitionError):
            self.inspect()
        cache.unlink()
        saved.rename(cache)
        os.link(self.folder/'result.json', self.folder/'receipt-copy.json')
        with self.assertRaisesRegex(AcquisitionError, 'unsafe_cached_tree_record'):
            self.inspect()

    def test_nonprivate_directory_file_and_foreign_owner_rejected(self):
        for path in (self.folder.parent, self.folder, self.folder/'02-response.json'):
            with self.subTest(path=path.name):
                old = path.stat().st_mode
                path.chmod(0o755 if path.is_dir() else 0o644)
                with self.assertRaises(AcquisitionError):
                    self.inspect()
                path.chmod(old & 0o777)
        current_uid = os.getuid()
        with patch('auto_lammps.author_resource_inventory_worker.os.getuid', return_value=current_uid+1):
            with self.assertRaisesRegex(AcquisitionError, 'private_owned_directory'):
                self.inspect()

    def test_symlink_folder_and_ancestor_rejected(self):
        saved = self.folder.with_name('saved')
        self.folder.rename(saved)
        self.folder.symlink_to(saved, target_is_directory=True)
        with self.assertRaises(AcquisitionError):
            self.inspect()
        self.folder.unlink()
        saved.rename(self.folder)
        alias = self.root/'hpc-alias'
        alias.symlink_to(self.folder.parent, target_is_directory=True)
        request = deepcopy(self.request)
        request['acquisition_request']['directory'] = str(alias)
        with self.assertRaises(AcquisitionError):
            self.inspect(request)

    def test_special_metadata_file_is_rejected_without_blocking(self):
        cache = self.folder/'02-response.json'
        cache.unlink()
        os.mkfifo(cache, 0o600)
        with self.assertRaisesRegex(AcquisitionError, 'unsafe_cached_tree_record'):
            self.inspect()

    def test_not_a_general_local_directory_reader(self):
        with patch('auto_lammps.author_resource_inventory_worker.sys.platform', 'darwin'):
            with self.assertRaisesRegex(AcquisitionError, 'requires_HPC'):
                inspect_cached_tree(self.request)
        with patch('auto_lammps.author_resource_inventory_worker.sys.platform', 'linux'), \
                patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(AcquisitionError, 'requires_HPC'):
                inspect_cached_tree(self.request)

    def test_report_tampering_rejected_and_retained_as_unknown(self):
        legitimate = self.inspect()
        changes = [
            lambda report: report.update(commit='e'*40),
            lambda report: report.update(execution_authorized=True),
            lambda report: report.update(B_input=0),
            lambda report: report['entries'][2].update(status='supported_blob'),
            lambda report: report['entries'][0].update(size=True),
            lambda report: report['summary'].update(entry_count=True),
            lambda report: report['summary']['counts'].update(directory=True),
            lambda report: report['summary'].update(supported_blob_bytes=21),
            lambda report: report['summary'].update(execution_ready=True),
            lambda report: report['summary']['cache_sha256'].update(tree_response='invalid'),
        ]
        for change in changes:
            report = deepcopy(legitimate)
            change(report)
            with self.subTest(report=report):
                with self.assertRaises(AcquisitionError):
                    RemoteResources.validate_tree_report(self.request, report)
        def corrupt_capture(*args, **kwargs):
            report = deepcopy(legitimate)
            report['entries'][2]['status'] = 'supported_blob'
            return dict(returncode=0, failure='', stderr='',
                        stdout=base64.b64encode(canonical(report)).decode())
        self.service.capture = corrupt_capture
        result = self.service.inspect_tree(self.receipt)
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(result['entries'], [])
        self.assertEqual(result['observation_failure'], 'invalid_cached_tree_report')

    def test_local_transport_uncertainty_cannot_redownload_or_claim_success(self):
        def lost(*args, **kwargs):
            raise OSError('synthetic unavailable transport')
        self.service.capture = lost
        result = self.service.inspect_tree(self.receipt)
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(result['observation_failure'], 'OSError')
        self.assertEqual(len(self.reader.calls), 2)


class InventoryBoundariesTests(unittest.TestCase):
    def summary(self, entries):
        rows = classify_entries(entries)
        return rows, inventory_summary(rows, {key: 'a'*64 for key in CACHE_KEYS})

    def test_size_limit_is_inclusive_and_totals_explain_full_tree_limits(self):
        rows, summary = self.summary([entry('at-limit', size=MAX_BLOB),
                                      entry('over-limit', size=MAX_BLOB+1)])
        self.assertEqual([row['status'] for row in rows], ['supported_blob', 'oversized_blob'])
        self.assertEqual(summary['supported_blob_bytes'], MAX_BLOB)
        _, summary = self.summary([entry(str(i), size=0) for i in range(129)])
        self.assertEqual(summary['acquisition_limit_reasons'], ['source_file_count_limit'])
        _, summary = self.summary([entry(str(i), size=MAX_BLOB) for i in range(5)])
        self.assertEqual(summary['acquisition_limit_reasons'], ['source_bytes_limit'])
        _, summary = self.summary([entry(str(i), size=MAX_BLOB) for i in range(4)])
        self.assertEqual(summary['supported_blob_bytes'], LIMITS['source_bytes'])
        self.assertEqual(summary['acquisition_limit_reasons'], [])

    def test_empty_and_tree_count_boundaries(self):
        rows, summary = self.summary([])
        self.assertEqual(rows, [])
        self.assertEqual(summary['acquisition_limit_reasons'], ['no_supported_blobs'])
        rows, summary = self.summary([entry(str(i), size=0) for i in range(2000)])
        self.assertEqual(len(rows), 2000)
        self.assertEqual(sum(summary['counts'].values()), 2000)
        with self.assertRaises(AcquisitionError):
            self.summary([entry(str(i)) for i in range(2001)])

    def test_malformed_entries_are_bounded_and_not_a_global_failure(self):
        rows, summary = self.summary([
            None, {}, entry('unicode-\u5b57\n'), entry('a'*501), entry('x', size=True),
            entry('dir', size='invalid', kind='tree', mode='040000'),
            entry('a', identity={'not': 'scalar'}), entry('ok'),
        ])
        self.assertEqual(len(rows), 8)
        self.assertEqual(rows[-1]['status'], 'supported_blob')
        self.assertEqual(summary['blocking_entry_count'], 7)
        self.assertTrue(all(isinstance(row['path'], str) and row['path'].isascii()
                            and len(row['path']) <= 500 for row in rows))
        self.assertEqual(classify_entries(rows), rows)


if __name__ == '__main__':
    unittest.main()
