"""Bounded reference validation with synthetic states; no real API or physics."""
from copy import deepcopy
import sqlite3
import unittest
from unittest.mock import Mock

from auto_lammps.author_reference_plan import _revision_sources, plan_selection_context, validate_author_plan
from auto_lammps.manifest import canonical, sha256
from auto_lammps.tasks import StaleTask, TaskError
import test_reference_scope as scope_fixture
import test_author_reference_plan as plan_fixture
import test_author_reference_workspace as workspace_fixture


VALIDATION = 'a' * 32


class AuthorValidationScopeTests(unittest.TestCase):
    def setUp(self):
        self.fx = scope_fixture.ReferenceScopeTests(); self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.scope = self.fx.create(); self.service = self.fx.service
        self.task = self.fx.task['id']; self.note = 'Approve a two-state author development validation only.'

    def preview(self, count=2, **kwargs):
        return self.service.preview_author_validation(self.task, self.scope['id'], count, self.note, **kwargs)

    def create(self, identifier=VALIDATION, count=2, **kwargs):
        preview = self.preview(count, **kwargs)
        return self.service.create_author_validation(self.task, self.fx.tasks.get(self.task)['revision'],
            identifier, self.scope['id'], count, self.note,
            expected_validation_sha256=preview['validation_sha256'], **kwargs)

    def test_overlay_does_not_replace_original_scope_B_or_old_records(self):
        before = self.fx.snapshot_old_records(); original = self.service.status(self.task)
        result = self.create()
        self.assertEqual(self.service.status(self.task), original)
        self.assertEqual(self.fx.snapshot_old_records(), before)
        self.assertEqual(result['contract']['unit_count'], 2)
        self.assertEqual(result['contract']['original_scope_receipt_sha256'], self.scope['receipt_sha256'])
        for key in ('B_input', 'execution_authorized', 'changes_B_scope', 'resets_plan_rounds', 'full_paper_success'):
            self.assertFalse(result['contract'][key])
        self.assertEqual(self.fx.calls.status()['used_requests'], 0)
        self.fx.transport.assert_not_called(); self.fx.key.assert_not_called()

    def test_count_and_explicit_intent_are_required(self):
        for count in (True, 0, -1, 129, 1.5, '2'):
            with self.subTest(count=count), self.assertRaises(TaskError):
                self.preview(count)
        with self.assertRaises(TaskError):
            self.service.preview_author_validation(self.task, self.scope['id'], 2)

    def test_guidance_is_loaded_from_this_task_and_cannot_be_replaced(self):
        guidance = self.fx.tasks.add_guidance(self.task, self.fx.task['revision'], self.note)[0]
        preview = self.service.preview_author_validation(self.task, self.scope['id'], 2, guidance_sequence=1)
        self.assertEqual(preview['contract']['user_intent'], dict(note=self.note, guidance_sequence=1, guidance_at=guidance['at']))
        with self.assertRaises(TaskError):
            self.service.preview_author_validation(self.task, self.scope['id'], 2, 'Altered intent', guidance_sequence=1)
        with self.assertRaises(TaskError):
            self.service.preview_author_validation(self.task, self.scope['id'], 2, guidance_sequence=2)

    def test_history_is_append_only_and_same_id_is_idempotent(self):
        first = self.create(); self.assertEqual(self.create(), first)
        second = self.create('b' * 32)
        self.assertEqual(second['previous_validation_id'], first['id'])
        self.assertEqual(second['previous_receipt_sha256'], first['receipt_sha256'])
        self.assertEqual(len(self.service.author_validation_status(self.task)['history']), 2)
        with self.assertRaises(TaskError):
            self.create(count=3)
        with self.fx.tasks.transaction() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('DELETE FROM author_validation_subsets WHERE id=?', (VALIDATION,))

    def test_preview_digest_and_current_task_revision_are_required(self):
        for digest in (None, 'f' * 64):
            with self.subTest(digest=digest), self.assertRaises(TaskError):
                self.service.create_author_validation(self.task, self.fx.task['revision'], VALIDATION,
                    self.scope['id'], 2, self.note, expected_validation_sha256=digest)
        with self.assertRaises(StaleTask):
            self.service.create_author_validation(self.task, self.fx.task['revision'] + 1, VALIDATION,
                self.scope['id'], 2, self.note, expected_validation_sha256=self.preview()['validation_sha256'])
        self.assertIsNone(self.service.get_author_validation(self.task))

    def test_new_parent_scope_invalidates_overlay_without_falling_back(self):
        saved = self.create(); old = deepcopy(saved['contract'])
        self.fx.create(['second'], scope_id=scope_fixture.SECOND)
        current = self.service.get_author_validation(self.task)
        self.assertEqual(current['state'], 'source_unavailable')
        self.assertFalse(current['can_prepare_A_plan']); self.assertEqual(current['contract'], old)
        with self.assertRaises(TaskError): self.preview()

    def test_reference_charge_and_failed_attempt_history_are_retained(self):
        row = self.fx.bind_run(dispatch=True); before = self.fx.ledger.get(row['id'])
        saved = self.create()
        prior = saved['contract']['prior_execution']['evaluations'][0]['requests'][0]
        self.assertEqual(prior['id'], row['id']); self.assertEqual(prior['state'], 'unknown')
        self.assertEqual(self.fx.ledger.get(row['id']), before)

    def test_cross_task_lookup_is_rejected(self):
        self.create(); other = self.fx.tasks.create('Other validation', 'Other intent', 'reproduction')
        paper = self.fx.papers.get(self.fx.paper['id'])
        self.fx.papers.link_task(paper['id'], paper['revision'], other['id'])
        with self.assertRaises(TaskError): self.service.get_author_validation(other['id'], VALIDATION)


class AuthorValidationPlanTests(unittest.TestCase):
    def setUp(self):
        self.fx = plan_fixture.AuthorReferencePlanTests(); self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        f = self.fx
        f.reader.texts['README.md'] += b'Synthetic state identifiers: 7 and 9. Explicit original state argument.\n'
        f.reader._write_sources(); f.reader._bind_receipt()
        request = f.reader.request([dict(path='README.md', start_line=1, end_line=20),
                                   dict(path='workflow/example.txt', start_line=1, end_line=20)])
        f.input['author_source'] = dict(capability=f.reader.service().capability(f.reader.receipt, purpose='author_reference'),
            acquisition_receipt=deepcopy(f.reader.receipt), reads=[dict(request=request, response=f.reader.worker(request))])
        self.original_evidence = f.service._input(f.task['id'])
        preview = f.scopes.preview_author_validation(f.task['id'], f.scope['id'], 2, 'Explicit two-state validation.')
        self.record = f.scopes.create_author_validation(f.task['id'], f.task['revision'], VALIDATION,
            f.scope['id'], 2, 'Explicit two-state validation.', expected_validation_sha256=preview['validation_sha256'])
        f.input['author_validation_id'] = VALIDATION
        self.evidence = f.service._input(f.task['id'])
        source_id = self.evidence['invocation_source_ids'][0]
        for row in f.value['invocations'][0]['citations'] + f.value['invocations'][0]['output_files'][0]['citations']:
            row['source_id'] = source_id
        template = deepcopy(f.value['invocations'][0]); calls = ['state-7', 'state-9']
        f.value['invocations'] = [dict(deepcopy(template), id=call,
            argv=template['argv'] + ['-var', 'synthetic_state', state]) for call, state in zip(calls, ('7', '9'))]
        f.value['coverage'] = [row for row in f.value['coverage'] if row['dependency_id'] in {'shared', 'post-a'}]
        f.value['coverage'][0]['invocation_ids'] = calls
        cite = [dict(source_id=source_id, quote='Synthetic state identifiers: 7 and 9. Explicit original state argument.')]
        f.value['validation_selection'] = dict(target_ids=['first'], states=[dict(id=state, label='Synthetic state ' + state,
            citations=deepcopy(cite), invocation_ids=[call], argument_bindings=[dict(invocation_id=call, argv_index=4)])
            for call, state in zip(calls, ('7', '9'))])

    def check(self, value=None):
        return validate_author_plan(self.evidence, self.fx.value if value is None else value)

    def test_AI_selects_subset_while_original_P_scope_is_unchanged(self):
        original = self.fx.scopes.get(self.fx.task['id'])
        result = self.fx.prepare()
        self.assertEqual(result['state'], 'prepared', result)
        report = result['report']; validation = report['author_validation']
        self.assertEqual(validation['selected_ids'], ['first'])
        self.assertEqual(validation['dependency_ids'], ['shared', 'post-a'])
        self.assertEqual([row['id'] for row in validation['states']], ['7', '9'])
        self.assertEqual(report['reference_scope']['scope']['selected_ids'], ['first', 'second'])
        self.assertEqual(self.fx.scopes.get(self.fx.task['id']), original)
        payload = self.fx.sent[-1]['messages'][-1]['content']
        self.assertIn('validation_selection', payload); self.assertNotIn('B_PRIVATE_CONDITION_CANARY', payload)

    def test_prepared_count_is_exact_and_partial_selection_is_only_clarification(self):
        value = deepcopy(self.fx.value); value['validation_selection']['states'].pop()
        value['invocations'].pop(); value['coverage'][0]['invocation_ids'].pop()
        with self.assertRaises(TaskError): self.check(value)
        value['status'] = 'needs_clarification'; value['questions'] = ['Remaining state not supported.']
        self.assertEqual(len(self.check(value)['author_validation']['states']), 1)
        value['validation_selection']['states'] *= 3
        with self.assertRaises(TaskError): self.check(value)

    def test_state_cannot_repeat_or_be_invented_without_author_quote(self):
        for change in ('repeat', 'invent'):
            value = deepcopy(self.fx.value)
            value['validation_selection']['states'][1]['id'] = '7' if change == 'repeat' else 'unsupported-42'
            with self.subTest(change=change), self.assertRaises(TaskError): self.check(value)

    def test_no_bulk_driver_can_be_counted_as_multiple_states(self):
        value = deepcopy(self.fx.value)
        value['validation_selection']['states'][1]['invocation_ids'] = ['state-7']
        value['validation_selection']['states'][1]['argument_bindings'][0]['invocation_id'] = 'state-7'
        with self.assertRaises(TaskError): self.check(value)

    def test_report_count_cannot_hide_full_driver_or_unbound_invocation(self):
        for change in ('wrong_value', 'wrong_index', 'missing_binding', 'unbound_call'):
            value = deepcopy(self.fx.value)
            if change == 'wrong_value': value['invocations'][1]['argv'][-1] = '50'
            if change == 'wrong_index': value['validation_selection']['states'][1]['argument_bindings'][0]['argv_index'] = 0
            if change == 'missing_binding': value['validation_selection']['states'][1]['argument_bindings'] = []
            if change == 'unbound_call':
                extra = dict(deepcopy(value['invocations'][0]), id='full-driver', argv=['-in', 'workflow/example.txt'])
                value['invocations'].append(extra); value['coverage'][0]['invocation_ids'].append('full-driver')
            with self.subTest(change=change), self.assertRaises(TaskError): self.check(value)

    def test_target_selection_and_dependency_coverage_are_exact(self):
        for change in ('foreign_target', 'old_full_coverage', 'missing_dependency'):
            value = deepcopy(self.fx.value)
            if change == 'foreign_target': value['validation_selection']['target_ids'] = ['unprovided']
            if change == 'old_full_coverage':
                value['coverage'].append(dict(deepcopy(value['coverage'][1]), dependency_id='post-b'))
            if change == 'missing_dependency': value['coverage'].pop()
            with self.subTest(change=change), self.assertRaises(TaskError): self.check(value)

    def test_claimed_reuse_has_no_unverified_shortcut(self):
        value = deepcopy(self.fx.value)
        value['validation_selection']['states'][0]['reuse_job'] = 'invented'
        with self.assertRaises(TaskError): self.check(value)
        value = deepcopy(self.fx.value); value['invocations'] = []
        for row in value['coverage']: row['invocation_ids'] = []; row['missing'] = ['Original output reuse unverified.']
        for state in value['validation_selection']['states']: state['invocation_ids'] = []; state['argument_bindings'] = []
        with self.assertRaises(TaskError): self.check(value)
        value['status'] = 'needs_clarification'
        self.assertFalse(self.check(value)['author_validation']['reused_outputs_verified'])

    def test_overlay_is_explicit_in_mandatory_menu_and_cannot_be_silently_omitted(self):
        menu = plan_selection_context(self.evidence)
        self.assertEqual(menu['author_validation'], self.evidence['author_validation'])
        self.assertIn('validation_selection', menu['output_types']['required'])
        self.fx.input.pop('author_validation_id')
        with self.assertRaises(TaskError): self.fx.service._input(self.fx.task['id'])

    def test_empty_or_corrupted_mandatory_overlay_is_not_a_disable_switch(self):
        for invalid in ({}, None, [], dict(self.evidence['author_validation'], validation_sha256='f' * 64)):
            evidence = dict(self.evidence, author_validation=invalid)
            with self.subTest(invalid=invalid), self.assertRaises(TaskError): plan_selection_context(evidence)

    def test_explicit_overlay_is_allowed_once_and_never_replaced_during_revision(self):
        updates = _revision_sources(self.original_evidence, self.evidence)
        self.assertTrue(updates['original_scope_unchanged']); self.assertTrue(updates['bounded_validation_explicitly_added'])
        removed = deepcopy(self.evidence); removed.pop('author_validation')
        with self.assertRaises(TaskError): _revision_sources(self.evidence, removed)
        changed = deepcopy(self.evidence); changed['author_validation']['contract']['unit_count'] = 3
        with self.assertRaises(TaskError): _revision_sources(self.evidence, changed)

    def test_same_persistent_three_round_limit_and_full_previous_answer(self):
        first = self.fx.prepare(); second = self.fx.prepare('5' * 32, revision_note='Refine synthetic wording.')
        third = self.fx.prepare('6' * 32, revision_note='Final synthetic wording.')
        self.assertEqual([first['round'], second['round'], third['round']], [1, 2, 3])
        self.assertEqual(second['revision']['complete_previous_answer'], self.fx.value)
        with self.assertRaises(TaskError): self.fx.prepare('7' * 32, revision_note='Fourth forbidden.')
        self.assertEqual(self.fx.calls.status()['used_requests'], 3)

    def test_two_old_full_scope_rounds_then_explicit_subset_uses_only_last_round(self):
        f = plan_fixture.AuthorReferencePlanTests(); f.setUp(); self.addCleanup(f.doCleanups)
        f.reader.texts['README.md'] += b'Synthetic state identifiers: 7 and 9. Explicit original state argument.\n'
        f.reader._write_sources(); f.reader._bind_receipt()
        request = f.reader.request([dict(path='README.md', start_line=1, end_line=20),
                                   dict(path='workflow/example.txt', start_line=1, end_line=20)])
        f.input['author_source'] = dict(capability=f.reader.service().capability(f.reader.receipt, purpose='author_reference'),
            acquisition_receipt=deepcopy(f.reader.receipt), reads=[dict(request=request, response=f.reader.worker(request))])
        source_id = f.service._input(f.task['id'])['invocation_source_ids'][0]
        for row in f.value['invocations'][0]['citations'] + f.value['invocations'][0]['output_files'][0]['citations']:
            row['source_id'] = source_id
        self.assertEqual(f.prepare()['round'], 1)
        previous = deepcopy(f.value)
        self.assertEqual(f.prepare('5' * 32, revision_note='Second full-scope synthetic version.')['round'], 2)
        preview = f.scopes.preview_author_validation(f.task['id'], f.scope['id'], 2, 'Explicit bounded validation.')
        f.scopes.create_author_validation(f.task['id'], f.task['revision'], VALIDATION, f.scope['id'], 2,
            'Explicit bounded validation.', expected_validation_sha256=preview['validation_sha256'])
        f.input['author_validation_id'] = VALIDATION; f.value = deepcopy(self.fx.value)
        third = f.prepare('6' * 32, revision_note='Apply the explicit bounded validation.')
        self.assertEqual(third['state'], 'prepared', third)
        self.assertEqual(third['round'], 3)
        self.assertEqual(third['revision']['complete_previous_answer'], previous)
        self.assertTrue(third['revision']['source_updates']['bounded_validation_explicitly_added'])
        self.assertEqual(f.service.status(f.task['id'])['remaining_rounds'], 0)


class AuthorValidationWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.fx = workspace_fixture.AuthorWorkspaceTests(); self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        f = self.fx.fx; self.scopes = f.scopes; self.task = f.task['id']; self.workspace = self.fx.workspace
        preview = self.scopes.preview_author_validation(self.task, f.scope['id'], 2, 'Explicit bounded validation.')
        self.record = self.scopes.create_author_validation(self.task, f.task['revision'], VALIDATION,
            f.scope['id'], 2, 'Explicit bounded validation.', expected_validation_sha256=preview['validation_sha256'])

    def test_status_exposes_saved_overlay_without_remote_read_or_model(self):
        status = self.workspace.status(self.task)
        self.assertEqual(status['author_validation']['latest']['id'], VALIDATION)
        self.assertEqual(self.fx.fx.reader.calls, 0); self.assertEqual(self.fx.fx.sent, [])
        self.assertEqual(status['used_rounds'], 0)

    def test_prepare_uses_explicit_overlay_without_replacing_parent_scope(self):
        self.workspace.planner.prepare = Mock(side_effect=lambda *args, **kwargs:
            deepcopy(self.workspace.current.get()['input']))
        value = self.workspace.prepare(self.task, 'c' * 32)
        self.assertEqual(value['author_validation_id'], VALIDATION)
        self.assertEqual(value['scope_id'], self.fx.fx.scope['id'])
        self.assertEqual(self.fx.fx.calls.status()['used_requests'], 0)

    def test_invalidated_overlay_disables_preparation_and_never_falls_back(self):
        self.fx.fx.fixture.create(['second'], scope_id=scope_fixture.SECOND)
        status = self.workspace.status(self.task)
        self.assertFalse(status['can_prepare'])
        self.assertEqual(status['author_validation']['latest']['state'], 'source_unavailable')
        with self.assertRaises(TaskError): self.workspace.prepare(self.task, 'c' * 32)
        self.assertEqual(self.fx.fx.calls.status()['used_requests'], 0)


if __name__ == '__main__':
    unittest.main()
