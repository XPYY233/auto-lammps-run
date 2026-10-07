"""Real service dispatch boundaries with synthetic outputs, never API or HPC."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

from auto_lammps import scientific_adapters as adapters
from auto_lammps.agent_candidates import CandidateError, ReviewContractError, generate_candidate_draft
from auto_lammps.condition_generation import condition_messages, generate_condition_draft
from auto_lammps.deepseek import DeepSeekClient, ModelCalls, ModelError, request_body
from auto_lammps.manifest import canonical, sha256
from auto_lammps.reference_generation import accounting_binding, reference_sources
from auto_lammps.tasks import TaskError
import test_agent_candidates as candidate_fixtures
import test_condition_generation as condition_fixtures
import test_reference_generation as reference_fixtures
import test_mandatory_adapters as stage_fixtures
from test_condition_generation import SOURCES, OUTPUT
from test_deepseek import response


class MandatoryScientificEntryTests(unittest.TestCase):
    def fixture(self, kind):
        fixture = {'candidate': candidate_fixtures.AgentCandidateTests,
                   'condition': condition_fixtures.GenerationTests,
                   'reference': reference_fixtures.ReferenceGenerationTests,
                   'stages': stage_fixtures.MandatoryAdapterTests}[kind]()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def candidate_client(self, fixture, outputs):
        calls = ModelCalls(fixture.root / 'dispatch.sqlite', fixture.calls.config, max_requests=3)
        transport = Mock(side_effect=outputs)
        client = DeepSeekClient(calls, transport=transport, key_reader=lambda: 'synthetic-key')
        return client, calls, transport

    def generate(self, fixture, client, **kwargs):
        return generate_candidate_draft(client, fixture.adapter, task_text='Synthetic complete research request',
            units='metal', resources=fixture.resources, store=fixture.root / 'dispatch-candidate', **kwargs)

    def test_all_three_legacy_entries_reject_missing_or_corrupt_stage_without_payment(self):
        for kind in ('condition', 'reference', 'candidate'):
            for missing in (None, {}, {'version': 0, 'rules': {'model_may_disable_checks': True}}):
                with self.subTest(kind=kind, missing=missing):
                    fixture = self.fixture(kind)
                    with patch.object(adapters, 'stage_context', return_value=missing):
                        with self.assertRaises((TaskError, CandidateError)):
                            fixture.generate()
                    fixture.transport.assert_not_called()
                    self.assertEqual(fixture.calls.status()['used_requests'], 0)

    def test_source_locator_missing_or_corrupt_cannot_send_or_import(self):
        for broken in (None, {}, {'tool': 'condition_source_spans', 'version': 0}):
            with self.subTest(context=broken):
                fixture = self.fixture('condition')
                with patch('auto_lammps.condition_generation.condition_evidence_context', return_value=broken):
                    with self.assertRaises(TaskError):
                        fixture.generate()
                fixture.transport.assert_not_called()
                self.assertEqual(fixture.store.get(fixture.doc['id']), fixture.doc)
                self.assertEqual(fixture.store.condition_requests(fixture.doc['id']), [])

    def test_candidate_actual_geometry_workflow_and_analysis_contracts_are_required(self):
        for tool in ('_geometry_context', 'workflow_tool_context', 'adapter_identity'):
            for broken in (None, {}, {'version': 0}):
                with self.subTest(tool=tool, broken=broken):
                    fixture = self.fixture('candidate')
                    with patch('auto_lammps.agent_candidates.' + tool, return_value=broken):
                        with self.assertRaises(CandidateError):
                            fixture.generate()
                    fixture.transport.assert_not_called()
                    self.assertEqual(fixture.calls.status()['used_requests'], 0)

    def test_missing_trusted_capabilities_never_validate_themselves_as_equal_null(self):
        from auto_lammps import candidate_tools, analysis_v2
        wrong_workflow = deepcopy(candidate_tools.workflow_tool_context())
        wrong_workflow['source_sha256'] = '0'*64
        empty_limits = deepcopy(candidate_tools.workflow_tool_context())
        empty_limits['limits'] = {}
        wrong_runtime = deepcopy(analysis_v2.site_adapter_identity())
        wrong_runtime['mixed_tables']['native_scalar'] = None
        stale_runtime = deepcopy(analysis_v2.site_adapter_identity())
        stale_runtime['mixed_tables']['source_sha256'] = '0'*64
        bad_version = deepcopy(analysis_v2.site_adapter_identity())
        bad_version['adapter_version'] = 0
        for path, values in (
                ('candidate_tools.workflow_tool_context', [None, {}, wrong_workflow, empty_limits]),
                ('analysis_v2.site_adapter_identity', [None, {}, wrong_runtime, stale_runtime, bad_version]),
                ('structures.geometry_tool_context', [None, {}]),
                ('coordination_analysis.GUIDE', [None, ''])):
            for broken in values:
                with self.subTest(path=path, broken=broken):
                    fixture = self.fixture('stages')
                    with patch('auto_lammps.'+path, return_value=broken) if path.endswith('context') or path.endswith('identity') else patch('auto_lammps.'+path, broken):
                        with self.assertRaises(TaskError):
                            fixture.condition_call()
                        with self.assertRaises(adapters.ScientificAdapterError):
                            fixture.failure_call()
                    fixture.transport.assert_not_called()
                    self.assertEqual(fixture.calls.status()['used_requests'], 0)

    def test_all_discussion_protocols_reject_missing_or_corrupt_actual_analysis_before_payment(self):
        from auto_lammps import analysis_v2
        stale = deepcopy(analysis_v2.site_adapter_identity())
        stale['site_thermodynamics']['source_sha256'] = '0'*64
        missing_nested = deepcopy(analysis_v2.site_adapter_identity())
        missing_nested['mixed_tables']['numeric_tables'] = None
        for provider in ('deepseek-official', 'glm', 'openai', 'anthropic'):
            for broken in (None, {}, stale, missing_nested):
                with self.subTest(provider=provider, broken=broken):
                    fixture = self.fixture('stages')
                    connections, transport = fixture.connections()
                    connections.save(provider, 'synthetic-model', 'synthetic-key')
                    with patch('auto_lammps.analysis_v2.site_adapter_identity', return_value=broken):
                        with self.assertRaises(TaskError):
                            connections.discuss(fixture.task['id'], 'd'*32, provider,
                                                'Synthetic permitted discussion', {'synthetic': True})
                    transport.assert_not_called()
                    self.assertEqual(fixture.calls.status()['used_requests'], 0)
                    self.assertEqual(connections.history(fixture.task['id']), [])

    def test_valid_generation_and_validation_revision_both_receive_checked_contracts(self):
        fixture = self.fixture('candidate')
        bad = deepcopy(fixture.value)
        bad['workflow'] = 'run 0\nwrite_data /output/undeclared.data'
        client, calls, transport = self.candidate_client(fixture,
            [(200, response(bad)), (200, response(fixture.value))])
        reserved = Mock()
        result = self.generate(fixture, client, before_proposal_request=reserved)
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(reserved.call_count, 2)
        self.assertEqual(calls.status()['used_requests'], 2)
        for sent in transport.call_args_list:
            messages = json.loads(sent.args[0])['messages']
            payload = json.loads(messages[1]['content'])
            contract = payload['scientific_adapter']
            self.assertEqual(contract['stage'], 'candidate_proposal')
            self.assertFalse(contract['rules']['model_may_disable_checks'])
            self.assertEqual(set(contract['capabilities']),
                {'geometry_adapter', 'workflow_adapter', 'analysis_adapter'})
            self.assertIn(adapters.MANDATORY_INSTRUCTION, messages[0]['content'])
        self.assertEqual(result['request_id'], sha256(canonical(result['generation']['input']))[:32])

    def test_invalid_actual_revision_contract_stops_before_new_call_or_round(self):
        for kind in ('validation', 'json'):
            with self.subTest(kind=kind):
                fixture = self.fixture('candidate')
                bad = deepcopy(fixture.value)
                bad['workflow'] = 'run 0\nwrite_data /output/undeclared.data'
                captured = []
                original = adapters.prepare_stage_messages
                def prepare(*args):
                    result = original(*args)
                    if args[0] == 'candidate_proposal': captured.append(result[0])
                    return result
                def first_response(*args):
                    messages = captured[0]
                    payload = json.loads(messages[1]['content'])
                    payload['geometry_adapter'] = {}
                    messages[1]['content'] = canonical(payload).decode()
                    if kind == 'json':
                        return 200, response(choices=[{'finish_reason': 'stop',
                            'message': {'role': 'assistant', 'content': 'invalid JSON'}}])
                    return 200, response(bad)
                client, calls, transport = self.candidate_client(fixture, first_response)
                reserved = Mock()
                with patch.object(adapters, 'prepare_stage_messages', side_effect=prepare):
                    with self.assertRaisesRegex(CandidateError, 'geometry_adapter'):
                        self.generate(fixture, client, before_proposal_request=reserved)
                self.assertEqual(transport.call_count, 1)
                self.assertEqual(calls.status()['used_requests'], 1)
                self.assertEqual(reserved.call_count, 1)

    def test_condition_repair_rechecks_actual_source_contract_before_payment(self):
        fixture = self.fixture('condition')
        messages = condition_messages(SOURCES)
        bad = deepcopy(OUTPUT)
        bad['conditions'][0]['quote'] = 'not in the actual source'
        def first_response(*args):
            payload = json.loads(messages[1]['content'])
            payload['source_locator_adapter'] = None
            messages[1]['content'] = canonical(payload).decode()
            return 200, response(bad)
        fixture.transport.side_effect = first_response
        with patch('auto_lammps.condition_generation.condition_messages', return_value=messages):
            with self.assertRaisesRegex(TaskError, 'source_locator_adapter'):
                fixture.generate()
        self.assertEqual(fixture.transport.call_count, 1)
        self.assertEqual(fixture.calls.status()['used_requests'], 1)
        self.assertEqual(fixture.store.get(fixture.doc['id']), fixture.doc)

    def test_corrupt_review_contract_retains_proposal_without_review_or_regeneration(self):
        fixture = self.fixture('candidate')
        proposal = deepcopy(fixture.value)
        proposal['analysis']['files'] = ['result.dat']
        proposal['analysis']['plan']['tables'][0]['file'] = 'result.dat'
        proposal['analysis']['plan']['operations'][0]['file'] = 'result.dat'
        proposal['workflow'] = 'run 0\nemit_table result.dat "0 $(pe)"'
        client, calls, transport = self.candidate_client(fixture, [(200, response(proposal))])
        original = adapters.prepare_stage_messages
        def prepare(stage, *args):
            result = original(stage, *args)
            if stage == 'candidate_review':
                payload = json.loads(result[0][1]['content'])
                payload['analysis_adapter'] = None
                result[0][1]['content'] = canonical(payload).decode()
            return result
        saved, reserved = [], Mock()
        with patch.object(adapters, 'prepare_stage_messages', side_effect=prepare):
            with self.assertRaisesRegex(ReviewContractError, 'analysis_adapter'):
                self.generate(fixture, client, review_plan=True, require_analysis_plan=True,
                              on_proposal=saved.append, before_proposal_request=reserved)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(calls.status()['used_requests'], 1)
        self.assertEqual(reserved.call_count, 1)
        self.assertEqual(len(saved), 1)
        self.assertFalse((fixture.root / 'dispatch-candidate').exists())

    def seed_legacy_reference(self, fixture, state):
        sources, exports = reference_sources([fixture.csv])
        messages = [{'role': 'system', 'content': 'Legacy synthetic quoted evidence JSON'},
                    {'role': 'user', 'content': canonical({'sources': sources}).decode()}]
        context = {'version': 1, 'task_id': fixture.doc['id'], 'sources': sources, 'exports': exports,
                   'request_sha256': sha256(request_body(fixture.calls.config, messages)),
                   'accounting_sha256': accounting_binding(fixture.client)}
        operation = sha256(canonical(context)); identifier = operation[:32]
        fixture.store.save_reference_intent(fixture.doc['id'], fixture.doc['revision'], identifier, context, operation)
        if state == 'unreserved':
            return identifier
        if state == 'unknown': fixture.transport.side_effect = TimeoutError('synthetic unknown request')
        try:
            completion = fixture.client.complete_json(identifier, messages)
        except ModelError:
            self.assertEqual(state, 'unknown')
        else:
            if state == 'imported':
                fixture.store.import_generated_reference(fixture.doc['id'], fixture.doc['revision'],
                                                        identifier, context, operation, completion)
        return identifier

    def test_new_reference_contract_preserves_legacy_success_interruption_and_unknown_identity(self):
        for state in ('imported', 'interrupted', 'unknown'):
            with self.subTest(state=state):
                fixture = self.fixture('reference')
                identifier = self.seed_legacy_reference(fixture, state)
                before = fixture.calls.history()
                fixture.client.key_reader = Mock(side_effect=AssertionError('Recovery must not read a key'))
                if state == 'unknown':
                    with self.assertRaisesRegex(ModelError, 'requires_attention'):
                        fixture.generate()
                else:
                    result = fixture.generate()
                    self.assertEqual(list(result['reference_batches']), [identifier])
                self.assertEqual(fixture.calls.history(), before)
                self.assertEqual(fixture.transport.call_count, 1)
                fixture.client.key_reader.assert_not_called()
                self.assertEqual(len(fixture.store.reference_requests(fixture.doc['id'])), 1)

    def test_reference_dispatch_has_author_original_workflow_boundary_and_literal_output_check(self):
        fixture = self.fixture('reference')
        result = fixture.generate()
        messages = json.loads(fixture.transport.call_args.args[0])['messages']
        payload = json.loads(messages[1]['content'])
        context = payload['scientific_adapter']
        self.assertEqual(context['stage'], 'reference_extraction')
        self.assertIn('original_author_files_workflow_and_parameters_only', context['rules']['author_reference'])
        self.assertFalse(context['rules']['model_may_disable_checks'])
        self.assertEqual(context['capabilities']['source_quotes']['matching'], 'literal_contiguous_quote')
        self.assertFalse(next(iter(result['reference_batches'].values()))['reference_qualified'])

    def test_unreserved_current_intent_resumes_first_dispatch_under_the_same_identity(self):
        fixture = self.fixture('reference')
        save = fixture.store.save_reference_intent
        def save_then_interrupt(*args):
            save(*args)
            raise OSError('Synthetic crash after persisted intent, before reservation')
        with patch.object(fixture.store, 'save_reference_intent', side_effect=save_then_interrupt):
            with self.assertRaises(OSError):
                fixture.generate()
        fixture.transport.assert_not_called()
        self.assertEqual(fixture.calls.status()['used_requests'], 0)
        intent = fixture.store.reference_requests(fixture.doc['id'])[0]
        identifier = intent['request_id']
        before = fixture.store.reference_intent(fixture.doc['id'], identifier)
        self.assertIsNone(fixture.calls.lookup(identifier))
        result = fixture.generate()
        self.assertEqual(list(result['reference_batches']), [identifier])
        self.assertEqual(fixture.store.reference_intent(fixture.doc['id'], identifier), before)
        fixture.transport.assert_called_once()
        self.assertEqual(fixture.calls.status()['used_requests'], 1)
        self.assertEqual(fixture.generate(), result)
        fixture.transport.assert_called_once()

    def test_unreserved_legacy_contract_change_keeps_original_binding_and_cannot_create_new_paid_identity(self):
        fixture = self.fixture('reference')
        identifier = self.seed_legacy_reference(fixture, 'unreserved')
        before = fixture.store.reference_intent(fixture.doc['id'], identifier)
        with self.assertRaisesRegex(ModelError, 'unreserved_adapter_context_changed'):
            fixture.generate()
        self.assertEqual(fixture.store.reference_intent(fixture.doc['id'], identifier), before)
        self.assertEqual(len(fixture.store.reference_requests(fixture.doc['id'])), 1)
        self.assertIsNone(fixture.calls.lookup(identifier))
        fixture.transport.assert_not_called()
        self.assertEqual(fixture.calls.status()['used_requests'], 0)


if __name__ == '__main__':
    unittest.main()
