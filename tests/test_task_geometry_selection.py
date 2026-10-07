"""Synthetic task/geometry metadata checks; no real resources or calculations."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from auto_lammps.atomic_structure_data import inspect_atomic_data
from auto_lammps.geometry_catalog import GeometryCatalogError
from auto_lammps.geometry_selection import validate_initial_geometry
from auto_lammps.manifest import canonical, sha256
from auto_lammps.task_packages import EXECUTION_FIELDS, split_condition_record
from auto_lammps.tasks import (FIELDS, GEOMETRY_CONDITION_FIELDS, FrozenTask,
                               StaleTask, TaskError, TaskStore)
from test_atomic_structure_data import DATA, OPTIONS
from test_literature import export_csv
from test_tasks import evidence, target_ready


def selection(data=DATA, catalog='a'*64):
    summary = inspect_atomic_data(data, **OPTIONS)
    summary['parser_sha256'] = 'b'*64
    entry = dict(size=len(data), sha256=sha256(data), summary=summary)
    entry['pin'] = sha256(canonical(entry))
    return dict(catalog_sha256=catalog, entry=entry)


class TaskGeometrySelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name).resolve()/'tasks.sqlite'
        self.store = TaskStore(self.path)
        self.selected = selection()

    def ready(self, mode='research'):
        doc = self.store.create('PRIVATE_TITLE_CANARY', 'PRIVATE_PROMPT_CANARY', mode)
        fields = [field for field in FIELDS if mode == 'reproduction' or field != 'reference']
        for field in fields:
            doc = self.store.add_candidate(doc['id'], doc['revision'], field,
                    evidence('PRIVATE_REFERENCE_CANARY' if field == 'reference' else 'input-'+field))
        return self.store.confirm(doc['id'], doc['revision'], fields)

    def choose(self, doc, selected=None):
        return self.store.select_initial_geometry(doc['id'], doc['revision'],
                                                 self.selected if selected is None else selected)

    def frozen(self):
        doc = self.choose(self.ready())
        return self.store.freeze(doc['id'], doc['revision'])

    def test_persistent_selection_is_metadata_only_and_unverified(self):
        doc = self.ready()
        original = deepcopy(doc['fields'])
        selected = deepcopy(self.selected)
        doc = self.choose(doc, selected)
        self.assertEqual(doc['initial_geometry'], self.selected)
        self.assertEqual(doc['fields'], original)
        self.assertEqual(set(doc['initial_geometry']), {'catalog_sha256', 'entry'})
        self.assertNotIn('path', doc['initial_geometry']['entry'])
        self.assertNotIn('positions', doc['initial_geometry']['entry']['summary'])
        self.assertIs(doc['initial_geometry']['entry']['summary']['scientifically_verified'], False)
        self.assertIs(doc['initial_geometry']['entry']['summary']['physical_evaluation_performed'], False)
        selected['entry']['summary']['type_elements'][0] = 'Fe'
        self.assertEqual(TaskStore(self.path).get(doc['id'])['initial_geometry'], self.selected)
        self.assertEqual(self.store.history(doc['id'])[-1]['event'], 'initial_geometry_selected')

    def test_selecting_geometry_does_not_fill_or_confirm_scientific_conditions(self):
        doc = self.store.create('Synthetic missing conditions', 'No conditions inferred.', 'research')
        original = deepcopy(doc['fields'])
        doc = self.choose(doc)
        self.assertEqual(doc['fields'], original)
        self.assertTrue(doc['issues'])
        with self.assertRaisesRegex(TaskError, '未确认'):
            self.store.freeze(doc['id'], doc['revision'])

    def test_changing_pin_appends_history_preserving_old_revision(self):
        doc = self.choose(self.ready())
        first_revision = doc['revision']
        second = selection(DATA.replace(b'2 1 2.2 3.1 6.8', b'2 1 2.1 3.1 6.8'), catalog='c'*64)
        doc = self.choose(doc, second)
        self.assertNotEqual(doc['initial_geometry']['entry']['pin'], self.selected['entry']['pin'])
        with self.store.transaction() as db:
            original = json.loads(db.execute('SELECT document FROM revisions WHERE task_id=? AND revision=?',
                                    (doc['id'], first_revision)).fetchone()[0])
        self.assertEqual(original['initial_geometry'], self.selected)
        self.assertEqual(doc['initial_geometry'], second)
        self.assertEqual(self.store.history(doc['id'])[-2]['event'], 'initial_geometry_selected')
        self.assertEqual(self.store.history(doc['id'])[-1]['event'], 'initial_geometry_selected')

    def test_stale_selection_does_not_change_task(self):
        doc = self.ready()
        self.choose(doc)
        before = self.store.get(doc['id'])
        history = self.store.history(doc['id'])
        with self.assertRaises(StaleTask):
            self.choose(doc)
        self.assertEqual(self.store.get(doc['id']), before)
        self.assertEqual(self.store.history(doc['id']), history)

    def test_invalid_metadata_is_rejected_atomically(self):
        doc = self.ready()
        mutations = (
            lambda value: value.update(path='/not/a/resource-selector'),
            lambda value: value.update(catalog_sha256='broken'),
            lambda value: value['entry'].update(pin='f'*64),
            lambda value: value['entry'].update(size=True),
            lambda value: value['entry'].update(path='source.data'),
            lambda value: value['entry']['summary'].update(positions=[[0, 0, 0]]),
            lambda value: value['entry']['summary'].update(author_answer='PRIVATE_RESULT_CANARY'),
            lambda value: value['entry']['summary'].update(scientifically_verified=True),
            lambda value: value['entry']['summary'].update(physical_evaluation_performed=True),
            lambda value: value['entry']['summary'].update(parser='/source/path'),
        )
        before, history = self.store.get(doc['id']), self.store.history(doc['id'])
        for mutate in mutations:
            value = deepcopy(self.selected)
            mutate(value)
            with self.subTest(mutation=mutate), self.assertRaises(TaskError):
                self.choose(doc, value)
            self.assertEqual(self.store.get(doc['id']), before)
            self.assertEqual(self.store.history(doc['id']), history)
        for value in (None, [], 'source.data', {'entry': self.selected['entry']}):
            with self.subTest(value=type(value)), self.assertRaises(TaskError):
                self.store.select_initial_geometry(doc['id'], doc['revision'], value)

    def test_geometry_validator_returns_detached_allowlisted_metadata(self):
        value = deepcopy(self.selected)
        clean = validate_initial_geometry(value)
        value['entry']['summary']['boundary'][0] = 'f'
        self.assertEqual(clean, self.selected)
        with self.assertRaises(GeometryCatalogError):
            validate_initial_geometry({**clean, 'caption': 'arbitrary context'})

    def test_freeze_binds_geometry_and_rejects_later_selection(self):
        doc = self.frozen()
        content = self.store.export(doc['id'])
        record = json.loads(content)
        self.assertEqual(record['initial_geometry'], self.selected)
        self.assertEqual(record['scientific_validation'], 'not_performed')
        self.assertIs(record['execution_authorized'], False)
        self.assertEqual(doc['record_sha256'], sha256(content))
        with self.assertRaises(FrozenTask):
            self.choose(doc, selection(catalog='c'*64))
        self.assertEqual(self.store.export(doc['id']), content)
        self.assertEqual(TaskStore(self.path).export(doc['id']), content)

    def test_packages_project_only_typed_geometry_and_selected_inputs(self):
        doc = self.frozen()
        record = json.loads(self.store.export(doc['id']))
        record.update(author_source='PRIVATE_SOURCE_CANARY', results={'P': 'PRIVATE_RESULT_CANARY'},
                      unselected_geometry={'path': '/forbidden', 'answer': 'PRIVATE_ANSWER_CANARY'})
        record['conditions']['structure']['candidates'][0]['source_locator'] = 'PRIVATE_LOCATOR_CANARY'
        pair = split_condition_record(canonical(record))
        execution = json.loads(pair['execution'])
        reference = json.loads(pair['reference'])
        self.assertEqual(execution['initial_geometry'], self.selected)
        self.assertNotIn(b'PRIVATE_', pair['execution'])
        self.assertEqual(reference['condition_review_record'], record)
        self.assertEqual(reference['execution_draft_sha256'], sha256(pair['execution']))
        for flag in ('execution_authorized', 'input_semantics_verified', 'resources_verified', 'runtime_isolation_verified'):
            self.assertIs(execution[flag], False)
        self.assertEqual(execution['release_status'], 'operator_review_required')

    def test_export_rejects_malformed_optional_geometry_instead_of_silently_dropping(self):
        doc = self.frozen()
        base = json.loads(self.store.export(doc['id']))
        for invalid in (None, [], '/raw/path', {**self.selected, 'raw_coordinates': [[0, 0, 0]]}):
            with self.subTest(invalid=type(invalid)), self.assertRaises(TaskError):
                split_condition_record(canonical({**base, 'initial_geometry': invalid}))

    def test_legacy_freeze_has_identical_contract_and_draft_bytes(self):
        doc = self.ready()
        expected = canonical(dict(schema_version=1, purpose='condition_review_record',
                    task_id=doc['id'], mode=doc['mode'], title=doc['title'], prompt=doc['prompt'],
                    conditions=doc['fields'], scientific_validation='not_performed', execution_authorized=False))
        doc = self.store.freeze(doc['id'], doc['revision'])
        self.assertEqual(self.store.export(doc['id']), expected)
        self.assertEqual(doc['record_sha256'], sha256(expected))
        pair = self.store.export_packages(doc['id'])
        inputs = {field: dict(value='input-'+field, unit='', applicability='required') for field in EXECUTION_FIELDS}
        task_text = '\n'.join(['请按以下已确认条件独立准备计算和分析方案。']+
                    [FIELDS[field]+'：input-'+field for field in EXECUTION_FIELDS])
        expected_draft = canonical(dict(schema_version=1, generator_version=1,
                    purpose='execution_task_draft', mode='research', release_status='operator_review_required',
                    execution_authorized=False, input_semantics_verified=False, resources_verified=False,
                    runtime_isolation_verified=False, conditions=inputs, task_text=task_text, resources=[]))
        self.assertEqual(pair['execution'], expected_draft)
        self.assertNotIn('initial_geometry', json.loads(pair['reference'])['condition_review_record'])
        history = self.store.history(doc['id'])
        self.assertEqual(TaskStore(self.path).export_packages(doc['id']), pair)
        self.assertEqual(self.store.history(doc['id']), history)

    def test_relevant_evidence_changes_invalidate_selection_with_history(self):
        for field in GEOMETRY_CONDITION_FIELDS:
            doc = self.choose(self.ready())
            selected_revision = doc['revision']
            doc = self.store.add_candidate(doc['id'], doc['revision'], field, evidence('changed-'+field))
            with self.subTest(field=field):
                self.assertNotIn('initial_geometry', doc)
                self.assertTrue(self.store.history(doc['id'])[-1]['event'].endswith(':initial_geometry_invalidated'))
                with self.store.transaction() as db:
                    before = json.loads(db.execute('SELECT document FROM revisions WHERE task_id=? AND revision=?',
                                        (doc['id'], selected_revision)).fetchone()[0])
                self.assertEqual(before['initial_geometry'], self.selected)

    def test_unrelated_conditions_and_confirmation_keep_geometry_selection(self):
        doc = self.choose(self.ready())
        doc = self.store.confirm(doc['id'], doc['revision'], ['structure', 'size', 'material'])
        self.assertEqual(doc['initial_geometry'], self.selected)
        selected_id = doc['fields']['structure']['selected']
        doc = self.store.select(doc['id'], doc['revision'], 'structure', selected_id, 'same input')
        self.assertEqual(doc['initial_geometry'], self.selected)
        doc = self.store.add_candidate(doc['id'], doc['revision'], 'temperature', evidence('400 K'))
        self.assertEqual(doc['initial_geometry'], self.selected)
        self.assertNotIn('initial_geometry_invalidated', self.store.history(doc['id'])[-1]['event'])

    def test_different_selected_condition_invalidates_geometry(self):
        doc = self.ready()
        original_id = doc['fields']['structure']['selected']
        doc = self.store.add_candidate(doc['id'], doc['revision'], 'structure', evidence('alternative structure'))
        alternative = doc['fields']['structure']['candidates'][-1]['id']
        doc = self.store.select(doc['id'], doc['revision'], 'structure', original_id, 'choose original')
        doc = self.choose(doc)
        doc = self.store.select(doc['id'], doc['revision'], 'structure', alternative, 'choose alternative')
        self.assertNotIn('initial_geometry', doc)
        self.assertEqual(self.store.history(doc['id'])[-1]['event'], 'condition_selected:structure:initial_geometry_invalidated')

    def test_literature_geometry_evidence_import_invalidates_selection(self):
        doc = self.choose(self.ready())
        content = export_csv(conditions='explicit synthetic geometry',
                             source_excerpt='explicit synthetic geometry')
        doc = self.store.import_literature(doc['id'], doc['revision'], content,
                    source_sha256=sha256(content.encode()), column='conditions', field='structure',
                    evidence_role='input', method_class='unclear',
                    classification_basis='Synthetic fixture only; no engine identification.')
        self.assertNotIn('initial_geometry', doc)
        self.assertEqual(self.store.history(doc['id'])[-1]['event'], 'literature_imported:structure:initial_geometry_invalidated')

    def test_generated_geometry_evidence_import_invalidates_selection(self):
        doc = self.choose(self.ready())
        sources = [dict(id='user-request', origin='user', locator='Synthetic input', text='Structure is bcc.')]
        value = dict(conditions=[dict(field='structure', value='bcc', unit='',
                                     source_id='user-request', quote='bcc')], questions=[])
        completion = dict(value=value, request_id='a'*32,
                          receipt=dict(state='completed', output_sha256=sha256(canonical(value))))
        doc = self.store.import_generated_conditions(doc['id'], doc['revision'], sources, completion)
        self.assertNotIn('initial_geometry', doc)
        self.assertEqual(self.store.history(doc['id'])[-1]['event'], 'conditions_generated:initial_geometry_invalidated')

    def test_first_and_changed_resource_selection_invalidate_target_selection(self):
        doc = target_ready(self.store, self.ready('reproduction'))
        self.assertIn('target_selection', doc)
        doc = self.choose(doc)
        self.assertNotIn('target_selection', doc)
        doc = self.store.select_targets(doc['id'], doc['revision'], ['fig-1-a'], '')
        old_target = deepcopy(doc['target_selection'])
        doc = self.choose(doc)
        self.assertEqual(doc['target_selection'], old_target)
        doc = self.choose(doc, selection(catalog='c'*64))
        self.assertNotIn('target_selection', doc)


if __name__ == '__main__':
    unittest.main()
