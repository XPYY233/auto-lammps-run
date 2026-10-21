"""Environment dependency inspection consumes only verified retained originals."""
import base64
from copy import deepcopy
import json
import os
import unittest
from unittest.mock import Mock, patch

from auto_lammps.author_reference_plan import author_source_inputs
from auto_lammps.manifest import canonical
from auto_lammps.selected_author_source_reader import SelectedAuthorSourceReader
from auto_lammps import selected_author_source_worker as selected_worker
from auto_lammps.tasks import TaskError
import test_author_reference_workspace as workspace_fixture
import test_author_initial_workspace as initial_fixture
import test_legacy_author_source_reader as legacy_fixture


class CachedAuthorEnvironmentSourcesTests(unittest.TestCase):
    def setUp(self):
        self.fx=workspace_fixture.AuthorWorkspaceTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.workspace,self.task=self.fx.workspace,self.fx.task

    def reject_side_effects(self):
        self.fx.source.capture=Mock(side_effect=AssertionError('Cached sources must not read HPC'))
        self.workspace._environment_metadata=Mock(side_effect=AssertionError('No recursive environment projection'))
        self.workspace.environment_inventory=Mock()
        self.workspace.environment_inventory.status.side_effect=AssertionError('No environment inventory recursion')

    def test_returns_standard_checked_original_bytes_without_environment_or_remote_work(self):
        self.workspace.collect_sources(self.task)
        binding,_,receipt,_=self.workspace._metadata(self.task)
        expected=author_source_inputs(self.workspace._cached_sources(self.task,binding,receipt),self.task)
        before=canonical(expected);self.reject_side_effects()
        checked=self.workspace.cached_environment_sources(self.task)
        self.assertEqual(canonical(checked),before)
        self.assertTrue(checked['files']);self.assertTrue(checked['reads']);self.assertTrue(checked['sources'])
        self.fx.source.capture.assert_not_called();self.workspace._environment_metadata.assert_not_called()
        self.workspace.environment_inventory.status.assert_not_called()
        self.assertEqual(self.workspace.planner.history(self.task),[]);self.assertEqual(self.fx.fx.sent,[])

    def test_missing_or_empty_cache_is_explicit_and_never_fetches(self):
        self.reject_side_effects()
        with self.assertRaisesRegex(TaskError,'缓存尚未就绪'):
            self.workspace.cached_environment_sources(self.task)
        with patch.object(self.workspace,'_cached_sources',return_value=None):
            with self.assertRaisesRegex(TaskError,'缺少已核验原件文本缓存'):
                self.workspace.cached_environment_sources(self.task)
        self.assertEqual(self.fx.fx.reader.calls,0);self.fx.source.capture.assert_not_called()
        self.assertEqual(self.workspace.planner.history(self.task),[])

    def test_default_metadata_still_projects_environment_and_raw_reads_have_separate_action_keys(self):
        self.workspace.collect_sources(self.task)
        projection=Mock(side_effect=lambda identifier,binding:dict(binding,environment_marker='projected'))
        with patch.object(self.workspace,'_environment_metadata',projection):
            with self.workspace._metadata_reads(self.task,'4'*32):
                normal=self.workspace._metadata(self.task,require_scope=False)
                raw=self.workspace._metadata(self.task,require_scope=False,include_environment=False)
                self.assertEqual(normal[0]['environment_marker'],'projected')
                self.assertNotIn('environment_marker',raw[0])
                self.workspace.cached_environment_sources(self.task)
                self.assertEqual(projection.call_count,1)

    def test_action_reuse_is_defensive_and_changed_license_cannot_cross_action_or_validation(self):
        self.workspace.collect_sources(self.task);self.reject_side_effects()
        verify=self.fx.binding['verify_license'];verify.reset_mock()
        with self.workspace._metadata_reads(self.task,'4'*32):
            first=self.workspace.cached_environment_sources(self.task)
            original=canonical(first);first['sources'][0]['text']='Caller mutation'
            self.assertEqual(canonical(self.workspace.cached_environment_sources(self.task)),original)
            self.assertEqual(verify.call_count,1)
            verify.side_effect=TaskError('Changed retained source license')
            with self.assertRaisesRegex(TaskError,'license'):self.workspace._validate_metadata_reads()
        self.assertIsNone(self.workspace.metadata_action.get())
        with self.assertRaisesRegex(TaskError,'license'):self.workspace.cached_environment_sources(self.task)
        self.fx.source.capture.assert_not_called()

    def test_wrong_task_changed_original_or_corrupt_cache_cannot_borrow_old_text(self):
        self.workspace.collect_sources(self.task);self.reject_side_effects()
        other=self.fx.fx.tasks.create('Unrelated reference','Synthetic independent request','reproduction')['id']
        with self.assertRaises(TaskError):self.workspace.cached_environment_sources(other)
        self.fx.association['paper']['title']='Changed source association'
        with self.assertRaisesRegex(TaskError,'关联回执'):self.workspace.cached_environment_sources(self.task)
        self.fx.association['paper']['title']=self.fx.fx.paper['title']
        with self.fx.fx.tasks.transaction() as db:
            db.execute('DROP TRIGGER immutable_author_source_evidence_UPDATE')
            db.execute('UPDATE author_source_evidence SET document=?',('{}',))
        with self.assertRaisesRegex(TaskError,'摘要不一致'):self.workspace.cached_environment_sources(self.task)
        self.fx.source.capture.assert_not_called()

    def test_legacy_cached_input_retains_original_format_and_receipts(self):
        legacy=legacy_fixture.LegacyAuthorSourceReaderTests();legacy.setUp();self.addCleanup(legacy.doCleanups)
        legacy.policy['reference_task_id']=self.task;reader=legacy.service()
        self.fx.binding.update(format='legacy_selected_resource_v3',reader=reader,
            selections=legacy.selections(),load_receipt=lambda:deepcopy(legacy.receipt),
            load_intent=lambda:deepcopy(legacy.intent),receipt_file_sha256=legacy.policy['receipt_sha256'])
        self.fx.association.update(repository=legacy.receipt['repository'],commit=legacy.receipt['commit'],
            resource_receipt_sha256=legacy.policy['receipt_sha256'])
        with patch.object(self.workspace.resources,'completed',return_value=None), \
                patch.object(self.workspace.selected_resources,'completed',return_value=[]):
            self.workspace.collect_sources(self.task)
            reader.capture=Mock(side_effect=AssertionError('No legacy reread'))
            with patch.object(self.workspace,'_environment_metadata',side_effect=AssertionError('No inventory projection')):
                checked=self.workspace.cached_environment_sources(self.task)
        self.assertEqual(checked['capability']['source_format'],'legacy_selected_resource_v3')
        self.assertEqual([row['path'] for row in checked['files']],['README.md','workflow/entry.py'])
        self.assertEqual(checked['reads'][0]['path'],'README.md');reader.capture.assert_not_called()


class InitialCachedAuthorEnvironmentSourcesTests(unittest.TestCase):
    def test_initial_and_supplemental_selected_packages_reuse_exact_cache_without_inventory_projection(self):
        fx=initial_fixture.InitialAuthorWorkspaceTests();fx.setUp();self.addCleanup(fx.doCleanups)
        fx.fx.repository.files['extra.py']=b'# Synthetic original dependency\n'
        fx.approve();fx.acquire()
        fx.operation='e'*32;fx.selection_id='f'*32;fx.acquire(paths=('extra.py',))
        calls=[]
        def capture(command,**options):
            envelope=json.loads(b''.join(options['input_chunks']));calls.append(envelope['request'])
            with patch('sys.platform','linux'),patch.dict(os.environ,{'SSH_CONNECTION':'synthetic only'}):
                report=selected_worker.read_source_report(envelope['request'])
            return dict(returncode=0,failure='',stdout=base64.b64encode(canonical(report)).decode(),stderr='')
        def reader(*args,**kwargs):return SelectedAuthorSourceReader(*args,**kwargs,capture=capture)
        with patch('auto_lammps.author_initial_workspace.SelectedAuthorSourceReader',side_effect=reader), \
                patch('auto_lammps.selected_author_source_reader.SelectedAuthorSourceReader',side_effect=reader):
            fx.workspace.collect_sources(fx.task)
        self.assertEqual(len(calls),2)
        with patch.object(fx.workspace,'_environment_metadata',side_effect=AssertionError('No environment recursion')), \
                patch.object(SelectedAuthorSourceReader,'read',side_effect=AssertionError('No remote read')):
            checked=fx.workspace.cached_environment_sources(fx.task)
        self.assertEqual(set(row['path'] for row in checked['files']),{'input.lmp','opaque.bin','extra.py'})
        self.assertEqual(set(row['path'] for row in checked['reads']),{'input.lmp','extra.py'})
        self.assertEqual(len(checked['sources']),2)
        self.assertEqual(fx.workspace.planner.history(fx.task),[]);fx.model.assert_not_called()


if __name__=='__main__':unittest.main()
