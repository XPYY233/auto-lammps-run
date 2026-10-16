"""Ordinary selected-source collection with real services and synthetic I/O.

No real provider, SSH process, author code or physical evaluation is allowed.
"""
import base64
from copy import deepcopy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from auto_lammps.author_reference_plan import author_source_inputs
from auto_lammps.author_selected_resource_workspace import AuthorSelectedResourceWorkspace
from auto_lammps.author_resource_inventory_worker import inspect_cached_tree
from auto_lammps.manifest import canonical, sha256
from auto_lammps.reference_resource_worker import acquire, MAX_BLOB
from auto_lammps.reference_selected_resource_worker import selected_resources
from auto_lammps.reference_selected_resources import ReferenceSelectedResources
from auto_lammps.selected_author_source_reader import SelectedAuthorSourceReader
from auto_lammps.selected_author_source_worker import read_source_report
from auto_lammps.web import create_app
from test_web import HEADERS, ORIGIN
import test_author_workspace_loader as loader_fixtures
import test_author_resource_inventory as inventory_fixtures
import test_reference_selected_resources as selected_fixtures
from test_potential_acquisition import TREE
from test_source_discovery import response


class FixedMetadata(inventory_fixtures.MetadataReader):
    def __init__(self, entries, repository, commit):
        super().__init__(entries); self.repository, self.commit = repository, commit

    def get(self, endpoint):
        if not endpoint.startswith('repos/'+self.repository+'/git/'):
            raise AssertionError('only fixed synthetic repository')
        if '/git/commits/' in endpoint:
            self.calls.append(endpoint); return response(dict(sha=self.commit, tree=dict(sha=TREE)))
        return super().get(endpoint)


class SelectedAuthorWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.fx = loader_fixtures.AuthorWorkspaceLoaderTests(); self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        # Preserve the two original invocation fixture files and add 21 inert
        # originals, giving the realistic pre-existing 23-file reference side.
        self.fx.legacy.texts.update({f'old-{index:02d}.txt': f'OLD_ORIGINAL_{index}\n'.encode()
                                    for index in range(21)})
        self.fx.legacy.refresh()
        self.fx.policy.update(self.fx.legacy.policy)
        self.fx.policy['license_approval_sha256'] = self.fx.row['license_approval']['sha256']
        self.fx.row.update(policy=deepcopy(self.fx.policy), receipt=self.fx.write_artifact(
            'receipt', self.fx.legacy.receipt_path.read_bytes()),
            intent=self.fx.write_artifact('intent', self.fx.legacy.intent),
            selections=[dict(path=name, start_line=1, end_line=400) for name in self.fx.legacy.texts])
        self.fx.association['resource_receipt_sha256'] = self.fx.row['receipt']['sha256']
        self.fx.row['association'] = self.fx.write_artifact('association', self.fx.association)
        self.fx.save_config(); self.workspace = self.fx.load(); self.task = self.fx.task
        first = self.workspace.prepare(self.task, '4'*32)
        second = self.workspace.prepare(self.task, '5'*32, revision_note='Improve the retained synthetic plan only.')
        self.assertEqual((first['state'], second['state']), ('prepared', 'prepared'))
        self.assertEqual(self.workspace.planner.status(self.task)['used_rounds'], 2)
        self.assertEqual(self.fx.fx.calls.status()['used_requests'], 2)
        self.originals = {path: (path.read_bytes(), path.stat().st_mode)
                          for path in self.fx.legacy.remote.rglob('*') if path.is_file()}
        self.before_history = self.history()
        self.before_model = self.fx.fx.calls.status()
        self.before_legacy_calls = self.fx.capture.call_count
        self.before_sent = deepcopy(self.fx.fx.sent)

        context, transport = self.workspace.resources._binding(self.task)
        self.original = dict(id='a'*32, repository=context['repository'], commit=context['commit'],
            directory=context['remote_directory'], bundle_sha256='b'*64, storage_bytes=512*1024**2)
        Path(self.original['directory']).mkdir(mode=0o700, parents=True)
        self.data = {'new/readme.txt': b'NEW_SELECTED_ORIGINAL\nThis is inert evidence.\n',
            'new/binary.bin': b'\0\xff\xfe\x01', 'new/large.txt': b'X'*(256*1024+1),
            'new/long.txt': b'one full original line\n'*401}
        self.entries = [inventory_fixtures.entry(name, size=len(data), identity=selected_fixtures.blob_hash(data))
                        for name, data in self.data.items()]
        self.entries.append(inventory_fixtures.entry('unselected-oversized.log', size=MAX_BLOB+1))
        self.old = acquire(self.original, transport=FixedMetadata(self.entries, context['repository'], context['commit']))
        self.assertFalse(self.old['source_complete'])
        self.original_bytes = (Path(self.old['remote_directory'])/'result.json').read_bytes()
        self.inventory_request = dict(version=1, acquisition_request=self.original,
            receipt_sha256=sha256(canonical(self.old)), tree_sha1=TREE)
        with patch('auto_lammps.author_resource_inventory_worker.sys.platform', 'linux'), \
                patch.dict(os.environ, {'SSH_CONNECTION':'synthetic offline'}):
            self.inventory = inspect_cached_tree(self.inventory_request)
        self.blobs = selected_fixtures.Blobs({selected_fixtures.blob_hash(data): data for data in self.data.values()})
        self.resource_actions, self.read_actions = [], []
        self.workspace.selected_resources = AuthorSelectedResourceWorkspace(self.workspace, factory=self.acquirer)
        self.workspace._resource_selection_input = Mock(return_value=dict(
            tree_request=self.inventory_request, tree_report=self.inventory, acquisition_receipt=self.old))
        self.workspace.resource_selections.selection = Mock(side_effect=self.selection)
        self.selected_names = list(self.data)
        self.selection_id = '7'*32; self.operation = 'c'*32
        self.patch_reader = patch('auto_lammps.selected_author_source_reader.SelectedAuthorSourceReader',
                                 side_effect=self.source_reader)
        self.patch_reader.start(); self.addCleanup(self.patch_reader.stop)
        self.http = TestClient(create_app(self.workspace.tasks, papers=self.workspace.papers,
            author_workspace_service=self.workspace), base_url=ORIGIN, raise_server_exceptions=False)
        self.addCleanup(self.http.close)
        self.route = '/api/tasks/'+self.task+'/author-resources/read'

    def history(self):
        with self.workspace.tasks.transaction() as db:
            return {table: [tuple(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                    for table in ('author_plan_intents', 'author_plan_receipts')}

    def selection(self, task, request):
        self.assertEqual((task, request), (self.task, self.selection_id))
        return dict(status='selected', scope='reference_only', B_input=False, execution_authorized=False,
            repository=self.original['repository'], commit=self.original['commit'], tree_sha1=TREE,
            acquisition_receipt_sha256=sha256(canonical(self.old)), tree_report_sha256=sha256(canonical(self.inventory)),
            selections=[dict(path=name, blob_sha1=selected_fixtures.blob_hash(self.data[name]),
                size=len(self.data[name]), git_mode='100644', reason='synthetic missing original', dependency_ids=['shared'])
                for name in self.selected_names])

    def acquirer(self, *args, **kwargs):
        return ReferenceSelectedResources(*args, **kwargs, capture=self.acquire_capture)

    def acquire_capture(self, command, **options):
        envelope = json.loads(b''.join(options['input_chunks'])); self.resource_actions.append(envelope['action'])
        with patch('auto_lammps.reference_selected_resource_worker.sys.platform', 'linux'), \
                patch.dict(os.environ, {'SSH_CONNECTION':'synthetic offline'}):
            report = selected_resources(envelope['request'], action=envelope['action'], transport=self.blobs)
        return dict(returncode=0, failure='', stdout=base64.b64encode(canonical(report)).decode(), stderr='')

    def source_reader(self, *args, **kwargs):
        return SelectedAuthorSourceReader(*args, **kwargs, capture=self.read_capture)

    def read_capture(self, command, **options):
        envelope = json.loads(b''.join(options['input_chunks'])); self.read_actions.append(envelope['request'])
        with patch('auto_lammps.selected_author_source_worker.sys.platform', 'linux'), \
                patch.dict(os.environ, {'SSH_CONNECTION':'synthetic offline'}):
            report = read_source_report(envelope['request'])
        return dict(returncode=0, failure='', stdout=base64.b64encode(canonical(report)).decode(), stderr='')

    def acquire_selected(self, names=None):
        if names is not None: self.selected_names = list(names)
        result = self.workspace.selected_resources.acquire(self.task, self.operation, self.selection_id)
        self.assertEqual(result['latest']['state'], 'selected_complete', result)
        return result

    def assert_preserved(self):
        self.assertEqual(self.history(), self.before_history)
        self.assertEqual(self.fx.fx.calls.status(), self.before_model)
        self.assertEqual(self.fx.fx.sent, self.before_sent)
        self.assertEqual(self.fx.capture.call_count, self.before_legacy_calls)
        self.assertEqual((Path(self.old['remote_directory'])/'result.json').read_bytes(), self.original_bytes)
        for path, before in self.originals.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mode), before)

    def test_ordinary_collect_reads_new_text_and_keeps_23_originals_and_two_saved_plans(self):
        acquired = self.acquire_selected(); count = len(self.blobs.calls)
        result = self.http.post(self.route, json={}, headers=HEADERS)
        self.assertEqual(result.status_code, 200, result.text)
        report = result.json(); self.assertEqual(report['files'], 27)
        self.assertEqual(report['read_files'], 25); self.assertEqual(len(self.read_actions), 1)
        self.assertEqual(set(report['unread_files']), {'new/binary.bin', 'new/large.txt'})
        self.assertEqual(report['truncated_files'], ['new/long.txt'])
        self.assertFalse(report['calls_model']); self.assertFalse(report['submits_HPC'])
        with self.workspace.tasks.transaction() as db:
            stored = [json.loads(row[0]) for row in db.execute('SELECT document FROM author_source_evidence WHERE task_id=?', (self.task,))]
        self.assertEqual(len(stored), 2)
        new = next(row for row in stored if row['capability']['adapter']=='pinned_selected_author_source_reader')
        self.assertEqual(new['acquisition_receipt'], acquired['latest']['report'])
        self.assertFalse(new['acquisition_receipt']['source_complete'])
        binding, _, receipt, _ = self.workspace._metadata(self.task)
        joined = self.workspace._sources(self.task, binding, receipt)
        self.assertEqual(len(joined['source_packages']), 2)
        self.assertEqual(len(author_source_inputs(joined, self.task)['files']), 27)
        self.assertEqual(len(self.blobs.calls), count); self.assert_preserved()

    def test_second_collect_and_service_restart_reuse_all_evidence_without_transport(self):
        self.acquire_selected(['new/readme.txt'])
        first = self.http.post(self.route, json={}, headers=HEADERS)
        self.assertEqual(first.status_code, 200, first.text)
        counts = (len(self.read_actions), len(self.resource_actions), len(self.blobs.calls))
        with patch.object(self, 'read_capture', side_effect=AssertionError('must reuse retained selected text')):
            second = self.http.post(self.route, json={}, headers=HEADERS)
        self.assertEqual(second.status_code, 200, second.text); self.assertEqual(first.json(), second.json())
        self.workspace = self.fx.load()
        self.workspace.selected_resources = AuthorSelectedResourceWorkspace(self.workspace, factory=self.acquirer)
        restarted = TestClient(create_app(self.workspace.tasks, papers=self.workspace.papers,
            author_workspace_service=self.workspace), base_url=ORIGIN, raise_server_exceptions=False)
        self.addCleanup(restarted.close)
        with patch.object(self, 'read_capture', side_effect=AssertionError('restart must reuse retained selected text')):
            restored = restarted.post(self.route, json={}, headers=HEADERS)
        self.assertEqual(restored.status_code, 200, restored.text); self.assertEqual(restored.json(), first.json())
        self.assertEqual((len(self.read_actions), len(self.resource_actions), len(self.blobs.calls)), counts)
        self.assert_preserved()

    def test_binary_and_large_only_selected_package_is_explicitly_unread_without_text_transport(self):
        self.acquire_selected(['new/binary.bin', 'new/large.txt'])
        result = self.http.post(self.route, json={}, headers=HEADERS)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['files'], 25); self.assertEqual(result.json()['read_files'], 23)
        self.assertEqual(set(result.json()['unread_files']), {'new/binary.bin', 'new/large.txt'})
        self.assertEqual(self.read_actions, []); self.assert_preserved()


if __name__ == '__main__': unittest.main()
