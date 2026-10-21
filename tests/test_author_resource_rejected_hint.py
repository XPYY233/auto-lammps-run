"""Received rejected drafts may guide fixed-tree discovery, never execution."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

from auto_lammps.author_resource_selection import AuthorResourceSelection, selection_context
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
