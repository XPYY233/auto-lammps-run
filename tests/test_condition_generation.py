"""Synthetic NLP outputs traverse the real service and persistent task store."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from auto_lammps.condition_generation import generate_condition_draft, validate_conditions
from auto_lammps.deepseek import DeepSeekClient, DeepSeekConfig, ModelCalls, ModelError
from auto_lammps.tasks import FIELDS, TaskError, TaskStore
from test_deepseek import response
from test_tasks import evidence

SOURCES = [dict(id='user-request', origin='user', locator='用户原始任务描述', text='希望研究铜在 300 K 下的性质。')]
OUTPUT = dict(conditions=[dict(field='temperature', value='300', unit='K', source_id='user-request', quote='300 K')],
              questions=[dict(field='quantity', question='需要计算哪项物理量？')])


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TaskStore(Path(self.tmp.name)/'tasks.sqlite')
        self.doc = self.store.create('合成自然语言测试', SOURCES[0]['text'], 'research')
        self.calls = ModelCalls(Path(self.tmp.name)/'models.sqlite', DeepSeekConfig('synthetic-model'), max_requests=2)
        self.transport = Mock(return_value=(200, response(OUTPUT)))
        self.client = DeepSeekClient(self.calls, transport=self.transport, key_reader=lambda:'synthetic-key')

    def generate(self, request_id='a'*32):
        return generate_condition_draft(self.client, self.store, self.doc['id'], self.doc['revision'], SOURCES, request_id)

    def test_free_prose_becomes_unconfirmed_sourced_conditions_and_questions(self):
        doc = self.generate()
        field = doc['fields']['temperature']
        self.assertEqual(field['candidates'][0]['value'], '300')
        self.assertFalse(field['confirmed'])
        self.assertEqual(field['candidates'][0]['generated_evidence']['quote'], '300 K')
        self.assertEqual(doc['generated_batches']['a'*32]['questions'], OUTPUT['questions'])
        self.assertEqual(self.store.history(doc['id'])[-1]['event'], 'conditions_generated')
        self.assertEqual(len(self.calls.history()), 1)
        self.assertNotIn('reference', [item['field'] for item in doc['issues']])
        self.assertEqual(TaskStore(self.store.path).get(doc['id']), doc)

    def test_invented_source_quote_unit_or_extra_authority_is_rejected(self):
        for changes in ({'source_id':'not-provided'}, {'quote':'400 K'}, {'value':'400'},
                        {'unit':'bar'}, {'field':'execute'}, {'confirmed':True}):
            result = deepcopy(OUTPUT)
            result['conditions'][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(TaskError):
                validate_conditions(SOURCES, result)

    def test_invalid_generation_changes_no_conditions_but_retains_model_output(self):
        bad = deepcopy(OUTPUT)
        bad['conditions'].append({**bad['conditions'][0], 'quote':'invented quote'})
        self.transport.return_value = (200, response(bad))
        with self.assertRaises(TaskError): self.generate()
        self.assertEqual(self.store.get(self.doc['id']), self.doc)
        self.assertEqual(self.calls.history()[0]['receipt']['structured_output'], bad)
        self.assertEqual(self.transport.call_count, 1)

    def test_generated_conflict_preserves_manual_evidence_and_revokes_confirmation(self):
        self.doc = self.store.add_candidate(self.doc['id'], self.doc['revision'], 'temperature', evidence('400'))
        self.doc = self.store.confirm(self.doc['id'], self.doc['revision'], ['temperature'])
        doc = self.generate()
        field = doc['fields']['temperature']
        self.assertIsNone(field['selected'])
        self.assertFalse(field['confirmed'])
        self.assertEqual([item['value'] for item in field['candidates']], ['400','300'])

    def test_update_during_model_call_rejects_stale_import_without_losing_new_edit(self):
        def transport(*args):
            self.store.add_candidate(self.doc['id'], self.doc['revision'], 'material', evidence('Cu'))
            return 200, response(OUTPUT)
        self.client.transport = transport
        with self.assertRaises(TaskError): self.generate()
        latest = self.store.get(self.doc['id'])
        self.assertEqual(latest['fields']['material']['candidates'][0]['value'], 'Cu')
        self.assertEqual(latest['fields']['temperature']['candidates'], [])
        self.assertEqual(len(self.calls.history()), 1)

    def test_stale_request_is_rejected_before_spending(self):
        self.store.add_candidate(self.doc['id'], self.doc['revision'], 'material', evidence('Cu'))
        with self.assertRaises(TaskError): self.generate()
        self.transport.assert_not_called()
        self.assertEqual(self.calls.history(), [])

    def test_research_freezes_and_exports_without_any_paper(self):
        for field in FIELDS:
            if field != 'reference':
                self.doc = self.store.add_candidate(self.doc['id'], self.doc['revision'], field, evidence('synthetic input'))
        self.doc = self.store.confirm(self.doc['id'], self.doc['revision'], [f for f in FIELDS if f != 'reference'])
        self.doc = self.store.freeze(self.doc['id'], self.doc['revision'])
        self.assertEqual(self.doc['fields']['reference']['candidates'], [])
        self.assertIn('execution', self.store.export_packages(self.doc['id']))
        with self.assertRaises(TaskError): self.generate()
        self.transport.assert_not_called()

    def test_quote_match_does_not_claim_scientific_or_answer_classification(self):
        sources = [{**SOURCES[0], 'text':'计算得到的结果是 300 K。'}]
        choices, _, _ = validate_conditions(sources, OUTPUT)
        self.assertEqual(choices[0]['candidate']['generated_evidence']['semantic_verification'], 'not_performed')
        # The quote exists but is a result, not an input. This check is not a
        # semantic verifier and must not auto-confirm or release Agent tasks.

    def test_repeated_evidence_does_not_duplicate_or_clear_existing_confirmation(self):
        self.doc = self.generate()
        self.doc = self.store.confirm(self.doc['id'], self.doc['revision'], ['temperature'])
        self.doc = self.generate('b'*32)
        self.assertEqual(len(self.doc['fields']['temperature']['candidates']), 1)
        self.assertTrue(self.doc['fields']['temperature']['confirmed'])
        self.assertGreater(self.doc['generated_batches']['b'*32]['revision'], self.doc['generated_batches']['a'*32]['revision'])

    def test_research_does_not_accept_model_request_for_paper(self):
        output = deepcopy(OUTPUT)
        output['questions'].append(dict(field='reference',question='请提供论文'))
        self.transport.return_value = (200,response(output))
        with self.assertRaisesRegex(TaskError, '不要求论文'): self.generate()
        self.assertEqual(self.store.get(self.doc['id']), self.doc)
