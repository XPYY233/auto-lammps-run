"""Dependency-capacity migration must replay sealed results, never re-extract.

Only the separately installed, reviewed native candidate exercises the migration.
All PDFs, answers, database authorities and transport calls here are synthetic.
"""
from copy import deepcopy
import importlib.util
import json
import unittest
from unittest.mock import patch

from auto_lammps.manifest import canonical
from auto_lammps.workbench_bridge import WorkbenchRecoveryBinding
import test_workbench_recovery as native_fixture


def _record_capacity_candidate_installed():
    if importlib.util.find_spec('auto_research') is None:
        return False
    from auto_research.evidence.literature_extraction_budget import MAX_EXTRACTED_RECORDS_PER_RESPONSE
    return MAX_EXTRACTED_RECORDS_PER_RESPONSE == 128


@unittest.skipUnless(_record_capacity_candidate_installed(),
    'reviewed native record-capacity candidate is optional')
class NativeRecordCapacityMigrationTests(unittest.TestCase):
    # Reuse the established native fixture without collecting its unrelated
    # test methods a second time.
    setUp = native_fixture.NativeWorkbenchRecoveryTests.setUp
    save_config = native_fixture.NativeWorkbenchRecoveryTests.save_config
    restart = native_fixture.NativeWorkbenchRecoveryTests.restart
    transport = native_fixture.NativeWorkbenchRecoveryTests.transport
    extract = native_fixture.NativeWorkbenchRecoveryTests.extract
    options = native_fixture.NativeWorkbenchRecoveryTests.options
    legacy_mapping = native_fixture.NativeWorkbenchRecoveryTests.legacy_mapping

    def test_128_combined_records_is_bounded_not_unlimited(self):
        from auto_research.evidence.literature_extraction_stages import _assert_extraction_payload_bounds
        for data_count, findings_count in ((128, 0), (0, 128), (65, 63)):
            payload = {'data': [{} for _ in range(data_count)],
                'findings': [{} for _ in range(findings_count)]}
            before = deepcopy(payload)
            _assert_extraction_payload_bounds(payload)
            self.assertEqual(payload, before)
        with self.assertRaisesRegex(ValueError, 'record limit'):
            _assert_extraction_payload_bounds({'data': [{} for _ in range(65)],
                'findings': [{} for _ in range(64)]})

    def test_capacity_migration_reuses_saved_stage_and_only_calls_later_validation(self):
        from auto_research.evidence import literature_extraction_budget as budget
        from auto_research.evidence import literature_extraction_stages as stages
        from auto_research.evidence import deepseek_extraction as extraction
        original_transport = self.client.transport

        def dense_initial(body, key, timeout):
            outgoing = json.loads(body)
            self.outgoing.append(outgoing)
            contract = json.loads(outgoing['messages'][0]['content'].rsplit('\n', 1)[-1])
            # Every row is a complete synthetic source value; no science field
            # is edited between the old failure and dependency migration.
            chunk = int(contract['scientific_adapter']['native_call_id'].split('-')[2])
            value = {'data': [{'value_text': '4.0', 'meaning': '样品硬度', 'unit': 'GPa',
                'context_explanation': '样品 C 在 400 K 条件下的测量', 'source_page': chunk,
                'source_locator': 'Results',
                'source_excerpt': 'Sample C has hardness 4.0 GPa at 400 K',
                'evidence_type': 'measured', 'source_precision': 'exact_text'} for _ in range(72)],
                'findings': [], 'pending_tasks': []}
            return 200, canonical({'choices': [{'finish_reason': 'stop', 'message': {
                'role': 'assistant', 'content': json.dumps(value)}}],
                'usage': {'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6}})

        self.client.transport = dense_initial
        with patch.object(budget, 'MAX_EXTRACTED_RECORDS_PER_RESPONSE', 64), \
                patch.object(stages, 'MAX_EXTRACTED_RECORDS_PER_RESPONSE', 64), \
                patch.object(extraction, 'MAX_EXTRACTED_RECORDS_PER_RESPONSE', 64):
            failure = self.extract()
        self.assertEqual(failure['state'], 'failed', failure)
        self.assertEqual(failure['error_code'], 'workbench_response_record_limit')
        self.assertEqual(len(failure['model_request_ids']), 12)
        self.failure = failure
        self.token = next(iter(self.bridge.runtime.jobs._jobs))
        original_calls = {i: deepcopy(self.calls.lookup(i)) for i in failure['model_request_ids']}
        native_before = self.bridge.runtime.native_recovery_reader(self.token)
        saved_results = deepcopy(native_before['execution'].completed_results)
        self.assertEqual(len(saved_results), 12)
        self.assertTrue(all(len(answer['data']) == 72 for answer in saved_results))
        self.restart()
        self.assertFalse(self.options()[0]['enabled'])
        self.bridge.recoveries[self.original_id] = WorkbenchRecoveryBinding(**self.legacy_mapping())
        options = self.options()
        self.assertTrue(options[0]['enabled'], options)
        self.assertEqual(options[0]['reused_model_calls'], 12)
        self.assertEqual(len(self.outgoing), 12)
        self.assertEqual(self.bridge.runtime.native_recovery_reader(self.token)['checkpoint'].revision,
            native_before['checkpoint'].revision)

        def later_stages_only(body, key, timeout):
            text = json.loads(body)['messages'][0]['content']
            self.assertNotIn('"stage":"initial_focus"', text)
            self.assertIn('"extraction_records_per_response":128', text)
            if '"stage":"third_review"' in text:
                # Valid empty review keeps uncertain/overflow items unresolved;
                # the capacity change must not silently approve them.
                self.outgoing.append(json.loads(body))
                return 200, canonical({'choices': [{'finish_reason': 'stop', 'message': {
                    'role': 'assistant', 'content': '{"verdicts":[]}'}}],
                    'usage': {'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6}})
            return original_transport(body, key, timeout)

        self.client.transport = later_stages_only
        validated = []
        def stop_before_publication(package):
            # This regression stops at the real native scientific gate. The
            # deliberately duplicated dense fixture is never published or
            # presented as a scientific success.
            validated.append(package)
            raise RuntimeError('Synthetic controlled stop before publication')

        with patch.object(self.bridge.runtime.ports.assembler._starter, 'start',
                side_effect=AssertionError('Must not re-extract the source')), \
                patch.object(self.bridge.runtime.ports.executor._finalizer, 'finalize',
                    side_effect=stop_before_publication):
            result = self.extract(self.resume_id, resume_of=self.original_id)
        self.assertEqual(result['state'], 'failed', result)
        self.assertEqual(result['error_code'], 'literature_commit_failed')
        self.assertEqual(len(validated), 1)
        quality = validated[0].quality_result
        self.assertTrue(any(record['gate_status'] == 'ai_unresolved' for record in quality['records']))
        self.assertEqual(self.bridge.runtime.data_items(self.bridge.runtime.database,
            self.binding.workbench_paper_id), [])
        self.assertEqual(result['reused_model_calls'], 12)
        self.assertGreater(result['new_model_calls'], 0)
        self.assertEqual(original_calls, {i: self.calls.lookup(i) for i in original_calls})
        self.assertEqual(self.bridge.history(self.task['id'])[0], failure)
        calls_after = len(self.outgoing)
        self.assertEqual(self.extract(self.resume_id, resume_of=self.original_id), result)
        self.assertEqual(len(self.outgoing), calls_after)


if __name__ == '__main__':
    unittest.main()
