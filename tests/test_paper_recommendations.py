"""Synthetic metadata, provider, identity and accounting checks only."""
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from auto_lammps.deepseek import DeepSeekConfig, ModelCalls
from auto_lammps.discovery_library import DiscoveryLibrary
from auto_lammps.manifest import canonical, sha256
from auto_lammps.model_connections import ModelConnections, official_request
from auto_lammps.paper_recommendations import (
    PaperRecommendations, SCOPE_PREFIX, recommendation_selection_context,
)
from auto_lammps.tasks import TaskError, TaskStore


class PaperRecommendationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.tasks = TaskStore(self.root / 'tasks.sqlite')
        self.source = self.root / 'synthetic-catalog.json'
        self.row = dict(repo='synthetic/alpha', commit='a' * 40,
            title='Full synthetic alpha paper title', doi='10.1234/alpha',
            pair_style=['meam'], potentials=['library.meam', 'alpha.meam'],
            inputs=['workflow/in.alpha'], elements=['Co', 'Ni'], elements_complete=True,
            type_order=['Co', 'Ni'], license='MIT', evidence='Pinned synthetic metadata claim',
            process=['Synthetic workflow'], repository_role='author_source')
        self.doc = dict(generated='synthetic-2026', tier_a=[self.row], tier_b=[], tier_c=[])
        self.save()
        self.library = DiscoveryLibrary(self.source)
        self.calls = ModelCalls(self.root / 'model.sqlite', DeepSeekConfig('synthetic-model',
                                max_input_bytes=262144), max_requests=40)
        self.transport = Mock(side_effect=self.response)
        self.connections = ModelConnections(self.tasks, calls=self.calls, transport=self.transport)
        self.connections.save('deepseek-official', 'synthetic-model', 'synthetic-connection-key')
        self.service = PaperRecommendations(self.tasks, self.connections, self.library)
        self.scope, self.request = '1' * 32, '2' * 32
        self.preparer = patch('auto_lammps.paper_recommendations.prepare_stage_messages',
                              side_effect=self.prepare)
        self.prepare_mock = self.preparer.start()
        self.addCleanup(self.preparer.stop)

    def save(self):
        self.source.write_text(json.dumps(self.doc))
        self.source.chmod(0o600)

    def prepare(self, stage, messages, evidence):
        self.assertEqual(stage, 'paper_recommendation')
        self.assertEqual(evidence['role'], 'reference_only')
        capability = recommendation_selection_context(evidence['verified_candidates'])
        self.assertEqual(capability['tool'], 'registered_paper_candidate_menu')
        payload = json.loads(messages[-1]['content'])
        payload['scientific_adapter'] = {'stage': stage, 'role': 'reference_only',
                                         'capabilities': {'registered_paper_candidate_menu': capability}}
        messages[-1]['content'] = canonical(payload).decode()
        return messages, {'stage': stage, 'evidence_sha256': sha256(canonical(evidence)),
                          'scope': 'reference_only'}

    def selection(self, payload):
        candidate = json.loads(payload['messages'][-1]['content'])['verified_candidates'][0]
        first = next(e['index'] for e in candidate['evidence'] if e['kind'] == 'author_source')
        second = next(e['index'] for e in candidate['evidence'] if e['kind'] == 'potential')
        return dict(candidate_index=0, reason='两种登记资源来自相同固定版本；仍须核验。', citations=[first, second])

    def response(self, provider, key, method, endpoint, payload):
        self.assertEqual((provider, method, endpoint), ('deepseek-official', 'POST', '/chat/completions'))
        self.assertEqual(key, 'synthetic-connection-key')
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(self.selection(payload))}}],
                'usage': {'total_tokens': 19, 'completion_tokens': 9}}

    def create(self, request=None, scope=None, **kwargs):
        return self.service.create(scope or self.scope, request or self.request,
                                   'deepseek-official', '请推荐下一篇成套候选', **kwargs)

    def test_fixed_pair_selection_has_original_titles_citations_and_no_compute_task(self):
        with patch.object(self.tasks, 'get', side_effect=AssertionError('Must not require compute task')):
            result = self.create()
            self.assertEqual(self.create(), result)
            self.assertEqual(self.service.history(self.scope), [result])
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['scope_id'], SCOPE_PREFIX + self.scope)
        candidate = result['document']['candidate']
        self.assertEqual(candidate['paper_title'], self.row['title'])
        self.assertEqual(candidate['doi'], self.row['doi'])
        self.assertEqual(candidate['author_source']['commit'], self.row['commit'])
        self.assertEqual(candidate['potential']['license'], 'MIT')
        self.assertEqual({c['kind'] for c in result['document']['citations']}, {'author_source', 'potential'})
        self.assertFalse(result['execution_ready'])
        self.assertEqual(result['scientific_status'], 'not_evaluated')
        self.assertEqual(self.tasks.list(), [])
        self.assertEqual(self.calls.status()['used_requests'], 1)
        self.transport.assert_called_once()
        receipt = self.calls.history()[0]['receipt']
        self.assertEqual(receipt['purpose'], 'paper_recommendation')
        self.assertEqual(receipt['scope'], 'reference_only')
        self.assertEqual(receipt['transport_attempts'], 1)
        self.assertEqual(receipt['usage']['total_tokens'], 19)

    def test_complete_service_dispatch_uses_real_active_adapter_and_explicit_menu_contract(self):
        self.preparer.stop()
        result=self.create()
        self.assertEqual(result['state'],'completed',result)
        payload=self.transport.call_args.args[-1]
        supplied=json.loads(payload['messages'][-1]['content'])
        self.assertIn('registered_paper_candidate_menu',supplied)
        self.assertEqual(supplied['scientific_adapter']['stage'],'paper_recommendation')
        self.assertEqual(supplied['registered_paper_candidate_menu']['scope'],'reference_only')
        self.assertFalse(supplied['registered_paper_candidate_menu']['B_input'])
        self.assertEqual(supplied['registered_paper_candidate_menu']['citation_count'],{'minimum':2,'maximum':16})

    def test_large_ineligible_catalog_retains_full_evidence_but_does_not_duplicate_model_file_lists(self):
        self.preparer.stop()
        extras=[]
        for i in range(15):
            extras.append({**self.row,'repo':f'synthetic/missing-{i}',
                'doi':f'10.1234/missing-{i}','license':'NOASSERTION',
                'evidence':'Synthetic unresolved declaration '+str(i),
                'inputs':[f'workflow/long-{j}-'+('x'*100)+'.lmp' for j in range(150)]})
        self.doc['tier_a']+=extras
        self.save()
        menu=self.service.menu()
        self.assertGreater(len(canonical(menu)),self.calls.config.max_input_bytes)
        result=self.create()
        self.assertEqual(result['state'],'completed',result)
        payload=self.transport.call_args.args[-1]
        supplied=json.loads(payload['messages'][-1]['content'])
        self.assertLess(len(canonical(payload)),self.calls.config.max_input_bytes)
        self.assertEqual(len(supplied['verified_candidates']),len(menu['verified_candidates']))
        self.assertEqual(len(supplied['excluded_candidates']),len(menu['excluded_candidates']))
        with self.tasks.transaction() as db:
            retained=json.loads(db.execute('SELECT context FROM paper_recommendation_intents WHERE id=?',(self.request,)).fetchone()[0])
        self.assertEqual(retained['excluded_candidates'],menu['excluded_candidates'])

    def test_no_catalog_candidates_preserves_request_without_key_read_or_model_call(self):
        self.doc['tier_a'] = []
        self.save()
        with patch.object(self.connections, '_read', side_effect=AssertionError('No key for empty menu')):
            result = self.create()
            self.assertEqual(self.create(), result)
        self.assertEqual(result['state'], 'unavailable')
        self.assertEqual(result['document']['error_code'], 'no_paired_candidates')
        self.transport.assert_not_called()
        self.assertEqual(self.calls.status()['used_requests'], 0)

    def test_conflict_and_incomplete_pairs_never_reach_model(self):
        for index, change in enumerate((dict(potentials=[], pair_style=[]), dict(inputs=[]),
                                      dict(elements_complete=False), dict(license='NOASSERTION'),
                                      dict(repository_role='validation_tests'))):
            with self.subTest(change=change):
                self.doc['tier_a'] = [dict(self.row, **change)]
                self.save()
                result = self.create(request=f'{index+3:032x}', scope=f'{index+200:032x}')
                self.assertEqual(result['state'], 'unavailable')
                self.assertTrue(result['document']['excluded_candidates'])
        self.transport.assert_not_called()

    def test_resource_reviews_are_part_of_frozen_identity(self):
        reviews = self.root / 'reviews.json'
        reviews.write_text(json.dumps(dict(version=1, reviews=[dict(repository=self.row['repo'],
            commit=self.row['commit'], state='conflict', notes=['Synthetic conflicting resource evidence'])])))
        self.service.library = DiscoveryLibrary(self.source, reviews)
        result = self.create()
        self.assertEqual(result['state'], 'unavailable')
        self.assertIn('Synthetic conflicting resource evidence', str(result['document']))
        self.transport.assert_not_called()

    def test_unknown_license_spellings_do_not_become_eligible_pairs(self):
        for name in ('undeclared', ' UNDECLARED ', 'unknown', 'NOASSERTION',
                     'none', 'unlicensed', '未声明', 'not specified'):
            with self.subTest(name=name):
                self.doc['tier_a'] = [dict(self.row, license=name)]
                self.save()
                menu = self.service.menu()
                self.assertEqual(menu['verified_candidates'], [])
                self.assertEqual(len(menu['excluded_candidates']), 1)
                self.assertIn('来源许可证尚未声明', menu['excluded_candidates'][0]['missing_evidence'])
        self.transport.assert_not_called()
        self.assertEqual(self.calls.status()['used_requests'], 0)

    def test_conflicting_paired_resource_mapping_cannot_be_selected(self):
        self.doc['tier_a'] = [dict(self.row, kind='author_source'),
                              dict(self.row, kind='potential', type_order=['Ni', 'Co'])]
        self.save()
        result = self.create()
        self.assertEqual(result['state'], 'unavailable')
        self.assertIn('映射声明冲突', str(result['document']))
        self.transport.assert_not_called()

    def test_doi_exclusions_normalize_bind_request_and_keep_remaining_fixed_menu(self):
        beta = dict(self.row, repo='synthetic/beta', commit='b' * 40,
                    title='Full synthetic beta paper title', doi='10.1234/beta')
        self.doc['tier_a'].append(beta)
        self.save()
        result = self.create(exclude_dois=['https://doi.org/10.1234/ALPHA'])
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['document']['candidate']['doi'], beta['doi'])
        self.assertEqual(self.create(exclude_dois=['doi:10.1234/alpha']), result)
        with self.assertRaises(TaskError):
            self.create(exclude_dois=[])
        self.transport.assert_called_once()
        with self.assertRaises(TaskError):
            self.service.menu(exclude_dois=['not a DOI'])
        empty = self.create(request='3' * 32, exclude_dois=[self.row['doi'], beta['doi']])
        self.assertEqual(empty['state'], 'unavailable')
        self.transport.assert_called_once()

    def test_output_must_select_declared_index_with_both_resource_citations(self):
        bad = [dict(candidate_index=99, reason='候选', citations=[0, 11]),
               dict(candidate_index=True, reason='候选', citations=[0, 11]),
               dict(candidate_index=0, reason='候选', citations=[]),
               dict(candidate_index=0, reason='候选', citations=[0, 1]),
               dict(candidate_index=0, reason='候选', citations=[0, 999]),
               dict(candidate_index=0, reason='候选', citations=[0, 0]),
               dict(candidate_index=0, reason='候选', citations=[0, 11], execution_ready=True),
               dict(candidate_index=0, reason='候选')]
        for index, selection in enumerate(bad):
            with self.subTest(selection=selection):
                self.transport.side_effect = None
                self.transport.return_value = {'choices': [{'finish_reason': 'stop',
                    'message': {'content': json.dumps(selection)}}], 'usage': {'total_tokens': 19}}
                result = self.create(request=f'{index+10:032x}', scope=f'{index+100:032x}')
                self.assertEqual(result['state'], 'invalid_output')
                self.assertEqual(result['usage']['total_tokens'], 19)
                self.assertNotIn('candidate', result['document'])
        self.assertEqual(self.calls.status()['used_requests'], len(bad))

    def test_duplicate_json_keys_and_truncated_generation_retain_original_output(self):
        self.transport.side_effect = None
        content = '{"candidate_index":0,"candidate_index":0,"reason":"候选","citations":[0,11]}'
        self.transport.return_value = {'choices': [{'finish_reason': 'stop', 'message': {'content': content}}],
                                       'usage': {'total_tokens': 7}}
        self.assertEqual(self.create()['state'], 'invalid_output')
        truncated = '{"candidate_index":0,"reason":"synthetic-connection-key'
        self.transport.return_value = {'choices': [{'finish_reason': 'length', 'message': {'content': truncated}}],
                                       'usage': {'total_tokens': 11}}
        result = self.create(request='3' * 32)
        self.assertEqual(result['state'], 'invalid_output')
        self.assertEqual(result['diagnostics']['finish_reason'], 'length')
        self.assertEqual(result['usage']['total_tokens'], 11)
        with self.tasks.transaction() as db:
            stored = db.execute('SELECT output FROM paper_recommendation_receipts WHERE id=?', ('3'*32,)).fetchone()[0]
        self.assertIn('[已隐藏密钥]', stored)
        self.assertNotIn('synthetic-connection-key', str(result) + stored + str(self.calls.history()))
        self.assertEqual(self.create(request='3'*32), result)
        self.assertEqual(self.transport.call_count, 2)

    def test_unknown_transport_is_not_resent_after_restart_or_connection_removal(self):
        self.transport.side_effect = TimeoutError('synthetic interrupted transport')
        result = self.create()
        self.assertEqual(result['state'], 'failed_or_unknown')
        self.connections.save('deepseek-official', '', remove=True)
        self.service = PaperRecommendations(self.tasks, self.connections, self.library)
        self.assertEqual(self.create(), result)
        self.transport.assert_called_once()
        self.assertEqual(self.calls.status()['used_requests'], 1)

    def test_missing_receipt_intent_is_unknown_and_not_replayed(self):
        context = {**self.service.menu(), 'scope_id': SCOPE_PREFIX + self.scope,
                   'question': '请推荐下一篇成套候选'}
        with self.tasks.transaction() as db:
            db.execute('INSERT INTO paper_recommendation_scopes VALUES (?,?,?,?)',
                       (SCOPE_PREFIX+self.scope, 'reference_only', 3, 'synthetic'))
            db.execute('INSERT INTO paper_recommendation_intents VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (self.request, SCOPE_PREFIX+self.scope, 'deepseek-official', 'synthetic-model',
                        '请推荐下一篇成套候选', sha256(canonical(context)), canonical(context).decode(), None, None, 'synthetic'))
        self.assertEqual(self.create()['state'], 'unknown')
        self.transport.assert_not_called()

    def test_source_change_after_response_rejects_selection_and_preserves_charge(self):
        def changing(*args):
            result = self.response(*args)
            self.row['commit'] = 'f'*40
            self.save()
            return result
        self.transport.side_effect = changing
        result = self.create()
        self.assertEqual(result['state'], 'source_unavailable')
        self.assertEqual(result['usage']['total_tokens'], 19)
        self.assertEqual(self.calls.status()['used_requests'], 1)
        self.assertEqual(self.create(), result)
        self.transport.assert_called_once()

    def test_completed_history_revalidates_catalog_and_preserves_original_receipt(self):
        original = self.create()
        self.doc['generated'] = 'changed synthetic source identity'
        self.save()
        current = self.create()
        self.assertEqual(current['state'], 'source_unavailable')
        self.assertEqual(current['original_state'], 'completed')
        self.assertEqual(current['document'], original['document'])
        with self.tasks.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM paper_recommendation_receipts').fetchone()[0], 'completed')
        self.transport.assert_called_once()

    def test_connection_rotation_during_transport_rejects_selection(self):
        def rotating(*args):
            result = self.response(*args)
            self.connections.save('deepseek-official', 'rotated-model', 'rotated-synthetic-key')
            return result
        self.transport.side_effect = rotating
        self.assertEqual(self.create()['state'], 'connection_changed')
        self.assertEqual(self.calls.status()['used_requests'], 1)
        self.transport.assert_called_once()

    def test_scope_cap_cannot_reset_when_service_configuration_changes(self):
        for index in range(3):
            self.assertEqual(self.create(request=f'{index+30:032x}')['state'], 'completed')
        self.service = PaperRecommendations(self.tasks, self.connections, self.library, max_requests_per_scope=16)
        with self.assertRaises(TaskError):
            self.create(request='4'*32)
        self.assertEqual(self.calls.status()['used_requests'], 3)
        self.assertEqual(self.transport.call_count, 3)
        with self.tasks.transaction() as db:
            self.assertEqual(db.execute('SELECT max_requests FROM paper_recommendation_scopes').fetchone()[0], 3)

    def test_zero_model_allowance_is_not_sent_and_reads_no_key(self):
        self.connections.calls = ModelCalls(self.root/'zero-model.sqlite', DeepSeekConfig('synthetic-model'), max_requests=0)
        with patch.object(self.connections, '_read', side_effect=AssertionError('No key for zero allowance')):
            result = self.create()
        self.assertEqual(result['state'], 'not_sent')
        self.transport.assert_not_called()
        self.assertEqual(self.connections.calls.status()['used_requests'], 0)

    def test_private_paths_and_unlisted_source_contents_are_not_sent(self):
        private_root = '/' + 'Users' + '/synthetic/private'
        self.row.update(evidence=f'source at {private_root}/author-solution',
                        pdf_path=f'{private_root}/paper.pdf', author_solution='private input contents')
        self.save()
        result = self.create()
        self.assertEqual(result['state'], 'unavailable')
        self.transport.assert_not_called()
        self.assertNotIn('/Users/', str(result))
        self.assertNotIn('private input contents', str(result))
        self.assertNotIn('pdf_path', str(result))

    def test_https_metadata_is_public_but_windows_absolute_paths_are_private(self):
        public = self.service.menu()
        self.assertEqual(len(public['verified_candidates']), 1)
        self.assertTrue(public['verified_candidates'][0]['author_source']['url'].startswith('https://'))
        self.row['evidence'] = r'Private source C:\private\synthetic\author-input'
        self.save()
        private = self.service.menu()
        self.assertEqual(private['verified_candidates'], [])
        self.assertNotIn('C:', str(private))

    def test_real_mandatory_adapter_uses_reference_only_registered_menu(self):
        self.preparer.stop()
        result = self.create()
        self.assertEqual(result['state'], 'completed')
        payload = self.transport.call_args.args[4]
        contract = json.loads(payload['messages'][-1]['content'])['scientific_adapter']
        self.assertEqual(contract['stage'], 'paper_recommendation')
        self.assertEqual(set(contract['capabilities']), {'registered_paper_candidate_menu'})
        self.assertEqual(contract['capabilities']['registered_paper_candidate_menu']['scope'], 'reference_only')
        self.assertFalse(result['execution_ready'])

    def test_oversized_menu_is_not_silently_filtered_or_truncated(self):
        self.doc['tier_a'] = [dict(self.row, repo=f'synthetic/r{index}', commit=f'{index+1:040x}',
                                  doi=f'10.1234/synthetic-{index}') for index in range(65)]
        self.save()
        result = self.create()
        self.assertEqual(result['state'], 'unavailable')
        self.assertEqual(result['document']['error_code'], 'candidate_menu_too_large')
        self.transport.assert_not_called()
        self.assertEqual(self.calls.status()['used_requests'], 0)

    def test_same_id_concurrent_requests_send_once_and_observer_sees_unknown(self):
        entered, release = threading.Event(), threading.Event()
        def held(*args):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('synthetic test release missing')
            return self.response(*args)
        self.transport.side_effect = held
        answers, failures = [], []
        def worker():
            try: answers.append(self.create())
            except BaseException as error: failures.append(error)
        thread = threading.Thread(target=worker)
        thread.start()
        self.addCleanup(lambda: release.set())
        self.assertTrue(entered.wait(5))
        try:
            self.assertEqual(self.create()['state'], 'unknown')
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(answers[0]['state'], 'completed')
        self.transport.assert_called_once()
        self.assertEqual(self.calls.status()['used_requests'], 1)

    def test_request_and_receipt_tables_are_immutable(self):
        self.create()
        for table in ('paper_recommendation_scopes', 'paper_recommendation_intents', 'paper_recommendation_receipts'):
            for statement in (f'DELETE FROM {table}', f'UPDATE {table} SET id=id'):
                with self.subTest(statement=statement), self.assertRaises(sqlite3.DatabaseError):
                    with self.tasks.transaction() as db: db.execute(statement)

    def test_pre_dispatch_catalog_change_prevents_transport(self):
        def prepare_then_change(*args):
            result = self.prepare(*args)
            self.row['commit'] = 'f'*40
            self.save()
            return result
        self.prepare_mock.side_effect = prepare_then_change
        self.assertEqual(self.create()['state'], 'source_unavailable')
        self.transport.assert_not_called()
        self.assertEqual(self.calls.status()['used_requests'], 0)

    def test_model_request_id_collision_does_not_copy_or_overwrite_other_receipt(self):
        self.calls.reserve(self.request, b'synthetic-other-purpose')
        self.calls.record(self.request, {'purpose': 'synthetic-other-purpose', 'state': 'completed'})
        result = self.create()
        self.assertEqual(result['state'], 'not_sent')
        self.assertEqual(result['document']['error_code'], 'request_already_reserved')
        self.transport.assert_not_called()
        self.assertEqual(self.calls.lookup(self.request)['receipt']['purpose'], 'synthetic-other-purpose')

    def test_real_active_adapter_and_official_transport_preserve_402_401_429_rejections(self):
        self.preparer.stop()
        self.connections.transport = official_request
        response = Mock()
        response.read.side_effect = AssertionError('Never read private supplier error body')
        connection = Mock()
        connection.getresponse.return_value = response
        with patch('auto_lammps.model_connections.ssl_context', return_value=object()), \
             patch('auto_lammps.model_connections.http.client.HTTPSConnection', return_value=connection):
            for i, status in enumerate((402, 401, 429)):
                with self.subTest(status=status):
                    response.status = status
                    rid = f'{i+2:032x}'
                    result = self.create(request=rid)
                    self.assertEqual(result['state'], 'rejected', result)
                    self.assertEqual(result['document']['error_code'], 'provider_request_failed')
                    self.assertEqual(result['diagnostics']['http_status'], status)
                    self.assertEqual(result['diagnostics']['transport_attempts'], 1)
                    self.assertEqual(result['diagnostics']['request_outcome'], 'provider_rejected')
                    self.assertEqual(result['usage'], {})  # No supplier usage was received.
                    receipt = self.calls.lookup(rid)['receipt']
                    self.assertEqual(receipt['state'], 'rejected')
                    self.assertEqual(receipt['http_status'], status)
                    self.assertEqual(receipt['transport_attempts'], 1)
                    self.assertEqual(receipt['scientific_adapter']['stage'], 'paper_recommendation')
                    payload = json.loads(connection.request.call_args.kwargs['body'])
                    supplied = json.loads(payload['messages'][-1]['content'])
                    self.assertEqual(supplied['scientific_adapter']['stage'], 'paper_recommendation')
                    self.assertIn('registered_paper_candidate_menu', supplied)
                    count = connection.request.call_count
                    self.assertEqual(self.create(request=rid), result)
                    self.assertEqual(connection.request.call_count, count)
        response.read.assert_not_called()
        self.assertEqual(connection.request.call_count, 3)
        self.assertEqual(self.calls.status()['used_requests'], 3)
        self.service = PaperRecommendations(self.tasks, self.connections, self.library, max_requests_per_scope=12)
        with self.assertRaises(TaskError):
            self.create(request='f'*32)
        self.assertEqual(self.tasks.list(), [])
        self.assertNotIn('synthetic-connection-key', str(self.calls.history()) + str(self.service.history(self.scope)))

    def test_real_transport_preconnection_failure_is_not_sent_but_reservation_is_retained(self):
        self.preparer.stop()
        self.connections.transport = official_request
        connection = Mock()
        connection.connect.side_effect = OSError('synthetic-connection-key private path')
        with patch('auto_lammps.model_connections.ssl_context', return_value=object()), \
             patch('auto_lammps.model_connections.http.client.HTTPSConnection', return_value=connection):
            result = self.create()
            self.assertEqual(result['state'], 'not_sent')
            self.assertEqual(result['document']['error_code'], 'provider_connection_failed')
            self.assertEqual(result['diagnostics']['transport_attempts'], 0)
            self.assertEqual(self.create(), result)
        connection.connect.assert_called_once()
        connection.request.assert_not_called()
        receipt = self.calls.lookup(self.request)['receipt']
        self.assertEqual(receipt['transport_attempts'], 0)
        self.assertEqual(receipt['state'], 'not_sent')
        self.assertEqual(self.calls.status()['used_requests'], 1)

    def test_transport_value_errors_cannot_be_claimed_unsent_or_selection_invalid(self):
        for i, error in enumerate((ValueError('selection_invalid'), TaskError('synthetic-connection-key'))):
            with self.subTest(error_type=type(error)):
                self.transport.side_effect = error
                rid = f'{i+2:032x}'
                result = self.create(request=rid)
                self.assertEqual(result['state'], 'failed_or_unknown')
                self.assertEqual(result['document']['error_code'], 'request_failed_or_unknown')
                self.assertEqual(result['diagnostics']['transport_attempts'], 1)
                self.assertIsNone(result['diagnostics']['http_status'])
                self.assertEqual(self.calls.lookup(rid)['receipt']['transport_attempts'], 1)
                self.assertEqual(self.create(request=rid), result)
        self.assertEqual(self.transport.call_count, 2)
        self.assertNotIn('synthetic-connection-key', str(self.service.history(self.scope)))

    def test_real_transport_unknown_and_received_protocol_invalid_keep_distinct_receipts(self):
        self.preparer.stop()
        self.connections.transport = official_request
        response = Mock(status=200)
        connection = Mock()
        connection.getresponse.return_value = response
        with patch('auto_lammps.model_connections.ssl_context', return_value=object()), \
             patch('auto_lammps.model_connections.http.client.HTTPSConnection', return_value=connection):
            response.read.side_effect = TimeoutError('synthetic-connection-key')
            result = self.create()
            self.assertEqual(result['state'], 'failed_or_unknown')
            self.assertEqual(result['diagnostics']['http_status'], 200)
            self.assertEqual(result['diagnostics']['request_outcome'], 'unknown')
            response.read.side_effect = None
            response.read.return_value = b'synthetic-connection-key malformed JSON'
            invalid = self.create(request='3'*32)
            self.assertEqual(invalid['state'], 'invalid_output')
            self.assertEqual(invalid['diagnostics']['request_outcome'], 'response_invalid')
            self.assertEqual(invalid['diagnostics']['http_status'], 200)
            self.assertEqual(invalid['document']['error_code'], 'provider_response_invalid')
            self.connections.save('deepseek-official', '', remove=True)
            self.service = PaperRecommendations(self.tasks, self.connections, self.library)
            with patch.object(self.connections, '_read', side_effect=AssertionError('No key for history')):
                self.assertEqual(self.create(), result)
                self.assertEqual(self.create(request='3'*32), invalid)
        self.assertEqual(connection.request.call_count, 2)
        self.assertEqual(self.calls.status()['used_requests'], 2)
        self.assertNotIn('synthetic-connection-key', str(self.service.history(self.scope)) + str(self.calls.history()))

    def test_received_missing_completion_envelope_is_invalid_output_with_known_usage_retained(self):
        self.transport.side_effect = None
        self.transport.return_value = {'usage': {'total_tokens': 11}}
        result = self.create()
        self.assertEqual(result['state'], 'invalid_output')
        self.assertEqual(result['document']['error_code'], 'response_shape_invalid')
        self.assertEqual(result['usage'], {'total_tokens': 11})
        self.assertEqual(self.calls.lookup(self.request)['receipt']['usage'], {'total_tokens': 11})
        self.assertEqual(result['diagnostics']['transport_attempts'], 1)
        self.assertEqual(self.create(), result)
        self.transport.assert_called_once()

    def legacy_intent(self, *, matching_payload=True, attempted=1, purpose='paper_recommendation'):
        context = {**self.service.menu(), 'scope_id': SCOPE_PREFIX+self.scope,
                   'question': '请推荐下一篇成套候选'}
        payload = canonical({'model': 'synthetic-model', 'synthetic_old_request': True})
        self.calls.reserve(self.request, payload if matching_payload else b'unrelated request')
        self.calls.record(self.request, {'purpose': purpose, 'requested_model': 'synthetic-model',
            'state': 'not_sent', 'transport_attempts': attempted, 'error_code': 'selection_invalid',
            'response_sha256': None, 'structured_output': None, 'usage': {}})
        with self.tasks.transaction() as db:
            db.execute('INSERT INTO paper_recommendation_scopes VALUES (?,?,?,?)',
                       (SCOPE_PREFIX+self.scope, 'reference_only', 3, 'synthetic'))
            db.execute('INSERT INTO paper_recommendation_intents VALUES (?,?,?,?,?,?,?,?,?,?)',
                (self.request, SCOPE_PREFIX+self.scope, 'deepseek-official', 'synthetic-model',
                 '请推荐下一篇成套候选', sha256(canonical(context)), canonical(context).decode(),
                 None, payload.decode(), 'synthetic'))
            db.execute('INSERT INTO paper_recommendation_receipts VALUES (?,?,?,?,?,?)',
                (self.request, 'not_sent', canonical({'error_code': 'selection_invalid',
                 'message': '旧未发送标记'}).decode(), '{}', None, '{}'))

    def test_legacy_misclassification_is_readonly_reconciled_without_inventing_http_status(self):
        self.legacy_intent()
        with self.tasks.transaction() as db:
            before = tuple(db.execute('SELECT * FROM paper_recommendation_receipts').fetchone())
        model_before = self.calls.history()
        with patch.object(self.connections, '_read', side_effect=AssertionError('No key for history')):
            result = self.service.history(self.scope)[0]
            self.assertEqual(result['state'], 'failed_or_unknown')
            self.assertEqual(result['original_state'], 'not_sent')
            self.assertEqual(result['original_document']['error_code'], 'selection_invalid')
            self.assertEqual(result['document']['error_code'], 'legacy_request_attempted')
            self.assertEqual(result['diagnostics']['transport_attempts'], 1)
            self.assertIsNone(result['diagnostics']['http_status'])
            self.assertNotIn('402', str(result))
            self.assertEqual(self.create(), result)
        self.transport.assert_not_called()
        self.assertEqual(self.calls.history(), model_before)
        with self.tasks.transaction() as db:
            after = tuple(db.execute('SELECT * FROM paper_recommendation_receipts').fetchone())
        self.assertEqual(before, after)
        self.assertEqual(self.calls.status()['used_requests'], 1)

    def test_unrelated_model_receipt_does_not_prove_legacy_transmission(self):
        self.legacy_intent(matching_payload=False)
        self.assertEqual(self.service.history(self.scope)[0]['state'], 'not_sent')
        self.transport.assert_not_called()

    def test_legacy_unattempted_receipt_is_preserved(self):
        self.legacy_intent(attempted=0)
        self.assertEqual(self.service.history(self.scope)[0]['state'], 'not_sent')
        self.transport.assert_not_called()


if __name__ == '__main__':
    unittest.main()
