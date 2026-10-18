"""Selected source inspection uses synthetic cached metadata/files only."""
import base64
from copy import deepcopy
from contextlib import contextmanager, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import zipfile

from auto_lammps import selected_author_source_worker as worker
from auto_lammps.author_reference_plan import author_source_inputs
from auto_lammps.author_source_worker import AuthorSourceError
from auto_lammps.manifest import canonical, sha256
from auto_lammps.selected_author_source_reader import (
    SelectedAuthorSourceReader, selected_reader_tool_bundle, BOOTSTRAP)
from auto_lammps.tasks import TaskError
from test_reference_selected_resources import SelectedResourceTests
from test_author_source_reader import AuthorSourceReaderTests


class SelectedAuthorSourceReaderTests(unittest.TestCase):
    def setUp(self):
        self.fixture = SelectedResourceTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.selection = self.fixture.request(3)
        self.receipt = self.fixture.run_worker(self.selection)
        self.task = 'b'*32; self.calls = []
        self.policy = dict(host_alias='offline-hpc', remote_directory=str(self.fixture.hpc),
            reference_task_id=self.task, repository=self.receipt['repository'], commit=self.receipt['commit'],
            license_approved=True, license_approval_sha256='d'*64,
            selection_request_sha256=sha256(canonical(self.selection)), receipt_sha256=sha256(canonical(self.receipt)))
        self.reader = self.service()

    def service(self, **changes):
        return SelectedAuthorSourceReader(self.fixture.root/'source-audit', changes.pop('policy', self.policy),
            selection_request=changes.pop('selection_request', self.selection),
            tree_inventory=changes.pop('tree_inventory', self.fixture.inventory),
            original_receipt=changes.pop('original_receipt', self.fixture.old),
            transport=changes.pop('transport', self.fixture.transport), capture=changes.pop('capture', self.capture), **changes)

    @contextmanager
    def hpc(self):
        with patch('auto_lammps.selected_author_source_worker.sys.platform', 'linux'), \
                patch.dict(os.environ, {'SSH_CONNECTION':'offline synthetic'}):
            yield

    def choices(self):
        return [dict(path=row['path'], start_line=1, end_line=20) for row in self.receipt['files']]

    def capture(self, command, **options):
        self.assertEqual(command[0], 'ssh'); self.assertIn('offline-only', command)
        envelope = json.loads(b''.join(options['input_chunks'])); self.calls.append(envelope['request'])
        raw = base64.b64decode(envelope['bundle']); self.assertEqual(sha256(raw), envelope['bundle_sha256'])
        with self.hpc(): result = worker.read_source_report(envelope['request'])
        return dict(returncode=0, failure='', stdout=base64.b64encode(canonical(result)).decode(), stderr='')

    def retained(self):
        request = self.reader.selection_request(self.receipt, self.choices())
        response = self.reader.read(self.receipt, self.choices(), purpose='author_reference')
        return self.reader.retained(self.receipt, [dict(request=request, response=response)])

    def test_selected_originals_read_without_promoting_original_partial_acquisition(self):
        with patch('urllib.request.build_opener', side_effect=AssertionError('no network')), \
                patch('subprocess.Popen', side_effect=AssertionError('no execution')):
            value = self.retained(); checked = author_source_inputs(value, self.task)
        self.assertEqual(len(checked['files']), 3); self.assertEqual(len(checked['sources']), 3)
        self.assertFalse(checked['capability']['source_complete'])
        self.assertTrue(checked['capability']['selected_complete'])
        self.assertFalse(self.fixture.old['source_complete'])
        self.assertEqual((Path(self.fixture.old['remote_directory'])/'result.json').read_bytes(), self.fixture.original_bytes)
        for row in value['reads'][0]['response']['files']:
            self.assertEqual(row['text'].encode(), self.fixture.data[row['path']])
        for path in (self.fixture.root/'source-audit').rglob('*'):
            if path.is_file(): self.assertNotIn(b'synthetic original', path.read_bytes())

    def test_no_legacy_reader_or_worker_byte_identity_is_changed(self):
        from auto_lammps import author_source_reader, author_source_worker
        bundle = selected_reader_tool_bundle()
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            for module in (author_source_worker, worker):
                self.assertEqual(archive.read('auto_lammps/'+Path(module.__file__).name), Path(module.__file__).read_bytes())
            self.assertIn('third_party/SIGA-LAMMPS-LICENSE.txt', archive.namelist())
            self.assertNotIn(self.receipt['files'][0]['path'], archive.namelist())
        self.assertIn('auto_lammps.selected_author_source_worker', BOOTSTRAP)
        self.assertNotEqual(BOOTSTRAP, author_source_reader.BOOTSTRAP)
        compile(BOOTSTRAP, '<selected-reader>', 'exec')

    def test_actual_fixed_bootstrap_reads_only_temp_selected_originals(self):
        request = self.reader.selection_request(self.receipt, self.choices())
        bundle = selected_reader_tool_bundle()
        raw = canonical(dict(request=request, bundle=base64.b64encode(bundle).decode(), bundle_sha256=sha256(bundle)))
        class Input:
            buffer = io.BytesIO(raw)
        output = io.StringIO(); original_path = list(sys.path)
        try:
            with self.hpc(), patch.object(sys, 'stdin', Input()), redirect_stdout(output), \
                    patch('urllib.request.build_opener', side_effect=AssertionError('no network')), \
                    patch('subprocess.Popen', side_effect=AssertionError('no author execution')):
                exec(BOOTSTRAP, {})
        finally:
            sys.path[:] = original_path
        report = json.loads(output.getvalue())
        self.assertEqual(report['state'], 'read'); self.assertFalse(report['source_complete'])
        self.assertEqual(len(report['files']), 3)

    def test_changed_original_cached_tree_is_not_replaced_by_selected_receipt(self):
        request = self.reader.selection_request(self.receipt, self.choices())
        # Same fixed basename used by the existing cached-tree inspector.
        target = Path(self.fixture.old['remote_directory'])/'02-response.json'
        target.chmod(0o600)
        target.write_bytes(b'{}'); target.chmod(0o400)
        with self.hpc(): report = worker.read_source_report(request)
        self.assertEqual(report['state'], 'rejected'); self.assertEqual(report['files'], [])

    def test_approval_task_tree_and_exact_selected_identity_are_not_interchangeable(self):
        for name in ('license', 'selection', 'tree', 'original', 'connection'):
            with self.subTest(name=name):
                changes = {}
                if name == 'license': changes['policy'] = dict(self.policy, license_approved=False)
                if name == 'selection':
                    value = deepcopy(self.selection); value['selected'][0]['blob_sha1'] = '0'*40
                    changes['selection_request'] = value
                if name == 'tree':
                    value = deepcopy(self.fixture.inventory); value['entries'][0]['sha'] = '0'*40
                    changes['tree_inventory'] = value
                if name == 'original':
                    value = deepcopy(self.fixture.old); value['commit'] = '0'*40
                    changes['original_receipt'] = value
                if name == 'connection': changes['transport'] = None
                with self.assertRaises(ValueError): self.service(**changes)
        self.assertEqual(self.calls, [])
        value = self.retained()
        with self.assertRaises(TaskError): author_source_inputs(value, 'e'*32)

    def test_reference_only_and_limits_fail_before_transport(self):
        for purpose in ('B', 'agent', '', None):
            with self.subTest(purpose=purpose), self.assertRaises(AuthorSourceError):
                self.reader.read(self.receipt, self.choices(), purpose=purpose)
            with self.assertRaises(AuthorSourceError): self.reader.capability(self.receipt, purpose=purpose)
        for choices in ([dict(path='../secret', start_line=1, end_line=1)],
                        [dict(path=self.choices()[0]['path'], start_line=1, end_line=401)], self.choices()*3):
            with self.subTest(choices=choices), self.assertRaises(ValueError):
                self.reader.read(self.receipt, choices, purpose='author_reference')
        self.assertEqual(self.calls, [])

    def test_selected_unknown_or_partial_receipt_is_not_complete(self):
        for state in ('unknown', 'partial'):
            receipt = deepcopy(self.receipt); receipt.update(state=state, selected_complete=False)
            with self.subTest(state=state), self.assertRaises(ValueError):
                self.reader.capability(receipt, purpose='author_reference')
        value = self.retained()
        for key in ('source_complete', 'selected_complete', 'selection_sha256', 'transport_binding_sha256', 'license_approval_sha256'):
            changed = deepcopy(value)
            changed['capability'][key] = True if key == 'source_complete' else False if key == 'selected_complete' else '0'*64
            with self.subTest(key=key), self.assertRaises(TaskError): author_source_inputs(changed, self.task)

    def test_transport_change_cannot_migrate_an_existing_selected_read(self):
        self.retained()
        self.fixture.transport.identity = dict(self.fixture.transport.identity, revision=2)
        other = self.service()
        with self.assertRaises(ValueError): other.read(self.receipt, self.choices(), purpose='author_reference')
        self.assertEqual(len(self.calls), 1)

    def test_remote_original_intent_and_saved_receipt_are_rechecked(self):
        request = self.reader.selection_request(self.receipt, self.choices())
        folder = Path(self.receipt['remote_directory'])
        for filename in ('intent.json', 'result.json'):
            path = folder/filename; old = path.read_bytes(); path.chmod(0o600)
            value = json.loads(old); value['id'] = 'f'*32; path.write_bytes(canonical(value)); path.chmod(0o400)
            with self.subTest(filename=filename), self.hpc():
                report = worker.read_source_report(request)
                self.assertEqual(report['state'], 'rejected'); self.assertEqual(report['files'], [])
            path.chmod(0o600); path.write_bytes(old); path.chmod(0o400)

    def test_resumed_49_originals_use_exact_chained_receipt_and_keep_initial_19(self):
        # Match the observed large-blob size without using author content.
        fixture=SelectedResourceTests()
        fixture.synthetic_contents={f'files/text-{index:03d}.txt':f'synthetic {index}\n'.encode() for index in range(48)}
        fixture.synthetic_contents['logs/large.log']=b'x'*11_205_503
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        selection=fixture.request(49)
        fixture.blobs.change=lambda value,count:value.update(sha='f'*40) if count==20 else None
        partial=fixture.run_worker(selection)
        self.assertEqual(len(partial['files']),19)
        folder=Path(partial['remote_directory']);old=(folder/'result.json').read_bytes()
        fixture.blobs.change=None
        complete=fixture.run_worker(selection,action='resume',previous_bundle_sha256='e'*64)
        self.assertEqual(len(complete['files']),49)
        self.assertEqual(complete['files'][:19],partial['files'])
        policy=dict(self.policy,remote_directory=str(fixture.hpc),
            selection_request_sha256=sha256(canonical(selection)),receipt_sha256=sha256(canonical(complete)))
        reader=SelectedAuthorSourceReader(fixture.root/'source-audit',policy,selection_request=selection,
            tree_inventory=fixture.inventory,original_receipt=fixture.old,transport=fixture.transport,
            capture=lambda *args,**kwargs:self.fail('no actual transport'))
        choices=[dict(path=row['path'],start_line=1,end_line=20) for row in complete['files'][:8]]
        request=reader.selection_request(complete,choices)
        with self.hpc():report=worker.read_source_report(request)
        self.assertEqual(report['state'],'read');self.assertEqual(len(report['files']),8)
        self.assertEqual((folder/'result.json').read_bytes(),old)
        stale=dict(request,receipt_sha256=sha256(old))
        with self.hpc():rejected=worker.read_source_report(stale)
        self.assertEqual(rejected['state'],'rejected')
        self.assertEqual(rejected['rejection_reason'],'selected_source_saved_receipt_changed')
        resume=folder/'resume-001-intent.json';saved=resume.read_bytes()
        wrong=json.loads(saved);wrong['previous_receipt_sha256']='0'*64
        resume.chmod(0o600);resume.write_bytes(canonical(wrong));resume.chmod(0o400)
        with self.hpc():rejected=worker.read_source_report(request)
        self.assertEqual(rejected['state'],'rejected')
        self.assertEqual(rejected['rejection_reason'],'selected_source_resume_chain_changed')
        self.assertEqual(rejected['files'],[])

    def test_missing_or_nonsequential_resume_cannot_fall_back_to_older_receipt(self):
        request=self.reader.selection_request(self.receipt,self.choices())
        folder=Path(self.receipt['remote_directory'])
        intent=dict(request_sha256=sha256(canonical(self.selection)),
            previous_receipt_sha256=sha256(canonical(self.receipt)),previous_bundle_sha256='e'*64)
        path=folder/'resume-002-intent.json';path.write_bytes(canonical(intent));path.chmod(0o400)
        with self.hpc():report=worker.read_source_report(request)
        self.assertEqual(report['state'],'rejected')
        self.assertEqual(report['rejection_reason'],'selected_source_resume_chain_changed')
        self.assertEqual(report['files'],[])

    def test_original_size_sha_blob_and_symlink_tampering_cannot_return_text(self):
        request = self.reader.selection_request(self.receipt, self.choices())
        path = Path(self.receipt['remote_directory'])/'source'/self.receipt['files'][0]['path']
        old = path.read_bytes(); path.chmod(0o600); path.write_bytes(b'X'*len(old)); path.chmod(0o400)
        with self.hpc(): report = worker.read_source_report(request)
        self.assertEqual(report['state'], 'rejected'); self.assertEqual(report['files'], [])
        path.unlink(); outside = self.fixture.root/'outside.txt'; outside.write_bytes(old); outside.chmod(0o400)
        path.symlink_to(outside)
        with self.hpc(): report = worker.read_source_report(request)
        self.assertEqual(report['state'], 'rejected'); self.assertEqual(report['files'], [])

    def test_retained_text_or_request_tampering_is_rejected(self):
        value = self.retained()
        for name in ('text', 'task', 'blob', 'request', 'response'):
            changed = deepcopy(value)
            if name == 'text': changed['reads'][0]['response']['files'][0]['text'] += 'changed'
            if name == 'task': changed['reads'][0]['request']['reference_task_id'] = '0'*32
            if name == 'blob': changed['acquisition_receipt']['files'][0]['blob_sha1'] = '0'*40
            if name == 'request': changed['selection_request']['selected'][0]['path'] = 'outside.txt'
            if name == 'response': changed['reads'][0]['response']['source_complete'] = True
            with self.subTest(name=name), self.assertRaises(TaskError): author_source_inputs(changed, self.task)

    def test_existing_23_originals_and_selected_new_files_keep_independent_packages(self):
        old = AuthorSourceReaderTests(); old.setUp(); self.addCleanup(old.doCleanups)
        old.texts = {f'old/file-{index:02d}.txt': f'old original {index}\n'.encode() for index in range(23)}
        (old.source/'old').mkdir(mode=0o700)
        old.policy.update(reference_task_id=self.task, repository=self.receipt['repository'], commit=self.receipt['commit'])
        old._write_sources(); old._bind_receipt()
        choice = [dict(path='old/file-00.txt', start_line=1, end_line=20)]
        request = old.service()._selection(old.receipt, choice)
        previous = dict(capability=old.service().capability(old.receipt, purpose='author_reference'),
            acquisition_receipt=old.receipt, reads=[dict(request=request, response=old.worker(request))])
        value = dict(source_packages=[previous, self.retained()])
        checked = author_source_inputs(value, self.task)
        self.assertEqual(len(checked['files']), 26); self.assertEqual(len(checked['capability']['members']), 2)
        self.assertEqual(len(checked['sources']), 4)
        self.assertFalse(checked['capability']['members'][1]['source_complete'])
        self.assertEqual(checked['capability']['members'][0]['adapter'], 'pinned_author_source_reader')


if __name__ == '__main__': unittest.main()
