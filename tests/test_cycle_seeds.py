"""Compiler-only seed sequences: no engine, science answers, SSH or paid calls."""
import unittest
from unittest.mock import patch

from auto_lammps.agent_candidates import CandidateError, candidate_messages, validate_body
from auto_lammps.authorization import candidate_check
from auto_lammps.candidate_tools import (MAX_CYCLE_SEED, cycle_metadata, expand_tools,
                                        workflow_tool_context)
import test_atom_swap_candidates as swap_fixture
from test_cycle_candidates import PLAN, scientific_body


def body(count=17):
    return scientific_body(count=count).replace(
        'sample_swap_types synthetic 2 24680',
        'sample_swap_types synthetic 2 24680\ncycle_seed mc 100003 37').replace(
        'atom/swap 1 7 34567', 'atom/swap 1 7 ${mc_seed}')


def screen(raw, packages=('MC',)):
    return validate_body(expand_tools(raw,PLAN,'/output/'),['final.data'],
                         structures={'initial':3},type_count=2,packages=packages)


class CycleSeedTests(unittest.TestCase):
    def test_bounded_seed_changes_without_changing_pair_rng_counts_or_physics(self):
        raw=body();metadata=cycle_metadata(raw);before=screen(scientific_body(count=17))
        self.assertNotIn('seed_sequences',before['bounded_cycles'][0])
        checked=screen(raw)
        self.assertEqual(metadata['sampling_seed'],24680)
        sequence=metadata['cycle_seed_variables']['mc_seed']
        self.assertEqual((sequence['first'],sequence['stride'],sequence['last'],sequence['count']),
                         (100003,37,100595,17))
        self.assertEqual(checked['calculation_commands'],before['calculation_commands'])
        old=before['workflow_requirements']['atom_swap_operations'][0]
        swap=checked['workflow_requirements']['atom_swap_operations'][0]
        self.assertEqual({k:v for k,v in swap.items() if k not in ('seed','seed_sequence')},
                         {k:v for k,v in old.items() if k!='seed'})
        self.assertEqual(swap['seed'],'${mc_seed}')
        self.assertFalse(swap['seed_sequence']['persistent_rng'])
        lowered=expand_tools(raw,PLAN,'/output/',lower_cycles=True)
        self.assertIn('variable mc_seed index $(100003+(v___alr_cycle_synthetic-1)*37:%.0f)',lowered)
        self.assertEqual(lowered.count('random('),2)  # unchanged pair selection only
        self.assertIn('variable mc_seed delete\nvariable synthetic_i delete',lowered)
        self.assertNotIn('cycle_seed mc',lowered)

    def test_full_technical_cycle_bound_and_exact_integer_endpoint(self):
        first=MAX_CYCLE_SEED-999999
        raw='\n'.join(['begin_cycle full 1000000',f'cycle_seed full {first} 1',
            'fix mc all atom/swap 1 1 ${full_seed} 220 types 1 2 ke no',
            'run 1','unfix mc','end_cycle full'])
        metadata=cycle_metadata(raw)
        self.assertEqual(metadata['cycle_seed_variables']['full_seed']['last'],MAX_CYCLE_SEED)
        self.assertEqual(metadata['calculation_commands'],1000000)
        self.assertLess(len(expand_tools(raw,None,'',lower_cycles=True)),1000)
        with self.assertRaisesRegex(ValueError,'without wrap'):
            cycle_metadata(raw.replace(f'{first} 1',f'{first+1} 1'))

    def test_explicit_schedule_uses_each_seed_once_with_the_same_run_intervals(self):
        raw='\n'.join(['begin_cycle scheduled 3','cycle_seed chosen 101 13',
            'fix mc all atom/swap 1 7 ${chosen_seed} 220 types 1 2 ke no',
            "run_schedule saved 0 '[5,17,61]'","save_state saved states.dump steps '[5,17,61]'",
            'unfix mc','emit_table final.data "$(step) $(pe)"','end_cycle scheduled'])
        checked=validate_body(expand_tools(raw,PLAN,'/output/'),['final.data','states.dump'],
                              structures={'initial':3},type_count=2,packages=('MC',))
        self.assertEqual(checked['calculation_commands'],3)
        lowered=expand_tools(raw,PLAN,'/output/',lower_cycles=True)
        for index in range(3):
            self.assertIn(f'variable chosen_seed index $(101+{index}*13:%.0f)',lowered)
        self.assertEqual(lowered.count('variable chosen_seed delete'),3)
        self.assertEqual([line for line in lowered.splitlines() if line.startswith('run ')],
                         ['run 5','run 12','run 44'])

    def test_invalid_bounds_prefix_and_external_or_dynamic_values_are_rejected(self):
        for declaration in ('cycle_seed mc 0 1','cycle_seed mc 1 0',
                'cycle_seed mc -1 1','cycle_seed mc 1 -1','cycle_seed mc 2147483647 1',
                'cycle_seed mc 2147483640 1','cycle_seed mc ${first} 1',
                'cycle_seed mc 1 $(step)','cycle_seed ../mc 1 1',
                'cycle_seed synthetic 1 1','cycle_seed mc 1 1\ncycle_seed mc 2 1'):
            with self.subTest(declaration=declaration),self.assertRaises(ValueError):
                cycle_metadata(body().replace('cycle_seed mc 100003 37',declaration))
        with self.assertRaises(ValueError):cycle_metadata('cycle_seed mc 1 1\nrun 0')

    def test_managed_seed_cannot_escape_alias_redefine_or_enter_another_argument(self):
        raw=body()
        invalid=[raw+'\nprint "${mc_seed}"',
            raw.replace('cycle_seed mc 100003 37','print "${mc_seed}"\ncycle_seed mc 100003 37'),
            raw.replace('run 1','variable mc_seed equal 1\nrun 1'),
            raw.replace('run 1','capture mc_seed 1\nrun 1'),
            raw.replace('run 1','capture alias v_mc_seed\nrun 1'),
            raw.replace('1 7 ${mc_seed}','${mc_seed} 7 101'),
            raw.replace('1 7 ${mc_seed}','1 7 $(v_mc_seed)'),
            raw.replace('types ${synthetic_i} ${synthetic_j}','types ${mc_seed} ${synthetic_j}'),
            raw+'\n'+scientific_body(name='other').replace('34567','${mc_seed}')]
        for changed in invalid:
            with self.subTest(raw=changed),self.assertRaises(ValueError):cycle_metadata(changed)
        with self.assertRaises(ValueError):
            screen('variable mc_seed equal 1\n'+raw)

    def test_engine_package_and_other_numeric_checks_are_retained(self):
        with self.assertRaisesRegex(CandidateError,'configured engine packages'):screen(body(),packages=())
        for changed in (body().replace('1 7 ${mc_seed}','0 7 ${mc_seed}'),
                        body().replace('1 7 ${mc_seed}','1 0 ${mc_seed}'),
                        body().replace('${mc_seed} 220','${mc_seed} NaN')):
            with self.assertRaises(CandidateError):screen(changed)

    def test_capability_is_proactive_and_explicitly_not_a_continuous_stream(self):
        context=workflow_tool_context()
        self.assertIn('cycle_seed',context['operations'])
        system=candidate_messages('Synthetic complete task',units='metal',resource_summaries=[],max_atoms=100)[0]['content']
        self.assertIn('NOT a continuous MC RNG',system)
        self.assertIn('cannot satisfy it',system)
        self.assertIn('no default is supplied',system)


class FrozenCycleSeedTests(unittest.TestCase):
    setUp=swap_fixture.AtomSwapCandidateTests.setUp
    generate=swap_fixture.AtomSwapCandidateTests.generate
    configure=swap_fixture.AtomSwapCandidateTests.configure

    def test_ordinary_candidate_freezes_and_rechecks_exact_compiler_output(self):
        from copy import deepcopy
        self.configure();self.value['workflow']=body();self.value['analysis']['plan']=deepcopy(PLAN)
        with patch('subprocess.Popen',side_effect=AssertionError('No physical evaluation or scheduler')):
            candidate=self.generate();checked=candidate_check(candidate['snapshot'],max_atoms=100000)
        self.assertEqual(checked['screen']['workflow_requirements']['atom_swap_operations'][0]['seed'],
                         '${mc_seed}')
        self.assertFalse(checked['screen']['workflow_requirements']['atom_swap_operations'][0]['seed_sequence']['persistent_rng'])
        self.assertEqual(self.transport.call_count,1)
        self.assertFalse(checked['screen']['execution_authorized'])

