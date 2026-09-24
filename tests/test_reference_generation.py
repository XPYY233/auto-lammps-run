"""Synthetic runtime API responses only; no actual literature/model execution."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from auto_lammps.deepseek import DeepSeekClient, DeepSeekConfig, ModelCalls, ModelError
from auto_lammps.reference_generation import generate_reference_draft, reference_sources, validate_reference
from auto_lammps.tasks import FIELDS, TaskError, TaskStore
from test_deepseek import response
from test_literature import export_csv
from test_tasks import evidence


class ReferenceGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = TaskStore(self.root / 'tasks.sqlite')
        self.doc = self.store.create('Synthetic reference', 'Synthetic source only', 'reproduction')
        self.csv = export_csv(source_excerpt='At 250 K, density is 7.654321 g/cm3, computed with LAMMPS.',
                              value_text='999999 digitization-only marker')
        self.sources, _ = reference_sources([self.csv])
        sid = self.sources[0]['id']
        self.output = dict(conditions=[dict(field='temperature', value='250', unit='K',
                              source_id=sid, quote='At 250 K')], questions=[], results=[dict(
                              quantity='density', value='7.654321', unit='g/cm3', source_id=sid,
                              quote='density is 7.654321 g/cm3', method_class='lammps_direct',
                              method_source_id=sid, method_quote='computed with LAMMPS')])
        self.calls = ModelCalls(self.root / 'calls.sqlite', DeepSeekConfig('synthetic'), max_requests=3)
        self.transport = Mock(side_effect=lambda *args: (200, response(self.output)))
        self.client = DeepSeekClient(self.calls, transport=self.transport, key_reader=lambda: 'synthetic-key')

    def generate(self, revision=None, csv=None):
        return generate_reference_draft(self.client, self.store, self.doc['id'],
                                        self.doc['revision'] if revision is None else revision,
                                        [self.csv if csv is None else csv])

    def test_quoted_conditions_and_results_are_saved_separately(self):
        result = self.generate()
        self.assertEqual(result['fields']['temperature']['candidates'][0]['value'], '250')
        self.assertFalse(result['fields']['temperature']['confirmed'])
        batch = next(iter(result['reference_batches'].values()))
        self.assertEqual(batch['reported_results'][0]['value'], '7.654321')
        self.assertFalse(batch['reported_results'][0]['eligible_for_scoring'])
        self.assertFalse(batch['reference_qualified'])
        self.assertEqual(batch['reported_results'][0]['method_evidence']['quote'], 'computed with LAMMPS')
        self.assertNotIn('7.654321', str(result['fields']))
        self.assertIn('999999 digitization-only marker', str(batch['exports']))
        self.assertNotIn(b'999999 digitization-only marker', self.transport.call_args.args[0])
        self.assertEqual(self.store.history(self.doc['id'])[-1]['event'], 'reference_evidence_generated')
        self.assertEqual(self.store.get(self.doc['id']), result)

    def test_repeat_and_reopen_reuse_import_without_calling_again(self):
        result = self.generate()
        self.store = TaskStore(self.store.path)
        self.assertEqual(self.generate(), result)
        self.assertEqual(self.transport.call_count, 1)
        self.assertEqual(len(self.calls.history()), 1)

    def test_saved_completion_recovers_after_import_interruption(self):
        with patch.object(self.store, 'import_generated_reference', side_effect=OSError('synthetic interruption')):
            with self.assertRaises(OSError): self.generate()
        self.assertEqual(self.store.get(self.doc['id']), self.doc)
        self.store = TaskStore(self.store.path)
        self.client = DeepSeekClient(ModelCalls.open_existing(self.calls.path), transport=self.transport,
                                    key_reader=lambda: 'synthetic-key')
        result = self.generate()
        self.assertIn('reference_batches', result)
        self.assertEqual(self.transport.call_count, 1)

    def test_concurrent_request_observes_inflight_intent_without_resending(self):
        started, release = threading.Event(), threading.Event()
        def transport(*args):
            started.set()
            if not release.wait(5): raise RuntimeError('synthetic timeout')
            return 200, response(self.output)
        self.transport.side_effect = transport
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.generate)
            try:
                self.assertTrue(started.wait(5))
                with self.assertRaisesRegex(ModelError, 'requires_attention'): self.generate()
            finally:
                release.set()
            self.assertIn('reference_batches', future.result(timeout=5))
        self.assertEqual(self.transport.call_count, 1)

    def test_source_persistence_failure_prevents_model_call(self):
        with patch.object(self.store, 'save_reference_intent', side_effect=OSError('synthetic disk error')):
            with self.assertRaises(OSError): self.generate()
        self.transport.assert_not_called()
        self.assertEqual(self.calls.history(), [])

    def test_unknown_call_keeps_intent_and_never_resends(self):
        self.transport.side_effect = TimeoutError('synthetic unknown')
        with self.assertRaises(ModelError): self.generate()
        with self.assertRaisesRegex(ModelError, 'requires_attention'): self.generate()
        self.assertEqual(self.transport.call_count, 1)
        self.assertEqual(self.store.get(self.doc['id']), self.doc)
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM reference_intents').fetchone()[0], 1)
            with self.assertRaises(Exception): db.execute('DELETE FROM reference_intents')

    def test_invented_result_or_method_source_rejects_whole_batch(self):
        for change in ({'value':'9.9'}, {'quantity':'modulus'}, {'unit':'Pa'},
                       {'source_id':'absent'}, {'quote':'invented'}, {'method_class':'verified'},
                       {'method_quote':'computed with another code'}, {'method_source_id':'absent'},
                       {'eligible_for_scoring':True}):
            bad = deepcopy(self.output); bad['results'][0].update(change)
            with self.subTest(change=change), self.assertRaises(TaskError): validate_reference(self.sources, bad)
        self.output['results'][0]['value'] = 'invented'
        with self.assertRaises(TaskError): self.generate()
        self.assertEqual(self.store.get(self.doc['id']), self.doc)
        self.assertEqual(self.calls.history()[0]['receipt']['structured_output'], self.output)
        with self.assertRaises(TaskError): self.generate()
        self.assertEqual(self.transport.call_count, 1)

    def test_unclear_method_and_conflicting_result_values_are_retained(self):
        self.output['results'][0].update(method_class='unclear', method_source_id=None, method_quote=None)
        self.csv = export_csv(source_excerpt='At 250 K, density is 7.654321 g/cm3. density is 8.1 g/cm3.')
        sources, _ = reference_sources([self.csv]); sid = sources[0]['id']
        self.output['conditions'][0]['source_id'] = sid
        self.output['results'][0]['source_id'] = sid
        self.output['results'].append({**self.output['results'][0], 'value':'8.1', 'quote':'density is 8.1 g/cm3'})
        result = self.generate()
        batch = next(iter(result['reference_batches'].values()))
        self.assertEqual([x['value'] for x in batch['reported_results']], ['7.654321', '8.1'])
        self.assertTrue(all(not x['eligible_for_scoring'] for x in batch['reported_results']))

    def test_stale_task_and_research_task_fail_before_model_call(self):
        changed = self.store.add_candidate(self.doc['id'], self.doc['revision'], 'material', evidence('X'))
        with self.assertRaises(TaskError): self.generate()
        research = self.store.create('Research', 'No paper needed', 'research')
        with self.assertRaises(TaskError):
            generate_reference_draft(self.client, self.store, research['id'], research['revision'], [self.csv])
        self.transport.assert_not_called()
        self.assertEqual(self.calls.history(), [])

    def test_edit_during_call_rolls_back_then_recovers_as_unconfirmed_conflict(self):
        def transport(*args):
            changed = self.store.add_candidate(self.doc['id'], self.doc['revision'], 'temperature', evidence('300'))
            self.store.confirm(changed['id'], changed['revision'], ['temperature'])
            return 200, response(self.output)
        self.transport.side_effect = transport
        with self.assertRaises(TaskError): self.generate()
        latest = self.store.get(self.doc['id'])
        self.assertNotIn('reference_batches', latest)
        result = self.generate(revision=latest['revision'])
        self.assertEqual([c['value'] for c in result['fields']['temperature']['candidates']], ['300', '250'])
        self.assertFalse(result['fields']['temperature']['confirmed'])
        self.assertEqual(self.transport.call_count, 1)

    def test_reference_only_results_do_not_enter_execution_task_draft(self):
        doc = self.generate()
        for field in FIELDS:
            if field != 'temperature':
                doc = self.store.add_candidate(doc['id'], doc['revision'], field, evidence('synthetic input'))
        doc = self.store.confirm(doc['id'], doc['revision'], list(FIELDS))
        self.store.freeze(doc['id'], doc['revision'])
        packages = self.store.export_packages(doc['id'])
        self.assertIn(b'7.654321', packages['reference'])
        self.assertIn(b'reference_batches', packages['reference'])
        self.assertNotIn(b'7.654321', packages['execution'])
        self.assertNotIn(b'999999', packages['execution'])
        self.assertNotIn(b'method_quote', packages['execution'])

    def test_zero_budget_and_duplicate_or_unusable_exports_do_not_send(self):
        zero = ModelCalls(self.root / 'zero.sqlite', DeepSeekConfig('synthetic'), max_requests=0)
        self.client = DeepSeekClient(zero, transport=self.transport, key_reader=lambda: 'synthetic-key')
        with self.assertRaisesRegex(ModelError, 'budget_exhausted'): self.generate()
        self.transport.assert_not_called()
        for exports in ([self.csv, self.csv], [export_csv(source_excerpt='')],
                        [self.csv, export_csv(article_title='Another paper')],
                        [export_csv(source_locator='', source_page='')]):
            with self.subTest(exports=len(exports)), self.assertRaises(TaskError): reference_sources(exports)

    def test_source_text_match_never_claims_semantic_verification(self):
        bad = deepcopy(self.output)
        bad['results'][0]['method_class'] = 'other'  # Contradicts quote meaning, yet text exists.
        _, results, _, _ = validate_reference(self.sources, bad)
        self.assertEqual(results[0]['semantic_verification'], 'not_performed')
        self.assertFalse(results[0]['eligible_for_scoring'])


if __name__ == '__main__':
    unittest.main()
