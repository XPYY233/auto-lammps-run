"""Restart recovery exercises original installed ports, never paid providers.

The PDF, databases, encrypted authority and provider output are all synthetic.
The installed workbench owns the planner, stage validation, replay and commit.
"""
from copy import deepcopy
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from auto_lammps.deepseek import DeepSeekClient, DeepSeekConfig, ModelCalls
from auto_lammps.manifest import canonical, sha256
from auto_lammps.papers import PaperStore
from auto_lammps.tasks import TaskError, TaskStore
from auto_lammps.workbench_bridge import WorkbenchRecoveryBinding
from auto_lammps.workbench_runtime import WorkbenchRuntimeError, assemble_private_workbench


@unittest.skipUnless(importlib.util.find_spec('auto_research') is not None,
    'separately installed literature workbench is optional')
class NativeWorkbenchRecoveryTests(unittest.TestCase):
    def setUp(self):
        import fitz
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.storage = self.root/'workbench'; self.storage.mkdir(mode=0o700)
        self.tasks = TaskStore(self.root/'tasks.sqlite')
        self.papers = PaperStore(self.tasks)
        self.task = self.tasks.create('Synthetic P recovery', 'No physics', 'reproduction')
        paper = self.papers.add('Synthetic recovery article', '10.1234/recovery', 'Scope', 'Test')
        paper = self.papers.select(paper['id'], paper['revision'])
        self.paper = self.papers.link_task(paper['id'], paper['revision'], self.task['id'])
        self.source = self.root/'source.pdf'
        document = fitz.open()
        # Six actual page blocks produce the original runner's twelve-call
        # initial and coverage stages. No native planner behavior is mirrored.
        for number in range(1, 25):
            page = document.new_page()
            page.insert_text((72, 72), 'Results. Sample C has hardness 4.0 GPa at 400 K.')
            page.insert_text((72, 95), f'Synthetic page {number}.')
        document.save(self.source); document.close()
        self.source_sha = sha256(self.source.read_bytes())
        self.config_path = self.root/'workbench.json'
        self.config = {'version': 1, 'private_root': str(self.storage),
            'session_id': 'synthetic-stable-recovery-session', 'bindings': []}
        self.save_config()
        self.bridge, _ = assemble_private_workbench(self.papers, self.config_path)
        upstream = self.bridge.runtime.database.upsert_paper(title=self.paper['title'], doi=self.paper['doi'],
            pdf_path=str(self.source), pdf_sha256=self.source_sha)
        self.config['bindings'] = [{'paper_id': self.paper['id'], 'workbench_paper_id': upstream,
            'pdf_sha256': self.source_sha}]
        self.save_config(); self.restart()
        self.calls = ModelCalls(self.root/'calls.sqlite', DeepSeekConfig('synthetic-model',
            max_output_tokens=16000, max_input_bytes=262144), max_requests=100)
        self.outgoing = []
        self.client = DeepSeekClient(self.calls, transport=self.transport,
            key_reader=lambda: 'synthetic-project-key')
        self.original_id, self.resume_id = 'a'*32, 'b'*32

    def save_config(self):
        self.config_path.write_bytes(canonical(self.config)); self.config_path.chmod(0o600)

    def restart(self):
        self.bridge, bindings = assemble_private_workbench(self.papers, self.config_path)
        if bindings:
            self.binding = bindings[self.paper['id']]

    def transport(self, body, key, timeout):
        payload = json.loads(body); self.outgoing.append(payload)
        content = payload['messages'][1]['content']
        with self.tasks.transaction() as db:
            row = db.execute('SELECT document FROM workbench_extraction_continuations '
                'ORDER BY rowid DESC LIMIT 1').fetchone()
        token = json.loads(row['document'])['job_token'] if row else next(iter(self.bridge.runtime.jobs._jobs))
        native = self.bridge.runtime.native_stage_reader(token)
        stage = native['stage']['name']
        if 'Verify these candidates:' in content:
            candidates = json.loads(content.split('Verify these candidates:\n', 1)[1]
                .split('\n\nSource pages:', 1)[0])
            value = {'verdicts': [{'candidate_id': row['candidate_id'],
                'verdict': 'supported', 'reason': 'Exact source text.'} for row in candidates]}
        elif stage == 'coverage_gap':
            # The currently executing native call identifies its own block;
            # returning one source value is test data, not a cloned planner.
            contract = payload['messages'][0]['content']
            chunk = int(re.search(r'"native_call_id":"[ab]-chunk-(\d+)-', contract).group(1))
            value = {'data': [{'value_text': '4.0', 'meaning': '样品硬度', 'unit': 'GPa',
                'context_explanation': '样品 C 在 400 K 条件下的测量', 'source_page': chunk*4-3,
                'source_locator': 'Results', 'source_excerpt': 'Sample C has hardness 4.0 GPa at 400 K',
                'evidence_type': 'measured', 'source_precision': 'exact_text'}],
                'findings': [], 'pending_tasks': []}
        else:
            value = {'data': [], 'findings': [], 'pending_tasks': []}
        return 200, canonical({'choices': [{'finish_reason': 'stop', 'message': {
            'role': 'assistant', 'content': json.dumps(value)}}],
            'usage': {'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6}})

    def extract(self, request_id=None, **kwargs):
        return self.bridge.extract(self.task['id'], request_id or self.original_id,
            self.binding, self.client, connection_revision=1, credential_generation=1, **kwargs)

    def fail_after_coverage(self, *, legacy=False):
        from auto_research.evidence.literature_extraction_job import LiteratureExtractionJobError
        export = self.bridge.runtime.jobs.export_private_state
        def interrupted(token, **kwargs):
            raw = export(token, **kwargs)
            if self.bridge.runtime.decode_job_state(raw).stage['name'] == 'coverage_verification':
                raise LiteratureExtractionJobError('literature_job_state_invalid', 'Synthetic local persistence interruption')
            return raw
        with patch.object(self.bridge.runtime.jobs, 'export_private_state', interrupted):
            if legacy:
                with patch.object(self.bridge, '_save_continuation'):
                    failure = self.extract()
            else:
                failure = self.extract()
        self.assertEqual(failure['state'], 'failed', failure)
        self.assertEqual(len(failure['model_request_ids']), 24, failure)
        self.assertEqual(len(self.outgoing), 24)
        self.assertTrue(all(self.calls.lookup(i)['receipt']['state'] == 'completed'
            for i in failure['model_request_ids']))
        self.token = next(iter(self.bridge.runtime.jobs._jobs))
        self.failure = failure
        return failure

    def options(self, **kwargs):
        return self.bridge.recovery_options(self.task['id'], self.binding, self.client,
            connection_revision=kwargs.get('connection_revision', 1),
            credential_generation=kwargs.get('credential_generation', 1))

    def legacy_mapping(self):
        with self.tasks.transaction() as db:
            intent = json.loads(db.execute('SELECT document FROM workbench_extraction_intents '
                'WHERE id=?', (self.original_id,)).fetchone()['document'])
        proof = self.calls.lookup(self.failure['model_request_ids'][0])['receipt']['scientific_adapter']
        return {'request_id': self.original_id, 'job_token': self.token,
            'intent_sha256': sha256(canonical(intent)), 'receipt_sha256': sha256(canonical(self.failure)),
            **self.bridge._model_binding(self.client),
            'prior_native_capability_sha256': sha256(canonical(proof['native_capabilities'])),
            'native_capability_sha256': sha256(canonical(self.bridge.runtime.native_capability_reader()))}

    def test_restart_reuses_24_calls_and_native_12_prefix_without_starting_again(self):
        failure = self.fail_after_coverage()
        original_calls = {i: self.calls.lookup(i) for i in failure['model_request_ids']}
        self.restart()
        native = self.bridge.runtime.native_recovery_reader(self.token)
        self.assertEqual(native['checkpoint'].stage, 'coverage_gap')
        self.assertEqual(len(native['execution'].completed_results), 12)
        before_revision = native['checkpoint'].revision
        options = self.options()
        self.assertEqual(options[0]['enabled'], True, options)
        self.assertEqual(options[0]['reused_model_calls'], 24)
        self.assertEqual(self.bridge.runtime.jobs._jobs, {})
        self.assertEqual(self.bridge.runtime.native_recovery_reader(self.token)['checkpoint'].revision,
            before_revision)
        self.assertEqual(len(self.outgoing), 24)
        self.assertNotIn(self.token, json.dumps(options))
        self.assertNotIn(str(self.storage), json.dumps(options))
        with patch.object(self.bridge.runtime.ports.assembler._starter, 'start',
                side_effect=AssertionError('Recovery must not restart PDF extraction')):
            result = self.extract(self.resume_id, resume_of=self.original_id)
        self.assertIn(result['state'], {'completed', 'completed_with_limitations'}, result)
        self.assertEqual(result['reused_model_calls'], 24)
        self.assertGreater(result['new_model_calls'], 0)
        self.assertEqual(len(self.outgoing), 24 + result['new_model_calls'])
        self.assertTrue(set(failure['model_request_ids']).isdisjoint(result['model_request_ids']))
        self.assertEqual(original_calls, {i: self.calls.lookup(i) for i in original_calls})
        self.assertEqual(self.bridge.history(self.task['id'])[0], failure)
        self.assertEqual(self.extract(self.resume_id, resume_of=self.original_id), result)
        self.restart()
        self.assertEqual(self.extract(self.resume_id, resume_of=self.original_id), result)
        with self.assertRaises(TaskError):
            self.extract('c'*32, resume_of=self.original_id)
        self.assertEqual(len(self.outgoing), 24 + result['new_model_calls'])
        for table in ('workbench_extraction_continuations', 'workbench_extraction_recoveries'):
            with self.assertRaises(sqlite3.DatabaseError):
                with self.tasks.transaction() as db:
                    db.execute('DELETE FROM '+table)

    def test_controller_registered_legacy_job_recovers_without_reextracting(self):
        self.fail_after_coverage(legacy=True)
        self.assertFalse(self.options()[0]['enabled'])
        mapping = self.legacy_mapping()
        self.config['recoveries'] = [mapping]; self.save_config(); self.restart()
        self.assertTrue(self.options()[0]['enabled'], self.options())
        with patch.object(self.bridge.runtime.ports.assembler._starter, 'start',
                side_effect=AssertionError('Legacy recovery must not call starter')):
            result = self.extract(self.resume_id, resume_of=self.original_id)
        self.assertIn(result['state'], {'completed', 'completed_with_limitations'}, result)
        self.assertEqual(result['reused_model_calls'], 24)

    def test_legacy_mapping_tampering_fails_with_zero_new_provider_calls(self):
        self.fail_after_coverage(legacy=True)
        original = self.legacy_mapping()
        for key in ('job_token', 'intent_sha256', 'receipt_sha256', 'model_ledger_sha256',
                'model_policy_sha256', 'prior_native_capability_sha256', 'native_capability_sha256'):
            changed = {**original, key: 'different-native-job' if key == 'job_token' else 'f'*64}
            self.config['recoveries'] = [changed]; self.save_config(); self.restart()
            self.assertFalse(self.options()[0]['enabled'], key)
            with self.assertRaises(TaskError):
                self.extract(self.resume_id, resume_of=self.original_id)
            self.assertEqual(len(self.outgoing), 24, key)

    def test_ledger_call_or_result_tampering_prevents_recovery_before_any_prepare(self):
        self.fail_after_coverage(); self.restart()
        lookup = self.calls.lookup
        for field in ('native_call_digest', 'native_contract_sha256', 'adapter_source_sha256'):
            def changed(identifier):
                record = deepcopy(lookup(identifier))
                if identifier == self.failure['model_request_ids'][0]:
                    record['receipt']['scientific_adapter'][field] = 'f'*64
                return record
            with patch.object(self.calls, 'lookup', changed), patch.object(
                    self.bridge.runtime.ports.assembler, 'preflight', side_effect=AssertionError('No prepare')):
                self.assertFalse(self.options()[0]['enabled'])
                with self.assertRaises(TaskError):
                    self.extract(self.resume_id, resume_of=self.original_id)
            self.assertEqual(len(self.outgoing), 24)
        def changed_result(identifier):
            record = deepcopy(lookup(identifier))
            if identifier == self.failure['model_request_ids'][0]:
                record['receipt']['structured_output'] = {'data': [{'value': 'fabricated'}]}
            return record
        with patch.object(self.calls, 'lookup', changed_result):
            self.assertFalse(self.options()[0]['enabled'])
        self.assertEqual(self.bridge.runtime.jobs._jobs, {})
        self.assertEqual(len(self.outgoing), 24)

    def test_cross_connection_ledger_source_and_modes_cannot_resume(self):
        self.fail_after_coverage(); self.restart()
        for kwargs in ({'force_rescan': True}, {'repair_visuals': True}):
            with self.assertRaises(TaskError):
                self.extract(self.resume_id, resume_of=self.original_id, **kwargs)
        for key in ('connection_revision', 'credential_generation'):
            self.assertFalse(self.options(**{key: 2})[0]['enabled'])
            with self.assertRaises(TaskError):
                self.bridge.extract(self.task['id'], self.resume_id, self.binding, self.client,
                    resume_of=self.original_id, connection_revision=2 if key == 'connection_revision' else 1,
                    credential_generation=2 if key == 'credential_generation' else 1)
        other = DeepSeekClient(ModelCalls(self.root/'other-calls.sqlite', self.calls.config, max_requests=100),
            transport=self.transport, key_reader=lambda: 'synthetic-project-key')
        with self.assertRaises(TaskError):
            self.bridge.extract(self.task['id'], self.resume_id, self.binding, other,
                resume_of=self.original_id, connection_revision=1, credential_generation=1)
        other_task = self.tasks.create('Other P task', 'No physics', 'reproduction')
        paper = self.papers.get(self.paper['id'])
        self.papers.link_task(paper['id'], paper['revision'], other_task['id'])
        with self.assertRaises(TaskError):
            self.bridge.extract(other_task['id'], self.resume_id, self.binding, self.client,
                resume_of=self.original_id, connection_revision=1, credential_generation=1)
        changed_binding = replace(self.binding, pdf_sha256='f'*64)
        self.bridge.runtime.database.upsert_paper(title=self.paper['title'], doi=self.paper['doi'],
            pdf_path=str(self.source), pdf_sha256='f'*64)
        with self.assertRaises(TaskError):
            self.bridge.extract(self.task['id'], self.resume_id, changed_binding, self.client,
                resume_of=self.original_id, connection_revision=1, credential_generation=1)
        self.assertEqual(len(self.outgoing), 24)
        self.assertEqual(self.bridge.runtime.jobs._jobs, {})

    def test_unknown_ledger_or_missing_sealed_checkpoint_never_resumes(self):
        self.fail_after_coverage(); self.restart()
        lookup = self.calls.lookup
        def unknown(identifier):
            record = deepcopy(lookup(identifier))
            if identifier == self.failure['model_request_ids'][0]:
                record['receipt']['state'] = 'unknown'
            return record
        with patch.object(self.calls, 'lookup', unknown):
            self.assertFalse(self.options()[0]['enabled'])
            with self.assertRaises(TaskError):
                self.extract(self.resume_id, resume_of=self.original_id)
        broken = replace(self.bridge.runtime, native_recovery_reader=lambda token:
            (_ for _ in ()).throw(ValueError('sealed integrity failure')))
        self.bridge.runtime = broken
        self.assertFalse(self.options()[0]['enabled'])
        with self.assertRaises(TaskError):
            self.extract(self.resume_id, resume_of=self.original_id)
        self.assertEqual(len(self.outgoing), 24)

    def test_second_explicit_recovery_retains_failed_recovery_and_all_old_charges(self):
        from auto_research.evidence.literature_extraction_job import LiteratureExtractionJobError
        self.fail_after_coverage(); self.restart()
        export = self.bridge.runtime.jobs.export_private_state
        def fail_again(token, **kwargs):
            raw = export(token, **kwargs)
            if self.bridge.runtime.decode_job_state(raw).stage['name'] == 'coverage_verification':
                raise LiteratureExtractionJobError('literature_job_state_invalid', 'Synthetic interrupted advance')
            return raw
        with patch.object(self.bridge.runtime.jobs, 'export_private_state', fail_again):
            second = self.extract(self.resume_id, resume_of=self.original_id)
        self.assertEqual(second['state'], 'failed', second)
        self.assertEqual(second['new_model_calls'], 0)
        self.assertEqual(len(self.outgoing), 24)
        self.restart()
        options = self.options()
        self.assertFalse(options[0]['enabled'])
        self.assertTrue(options[1]['enabled'], options)
        result = self.extract('c'*32, resume_of=self.resume_id)
        self.assertIn(result['state'], {'completed', 'completed_with_limitations'}, result)
        self.assertEqual(result['reused_model_calls'], 24)
        self.assertEqual(self.bridge.history(self.task['id'])[1], second)
        self.assertEqual(len(self.outgoing), 24 + result['new_model_calls'])

    def test_free_preflight_failure_retains_new_binding_and_can_resume_same_native_job(self):
        self.fail_after_coverage(); self.restart()
        with patch.object(self.bridge.runtime.ports.assembler, 'preflight',
                side_effect=ValueError('Synthetic free validation interruption')):
            interrupted = self.extract(self.resume_id, resume_of=self.original_id)
        self.assertEqual(interrupted['state'], 'failed')
        self.assertEqual(interrupted['new_model_calls'], 0)
        self.assertEqual(len(self.outgoing), 24)
        self.restart()
        options = self.options()
        self.assertFalse(options[0]['enabled'])
        self.assertTrue(options[1]['enabled'], options)
        self.assertEqual(options[1]['reused_model_calls'], 24)
        with patch.object(self.bridge.runtime.ports.assembler._starter, 'start',
                side_effect=AssertionError('A free failure must not force a fresh extraction')):
            result = self.extract('c'*32, resume_of=self.resume_id)
        self.assertIn(result['state'], {'completed', 'completed_with_limitations'}, result)
        self.assertEqual(result['reused_model_calls'], 24)
        self.assertEqual(self.bridge.history(self.task['id'])[1], interrupted)
        self.assertEqual(len(self.outgoing), 24 + result['new_model_calls'])

    def test_live_memory_ahead_of_checkpoint_never_sends_or_restarts(self):
        self.fail_after_coverage()
        with patch.object(self.bridge.runtime.ports.assembler._starter, 'start',
                side_effect=AssertionError('Memory disagreement cannot restart extraction')):
            result = self.extract(self.resume_id, resume_of=self.original_id)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['new_model_calls'], 0)
        self.assertEqual(len(self.outgoing), 24)
        self.restart()
        options = self.options()
        self.assertFalse(options[0]['enabled'])
        self.assertTrue(options[1]['enabled'], options)
        self.assertIn(self.extract('c'*32, resume_of=self.resume_id)['state'],
            {'completed', 'completed_with_limitations'})

    def test_actual_PDF_byte_change_and_unresolved_intent_cannot_recharge_old_calls(self):
        self.fail_after_coverage(); self.restart()
        original = self.source.read_bytes()
        self.source.write_bytes(original + b'\n% changed source identity\n')
        self.assertFalse(self.options()[0]['enabled'])
        with self.assertRaises(TaskError):
            self.extract(self.resume_id, resume_of=self.original_id)
        self.assertEqual(len(self.outgoing), 24)
        self.source.write_bytes(original)
        with self.tasks.transaction() as db:
            intent = {'request_id': 'c'*32, 'source_sha256': self.source_sha}
            db.execute('INSERT INTO workbench_extraction_intents VALUES (?,?,?,?,?)',
                ('c'*32, self.task['id'], self.paper['id'], sha256(canonical(intent)), canonical(intent).decode()))
        self.assertFalse(self.options()[0]['enabled'])
        with self.assertRaises(TaskError):
            self.extract('d'*32, resume_of=self.original_id)
        self.assertEqual(len(self.outgoing), 24)

    def test_dependency_change_requires_explicit_registered_repair_identity(self):
        self.fail_after_coverage(); self.restart()
        original = self.bridge.runtime.native_capability_reader
        changed = original()
        changed['modules'][next(iter(changed['modules']))] = 'f'*64
        self.bridge.runtime = replace(self.bridge.runtime, native_capability_reader=lambda: deepcopy(changed))
        self.assertFalse(self.options()[0]['enabled'])
        registration = self.legacy_mapping()
        self.bridge.recoveries = {self.original_id: WorkbenchRecoveryBinding(**registration)}
        self.assertTrue(self.options()[0]['enabled'], self.options())
        self.assertEqual(len(self.outgoing), 24)


class RecoveryConfigValidationTests(unittest.TestCase):
    def test_legacy_registry_has_exact_private_schema_and_no_duplicate_native_jobs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve(); storage = root/'workbench'; storage.mkdir(mode=0o700)
            papers = PaperStore(TaskStore(root/'tasks.sqlite'))
            path = root/'config.json'
            item = dict(request_id='a'*32, job_token='synthetic-native-job',
                intent_sha256='b'*64, receipt_sha256='c'*64, model_ledger_sha256='d'*64,
                model_policy_sha256='e'*64, prior_native_capability_sha256='f'*64,
                native_capability_sha256='0'*64)
            for recoveries in ([{**item, 'api_key': 'forbidden'}], [item, item],
                    [item, {**item, 'request_id': 'b'*32}], [{'request_id': 'a'*32}], 'invalid'):
                path.write_bytes(canonical({'version': 1, 'private_root': str(storage),
                    'session_id': 'synthetic-session', 'bindings': [], 'recoveries': recoveries}))
                path.chmod(0o600)
                with self.assertRaises(WorkbenchRuntimeError):
                    assemble_private_workbench(papers, path)
                self.assertEqual(list(storage.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
