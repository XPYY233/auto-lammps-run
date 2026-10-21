"""Bounded literal excerpts and free revalidation of completed diagnoses."""
from copy import deepcopy
import unittest
from unittest.mock import Mock,patch

from auto_lammps.deepseek import ModelError
from auto_lammps.failure_recovery import diagnose, MAX_DIAGNOSIS_EXCERPTS
from auto_lammps.manifest import canonical,sha256
import test_agent_candidates as fixtures


class FailureDiagnosisRevalidationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.AgentCandidateTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def fixture(self,count):
        quotes=[f'ERROR: synthetic literal log event {index:02d}' for index in range(count)]
        evidence=dict(request_id='synthetic-cancelled',logs=[dict(tail='\n'.join(quotes))])
        value=dict(summary='Synthetic diagnosis.',evidence=quotes,cause='Unverified synthetic cause.',
                   repair='Retain the complete scope.',proposed_lesson='Unverified synthetic rule.')
        self.f.transport.side_effect=lambda *args:(200,fixtures.response(value))
        return evidence,value

    def test_nine_actual_log_excerpts_are_preserved_without_truncation(self):
        evidence,value=self.fixture(9)
        result=diagnose(self.f.client,evidence,dict(workflow='retained complete proposal'))
        self.assertEqual(result['evidence'],value['evidence'])
        self.assertEqual(len(result['evidence']),9)
        self.assertEqual(self.f.transport.call_count,1)

    def test_thirty_two_literal_excerpts_are_supported(self):
        evidence,value=self.fixture(MAX_DIAGNOSIS_EXCERPTS)
        self.assertEqual(MAX_DIAGNOSIS_EXCERPTS,32)
        result=diagnose(self.f.client,evidence,{})
        self.assertEqual(result['evidence'],value['evidence'])

    def test_more_than_thirty_two_excerpts_remain_rejected_and_recorded(self):
        evidence,_=self.fixture(MAX_DIAGNOSIS_EXCERPTS+1)
        with self.assertRaisesRegex(ValueError,'actual supplied'):diagnose(self.f.client,evidence,{})
        before=deepcopy(self.f.calls.history())
        with self.assertRaisesRegex(ValueError,'actual supplied'):diagnose(self.f.client,evidence,{})
        self.assertEqual(self.f.calls.history(),before)
        self.assertEqual(self.f.transport.call_count,1)

    def test_fabricated_empty_and_overlong_excerpts_are_not_admitted(self):
        cases=[('actual log','fabricated quote'),('actual log',''),('x'*2001,'x'*2001)]
        for index,(tail,quote) in enumerate(cases):
            evidence=dict(request_id=f'failed-{index}',logs=[dict(tail=tail)])
            value=dict(summary='Synthetic diagnosis.',evidence=[quote],cause='Unverified.',
                       repair='Retain scope.',proposed_lesson='Unverified.')
            with self.subTest(index=index):
                client=Mock();client.calls.lookup.return_value=None
                client.complete_json.return_value=dict(value=value,
                    receipt=dict(state='completed',output_sha256=sha256(canonical(value))))
                with self.assertRaisesRegex(ValueError,'actual supplied'):diagnose(client,evidence,{})

    def test_completed_response_revalidation_keeps_identity_body_output_cost_and_model_count(self):
        evidence,_=self.fixture(9);proposal=dict(workflow='retained complete proposal')
        first=diagnose(self.f.client,evidence,proposal)
        before=deepcopy(self.f.calls.lookup(first['request_id']))
        with patch.object(self.f.client,'complete_json',side_effect=AssertionError('No second provider request')):
            second=diagnose(self.f.client,evidence,proposal)
        self.assertEqual(second['request_id'],first['request_id'])
        self.assertEqual(second['receipt'],first['receipt'])
        self.assertEqual(self.f.calls.lookup(first['request_id']),before)
        self.assertEqual(self.f.calls.status()['used_requests'],1)
        self.assertEqual(self.f.transport.call_count,1)
        proof=second['scientific_adapter']['completed_response_revalidation']
        self.assertEqual(proof['request_sha256'],before['request_sha256'])
        self.assertEqual(proof['output_sha256'],before['receipt']['output_sha256'])
        self.assertFalse(proof['request_body_replayed'])
        self.assertEqual(second['validation_status'],'proposed_not_verified')

    def test_unknown_existing_diagnosis_stops_without_replay_or_new_identity(self):
        evidence,_=self.fixture(9)
        self.f.transport.side_effect=TimeoutError('Synthetic unknown dispatch')
        with self.assertRaises(ModelError):diagnose(self.f.client,evidence,{})
        before=deepcopy(self.f.calls.history())
        with self.assertRaisesRegex(ModelError,'request_already_reserved'):diagnose(self.f.client,evidence,{})
        self.assertEqual(self.f.calls.history(),before)
        self.assertEqual(self.f.transport.call_count,1)

    def test_changed_cached_request_or_output_proof_cannot_reach_a_model(self):
        evidence,_=self.fixture(9)
        first=diagnose(self.f.client,evidence,{})
        saved=deepcopy(self.f.calls.lookup(first['request_id']))
        for field in ('request_id','request_sha256','output_sha256'):
            changed=deepcopy(saved)
            if field=='output_sha256':changed['receipt'][field]='a'*64
            else:changed[field]='b'*32 if field=='request_id' else 'b'*64
            with self.subTest(field=field),patch.object(self.f.calls,'lookup',return_value=changed):
                with self.assertRaises(ValueError):diagnose(self.f.client,evidence,{})
        self.assertEqual(self.f.transport.call_count,1)

