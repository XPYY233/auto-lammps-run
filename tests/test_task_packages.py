"""Synthetic reference canaries only; no paper answers or model execution."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from auto_lammps.manifest import canonical, sha256
from auto_lammps.task_packages import EXECUTION_FIELDS, split_condition_record
from auto_lammps.tasks import FIELDS, TaskError, TaskStore
from test_tasks import evidence


def frozen_task(store):
    doc = store.create('PRIVATE_TITLE_CANARY', 'PRIVATE_PROMPT_CANARY', 'reproduction')
    for key in FIELDS:
        doc = store.add_candidate(doc['id'], doc['revision'], key,
                                  evidence('PRIVATE_REFERENCE_CANARY' if key == 'reference' else 'input-'+key,
                                           origin='code' if key == 'boundary' else 'paper'))
    doc = store.confirm(doc['id'], doc['revision'], list(FIELDS))
    return store.freeze(doc['id'], doc['revision'])


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'tasks.sqlite'
        self.store = TaskStore(self.path)
        self.doc = frozen_task(self.store)
        self.record = json.loads(self.store.export(self.doc['id']))

    def test_reference_canaries_and_unselected_values_do_not_enter_execution_draft(self):
        record = deepcopy(self.record)
        field = record['conditions']['temperature']
        selected = field['candidates'][0]
        selected.update(source_locator='PRIVATE_SOURCE_CANARY',
                        literature_source={'value': 'PRIVATE_CSV_CANARY'}, extra='PRIVATE_EXTRA_CANARY')
        field['resolution'] = 'PRIVATE_RESOLUTION_CANARY'
        field['candidates'].append({**selected, 'id': 'not-selected', 'value': 'PRIVATE_ALTERNATIVE_CANARY'})
        record['literature_sources'] = {'private': {'target': 'PRIVATE_TARGET_CANARY'}}
        record['future_reference_field'] = 'PRIVATE_FUTURE_CANARY'
        pair = split_condition_record(canonical(record))
        execution = json.loads(pair['execution'])
        reference = json.loads(pair['reference'])
        self.assertNotIn(b'PRIVATE_', pair['execution'])
        self.assertEqual(set(execution['conditions']), set(EXECUTION_FIELDS))
        self.assertEqual(execution['conditions']['temperature']['value'], 'input-temperature')
        self.assertEqual(reference['condition_review_record'], record)
        self.assertEqual(reference['input_mapping']['temperature']['source_locator'], 'PRIVATE_SOURCE_CANARY')
        self.assertEqual(reference['condition_origin'], 'code_supplemented')

    def test_paired_digests_and_restart_reproduce_same_exports_without_mutation(self):
        history = self.store.history(self.doc['id'])
        pair = self.store.export_packages(self.doc['id'])
        reference = json.loads(pair['reference'])
        self.assertEqual(reference['condition_record_sha256'], self.doc['record_sha256'])
        self.assertEqual(reference['execution_draft_sha256'], sha256(pair['execution']))
        draft = json.loads(pair['execution'])
        for key, item in draft['conditions'].items():
            self.assertEqual(reference['input_mapping'][key]['input_sha256'], sha256(canonical(item)))
        self.assertEqual(TaskStore(self.path).export_packages(self.doc['id']), pair)
        self.assertEqual(self.store.history(self.doc['id']), history)

    def test_embedded_answer_is_not_claimed_removed_or_released(self):
        record = deepcopy(self.record)
        record['conditions']['quantity']['candidates'][0]['value'] = 'embedded-answer-canary'
        pair = split_condition_record(canonical(record))
        draft = json.loads(pair['execution'])
        # Semantic screening is not implemented. Never claim this projection is
        # a released blind package just because provenance keys were removed.
        self.assertIn('embedded-answer-canary', draft['task_text'])
        self.assertEqual(draft['release_status'], 'operator_review_required')
        for key in ('execution_authorized', 'input_semantics_verified', 'resources_verified', 'runtime_isolation_verified'):
            self.assertIs(draft[key], False)
        self.assertEqual(draft['resources'], [])
        reference = json.loads(pair['reference'])
        self.assertEqual(reference['results'], {'P': None, 'A': None, 'B': None})
        self.assertEqual(reference['qualification_status'], 'not_evaluated')

    def test_text_keeps_units_multiline_inputs_and_explicit_non_applicability(self):
        record = deepcopy(self.record)
        selected = record['conditions']['temperature']['candidates'][0]
        selected.update(value='300', unit='K')
        record['conditions']['ensemble']['candidates'][0].update(value='静态任务', applicability='not_applicable')
        record['conditions']['stages']['candidates'][0]['value'] = '阶段一\n阶段二'
        draft = json.loads(split_condition_record(canonical(record))['execution'])
        self.assertIn('温度：300（单位：K）', draft['task_text'])
        self.assertIn('系综：不适用；理由：静态任务', draft['task_text'])
        self.assertIn('阶段一\n阶段二', draft['task_text'])
        self.assertNotIn('论文标识', draft['task_text'])

    def test_incomplete_or_invalid_selection_never_exports(self):
        changes = [lambda f: f.update(confirmed=False), lambda f: f.update(selected=None),
                   lambda f: f.update(selected='missing'), lambda f: f['candidates'].append(deepcopy(f['candidates'][0])),
                   lambda f: f['candidates'][0].update(evidence_role='result'),
                   lambda f: f['candidates'][0].update(source_locator=''),
                   lambda f: f['candidates'][0].update(applicability='not_applicable')]
        for change in changes:
            with self.subTest(change=change):
                record = deepcopy(self.record)
                change(record['conditions']['potential'])
                with self.assertRaises(TaskError): split_condition_record(canonical(record))
        draft = self.store.create('unfinished', 'unconfirmed', 'research')
        with self.assertRaises(TaskError): self.store.export_packages(draft['id'])

    def test_wrong_schema_and_missing_fields_are_rejected(self):
        for record in (None, [], {}, {**self.record, 'purpose': 'execution_task_draft'},
                       {**self.record, 'execution_authorized': True},
                       {**self.record, 'conditions': {}}):
            with self.subTest(record=type(record)):
                with self.assertRaises(TaskError): split_condition_record(canonical(record))
        with self.assertRaises(TaskError): split_condition_record(b'broken json')

    def test_excluding_paper_identifier_does_not_remove_need_for_its_confirmation(self):
        record = deepcopy(self.record)
        record['conditions']['reference']['confirmed'] = False
        with self.assertRaises(TaskError): split_condition_record(canonical(record))
