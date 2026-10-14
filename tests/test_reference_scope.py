"""Synthetic persisted P scopes; no API request, engine or actual research data."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

from auto_lammps.deepseek import DeepSeekClient, DeepSeekConfig, ModelCalls, request_body
from auto_lammps.ledger import Ledger, Policy, Resources
from auto_lammps.manifest import canonical, sha256
from auto_lammps.papers import PaperStore
from auto_lammps.reference_scope import ReferenceScopeService, ReferenceScopeUnavailable
from auto_lammps.reference_targets import (LEGACY_ADAPTER_SHA256, LEGACY_CONTROLLER_SHA256,
    LEGACY_SEMANTICS_SHA256, NORMALIZATION_RULES, ReferenceTargetAnalysis,
    model_ledger_identity, validate_target_analysis)
from auto_lammps.tasks import StaleTask, TaskError, TaskStore
from test_tasks import target_inventory


H1, H2, H3 = 'a' * 64, 'b' * 64, 'c' * 64
REQUEST, SCOPE, SECOND = '1' * 32, '2' * 32, '3' * 32


class ReferenceScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.tasks = TaskStore(self.root / 'tasks.sqlite')
        self.task = self.tasks.create('Synthetic reference scope', 'B_PRIVATE_CONDITION_CANARY', 'reproduction')
        self.papers = PaperStore(self.tasks)
        paper = self.papers.add('Synthetic target article', '10.1234/scopes', 'Synthetic original scope', 'PRIVATE_AUTHOR_CODE_CANARY')
        paper = self.papers.select(paper['id'], paper['revision'])
        self.paper = self.papers.link_task(paper['id'], paper['revision'], self.task['id'])
        inv = target_inventory(); inv['paper'] = {key: self.paper[key] for key in ('title', 'doi')}
        original = inv['targets'][0]
        inv['targets'] = [{**original, 'id': key, 'label': 'Synthetic ' + key}
                          for key in ('first', 'second', 'uncertain')]
        self.input = dict(task_id=self.task['id'], paper_id=self.paper['id'], paper=deepcopy(inv['paper']),
            role='paper_reference_human_only', inventory=inv,
            sources=[dict(id='p-text', origin='paper', locator='Synthetic page 1', text='Synthetic retained source quote.')],
            source_binding=dict(inventory_source_sha256=H1, source_receipt_sha256=H1,
                                pdf_sha256=H2, package_manifest_sha256=H3))
        cite = [dict(source_id='p-text', quote='Synthetic retained source quote.')]
        self.value = dict(targets=[dict(target_id=key, data_class='postprocessing' if key != 'uncertain' else 'unclear',
            priority=2, reason='Synthetic proposed classification', citations=deepcopy(cite),
            dependency_ids=['post-a' if key == 'first' else 'post-b' if key == 'second' else 'unknown'],
            missing=['Synthetic method missing'] if key == 'uncertain' else [])
            for key in ('first', 'second', 'uncertain')], dependencies=[
            dict(id='post-a', data_class='postprocessing', label='Synthetic first transform', description='Synthetic postprocess',
                 requires=['shared'], citations=deepcopy(cite), missing=[]),
            dict(id='post-b', data_class='postprocessing', label='Synthetic second transform', description='Synthetic postprocess',
                 requires=['shared'], citations=deepcopy(cite), missing=[]),
            dict(id='shared', data_class='raw_lammps', label='Synthetic shared source', description='Synthetic raw source',
                 requires=[], citations=deepcopy(cite), missing=[]),
            dict(id='unknown', data_class='unclear', label='Synthetic unknown source', description='Synthetic missing method',
                 requires=[], citations=[], missing=['Synthetic method missing'])], limitations=['Synthetic only; not science.'])
        self.calls = ModelCalls(self.root / 'models.sqlite', DeepSeekConfig('synthetic-scope-model'), max_requests=10)
        self.transport, self.key = Mock(side_effect=AssertionError('must not send API')), Mock(side_effect=AssertionError('must not read key'))
        self.client = DeepSeekClient(self.calls, transport=self.transport, key_reader=self.key)
        self.connection = dict(enabled=True, model=self.client.model, connection_revision=1,
            credential_generation=1, model_ledger_sha256=model_ledger_identity(self.client))
        self.targets = ReferenceTargetAnalysis(self.tasks, self.papers, lambda _: deepcopy(self.input),
            connection_provider=lambda _: deepcopy(self.connection))
        self.service = ReferenceScopeService(self.tasks, self.papers, self.targets)
        self.context = self.targets._context(self.task['id'], self.client, 1, 1)
        self.report = validate_target_analysis(self.context['input'], self.value)
        self.model_receipt = dict(provider='deepseek', requested_model=self.client.model, state='completed',
            request_sha256=self.context['request_sha256'], response_sha256=H3,
            output_sha256=sha256(canonical(self.value)), http_status=200, finish_reason='stop',
            transport_attempts=1, usage={'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5})
        self.save_target()

    def save_target(self, request=REQUEST, *, context=None, report=None, state='completed', model=None):
        context = deepcopy(context or self.context)
        with self.tasks.transaction() as db:
            db.execute('INSERT OR IGNORE INTO reference_target_scopes VALUES (?,?,?)', (self.task['id'], 3, 'synthetic'))
            db.execute('INSERT INTO reference_target_intents VALUES (?,?,?,?,?)',
                       (request, self.task['id'], canonical(context).decode(), sha256(canonical(context)), 'synthetic'))
            self.targets._event(db, request, 'dispatching')
        return self.targets._finish(self.task['id'], request, state, report=self.report if report is None else report,
                                    model_receipt=self.model_receipt if model is None else model)

    def preview(self, selected=None, **kwargs):
        return self.service.preview(self.task['id'], REQUEST, selected or ['first', 'second'], 'Synthetic exclusion', **kwargs)

    def create(self, selected=None, scope_id=SCOPE, **kwargs):
        selected = selected or ['first', 'second']; preview = self.preview(selected, **kwargs)
        return self.service.create(self.task['id'], self.task['revision'], scope_id, REQUEST, selected,
                                   'Synthetic exclusion', expected_scope_sha256=preview['scope_sha256'], **kwargs)

    def snapshot_old_records(self):
        with self.tasks.transaction() as db:
            return {name: [tuple(row) for row in db.execute('SELECT * FROM ' + name)]
                    for name in ('tasks', 'revisions', 'frozen', 'paper_evaluations', 'reference_target_intents',
                                 'reference_target_receipts', 'reference_target_events')}

    def test_shared_dependency_closure_once_upstream_first_and_stable_selection_order(self):
        first = self.preview(['second', 'first']); second = self.preview(['first', 'second'])
        self.assertEqual(first, second)
        scope = first['scope']
        self.assertEqual(scope['kind'], 'preregistered_scope')
        self.assertEqual(scope['selected_ids'], ['first', 'second'])
        self.assertEqual(scope['dependency_ids'], ['shared', 'post-a', 'post-b'])
        self.assertEqual(scope['excluded_ids'], ['uncertain'])
        self.assertTrue(first['can_prepare_A_plan']); self.assertFalse(first['execution_authorized'])
        self.assertFalse(first['B_input']); self.assertEqual(scope['classification_status'], 'model_proposed_not_verified')

    def test_one_target_does_not_include_unrelated_dependencies(self):
        scope = self.preview(['second'])['scope']
        self.assertEqual(scope['dependency_ids'], ['shared', 'post-b'])
        self.assertEqual(scope['targets'], [self.report['targets'][1]])
        self.assertEqual(scope['inventory_targets'], [self.input['inventory']['targets'][1]])

    def test_selection_rejects_unknown_duplicate_empty_nonstring_and_scientific_fields(self):
        for selected in ([], ['absent'], ['first', 'first'], [1], [{'target_id': 'first', 'priority': 1}]):
            with self.subTest(selected=selected), self.assertRaises(TaskError):
                self.service.preview(self.task['id'], REQUEST, selected, 'Synthetic exclusion')
        with self.assertRaises(TypeError):
            self.service.preview(self.task['id'], REQUEST, ['first'], 'Synthetic exclusion', targets=self.value['targets'])
        with self.assertRaises(TaskError):
            self.service.preview(self.task['id'], REQUEST, ['first'])

    def test_missing_method_is_retained_and_does_not_enable_A_plan(self):
        result = self.preview(['uncertain'])
        self.assertFalse(result['can_prepare_A_plan'])
        self.assertEqual(result['scope']['dependency_ids'], ['unknown'])
        self.assertEqual(len(result['scope']['blockers']), 2)
        self.assertFalse(self.create(['uncertain'])['can_prepare_A_plan'])

    def test_append_receipt_chain_and_no_mutation_of_old_task_freeze_or_model_records(self):
        before = self.snapshot_old_records()
        first = self.create(); second = self.create(['second'], scope_id=SECOND)
        self.assertEqual(first['sequence'], 1); self.assertEqual(second['sequence'], 2)
        self.assertEqual(second['previous_scope_id'], first['id'])
        self.assertEqual(second['previous_receipt_sha256'], first['receipt_sha256'])
        self.assertEqual(before, self.snapshot_old_records())
        self.assertEqual(self.service.status(self.task['id'])['latest']['id'], SECOND)
        self.transport.assert_not_called(); self.key.assert_not_called()
        self.assertEqual(self.calls.status()['used_requests'], 0)

    def test_scope_id_replay_is_idempotent_and_content_conflict_rejected(self):
        first = self.create(); replay = self.create(['second', 'first'])
        self.assertEqual(first, replay); self.assertEqual(len(self.service.history(self.task['id'])), 1)
        with self.assertRaises(TaskError): self.create(['second'])

    def test_preview_digest_and_revision_are_mandatory_before_save(self):
        for digest in (None, H3):
            with self.subTest(digest=digest), self.assertRaises(TaskError):
                self.service.create(self.task['id'], self.task['revision'], SCOPE, REQUEST, ['first', 'second'],
                                    'Synthetic exclusion', expected_scope_sha256=digest)
        digest = self.preview()['scope_sha256']
        with self.assertRaises(StaleTask):
            self.service.create(self.task['id'], self.task['revision'] + 1, SCOPE, REQUEST, ['first', 'second'],
                                'Synthetic exclusion', expected_scope_sha256=digest)
        self.assertEqual(self.service.history(self.task['id']), [])

    def bind_run(self, *, dispatch=False):
        self.ledger = Ledger(self.root / 'execution.sqlite')
        policy = Policy(max_cores=32, max_memory_bytes=8 * 1024**3, max_wall_seconds=604800,
            total_core_seconds=33177600, total_storage_bytes=16 * 1024**3, concurrency=1, approval_sha256=H1)
        self.ledger.create_campaign('synthetic', policy); self.papers.ledger = self.ledger
        evaluation = self.ledger.register_evaluation('synthetic', task_sha256=H1, repetition=0,
                                                     role='reference', system_sha256=H2)
        self.papers.bind_reference_evaluation(self.paper['id'], self.task['id'], evaluation)
        row = self.ledger.reserve(evaluation, 'synthetic-original', H3, Resources(32, 60, 8 * 1024**3, 1024**3))
        if dispatch:
            self.ledger.begin_dispatch(row['id']); self.ledger.uncertain(row['id'], {'synthetic': True})
        return row

    def test_reserved_and_unknown_dispatched_runs_only_create_continuation_and_keep_fees(self):
        row = self.bind_run(dispatch=True); before = self.ledger.get(row['id'])
        record = self.create()
        self.assertEqual(record['scope']['kind'], 'continuation_scope')
        history = record['scope']['prior_execution']['evaluations'][0]
        self.assertEqual(history['dispatch_claims'], 1); self.assertEqual(history['requests'][0]['state'], 'unknown')
        self.assertEqual(before, self.ledger.get(row['id']))
        self.assertFalse(record['execution_authorized'])

    def test_reservation_even_without_job_id_is_not_preregistration(self):
        row = self.bind_run(); self.assertIsNone(row['job_id'])
        self.assertEqual(self.create()['scope']['kind'], 'continuation_scope')

    def test_unavailable_old_ledger_cannot_be_treated_as_zero_history(self):
        self.bind_run(); self.papers.ledger = None
        with self.assertRaises(ReferenceScopeUnavailable): self.create()
        self.assertEqual(self.service.history(self.task['id']), [])

    def test_existing_frozen_record_is_preserved_and_never_replaced(self):
        with self.tasks.transaction() as db:
            db.execute('INSERT INTO frozen VALUES (?,?,?)', (self.task['id'], H3, 'SYNTHETIC_OLD_FROZEN_CANARY'))
        before = self.snapshot_old_records()
        record = self.create()
        self.assertEqual(record['scope']['kind'], 'continuation_scope')
        self.assertEqual(record['scope']['frozen_record_sha256'], H3)
        self.assertEqual(before, self.snapshot_old_records())
        self.assertNotIn('SYNTHETIC_OLD_FROZEN_CANARY', canonical(record).decode())
        self.assertNotIn('B_PRIVATE_CONDITION_CANARY', canonical(record).decode())
        self.assertNotIn('PRIVATE_AUTHOR_CODE_CANARY', canonical(record).decode())

    def test_complete_input_change_with_same_four_hashes_blocks_new_scope_and_marks_history(self):
        first = self.create()
        self.input['sources'][0]['text'] += ' Changed actual source text.'
        with self.assertRaises(ReferenceScopeUnavailable): self.preview()
        historical = self.service.get(self.task['id'], first['id'])
        self.assertEqual(historical['state'], 'source_unavailable')
        self.assertEqual(historical['scope'], first['scope']); self.assertFalse(historical['can_prepare_A_plan'])
        self.assertEqual(len(self.service.history(self.task['id'])), 1)

    def test_source_hash_change_between_preview_and_create_is_rejected(self):
        digest = self.preview()['scope_sha256']
        self.input['source_binding']['pdf_sha256'] = 'd' * 64
        with self.assertRaises(TaskError):
            self.service.create(self.task['id'], self.task['revision'], SCOPE, REQUEST, ['first', 'second'],
                                'Synthetic exclusion', expected_scope_sha256=digest)

    def test_cross_task_source_receipt_and_scope_id_are_rejected(self):
        record = self.create()
        other = self.tasks.create('Other synthetic reference', 'Other', 'reproduction')
        paper = self.papers.get(self.paper['id'])
        self.papers.link_task(paper['id'], paper['revision'], other['id'])
        with self.assertRaises(TaskError): self.service.get(other['id'], record['id'])
        with self.assertRaises(TaskError): self.service.preview(other['id'], REQUEST, ['first'], 'Synthetic exclusion')

    def test_other_unknown_target_request_blocks_selection_of_old_complete_answer(self):
        self.save_target(SECOND, state='unknown')
        with self.assertRaises(ReferenceScopeUnavailable): self.preview()

    def test_failed_or_missing_target_request_does_not_create_scope(self):
        with self.assertRaises(ReferenceScopeUnavailable):
            self.service.preview(self.task['id'], SECOND, ['first'], 'Synthetic exclusion')
        self.save_target(SECOND, state='invalid_output')
        with self.assertRaises(ReferenceScopeUnavailable):
            self.service.preview(self.task['id'], SECOND, ['first'], 'Synthetic exclusion')

    def test_tampered_citation_cycle_report_and_adapter_proof_are_rejected(self):
        for field in ('quote', 'cycle', 'proof', 'model'):
            context, report, model = deepcopy(self.context), deepcopy(self.report), deepcopy(self.model_receipt)
            if field == 'quote': report['targets'][0]['citations'][0]['quote'] = 'Not in supplied P'
            elif field == 'cycle': report['dependencies'][2]['requires'] = ['post-a']
            elif field == 'proof': context['scientific_adapter']['contract_sha256'] = H3
            else: model['requested_model'] = 'wrong-model'
            request = sha256(field.encode())[:32]
            self.save_target(request, context=context, report=report, model=model)
            with self.subTest(field=field), self.assertRaises(TaskError):
                self.service.preview(self.task['id'], request, ['first'], 'Synthetic exclusion')

    def test_append_only_tables_and_persisted_receipt_digest(self):
        record = self.create()
        with self.tasks.transaction() as db:
            for statement in ('DELETE FROM reference_scope_receipts',
                              "UPDATE reference_scope_receipts SET document='{}'"):
                with self.assertRaises(sqlite3.IntegrityError): db.execute(statement)
            db.execute('DROP TRIGGER immutable_reference_scope_UPDATE')
            db.execute("UPDATE reference_scope_receipts SET document='{}'")
        with self.assertRaises(TaskError): self.service.get(self.task['id'], record['id'])

    def test_same_id_concurrent_saves_do_not_duplicate_receipts(self):
        digest = self.preview()['scope_sha256']
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(lambda _: self.service.create(self.task['id'], self.task['revision'], SCOPE, REQUEST,
                ['first', 'second'], 'Synthetic exclusion', expected_scope_sha256=digest), range(2)))
        self.assertEqual(values[0], values[1]); self.assertEqual(len(self.service.history(self.task['id'])), 1)

    def save_review(self):
        failed_request = '4' * 32
        self.save_target(failed_request, state='invalid_output')
        row, context, receipt, _ = self.targets._stored(self.task['id'], failed_request)
        rules = sha256(canonical(NORMALIZATION_RULES)); review_id = '5' * 32
        review = dict(review_id=review_id, task_id=self.task['id'], parent_request=failed_request,
            state='completed', report=deepcopy(self.report), normalization_rules_sha256=rules,
            parent_context_sha256=row['document_sha256'], parent_receipt_sha256=row['receipt_sha256'],
            parent_model_receipt_sha256=H3, source_output_sha256=self.model_receipt['output_sha256'],
            original_model_request_sha256=context['request_sha256'],
            input_sha256=sha256(canonical(context['input'])), semantic_validator_sha256=LEGACY_SEMANTICS_SHA256,
            new_model_calls=0, new_model_cost=0, at='synthetic')
        with self.tasks.transaction() as db:
            db.execute('INSERT INTO reference_target_reviews VALUES (?,?,?,?,?,?,?)',
                (review_id,self.task['id'],failed_request,rules,canonical(review).decode(),sha256(canonical(review)),'synthetic'))
        return failed_request, review_id

    def test_explicit_completed_review_uses_review_report_and_keeps_failed_parent(self):
        parent, review_id = self.save_review()
        before = self.targets._stored(self.task['id'], parent)
        self.assertEqual(self.targets.get(self.task['id'], parent)['state'], 'invalid_output')
        with self.assertRaises(ReferenceScopeUnavailable):
            self.service.preview(self.task['id'], parent, ['first'], 'Synthetic exclusion')
        preview = self.service.preview(self.task['id'], parent, ['first'], 'Synthetic exclusion', target_review_id=review_id)
        record = self.service.create(self.task['id'], self.task['revision'], SECOND, parent, ['first'],
            'Synthetic exclusion', target_review_id=review_id, expected_scope_sha256=preview['scope_sha256'])
        self.assertEqual(record['state'], 'recorded')
        self.assertEqual(record['scope']['provenance']['target_review_id'], review_id)
        self.assertEqual(self.targets._stored(self.task['id'], parent), before)
        self.transport.assert_not_called(); self.key.assert_not_called()

    def test_completed_review_with_changed_actual_P_cannot_be_used_from_retained_report(self):
        parent, review_id = self.save_review()
        self.input['sources'][0]['text'] += ' Changed source'
        review = self.targets.reviews(self.task['id'], parent)[0]
        self.assertEqual(review['state'], 'source_unavailable'); self.assertIsNotNone(review['report'])
        with self.assertRaises(TaskError):
            self.service.preview(self.task['id'], parent, ['first'], 'Synthetic exclusion', target_review_id=review_id)

    def test_review_identity_and_parent_hash_cannot_be_borrowed(self):
        parent, review_id = self.save_review()
        with self.assertRaises(TaskError):
            self.service.preview(self.task['id'], REQUEST, ['first'], 'Synthetic exclusion', target_review_id=review_id)
        with self.tasks.transaction() as db:
            db.execute('DROP TRIGGER immutable_reference_target_reviews_UPDATE')
            row = db.execute('SELECT document FROM reference_target_reviews WHERE id=?', (review_id,)).fetchone()
            value = json.loads(row['document']); value['parent_context_sha256'] = H3
            db.execute('UPDATE reference_target_reviews SET document=?,document_sha256=? WHERE id=?',
                       (canonical(value).decode(),sha256(canonical(value)),review_id))
        with self.assertRaises(TaskError):
            self.service.preview(self.task['id'], parent, ['first'], 'Synthetic exclusion', target_review_id=review_id)

    def test_retained_legacy_adapter_is_bound_without_replacing_it_with_current_contract(self):
        context = deepcopy(self.context); payload = json.loads(context['messages'][-1]['content'])
        adapter = payload['scientific_adapter']; menu = payload['reference_target_dependency_menu']
        context['controller_sha256'] = menu['source_sha256'] = LEGACY_CONTROLLER_SHA256
        adapter['source_sha256'] = LEGACY_ADAPTER_SHA256
        menu['version'] = 1; menu.pop('output_types'); menu.pop('presentation_normalization')
        adapter['rules'].pop('presentation_normalization')
        adapter['capabilities']['reference_target_dependency_menu']['contract_sha256'] = sha256(canonical(menu))
        context['messages'][-1]['content'] = canonical(payload).decode()
        context['scientific_adapter']['contract_sha256'] = sha256(canonical(adapter))
        binding = context['model_binding']
        context['request_sha256'] = sha256(request_body(DeepSeekConfig(**binding['config']), context['messages'],
                                                       model=binding['model'], thinking=binding['thinking']))
        model = dict(self.model_receipt, request_sha256=context['request_sha256'])
        self.save_target(SECOND, context=context, model=model)
        result = self.service.preview(self.task['id'], SECOND, ['first'], 'Synthetic exclusion')
        self.assertEqual(result['scope']['provenance']['scientific_adapter'], context['scientific_adapter'])
        self.assertEqual(result['scope']['provenance']['controller_sha256'], LEGACY_CONTROLLER_SHA256)

    def test_changed_messages_or_model_options_cannot_keep_original_request_sha(self):
        for key in ('messages', 'config', 'thinking'):
            context = deepcopy(self.context)
            if key == 'messages': context['messages'][0]['content'] += ' Changed request bytes.'
            elif key == 'config': context['model_binding']['config']['max_output_tokens'] += 1
            else: context['model_binding']['thinking'] = {'type': 'enabled'}
            request = sha256(key.encode())[:32]
            self.save_target(request, context=context)
            with self.subTest(key=key), self.assertRaises(TaskError):
                self.service.preview(self.task['id'], request, ['first'], 'Synthetic exclusion')

    def test_finished_task_keeps_scope_history_but_cannot_append(self):
        first = self.create()
        with self.tasks.transaction() as db:
            db.execute('INSERT INTO task_lifecycle VALUES (?,?,?,?)', (self.task['id'], 1, 'finish', 'synthetic'))
        with self.assertRaises(TaskError): self.create(scope_id=SECOND)
        self.assertEqual(self.service.get(self.task['id'], first['id'])['receipt_sha256'], first['receipt_sha256'])

    def test_paused_task_rejects_preview_and_save_without_new_scope_or_model_call(self):
        digest = self.preview()['scope_sha256']
        paused = self.tasks.set_paused(self.task['id'], self.task['revision'], True)
        with self.assertRaises(TaskError): self.preview()
        with self.assertRaises(TaskError):
            self.service.create(self.task['id'], paused['revision'], SCOPE, REQUEST, ['first', 'second'],
                                'Synthetic exclusion', expected_scope_sha256=digest)
        self.assertEqual(self.service.history(self.task['id']), [])
        self.transport.assert_not_called(); self.key.assert_not_called()

    def test_paused_history_is_readable_without_model_connection_but_cannot_prepare_A(self):
        first = self.create()
        self.tasks.set_paused(self.task['id'], self.task['revision'], True)
        self.targets.connection_provider = Mock(side_effect=AssertionError('must not need model connection'))
        saved = self.service.status(self.task['id'])['latest']
        self.assertEqual(saved['scope'], first['scope']); self.assertEqual(saved['state'], 'recorded')
        self.assertFalse(saved['can_prepare_A_plan']); self.assertIn('暂停', saved['disabled_reason'])

    def test_same_task_nonreference_evaluation_does_not_hide_A_history_or_import_B_data(self):
        self.bind_run()
        evaluation = self.ledger.register_evaluation('synthetic', task_sha256=H3, repetition=0,
                                                     role='agent', system_sha256=H2)
        # Synthetic pre-existing product association. The scope service must not
        # modify this relationship or read any B scientific conditions/results.
        with self.tasks.transaction() as db:
            db.execute('INSERT INTO paper_evaluations VALUES (?,?,?,?)',
                       (self.paper['id'], self.task['id'], evaluation, H3))
        before = self.snapshot_old_records()
        result = self.create(); history = result['scope']['prior_execution']
        self.assertEqual(result['scope']['kind'], 'continuation_scope')
        self.assertEqual(len(history['evaluations']), 1)
        self.assertEqual(history['other_evaluations'], [dict(evaluation_id=evaluation, role='agent',
                                                           dispatch_claims=0, reserved_attempts=0)])
        self.assertEqual(before, self.snapshot_old_records())
        self.assertNotIn('B_PRIVATE_CONDITION_CANARY', canonical(result).decode())


if __name__ == '__main__':
    unittest.main()
