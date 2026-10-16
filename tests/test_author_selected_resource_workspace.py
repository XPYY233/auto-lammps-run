"""Reference selection control using actual acquisition tools and synthetic I/O."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from auto_lammps.author_reference_resources import AuthorReferenceResources
from auto_lammps.author_selected_resource_workspace import AuthorSelectedResourceWorkspace
from auto_lammps.manifest import canonical,sha256
from auto_lammps.reference_selected_resources import ReferenceSelectedResources
from auto_lammps.tasks import TaskStore,TaskError
import test_reference_selected_resources as fixtures


class AuthorSelectedWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.SelectedResourceTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.tasks=TaskStore(self.fx.root/'tasks.sqlite')
        self.task=self.tasks.create('Synthetic paper','Synthetic reference scope','reproduction')['id']
        self.other=self.tasks.create('Another synthetic paper','Another scope','reproduction')['id']
        self.selection_id='7'*32;self.operation='c'*32
        self.raw=dict(tree_request=deepcopy(self.fx.inventory_request),
            acquisition_receipt=deepcopy(self.fx.old),tree_report=deepcopy(self.fx.inventory))
        self.report=dict(status='selected',scope='reference_only',B_input=False,execution_authorized=False,
            repository=self.fx.original['repository'],commit=self.fx.original['commit'],tree_sha1=self.fx.inventory['tree_sha1'],
            acquisition_receipt_sha256=sha256(canonical(self.fx.old)),tree_report_sha256=sha256(canonical(self.fx.inventory)),
            selections=[dict(row,reason='Synthetic missing author original',dependency_ids=['shared'])
                        for row in reversed(self.fx.choices(3))])
        self.source_context=dict(task_id=self.task,repository=self.report['repository'],commit=self.report['commit'],
            license_approval_sha256='1'*64,transport_identity_sha256=sha256(canonical(self.fx.transport.identity)),
            host_alias='offline-hpc')
        self.source_contexts={self.task:self.source_context,self.other:dict(self.source_context,task_id=self.other)}
        self.selections={(self.task,self.selection_id):deepcopy(self.report)}
        self.selected=Mock(side_effect=self.selection)
        self.workspace=SimpleNamespace(tasks=self.tasks)
        self.workspace.locks=self.fx.root/'locks';self.workspace.locks.mkdir(mode=0o700)
        resources=object.__new__(AuthorReferenceResources)
        resources.workspace=self.workspace;resources.tasks=self.tasks;resources.audit=self.fx.root/'resources'
        resources.audit.mkdir(mode=0o700)
        resources._binding=Mock(side_effect=lambda identifier:(deepcopy(self.source_contexts[identifier]),self.fx.transport))
        self.workspace.resources=resources
        self.workspace.resource_selections=SimpleNamespace(selection=self.selected)
        self.workspace._resource_selection_input=Mock(side_effect=lambda identifier:deepcopy(self.raw))
        self.actions=[];self.lose_response=False;self.invalid_response=False
        self.factory_calls=0
        self.service=AuthorSelectedResourceWorkspace(self.workspace,factory=self.factory)

    def selection(self,identifier,request):
        value=self.selections.get((identifier,request))
        if value is None:raise TaskError('selection belongs to another task or is not selected')
        return deepcopy(value)

    def capture(self,command,**options):
        envelope=json.loads(b''.join(options['input_chunks']));self.actions.append(envelope['action'])
        result=self.fx.capture(command,**options)
        if self.lose_response:return dict(returncode=None,failure='timeout',stdout='',stderr='')
        if self.invalid_response:
            import base64
            report=json.loads(base64.b64decode(result['stdout']));report['source_complete']=True
            result['stdout']=base64.b64encode(canonical(report)).decode()
        return result

    def factory(self,*args,**kwargs):
        self.factory_calls+=1
        return ReferenceSelectedResources(*args,**kwargs,capture=self.capture)

    def acquire(self,operation=None,selection=None,identifier=None):
        return self.service.acquire(identifier or self.task,operation or self.operation,selection or self.selection_id)

    def test_actual_selected_receipt_is_retained_and_completed_does_not_mean_whole_repository(self):
        result=self.acquire();latest=result['latest']
        self.assertEqual(latest['state'],'selected_complete');self.assertTrue(latest['selected_complete'])
        self.assertFalse(latest['source_complete']);self.assertEqual(self.actions,['acquire'])
        completed=self.service.completed(self.task);self.assertEqual(len(completed),1)
        report,context=completed[0]
        self.assertEqual(report,latest['report']);self.assertEqual(context['selection_report'],self.report)
        self.assertEqual(context['selection_sha256'],sha256(canonical(self.report)))
        self.assertEqual(context['request']['selected'],sorted(self.fx.choices(3),key=lambda row:row['path']))
        self.assertEqual((Path(self.fx.old['remote_directory'])/'result.json').read_bytes(),self.fx.original_bytes)

    def test_cross_task_operation_or_model_selection_is_rejected_before_acquisition(self):
        self.acquire();calls=len(self.fx.blobs.calls)
        with self.assertRaisesRegex(TaskError,'另一任务'):
            self.acquire(identifier=self.other)
        with self.assertRaisesRegex(TaskError,'another task'):
            self.acquire(operation='d'*32,identifier=self.other)
        self.assertEqual(len(self.fx.blobs.calls),calls);self.assertEqual(self.service.status(self.other)['history'],[])

    def test_terminal_same_operation_only_reads_retained_state(self):
        self.acquire();count=len(self.fx.blobs.calls);self.selected.reset_mock()
        self.workspace._resource_selection_input.reset_mock();self.workspace.resources._binding.reset_mock()
        self.assertEqual(self.acquire()['latest']['state'],'selected_complete')
        self.selected.assert_not_called();self.workspace._resource_selection_input.assert_not_called()
        self.workspace.resources._binding.assert_not_called();self.assertEqual(len(self.fx.blobs.calls),count)
        self.assertEqual(self.actions,['acquire'])

    def test_unknown_reuses_original_operation_for_reconcile_without_model_or_new_download(self):
        self.lose_response=True;value=self.acquire();self.assertEqual(value['latest']['state'],'unknown')
        count=len(self.fx.blobs.calls);self.selected.reset_mock()
        self.workspace._resource_selection_input.reset_mock()
        with self.assertRaisesRegex(TaskError,'待核对'):
            self.acquire(operation='d'*32,selection='8'*32)
        self.selected.assert_not_called();self.workspace._resource_selection_input.assert_not_called()
        self.lose_response=False
        value=self.acquire();self.assertEqual(value['latest']['state'],'selected_complete')
        self.assertEqual(self.actions,['acquire','reconcile']);self.assertEqual(len(self.fx.blobs.calls),count)
        self.selected.assert_not_called();self.workspace._resource_selection_input.assert_not_called()
        rows=self.service._rows(self.task)
        self.assertEqual(rows[0]['events'][1]['report']['state'],'unknown')
        self.assertEqual(len(rows),1)

    def test_same_selected_set_under_another_model_request_does_not_create_an_unknown_stub(self):
        self.acquire();self.selections[(self.task,'8'*32)]=deepcopy(self.report)
        self.selections[(self.task,'8'*32)]['selections'][0]['reason']='Changed prose only'
        with self.assertRaisesRegex(TaskError,'相同原件集合'):
            self.acquire(operation='d'*32,selection='8'*32)
        self.assertEqual(len(self.service.status(self.task)['history']),1)
        self.assertEqual(self.actions,['acquire']);self.assertEqual(len(self.fx.blobs.calls),3)

    def test_same_model_selection_cannot_get_a_second_operation_identity(self):
        self.acquire()
        with self.assertRaisesRegex(TaskError,'已有获取记录'):
            self.acquire(operation='d'*32)
        with self.assertRaisesRegex(TaskError,'另一选择'):
            self.acquire(selection='8'*32)
        self.assertEqual(len(self.service.status(self.task)['history']),1)

    def test_selection_and_second_metadata_snapshot_must_have_identical_pins(self):
        for field in ('repository','commit','tree_sha1','tree_report_sha256','acquisition_receipt_sha256'):
            self.selections[(self.task,self.selection_id)]=deepcopy(self.report)
            self.selections[(self.task,self.selection_id)][field]='f'*64
            with self.subTest(field=field),self.assertRaisesRegex(TaskError,'原模型选择'):
                self.acquire()
            self.assertEqual(self.service.status(self.task)['history'],[])
        self.assertEqual(self.actions,[])

    def test_partial_and_forged_selected_receipts_never_enter_completed(self):
        self.fx.blobs.change=lambda value,count:value.update(sha='f'*40) if count==2 else None
        partial=self.acquire();self.assertEqual(partial['latest']['state'],'partial')
        self.assertEqual(self.service.completed(self.task),[]);self.assertEqual(self.actions,['acquire'])
        self.assertEqual(self.acquire()['latest']['state'],'partial');self.assertEqual(self.actions,['acquire'])

    def test_forged_whole_repository_receipt_becomes_unknown_and_keeps_remote_originals(self):
        self.invalid_response=True;result=self.acquire()
        self.assertEqual(result['latest']['state'],'unknown');self.assertEqual(self.service.completed(self.task),[])
        self.invalid_response=False
        self.assertEqual(self.acquire()['latest']['state'],'selected_complete')
        self.assertEqual(self.actions,['acquire','reconcile']);self.assertEqual(len(self.fx.blobs.calls),3)

    def test_changed_saved_connection_cannot_migrate_original_unknown_operation(self):
        self.lose_response=True;self.acquire();self.lose_response=False
        self.source_context['transport_identity_sha256']='f'*64
        self.assertEqual(self.acquire()['latest']['state'],'unknown')
        self.assertEqual(self.actions,['acquire'])
        self.assertEqual(self.service.completed(self.task),[])

    def test_history_and_status_never_invoke_models_or_remote_tools(self):
        result=self.service.status(self.task);self.assertIsNone(result['latest'])
        self.assertEqual(self.factory_calls,0);self.selected.assert_not_called()
        self.workspace.resources._binding.assert_not_called()
        self.workspace._resource_selection_input.assert_not_called();self.assertEqual(self.actions,[])

    def test_corrupt_model_selection_binding_is_rejected_even_when_outer_document_hash_is_recomputed(self):
        self.acquire()
        with self.tasks.transaction() as db:
            row=db.execute('SELECT * FROM author_selected_operations WHERE id=?',(self.operation,)).fetchone()
            context=json.loads(row['document']);context['selection_report']['commit']='f'*40
            db.execute('DROP TRIGGER immutable_author_selected_operations_UPDATE')
            db.execute('UPDATE author_selected_operations SET document=?,sha256=? WHERE id=?',
                (canonical(context).decode(),sha256(canonical(context)),self.operation))
        with self.assertRaisesRegex(TaskError,'原模型选择'):self.service.status(self.task)
        self.assertEqual(self.actions,['acquire'])


if __name__=='__main__':unittest.main()
