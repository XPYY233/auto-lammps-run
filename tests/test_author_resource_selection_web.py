"""Ordinary selection/fetch HTTP contracts; entirely synthetic, no network."""
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from auto_lammps.author_reference_workspace import AuthorReferenceWorkspace
from auto_lammps.tasks import TaskError
from auto_lammps.web import create_app
from test_deepseek import response
from test_web import HEADERS, ORIGIN
import test_author_resource_selection as selection_fixtures
import test_author_selected_resource_workspace as fetch_fixtures


class AuthorResourceSelectionWebTests(unittest.TestCase):
    def setUp(self):
        self.fx=selection_fixtures.AuthorResourceSelectionTests()
        self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        plans=self.fx.fx
        self.task=self.fx.task
        self.model_provider=Mock(side_effect=lambda _:dict(enabled=True,client=plans.client,
            connection_revision=1,credential_generation=1))
        self.workspace=AuthorReferenceWorkspace(plans.tasks,plans.papers,plans.scopes,{},
            model_provider=self.model_provider,connection_provider=plans.connection_provider,
            model_calls=plans.calls)
        # Production workspace/HTTP/controller with the existing fully validated,
        # synthetic pinned input provider; no deployment or remote reader is used.
        self.workspace.planner=plans.service
        self.workspace.resource_selections=self.fx.service
        self.workspace._resource_selection_input=self.fx.provider
        self.client=TestClient(create_app(plans.tasks,papers=plans.papers,
            author_workspace_service=self.workspace),base_url=ORIGIN,raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.route='/api/tasks/'+self.task+'/author-resources/selections'

    def post(self,request='7'*32,*,note='',task=None,extra=None):
        route='/api/tasks/'+(task or self.task)+'/author-resources/selections'
        return self.client.post(route,json=dict(request_id=request,revision_note=note,**(extra or {})),headers=HEADERS)

    def test_plain_GET_is_local_and_has_separate_zero_selection_rounds(self):
        before=self.fx.fx.calls.status()['used_requests']
        for _ in range(2):
            result=self.client.get(self.route)
            self.assertEqual(result.status_code,200,result.text)
            value=result.json()
            self.assertTrue(value['can_prepare']);self.assertEqual(value['used_rounds'],0)
            self.assertEqual(value['max_rounds'],3);self.assertEqual(value['history'],[])
            for key in ('calls_model_on_GET','submits_HPC','B_input','execution_authorized'):
                self.assertIs(value[key],False)
            self.assertEqual(value['acquisitions']['history'],[])
        self.assertEqual(self.fx.fx.calls.status()['used_requests'],before)
        self.fx.transport.assert_not_called()
        self.assertEqual(self.fx.fx.service.status(self.task)['used_rounds'],2)

    def test_browser_can_send_only_request_ID_and_optional_note(self):
        for extra in (dict(paths=['scripts/helper.py']),dict(repository='other/repo'),
            dict(commit='e'*40),dict(license_approved=True),dict(tree_report={}),
            dict(scope='research'),dict(execution_authorized=True),dict(operation_id='a'*32)):
            with self.subTest(extra=extra):
                result=self.post(extra=extra)
                self.assertEqual(result.status_code,422,result.text)
        self.assertEqual(self.fx.service.status(self.task)['used_rounds'],0)
        self.fx.transport.assert_not_called()
        result=self.client.post(self.route,json=dict(request_id='7'*32),headers=HEADERS)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['state'],'selected')
        self.assertEqual(self.fx.transport.call_count,1)
        self.assertFalse(result.json()['report']['scientific_applicability_verified'])
        self.assertFalse(result.json()['report']['source_complete'])

    def test_same_ID_reopens_immutable_selection_with_one_accounted_model_call(self):
        first=self.post(note='Keep the exact original scope.')
        self.assertEqual(first.status_code,200,first.text)
        repeated=self.post(note='Keep the exact original scope.')
        self.assertEqual(repeated.status_code,200,repeated.text)
        self.assertEqual(repeated.json(),first.json())
        self.assertEqual(self.client.get(self.route).json()['latest'],first.json())
        changed=self.post(note='Cannot replace the frozen note.')
        self.assertEqual(changed.status_code,422,changed.text)
        self.assertEqual(self.fx.transport.call_count,1)
        self.assertEqual(self.fx.fx.calls.status()['used_requests'],self.fx.baseline+1)

    def test_three_full_selection_versions_do_not_spend_author_plan_third_round(self):
        self.fx.proposal['full_invalid_answer']='SYNTHETIC_COMPLETE_PREVIOUS_'*500
        for digit in ('7','8','9'):
            result=self.post(digit*32,note='Preserve all original requirements.')
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(result.json()['state'],'invalid_output')
        state=self.client.get(self.route).json()
        self.assertEqual(state['used_rounds'],3);self.assertEqual(state['remaining_rounds'],0)
        self.assertFalse(state['can_prepare']);self.assertEqual(len(state['history']),3)
        final=state['latest']['revision']
        self.assertEqual(final['parent_request'],'8'*32)
        self.assertEqual(final['complete_previous_answer'],self.fx.proposal)
        self.assertFalse(final['target_scope_reduction_allowed'])
        blocked=self.post('e'*32,note='No fourth selection.')
        self.assertEqual(blocked.status_code,422,blocked.text)
        self.assertEqual(self.fx.transport.call_count,3)
        self.assertEqual(self.fx.fx.service.history(self.task),self.fx.old_author)

    def test_unknown_selection_blocks_all_new_IDs_and_original_reopens_without_send(self):
        self.fx.transport.side_effect=TimeoutError('SYNTHETIC_PRIVATE_ERROR')
        first=self.post()
        self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(first.json()['state'],'unknown')
        status=self.client.get(self.route)
        self.assertEqual(status.status_code,200,status.text)
        self.assertTrue(status.json()['pending_reconciliation']);self.assertFalse(status.json()['can_prepare'])
        self.assertNotIn('SYNTHETIC_PRIVATE_ERROR',status.text)
        blocked=self.post('8'*32,note='New identity cannot bypass unknown.')
        self.assertEqual(blocked.status_code,422,blocked.text)
        reopened=self.post()
        self.assertEqual(reopened.status_code,200,reopened.text)
        self.assertEqual(reopened.json(),first.json())
        self.assertEqual(self.fx.transport.call_count,1)
        self.assertEqual(self.fx.service.status(self.task)['used_rounds'],1)

    def test_completed_model_reply_recovers_same_ID_after_local_metadata_unavailable(self):
        def received(*args):
            self.fx.provider.side_effect=TaskError('Synthetic metadata temporarily unavailable')
            return 200,response(deepcopy(self.fx.proposal))
        self.fx.transport.side_effect=received
        paused=self.post()
        self.assertEqual(paused.status_code,200,paused.text)
        self.assertEqual(paused.json()['state'],'preparation_paused')
        self.assertTrue(self.client.get(self.route).json()['latest']['recoverable'])
        blocked=self.post('8'*32,note='Must recover original metadata request.')
        self.assertEqual(blocked.status_code,422,blocked.text)
        self.fx.provider.side_effect=lambda _:deepcopy(self.fx.input)
        restored=self.post()
        self.assertEqual(restored.status_code,200,restored.text)
        self.assertEqual(restored.json()['state'],'selected')
        self.assertEqual(restored.json()['context_sha256'],paused.json()['context_sha256'])
        self.assertEqual(self.fx.transport.call_count,1)

    def test_zero_model_allowance_creates_no_selection_or_unknown_record(self):
        calls=self.fx.fx.calls
        for index in range(calls.status()['remaining_requests']):
            calls.reserve(format(index,'032x'),b'synthetic other accounted request')
        self.fx.provider.reset_mock()
        blocked=self.post()
        self.assertEqual(blocked.status_code,422,blocked.text)
        self.assertEqual(self.fx.service.status(self.task)['history'],[])
        self.fx.provider.assert_not_called();self.fx.transport.assert_not_called()

    def test_same_selection_ID_cannot_cross_task_and_B_cannot_enter_reference_selection(self):
        self.assertEqual(self.post().status_code,200)
        other=self.fx.fx.tasks.create('Another synthetic P','Synthetic scope only','reproduction')['id']
        blocked=self.post(task=other)
        self.assertEqual(blocked.status_code,422,blocked.text)
        empty=self.client.get('/api/tasks/'+other+'/author-resources/selections')
        self.assertEqual(empty.status_code,200,empty.text)
        self.assertEqual(empty.json()['history'],[]);self.assertFalse(empty.json()['can_prepare'])
        research=self.fx.fx.tasks.create('Synthetic research','Answer-free B condition','research')['id']
        blocked=self.post('8'*32,task=research)
        self.assertEqual(blocked.status_code,422,blocked.text)
        self.assertEqual(self.fx.transport.call_count,1)

    def test_changed_fixed_source_disables_new_selection_and_fetch_but_retains_history(self):
        first=self.post();self.assertEqual(first.status_code,200,first.text)
        self.fx.input['binding']['transport_identity_sha256']='d'*64
        blocked=self.post('8'*32,note='Changed binding is not a new proposal.')
        self.assertEqual(blocked.status_code,422,blocked.text)
        fetch=self.client.post('/api/tasks/'+self.task+'/author-resources/selected-files',
            json=dict(request_id='c'*32,selection_request_id='7'*32),headers=HEADERS)
        self.assertEqual(fetch.status_code,422,fetch.text)
        state=self.client.get(self.route).json()
        self.assertEqual(state['latest'],first.json());self.assertEqual(state['acquisitions']['history'],[])
        self.assertEqual(self.fx.transport.call_count,1)


class AuthorSelectedFilesWebTests(unittest.TestCase):
    def setUp(self):
        self.fx=fetch_fixtures.AuthorSelectedWorkspaceTests()
        self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.fx.workspace.selected_resources=self.fx.service
        self.fx.workspace.execution=None
        self.client=TestClient(create_app(self.fx.tasks,author_workspace_service=self.fx.workspace),
            base_url=ORIGIN,raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        guard=patch('auto_lammps.slurm_read.subprocess.Popen',
            side_effect=AssertionError('Only synthetic acquisition capture is allowed'))
        guard.start();self.addCleanup(guard.stop)
        self.route='/api/tasks/'+self.fx.task+'/author-resources/selected-files'

    def post(self,operation=None,selection=None,task=None,extra=None):
        route='/api/tasks/'+(task or self.fx.task)+'/author-resources/selected-files'
        return self.client.post(route,json=dict(request_id=operation or self.fx.operation,
            selection_request_id=selection or self.fx.selection_id,**(extra or {})),headers=HEADERS)

    def test_fetch_accepts_only_saved_selection_ID_and_operation_ID_and_is_idempotent(self):
        for extra in (dict(paths=['arbitrary.txt']),dict(repository='other/repo'),
                      dict(commit='f'*40),dict(license_approved=True),dict(selections=[])):
            with self.subTest(extra=extra):
                blocked=self.post(extra=extra)
                self.assertEqual(blocked.status_code,422,blocked.text)
        self.assertEqual(self.fx.actions,[])
        result=self.post();self.assertEqual(result.status_code,200,result.text)
        latest=result.json()['latest']
        self.assertEqual(latest['state'],'selected_complete')
        self.assertFalse(latest['source_complete']);self.assertEqual(self.fx.actions,['acquire'])
        count=len(self.fx.fx.blobs.calls)
        again=self.post();self.assertEqual(again.status_code,200,again.text)
        self.assertEqual(again.json(),result.json());self.assertEqual(self.fx.actions,['acquire'])
        self.assertEqual(len(self.fx.fx.blobs.calls),count)
        self.assertFalse(result.json()['execution_authorized']);self.assertFalse(result.json()['B_input'])

    def test_unknown_fetch_can_only_reconcile_original_operation_and_selection(self):
        self.fx.lose_response=True
        result=self.post();self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['latest']['state'],'unknown')
        count=len(self.fx.fx.blobs.calls)
        for operation,selection in [('d'*32,'8'*32),('d'*32,self.fx.selection_id),
                                     (self.fx.operation,'8'*32)]:
            with self.subTest(operation=operation,selection=selection):
                blocked=self.post(operation,selection)
                self.assertEqual(blocked.status_code,422,blocked.text)
        self.fx.lose_response=False
        recovered=self.post();self.assertEqual(recovered.status_code,200,recovered.text)
        self.assertEqual(recovered.json()['latest']['state'],'selected_complete')
        self.assertEqual(self.fx.actions,['acquire','reconcile'])
        self.assertEqual(len(self.fx.fx.blobs.calls),count)

    def test_cross_task_ID_or_unselected_request_cannot_start_fetch(self):
        for task,operation,selection in [(self.fx.other,self.fx.operation,self.fx.selection_id),
            (self.fx.task,'d'*32,'9'*32)]:
            with self.subTest(task=task,operation=operation):
                blocked=self.post(operation,selection,task)
                self.assertEqual(blocked.status_code,422,blocked.text)
        self.assertEqual(self.fx.actions,[]);self.assertEqual(self.fx.factory_calls,0)
        result=self.post();self.assertEqual(result.status_code,200,result.text)
        count=len(self.fx.fx.blobs.calls)
        borrowed=self.post(task=self.fx.other)
        self.assertEqual(borrowed.status_code,422,borrowed.text)
        self.assertEqual(len(self.fx.fx.blobs.calls),count)


if __name__=='__main__':unittest.main()
