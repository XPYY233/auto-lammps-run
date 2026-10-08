"""Planning policy through actual mock dispatches; no model or physics execution."""
from copy import deepcopy
import json
import math
import unittest
from unittest.mock import Mock, patch

import test_agent_candidates as candidate_fixture
import test_fixed_geometry_candidate as fixed_fixture
from test_deepseek import response

from auto_lammps.agent_candidates import (CandidateError, PlanIterationLimit,
    generate_candidate_draft)
from auto_lammps.authorization import candidate_check
from auto_lammps.deepseek import DeepSeekClient, ModelCalls
from auto_lammps.manifest import canonical, sha256
from auto_lammps.plan_review import requirements


DELEGATION = 'Choose unspecified numerical implementation details yourself; retain every scientific condition.'
GUIDANCE = ['Explain proposed assumptions and actual parameter values in the complete plan for my review.']


def request_messages(call):
    return json.loads(call.args[0])['messages']


def assert_planning_policy(case, messages, stage):
    """Counter the original bans without copying a production prompt constant."""
    system = messages[0]['content']
    case.assertRegex(system, r'actively propose unspecified numerical implementation parameters')
    case.assertRegex(system, r'Honor free-text delegation in answers or guidance')
    case.assertRegex(system, r'actual artifact values, rationale, permitted basis and limitations')
    case.assertRegex(system, r'Explicit_reference numerical values still require permitted provenance')
    case.assertRegex(system, r'generate the complete explicit integer timestep list yourself')
    for obsolete in ('do not guess lattice constants', 'invent a seed',
                     'Missing ordering, seed or layer boundaries require clarification',
                     'obtain all scientific parameters from confirmed conditions',
                     'Obtain the complete list, starting timestep and MC parameters from confirmed task conditions',
                     'All scientific parameters and diagnostics criteria must be supplied explicitly'):
        case.assertNotIn(obsolete, ' '.join(system.split()))
    payload = json.loads(messages[1]['content'])
    contract = payload['scientific_adapter']
    case.assertEqual(contract['stage'], stage)
    case.assertTrue(contract['rules']['application_supplied'])
    case.assertFalse(contract['rules']['model_may_disable_checks'])
    case.assertEqual(contract['rules']['frozen_conditions'], 'unchanged')
    case.assertEqual(contract['rules']['attempts'], 'same_task_existing_persistent_limits')
    case.assertEqual(contract['rules']['physical_evaluation'], 'accounted_HPC_only')
    return payload


class ModelPlanningChoicesTests(unittest.TestCase):
    setUp = candidate_fixture.AgentCandidateTests.setUp

    def client_for(self, values, name, *, limit=4):
        calls = ModelCalls(self.root / (name + '.sqlite'), self.calls.config, max_requests=limit)
        transport = Mock(side_effect=[(200, response(value)) for value in values])
        return DeepSeekClient(calls, transport=transport, key_reader=lambda: 'synthetic-key'), transport

    def proposal(self):
        proposal = deepcopy(self.value)
        proposal['summary'] = (
            'Synthetic packaging only. Model-proposed initial fcc lattice a=4 angstrom, mass=63.5 amu '
            'and assignment seed=1729 are fixture assumptions, not user or source facts. '
            'The deterministic seed fixes reproducibility. Proposed cg minimize etol=1e-10, ftol=1e-8, '
            'maxiter=100, maxeval=1000 are finite fixture numerical settings; actual convergence requires '
            'HPC outputs. The final-value analysis window is [0,1000]. No scientific verification claimed.')
        proposal['structure']['assignment'] = dict(mode='random_counts', counts=[96], seed=1729)
        proposal['workflow'] = ('min_style cg\nminimize 1e-10 1e-8 100 1000\nrun 0\n'
                                'emit_table result.dat "$(step) $(pe)"')
        proposal['analysis']['files'] = ['result.dat']
        proposal['analysis']['plan']['tables'][0]['file'] = 'result.dat'
        proposal['analysis']['plan']['tables'][0]['columns'] = [
            dict(name='step', unit='step'), dict(name='energy', unit='eV')]
        proposal['analysis']['plan']['operations'][0].update(
            file='result.dat', x='step', y='energy', window=[0, 1000])
        return proposal

    def generate(self, client, task, name, **kwargs):
        with patch('subprocess.Popen', side_effect=AssertionError('no engine, SSH or paid transport')):
            return generate_candidate_draft(client, self.adapter, task_text=task, units='metal',
                resources=self.resources, store=self.root / name, condition_record_sha256='a' * 64,
                answers=DELEGATION, guidance=GUIDANCE, require_analysis_plan=True, **kwargs)

    def test_delegation_and_planning_policy_reach_initial_repair_and_static_review(self):
        task = 'Synthetic packaging only: retain the Cu fcc scope and report the relaxed final energy.'
        proposal = self.proposal()
        rejected = deepcopy(proposal)
        rejected['workflow'] = 'include forbidden.lmp'
        required = requirements(task, answers=DELEGATION, guidance=GUIDANCE)
        review = dict(issues=[], summary='Static fixture consistency only.', coverage=[
            dict(requirement=item['id'], evidence=[dict(source='rendered_script', quote='run 0')])
            for item in required])
        client, transport = self.client_for([rejected, proposal, review], 'three-dispatches')
        reservations = Mock()
        result = self.generate(client, task, 'reviewed', review_plan=True,
                               before_proposal_request=reservations)
        self.assertEqual(transport.call_count, 3)
        self.assertEqual([c.args[1] for c in reservations.call_args_list], ['initial', 'validation_repair'])
        for call in transport.call_args_list[:2]:
            payload = assert_planning_policy(self, request_messages(call), 'candidate_proposal')
            self.assertEqual(payload['task_text'], task)
            self.assertEqual(payload['answers'], DELEGATION)
            self.assertEqual(payload['guidance'], GUIDANCE)
            self.assertEqual(payload['proposal_round_budget']['limit'], 3)
            self.assertIn('not every unspecified implementation number',
                          payload['geometry_adapter']['missing_scientific_parameters'])
        review_payload = assert_planning_policy(self, request_messages(transport.call_args_list[2]), 'candidate_review')
        self.assertEqual(review_payload['requirement_references'], required)
        self.assertEqual(review_payload['proposal'], proposal)
        self.assertIn('summary alone is not implementation evidence',
                      request_messages(transport.call_args_list[2])[0]['content'])
        self.assertIn('minimize 1e-10 1e-8 100 1000', review_payload['rendered_script'])
        saved = json.loads((result['snapshot'].path / 'generation.json').read_text())
        self.assertEqual(saved['proposal'], proposal)
        self.assertEqual(saved['input']['condition_record_sha256'], 'a' * 64)
        self.assertEqual(saved['request_id'], sha256(canonical(saved['input']))[:32])
        self.assertEqual(saved['geometry_receipt']['composition_assignment']['specification']['seed'], 1729)
        self.assertFalse(saved['execution_authorized'])
        self.assertFalse(saved['scientific_conditions_verified'])
        self.assertEqual(candidate_check(result['snapshot'], max_atoms=100000)['scientific_status'], 'not_evaluated')
        self.assertEqual(client.calls.status()['used_requests'], 3)

    def test_complete_sampling_goal_needs_no_user_entered_list_and_retains_all_50_states(self):
        task = ('Synthetic packaging only: record 50 logarithmic samples from step 10 to 100000, '
                'round upward and retain every state; initial timestep is 0.')
        # Mock-model data are deliberately synthetic, not a real candidate for B.
        steps = [math.ceil(10 * (10000 ** (i / 49))) for i in range(50)]
        steps[-1] = 100000
        self.assertEqual(len(set(steps)), 50)
        proposal = self.proposal()
        proposal['summary'] = ('Synthetic model-proposed schedule: 50 logarithmic samples from 10 to 100000, '
            'round upward; full list is in workflow. Seed 1729 and geometry are declared fixture assumptions. '
            'This is planned work and not observed simulation output.')
        literal = json.dumps(steps)
        proposal['workflow'] = ("fix integrate all nve\nbegin_cycle observations 50\n"
            f"run_schedule sampled 0 '{literal}'\n"
            f"save_state sampled states.dump steps '{literal}'\n"
            'end_cycle observations\nunfix integrate\nemit_table result.dat "$(step) $(pe)"')
        proposal['analysis']['files'].append('states.dump')
        proposal['analysis']['plan']['operations'][0]['window'] = [0, 100000]
        client, transport = self.client_for([proposal], 'sampling')
        result = self.generate(client, task, 'sampled')
        payload = assert_planning_policy(self, request_messages(transport.call_args), 'candidate_proposal')
        self.assertNotIn(literal, payload['task_text'] + payload['answers'])
        screen = result['generation']['script_screen']
        self.assertEqual(screen['run_schedules'][0]['steps'], steps)
        self.assertEqual(screen['run_schedules'][0]['total_run_steps'], 100000)
        self.assertEqual(screen['state_site_scan']['states'][0]['count'], 50)
        self.assertEqual(result['generation']['proposal']['workflow'].count(literal), 2)
        self.assertFalse(result['execution_authorized'])

    def test_missing_intent_or_explicit_reference_still_returns_questions_and_no_snapshot(self):
        tasks = [
            ('Synthetic task: the physical study range conflicts between 100 K and 900 K.',
             'Which physical study range is intended?'),
            ('Synthetic task requires explicit_reference_v1 but supplies no permitted numerical reference.',
             'Provide the permitted numerical reference values and their provenance.')]
        for index, (task, question) in enumerate(tasks):
            proposal = dict(summary='Required scientific intent or permitted reference is unresolved.',
                questions=[question], structure=None, potential_pin=None, workflow=None, analysis=None)
            client, transport = self.client_for([proposal], 'question-' + str(index))
            with self.subTest(task=task), patch('auto_lammps.agent_candidates.build_structure') as build:
                result = self.generate(client, task, 'unresolved-' + str(index))
                assert_planning_policy(self, request_messages(transport.call_args), 'candidate_proposal')
                build.assert_not_called()
                self.assertEqual(result['status'], 'clarification_required')
                self.assertNotIn('snapshot', result)
                self.assertFalse((self.root / ('unresolved-' + str(index))).exists())
                self.assertEqual(transport.call_count, 1)

    def test_delegation_cannot_disable_missing_mandatory_capabilities(self):
        client, transport = self.client_for([self.proposal()], 'missing-capability')
        with patch('auto_lammps.agent_candidates.workflow_tool_context', return_value=None), \
                self.assertRaises(CandidateError):
            self.generate(client, 'Synthetic complete goal.', 'missing')
        transport.assert_not_called()
        self.assertEqual(client.calls.status()['used_requests'], 0)

    def test_delegation_does_not_grant_a_fourth_round_or_allow_native_control(self):
        rejected = []
        for command in ('include forbidden.lmp', 'shell forbidden', 'label forbidden'):
            value = self.proposal()
            value['workflow'] = command
            rejected.append(value)
        client, transport = self.client_for(rejected + [self.proposal()], 'bounded', limit=4)
        reservations = Mock()
        with self.assertRaises(PlanIterationLimit):
            self.generate(client, 'Synthetic complete goal.', 'bounded', before_proposal_request=reservations)
        self.assertEqual(transport.call_count, 3)
        self.assertEqual(reservations.call_count, 3)
        self.assertEqual(client.calls.status()['used_requests'], 3)
        self.assertFalse((self.root / 'bounded').exists())


class FixedGeometryPlanningChoicesTests(unittest.TestCase):
    setUp = fixed_fixture.FixedGeometryCandidateTests.setUp

    def test_delegated_choices_keep_original_geometry_and_exact_resource_pin(self):
        task = 'Synthetic allowed conditions; no reference answer.'
        required = requirements(task, answers=DELEGATION, guidance=GUIDANCE)
        review = dict(issues=[], summary='Static fixture consistency only.', coverage=[
            dict(requirement=item['id'], evidence=[
                dict(source='rendered_script', quote='read_data structure.data')]) for item in required])
        calls = ModelCalls(self.root / 'fixed-review.sqlite', self.calls.config, max_requests=2)
        transport = Mock(side_effect=[(200, response(self.value)), (200, response(review))])
        client = DeepSeekClient(calls, transport=transport, key_reader=lambda: 'synthetic-key')
        with patch('auto_lammps.agent_candidates.build_structure', side_effect=AssertionError('immutable input')), \
                patch('subprocess.Popen', side_effect=AssertionError('no physics')):
            result = generate_candidate_draft(client, self.adapter, task_text=task, units='metal',
                resources=self.resources, store=self.root / 'fixed', initial_geometry=self.selected, max_atoms=100,
                answers=DELEGATION, guidance=GUIDANCE, require_analysis_plan=True, review_plan=True)
            candidate_check(result['snapshot'], max_atoms=100)
        for index, call in enumerate(transport.call_args_list):
            stage = 'candidate_proposal' if index == 0 else 'candidate_review'
            payload = assert_planning_policy(self, request_messages(call), stage)
            self.assertEqual(payload['geometry_adapter']['initial_geometry'], self.selected)
            self.assertFalse(payload['geometry_adapter']['local_geometry_builder_allowed'])
        self.assertEqual(result['generation']['proposal']['structure'], self.value['structure'])
        self.assertEqual(result['generation']['proposal']['potential_pin'], self.pin)
        self.assertFalse(result['execution_authorized'])


if __name__ == '__main__':
    unittest.main()
