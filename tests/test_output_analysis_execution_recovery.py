"""Synthetic execution recovery; no live model, HPC, or research data."""
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from auto_lammps import output_analysis
from auto_lammps.deepseek import DeepSeekClient, DeepSeekConfig, ModelCalls, ModelError, request_body
from auto_lammps.ledger import LimitExceeded
from auto_lammps.manifest import canonical, sha256
from auto_lammps.tasks import TaskError
import test_output_analysis as fixture


class OutputAnalysisExecutionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.OutputAnalysisTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.request_id = 'a' * 32

    def assert_original_run_unchanged(self, before):
        after = self.fx.ledger.get(self.fx.source)
        for field in ('state', 'actual_core_seconds', 'dispatch_claimed'):
            self.assertEqual(after[field], before[field], field)

    def assert_one_preserved_model_answer(self):
        self.assertEqual(self.fx.transport.call_count, 1)
        self.assertEqual(self.fx.calls.status()['used_requests'], 1)
        calls = self.fx.calls.history()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['request_id'], self.request_id)
        self.assertEqual(calls[0]['receipt']['structured_output'], self.fx.proposal)

    def assert_recoverable_failure(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        record = response.json()
        self.assertEqual(record['request_id'], self.request_id)
        self.assertEqual(record['state'], 'analysis_execution_failed', record)
        self.assertEqual(record['round'], 1)
        self.assertEqual(record['proposal'], self.fx.proposal)
        self.assertEqual(record['model_receipt']['structured_output'], self.fx.proposal)
        self.assertIsNone(record['result'])
        self.assertEqual(record['scientific_status'], 'not_evaluated')
        reread = self.fx.service.get(self.fx.task, self.request_id)
        self.assertTrue(reread['recoverable'])
        self.assertEqual(reread['proposal'], self.fx.proposal)
        with self.fx.tasks.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM output_analysis_receipts').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM output_analysis_failures').fetchone()[0], 1)
        different_id = self.fx.post('b' * 32)
        self.assertEqual(different_id.status_code, 422, different_id.text)
        self.assertIs(different_id.json()['request_recorded'], False)
        self.assertIsNone(self.fx.service.get(self.fx.task, 'b' * 32))
        self.assert_one_preserved_model_answer()

    def assert_original_id_recovers_without_model(self):
        original_call = deepcopy(self.fx.calls.lookup(self.request_id))
        resumed = self.fx.post(self.request_id)
        self.assertEqual(resumed.status_code, 200, resumed.text)
        record = resumed.json()
        self.assertEqual(record['state'], 'completed', record)
        self.assertEqual(record['request_id'], self.request_id)
        self.assertEqual(record['round'], 1)
        self.assertEqual(record['proposal'], self.fx.proposal)
        self.assertEqual(record['result']['scientific_status'], 'not_evaluated')
        self.assertEqual(self.fx.calls.lookup(self.request_id), original_call)
        self.assert_one_preserved_model_answer()
        with self.fx.tasks.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM output_analysis_intents').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM output_analysis_receipts').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM output_analysis_failures').fetchone()[0], 1)
        self.assertEqual(len(self.fx.service.history(self.fx.task)), 1)

    def test_renderer_oserror_preserves_full_proposal_and_original_id_recovers(self):
        before = self.fx.ledger.get(self.fx.source)
        with patch('auto_lammps.postprocess_figures.render_figures',
                   side_effect=OSError('PRIVATE_RENDERER_ERROR_CANARY')):
            failed = self.fx.post(self.request_id)
        self.assert_recoverable_failure(failed)
        self.assertNotIn('PRIVATE_RENDERER_ERROR_CANARY', failed.text)
        self.assert_original_run_unchanged(before)
        self.assert_original_id_recovers_without_model()
        self.assert_original_run_unchanged(before)

    def test_result_storage_limit_is_recoverable_not_http500_or_model_retry(self):
        before = self.fx.ledger.get(self.fx.source)
        question = '分析合成原始值并绘制完整分布'
        evidence, _ = self.fx.service._input(self.fx.task, self.fx.source, question)
        result_reservation = sha256(canonical(dict(
            request=self.request_id, input_sha256=sha256(canonical(evidence)))))
        real_reserve = self.fx.ledger.reserve_raw_export
        blocked = []

        def reserve(source_request, digest, maximum_bytes):
            if digest == result_reservation:
                blocked.append((source_request, maximum_bytes))
                raise LimitExceeded('PRIVATE_STORAGE_ERROR_CANARY')
            return real_reserve(source_request, digest, maximum_bytes)

        with patch.object(self.fx.ledger, 'reserve_raw_export', side_effect=reserve):
            failed = self.fx.post(self.request_id)
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0][0], self.fx.source)
        self.assertGreater(blocked[0][1], 0)
        self.assert_recoverable_failure(failed)
        self.assertNotIn('PRIVATE_STORAGE_ERROR_CANARY', failed.text)
        self.assert_original_run_unchanged(before)
        self.assert_original_id_recovers_without_model()
        self.assert_original_run_unchanged(before)

    def test_zero_model_budget_preflight_records_no_intent_call_or_unknown(self):
        before = self.fx.ledger.get(self.fx.source)
        calls = ModelCalls(self.fx.root / 'zero-analysis-model.sqlite',
                           DeepSeekConfig('synthetic', max_input_bytes=262144), max_requests=0)
        key_reader = Mock(side_effect=AssertionError('No credential access before budget approval'))
        client = DeepSeekClient(calls, transport=self.fx.transport, key_reader=key_reader)
        self.fx.service.connection_provider = lambda task: dict(
            client=client, enabled=True, connection_revision=1, credential_generation=1)
        self.fx.service.model_calls = calls

        response = self.fx.post(self.request_id)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIs(response.json()['request_recorded'], False)
        self.assertNotIn('unknown', response.text)
        self.assertIsNone(self.fx.service.get(self.fx.task, self.request_id))
        self.assertEqual(self.fx.service.history(self.fx.task), [])
        with self.fx.tasks.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM output_analysis_intents').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM output_analysis_receipts').fetchone()[0], 0)
        self.assertEqual(calls.status()['used_requests'], 0)
        self.assertEqual(calls.history(), [])
        self.assertEqual(self.fx.calls.status()['used_requests'], 0)
        self.fx.transport.assert_not_called()
        key_reader.assert_not_called()
        self.assertEqual(self.fx.ledger.get(self.fx.source)['charge_storage_bytes'], before['charge_storage_bytes'])
        self.assert_original_run_unchanged(before)

    def test_download_rechecks_actual_consumed_bytes_after_record_validation(self):
        completed = self.fx.post(self.request_id)
        self.assertEqual(completed.status_code, 200, completed.text)
        record = completed.json()
        self.assertEqual(record['state'], 'completed', record)
        item = next(row for row in record['result']['files'] if row['name'] == 'numeric-report.json')
        target = self.fx.service.directory / self.request_id / item['name']
        original_read = output_analysis.read_regular
        baseline = self.fx.service.download(self.fx.task, self.request_id, item['name'])
        self.assertEqual(sha256(baseline), item['sha256'])

        for damaged in (bytes([baseline[0] ^ 1]) + baseline[1:], baseline[:-1]):
            consumed = []

            def read_after_validation(path, maximum_bytes, *, private=False):
                data = original_read(path, maximum_bytes, private=private)
                # get() verifies every artifact first. Corrupt only the separate,
                # bounded read whose bytes will actually be returned to the user.
                if path == target and maximum_bytes == item['size']:
                    consumed.append(path)
                    return damaged
                return data

            with self.subTest(consumed_size=len(damaged)):
                with patch('auto_lammps.output_analysis.read_regular', side_effect=read_after_validation):
                    with self.assertRaisesRegex(TaskError, '最终下载字节'):
                        self.fx.service.download(self.fx.task, self.request_id, item['name'])
                self.assertEqual(consumed, [target])
        self.assertEqual(self.fx.service.download(self.fx.task, self.request_id, item['name']), baseline)
        self.assert_one_preserved_model_answer()

    def test_expected_model_request_hash_mismatch_never_reserves_or_sends(self):
        messages = [dict(role='system', content='Return JSON for this synthetic request only.')]
        digest = sha256(request_body(self.fx.calls.config, messages,
                                     model=self.fx.client.model, thinking=self.fx.client.thinking))
        wrong_digest = ('0' if digest[0] != '0' else '1') + digest[1:]
        key_reader = Mock(side_effect=AssertionError('No credential access for changed request bytes'))
        self.fx.client.key_reader = key_reader

        with patch.object(self.fx.calls, 'reserve', wraps=self.fx.calls.reserve) as reserve:
            with self.assertRaisesRegex(ModelError, '^model_request_changed$'):
                self.fx.client.complete_json(self.request_id, messages, expected_request_sha256=wrong_digest)
            reserve.assert_not_called()
        self.assertEqual(self.fx.calls.status()['used_requests'], 0)
        self.assertEqual(self.fx.calls.history(), [])
        self.assertIsNone(self.fx.calls.lookup(self.request_id))
        self.fx.transport.assert_not_called()
        key_reader.assert_not_called()


if __name__ == '__main__':
    unittest.main()
