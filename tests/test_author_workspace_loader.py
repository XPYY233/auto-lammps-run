"""Private JSON loader integration using synthetic source and saved connections."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from auto_lammps.author_reference_workspace import load_author_workspace
from auto_lammps.legacy_author_source_reader import LegacyAuthorSourceReader
from auto_lammps.manifest import canonical, sha256
from auto_lammps.runtime_launcher import ExecutionDenied
from auto_lammps.tasks import TaskError
from auto_lammps.web import create_app
import test_author_reference_plan as plan_fixture
import test_legacy_author_source_reader as legacy_fixture
import test_reference_saved_transport as transport_fixture
import test_web as web_fixture


class AuthorWorkspaceLoaderTests(unittest.TestCase):
    def setUp(self):
        self.fx = plan_fixture.AuthorReferencePlanTests()
        self.fx.setUp(); self.addCleanup(self.fx.doCleanups)
        self.legacy = legacy_fixture.LegacyAuthorSourceReaderTests()
        self.legacy.setUp(); self.addCleanup(self.legacy.doCleanups)
        self.task = self.fx.task['id']
        self.legacy.texts = deepcopy(self.fx.reader.texts)
        self.legacy.policy['reference_task_id'] = self.task
        self.legacy.refresh()
        self.root = self.legacy.root/'workspace-metadata'
        self.root.mkdir(mode=0o700)
        # Use the shared fixture constructor rather than a transport-shaped fake.
        self.transport = transport_fixture.saved_transport(self.root, self.legacy.policy['host_alias'])
        self.hpc = self.transport.connections
        # The saved profile must contain the synthetic remote source directory.
        profile = deepcopy(self.transport.identity['profile'])
        profile['work_directory'] = str(self.legacy.root)
        self.hpc.save(profile, 1, known_hosts='SYNTHETIC_HOST_KEY')
        self.revision = 2
        self.capture = Mock(wraps=self.legacy.capture)
        self.model_transport = Mock(side_effect=self.synthetic_model)
        self.fx.client.transport = self.model_transport
        self.policy = deepcopy(self.legacy.policy)
        license_spec = self.write_artifact('license', dict(scope='reference_only', task_id=self.task,
            reading_approved=True, execution_authorized=False))
        self.policy['license_approval_sha256'] = license_spec['sha256']
        receipt_spec = self.write_artifact('receipt', self.legacy.receipt_path.read_bytes())
        intent_spec = self.write_artifact('intent', self.legacy.intent)
        self.association = dict(scope='reference_only', paper_id=self.fx.paper['id'],
            paper={key:self.fx.paper[key] for key in ('title', 'doi')},
            repository=self.legacy.receipt['repository'], commit=self.legacy.receipt['commit'],
            resource_receipt_sha256=receipt_spec['sha256'], reading_approved=True, execution_authorized=False)
        self.row = dict(task_id=self.task, paper_id=self.fx.paper['id'], format='legacy_selected_resource_v3',
            policy=self.policy, connection_revision=self.revision, receipt=receipt_spec, intent=intent_spec,
            license_approval=license_spec, association=self.write_artifact('association', self.association),
            selections=[dict(path=name, start_line=1, end_line=20) for name in self.legacy.texts],
            invocation_paths=['README.md'], deployment=deepcopy(self.fx.input['deployment']), missing=[])
        self.config = dict(version=1, audit_directory=str(self.root/'read-audit'), registrations=[self.row])
        self.path = self.root/'author-workspace.json'
        self.save_config()
        self.model = Mock(side_effect=lambda _:dict(enabled=True, client=self.fx.client,
            connection_revision=1, credential_generation=1))
        # A forgotten test boundary must fail locally, never open SSH or an API.
        guard = patch('auto_lammps.slurm_read.subprocess.Popen',
            side_effect=AssertionError('External processes are forbidden in this test'))
        guard.start(); self.addCleanup(guard.stop)

    def write_artifact(self, name, value):
        raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, indent=2).encode()
        path = self.root/(name+'.json')
        path.write_bytes(raw); path.chmod(0o600)
        return dict(path=str(path), sha256=sha256(raw))

    def save_config(self):
        self.path.write_bytes(canonical(self.config)); self.path.chmod(0o600)

    def load(self):
        workspace = load_author_workspace(self.fx.tasks, self.fx.papers, self.fx.scopes, self.path, self.hpc,
            model_provider=self.model, connection_provider=self.fx.connection_provider, model_calls=self.fx.calls)
        # Only the process boundary is replaced; JSON parsing, transport pins,
        # the legacy reader, worker verification and planner remain production.
        workspace.bindings[self.task]['reader'].capture = self.capture
        return workspace

    def synthetic_model(self, body, key, timeout):
        evidence = json.loads(json.loads(body)['messages'][-1]['content'])
        source_id = evidence['invocation_source_ids'][0]
        invocation = self.fx.value['invocations'][0]
        for citation in invocation['citations'] + invocation['output_files'][0]['citations']:
            citation['source_id'] = source_id
        return self.fx.transport(body, key, timeout)

    def assert_no_external_calls(self):
        self.capture.assert_not_called(); self.model_transport.assert_not_called()
        self.assertEqual(self.legacy.calls, 0)
        self.assertEqual(self.fx.calls.status()['used_requests'], 0)
        self.assertEqual(self.fx.sent, [])

    def test_private_json_assembles_real_legacy_reader_and_GET_is_read_only(self):
        workspace = self.load(); binding = workspace.bindings[self.task]
        self.assertIsInstance(binding['reader'], LegacyAuthorSourceReader)
        self.assertEqual(binding['reader'].policy, self.policy)
        self.assertEqual(binding['load_receipt'](), self.legacy.receipt)
        self.assertEqual(binding['load_intent'](), self.legacy.intent)
        self.assertEqual(binding['load_association'](), self.association)
        self.assertEqual(binding['receipt_file_sha256'], sha256(self.legacy.receipt_path.read_bytes()))
        self.assertEqual(binding['reader'].transport.revision, self.revision)
        with TestClient(create_app(self.fx.tasks, papers=self.fx.papers,
                author_workspace_service=workspace), base_url=web_fixture.ORIGIN) as client:
            for _ in range(2):
                response = client.get('/api/tasks/'+self.task+'/author-plan')
                self.assertEqual(response.status_code, 200, response.text)
                status = response.json()
                self.assertTrue(status['configured']); self.assertTrue(status['can_prepare'])
                self.assertFalse(status['source_reads_on_GET'])
                self.assertFalse(status['can_submit']); self.assertFalse(status['can_approve'])
                self.assertEqual(status['used_rounds'], 0); self.assertEqual(status['history'], [])
        self.assert_no_external_calls()

    def test_loaded_legacy_documents_prepare_once_without_rewriting_original_bytes(self):
        before = {path:(path.read_bytes(), path.stat().st_mode)
                  for path in self.legacy.remote.rglob('*') if path.is_file()}
        workspace = self.load()
        result = workspace.prepare(self.task, '4'*32)
        self.assertEqual(result['state'], 'prepared', result)
        self.assertEqual(result['round'], 1)
        self.assertEqual(result['report']['reference_scope']['scope']['selected_ids'], ['first', 'second'])
        self.assertFalse(result['can_submit']); self.assertFalse(result['can_approve'])
        self.assertEqual(self.capture.call_count, 1); self.assertEqual(self.model_transport.call_count, 1)
        restarted = self.load()
        self.assertEqual(restarted.prepare(self.task, '4'*32)['state'], 'prepared')
        self.assertEqual(self.capture.call_count, 1); self.assertEqual(self.model_transport.call_count, 1)
        for path, original in before.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mode), original)
        self.assertNotIn('schema_version', workspace.bindings[self.task]['load_receipt']())
        with self.fx.tasks.transaction() as db:
            evidence = json.loads(db.execute('SELECT document FROM author_source_evidence WHERE task_id=?',
                (self.task,)).fetchone()[0])
        self.assertEqual(evidence['legacy_staging_intent'], self.legacy.intent)
        self.assertEqual(evidence['acquisition_receipt'], self.legacy.receipt)
        self.assertEqual(evidence['capability']['binding']['reference_task_id'], self.task)

    def test_HTTP_private_receipt_rejection_disables_new_preparation_without_losing_saved_history(self):
        workspace = self.load()
        original = workspace.prepare(self.task, '4'*32)
        self.assertEqual(original['state'], 'prepared', original)
        model_history = deepcopy(self.fx.calls.history())
        planner_history = deepcopy(workspace.planner.status(self.task)['history'])
        url = '/api/tasks/'+self.task+'/author-plan'
        with TestClient(create_app(self.fx.tasks, papers=self.fx.papers,
                author_workspace_service=workspace), base_url=web_fixture.ORIGIN,
                raise_server_exceptions=False) as client:
            baseline = client.get(url)
            self.assertEqual(baseline.status_code, 200, baseline.text)
            saved_history = baseline.json()['history']
            Path(self.row['receipt']['path']).chmod(0o644)
            status = client.get(url)
            self.assertEqual(status.status_code, 200, status.text)
            self.assertFalse(status.json()['enabled']); self.assertFalse(status.json()['can_prepare'])
            self.assertTrue(status.json()['configured'])
            self.assertEqual(status.json()['disabled_reason'], '作者来源或固定部署暂未通过核验。')
            self.assertEqual(status.json()['history'], saved_history)
            self.assertEqual(status.json()['history'][0]['proposal'], self.fx.value)
            rejected = client.post(url, json=dict(request_id='5'*32, revision_note='Synthetic revision'),
                headers=web_fixture.HEADERS)
            self.assertEqual(rejected.status_code, 422, rejected.text)
            self.assertFalse(rejected.json()['request_recorded'])
            self.assertIn('未自动重发，已有记录保留', rejected.json()['detail'])
            # The original terminal reply is a read of its saved receipt even
            # while the current source metadata blocks a new paid revision.
            retained = client.post(url, json=dict(request_id='4'*32, revision_note=''),
                headers=web_fixture.HEADERS)
            self.assertEqual(retained.status_code, 200, retained.text)
            self.assertEqual(retained.json()['receipt'], original['receipt'])
            final = client.get(url)
            self.assertEqual(final.status_code, 200, final.text)
            self.assertEqual(final.json()['history'], saved_history)
            self.assertEqual(final.json()['used_rounds'], 1)
            self.assertEqual(final.json()['remaining_rounds'], 2)
        self.assertEqual(workspace.planner.status(self.task)['history'], planner_history)
        self.assertEqual(self.fx.calls.history(), model_history)
        self.assertEqual(self.capture.call_count, 1); self.assertEqual(self.model_transport.call_count, 1)
        self.assertEqual(self.fx.calls.status()['used_requests'], 1)

    def test_public_0644_private_metadata_is_rejected_before_any_external_call(self):
        paths = [self.path] + [Path(self.row[key]['path'])
            for key in ('license_approval', 'receipt', 'intent', 'association')]
        for path in paths:
            with self.subTest(metadata=path.name):
                path.chmod(0o644)
                try:
                    with self.assertRaises((ExecutionDenied, TaskError)):
                        self.load().prepare(self.task, '4'*32)
                finally:
                    path.chmod(0o600)
                self.assert_no_external_calls()

    def test_semantically_equal_reformatted_artifact_bytes_do_not_match_original_pin(self):
        for key in ('license_approval', 'receipt', 'intent', 'association'):
            path = Path(self.row[key]['path']); original = path.read_bytes()
            with self.subTest(metadata=key):
                path.write_bytes(original+b'\n')
                self.assertEqual(json.loads(path.read_bytes()), json.loads(original))
                try:
                    with self.assertRaisesRegex(TaskError, '原字节已改变'):
                        self.load().prepare(self.task, '4'*32)
                finally:
                    path.write_bytes(original)
                self.assert_no_external_calls()

    def test_loader_keeps_legacy_policy_pins_instead_of_recomputing_them_from_documents(self):
        for field in ('receipt_document_sha256', 'intent_sha256', 'selection_sha256', 'inventory_sha256'):
            original = self.policy[field]
            with self.subTest(policy=field):
                self.policy[field] = '0'*64; self.save_config()
                try:
                    with self.assertRaisesRegex(TaskError, '固定作者来源'):
                        self.load().prepare(self.task, '4'*32)
                finally:
                    self.policy[field] = original; self.save_config()
                self.assert_no_external_calls()

    def test_changed_original_source_bytes_are_rejected_without_model_or_round_consumption(self):
        workspace = self.load(); path = self.legacy.source/'README.md'
        original = path.read_bytes()
        path.chmod(0o600); path.write_bytes(b'x'*len(original)); path.chmod(0o400)
        with self.assertRaisesRegex(TaskError, '固定作者文本读取未完成'):
            workspace.prepare(self.task, '4'*32)
        self.assertEqual(self.capture.call_count, 1)
        self.model_transport.assert_not_called()
        self.assertEqual(self.fx.calls.status()['used_requests'], 0)
        self.assertEqual(workspace.planner.status(self.task)['used_rounds'], 0)
        path.chmod(0o600); path.write_bytes(original); path.chmod(0o400)
        restored = workspace.prepare(self.task, '4'*32)
        self.assertEqual(restored['state'], 'prepared', restored)
        self.assertEqual(restored['round'], 1)
        self.assertEqual(self.capture.call_count, 2)
        self.assertEqual(self.model_transport.call_count, 1)

    def test_wrong_task_policy_or_paper_association_never_reads_or_sends(self):
        self.policy['reference_task_id'] = 'e'*32; self.save_config()
        with self.assertRaisesRegex(TaskError, '任务绑定'):
            self.load()
        self.policy['reference_task_id'] = self.task
        self.association['paper']['title'] = 'Another synthetic paper'
        self.row['association'] = self.write_artifact('association', self.association); self.save_config()
        workspace = self.load()
        status = workspace.status(self.task)
        self.assertFalse(status['can_prepare']); self.assertEqual(status['history'], [])
        with self.assertRaisesRegex(TaskError, '论文关联'):
            workspace.prepare(self.task, '4'*32)
        self.assert_no_external_calls()

    def test_missing_saved_revision_and_changed_pinned_credentials_are_rejected(self):
        self.row['connection_revision'] = 999; self.save_config()
        with self.assertRaisesRegex(ValueError, 'revision does not exist'):
            self.load()
        self.row['connection_revision'] = self.revision; self.save_config()
        workspace = self.load(); transport = workspace.bindings[self.task]['reader'].transport
        credential = self.hpc.directory/(transport.identity['credential_id']+'.json')
        contents = json.loads(credential.read_bytes()); contents['known_hosts'] = 'CHANGED_SYNTHETIC_KEY'
        credential.write_bytes(canonical(contents))
        self.assertFalse(workspace.status(self.task)['can_prepare'])
        with self.assertRaisesRegex(TaskError, '来源连接变化'):
            workspace.prepare(self.task, '4'*32)
        self.assert_no_external_calls()

    def test_new_default_connection_does_not_migrate_pin_and_explicit_rebind_cannot_reuse_source(self):
        workspace = self.load(); pinned = workspace.bindings[self.task]['reader'].transport
        profile = deepcopy(pinned.identity['profile']); profile['host'] = 'new-synthetic.example.edu'
        self.hpc.save(profile, self.revision, known_hosts='NEW_SYNTHETIC_HOST_KEY')
        self.assertTrue(workspace.status(self.task)['can_prepare'])
        result = workspace.prepare(self.task, '4'*32)
        self.assertEqual(result['state'], 'prepared', result)
        command = self.capture.call_args.args[0]
        self.assertIn('saved.example.edu', command); self.assertNotIn('new-synthetic.example.edu', command)
        self.row['connection_revision'] = self.revision+1; self.save_config()
        migrated = self.load()
        with self.assertRaisesRegex(TaskError, '固定作者来源'):
            migrated.prepare(self.task, '5'*32, revision_note='Synthetic revision')
        self.assertEqual(self.capture.call_count, 1); self.assertEqual(self.model_transport.call_count, 1)
        self.assertEqual(self.fx.calls.status()['used_requests'], 1)
        self.assertEqual(migrated.planner.status(self.task)['used_rounds'], 1)


if __name__ == '__main__':
    unittest.main()
