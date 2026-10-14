"""Synthetic same-answer revisions; no real model, paper or HPC."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch
from auto_lammps.manifest import canonical, sha256
from auto_lammps.reference_targets import target_revision_feedback, reference_target_input
from auto_lammps.scientific_adapters import validate_prepared_messages
import test_reference_targets as fixtures


class ReferenceTargetIterationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ReferenceTargetTests('test_missing_trusted_input_is_recorded_unavailable_without_model_call')
        self.f.setUp();self.addCleanup(self.f.doCleanups)

    def failed(self):
        self.f.value['targets'][0]['citations'][0]['quote']='Synthetic unsupported quotation.'
        first=self.f.analyze()
        self.assertEqual(first['state'],'unavailable')
        return first,deepcopy(self.f.calls.lookup(self.f.request_id))

    def test_feedback_reports_all_bad_quotes_without_changing_original(self):
        value=deepcopy(self.f.value)
        value['targets'][0]['citations'][0]['quote']='bad quotation one'
        value['dependencies'][0]['citations'][0]['quote']='bad quotation two'
        before=deepcopy(value)
        feedback=target_revision_feedback(self.f.inputs,value)
        paths=[e['path'] for e in feedback['errors']]
        self.assertIn(['targets',0,'citations',0],paths)
        self.assertIn(['dependencies',0,'citations',0],paths)
        self.assertEqual(value,before)
        self.assertEqual(feedback['source_output_sha256'],sha256(canonical(value)))
        self.assertFalse(feedback['automatic_scientific_repair'])
        self.assertFalse(feedback['scope_reduction_allowed'])

    def test_next_request_carries_complete_previous_answer_and_full_sources_with_adapter(self):
        first,original=self.failed()
        second=self.f.analyze(request_id='e'*32)
        self.assertEqual(second['state'],'unavailable')
        payload=json.loads(self.f.sent[1]['messages'][-1]['content'])
        parent=payload['reference_target_revision']
        self.assertEqual(parent['parent_request'],self.f.request_id)
        self.assertEqual(parent['complete_previous_answer'],original['receipt']['structured_output'])
        self.assertEqual(payload['sources'],self.f.inputs['sources'])
        self.assertEqual(len(payload['inventory']['targets']),len(self.f.inputs['inventory']['targets']))
        self.assertEqual(payload['scientific_adapter']['stage'],'reference_target_analysis')
        proof=validate_prepared_messages('reference_target_analysis',self.f.sent[1]['messages'],reference_target_input(self.f.inputs))
        self.assertEqual(proof['input_check'],'passed')
        self.assertIn('rather than starting from scratch',self.f.sent[1]['messages'][0]['content'])
        self.assertEqual(self.f.calls.lookup(self.f.request_id),original)
        self.assertEqual(self.f.calls.status()['used_requests'],2)
        self.assertEqual(self.f.service.get(self.f.task['id'],self.f.request_id)['receipt'],first['receipt'])
        again=self.f.analyze(request_id='e'*32)
        self.assertEqual(again,second)
        self.assertEqual(len(self.f.sent),2)

    def test_corrected_third_answer_still_has_original_three_call_cap_and_retained_failures(self):
        self.failed();self.f.analyze(request_id='e'*32)
        self.f.value['targets'][0]['citations'][0]['quote']='Synthetic P evidence.'
        result=self.f.analyze(request_id='f'*32)
        self.assertEqual(result['state'],'completed')
        payload=json.loads(self.f.sent[2]['messages'][-1]['content'])
        self.assertEqual(payload['reference_target_revision']['parent_request'],'e'*32)
        self.assertEqual(len(self.f.service.history(self.f.task['id'])),3)
        with self.assertRaises(Exception):self.f.analyze(request_id='a'*32)
        self.assertEqual(len(self.f.sent),3)
        self.assertEqual(self.f.calls.status()['used_requests'],3)

    def test_changed_source_does_not_start_over_or_use_stale_answer(self):
        self.failed();self.f.inputs['sources'][0]['text']+=' Changed synthetic input.'
        result=self.f.analyze(request_id='e'*32)
        self.assertEqual(result['state'],'unavailable')
        self.assertEqual(result['receipt']['error_code'],'revision_parent_changed')
        self.assertEqual(len(self.f.sent),1)
        self.assertEqual(self.f.calls.status()['used_requests'],1)

    def test_unbound_model_output_is_rejected_without_call(self):
        self.failed();original_lookup=self.f.calls.lookup
        def modified(request):
            value=deepcopy(original_lookup(request))
            if value and request==self.f.request_id:value['receipt']['structured_output']['limitations']=['Changed output']
            return value
        with patch.object(self.f.calls,'lookup',side_effect=modified):
            result=self.f.analyze(request_id='e'*32)
        self.assertEqual(result['state'],'unavailable')
        self.assertEqual(result['receipt']['error_code'],'revision_parent_changed')
        self.assertEqual(len(self.f.sent),1)

    def test_other_task_answer_never_becomes_parent(self):
        self.failed()
        task=self.f.tasks.create('Other synthetic reference','Different case','reproduction')
        paper=self.f.papers.get(self.f.paper['id'])
        self.f.papers.link_task(paper['id'],paper['revision'],task['id'])
        self.f.provider.side_effect=lambda identifier:{**deepcopy(self.f.inputs),'task_id':identifier}
        result=self.f.service.analyze(task['id'],'e'*32,self.f.client,
            connection_revision=1,credential_generation=1)
        self.assertEqual(result['state'],'unavailable')
        payload=json.loads(self.f.sent[1]['messages'][-1]['content'])
        self.assertEqual(payload['task_id'],task['id'])
        self.assertNotIn('reference_target_revision',payload)
        self.assertEqual(len(self.f.sent),2)
