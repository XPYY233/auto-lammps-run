"""Coverage-stage length proofs preserve earlier paid stages, offline only.

The separately installed workbench owns all planning and checkpoint encoding.
Temporary synthetic PDFs and fake HTTP exercise the real bridge; no project
paper, provider credential, scientific database or simulation is opened here.
"""
from copy import deepcopy
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from auto_lammps.deepseek import DeepSeekClient, DeepSeekConfig, ModelCalls
from auto_lammps.manifest import canonical, sha256
from auto_lammps.tasks import TaskError
from auto_lammps.workbench_known_failure import _evidence
import test_workbench_known_failure_bridge as known_fixtures
import test_workbench_recovery as fixtures


def coverage_native_available():
    if not known_fixtures.native_available():
        return False
    from auto_research.evidence.literature_task_checkpoint import MAX_CALLS
    return MAX_CALLS == 513  # Only reviewed continuation, ordinary cap stays 512.


@unittest.skipUnless(known_fixtures.native_available(), 'optional native dependency is unavailable')
class CoverageLengthEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.NativeWorkbenchRecoveryTests(
            methodName='test_native_truncated_reply_is_known_invalid_not_unknown_and_never_retried')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.calls = ModelCalls(self.fixture.root/'coverage-calls.sqlite',
            DeepSeekConfig('synthetic-model', max_output_tokens=65536, max_input_bytes=262144),
            max_requests=600)
        self.outgoing = []
        self.length_at = {15}  # All twelve initial calls, two coverage successes.
        self.client = DeepSeekClient(self.calls, transport=self.transport,
            key_reader=lambda: 'synthetic-project-key')
        self.original_id, self.child_id = 'a'*32, 'b'*32

    @property
    def bridge(self):
        return self.fixture.bridge

    def transport(self, body, key, timeout):
        payload = json.loads(body)
        self.outgoing.append(payload)
        length = len(self.outgoing) in self.length_at
        value = {'data': [], 'findings': [], 'pending_tasks': []}
        return 200, canonical({'choices': [{'finish_reason': 'length' if length else 'stop',
            'message': {'role': 'assistant', 'content': '{"data":[' if length else json.dumps(value)}}],
            'usage': {'prompt_tokens': 4, 'completion_tokens': payload['max_tokens'] if length else 2,
                'total_tokens': 4 + (payload['max_tokens'] if length else 2)}})

    def extract(self, request_id, **kwargs):
        return self.bridge.extract(self.fixture.task['id'], request_id, self.fixture.binding,
            self.client, connection_revision=1, credential_generation=1, **kwargs)

    def evidence(self):
        return _evidence(self.bridge, self.fixture.task['id'], self.original_id,
            self.fixture.binding, self.client, connection_revision=1, credential_generation=1,
            now=__import__('time').time())

    def begin_failure(self):
        result = self.extract(self.original_id)
        self.assertEqual(result['state'], 'failed', result)
        self.assertEqual(result['error_code'], 'workbench_model_response_invalid', result)
        self.assertEqual(len(result['model_request_ids']), 15)
        self.failure = result
        with self.fixture.tasks.transaction() as db:
            self.token = self.bridge._continuation(db, self.original_id)['job_token']
        native = self.bridge.runtime.native_recovery_reader(self.token)
        self.assertEqual(native['checkpoint'].stage, 'coverage_gap')
        self.assertEqual(native['execution'].receipt_offset, 12)
        self.assertEqual(len(native['execution'].completed_results), 2)
        self.parent_bytes = known_fixtures.NormalKnownFailureBridgeTests.checkpoint_bytes(native)
        self.parent_rows = {key: self.calls.lookup(key) for key in result['model_request_ids']}
        return result

    def assert_unchanged(self):
        native = self.bridge.runtime.native_recovery_reader(self.token)
        self.assertEqual(known_fixtures.NormalKnownFailureBridgeTests.checkpoint_bytes(native), self.parent_bytes)
        self.assertEqual({key: self.calls.lookup(key) for key in self.failure['model_request_ids']}, self.parent_rows)
        self.assertEqual(len(self.outgoing), 15)

    def test_exact_global_receipts_and_local_coverage_prefix_are_distinct(self):
        self.begin_failure()
        context, native = self.evidence()
        self.assertEqual(context.recovery_metadata['reused_model_calls'], 14)
        self.assertEqual(context.recovery_metadata['prefix_count'], 2)
        self.assertEqual(context.recovery_metadata['inherited_failed_tokens'], 12000)
        self.assertEqual(context.proof_kwargs['max_output_tokens'], 12000)
        self.assertEqual(len(context.successful_prefix), 2)
        self.assertEqual(len(context.model_request_ids), 15)
        self.assertEqual(native['checkpoint'].spent_calls, 15)
        self.assert_unchanged()

    def test_prior_stage_source_proof_and_each_paid_digest_are_required(self):
        self.begin_failure()
        original = self.calls.lookup
        for index, key, value in ((0, 'stage_fingerprint', 'd'*64),
                (0, 'native_call_digest', 'd'*64), (12, 'native_call_digest', 'd'*64),
                (14, 'native_call_digest', 'd'*64)):
            with self.subTest(index=index, key=key):
                def changed(identifier):
                    record = deepcopy(original(identifier))
                    if identifier == self.failure['model_request_ids'][index]:
                        record['receipt']['scientific_adapter'][key] = value
                    return record
                with patch.object(self.calls, 'lookup', changed):
                    with self.assertRaises(TaskError):
                        self.evidence()
        self.assert_unchanged()

    def test_wrong_offset_and_outcome_unknown_are_never_reinterpreted(self):
        self.begin_failure()
        read = self.bridge.runtime.native_recovery_reader
        for offset in (0, 11, 13):
            with self.subTest(offset=offset):
                def changed(token):
                    native = read(token)
                    return {**native, 'execution': replace(native['execution'], receipt_offset=offset)}
                self.bridge.runtime = replace(self.bridge.runtime, native_recovery_reader=changed)
                with self.assertRaises(TaskError):
                    self.evidence()
                self.bridge.runtime = replace(self.bridge.runtime, native_recovery_reader=read)
        lookup = self.calls.lookup
        def unknown(identifier):
            record = deepcopy(lookup(identifier))
            if identifier == self.failure['model_request_ids'][-1]:
                record['receipt'].update(state='unknown', finish_reason=None, http_status=None)
            return record
        with patch.object(self.calls, 'lookup', unknown):
            with self.assertRaises(TaskError):
                self.evidence()
        self.assert_unchanged()

    @unittest.skipUnless(coverage_native_available(), 'optional coverage continuation dependency is unavailable')
    def test_normal_action_reuses_all_prior_stages_and_accounts_the_12k_failure(self):
        self.begin_failure()
        parent = self.bridge.runtime.native_recovery_reader(self.token)
        options = self.bridge.recovery_options(self.fixture.task['id'], self.fixture.binding,
            self.client, connection_revision=1, credential_generation=1)
        self.assertTrue(options[0]['enabled'], options)
        self.assertEqual(options[0]['reused_model_calls'], 14)
        with patch.object(self.bridge.runtime.ports.assembler._starter, 'start',
                side_effect=AssertionError('Recovery must not restart the PDF scan')):
            result = self.extract(self.child_id, resume_of=self.original_id)
        self.assertEqual(result['state'], 'no_evidence_published', result)
        self.assertEqual(result['reused_model_calls'], 14)
        self.assertEqual(result['inherited_failed_reserved_tokens'], 12000)
        self.assertEqual(self.outgoing[15]['max_tokens'], 32000)
        self.assertGreater(len(result['model_request_ids']), 1)
        self.assertTrue(set(result['model_request_ids']).isdisjoint(self.failure['model_request_ids']))
        with self.fixture.tasks.transaction() as db:
            child_token = self.bridge._continuation(db, self.child_id)['job_token']
        child = self.bridge.runtime.native_recovery_reader(child_token)
        self.assertEqual(child['checkpoint'].state, 'completed')
        self.assertEqual(child['checkpoint'].receipts[:14], parent['checkpoint'].receipts[:-1])
        self.assertEqual(child['checkpoint'].spent_calls, 15 + len(result['model_request_ids']))
        self.assertEqual(child['checkpoint'].continuation_lineage.failed_receipt.max_tokens, 12000)
        self.assertEqual(len(child['checkpoint'].continuation_lineage.prefix_result_digests), 2)
        # Earlier failure and its ciphertext stay unchanged after child success.
        current = self.bridge.runtime.native_recovery_reader(self.token)
        self.assertEqual(known_fixtures.NormalKnownFailureBridgeTests.checkpoint_bytes(current), self.parent_bytes)
        self.assertEqual({key: self.calls.lookup(key) for key in self.failure['model_request_ids']}, self.parent_rows)

    @unittest.skipUnless(coverage_native_available(), 'optional coverage continuation dependency is unavailable')
    def test_unpaid_child_interruption_keeps_global_prefix_and_same_child_identity(self):
        self.begin_failure()
        with patch.object(self.bridge.runtime.ports.assembler, 'preflight',
                side_effect=TaskError('Synthetic unpaid interruption')):
            interrupted = self.extract(self.child_id, resume_of=self.original_id)
        self.assertEqual(interrupted['state'], 'failed')
        self.assertEqual(interrupted['model_request_ids'], [])
        self.assertEqual(len(self.outgoing), 15)
        with self.fixture.tasks.transaction() as db:
            child_token = self.bridge._continuation(db, self.child_id)['job_token']
        self.fixture.restart()
        options = self.bridge.recovery_options(self.fixture.task['id'], self.fixture.binding,
            self.client, connection_revision=1, credential_generation=1)
        option = next(row for row in options if row['resume_of'] == self.child_id)
        self.assertTrue(option['enabled'], option)
        self.assertEqual(option['reused_model_calls'], 14)
        result = self.extract('c'*32, resume_of=self.child_id)
        self.assertEqual(result['state'], 'no_evidence_published', result)
        self.assertEqual(result['inherited_failed_reserved_tokens'], 12000)
        with self.fixture.tasks.transaction() as db:
            self.assertEqual(self.bridge._continuation(db, 'c'*32)['job_token'], child_token)

    def admit_unpaid_child_near_parent_expiry(self):
        self.begin_failure()
        parent = self.bridge.runtime.native_recovery_reader(self.token)
        issued = parent['checkpoint'].manifest.expires_at - 100
        with patch('auto_lammps.workbench_known_failure.time.time', return_value=issued):
            with patch.object(self.bridge.runtime.ports.assembler, 'preflight',
                    side_effect=TaskError('Synthetic unpaid interruption')):
                result = self.extract(self.child_id, resume_of=self.original_id)
        self.assertEqual(result['state'], 'failed', result)
        self.assertEqual(result['model_request_ids'], [])
        with self.fixture.tasks.transaction() as db:
            token = self.bridge._continuation(db, self.child_id)['job_token']
        child = self.bridge.runtime.native_recovery_reader(token)
        self.assertEqual(child['checkpoint'].manifest.issued_at, issued)
        self.assertEqual(child['job'].issued_at, issued)
        lifetime = min(86400,
            parent['checkpoint'].manifest.expires_at - parent['checkpoint'].manifest.issued_at,
            int(parent['job'].expires_at - parent['job'].issued_at))
        self.assertEqual(child['checkpoint'].manifest.expires_at, issued + lifetime)
        self.assertEqual(child['job'].expires_at, issued + lifetime)
        self.assertGreater(issued + lifetime, parent['checkpoint'].manifest.expires_at)
        self.assert_unchanged()
        return parent, child, token

    @unittest.skipUnless(coverage_native_available(), 'optional coverage continuation dependency is unavailable')
    def test_expired_parent_only_reproves_and_resumes_the_existing_sealed_child(self):
        parent, child, token = self.admit_unpaid_child_near_parent_expiry()
        self.fixture.restart()
        future = parent['checkpoint'].manifest.expires_at + 10
        with patch('auto_lammps.workbench_known_failure.time.time', return_value=future):
            options = self.bridge.recovery_options(self.fixture.task['id'], self.fixture.binding,
                self.client, connection_revision=1, credential_generation=1)
            self.assertTrue(next(row for row in options if row['resume_of'] == self.child_id)['enabled'], options)
            with patch.object(self.bridge.runtime.ports.assembler._starter, 'start',
                    side_effect=AssertionError('An expired parent must not create a fresh scan')):
                result = self.extract('c'*32, resume_of=self.child_id)
        self.assertEqual(result['state'], 'no_evidence_published', result)
        self.assertEqual(result['reused_model_calls'], 14)
        self.assertEqual(result['inherited_failed_reserved_tokens'], 12000)
        with self.fixture.tasks.transaction() as db:
            self.assertEqual(self.bridge._continuation(db, 'c'*32)['job_token'], token)
        saved = self.bridge.runtime.native_recovery_reader(token)
        self.assertEqual(saved['checkpoint'].manifest.issued_at, child['checkpoint'].manifest.issued_at)
        self.assertEqual(saved['checkpoint'].manifest.expires_at, child['checkpoint'].manifest.expires_at)
        self.assertEqual(known_fixtures.NormalKnownFailureBridgeTests.checkpoint_bytes(
            self.bridge.runtime.native_recovery_reader(self.token)), self.parent_bytes)

    @unittest.skipUnless(coverage_native_available(), 'optional coverage continuation dependency is unavailable')
    def test_expired_parent_without_admitted_child_is_not_renewable(self):
        self.begin_failure()
        parent = self.bridge.runtime.native_recovery_reader(self.token)
        with patch('auto_lammps.workbench_known_failure.time.time',
                return_value=parent['checkpoint'].manifest.expires_at + 1):
            with self.assertRaises(TaskError):
                self.extract(self.child_id, resume_of=self.original_id)
        self.assert_unchanged()
        with self.fixture.tasks.transaction() as db:
            self.assertIsNone(self.bridge._continuation(db, self.child_id))

    @unittest.skipUnless(coverage_native_available(), 'optional coverage continuation dependency is unavailable')
    def test_claim_without_sealed_child_cannot_reprove_expired_parent(self):
        self.begin_failure()
        runtime = self.bridge.runtime
        self.bridge.runtime = replace(runtime, native_known_failure_creator=lambda *a, **k:
            (_ for _ in ()).throw(TaskError('Synthetic failure before child admission')))
        result = self.extract(self.child_id, resume_of=self.original_id)
        self.assertEqual(result['state'], 'failed', result)
        self.assertEqual(result['model_request_ids'], [])
        self.bridge.runtime = runtime
        parent = runtime.native_recovery_reader(self.token)
        from auto_lammps.workbench_known_failure import known_failure_context
        with self.assertRaises(TaskError):
            known_failure_context(self.bridge, self.fixture.task['id'], self.original_id,
                self.fixture.binding, self.client, connection_revision=1, credential_generation=1,
                continuation_request_id=self.child_id, now=parent['checkpoint'].manifest.expires_at + 1)
        self.assert_unchanged()

    @unittest.skipUnless(coverage_native_available(), 'optional coverage continuation dependency is unavailable')
    def test_expired_parent_requires_exact_child_lineage_and_live_child_window(self):
        parent, child, token = self.admit_unpaid_child_near_parent_expiry()
        from auto_lammps.workbench_known_failure import known_failure_context
        runtime = self.bridge.runtime
        checkpoint = child['checkpoint']
        future = parent['checkpoint'].manifest.expires_at + 10
        mutations = (
            replace(checkpoint, continuation_lineage=replace(checkpoint.continuation_lineage,
                parent_checkpoint_digest='d'*64)),
            replace(checkpoint, continuation_lineage=replace(checkpoint.continuation_lineage,
                parent_job_state_digest='d'*64)),
            replace(checkpoint, continuation_lineage=replace(checkpoint.continuation_lineage,
                continuation_request_id='d'*32)),
            replace(checkpoint, manifest=replace(checkpoint.manifest,
                issued_at=parent['checkpoint'].manifest.expires_at)),
            replace(checkpoint, manifest=replace(checkpoint.manifest, expires_at=future)),
        )
        for changed in mutations:
            with self.subTest(changed=changed.manifest.issued_at):
                self.bridge.runtime = replace(runtime, native_recovery_reader=lambda value:
                    {**child, 'checkpoint': changed} if value == token else runtime.native_recovery_reader(value))
                with self.assertRaises(TaskError):
                    known_failure_context(self.bridge, self.fixture.task['id'], self.original_id,
                        self.fixture.binding, self.client, connection_revision=1, credential_generation=1,
                        continuation_request_id=self.child_id, now=future)
        self.bridge.runtime = runtime
        self.assert_unchanged()

    @unittest.skipUnless(coverage_native_available(), 'optional coverage continuation dependency is unavailable')
    def test_maximum_fragment_parent_uses_only_authenticated_513th_slot(self):
        import fitz
        document = fitz.open()
        # Thirty-one dense two-fragment pages plus one short page: the native
        # source partitioner, not this test, still decides the exact chunks.
        for number in range(1, 33):
            page = document.new_page()
            for row in range(33 if number < 32 else 1):
                page.insert_text((72, 72 + row*18),
                    f'Results. Synthetic sample {number} has hardness 4.0 GPa at 400 K.'
                    if row == 0 else '4.0')
        document.save(self.fixture.source)
        document.close()
        self.fixture.source_sha = sha256(self.fixture.source.read_bytes())
        paper = self.fixture.bridge.runtime.database.upsert_paper(
            title=self.fixture.paper['title'], doi=self.fixture.paper['doi'],
            pdf_path=str(self.fixture.source), pdf_sha256=self.fixture.source_sha)
        self.fixture.config['bindings'] = [dict(paper_id=self.fixture.paper['id'],
            workbench_paper_id=paper, pdf_sha256=self.fixture.source_sha)]
        self.fixture.save_config()
        self.fixture.restart()
        self.length_at = {156}
        result = self.extract(self.original_id)
        self.assertEqual(result['state'], 'failed', result)
        self.assertEqual(len(result['model_request_ids']), 156)
        with self.fixture.tasks.transaction() as db:
            token = self.bridge._continuation(db, self.original_id)['job_token']
        parent = self.bridge.runtime.native_recovery_reader(token)
        self.assertEqual(len(parent['job'].chunks), 63)
        self.assertEqual(parent['checkpoint'].manifest.max_calls, 512)
        self.assertEqual(parent['execution'].receipt_offset, 126)
        self.assertEqual(len(parent['execution'].completed_results), 29)
        from auto_lammps.workbench_known_failure import known_failure_context
        context = known_failure_context(self.bridge, self.fixture.task['id'],
            self.original_id, self.fixture.binding, self.client,
            connection_revision=1, credential_generation=1)
        self.assertEqual(context.recovery_metadata['reused_model_calls'], 155)
        self.assertEqual(context.recovery_metadata['prefix_count'], 29)
        self.assertEqual(context.recovery_metadata['max_calls'], 513)
        self.assertEqual(context.recovery_metadata['max_tokens'], 4620000)
        self.assertEqual(context.proof_kwargs['max_output_tokens'], 12000)
        self.assertEqual(len(self.outgoing), 156)  # Preview never dispatches.
        from auto_research.evidence.literature_extraction_job import LiteratureExtractionJobError
        export = self.bridge.runtime.jobs.export_private_state
        def next_stage(token, **kwargs):
            raw = export(token, **kwargs)
            if self.bridge.runtime.decode_job_state(raw).stage['name'] == 'coverage_verification':
                raise LiteratureExtractionJobError('literature_job_state_invalid',
                    'Synthetic stop after the complete restored coverage stage')
            return raw
        with patch.object(self.bridge.runtime.ports.assembler._starter, 'start',
                side_effect=AssertionError('Recovery must not restart the source scan')):
            with patch.object(self.bridge.runtime.jobs, 'export_private_state', next_stage):
                recovered = self.extract(self.child_id, resume_of=self.original_id)
        self.assertEqual(recovered['state'], 'failed', recovered)
        self.assertEqual(recovered['reused_model_calls'], 155)
        self.assertEqual(recovered['inherited_failed_reserved_tokens'], 12000)
        self.assertEqual(recovered['new_model_calls'], 97)
        self.assertEqual(len(self.outgoing), 253)
        self.assertEqual(self.outgoing[156]['max_tokens'], 32000)
        with self.fixture.tasks.transaction() as db:
            child_token = self.bridge._continuation(db, self.child_id)['job_token']
        child = self.bridge.runtime.native_recovery_reader(child_token)
        self.assertEqual(child['checkpoint'].manifest.max_calls, 513)
        self.assertEqual(child['checkpoint'].spent_calls, 253)
        self.assertEqual(child['checkpoint'].receipts[:155], parent['checkpoint'].receipts[:-1])
        self.assertEqual(child['checkpoint'].stage, 'coverage_gap')
        self.assertEqual(len(child['execution'].completed_results), 126)
        self.assertEqual(child['checkpoint'].continuation_lineage.failed_receipt.max_tokens, 12000)


if __name__ == '__main__':
    unittest.main()
