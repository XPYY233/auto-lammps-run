"""Synthetic user-resource acquisition and recovery; never SSH/API/physics."""
import base64
from copy import deepcopy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from auto_lammps.author_reference_resources import AuthorReferenceResources
from auto_lammps.author_source_reader import AuthorSourceReader
from auto_lammps.author_source_worker import read_source_report
from auto_lammps.manifest import canonical, sha256
from auto_lammps.reference_resource_worker import acquire as worker_acquire
from auto_lammps.remote_resources import RemoteResources
from auto_lammps.tasks import TaskError
from auto_lammps.web import create_app
import test_author_workspace_loader as loader_fixtures
from test_potential_acquisition import Reader, TREE
from test_source_discovery import response
from test_web import HEADERS, ORIGIN


class PinnedSyntheticRepository(Reader):
    """Keep the existing static-resource fixture, with the saved source pin."""
    def __init__(self, repository, commit, files):
        super().__init__()
        self.repository, self.commit = repository, commit
        self.files.update(deepcopy(files))
        self.files['extra.txt'] = b'SYNTHETIC_ADDITIONAL_SOURCE\nNot a scientific program.\n'

    def get(self, endpoint):
        if not endpoint.startswith('repos/'+self.repository+'/git/'):
            raise AssertionError('Only the fixed synthetic repository is permitted')
        if '/git/commits/' in endpoint:
            self.calls.append(endpoint)
            return response(dict(sha=self.commit, tree=dict(sha=TREE)))
        return super().get(endpoint)


class AuthorResourceWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.fx = loader_fixtures.AuthorWorkspaceLoaderTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.workspace = self.fx.load()
        self.identifier = self.fx.task
        self.request = '7'*32
        self.repository = PinnedSyntheticRepository(self.fx.legacy.receipt['repository'],
            self.fx.legacy.receipt['commit'], self.fx.legacy.texts)
        self.requests, self.commands = [], []
        self.mode = 'normal'
        self.transform = None
        self.expected_legacy_reads = 0
        self.capture = Mock(side_effect=self.synthetic_capture)
        self.factory = Mock(side_effect=lambda directory, policy, *, transport:
            RemoteResources(directory, policy, transport=transport, capture=self.capture))
        self.service = AuthorReferenceResources(self.workspace, acquirer_factory=self.factory)
        paths = [self.fx.legacy.receipt_path]
        paths += [Path(self.fx.row[key]['path']) for key in ('receipt', 'intent', 'association', 'license_approval')]
        paths += [self.fx.legacy.source/name for name in self.fx.legacy.texts]
        self.originals = {path: (sha256(path.read_bytes()), path.stat().st_mode) for path in paths}
        self.addCleanup(self.assert_no_model_or_plan)

    def synthetic_capture(self, command, **options):
        self.assertEqual(command[0], 'ssh')
        self.assertIn('saved.example.edu', command)
        self.assertIn('2244', command)
        self.assertIn('StrictHostKeyChecking=yes', command)
        self.commands.append(list(command))
        envelope = json.loads(b''.join(options['input_chunks']))
        self.assertEqual(sha256(base64.b64decode(envelope['bundle'])), envelope['request']['bundle_sha256'])
        self.requests.append(deepcopy(envelope['request']))
        report = worker_acquire(envelope['request'], transport=self.repository)
        if self.mode == 'lost_after_remote_completion':
            return dict(returncode=None, failure='timeout', stdout='', stderr='')
        observed = deepcopy(report)
        if self.transform:
            self.transform(observed)
        return dict(returncode=0, failure='', stdout=base64.b64encode(canonical(observed)).decode(), stderr='')

    def assert_no_model_or_plan(self):
        self.assertEqual(self.fx.capture.call_count, self.expected_legacy_reads)
        self.fx.model.assert_not_called()
        self.fx.model_transport.assert_not_called()
        self.assertEqual(self.fx.fx.calls.status()['used_requests'], 0)
        self.assertEqual(self.workspace.planner.status(self.identifier)['used_rounds'], 0)

    def assert_originals_unchanged(self):
        for path, before in self.originals.items():
            self.assertEqual((sha256(path.read_bytes()), path.stat().st_mode), before)

    def counts(self):
        with self.workspace.tasks.transaction() as db:
            return {table: db.execute('SELECT count(*) FROM '+table).fetchone()[0]
                    for table in ('author_resource_requests', 'author_resource_events',
                                  'author_plan_intents', 'author_plan_receipts', 'author_source_evidence')}

    def second_task(self):
        task = self.fx.fx.tasks.create('Second synthetic source consumer', 'Synthetic request only.', 'reproduction')
        paper = self.fx.fx.papers.get(self.fx.fx.paper['id'])
        self.fx.fx.papers.link_task(paper['id'], paper['revision'], task['id'])
        row = deepcopy(self.fx.row)
        row['task_id'] = task['id']
        row['policy']['reference_task_id'] = task['id']
        row['license_approval'] = self.fx.write_artifact('second-license', dict(scope='reference_only',
            task_id=task['id'], reading_approved=True, execution_authorized=False))
        row['policy']['license_approval_sha256'] = row['license_approval']['sha256']
        self.fx.config['registrations'].append(row)
        self.fx.save_config()
        workspace = self.fx.load()
        return task['id'], AuthorReferenceResources(workspace, acquirer_factory=self.factory)

    def test_status_and_completed_are_local_and_do_not_create_requests_or_read_source(self):
        before = self.counts()
        status = self.service.status(self.identifier)
        self.assertTrue(status['enabled'], status)
        self.assertEqual(status['history'], [])
        self.assertIsNone(self.service.completed(self.identifier))
        self.assertEqual(self.counts(), before)
        self.factory.assert_not_called()
        self.capture.assert_not_called()
        self.assertFalse(status['calls_model'])
        self.assertFalse(status['submits_HPC'])
        self.assertFalse(status['execution_authorized'])
        self.assert_originals_unchanged()

    def test_acquire_uses_fixed_repo_commit_saved_connection_and_same_id_is_idempotent(self):
        first = self.service.acquire(self.identifier, self.request)
        self.assertEqual(first['latest']['state'], 'finished', first)
        self.assertTrue(first['latest']['source_complete'])
        self.assertEqual((first['latest']['repository'], first['latest']['commit']),
                         (self.repository.repository, self.repository.commit))
        context = self.service._rows(self.identifier)[0]['context']
        transport = self.workspace.bindings[self.identifier]['reader'].transport
        self.assertEqual(context['transport_identity_sha256'], sha256(canonical(transport.identity)))
        self.assertEqual(context['transport_revision'], self.fx.revision)
        self.assertEqual(context['remote_directory'], str(Path(transport.identity['profile']['work_directory'])/
                                                       'Auto-LAMMPS-Resources'/'author-sources'))
        self.assertEqual(context['original_receipt_sha256'], sha256(canonical(self.fx.legacy.receipt)))
        counts, downloads = self.counts(), list(self.repository.calls)
        restarted = AuthorReferenceResources(self.workspace, acquirer_factory=self.factory)
        self.assertEqual(restarted.acquire(self.identifier, self.request), first)
        self.assertEqual(restarted.acquire(self.identifier, '8'*32), first)
        self.assertEqual(self.counts(), counts)
        self.assertEqual(self.repository.calls, downloads)
        self.assertEqual(self.capture.call_count, 1)
        self.assertEqual(self.factory.call_count, 1)
        self.assertIs(self.factory.call_args.kwargs['transport'], transport)
        self.assert_originals_unchanged()

    def test_lost_reply_only_recovers_original_id_without_redownloading_or_erasing_failure(self):
        self.mode = 'lost_after_remote_completion'
        first = self.service.acquire(self.identifier, self.request)
        self.assertEqual(first['latest']['state'], 'unknown')
        self.assertFalse(first['latest']['source_complete'])
        self.assertTrue(first['latest']['recoverable'])
        self.assertIsNone(self.service.completed(self.identifier))
        calls = list(self.repository.calls)
        with self.assertRaisesRegex(TaskError, '原请求'):
            self.service.acquire(self.identifier, '8'*32)
        self.assertEqual(self.capture.call_count, 1)
        self.mode = 'normal'
        restarted = AuthorReferenceResources(self.workspace, acquirer_factory=self.factory)
        restored = restarted.acquire(self.identifier, self.request)
        self.assertEqual(restored['latest']['state'], 'finished', restored)
        self.assertEqual(self.repository.calls, calls)
        self.assertEqual(self.capture.call_count, 2)
        self.assertEqual([row['state'] for row in restarted._rows(self.identifier)[0]['events']],
                         ['started', 'unknown', 'finished'])
        self.assertEqual(self.counts()['author_resource_requests'], 1)
        self.assert_originals_unchanged()

    def test_cross_task_operation_identity_is_rejected_before_and_after_own_completion(self):
        self.service.acquire(self.identifier, self.request)
        other, service = self.second_task()
        with self.assertRaisesRegex(TaskError, '另一任务'):
            service.acquire(other, self.request)
        service.acquire(other, '8'*32)
        before = self.capture.call_count
        with self.assertRaisesRegex(TaskError, '另一任务'):
            service.acquire(other, self.request)
        self.assertEqual(self.capture.call_count, before)
        self.assertEqual(len(service.status(other)['history']), 1)
        self.assert_originals_unchanged()

    def test_changed_pinned_credentials_disable_status_and_block_recovery_before_transport(self):
        self.mode = 'lost_after_remote_completion'
        self.service.acquire(self.identifier, self.request)
        transport = self.workspace.bindings[self.identifier]['reader'].transport
        credential = self.fx.hpc.directory/(transport.identity['credential_id']+'.json')
        document = json.loads(credential.read_bytes())
        document['known_hosts'] = 'CHANGED_SYNTHETIC_KEY'
        credential.write_bytes(canonical(document))
        status = self.service.status(self.identifier)
        self.assertFalse(status['enabled'])
        self.assertEqual(status['latest']['state'], 'unknown')
        with self.assertRaisesRegex(TaskError, '连接变化'):
            self.service.acquire(self.identifier, self.request)
        self.assertEqual(self.capture.call_count, 1)
        self.assert_originals_unchanged()

    def test_new_default_connection_does_not_migrate_existing_source_pin(self):
        transport = self.workspace.bindings[self.identifier]['reader'].transport
        profile = deepcopy(transport.identity['profile'])
        profile['host'] = 'different-synthetic.example.edu'
        self.fx.hpc.save(profile, self.fx.revision, known_hosts='NEW_SYNTHETIC_KEY')
        self.assertTrue(self.service.status(self.identifier)['enabled'])
        result = self.service.acquire(self.identifier, self.request)
        self.assertEqual(result['latest']['state'], 'finished')
        self.assertNotIn('different-synthetic.example.edu', self.commands[0])
        self.assertEqual(self.service.completed(self.identifier)[1]['transport_revision'], self.fx.revision)
        self.assert_originals_unchanged()

    def test_changed_original_pin_in_returned_inventory_is_unknown_and_original_id_can_recover(self):
        def changed_pin(report):
            selected = next(row for row in report['files'] if row['path'] == 'README.md')
            selected['sha256'] = 'f'*64
            report['inventory_sha256'] = sha256(canonical(report['files']))
        self.transform = changed_pin
        bad = self.service.acquire(self.identifier, self.request)
        self.assertEqual(bad['latest']['state'], 'unknown')
        self.assertIsNone(self.service.completed(self.identifier))
        self.assert_originals_unchanged()
        self.transform = None
        restored = self.service.acquire(self.identifier, self.request)
        self.assertEqual(restored['latest']['state'], 'finished', restored)
        self.assertEqual(self.counts()['author_resource_requests'], 1)
        self.assert_originals_unchanged()

    def test_complete_supplement_can_be_read_by_existing_standard_reader_without_plan_or_simulation(self):
        self.service.acquire(self.identifier, self.request)
        report, context = self.service.completed(self.identifier)
        policy = dict(host_alias=context['host_alias'], remote_directory=context['remote_directory'],
            reference_task_id=self.identifier, repository=context['repository'], commit=context['commit'],
            acquisition_id=self.request, receipt_sha256=sha256(canonical(report)),
            inventory_sha256=report['inventory_sha256'], license_approved=True,
            license_approval_sha256=context['license_approval_sha256'])
        transport = self.workspace.bindings[self.identifier]['reader'].transport

        def read_capture(command, **options):
            self.assertIn('saved.example.edu', command)
            envelope = json.loads(b''.join(options['input_chunks']))
            with patch('auto_lammps.author_source_worker.sys.platform', 'linux'), \
                    patch.dict(os.environ, {'SSH_CONNECTION': 'synthetic'}):
                value = read_source_report(envelope['request'])
            return dict(returncode=0, failure='', stdout=base64.b64encode(canonical(value)).decode(), stderr='')

        reader = AuthorSourceReader(self.fx.root/'supplement-reads', policy, transport=transport, capture=read_capture)
        value = reader.read(report, [dict(path='extra.txt', start_line=1, end_line=20)], purpose='author_reference')
        self.assertEqual(value['state'], 'read', value)
        self.assertEqual(value['files'][0]['text'].encode(), self.repository.files['extra.txt'])
        self.assertFalse(value['physical_evaluation_performed'])
        self.assertFalse(value['execution_authorized'])
        self.assertFalse(value['B_input'])
        self.assert_originals_unchanged()

    def test_HTTP_acquire_and_read_reuse_cached_evidence_without_model_or_author_round(self):
        self.workspace.resources = self.service
        standard_capture = Mock()

        def read_capture(command, **options):
            self.assertIn('saved.example.edu', command)
            envelope = json.loads(b''.join(options['input_chunks']))
            with patch('auto_lammps.author_source_worker.sys.platform', 'linux'), \
                    patch.dict(os.environ, {'SSH_CONNECTION': 'synthetic'}):
                value = read_source_report(envelope['request'])
            return dict(returncode=0, failure='', stdout=base64.b64encode(canonical(value)).decode(), stderr='')

        standard_capture.side_effect = read_capture

        def source_reader(directory, policy, *, transport):
            self.assertEqual(policy['repository'], self.repository.repository)
            self.assertEqual(policy['commit'], self.repository.commit)
            self.assertEqual(policy['reference_task_id'], self.identifier)
            self.assertEqual(policy['acquisition_id'], self.request)
            return AuthorSourceReader(directory, policy, transport=transport, capture=standard_capture)

        route = '/api/tasks/'+self.identifier+'/author-resources'
        with patch('auto_lammps.author_reference_workspace.AuthorSourceReader', side_effect=source_reader), \
                TestClient(create_app(self.fx.fx.tasks, papers=self.fx.fx.papers,
                    author_workspace_service=self.workspace), base_url=ORIGIN,
                    raise_server_exceptions=False) as client:
            before = self.counts()
            status = client.get(route)
            self.assertEqual(status.status_code, 200, status.text)
            self.assertEqual(self.counts(), before)
            self.capture.assert_not_called()
            for extra in (dict(repository='another/repository'), dict(commit='f'*40),
                          dict(remote_directory='/outside'), dict(connection_revision=999)):
                rejected = client.post(route, json=dict(request_id=self.request, **extra), headers=HEADERS)
                self.assertEqual(rejected.status_code, 422, rejected.text)
            self.capture.assert_not_called()
            acquired = client.post(route, json=dict(request_id=self.request), headers=HEADERS)
            self.assertEqual(acquired.status_code, 200, acquired.text)
            self.assertEqual(acquired.json()['latest']['state'], 'finished', acquired.text)
            rejected = client.post(route+'/read', json=dict(path='extra.txt'), headers=HEADERS)
            self.assertEqual(rejected.status_code, 422, rejected.text)
            self.fx.capture.assert_not_called()
            self.expected_legacy_reads = 1
            first = client.post(route+'/read', json={}, headers=HEADERS)
            self.assertEqual(first.status_code, 200, first.text)
            value = first.json()
            self.assertEqual(value['files'], len(self.repository.files))
            self.assertEqual(value['read_files'], 5)
            self.assertEqual(set(value['unread_files']), {'library.meam', 'alloy.meam'})
            self.assertEqual(value['truncated_files'], [])
            self.assertFalse(value['calls_model'])
            self.assertFalse(value['submits_HPC'])
            self.assertFalse(value['execution_authorized'])
            self.assertFalse(value['physical_evaluation_performed'])
            self.assertEqual(standard_capture.call_count, 1)
            self.assertEqual(self.fx.capture.call_count, 1)
            counts = self.counts()
            second = client.post(route+'/read', json={}, headers=HEADERS)
            self.assertEqual(second.status_code, 200, second.text)
            self.assertEqual(second.json(), value)
            self.assertEqual(self.counts(), counts)
            self.assertEqual(standard_capture.call_count, 1)
            self.assertEqual(self.fx.capture.call_count, 1)
            self.assertEqual(self.capture.call_count, 1)
            # Deliberately corrupt only this temporary fixture's protected
            # cache. The normal action must reject it, never fetch around it.
            with self.workspace.tasks.transaction() as db:
                db.execute('DROP TRIGGER immutable_author_source_evidence_UPDATE')
                db.execute('UPDATE author_source_evidence SET sha256=? WHERE '
                           'json_extract(document,"$.capability.adapter")="pinned_author_source_reader"',
                           ('f'*64,))
            corrupt = client.post(route+'/read', json={}, headers=HEADERS)
            self.assertEqual(corrupt.status_code, 422, corrupt.text)
            self.assertIn('读取证据摘要不一致', corrupt.json()['detail'])
            self.assertEqual(standard_capture.call_count, 1)
            self.assertEqual(self.fx.capture.call_count, 1)
            self.assertEqual(self.capture.call_count, 1)
        self.assert_originals_unchanged()


if __name__ == '__main__':
    unittest.main()
