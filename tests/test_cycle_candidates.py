"""Finite scientific cycles use the ordinary freezer and grant recheck, not an engine."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from auto_lammps.agent_candidates import CandidateError, candidate_messages, validate_body
from auto_lammps.authorization import candidate_check
from auto_lammps.candidate_tools import expand_tools
import test_atom_swap_candidates as swap_fixture


def scientific_body(name='synthetic', count=19, temperature=220, types=2):
    return '\n'.join([
        f'begin_cycle {name} {count}',
        f'sample_swap_types {name} {types} 24680',
        f'fix exchange all atom/swap 1 7 34567 {temperature} types ${{{name}_i}} ${{{name}_j}} ke no',
        'run 1', 'unfix exchange',
        f'fix dynamics all npt temp {temperature} {temperature} 0.2 iso 0 0 2',
        'run 2', 'unfix dynamics',
        'emit_table final.data "$(step) $(pe)"',
        f'end_cycle {name}'])


PLAN={'tables':[{'file':'final.data','columns':[{'name':'step','unit':'step'},
                                              {'name':'energy','unit':'eV'}]}],
      'operations':[{'id':'final','method':'last','file':'final.data','x':'step',
                     'y':'energy','window':[0,10000]}]}


class CycleScreenTests(unittest.TestCase):
    def check(self, body, *, types=2):
        return validate_body(expand_tools(body,PLAN,'/output/'),['final.data'],
                             structures={'initial':3},type_count=types,packages=('MC',))

    def test_counted_closed_cycle_retains_scientific_parameters(self):
        record=self.check(scientific_body())
        self.assertEqual(record['calculation_commands'],38)
        self.assertEqual(record['bounded_cycles'][0]['count'],19)
        swap=record['workflow_requirements']['atom_swap_operations'][0]
        self.assertEqual(swap['cycle_count'],19)
        self.assertEqual(swap['types'],['${synthetic_i}','${synthetic_j}'])
        self.assertEqual(swap['type_sampler'],{'prefix':'synthetic','type_count':2,'seed':24680})
        self.assertEqual((swap['every_steps'],swap['attempts_per_event'],swap['temperature']),(1,7,220))
        self.assertFalse(record['execution_authorized'])

    def test_sampled_types_must_cover_actual_types_and_both_exact_placeholders(self):
        for raw,types in [(scientific_body(),3),
                          (scientific_body().replace('${synthetic_j}','${synthetic_i}'),2),
                          (scientific_body().replace('${synthetic_j}','1'),2)]:
            with self.subTest(types=types),self.assertRaises(CandidateError):self.check(raw,types=types)

    def test_unclosed_fix_lifecycle_is_not_a_repeatable_cycle(self):
        for raw in [scientific_body().replace('unfix exchange\n',''),
                    scientific_body().replace('unfix dynamics\n','')]:
            with self.assertRaisesRegex(CandidateError,'fix lifecycle'):self.check(raw)

    def test_active_fix_outside_cycle_must_be_preserved(self):
        self.check('fix original all momentum 100 linear 1 1 1\n'+scientific_body())
        with self.assertRaisesRegex(CandidateError,'fix lifecycle'):
            self.check('fix original all momentum 100 linear 1 1 1\n'+scientific_body().replace('run 1','unfix original\nrun 1'))

    def test_unknown_unfix_and_style_change_are_execution_errors_not_optional_advice(self):
        for raw in [scientific_body().replace('run 1','unfix missing\nrun 1'),
                    scientific_body().replace('run 1','unfix exchange extra\nrun 1'),
                    scientific_body().replace('run 2','fix dynamics all nve\nrun 2')]:
            with self.subTest(raw=raw),self.assertRaises(CandidateError):self.check(raw)

    def test_proactive_prompt_exposes_compiler_and_structural_contracts(self):
        messages=candidate_messages('Synthetic complete task',units='metal',resource_summaries=[],max_atoms=100,
                                    packages=('MC',))
        self.assertIn('begin_cycle/end_cycle',messages[0]['content'])
        self.assertIn('warren_cowley_first_shell',messages[0]['content'])
        self.assertIn('structural_contract',json.loads(messages[1]['content'])['analysis_adapter'])
        tool=json.loads(messages[1]['content'])['workflow_adapter']
        self.assertEqual(tool['version'],3)
        self.assertIn('sample_swap_types',tool['operations'])
        self.assertFalse(tool['limits_grant_resources'])


class CycleFrozenCandidateTests(unittest.TestCase):
    setUp=swap_fixture.AtomSwapCandidateTests.setUp
    generate=swap_fixture.AtomSwapCandidateTests.generate
    configure=swap_fixture.AtomSwapCandidateTests.configure

    def test_nine_independent_conditions_freeze_recheck_and_exact_initial_reloads(self):
        self.configure()
        self.value['analysis']['plan']=deepcopy(PLAN)
        stages=[]
        for index in range(9):
            if index:stages.append('reset_structure initial')
            stages.extend(['run 0',scientific_body(name=f'condition{index}',temperature=220+70*index)])
        self.value['workflow']='\n'.join(stages)
        with patch('subprocess.Popen',side_effect=AssertionError('No physics, SSH or paid API')):
            result=self.generate()
            rechecked=candidate_check(result['snapshot'],max_atoms=100000)
        script=(result['snapshot'].path/'in.lammps').read_text()
        record=json.loads((result['snapshot'].path/'generation.json').read_text())
        self.assertEqual(rechecked['screen'],record['script_screen'])
        self.assertEqual(rechecked['screen']['calculation_commands'],9*(1+2*19))
        self.assertEqual(script.count('read_data structure.data'),9)
        self.assertEqual(script.count(' loop 19'),9)
        self.assertEqual(script.count('jump SELF __alr_label_'),9)
        self.assertEqual(script.count('# columns: step energy'),1)
        self.assertNotIn('begin_cycle',script)
        self.assertNotIn('sample_swap_types',script)
        self.assertEqual(len(record['script_screen']['bounded_cycles']),9)
        self.assertEqual(self.transport.call_count,1)
        self.assertEqual(record['input']['workflow_adapter']['version'],3)

    def test_mixed_eighteen_sources_bind_structural_plan_to_the_same_grant(self):
        self.configure()
        tables=[];operations=[];files=[];stages=[]
        for index in range(9):
            numeric=f'energy{index}.dat';dump=f'geometry{index}.dump'
            files.extend([numeric,dump])
            tables.append({'file':numeric,'columns':[{'name':'step','unit':'step'},
                                                    {'name':'energy','unit':'eV'}]})
            operations.append({'id':f'energy{index}','method':'last','file':numeric,
                               'x':'step','y':'energy','window':[0,10000]})
            tables.append({'file':dump,'format':'lammps_dump','length_unit':'angstrom',
                           'elements':{'1':'Cu','2':'Ni'},'expected_counts':{'1':2,'2':1},
                           'pbc':[True,True,True]})
            operations.append({'id':f'ordering{index}','method':'warren_cowley_first_shell',
                               'file':dump,'neighbors':1,'neighbor_selection':'nearest_k',
                               'frames':{'first':0,'last':0,'stride':1},
                               'aggregation':'equal_frame_mean','pair_mode':'directed_and_symmetric'})
            if index:stages.append('reset_structure initial')
            stages.extend([f'dump d{index} all custom 3 /output/{dump} id type x y z',
                           'run 0',scientific_body(name=f'condition{index}').replace('final.data',numeric)])
        self.value['analysis'].update(files=files,plan={'tables':tables,'operations':operations})
        self.value['workflow']='\n'.join(stages)
        with patch('subprocess.Popen',side_effect=AssertionError('No engine or external model request')):
            result=self.generate()
            check=candidate_check(result['snapshot'],max_atoms=100000)
        analysis=json.loads((result['snapshot'].path/'analysis.json').read_text())
        self.assertEqual(analysis['implementation_status'],'numeric_tables_v3')
        self.assertEqual(len(analysis['proposal']['plan']['tables']),18)
        self.assertEqual(check['outputs'],['log.lammps','stderr.txt','stdout.txt']+files)
        self.assertEqual(check['screen']['calculation_commands'],9*(1+19*2))
        self.assertEqual(self.transport.call_count,1)
