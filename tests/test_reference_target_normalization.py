"""Saved synthetic replies only; no real P, provider or HPC operations."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import sqlite3
import unittest
from unittest.mock import patch

from auto_lammps.deepseek import DeepSeekClient, ModelCalls
from auto_lammps.manifest import canonical, sha256
from auto_lammps.reference_targets import (LEGACY_CONTROLLER_SHA256, LEGACY_ADAPTER_SHA256,
    LEGACY_SEMANTICS_SHA256, NORMALIZATION_RULES, normalize_target_presentation,
    target_analysis_context, validate_target_analysis, _review_semantics_sha256)
from auto_lammps.tasks import TaskError
import test_reference_targets as fixtures


class ReferenceTargetNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ReferenceTargetTests('test_missing_trusted_input_is_recorded_unavailable_without_model_call')
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.service=self.fixture.service
        self.raw=deepcopy(self.fixture.value)
        for row in self.raw['targets']:row['missing']='  Exact synthetic missing text\n'
        for row in self.raw['dependencies']:row['missing']='Synthetic dependency gap'
        self.raw['limitations']='Synthetic limitation'

    def current(self):
        f=self.fixture
        return self.service._context(f.task['id'],f.client,1,1)

    def old_failure(self, raw=None):
        """Retain a complete accounted legacy response and its failed review.

        Historical setup does not dispatch through today's changed contract.
        The old message/body, raw ModelCalls receipt and original failure are
        recorded separately, exactly as an already installed old controller.
        """
        f=self.fixture
        self.fixture.value=deepcopy(self.raw if raw is None else raw)
        context=self.service._legacy_context(self.current(),f.client)
        self.assertEqual(context['controller_sha256'],LEGACY_CONTROLLER_SHA256)
        payload=json.loads(context['messages'][-1]['content'])
        self.assertEqual(payload['scientific_adapter']['source_sha256'],LEGACY_ADAPTER_SHA256)
        self.assertNotIn('output_types',payload['reference_target_dependency_menu'])
        self.assertNotIn('presentation_normalization',payload['scientific_adapter']['rules'])
        with f.tasks.transaction() as db:
            db.execute('INSERT INTO reference_target_scopes VALUES (?,?,?)',(f.task['id'],3,'synthetic'))
            db.execute('INSERT INTO reference_target_intents VALUES (?,?,?,?,?)',
                (f.request_id,f.task['id'],canonical(context).decode(),sha256(canonical(context)),'synthetic'))
            self.service._event(db,f.request_id,'prepared')
        f.client.complete_json(f.request_id,context['messages'])
        call=f.calls.lookup(f.request_id)
        failure=self.service._finish(f.task['id'],f.request_id,'invalid_output',model_receipt=call['receipt'],
            code='target_output_invalid',message='建议中的缺项或限制列表无效。')
        self.original=self.service._stored(f.task['id'],f.request_id)
        self.original_call=deepcopy(call)
        self.failure=deepcopy(failure)
        return context

    def review(self, **kwargs):
        f=self.fixture
        with patch.object(f.client,'complete_json',side_effect=AssertionError('Review must never send a model request')):
            return self.service.review_existing(f.task['id'],f.request_id,kwargs.get('client',f.client),
                connection_revision=kwargs.get('connection_revision',1),
                credential_generation=kwargs.get('credential_generation',1))

    def assert_original_unchanged(self):
        f=self.fixture
        self.assertEqual(self.service._stored(f.task['id'],f.request_id),self.original)
        self.assertEqual(f.calls.lookup(f.request_id),self.original_call)
        self.assertEqual(f.calls.status()['used_requests'],1)
        self.assertEqual(len(f.sent),1)
        self.assertEqual(self.service.get(f.task['id'],f.request_id)['state'],'invalid_output')

    def test_exact_singleton_normalization_and_complete_explicit_menu_types(self):
        before=deepcopy(self.raw)
        normalized,proof=normalize_target_presentation(self.raw)
        report=validate_target_analysis(self.fixture.inputs,self.raw)
        self.assertEqual(self.raw,before)
        self.assertEqual(report['targets'][0]['missing'],['  Exact synthetic missing text\n'])
        self.assertEqual(proof['source_output_sha256'],sha256(canonical(self.raw)))
        self.assertEqual(proof['normalized_output_sha256'],sha256(canonical(normalized)))
        self.assertFalse(proof['semantic_changes'])
        self.assertEqual(len(proof['changed_fields']),len(self.raw['targets'])+len(self.raw['dependencies'])+1)
        menu=target_analysis_context(self.fixture.inputs)
        props=menu['output_types']['properties']
        self.assertEqual(props['targets']['items']['properties']['priority'],dict(type='integer',enum=[1,2,3]))
        self.assertEqual(props['dependencies']['maxItems'],512)
        self.assertEqual(menu['presentation_normalization'],NORMALIZATION_RULES)
        self.assertEqual(_review_semantics_sha256(),LEGACY_SEMANTICS_SHA256)

    def test_future_accept_records_normalization_and_keeps_raw_model_sha(self):
        self.fixture.value=deepcopy(self.raw)
        result=self.fixture.analyze()
        self.assertEqual(result['state'],'completed',result)
        receipt=self.fixture.calls.lookup(self.fixture.request_id)['receipt']
        self.assertEqual(receipt['structured_output'],self.raw)
        self.assertEqual(receipt['output_sha256'],sha256(canonical(self.raw)))
        self.assertEqual(result['receipt']['normalization']['source_output_sha256'],receipt['output_sha256'])
        self.assertFalse(result['execution_authorized']);self.assertFalse(result['B_input'])

    def test_zero_model_legacy_review_is_append_only_and_idempotent(self):
        self.old_failure()
        result=self.review()
        self.assertEqual(result['state'],'completed',result)
        self.assertEqual(result['new_model_calls'],0)
        self.assertFalse(result['suggestion_count_refunded'])
        self.assertEqual(result['normalization']['source_output_sha256'],self.original_call['receipt']['output_sha256'])
        self.assertEqual(self.review(),result)
        self.assertEqual(self.service.history(self.fixture.task['id'])[0]['reviews'],[result])
        self.assert_original_unchanged()
        with self.fixture.tasks.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM reference_target_intents').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM reference_target_reviews').fetchone()[0],1)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('UPDATE reference_target_reviews SET document="{}"')

    def test_concurrent_review_has_one_record_and_no_paid_call(self):
        self.old_failure()
        def attempt(_):
            try:return self.review()
            except TaskError:return None
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(attempt,range(2)))
        self.assertTrue(any(r and r['state']=='completed' for r in results))
        self.assertEqual(len(self.service.reviews(self.fixture.task['id'],self.fixture.request_id)),1)
        self.assert_original_unchanged()

    def test_empty_whitespace_excessive_nul_or_wrong_type_is_not_repaired(self):
        for invalid in ('','  ','x'*2001,'bad\x00text',False,7,{},[['nested']]):
            with self.subTest(invalid_type=type(invalid).__name__):
                value=deepcopy(self.raw);value['targets'][0]['missing']=invalid
                with self.assertRaises(TaskError):validate_target_analysis(self.fixture.inputs,value)

    def test_numeric_target_coverage_graph_and_citations_are_still_enforced(self):
        cases=[lambda v:v['targets'][0].update(priority='2'),lambda v:v['targets'].pop(),
            lambda v:v['targets'][0]['citations'][0].update(quote='Invented synthetic quote'),
            lambda v:v['dependencies'][0].update(requires=[v['dependencies'][0]['id']])]
        for mutate in cases:
            with self.subTest(mutation=mutate):
                value=deepcopy(self.raw);mutate(value)
                with self.assertRaises(TaskError):validate_target_analysis(self.fixture.inputs,value)

    def test_nonformat_saved_error_keeps_specific_reason_and_raw_reply(self):
        value=deepcopy(self.raw);value['targets'][0]['citations'][0]['quote']='Synthetic unsupported quote'
        self.old_failure(value)
        result=self.review()
        self.assertEqual(result['state'],'unavailable',result)
        self.assertEqual(result['error_code'],'citation_unavailable')
        self.assertIn('逐字匹配',result['message'])
        self.assertIsNone(result['report'])
        self.assert_original_unchanged()

    def test_source_change_model_generation_thinking_and_policy_are_rejected(self):
        self.old_failure()
        original=deepcopy(self.fixture.inputs)
        self.fixture.inputs['sources'][0]['text']+=' changed'
        with self.assertRaises(TaskError):self.review()
        self.fixture.inputs=original
        with self.assertRaises(TaskError):self.review(credential_generation=2)
        with self.assertRaises(TaskError):self.review(connection_revision=2)
        self.fixture.client.thinking=True
        with self.assertRaises(TaskError):self.review()
        self.fixture.client.thinking=False
        self.fixture.calls.amend_allowance(101,reason_sha256='e'*64)
        with self.assertRaises(TaskError):self.review()
        self.assertEqual(self.service.reviews(self.fixture.task['id'],self.fixture.request_id),[])
        self.assert_original_unchanged()

    def test_changed_ledger_or_unknown_receipt_is_rejected_without_model(self):
        self.old_failure()
        other=DeepSeekClient(ModelCalls(self.fixture.root/'other.sqlite',self.fixture.calls.config,max_requests=100))
        with self.assertRaises(TaskError):self.review(client=other)
        lookup=self.fixture.calls.lookup
        def unknown(identifier):
            row=deepcopy(lookup(identifier));row['receipt']['state']='unknown';return row
        with patch.object(self.fixture.calls,'lookup',unknown),self.assertRaises(TaskError):self.review()
        context=self.current()
        with self.fixture.tasks.transaction() as db:
            db.execute('INSERT INTO reference_target_intents VALUES (?,?,?,?,?)',
                ('e'*32,self.fixture.task['id'],canonical(context).decode(),sha256(canonical(context)),'synthetic'))
            self.service._event(db,'e'*32,'prepared')
        with self.assertRaises(TaskError):self.review()
        self.assertEqual(other.calls.status()['used_requests'],0)
        self.assertEqual(self.service.reviews(self.fixture.task['id'],self.fixture.request_id),[])

    def test_changed_output_sha_or_unreviewed_semantics_are_rejected(self):
        self.old_failure()
        lookup=self.fixture.calls.lookup
        def changed(identifier):
            row=deepcopy(lookup(identifier));row['receipt']['output_sha256']='f'*64;return row
        with patch.object(self.fixture.calls,'lookup',changed),self.assertRaises(TaskError):self.review()
        with patch('auto_lammps.reference_targets._review_semantics_sha256',return_value='f'*64),self.assertRaises(TaskError):self.review()
        self.assert_original_unchanged()

    def test_historical_context_cannot_change_permissions_or_controller(self):
        self.old_failure()
        read=self.service._stored
        def changed(task,request):
            value=list(read(task,request));value[1]=deepcopy(value[1]);value[1]['controller_sha256']='f'*64
            return tuple(value)
        with patch.object(self.service,'_stored',changed),self.assertRaises(TaskError):self.review()
        self.assert_original_unchanged()

    def test_history_is_stale_when_source_changes_without_touching_original_review(self):
        self.old_failure();review=self.review()
        self.fixture.inputs['sources'][0]['text']+=' changed'
        saved=self.service.get(self.fixture.task['id'],self.fixture.request_id)['reviews'][0]
        self.assertEqual(saved['state'],'source_unavailable')
        self.assertEqual(saved['original_state'],'completed')
        self.assertEqual(saved['report'],review['report'])
        with self.assertRaises(TaskError):self.review()

    def test_three_original_suggestions_stay_spent_after_free_review(self):
        self.old_failure()
        self.fixture.value=deepcopy(self.fixture.value)
        for request in ('e'*32,'f'*32):
            self.assertEqual(self.fixture.analyze(request_id=request)['state'],'completed')
        self.assertEqual(self.fixture.calls.status()['used_requests'],3)
        self.assertEqual(self.review()['state'],'completed')
        self.assertEqual(self.fixture.calls.status()['used_requests'],3)
        with self.assertRaisesRegex(TaskError,'三次'):self.fixture.analyze(request_id='a'*32)
        self.assertEqual(len(self.service.history(self.fixture.task['id'])),3)
        self.assertEqual(len(self.fixture.sent),3)


if __name__=='__main__':unittest.main()
