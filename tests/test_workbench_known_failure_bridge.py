"""Normal user-action recovery with the optional native runner and fake HTTP.

Every PDF, key, ledger, output and task is generated in a temporary directory.
These checks exercise the installed factory and mandatory scientific adapter;
they neither call a provider nor claim a scientific result.
"""
from copy import deepcopy
from dataclasses import replace
import importlib.util
import json
import re
import unittest
from unittest.mock import patch

from auto_lammps.deepseek import DeepSeekClient, DeepSeekConfig, ModelCalls
from auto_lammps.manifest import canonical, sha256
from auto_lammps.tasks import TaskError
from auto_lammps.workbench_bridge import WorkbenchRecoveryBinding
import test_workbench_recovery as fixtures


def native_available():
    try:
        return importlib.util.find_spec('auto_research.evidence.literature_known_length_recovery') is not None
    except ModuleNotFoundError:
        return False


@unittest.skipUnless(native_available(), 'optional native known-length dependency is unavailable')
class NormalKnownFailureBridgeTests(unittest.TestCase):
    def setUp(self):
        import fitz
        self.fixture = fixtures.NativeWorkbenchRecoveryTests(
            methodName='test_native_truncated_reply_is_known_invalid_not_unknown_and_never_retried')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.document = fitz.open()
        for number in range(1, 27):
            page = self.document.new_page()
            # Each page is dense enough for two whole-line fragments. The
            # complete paper stays within the existing 32-source public port.
            for row in range(33):
                page.insert_text((72, 72 + row * 18),
                    f'Results. Synthetic sample {number} has hardness 4.0 GPa at 400 K.' if row == 0 else '4.0')
        self.document.save(self.fixture.source)
        self.document.close()
        self.fixture.source_sha = sha256(self.fixture.source.read_bytes())
        upstream = self.fixture.bridge.runtime.database.upsert_paper(
            title=self.fixture.paper['title'], doi=self.fixture.paper['doi'],
            pdf_path=str(self.fixture.source), pdf_sha256=self.fixture.source_sha)
        self.fixture.config['bindings'] = [dict(paper_id=self.fixture.paper['id'],
            workbench_paper_id=upstream, pdf_sha256=self.fixture.source_sha)]
        self.fixture.save_config(); self.fixture.restart()
        self.calls = ModelCalls(self.fixture.root/'known-calls.sqlite',
            DeepSeekConfig('synthetic-model', max_output_tokens=65536, max_input_bytes=262144),
            max_requests=600)
        self.outgoing = []
        self.length_at = {39}
        self.client = DeepSeekClient(self.calls, transport=self.transport,
            key_reader=lambda: 'synthetic-project-key')
        self.original_id, self.child_id, self.later_id = 'a'*32, 'b'*32, 'c'*32

    @property
    def bridge(self):
        return self.fixture.bridge

    def transport(self, body, key, timeout):
        payload = json.loads(body); self.outgoing.append(payload)
        length = len(self.outgoing) in self.length_at
        value = {'data': [], 'findings': [], 'pending_tasks': []}
        return 200, canonical({'choices': [{'finish_reason': 'length' if length else 'stop',
            'message': {'role': 'assistant', 'content': '{"data":[' if length else json.dumps(value)}}],
            'usage': {'prompt_tokens': 4, 'completion_tokens': payload['max_tokens'] if length else 2,
                'total_tokens': 4 + (payload['max_tokens'] if length else 2)}})

    def extract(self, request_id, **kwargs):
        return self.bridge.extract(self.fixture.task['id'], request_id, self.fixture.binding,
            self.client, connection_revision=1, credential_generation=1, **kwargs)

    def options(self, **kwargs):
        return self.bridge.recovery_options(self.fixture.task['id'], self.fixture.binding,
            self.client, connection_revision=kwargs.get('connection_revision', 1),
            credential_generation=kwargs.get('credential_generation', 1))

    def continuation(self, request_id):
        with self.fixture.tasks.transaction() as db:
            return self.bridge._continuation(db, request_id)

    @staticmethod
    def checkpoint_bytes(native):
        from auto_research.evidence.literature_task_checkpoint_codec import encode_checkpoint
        return encode_checkpoint(native['checkpoint'])

    def native(self, token=None):
        return self.bridge.runtime.native_recovery_reader(token or self.parent_token)

    def begin_failure(self):
        failure = self.extract(self.original_id)
        self.assertEqual(failure['state'], 'failed', failure)
        self.assertEqual(failure['error_code'], 'workbench_model_response_invalid', failure)
        self.assertEqual(len(failure['model_request_ids']), 39)
        self.assertEqual(len(self.outgoing), 39)
        self.failure = failure
        self.parent_token = self.continuation(self.original_id)['job_token']
        parent = self.native()
        self.assertEqual(parent['checkpoint'].state, 'outcome_unknown')
        self.assertEqual(len(parent['execution'].completed_results), 38)
        self.assertEqual(len(parent['job'].chunks), 52)
        self.assertEqual(parent['job'].snapshot['page_count'], 26)
        self.assertEqual(parent['checkpoint'].manifest.max_calls, 424)
        self.parent_bytes = self.checkpoint_bytes(parent)
        self.parent_rows = {identifier: self.calls.lookup(identifier)
            for identifier in failure['model_request_ids']}
        return failure

    def assert_parent_immutable(self):
        self.assertEqual(self.checkpoint_bytes(self.native()), self.parent_bytes)
        self.assertEqual({identifier: self.calls.lookup(identifier)
            for identifier in self.failure['model_request_ids']}, self.parent_rows)

    def assert_disabled(self, request_id, **kwargs):
        option = next(row for row in self.options(**kwargs) if row['resume_of'] == request_id)
        self.assertFalse(option['enabled'], option)
        self.assertEqual(len(self.outgoing), 39)

    def test_factory_preview_is_read_only_and_preserves_full_paper_reader(self):
        self.begin_failure()
        runtime = self.bridge.runtime
        self.assertTrue(callable(runtime.native_known_failure_planner))
        self.assertTrue(callable(runtime.native_known_failure_creator))
        capabilities = runtime.native_capability_reader()
        self.assertIn('auto_research.evidence.literature_known_length_recovery', capabilities['modules'])
        from auto_research.evidence.literature_extraction_budget import MAX_EXTRACTED_RECORDS_PER_RESPONSE
        self.assertEqual(capabilities['output_limits']['extraction_records_per_response'],
                         MAX_EXTRACTED_RECORDS_PER_RESPONSE)
        source = runtime.native_paper_sources_reader(self.fixture.binding.workbench_paper_id)
        self.assertEqual(source['pdf_sha256'], self.fixture.source_sha)
        self.assertEqual(len(source['sources']), 26)
        self.assertIn('Synthetic sample 26', source['sources'][-1]['text'])
        before_files = sorted(str(path.relative_to(self.fixture.storage))
            for path in self.fixture.storage.rglob('*') if path.is_file())
        before_jobs = set(runtime.jobs._jobs)
        self.fixture.bridge.runtime = replace(runtime,
            native_known_failure_creator=lambda *args, **kwargs: self.fail('preview admission'))
        first, second = self.options(), self.options()
        self.assertEqual(first, second)
        self.assertEqual(len(first), 1)
        self.assertTrue(first[0]['enabled'], first)
        self.assertEqual(first[0]['reused_model_calls'], 38)
        self.assertEqual(first[0]['replacement_max_tokens'], 32000)
        self.assertEqual(first[0]['extra_calls'], 1)
        self.assertEqual(first[0]['extra_tokens'], 32000)
        self.assertNotIn('job_token', first[0])
        self.assertNotIn('successful_prefix', first[0])
        self.assertEqual(set(runtime.jobs._jobs), before_jobs)
        self.assertEqual(sorted(str(path.relative_to(self.fixture.storage))
            for path in self.fixture.storage.rglob('*') if path.is_file()), before_files)
        self.assertEqual(len(self.outgoing), 39)
        self.assert_parent_immutable()

    def test_normal_resume_reuses_38_answers_and_adds_one_32k_replacement(self):
        self.begin_failure()
        result = self.extract(self.child_id, resume_of=self.original_id)
        self.assertEqual(result['state'], 'no_evidence_published', result)
        self.assertEqual(result['reused_model_calls'], 38)
        self.assertEqual(result['inherited_failed_model_calls'], 1)
        self.assertEqual(result['inherited_failed_reserved_tokens'], 16000)
        self.assertFalse(result['execution_authorized'])
        self.assertEqual(result['scientific_status'], 'not_evaluated')
        self.assertEqual(self.outgoing[39]['max_tokens'], 32000)
        self.assertIn('a-chunk-39-focus-1', self.outgoing[39]['messages'][0]['content'])
        replacement = re.search(r'"native_call_id":"([^"]+)"', self.outgoing[39]['messages'][0]['content']).group(1)
        self.assertEqual(replacement, 'a-chunk-39-focus-1')
        original_digest = re.search(r'"native_call_digest":"([^"]+)"',
            self.outgoing[38]['messages'][0]['content']).group(1)
        replacement_digest = re.search(r'"native_call_digest":"([^"]+)"',
            self.outgoing[39]['messages'][0]['content']).group(1)
        self.assertNotEqual(original_digest, replacement_digest)
        self.assertTrue(all(row['max_tokens'] <= 16000 for row in self.outgoing[40:]))
        self.assertTrue(set(result['model_request_ids']).isdisjoint(self.failure['model_request_ids']))
        child_token = self.continuation(self.child_id)['job_token']
        child = self.native(child_token)
        self.assertEqual(child['checkpoint'].state, 'completed')
        self.assertEqual(child['checkpoint'].manifest.max_calls, 425)
        self.assertEqual(child['checkpoint'].manifest.max_tokens, 3828000)
        self.assertEqual(child['checkpoint'].spent_calls, 39 + len(result['model_request_ids']))
        self.assertEqual(child['checkpoint'].spent_tokens,
            624000 + sum(self.calls.lookup(i)['receipt']['max_output_tokens'] for i in result['model_request_ids']))
        self.assertEqual(child['checkpoint'].continuation_lineage.parent_task_id, self.native()['checkpoint'].manifest.task_id)
        self.assertNotEqual(child['job'].snapshot_ref, self.native()['job'].snapshot_ref)
        self.assertEqual(child['job'].snapshot, self.native()['job'].snapshot)
        self.assert_parent_immutable()
        count = len(self.outgoing)
        self.assertEqual(self.extract(self.child_id, resume_of=self.original_id), result)
        self.assertEqual(len(self.outgoing), count)
        with self.assertRaises(TaskError): self.extract(self.later_id, resume_of=self.original_id)
        self.assertEqual(len(self.outgoing), count)

    def test_source_change_and_model_generation_block_before_child_admission(self):
        self.begin_failure()
        self.fixture.source.write_bytes(self.fixture.source.read_bytes() + b'\nchanged source')
        self.assert_disabled(self.original_id)
        with self.assertRaises(TaskError): self.extract(self.child_id, resume_of=self.original_id)
        self.assert_parent_immutable()
        self.assertIsNone(self.continuation(self.child_id))

    def test_model_generation_change_blocks_read_only_options(self):
        self.begin_failure()
        self.assert_disabled(self.original_id, credential_generation=2)
        self.assert_disabled(self.original_id, connection_revision=2)
        self.assert_parent_immutable()

    def test_changed_capability_needs_exact_prior_and_current_registration(self):
        self.begin_failure()
        old = self.bridge.runtime.native_capability_reader()
        changed = deepcopy(old); changed['modules']['synthetic-reviewed-native-change'] = 'f'*64
        old_sha, new_sha = sha256(canonical(old)), sha256(canonical(changed))
        self.fixture.bridge.runtime = replace(self.bridge.runtime, native_capability_reader=lambda: deepcopy(changed))
        self.assert_disabled(self.original_id)
        with self.fixture.tasks.transaction() as db:
            intent, receipt = self.bridge._request(db, self.original_id)
        registration = dict(request_id=self.original_id, job_token=self.parent_token,
            intent_sha256=sha256(canonical(intent)), receipt_sha256=sha256(canonical(receipt)),
            **self.bridge._model_binding(self.client), prior_native_capability_sha256=old_sha,
            native_capability_sha256=new_sha)
        self.bridge.recoveries[self.original_id] = WorkbenchRecoveryBinding(**registration)
        option = next(row for row in self.options() if row['resume_of'] == self.original_id)
        self.assertTrue(option['enabled'], option)
        self.bridge.recoveries[self.original_id] = WorkbenchRecoveryBinding(
            **{**registration, 'prior_native_capability_sha256': 'd'*64})
        self.assert_disabled(self.original_id)
        self.assert_parent_immutable()

    def test_unknown_model_receipt_blocks_reconciliation(self):
        self.begin_failure()
        last = self.failure['model_request_ids'][-1]
        lookup = self.calls.lookup
        def unknown(identifier):
            row = lookup(identifier)
            if identifier == last:
                row = deepcopy(row); row['receipt']['state'] = 'unknown'
            return row
        with patch.object(self.calls, 'lookup', unknown):
            self.assert_disabled(self.original_id)
            with self.assertRaises(TaskError): self.extract(self.child_id, resume_of=self.original_id)
        self.assert_parent_immutable()

    def test_original_source_history_change_blocks_before_admission(self):
        self.begin_failure()
        read = self.bridge._request
        def changed(db, identifier):
            intent, receipt = read(db, identifier)
            if identifier == self.original_id:
                intent = deepcopy(intent); intent['title'] = 'Changed synthetic source history'
            return intent, receipt
        with patch.object(self.bridge, '_request', changed):
            self.assert_disabled(self.original_id)
            with self.assertRaises(TaskError): self.extract(self.child_id, resume_of=self.original_id)
        self.assertIsNone(self.continuation(self.child_id))
        self.assert_parent_immutable()

    def test_model_policy_amendment_blocks_original_binding(self):
        self.begin_failure()
        self.calls.amend_limits(replace(self.calls.config, max_output_tokens=64000), reason_sha256='e'*64)
        self.assert_disabled(self.original_id)
        with self.assertRaises(TaskError): self.extract(self.child_id, resume_of=self.original_id)
        self.assertIsNone(self.continuation(self.child_id))
        self.assert_parent_immutable()

    def test_source_change_after_child_admission_blocks_before_any_new_http(self):
        self.begin_failure()
        preflight = self.bridge.runtime.ports.assembler.preflight
        def changed(request):
            preflight(request)
            self.fixture.source.write_bytes(self.fixture.source.read_bytes() + b'\nsynthetic changed source')
        with patch.object(self.bridge.runtime.ports.assembler, 'preflight', changed):
            failed = self.extract(self.child_id, resume_of=self.original_id)
        self.assertEqual(failed['state'], 'failed', failed)
        self.assertEqual(failed['model_request_ids'], [])
        self.assertEqual(len(self.outgoing), 39)
        token = self.continuation(self.child_id)['job_token']
        self.assertEqual(self.native(token)['checkpoint'].state, 'authorized')
        self.assertTrue(all(not row['enabled'] for row in self.options()))
        self.assert_parent_immutable()

    def test_partial_child_live_lease_blocks_new_resume_until_native_reconciles(self):
        from auto_research.evidence.literature_extraction_job import LiteratureExtractionJobError
        self.begin_failure()
        export = self.bridge.runtime.jobs.export_private_state
        def interrupted(token, **kwargs):
            raw = export(token, **kwargs)
            if self.bridge.runtime.decode_job_state(raw).stage['name'] == 'coverage_gap':
                raise LiteratureExtractionJobError('literature_job_state_invalid', 'Synthetic local stage interruption')
            return raw
        with patch.object(self.bridge.runtime.jobs, 'export_private_state', interrupted):
            failed = self.extract(self.child_id, resume_of=self.original_id)
        self.assertEqual(failed['state'], 'failed', failed)
        self.assertEqual(len(failed['model_request_ids']), 66)
        token = self.continuation(self.child_id)['job_token']
        child = self.native(token)['checkpoint']
        self.assertEqual(child.state, 'running')
        self.assertIsNotNone(child.lease_owner_digest)
        self.assertEqual(child.spent_calls, 105)
        self.assertEqual(len([row for row in child.receipts if row.state == 'succeeded']), 104)
        child_bytes = self.checkpoint_bytes(self.native(token))
        sent = len(self.outgoing)
        self.fixture.restart()
        option = next(row for row in self.options() if row['resume_of'] == self.child_id)
        self.assertFalse(option['enabled'], option)
        with self.assertRaises(TaskError): self.extract(self.later_id, resume_of=self.child_id)
        self.assertEqual(self.extract(self.child_id, resume_of=self.original_id), failed)
        self.assertEqual(len(self.outgoing), sent)
        self.assertIsNone(self.continuation(self.later_id))
        self.assertEqual(self.checkpoint_bytes(self.native(token)), child_bytes)
        self.assertEqual(self.native(token)['checkpoint'].continuation_lineage.continuation_request_id, self.child_id)
        self.assertEqual(sum(row['max_tokens'] == 32000 for row in self.outgoing), 1)
        self.assert_parent_immutable()

    def test_second_length_is_unknown_child_and_never_automatically_retried(self):
        self.begin_failure()
        self.length_at.add(40)
        result = self.extract(self.child_id, resume_of=self.original_id)
        self.assertEqual(result['state'], 'failed', result)
        self.assertEqual(result['error_code'], 'workbench_model_response_invalid')
        self.assertEqual(len(result['model_request_ids']), 1)
        self.assertEqual(len(self.outgoing), 40)
        child_token = self.continuation(self.child_id)['job_token']
        self.assertEqual(self.native(child_token)['checkpoint'].state, 'outcome_unknown')
        self.assertTrue(all(not row['enabled'] for row in self.options()))
        self.assertEqual(self.extract(self.child_id, resume_of=self.original_id), result)
        with self.assertRaises(TaskError): self.extract(self.later_id, resume_of=self.child_id)
        self.assertEqual(len(self.outgoing), 40)
        self.assert_parent_immutable()

    def test_unpaid_preflight_failure_restores_same_child_on_restart(self):
        self.begin_failure()
        with patch.object(self.bridge.runtime.ports.assembler, 'preflight',
                side_effect=TaskError('Synthetic unpaid preflight interruption')):
            failed = self.extract(self.child_id, resume_of=self.original_id)
        self.assertEqual(failed['state'], 'failed', failed)
        self.assertEqual(failed['model_request_ids'], [])
        self.assertEqual(len(self.outgoing), 39)
        token = self.continuation(self.child_id)['job_token']
        child = self.native(token)
        self.assertEqual(child['checkpoint'].state, 'authorized')
        self.assertEqual(len(child['execution'].completed_results), 38)
        self.fixture.restart()
        option = next(row for row in self.options() if row['resume_of'] == self.child_id)
        self.assertTrue(option['enabled'], option)
        result = self.extract(self.later_id, resume_of=self.child_id)
        self.assertEqual(result['state'], 'no_evidence_published', result)
        self.assertEqual(self.continuation(self.later_id)['job_token'], token)
        self.assertEqual(self.native(token)['checkpoint'].continuation_lineage.continuation_request_id, self.child_id)
        self.assertEqual(self.outgoing[39]['max_tokens'], 32000)
        self.assert_parent_immutable()


if __name__ == '__main__':
    unittest.main()
