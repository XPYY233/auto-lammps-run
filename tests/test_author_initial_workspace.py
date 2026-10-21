"""Initial reference onboarding over real controllers and synthetic I/O only."""
import base64
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from auto_lammps.author_initial_workspace import InitialAuthorWorkspace, READING_PURPOSE, _reading_selections
from auto_lammps.author_reference_workspace import AuthorReferenceWorkspace
from auto_lammps.author_source_worker import MAX_LINES, MAX_TEXT_BYTES
from auto_lammps.manifest import canonical, sha256
from auto_lammps.reference_selected_resources import ReferenceSelectedResources
from auto_lammps.reference_scope import ReferenceScopeService
from auto_lammps import reference_selected_resource_worker as selected_worker
from auto_lammps import selected_author_source_worker as source_worker
from auto_lammps.selected_author_source_reader import SelectedAuthorSourceReader
from auto_lammps.tasks import TaskError
import test_author_resource_bootstrap as bootstrap_fixture


class InitialAuthorWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.fx = bootstrap_fixture.AuthorResourceBootstrapTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.tasks, self.papers = self.fx.tasks, self.fx.papers
        self.scopes = object.__new__(ReferenceScopeService)
        self.scopes.tasks, self.scopes.papers = self.tasks, self.papers
        self.scopes.get = Mock(side_effect=lambda _: deepcopy(self.fx.scope))
        self.task, self.bootstrap = self.fx.task['id'], self.fx.service
        self.bootstrap.scopes = self.scopes
        self.fx.repository.files['opaque.bin'] = b'\x00synthetic binary\xff'
        self.sources = [dict(id='paper-1', origin='paper', locator='Synthetic P', text='Synthetic unchanged P scope.')]
        self.paper_input = Mock(side_effect=lambda *args, **kwargs: dict(sources=deepcopy(self.sources)))
        self.scopes.targets = SimpleNamespace(_input=self.paper_input)
        self.model = Mock(side_effect=AssertionError('No model belongs to this bridge'))
        self.workspace = AuthorReferenceWorkspace(self.tasks, self.papers, self.scopes, {},
            model_provider=self.model, connection_provider=Mock(side_effect=AssertionError('No model connection')))
        self.initial = InitialAuthorWorkspace(self.workspace, self.bootstrap)
        self.workspace.initial = self.initial
        self.selection_id, self.operation = 'c'*32, 'd'*32
        self.actions = []
        self.workspace.selected_resources.factory = self.factory
        guard = patch('subprocess.Popen', side_effect=AssertionError('No real process or SSH'))
        guard.start()
        self.addCleanup(guard.stop)

    def inspect(self):
        result = self.fx.inspect()
        self.assertEqual(result['state'], 'metadata_ready', result)
        return self.bootstrap.metadata_for_selection(self.task)

    def approve(self):
        metadata = self.inspect()
        return self.initial.approve_reading(self.task, metadata['bootstrap_request_id'],
            metadata['bootstrap_receipt_sha256'], 'Synthetic project reading acknowledgement')

    def capture(self, command, **options):
        envelope = json.loads(b''.join(options['input_chunks']))
        self.actions.append(envelope['action'])
        self.assertEqual(command[0], 'ssh')
        self.assertEqual(sha256(base64.b64decode(envelope['bundle'])), envelope['bundle_sha256'])
        with patch('sys.platform', 'linux'), patch.dict(os.environ, {'SSH_CONNECTION': 'synthetic only'}):
            report = selected_worker.selected_resources(envelope['request'], action=envelope['action'],
                transport=self.fx.repository, previous_bundle_sha256=envelope.get('previous_bundle_sha256'))
        return dict(returncode=0, failure='', stdout=base64.b64encode(canonical(report)).decode(), stderr='')

    def factory(self, *args, **kwargs):
        return ReferenceSelectedResources(*args, **kwargs, capture=self.capture)

    def acquire(self, paths=('input.lmp', 'opaque.bin')):
        raw = self.initial.selection_input(self.task)
        metadata = self.bootstrap.metadata_for_selection(self.task)
        rows = {row['path']: row for row in metadata['tree_report']['entries']}
        report = dict(status='selected', scope='reference_only', B_input=False, execution_authorized=False,
            repository=metadata['acquisition_request']['repository'], commit=metadata['acquisition_request']['commit'],
            tree_sha1=raw['tree_request']['tree_sha1'],
            acquisition_receipt_sha256=sha256(canonical(raw['acquisition_receipt'])),
            tree_report_sha256=sha256(canonical(raw['tree_report'])),
            selections=[dict(path=path, blob_sha1=rows[path]['sha'], size=rows[path]['size'],
                git_mode=rows[path]['mode'], reason='Synthetic stored AI selection', dependency_ids=['shared'])
                for path in paths], missing=['Synthetic original execution environment still unknown.'])
        self.workspace.resource_selections.selection = Mock(return_value=report)
        result = self.workspace.selected_resources.acquire(self.task, self.operation, self.selection_id)
        self.assertEqual(result['latest']['state'], 'selected_complete', result)
        return result['latest']['report']

    def test_status_and_missing_bootstrap_are_local_and_never_call_model(self):
        status = self.initial.status(self.task)
        self.assertTrue(status['enabled'], 'Initial inspection must remain available before a receipt exists.')
        self.assertFalse(status['reading_approved'])
        self.assertFalse(status['binding_ready'])
        self.assertIsNone(status['receipt_sha256'])
        self.assertFalse(status['calls_model'])
        self.fx.capture.assert_not_called()
        self.model.assert_not_called()
        self.paper_input.assert_not_called()

    def test_explicit_purpose_is_immutable_idempotent_and_never_legal_or_execution_approval(self):
        metadata = self.inspect()
        before = len(self.fx.repository.calls)
        with self.assertRaises(TaskError):
            self.initial.selection_input(self.task)
        self.assertEqual(self.initial._approvals(self.task), [])
        result = self.approve()
        approval = result['reading_approval']
        self.assertTrue(result['reading_approved'])
        self.assertFalse(result['binding_ready'])
        self.assertEqual(approval['document']['purpose'], READING_PURPOSE)
        for key in ('use_approved', 'redistribution_approved', 'execution_authorized', 'contract_signed', 'B_input'):
            self.assertIs(approval['document'][key], False)
        self.assertEqual(approval['document']['license_status'], 'text_found_scope_unverified')
        self.assertEqual(approval['document']['binding']['license_evidence_sha256'],
                         sha256(canonical(metadata['license_evidence'])))
        self.initial.approve_reading(self.task, self.fx.request, metadata['bootstrap_receipt_sha256'], 'Changed note')
        restarted = InitialAuthorWorkspace(self.workspace, self.bootstrap)
        self.assertEqual(restarted._approvals(self.task), [approval])
        self.assertEqual(len(self.fx.repository.calls), before)
        self.assertFalse(self.bootstrap.metadata_for_selection(self.task)['license_evidence']['read_approved'])
        with self.tasks.transaction() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('DELETE FROM author_initial_read_approvals')

    def test_wrong_original_receipt_cannot_approve_or_fetch(self):
        self.inspect()
        before = len(self.fx.repository.calls)
        with self.assertRaises(TaskError):
            self.initial.approve_reading(self.task, self.fx.request, '0'*64)
        self.assertEqual(self.initial._approvals(self.task), [])
        self.assertEqual(len(self.fx.repository.calls), before)

    def test_missing_license_text_keeps_candidate_but_cannot_grant_reading(self):
        del self.fx.repository.files['LICENSE']
        metadata = self.inspect()
        self.assertEqual(metadata['license_evidence']['files'], [])
        with self.assertRaisesRegex(TaskError, '许可证原文'):
            self.initial.approve_reading(self.task, self.fx.request, metadata['bootstrap_receipt_sha256'])
        self.assertEqual(self.initial._approvals(self.task), [])
        self.assertFalse(self.initial.status(self.task)['reading_approved'])

    def test_selection_provider_uses_entire_p_scope_sources_and_original_empty_receipt(self):
        approved = self.approve()
        raw = self.workspace._resource_selection_input(self.task)
        expected = {'task_id','scope','role','B_input','author_plan_request_id','tree_request','tree_report',
                    'acquisition_receipt','existing_files','binding','mode','reference_sources','reference_scope'}
        self.assertEqual(set(raw), expected)
        self.assertEqual(raw['mode'], 'initial_scope_selection')
        self.assertIsNone(raw['author_plan_request_id'])
        self.assertEqual(raw['reference_scope'], self.fx.scope)
        self.assertEqual(raw['reference_sources'], self.sources)
        self.assertEqual(raw['existing_files'], [])
        self.assertEqual(raw['acquisition_receipt']['files'], [])
        self.assertFalse(raw['acquisition_receipt']['source_complete'])
        self.assertEqual(raw['binding']['license_approval_sha256'], approved['reading_approval']['sha256'])
        self.paper_input.assert_called_with(self.task, active=False)
        self.assertEqual(self.workspace.planner.history(self.task), [])
        self.model.assert_not_called()

    def test_research_and_existing_private_binding_never_consult_initial_metadata(self):
        research = self.tasks.create('Synthetic B', 'Synthetic answer-free research', 'research')['id']
        getter = Mock(side_effect=AssertionError('B must not receive reference metadata'))
        with patch.object(self.bootstrap, 'metadata_for_selection', getter), patch.object(self.bootstrap, 'status', getter):
            self.assertFalse(self.initial.status(research)['enabled'])
            for method in (self.initial.selection_input, self.initial.resource_binding, self.initial.load_binding):
                with self.assertRaises(TaskError): method(research)
            with self.assertRaises(TaskError): self.initial.approve_reading(research, 'e'*32, 'f'*64)
        getter.assert_not_called()
        self.workspace.bindings[self.task] = {'sentinel': True}
        with patch.object(self.bootstrap, 'metadata_for_selection', getter):
            with self.assertRaises(TaskError): self.initial.selection_input(self.task)
        getter.assert_not_called()

    def test_dynamic_binding_requires_actual_completed_selection_and_preserves_bootstrap_context(self):
        self.approve()
        before, _ = self.initial.resource_binding(self.task)
        with self.assertRaises(TaskError): self.initial.load_binding(self.task)
        receipt = self.acquire()
        binding = self.initial.load_binding(self.task)
        self.assertIsInstance(binding['reader'], SelectedAuthorSourceReader)
        self.assertEqual(binding['format'], 'selected_acquisition_v1')
        self.assertEqual(binding['load_receipt'](), receipt)
        self.assertEqual(binding['receipt_file_sha256'], sha256(canonical(receipt)))
        self.assertEqual(binding['receipt_proof']['sha256'], sha256(canonical(receipt)))
        self.assertFalse(binding['receipt_proof']['disk_artifact'])
        self.assertEqual(binding['selections'], [dict(path='input.lmp', start_line=1, end_line=MAX_LINES)])
        self.assertEqual(binding['unread_files'], ['opaque.bin'])
        self.assertEqual(binding['deployment']['environments'], [])
        self.assertTrue(binding['deployment']['missing'])
        self.assertFalse(binding['deployment']['resources']['gpu_allowed'])
        self.assertEqual(binding['invocation_paths'], [])
        self.assertEqual(self.initial.resource_binding(self.task)[0], before)
        loaded, paper, actual, scope = self.workspace._metadata(self.task)
        self.assertEqual(actual, receipt)
        self.assertEqual(scope, self.fx.scope)
        self.assertEqual(paper['id'], self.fx.paper['id'])
        self.assertEqual(loaded['load_association']()['resource_receipt_sha256'], sha256(canonical(receipt)))
        self.assertEqual(self.actions, ['acquire'])
        self.assertTrue(self.initial.status(self.task)['binding_ready'])
        self.assertEqual(self.workspace.planner.history(self.task), [])
        self.model.assert_not_called()
        for path in self.workspace.resources.audit.rglob('*'):
            if path.is_file():
                self.assertNotEqual(path.read_bytes(), self.fx.repository.files['input.lmp'])

    def test_prepare_action_reuses_initial_proofs_and_rechecks_once_without_cross_action_cache(self):
        self.approve();self.acquire()
        original=self.bootstrap.metadata_for_selection
        with patch.object(self.bootstrap,'metadata_for_selection',wraps=original) as metadata:
            with self.workspace._metadata_reads(self.task,'e'*32):
                first=self.workspace._metadata(self.task)
                for _ in range(5):
                    value=self.workspace._metadata(self.task)
                    value[0]['load_receipt']();value[0]['load_association']();value[0]['verify_license']()
                self.assertEqual(metadata.call_count,1)
                self.workspace._validate_metadata_reads()
                self.assertEqual(metadata.call_count,2)
            with self.workspace._metadata_reads(self.task,'f'*32):self.workspace._metadata(self.task)
            self.assertEqual(metadata.call_count,3)
        self.assertIsNone(self.workspace.metadata_action.get());self.model.assert_not_called()

    def test_initial_action_refuses_changed_original_bootstrap_after_snapshot(self):
        self.approve();self.acquire()
        with self.workspace._metadata_reads(self.task,'e'*32):
            self.workspace._metadata(self.task)
            with patch.object(self.bootstrap,'metadata_for_selection',side_effect=TaskError('Original scope changed')):
                with self.assertRaisesRegex(TaskError,'Original scope changed'):self.workspace._validate_metadata_reads()
        self.assertIsNone(self.workspace.metadata_action.get());self.model.assert_not_called()

    def test_initial_action_refuses_a_saved_connection_change_after_snapshot(self):
        self.approve();self.acquire()
        with self.workspace._metadata_reads(self.task,'e'*32):
            self.workspace._metadata(self.task)
            profile=dict(self.fx.profile,work_directory=str(self.fx.f.root/'changed-remote'))
            self.fx.hpc.save(profile,1,known_hosts='Synthetic changed saved connection')
            with self.assertRaises(TaskError):self.workspace._validate_metadata_reads()
        self.assertIsNone(self.workspace.metadata_action.get());self.model.assert_not_called()

    def test_scope_change_pauses_reading_without_rewriting_approval(self):
        approved = self.approve()['reading_approval']
        self.fx.scope['scope_sha256'] = 'b'*64
        with self.assertRaises(TaskError): self.initial.resource_binding(self.task)
        status = self.initial.status(self.task)
        self.assertFalse(status['reading_approved'])
        self.assertEqual(status['approval_history'], [approved])

    def test_paused_task_keeps_bootstrap_and_approval_history_visible_without_reading(self):
        approved = self.approve()['reading_approval']
        self.tasks.set_paused(self.task, self.tasks.get(self.task)['revision'], True)
        status = self.initial.status(self.task)
        self.assertFalse(status['enabled'])
        self.assertFalse(status['binding_ready'])
        self.assertEqual(status['approval_history'], [approved])
        self.assertEqual(status['latest']['request_id'], self.fx.request)
        with self.assertRaises(TaskError): self.initial.selection_input(self.task)

    def test_saved_connection_change_cannot_migrate_prior_approval(self):
        approved = self.approve()['reading_approval']
        profile = dict(self.fx.profile, work_directory=str(self.fx.f.root/'different-remote'))
        self.fx.hpc.save(profile, 1, known_hosts='Synthetic-new-known-host')
        with self.assertRaises(TaskError): self.initial.resource_binding(self.task)
        self.assertEqual(self.initial._approvals(self.task), [approved])

    def test_bootstrap_license_or_catalog_proof_change_cannot_reuse_reading_acknowledgement(self):
        self.approve()
        original = self.bootstrap.metadata_for_selection(self.task)
        for field in ('license_evidence', 'catalog_association'):
            changed = deepcopy(original)
            changed[field]['synthetic_changed_proof'] = True
            with patch.object(self.bootstrap, 'metadata_for_selection', return_value=changed):
                with self.assertRaises(TaskError): self.initial.selection_input(self.task)
        self.assertEqual(len(self.initial._approvals(self.task)), 1)

    def test_bound_closures_revalidate_completed_receipt_without_reinterpreting_history(self):
        self.approve()
        self.acquire()
        binding = self.initial.load_binding(self.task)
        receipt, context = self.workspace.selected_resources.completed(self.task)[0]
        changed = deepcopy(receipt)
        changed['selected_complete'] = False
        with patch.object(self.workspace.selected_resources, 'completed', return_value=[(changed, context)]):
            for method in ('load_receipt', 'load_association', 'verify_license'):
                with self.assertRaises(TaskError): binding[method]()

    def test_binary_only_completion_remains_retained_and_explicitly_unread(self):
        self.approve()
        self.acquire(paths=('opaque.bin',))
        with self.assertRaisesRegex(TaskError, '二进制'):
            self.initial.load_binding(self.task)
        self.assertFalse(self.initial.status(self.task)['binding_ready'])
        self.assertEqual(self.workspace.selected_resources.completed(self.task)[0][0]['files'][0]['path'], 'opaque.bin')
        self.assertEqual(self.actions, ['acquire'])

    def test_foreign_completed_context_is_not_admitted_as_real_source(self):
        self.approve()
        self.acquire()
        receipt, context = self.workspace.selected_resources.completed(self.task)[0]
        context['acquisition_receipt']['purpose'] = 'different-original'
        with patch.object(self.workspace.selected_resources, 'completed', return_value=[(receipt, context)]):
            with self.assertRaises(TaskError): self.initial.load_binding(self.task)

    def test_existing_author_plan_uses_original_revision_input_and_real_retained_inventory(self):
        self.approve()
        receipt = self.acquire()
        history = [dict(request_id='e'*32, report=dict(missing=['Synthetic full prior clarification']))]
        with patch.object(self.workspace.planner, 'history', return_value=history):
            raw = self.initial.selection_input(self.task)
        self.assertEqual(set(raw), {'task_id','scope','role','B_input','author_plan_request_id','tree_request',
                                  'tree_report','acquisition_receipt','existing_files','binding'})
        self.assertEqual(raw['author_plan_request_id'], 'e'*32)
        self.assertEqual(raw['existing_files'],
            [{key: row[key] for key in ('path','size','sha256')} for row in receipt['files']])
        self.assertEqual(raw['acquisition_receipt']['files'], [])
        self.model.assert_not_called()

    def test_ordinary_collect_sources_reads_actual_selected_text_once_and_keeps_binary_unread(self):
        self.approve()
        self.acquire()
        reads = []

        def capture(command, **options):
            envelope = json.loads(b''.join(options['input_chunks']))
            reads.append(envelope['request'])
            with patch('sys.platform', 'linux'), patch.dict(os.environ, {'SSH_CONNECTION': 'synthetic only'}):
                report = source_worker.read_source_report(envelope['request'])
            return dict(returncode=0, failure='', stdout=base64.b64encode(canonical(report)).decode(), stderr='')

        def reader(*args, **kwargs):
            return SelectedAuthorSourceReader(*args, **kwargs, capture=capture)

        with patch('auto_lammps.author_initial_workspace.SelectedAuthorSourceReader', side_effect=reader), \
                patch.object(self.workspace, '_source', wraps=self.workspace._source) as source:
            result = self.workspace.collect_sources(self.task)
            self.assertEqual(source.call_count, 1, 'The base selected receipt must not be appended a second time.')
        self.assertEqual(result['files'], 2)
        self.assertEqual(result['read_files'], 1)
        self.assertEqual(result['unread_files'], ['opaque.bin'])
        self.assertEqual(result['source_fragments'], 1)
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0]['selections'][0]['path'], 'input.lmp')
        self.assertEqual(self.workspace.planner.history(self.task), [])
        self.model.assert_not_called()

    def test_ordinary_read_follows_only_missing_421_line_tail_and_keeps_original_receipts(self):
        self.fx.repository.files['input.lmp']=''.join('# Synthetic line '+str(i)+'\n' for i in range(1,422)).encode()
        self.approve();self.acquire();reads=[]
        def capture(command,**options):
            envelope=json.loads(b''.join(options['input_chunks']));reads.append(deepcopy(envelope['request']))
            with patch('sys.platform','linux'),patch.dict(os.environ,{'SSH_CONNECTION':'synthetic only'}):
                report=source_worker.read_source_report(envelope['request'])
            return dict(returncode=0,failure='',stdout=base64.b64encode(canonical(report)).decode(),stderr='')
        def reader(*args,**kwargs):return SelectedAuthorSourceReader(*args,**kwargs,capture=capture)
        # Retain the old first-page format before ordinary source collection.
        with patch('auto_lammps.author_initial_workspace.SelectedAuthorSourceReader',side_effect=reader):
            binding,_,receipt,_=self.workspace._metadata(self.task)
            choices=binding['selections'];request=binding['reader']._selection(receipt,choices)
            report=binding['reader'].read(receipt,choices,purpose='author_reference')
            original=binding['reader'].retained(receipt,[dict(request=request,response=report)])
            self.workspace._save_source_evidence(self.task,original)
            before=canonical(original)
            result=self.workspace.collect_sources(self.task)
            self.assertEqual(result['complete_text_files'],['input.lmp'])
            self.assertEqual(result['truncated_files'],[])
            fragments=result['text_coverage']['complete_files'][0]['fragments']
            self.assertEqual([(r['start_line'],r['returned_end_line']) for r in fragments],[(1,400),(401,421)])
            self.assertTrue(all(r['truncated'] for r in fragments))
            self.assertEqual(len(reads),2)
            self.assertEqual(reads[-1]['selections'][0]['start_line'],401)
            self.assertEqual(reads[-1]['selections'][0]['end_line'],421)
            self.assertEqual(self.workspace.collect_sources(self.task)['complete_text_files'],['input.lmp'])
            self.assertEqual(len(reads),2,'Already complete text must not be read again')
            cached=self.workspace._cached_sources(self.task,binding,receipt)
            self.assertEqual(len(cached['reads']),2)
        with self.tasks.transaction() as db:
            records=[json.loads(row[0]) for row in db.execute('SELECT document FROM author_source_evidence WHERE task_id=?',(self.task,))]
        self.assertIn(before,[canonical(row) for row in records]);self.assertEqual(len(records),2)
        self.assertEqual(self.workspace.planner.history(self.task),[]);self.model.assert_not_called()

    def test_metadata_budget_prefers_source_then_documentation_and_keeps_explicit_unread_reasons(self):
        files = [dict(path='small.txt', size=1), dict(path='data.csv', size=200),
                 dict(path='large.txt', size=16*1024+1), dict(path='README.md', size=1024),
                 dict(path='script.mod', size=MAX_TEXT_BYTES-1024)]
        selections, unread, total = _reading_selections(files)
        self.assertEqual(total, MAX_TEXT_BYTES)
        self.assertEqual(selections, [dict(path=path, start_line=1, end_line=MAX_LINES)
                                     for path in ('script.mod', 'README.md')])
        self.assertEqual({row['path']: row['reason'] for row in unread},
            {'small.txt': 'source_text_byte_budget', 'data.csv': 'raw_data_not_source_text',
             'large.txt': 'large_text_not_source_description'})

    def test_fifty_small_source_files_read_with_templates_and_large_data_retained_unread(self):
        extensions = ('.py', '.sh', '.in', '.mod', '.prm', '.meam', '.json')
        data = {'source/file-'+str(index)+extensions[index % len(extensions)]: b'synthetic source\n'*120
                for index in range(35)}
        data['README.md'] = b'synthetic documentation\n'*500
        data.update({'notes/small-'+str(index)+'.txt': b'synthetic note\n'*180 for index in range(14)})
        data['data/large.txt'] = b'x'*(1500*1024)
        data.update({'data/raw-'+str(index)+('.csv' if index % 2 else '.dat'): b'0 1\n'*5000
                     for index in range(6)})
        self.assertEqual(len(data), 57)
        self.fx.repository.files.update(data)
        self.approve()
        receipt = self.acquire(paths=tuple(data))
        binding = self.initial.load_binding(self.task)
        self.assertEqual(len(binding['selections']), 50)
        self.assertEqual(len(binding['unread_files']), 7)
        self.assertEqual(binding['text_selection_bytes'],
            sum(row['size'] for row in receipt['files'] if row['path'] not in binding['unread_files']))
        self.assertLess(binding['text_selection_bytes'], MAX_TEXT_BYTES)
        self.assertEqual(binding['text_selection_byte_limit'], MAX_TEXT_BYTES)
        for extension in ('.mod', '.prm', '.meam'):
            self.assertTrue(any(row['path'].endswith(extension) for row in binding['selections']))
        reasons = {row['path']: row['reason'] for row in binding['unread_file_reasons']}
        self.assertEqual(reasons['data/large.txt'], 'large_text_not_source_description')
        self.assertEqual(sum(reason == 'raw_data_not_source_text' for reason in reasons.values()), 6)
        self.assertTrue(any('TXT超过16KiB' in item for item in binding['missing']))
        reads = []

        def capture(command, **options):
            envelope = json.loads(b''.join(options['input_chunks']))
            reads.append(envelope['request'])
            with patch('sys.platform', 'linux'), patch.dict(os.environ, {'SSH_CONNECTION': 'synthetic only'}):
                report = source_worker.read_source_report(envelope['request'])
            return dict(returncode=0, failure='', stdout=base64.b64encode(canonical(report)).decode(), stderr='')

        def reader(*args, **kwargs):
            return SelectedAuthorSourceReader(*args, **kwargs, capture=capture)

        with patch('auto_lammps.author_initial_workspace.SelectedAuthorSourceReader', side_effect=reader), \
                patch('urllib.request.build_opener', side_effect=AssertionError('No API or network')):
            result = self.workspace.collect_sources(self.task)
        self.assertEqual((result['files'], result['read_files']), (57, 50))
        self.assertEqual(set(result['unread_files']), set(binding['unread_files']))
        requested = {row['path'] for request in reads for row in request['selections']}
        self.assertEqual(len(requested), 50)
        self.assertFalse(requested.intersection(binding['unread_files']))
        self.assertEqual(self.actions, ['acquire'])
        self.model.assert_not_called()


if __name__ == '__main__':
    unittest.main()
