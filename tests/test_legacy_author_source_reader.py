"""Synthetic old selections, saved transport, and real local helper processes."""
import base64
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from auto_lammps.author_source_worker import AuthorSourceError, MAX_TEXT_BYTES
from auto_lammps.hpc_transport import SavedHPCTransport
from auto_lammps.legacy_author_source_reader import (
    BOOTSTRAP, LegacyAuthorSourceReader, reader_tool_bundle, validated_inputs,
)
from auto_lammps import legacy_author_source_worker as worker
from auto_lammps.manifest import canonical, sha256
from auto_lammps.potential_acquisition import AcquisitionError
from auto_lammps.slurm_read import _capture
from test_reference_saved_transport import saved_transport


def git_blob(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode('ascii') + b'\0' + data).hexdigest()


class LegacyAuthorSourceReaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.commit = 'c' * 40
        self.remote = self.root/'legacy'/self.commit
        self.remote.mkdir(mode=0o700, parents=True)
        self.source = self.remote/'source'
        self.source.mkdir(mode=0o700)
        (self.source/'workflow').mkdir(mode=0o700)
        self.texts = {'README.md': b'SYNTHETIC_PRIVATE_SOURCE_CANARY\nSecond complete line.\nLast line.',
                      'workflow/entry.py': '第一行\r\nraise RuntimeError("AUTHOR_MUST_NOT_EXECUTE")\r\n'.encode()}
        self.policy = dict(host_alias='synthetic-hpc', remote_directory=str(self.remote),
            reference_task_id='b'*32, repository='example/synthetic-legacy', commit=self.commit,
            receipt_sha256='', receipt_document_sha256='', intent_sha256='', selection_sha256='',
            inventory_sha256='', license_approved=True, license_approval_sha256='d'*64)
        self.refresh()
        self.transport = saved_transport(self.root, self.policy['host_alias'])
        self.calls = 0

    def refresh(self, *, bad_blob=False):
        files, selected = [], []
        for name, data in self.texts.items():
            path = self.source/name
            if path.exists():
                path.chmod(0o600)
            path.write_bytes(data)
            path.chmod(0o400)
            blob = '0'*40 if bad_blob and name == 'README.md' else git_blob(data)
            files.append(dict(path=name, size=len(data), blob_sha1=blob, sha256=sha256(data)))
            selected.append(dict(path=name, size=len(data), sha=blob, type='blob',
                mode='100755' if name.endswith('.py') else '100644',
                url='https://api.github.com/repos/example/synthetic-legacy/git/blobs/'+blob))
        plan = dict(repository=self.policy['repository'], commit=self.commit, label='Synthetic-legacy',
                    tree_sha1='e'*40, selected=selected)
        self.intent = dict(plan=plan, selection_sha256=sha256(canonical(plan)))
        self.receipt = dict(repository=self.policy['repository'], commit=self.commit, tree_sha1='e'*40,
            selection_sha256=self.intent['selection_sha256'], remote_directory=str(self.source), files=files,
            bytes=sum(row['size'] for row in files), request_count=4, whole_repository_downloaded=False,
            selection_complete=True, source_executed=False, scientific_validation=False, cached_reuse=False)
        self.receipt_path = self.remote/'selected-resource-receipt.json'
        if self.receipt_path.exists():
            self.receipt_path.chmod(0o600)
        self.receipt_path.write_text(json.dumps(self.receipt, indent=2))
        self.receipt_path.chmod(0o400)
        self.policy.update(receipt_sha256=sha256(self.receipt_path.read_bytes()),
            receipt_document_sha256=sha256(canonical(self.receipt)), intent_sha256=sha256(canonical(self.intent)),
            selection_sha256=self.intent['selection_sha256'], inventory_sha256=sha256(canonical(files)))

    def service(self, *, policy=None, transport=None, capture=None):
        return LegacyAuthorSourceReader(self.root/'audit', policy or self.policy,
            transport=transport or self.transport, capture=capture or self.capture)

    def selections(self):
        return [dict(path='README.md', start_line=1, end_line=2)]

    def request(self, selections=None):
        return self.service().selection_request(self.receipt, self.intent, selections or self.selections())

    def worker(self, request, intent=None):
        with patch('auto_lammps.legacy_author_source_worker.sys.platform', 'linux'), \
                patch.dict(os.environ, {'SSH_CONNECTION': 'synthetic'}):
            return worker.read_source_report(request, intent or self.intent)

    def capture(self, command, **options):
        self.calls += 1
        self.assertEqual(command[0], 'ssh')
        self.assertIn('saved.example.edu', command)
        self.assertNotIn(str(self.remote), command[-1])
        envelope = json.loads(b''.join(options['input_chunks']))
        bundle = base64.b64decode(envelope['bundle'], validate=True)
        self.assertEqual(sha256(bundle), envelope['bundle_sha256'])
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            self.assertIn('auto_lammps/legacy_author_source_worker.py', archive.namelist())
            self.assertNotIn('README.md', archive.namelist())
            self.assertNotIn('workflow/entry.py', archive.namelist())
        report = self.worker(envelope['request'], envelope['legacy_staging_intent'])
        return dict(returncode=0, failure='', stdout=base64.b64encode(canonical(report)).decode(), stderr='')

    def read(self, *, service=None, selections=None):
        return (service or self.service()).read(self.receipt, self.intent,
            selections or self.selections(), purpose='author_reference')

    def inputs(self, *, selections=None):
        reader = self.service()
        chosen = selections or self.selections()
        request = reader.selection_request(self.receipt, self.intent, chosen)
        response = self.read(service=reader, selections=chosen)
        return dict(capability=reader.capability(self.receipt, self.intent, purpose='author_reference'),
            acquisition_receipt=deepcopy(self.receipt), legacy_staging_intent=deepcopy(self.intent),
            reads=[dict(request=request, response=response)])

    def test_legacy_documents_and_originals_stay_unchanged_without_standard_acquisition(self):
        before = {p: (p.read_bytes(), p.stat().st_mode) for p in self.remote.rglob('*') if p.is_file()}
        old_receipt, old_intent = deepcopy(self.receipt), deepcopy(self.intent)
        report = self.read()
        self.assertEqual(report['state'], 'read', report)
        self.assertEqual(report['files'][0]['text'], 'SYNTHETIC_PRIVATE_SOURCE_CANARY\nSecond complete line.\n')
        self.assertEqual(report['source_format'], 'legacy_selected_resource_v3')
        self.assertFalse(report['whole_repository_downloaded'])
        self.assertFalse(report['execution_authorized'])
        self.assertFalse(report['physical_evaluation_performed'])
        self.assertEqual(self.receipt, old_receipt)
        self.assertEqual(self.intent, old_intent)
        self.assertNotIn('schema_version', self.receipt)
        self.assertNotIn('id', self.receipt)
        self.assertFalse((self.remote/'result.json').exists())
        for path, original in before.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mode), original)
        for path in (self.root/'audit').rglob('*.json'):
            data = path.read_text()
            self.assertNotIn('SYNTHETIC_PRIVATE_SOURCE_CANARY', data)
            self.assertNotIn('SYNTHETIC_HOST_KEY', data)
        audit = json.loads(next((self.root/'audit').rglob('*-result.json')).read_bytes())
        self.assertFalse(audit['returned_text_persisted'])
        self.assertNotIn('text', audit['files'][0])

    def test_actual_capture_bootstrap_bundle_and_worker_share_one_json_protocol(self):
        calls = []

        def local_process(command, **options):
            calls.append(command)
            remote = shlex.split(command[-1])
            self.assertEqual(remote[:3], ['python3', '-I', '-c'])
            self.assertEqual(remote[3], BOOTSTRAP)
            # Emulate only the SSH/Linux host gate on synthetic local files. The
            # actual bootstrap, ZIP imports, descriptor reads and _capture run.
            prefix = 'import sys,os;sys.platform="linux";os.environ["SSH_CONNECTION"]="synthetic";\n'
            return _capture([sys.executable, '-I', '-c', prefix+remote[3]], **options)

        before = self.receipt_path.read_bytes()
        report = self.read(service=self.service(capture=local_process))
        self.assertEqual(report['state'], 'read', report)
        self.assertEqual(report['files'][0]['text_sha256'], sha256(report['files'][0]['text'].encode()))
        self.assertEqual(len(calls), 1)
        bundles = list(self.remote.glob('legacy-reader-tools-*.zip'))
        self.assertEqual(len(bundles), 1)
        self.assertEqual(bundles[0].read_bytes(), reader_tool_bundle())
        self.assertEqual(self.receipt_path.read_bytes(), before)
        self.assertEqual((self.source/'workflow/entry.py').read_bytes(), self.texts['workflow/entry.py'])

    def test_capability_and_validated_inputs_retain_legacy_identity_and_source_locations(self):
        value = self.inputs()
        cap = value['capability']
        self.assertEqual(cap['adapter'], 'pinned_legacy_author_source_reader')
        self.assertEqual(cap['binding']['receipt_sha256'], self.policy['receipt_sha256'])
        self.assertEqual(cap['binding']['intent_sha256'], self.policy['intent_sha256'])
        self.assertEqual(cap['binding']['transport_binding']['revision'], 1)
        self.assertFalse(cap['whole_repository_downloaded'])
        self.assertEqual(cap['operation']['maximum_files'], 8)
        self.assertEqual(cap['operation']['maximum_lines_per_file'], 400)
        self.assertEqual(cap['operation']['maximum_text_bytes'], 256*1024)
        projected = validated_inputs(value, self.policy['reference_task_id'])
        self.assertEqual(set(projected), {'capability', 'sources', 'files', 'reads'})
        source = projected['sources'][0]
        self.assertEqual(source['origin'], 'author_source')
        self.assertEqual(source['locator'], 'README.md:1-2')
        self.assertEqual(source['source_sha256'], sha256(canonical({k:v for k,v in source.items() if k!='source_sha256'})))
        proof = projected['reads'][0]
        self.assertEqual(proof['source_id'], source['id'])
        self.assertEqual(proof['blob_sha1'], git_blob(self.texts['README.md']))
        self.assertEqual(proof['receipt_sha256'], self.policy['receipt_sha256'])
        self.assertTrue(proof['truncated'])

    def test_UTF8_CRLF_full_file_and_empty_file_are_byte_faithful(self):
        response = self.read(selections=[dict(path='workflow/entry.py', start_line=1, end_line=20)])
        row = response['files'][0]
        self.assertEqual(row['text'].encode(), self.texts['workflow/entry.py'])
        self.assertEqual(row['total_lines'], 2)
        self.assertEqual(row['returned_end_line'], 2)
        self.assertFalse(row['truncated'])
        self.texts['README.md'] = b''
        self.refresh()
        report = self.worker(self.request())
        self.assertEqual(report['state'], 'read')
        self.assertEqual(report['files'][0]['text'], '')

    def test_role_license_and_saved_transport_guards_run_before_capture(self):
        reader = self.service()
        for purpose in ('B', 'research', 'candidate', '', None):
            with self.subTest(purpose=purpose), self.assertRaisesRegex(AuthorSourceError, 'forbidden_to_B'):
                reader.read(self.receipt, self.intent, self.selections(), purpose=purpose)
            with self.assertRaises(AuthorSourceError):
                reader.capability(self.receipt, self.intent, purpose=purpose)
        with self.assertRaises(AuthorSourceError):
            self.service(policy=dict(self.policy, license_approved=False))
        with self.assertRaises(AuthorSourceError):
            LegacyAuthorSourceReader(self.root/'other-audit', self.policy, transport=None)
        with self.assertRaises(AcquisitionError):
            self.service(transport=object())
        self.assertEqual(self.calls, 0)

    def test_unknown_paths_traversal_duplicate_and_request_limits_never_send(self):
        cases = [[dict(path=path, start_line=1, end_line=1)] for path in
                 ('/etc/passwd', '../README.md', 'workflow/../README.md', '.git/config', 'missing.txt')]
        cases += [[dict(path='README.md', start_line=start, end_line=end)] for start,end in
                  ((True, 2), (0, 1), (1, 401), (2, 1))]
        cases += [self.selections()*2, self.selections()*9]
        for chosen in cases:
            with self.subTest(chosen=chosen), self.assertRaises(AuthorSourceError):
                self.read(selections=chosen)
        self.assertEqual(self.calls, 0)

    def test_pinned_receipt_intent_selection_inventory_and_root_must_match(self):
        for name in ('receipt_document_sha256', 'intent_sha256', 'selection_sha256', 'inventory_sha256'):
            with self.subTest(pin=name), self.assertRaises(AuthorSourceError):
                self.read(service=self.service(policy=dict(self.policy, **{name:'0'*64})))
        for field, value in (('commit', 'f'*40), ('repository', 'other/repo'),
                             ('remote_directory', str(self.root/'elsewhere'))):
            with self.subTest(field=field), self.assertRaises((AuthorSourceError, AcquisitionError)):
                self.read(service=self.service(policy=dict(self.policy, **{field:value})))
        damaged = deepcopy(self.intent)
        damaged['plan']['selected'].pop()
        with self.assertRaises(AuthorSourceError):
            self.service().read(self.receipt, damaged, self.selections(), purpose='author_reference')
        self.assertEqual(self.calls, 0)

    def test_remote_original_receipt_bytes_cannot_be_reformatted(self):
        request = self.request()
        self.receipt_path.chmod(0o600)
        self.receipt_path.write_bytes(canonical(self.receipt))
        self.receipt_path.chmod(0o400)
        report = self.worker(request)
        self.assertEqual(report['state'], 'rejected')
        self.assertEqual(report['rejection_reason'], 'saved_legacy_receipt_SHA256_mismatch')

    def test_source_SHA_size_and_real_git_blob_are_independently_verified(self):
        path = self.source/'README.md'
        request = self.request()
        for data, reason in ((b'x'*len(self.texts['README.md']), 'SHA256'), (b'x', 'size')):
            path.chmod(0o600)
            path.write_bytes(data)
            path.chmod(0o400)
            response = self.worker(request)
            self.assertEqual(response['state'], 'rejected')
            self.assertIn(reason, response['rejection_reason'])
        self.refresh(bad_blob=True)
        response = self.worker(self.request())
        self.assertEqual(response['state'], 'rejected')
        self.assertEqual(response['rejection_reason'], 'source_file_git_blob_mismatch')

    def test_file_symlinks_hardlinks_FIFO_and_public_modes_are_rejected(self):
        path = self.source/'README.md'
        request = self.request()
        outside = self.root/'outside'
        outside.write_bytes(self.texts['README.md'])
        outside.chmod(0o400)
        path.unlink()
        path.symlink_to(outside)
        self.assertEqual(self.worker(request)['state'], 'rejected')
        path.unlink()
        path.write_bytes(self.texts['README.md'])
        path.chmod(0o400)
        linked = self.root/'hardlink'
        os.link(path, linked)
        self.assertEqual(self.worker(request)['state'], 'rejected')
        linked.unlink()
        path.unlink()
        os.mkfifo(path, mode=0o600)
        self.assertEqual(self.worker(request)['state'], 'rejected')
        path.unlink()
        path.write_bytes(self.texts['README.md'])
        path.chmod(0o444)
        self.assertEqual(self.worker(request)['state'], 'rejected')

    def test_receipt_source_directory_root_and_nested_links_are_rejected(self):
        request = self.request()
        target = self.root/'original-receipt'
        self.receipt_path.rename(target)
        self.receipt_path.symlink_to(target)
        self.assertEqual(self.worker(request)['state'], 'rejected')
        self.receipt_path.unlink()
        target.rename(self.receipt_path)
        self.source.chmod(0o755)
        self.assertEqual(self.worker(request)['state'], 'rejected')
        self.source.chmod(0o700)
        original_source = self.remote/'original-source'
        self.source.rename(original_source)
        self.source.symlink_to(original_source, target_is_directory=True)
        self.assertEqual(self.worker(request)['state'], 'rejected')
        self.source.unlink()
        original_source.rename(self.source)
        nested = self.source/'workflow'
        renamed = self.source/'original-workflow'
        nested.rename(renamed)
        nested.symlink_to(renamed, target_is_directory=True)
        response = self.worker(self.request([dict(path='workflow/entry.py', start_line=1, end_line=1)]))
        self.assertEqual(response['state'], 'rejected')
        original_root = self.root/'original-root'
        self.remote.rename(original_root)
        self.remote.symlink_to(original_root, target_is_directory=True)
        self.assertEqual(self.worker(request)['state'], 'rejected')

    def test_no_local_execution_or_invalid_encoding_and_response_overflow(self):
        request = self.request()
        with patch('auto_lammps.legacy_author_source_worker.sys.platform', 'darwin'), \
                patch('auto_lammps.legacy_author_source_worker._root_descriptor') as opener:
            with self.assertRaisesRegex(AuthorSourceError, 'requires_HPC_SSH'):
                worker.read_pinned_text(request, self.intent)
            opener.assert_not_called()
        for data, reason in ((b'\xff', 'not_UTF8'), (b'x\0y', 'contains_NUL'),
                             (b'x'*(MAX_TEXT_BYTES+1), 'response_limit')):
            with self.subTest(reason=reason):
                self.texts['README.md'] = data
                self.refresh()
                response = self.worker(self.request())
                self.assertEqual(response['state'], 'rejected')
                self.assertIn(reason, response['rejection_reason'])

    def test_eight_files_and_400_lines_preserve_every_requested_line(self):
        self.texts = {'file'+str(i)+'.txt': b'complete synthetic line\n'*401 for i in range(8)}
        self.refresh()
        chosen = [dict(path=name, start_line=1, end_line=400) for name in self.texts]
        response = self.read(selections=chosen)
        self.assertEqual(response['state'], 'read', response)
        self.assertEqual(len(response['files']), 8)
        for row in response['files']:
            self.assertEqual(row['text'].encode(), b'complete synthetic line\n'*400)
            self.assertEqual(row['returned_end_line'], 400)
            self.assertEqual(row['total_lines'], 401)
            self.assertTrue(row['truncated'])

    def test_cumulative_text_limit_rejects_all_files_without_partial_publication(self):
        self.texts = {'first.txt': b'a'*(MAX_TEXT_BYTES//2+1),
                      'second.txt': b'b'*(MAX_TEXT_BYTES//2+1)}
        self.refresh()
        response = self.read(selections=[dict(path=name, start_line=1, end_line=1) for name in self.texts])
        self.assertEqual(response['state'], 'rejected')
        self.assertEqual(response['rejection_reason'], 'source_text_response_limit')
        self.assertEqual(response['files'], [])

    def test_two_reference_tasks_share_source_connection_but_never_read_pairs(self):
        original_bytes = self.receipt_path.read_bytes()
        first = self.inputs()
        second_id = 'f'*32
        second_reader = self.service(policy=dict(self.policy, reference_task_id=second_id))
        source_sidecar = next((self.root/'audit/legacy-source-bindings').iterdir())
        source_binding = source_sidecar.read_bytes()
        self.assertNotIn('reference_task_id', json.loads(source_binding)['binding'])
        # Retained per-read audit can also reconstruct this shared source pin
        # after a missing sidecar, without treating the first task as its owner.
        source_sidecar.unlink()
        second_request = second_reader.selection_request(self.receipt, self.intent, self.selections())
        second_response = self.read(service=second_reader)
        self.assertEqual(second_response['state'], 'read', second_response)
        self.assertEqual(source_sidecar.read_bytes(), source_binding)
        second = dict(capability=second_reader.capability(self.receipt, self.intent, purpose='author_reference'),
            acquisition_receipt=deepcopy(self.receipt), legacy_staging_intent=deepcopy(self.intent),
            reads=[dict(request=second_request, response=second_response)])
        projected_first = validated_inputs(first, self.policy['reference_task_id'])
        projected_second = validated_inputs(second, second_id)
        self.assertEqual(projected_first['files'], projected_second['files'])
        self.assertEqual(projected_first['sources'][0]['text'], projected_second['sources'][0]['text'])
        self.assertNotEqual(projected_first['sources'][0]['id'], projected_second['sources'][0]['id'])
        for reads in (deepcopy(first['reads']),
                      [dict(request=second_request, response=deepcopy(first['reads'][0]['response']))]):
            with self.assertRaises(AuthorSourceError):
                validated_inputs(dict(second, reads=reads), second_id)
        task_files = list((self.root/'audit/legacy-task-bindings').iterdir())
        self.assertEqual({path.stem for path in task_files}, {self.policy['reference_task_id'], second_id})
        for path in task_files:
            self.assertEqual(json.loads(path.read_bytes())['binding']['reference_task_id'], path.stem)
        self.assertEqual(len(list((self.root/'audit/legacy-source-bindings').iterdir())), 1)
        self.assertEqual(self.receipt_path.read_bytes(), original_bytes)
        self.assertEqual(self.calls, 2)

    def test_changed_saved_connection_cannot_adopt_read_binding(self):
        self.assertEqual(self.read()['state'], 'read')
        capture = Mock()
        credential = self.transport.connections.directory/(self.transport.identity['credential_id']+'.json')
        original = json.loads(credential.read_bytes())
        original['known_hosts'] = 'CHANGED_SYNTHETIC_HOST_KEY'
        credential.write_bytes(canonical(original))
        with self.assertRaises(AcquisitionError):
            self.read(service=self.service(capture=capture))
        changed = SavedHPCTransport(self.transport.connections, 1, self.policy['host_alias'])
        with self.assertRaises(AcquisitionError):
            self.read(service=self.service(transport=changed, capture=capture))
        for sidecar in (self.root/'audit/legacy-source-bindings').iterdir():
            sidecar.unlink()
        with self.assertRaises(AuthorSourceError):
            self.read(service=self.service(transport=changed, capture=capture))
        capture.assert_not_called()
        self.assertEqual(self.calls, 1)

    def test_forged_source_pairs_capability_and_cross_task_projection_are_rejected(self):
        value = self.inputs()
        with self.assertRaises(AuthorSourceError):
            validated_inputs(value, 'f'*32)
        for mutate in (
                lambda v: v['capability'].update(whole_repository_downloaded=True),
                lambda v: v['capability'].update(source_sha256='0'*64),
                lambda v: v['acquisition_receipt'].update(source_executed=True),
                lambda v: v['legacy_staging_intent']['plan']['selected'][0].update(mode='120000'),
                lambda v: v['reads'][0]['request'].update(reference_task_id='f'*32),
                lambda v: v['reads'][0]['response']['files'][0].update(text='forged'),
                lambda v: v['reads'][0]['response']['files'][0].update(returned_end_line=3),
                lambda v: v['reads'].append(deepcopy(v['reads'][0]))):
            damaged = deepcopy(value)
            mutate(damaged)
            with self.assertRaises((AuthorSourceError, ValueError)):
                validated_inputs(damaged, self.policy['reference_task_id'])

    def test_transport_failures_and_forged_responses_keep_no_text_or_secrets(self):
        canary = 'PRIVATE_TRANSPORT_SOURCE_AND_CREDENTIAL_CANARY'
        capture = Mock(return_value=dict(returncode=255, failure='unsafe '+canary, stdout=canary, stderr=canary))
        response = self.read(service=self.service(capture=capture))
        self.assertEqual(response['state'], 'unknown')
        self.assertNotIn(canary, canonical(response).decode())
        value = self.inputs()

        def forged(*args, **kwargs):
            report = deepcopy(value['reads'][0]['response'])
            report['files'][0]['text'] = canary
            return dict(returncode=0, failure='', stdout=base64.b64encode(canonical(report)).decode(), stderr=canary)

        self.assertEqual(self.read(service=self.service(capture=forged))['state'], 'unknown')
        for path in (self.root/'audit').rglob('*.json'):
            self.assertNotIn(canary, path.read_text())
            self.assertNotIn('SYNTHETIC_PRIVATE_SOURCE_CANARY', path.read_text())

    def test_tool_bundle_is_deterministic_minimal_and_compiles(self):
        bundle = reader_tool_bundle()
        self.assertEqual(bundle, reader_tool_bundle())
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            self.assertEqual(set(archive.namelist()), {
                'auto_lammps/__init__.py', 'auto_lammps/author_source_worker.py',
                'auto_lammps/legacy_author_source_worker.py', 'LICENSE', 'COPYRIGHT.md',
                'third_party/SIGA-LAMMPS-LICENSE.txt'})
            for name in archive.namelist():
                if name.endswith('.py'):
                    compile(archive.read(name), name, 'exec')
        compile(BOOTSTRAP, '<legacy helper>', 'exec')


if __name__ == '__main__':
    unittest.main()
