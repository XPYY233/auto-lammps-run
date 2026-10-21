"""Received rejected drafts may guide fixed-tree discovery, never execution."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

from auto_lammps.author_resource_selection import AuthorResourceSelection, selection_context, selection_progression
from auto_lammps.tasks import TaskError
import test_author_resource_selection as fixtures
from test_deepseek import response


class RejectedAuthorDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.AuthorResourceSelectionTests()
        # The existing fixture's cleanup also protects immutable author history.
        self.fx.assert_author_history_unchanged=lambda:self.assertEqual(
            self.fx.fx.service.history(self.fx.task),self.fx.old_author)
        self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.fx.fx.value['invocations'][0]['entrypoint']['path']='missing.py'
        self.fx.fx.client.transport=Mock(return_value=(200,response(self.fx.fx.value)))
        result=self.fx.fx.service.prepare(self.fx.task,'6'*32,self.fx.fx.client,
            connection_revision=1,credential_generation=1,revision_note='Resolve the missing original.')
        self.assertEqual(result['state'],'invalid_output',result)
        self.fx.old_author=deepcopy(self.fx.fx.service.history(self.fx.task))
        self.fx.input['author_plan_request_id']='6'*32
        self.fx.fx.client.transport=self.fx.transport
        self.service=AuthorResourceSelection(self.fx.fx.service,self.fx.provider,
            connection_provider=self.fx.fx.connection_provider,model_calls=self.fx.fx.calls)

    def test_complete_rejected_answer_is_only_a_search_hint_and_reading_spends_nothing(self):
        before=self.fx.fx.calls.status()['used_requests']
        evidence=self.service._input(self.fx.task);hint=evidence['author_plan']
        self.assertEqual(hint['unvalidated_proposal'],self.fx.fx.value)
        self.assertIsNone(hint['report'])
        self.assertFalse(hint['source_references_verified'])
        self.assertFalse(hint['execution_authorized'])
        self.assertFalse(hint['scientific_validation'])
        self.assertEqual(hint['validation_feedback']['code'],'author_plan_invalid')
        self.assertIn('search clues',selection_context(evidence)['rules'][-1])
        self.assertEqual(self.fx.fx.calls.status()['used_requests'],before)
        self.fx.transport.assert_not_called()
        result=self.fx.prepare(service=self.service)
        self.assertEqual(result['state'],'selected',result)
        self.assertEqual(result['report']['paths'],['scripts/helper.py'])
        self.assertFalse(result['report']['execution_ready'])
        payload=json.loads(self.fx.transport.call_args.args[0])
        sent=json.loads(payload['messages'][-1]['content'])
        self.assertEqual(sent['author_plan'],hint)

    def test_unknown_or_missing_supplier_receipt_does_not_unlock_discovery(self):
        with patch.object(self.fx.fx.calls,'lookup',return_value=None):
            with self.assertRaises(TaskError):self.service._input(self.fx.task)
        self.fx.transport.assert_not_called()

    def test_changed_output_or_request_pin_does_not_unlock_discovery(self):
        original=self.fx.fx.calls.lookup('6'*32)
        for field,value in [('request_sha256','0'*64),('output_sha256','0'*64),('state','unknown')]:
            changed=deepcopy(original);changed['receipt'][field]=value
            with patch.object(self.fx.fx.calls,'lookup',return_value=changed):
                with self.assertRaises(TaskError):self.service._input(self.fx.task)
        self.fx.transport.assert_not_called()

    def test_no_bound_ledger_cannot_use_rejected_source_references(self):
        self.service.model_calls=None
        with self.assertRaises(TaskError):self.service._input(self.fx.task)
        self.fx.transport.assert_not_called()

    def test_initial_selection_can_progress_to_original_files_and_rejected_author_draft(self):
        current=self.service._input(self.fx.task)
        previous=deepcopy(current);previous['first_selection']=True;previous['author_plan']=None
        previous['sources']=[row for row in previous['sources'] if row['origin']=='paper']
        files=previous.pop('existing_files');previous['existing_files']=[]
        old_tree={row['path']:row for row in previous['tree_report']['entries']}
        previous['available_files']+= [deepcopy(old_tree[row['path']]) for row in files]
        receipt=dict(state='selected',report=dict(selections=deepcopy(files)))
        result=selection_progression(previous,current,receipt)
        self.assertEqual(result['added_files'],sorted(files,key=lambda row:row['path']))
        self.assertEqual(result['current_author_request'],'6'*32)
        self.assertFalse(result['execution_authorized'])
        for mutate in [lambda data:data['binding'].update(license_approval_sha256='0'*64),
                       lambda data:data['reference_scope'].update(scope_sha256='0'*64),
                       lambda data:data['available_files'][0].update(sha='0'*40),
                       lambda data:data['sources'][0].update(text='replaced original paper')]:
            wrong=deepcopy(current);mutate(wrong)
            with self.assertRaises(TaskError):selection_progression(previous,wrong,receipt)
        with self.assertRaises(TaskError):selection_progression(previous,current,dict(state='selected',report={'selections':[]}))

    def test_revision_retains_rejected_hint_and_previous_selection_without_resetting_rounds(self):
        first=self.fx.prepare(service=self.service)
        self.assertEqual(first['state'],'selected')
        # A new current author draft is allowed; the complete previous selection
        # and fixed sources remain present instead of restarting from scratch.
        second=self.fx.prepare('8'*32,service=self.service,revision_note='Inspect remaining fixed-tree originals.')
        self.assertEqual(second['state'],'selected',second)
        self.assertEqual(second['round'],2)
        self.assertEqual(second['revision']['complete_previous_answer'],self.fx.proposal)
        self.assertTrue(second['revision']['verified_source_progression']['original_scope_unchanged'])
