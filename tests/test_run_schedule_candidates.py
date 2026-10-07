"""Explicit planned timesteps traverse candidate checks and freezing, no physics."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

from auto_lammps.agent_candidates import CandidateError, generate_candidate_draft, validate_body
from auto_lammps.authorization import candidate_check
from auto_lammps.deepseek import DeepSeekClient, ModelCalls
from auto_lammps.manifest import canonical, sha256
from auto_lammps.plan_review import requirements
import test_atom_swap_candidates as fixtures
import test_state_site_scan_candidates as scan_fixtures
from test_deepseek import response


WORKFLOW = '\n'.join([
    'run 3', 'fix exchange all atom/swap 7 3 12345 600 types 1 2 ke no',
    'begin_cycle evolution 3', "run_schedule saved 3 '[5,9,16]'",
    "save_state saved states.dump steps '[5,9,16]'", 'end_cycle evolution', 'unfix exchange'])


class RunScheduleCandidateTests(unittest.TestCase):
    def check(self, workflow=WORKFLOW, packages=('MC',)):
        return validate_body(workflow, ['states.dump'], structures={'initial': 3},
            type_count=2, type_elements=['Cu', 'Ni'], boundary=['p', 'p', 'p'], packages=packages)

    def test_workload_counts_MC_fix_creation_phase_across_segments_and_preserves_full_steps(self):
        with patch('subprocess.Popen', side_effect=AssertionError('No simulation or SSH')):
            record = self.check()
        self.assertEqual(record['calculation_commands'], 4)
        self.assertEqual(record['run_schedules'][0]['steps'], [5, 9, 16])
        self.assertEqual(record['run_schedules'][0]['run_steps_per_iteration'], [2, 4, 7])
        self.assertEqual(record['run_schedules'][0]['total_run_steps'], 13)
        self.assertEqual(record['total_run_steps'], 16)
        swap = record['workflow_requirements']['atom_swap_operations'][0]
        self.assertEqual(swap['run_schedule_id'], 'saved')
        self.assertEqual(swap['planned_events_per_segment'], [1, 0, 1])
        self.assertEqual(swap['planned_events'], 2)
        self.assertEqual(swap['planned_attempts_per_segment'], [3, 0, 3])
        self.assertEqual(swap['planned_attempts'], 6)
        self.assertEqual(swap['planned_run_steps'], 13)
        self.assertEqual(swap['planned_work_scope'], 'complete_dynamics_for_fix_declaration')
        self.assertFalse(record['workflow_requirements']['environment_verified'])
        self.assertFalse(record['execution_authorized'])

    def test_schedule_does_not_relax_engine_scope_literal_runs_or_saved_state_identity(self):
        for workflow, packages in [
                (WORKFLOW, ()),
                (WORKFLOW.replace("save_state saved states.dump steps '[5,9,16]'",
                                  "save_state saved states.dump steps '[5,9,17]'"), ('MC',)),
                (WORKFLOW.replace("run_schedule saved 3 '[5,9,16]'", 'run ${arbitrary}'), ('MC',)),
                (WORKFLOW.replace("run_schedule saved 3 '[5,9,16]'",
                                  "run_schedule saved 3 '[5,9,16]'\nrun 1"), ('MC',))]:
            with self.subTest(workflow=workflow, packages=packages), self.assertRaises(CandidateError):
                self.check(workflow, packages)

    def test_each_recreated_fix_has_its_own_segment_phase_and_mismatched_start_is_rejected(self):
        workflow = '\n'.join(['run 3', 'begin_cycle evolution 3',
            'fix exchange all atom/swap 7 3 12345 600 types 1 2 ke no',
            "run_schedule saved 3 '[5,9,16]'", "save_state saved states.dump steps '[5,9,16]'",
            'unfix exchange', 'end_cycle evolution'])
        swap = self.check(workflow)['workflow_requirements']['atom_swap_operations'][0]
        self.assertEqual(swap['planned_events_per_segment'], [1, 1, 1])
        self.assertEqual(swap['planned_attempts'], 9)
        with self.assertRaisesRegex(CandidateError, 'starting timestep'):
            self.check(WORKFLOW.replace('run 3\n', 'run 2\n'))

    def test_persistent_fix_accumulates_all_runs_and_multiple_schedules_without_resetting_phase(self):
        workflow = '\n'.join([
            'fix exchange all atom/swap 7 3 12345 600 types 1 2 ke no', 'run 3',
            'begin_cycle evolution 3', "run_schedule saved 3 '[5,9,16]'",
            "save_state saved states.dump steps '[5,9,16]'", 'end_cycle evolution', 'run 2',
            'begin_cycle continuation 2', "run_schedule later 18 '[21,25]'",
            "save_state later later.dump steps '[21,25]'", 'end_cycle continuation',
            'run 6', 'unfix exchange'])
        record = validate_body(workflow, ['states.dump', 'later.dump'], structures={'initial': 3},
            type_count=2, type_elements=['Cu', 'Ni'], boundary=['p', 'p', 'p'], packages=('MC',))
        swap = record['workflow_requirements']['atom_swap_operations'][0]
        self.assertEqual(record['calculation_commands'], 8)
        self.assertEqual(record['total_run_steps'], 31)
        self.assertEqual(swap['planned_run_steps'], 31)
        self.assertEqual(swap['run_schedule_ids'], ['saved', 'later'])
        self.assertNotIn('run_schedule_id', swap)
        self.assertEqual(swap['planned_events_per_segment'], [1, 0, 1, 1, 0, 0, 1, 1])
        self.assertEqual(swap['planned_events'], 5)
        self.assertEqual(swap['planned_attempts'], 15)
        self.assertEqual([item['command'] for item in swap['planned_work']],
                         ['run', 'run_schedule', 'run', 'run_schedule', 'run'])

    def test_ordinary_bounded_cycle_counts_persistent_phase_and_unknown_lifecycle_fails_closed(self):
        warmup = '\n'.join(['fix exchange all atom/swap 7 3 12345 600 types 1 2 ke no',
            'begin_cycle warmup 3', 'run 1', 'run 2', 'end_cycle warmup'])
        tail = '\n'.join(['begin_cycle evolution 2', "run_schedule saved 9 '[11,16]'",
            "save_state saved states.dump steps '[11,16]'", 'end_cycle evolution', 'unfix exchange'])
        record = self.check(warmup+'\n'+tail)
        swap = record['workflow_requirements']['atom_swap_operations'][0]
        self.assertEqual(record['total_run_steps'], 16)
        self.assertEqual(swap['planned_events'], 3)
        self.assertEqual(swap['planned_work'][0]['command'], 'bounded_cycle_runs')
        self.assertEqual(swap['planned_work'][0]['total_run_steps'], 9)
        changed = warmup.replace('run 1', 'unfix exchange\nfix exchange all atom/swap 7 3 12345 600 types 1 2 ke no\nrun 1')
        with self.assertRaisesRegex(CandidateError, 'known fix lifecycle'):
            self.check(changed+'\n'+tail)

    def test_workflow_totals_include_independent_unscheduled_MC_fix_and_reject_unknown_phase(self):
        first = 'fix exchange all atom/swap 2 4 12345 600 types 1 2 ke no\nrun 3\nunfix exchange\n'
        record = self.check(first+WORKFLOW.removeprefix('run 3\n'))
        planned = record['workflow_requirements']
        self.assertEqual(record['total_run_steps'], 16)
        self.assertEqual(planned['planned_work_scope'], 'all_atom_swap_declarations')
        self.assertEqual([item['planned_events'] for item in planned['atom_swap_operations']], [2, 2])
        self.assertEqual(planned['atom_swap_operations'][0]['run_schedule_ids'], [])
        self.assertEqual(planned['planned_events_total'], 4)
        self.assertEqual(planned['planned_attempts_total'], 14)
        unknown = ('begin_cycle warmup 2\nfix earlier all atom/swap 2 4 12345 600 types 1 2 ke no\n'
                   'run 3\nunfix earlier\nend_cycle warmup\nreset_timestep 3\n')
        with self.assertRaisesRegex(CandidateError, 'known fix lifecycle'):
            self.check(unknown+WORKFLOW.removeprefix('run 3\n'))

    def test_minimize_requires_explicit_known_start_before_creating_persistent_MC(self):
        scheduled = WORKFLOW.removeprefix('run 3\n').replace("saved 3 '[5,9,16]'", "saved 0 '[5,9,16]'")
        with self.assertRaisesRegex(CandidateError, 'starting timestep'):
            self.check('minimize 0 1e-7 1200 9000\n'+scheduled)
        record = self.check('minimize 0 1e-7 1200 9000\nreset_timestep 0\n'+scheduled)
        self.assertEqual(record['workflow_requirements']['atom_swap_operations'][0]['planned_events'], 3)
        with self.assertRaisesRegex(CandidateError, 'Unfix atom/swap before reset_timestep'):
            self.check('fix exchange all atom/swap 7 3 12345 600 types 1 2 ke no\n'
                'minimize 0 1e-7 1200 9000\nreset_timestep 0\n'+scheduled.split('\n', 1)[1])
        with self.assertRaisesRegex(CandidateError, 'known fix lifecycle'):
            self.check(WORKFLOW.replace('unfix exchange', 'minimize 0 1e-7 1200 9000\nrun 1\nunfix exchange'))

    def test_scan_restart_timestep_is_complete_state_ordinal_before_next_schedule(self):
        first = scan_fixtures.scheduled_workflow()
        tail = '\n'.join(['fix exchange all atom/swap 7 3 12345 600 types 1 2 ke no',
            'begin_cycle continuation 2', "run_schedule later 3 '[5,9]'",
            "save_state later later.dump steps '[5,9]'", 'end_cycle continuation', 'unfix exchange'])
        outputs = [*scan_fixtures.OUTPUTS, 'later.dump']
        record = validate_body(first+'\n'+tail, outputs, structures={'initial': 3},
            type_count=2, type_elements=['Cu', 'Ni'], boundary=['p', 'p', 'p'], packages=('MC','MEAM'))
        self.assertEqual(record['state_site_scan']['scans'][0]['state_count'], 3)
        self.assertEqual(record['workflow_requirements']['atom_swap_operations'][1]['planned_events'], 1)
        with self.assertRaisesRegex(CandidateError, 'starting timestep'):
            wrong_start = tail.replace("later 3 '[5,9]'", "later 105 '[107,111]'").replace(
                "later.dump steps '[5,9]'", "later.dump steps '[107,111]'")
            validate_body(first+'\n'+wrong_start, outputs,
                structures={'initial': 3}, type_count=2, type_elements=['Cu', 'Ni'],
                boundary=['p','p','p'], packages=('MC','MEAM'))

    def test_same_schedule_reaches_model_review_literal_compiler_and_authorization_recheck(self):
        fixture = fixtures.AtomSwapCandidateTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.configure()
        proposal = deepcopy(fixture.value)
        proposal['workflow'] = WORKFLOW + '\nemit_table final.data "0 $(pe)"'
        proposal['analysis']['files'] = ['states.dump', 'final.data']
        task_text = 'Synthetic explicit complete schedule'
        review = {'summary': 'Synthetic static review only', 'issues': [], 'coverage': [
            {'requirement': item['id'], 'evidence': [{'source': 'rendered_script', 'quote': 'run 2'}]}
            for item in requirements(task_text)]}
        calls = ModelCalls(fixture.root / 'scheduled.sqlite', fixture.calls.config, max_requests=2)
        transport = Mock(side_effect=[(200, response(proposal)), (200, response(review))])
        client = DeepSeekClient(calls, transport=transport, key_reader=lambda: 'synthetic-key')
        with patch('subprocess.Popen', side_effect=AssertionError('No simulation or SSH')):
            result = generate_candidate_draft(client, fixture.adapter, task_text=task_text, units='metal',
                resources=fixture.resources, store=fixture.root / 'schedule-snapshot',
                review_plan=True, require_analysis_plan=True)
            checked = candidate_check(result['snapshot'], max_atoms=100000)
        saved = result['generation']
        self.assertEqual(checked['screen'], saved['script_screen'])
        self.assertEqual(saved['script_screen']['run_schedules'][0]['steps'], [5, 9, 16])
        self.assertEqual(saved['script_screen']['state_site_scan']['states'][0]['total_run_steps'], 13)
        script = (result['snapshot'].path / 'in.lammps').read_text()
        self.assertNotIn('run_schedule', script)
        for command in ('run 3', 'run 2', 'run 4', 'run 7'):
            self.assertIn(command, script)
        self.assertEqual(script.count('fix exchange all atom/swap'), 1)
        self.assertEqual(script.count('unfix exchange'), 1)
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(saved['request_id'], sha256(canonical(saved['input']))[:32])
        actual_review = json.loads(json.loads(transport.call_args.args[0])['messages'][1]['content'])
        self.assertEqual(actual_review['workflow_screen']['run_schedules'][0]['steps'], [5, 9, 16])
        self.assertEqual(actual_review['scientific_adapter']['stage'], 'candidate_review')
        self.assertEqual(checked['scientific_status'], 'not_evaluated')


if __name__ == '__main__':
    unittest.main()
