"""Complete controlled state/site compilation; fake engines are never physics evidence."""
from copy import deepcopy
import json
import shlex
import unittest
from unittest.mock import patch

from auto_lammps.agent_candidates import (CandidateError, candidate_messages, render_candidate_script,
                                         validate_body)
from auto_lammps.candidate_tools import (expand_tools, state_scan_metadata, scan_columns,
                                        check_table_writers, workflow_tool_context, cycle_metadata,
                                        scheduled_swap_accounting)
from auto_lammps.authorization import candidate_check
import test_atom_swap_candidates as fixture


RELAX={'min_style':'cg','etol':0,'ftol':1e-7,'max_iterations':1200,'max_evaluations':9000,
       'kinetic_energy':'zero','box':{'mode':'iso','pressure':0,'vmax':0.002},
       'convergence':{'force_metric':'fnorm','force_tolerance':1e-7,
                      'pressure_target':0,'pressure_tolerance':0.03}}


def specification(atoms=3,elements=('Cu','Ni')):
    return dict(version=1,states='saved',atom_count=atoms,units='metal',elements=list(elements),
                variants=[{'id':index,'type':index+1} for index in range(len(elements))]+[{'id':len(elements),'type':None}],
                baseline_file='baselines.dump',cache_file='baseline.restart',table_file='sites.dat',
                baseline_relaxation=deepcopy(RELAX),variant_relaxation=deepcopy(RELAX))


def workflow(spec=None,count=2,stride=1000):
    spec=spec if spec is not None else specification()
    return '\n'.join(['fix evolution all npt temp 600 600 0.2 iso 0 0 2',
        'fix exchange all atom/swap 1000 25 12345 600 types 1 2 ke no',
        f'begin_cycle production {count}',f'run {stride}',f'save_state saved states.dump {stride} {stride}',
        'end_cycle production','unfix exchange','unfix evolution',
        'scan_sites complete '+shlex.quote(json.dumps(spec,separators=(',',':')))])


def scheduled_workflow(spec=None,steps=(14,35,105),start_step=0):
    spec=spec if spec is not None else specification()
    declared=shlex.quote(json.dumps(list(steps),separators=(',',':')))
    return '\n'.join(['fix evolution all npt temp 600 600 0.2 iso 0 0 2',
        'fix exchange all atom/swap 7 4 12345 600 types 1 2 ke no',
        f'begin_cycle production {len(steps)}',f'run_schedule saved {start_step} {declared}',
        f'save_state saved states.dump steps {declared}',
        'end_cycle production','unfix exchange','unfix evolution',
        'scan_sites complete '+shlex.quote(json.dumps(spec,separators=(',',':')))])


def plan(spec=None):
    spec=spec if spec is not None else specification()
    return {'tables':[{'file':'sites.dat','format':'site_scan_array_v1','columns':scan_columns(spec['elements'])}],
            'operations':[]}


def thermodynamics_operation(states=2,sites=3):
    return dict(id='complete',method='site_thermodynamics_v1',file='sites.dat',
        equations_version='independent_binary_sites_v1',states={'first':1,'last':states,'stride':1},
        sites={'first':1,'last':sites,'stride':1},elements={'0':'Cu','1':'Ni'},
        expected_counts={'0':sites-1,'1':1},vacancy_variant=2,pressure_GPa=0,
        convergence={'force_metric':'fnorm','force_max_eV_per_A':1e-7,'pressure_tolerance_GPa':0.000003,
                     'max_iterations':1200,'max_evaluations':9000},
        baseline_tolerance={'energy_eV':1e-10,'volume_A3':1e-10},
        reservoir_anchor={'method':'baseline_euler_enthalpy_v1','source':'Explicit synthetic fixture assumption'},
        temperatures_K=[250,600],beta_grid={'first':0.2,'last':4.0,'count':9},
        solver={'method':'safeguarded_newton_bisection_v1','chemical_potential_bounds_eV':[-20,20],
                'composition_tolerance':1e-8,'chemical_potential_tolerance_eV':1e-10,'max_iterations':120},
        models=['two_state_host_vacancy','three_state_competing_species'],aggregation='equal_state_site_weight')


OUTPUTS=['states.dump','baselines.dump','baseline.restart','sites.dat']
HEADER=['units metal','atom_style atomic','atom_modify map array','boundary p p p','read_data structure.data',
        'pair_style meam','pair_coeff * * potentials/library.meam Cu Ni potentials/parameters.meam Cu Ni']


class CompleteStateSiteSyntaxTests(unittest.TestCase):
    def check(self,body=None,*,atoms=3,elements=('Cu','Ni'),outputs=OUTPUTS):
        return validate_body(expand_tools(body or workflow(),plan(specification(atoms,elements)),'/output/'),
            outputs,structures={'initial':atoms},type_count=len(elements),type_elements=elements,
            boundary=['p','p','p'],packages=('MC','MEAM'))

    def test_full_fifty_states_and_all_1024_sites_without_unrolling_arrays(self):
        spec=specification(1024)
        raw=workflow(spec,50,20000)
        record=self.check(raw,atoms=1024)
        scan=record['state_site_scan']['scans'][0]
        self.assertEqual(record['calculation_commands'],50+50*(1+1024*3))
        self.assertEqual(scan['rows'],50*1024*4)
        self.assertEqual(scan['site_domain'],[1,1024])
        self.assertEqual(scan['state_domain'],[1,50])
        self.assertEqual(scan['retry_count'],0)
        self.assertEqual(scan['additional_submissions'],0)
        self.assertEqual(scan['maximum_force_evaluations'],50*9000+50*1024*3*9000)
        self.assertEqual(record['state_site_scan']['states'][0]['last_step'],1000000)
        self.assertEqual(record['state_site_scan']['states'][0]['total_run_steps'],1000000)
        script=expand_tools(raw,plan(spec),'/output/',lower_cycles=True,reload_header=HEADER)
        self.assertLess(len(script),20000)
        self.assertEqual(script.count(' loop 50'),2)
        self.assertEqual(script.count(' loop 1024'),1)
        self.assertEqual(script.count('read_restart /output/baseline.restart'),5)
        self.assertEqual(script.count('write_restart /output/baseline.restart'),1)
        self.assertEqual(script.count('minimize 0 1e-07 1200 9000'),4)
        self.assertIn('purge yes add keep',script)
        self.assertIn('delete_atoms group __alr_scan_complete_sitegroup compress no',script)
        self.assertNotIn('reset_atoms',script)
        self.assertEqual(record['state_site_scan']['output_files'],OUTPUTS)
        self.assertIn('format float %.17g',script)

    def test_explicit_schedule_saves_and_scans_every_exact_step_and_counts_mc_intervals(self):
        steps=[14,35,105]
        raw=scheduled_workflow(steps=steps)
        record=self.check(raw)
        saved=record['state_site_scan']['states'][0]
        self.assertEqual(record['state_site_scan']['version'],2)
        self.assertEqual(saved['steps'],steps)
        self.assertEqual(saved['run_steps_per_iteration'],[14,21,70])
        self.assertEqual(saved['total_run_steps'],105)
        self.assertNotIn('stride',saved)
        self.assertEqual(record['state_site_scan']['scans'][0]['source_timesteps'],steps)
        self.assertEqual(record['calculation_commands'],3+3*(1+3*3))
        schedule=cycle_metadata(raw)['run_schedules'][0]
        counted=scheduled_swap_accounting(schedule,7,4,fix_created_step=0)
        self.assertEqual(counted,dict(planned_events_per_segment=[2,3,10],planned_events=15,
            planned_attempts_per_segment=[8,12,40],planned_attempts=60))
        # Event phase follows fix creation, not global multiples of N or the
        # first step of each literal run segment.
        offset=cycle_metadata(scheduled_workflow(steps=[14,35,105],start_step=3))['run_schedules'][0]
        self.assertEqual(scheduled_swap_accounting(offset,7,4,fix_created_step=3)['planned_events'],15)
        phased=cycle_metadata(scheduled_workflow(steps=[5,9,16],start_step=3))['run_schedules'][0]
        self.assertEqual(scheduled_swap_accounting(phased,7,4,fix_created_step=3),
            dict(planned_events_per_segment=[1,0,1],planned_events=2,
                 planned_attempts_per_segment=[4,0,4],planned_attempts=8))
        self.assertEqual(scheduled_swap_accounting(phased,7,4,recreate_per_segment=True),
            dict(planned_events_per_segment=[1,1,1],planned_events=3,
                 planned_attempts_per_segment=[4,4,4],planned_attempts=12))
        with self.assertRaises(ValueError):scheduled_swap_accounting(phased,7,4)
        with self.assertRaises(ValueError):scheduled_swap_accounting(phased,7,4,fix_created_step=4)
        script=expand_tools(raw,plan(),'/output/',lower_cycles=True,reload_header=HEADER)
        self.assertEqual([int(words[1]) for line in script.splitlines()
            if (words:=shlex.split(line)) and words[0]=='run'],[14,21,70])
        self.assertEqual(script.count('fix exchange all atom/swap'),1)
        self.assertEqual(script.count('unfix exchange'),1)
        self.assertNotIn('run 0',script)
        self.assertNotIn('jump SELF __alr_label_production',script)
        self.assertIn('variable __alr_scan_complete_state index 1 2 3',script)
        self.assertIn('variable __alr_scan_complete_step index 14 35 105',script)
        self.assertIn('next __alr_scan_complete_state __alr_scan_complete_step',script)
        self.assertIn('read_dump /output/states.dump ${__alr_scan_complete_step}',script)
        for previous,target in zip([0,*steps[:-1]],steps):
            self.assertIn(f'if "$(step) != {previous}" then "quit 90"\nrun {target-previous}',script)
            self.assertIn(f'if "$(step) != {target}" then "quit 90"\nwrite_dump',script)

    def test_explicit_schedule_rejects_reduction_missing_or_inconsistent_steps(self):
        raw=scheduled_workflow()
        invalid_lists=['[]','[1,1,3]','[3,1,9]','[-1,2,3]','[true,2,3]',
            '[1.0,2,3]','["1",2,3]','[1,2,2147483648]','[1,2,NaN]',
            '{"steps":[1,2,3]}',json.dumps(list(range(257)))]
        for invalid in invalid_lists:
            replaced=raw.replace("'[14,35,105]'",shlex.quote(invalid))
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):
                expand_tools(replaced,plan(),'/output/')
        bad_bodies=[raw.replace('production 3','production 2'),
            raw.replace('run_schedule saved 0','run_schedule saved 14'),
            raw.replace('run_schedule saved 0','run_schedule saved ${step}'),
            raw.replace('save_state saved states.dump steps','save_state other states.dump steps'),
            raw.replace("save_state saved states.dump steps '[14,35,105]'",
                        "save_state saved states.dump steps '[14,35,106]'"),
            raw.replace("save_state saved states.dump steps '[14,35,105]'",'save_state saved states.dump 14 21'),
            raw.replace("save_state saved states.dump steps '[14,35,105]'",''),
            raw.replace("run_schedule saved 0 '[14,35,105]'",''),
            raw.replace('end_cycle production','run 0\nend_cycle production'),
            raw.replace('end_cycle production','minimize 0 1e-7 1200 9000\nend_cycle production'),
            raw.replace('end_cycle production','reset_timestep 0\nend_cycle production'),
            raw.replace("run_schedule saved 0 '[14,35,105]'\nsave_state saved states.dump steps '[14,35,105]'",
                        "save_state saved states.dump steps '[14,35,105]'\nrun_schedule saved 0 '[14,35,105]'"),
            raw.replace('end_cycle production',"run_schedule other 0 '[14,35,105]'\nend_cycle production")]
        for invalid in bad_bodies:
            with self.subTest(invalid=invalid[:100]),self.assertRaises(ValueError):
                expand_tools(invalid,plan(),'/output/')
        schedule=cycle_metadata(raw)['run_schedules'][0]
        for mutate in [lambda s:s['run_intervals'][1].__setitem__(0,15),lambda s:s.update(total_run_steps=104)]:
            changed=deepcopy(schedule);mutate(changed)
            with self.assertRaises(ValueError):scheduled_swap_accounting(changed,7,4,fix_created_step=0)

    def test_each_variant_restores_complete_baseline_before_mutation_and_minimization(self):
        script=expand_tools(workflow(),plan(),'/output/',lower_cycles=True,reload_header=HEADER)
        for action in ['set atom ${__alr_scan_complete_site} type 1',
                       'set atom ${__alr_scan_complete_site} type 2',
                       'delete_atoms group __alr_scan_complete_sitegroup compress no']:
            end=script.index(action)
            restart=script.rfind('read_restart /output/baseline.restart',0,end)
            self.assertGreater(restart,script.rfind('minimize',0,end))
            self.assertIn(HEADER[-1],script[restart:end])
        # Type groups are removed before caching, so later replacement counts are
        # recomputed instead of unioning old restart memberships.
        cache=script.index('write_restart')
        self.assertIn('group __alr_scan_complete_type1 delete',script[:cache])
        self.assertIn('group __alr_scan_complete_type2 delete',script[:cache])
        self.assertIn('AUTO_LAMMPS_SCAN complete state',script)
        self.assertIn('$(step-v___alr_scan_complete_start:%.17g)',script)

    def test_original_host_uses_explicit_variant_ID_mapping(self):
        spec=specification()
        spec['variants']=[{'id':7,'type':1},{'id':11,'type':2},{'id':99,'type':None}]
        script=expand_tools(workflow(spec),plan(spec),'/output/',lower_cycles=True,reload_header=HEADER)
        self.assertIn('(type[v___alr_scan_complete_site]==1)*7',script)
        self.assertIn('(type[v___alr_scan_complete_site]==2)*11',script)

    def test_scope_reduction_duplicate_variants_and_unbounded_parameters_reject(self):
        for change in [lambda s:s.update(atom_count=2),lambda s:s.update(elements=['Ni','Cu']),
                       lambda s:s.update(variants=s['variants'][:1]),
                       lambda s:s['variants'][1].update(id=0),
                       lambda s:s['variants'][1].update(type=1),
                       lambda s:s['variant_relaxation'].update(max_evaluations=0),
                       lambda s:s['variant_relaxation'].update(ftol=float('nan')),
                       lambda s:s['variant_relaxation'].update(min_style=[]),
                       lambda s:s['variant_relaxation']['convergence'].update(force_metric=[]),
                       lambda s:s['variant_relaxation']['box'].update(vmax=0),
                       lambda s:s['variant_relaxation'].update(kinetic_energy='unchanged'),
                       lambda s:s.update(cache_file='../restart'),
                       lambda s:s.update(baseline_file='states.dump')]:
            spec=specification();change(spec)
            with self.subTest(spec=spec),self.assertRaises((CandidateError,ValueError)):
                self.check(workflow(spec))

    def test_native_paths_nested_scans_and_multiple_output_writers_reject(self):
        bodies=[workflow()+'\nread_restart /output/baseline.restart',
                workflow()+'\nset atom 1 type 2',workflow()+'\nwrite_restart /output/other.restart',
                workflow()+'\nprint "1" append /output/sites.dat',
                workflow().replace('save_state saved','save_state ../saved'),
                workflow().replace('save_state saved states.dump 1000 1000','save_state saved states.dump ${step} 1000'),
                workflow().replace('end_cycle production','save_state other other.dump 1000 1000\nend_cycle production'),
                workflow().replace('unfix evolution\n',''),
                'group defect id 3\ndelete_atoms group defect compress no\n'+workflow(),
                workflow().replace('end_cycle production','scan_sites nested '+shlex.quote(json.dumps(specification()))+'\nend_cycle production')]
        for body in bodies:
            with self.subTest(body=body[:120]),self.assertRaises((CandidateError,ValueError)):
                self.check(body)
        with self.assertRaises(CandidateError):self.check(outputs=OUTPUTS[:-1])

    def test_literal_technical_bounds_and_table_contract_fail_closed(self):
        for count in (0,257,1000000):
            with self.assertRaises((CandidateError,ValueError)):self.check(workflow(count=count))
        with self.assertRaisesRegex(ValueError,'minimization bound'):
            state_scan_metadata(workflow(specification(100000),50))
        bad=plan();bad['tables'][0]['columns'][4]['unit']='1'
        with self.assertRaisesRegex(ValueError,'columns and units'):expand_tools(workflow(),bad,'/output/')
        with self.assertRaisesRegex(ValueError,'header'):expand_tools(workflow(),plan(),'/output/',lower_cycles=True)
        with self.assertRaisesRegex(ValueError,'units'):
            expand_tools(workflow(),plan(),'/output/',lower_cycles=True,reload_header=['units real',*HEADER[1:]])

    def test_active_context_and_table_writer_check_expose_the_same_full_protocol(self):
        messages=candidate_messages('Complete synthetic alloy scan',units='metal',resource_summaries=[],max_atoms=2000,packages=('MC',))
        context=json.loads(messages[1]['content'])['workflow_adapter']
        self.assertEqual(context,workflow_tool_context())
        self.assertIn('scan_sites',context['operations'])
        self.assertIn('run_schedule',context['operations'])
        self.assertEqual(context['explicit_sampling']['scan_steps'],'same_complete_list')
        self.assertIn("run_schedule sampled 0 '[14,35,105]'",messages[0]['content'])
        self.assertIn('ONE accounted HPC',messages[0]['content'])
        check_table_writers(expand_tools(workflow(),plan(),'/output/'),plan(),'/output/')

    def test_analysis_cannot_subsample_full_scan_or_change_its_diagnostics(self):
        frozen=plan();frozen['operations']=[thermodynamics_operation()]
        expand_tools(workflow(),frozen,'/output/')
        mutations=[lambda op:op['states'].update(last=1),lambda op:op['sites'].update(stride=2),
                   lambda op:op.update(vacancy_variant=9),lambda op:op.update(elements={'0':'Ni','1':'Cu'}),
                   lambda op:op['convergence'].update(force_max_eV_per_A=1e-6),
                   lambda op:op['convergence'].update(max_evaluations=9001),
                   lambda op:op.update(pressure_GPa=1)]
        for mutate in mutations:
            changed=deepcopy(frozen);mutate(changed['operations'][0])
            with self.subTest(changed=changed),self.assertRaisesRegex(ValueError,'SAME'):
                expand_tools(workflow(),changed,'/output/')


class CompleteStateSiteFrozenTests(unittest.TestCase):
    setUp=fixture.AtomSwapCandidateTests.setUp
    generate=fixture.AtomSwapCandidateTests.generate
    configure=fixture.AtomSwapCandidateTests.configure

    def test_freezer_and_submit_recheck_reconstruct_complete_compiler_bytes(self):
        self.configure()
        self.value['workflow']=workflow()
        self.value['analysis'].update(files=OUTPUTS,plan={'tables':[{'file':'sites.dat','format':'site_scan_array_v1',
            'columns':scan_columns(['Cu','Ni'])}], 'operations':[thermodynamics_operation()]})
        with patch('subprocess.Popen',side_effect=AssertionError('No physics, SSH or paid model')):
            result=self.generate()
            rechecked=candidate_check(result['snapshot'],max_atoms=100000)
        saved=json.loads((result['snapshot'].path/'generation.json').read_text())
        self.assertEqual(rechecked['screen'],saved['script_screen'])
        self.assertEqual(saved['script_screen']['state_site_scan']['scans'][0]['variant_minimizations'],18)
        script=(result['snapshot'].path/'in.lammps').read_text()
        self.assertIn('read_restart /output/baseline.restart',script)
        self.assertNotIn('scan_sites complete',script)
        self.assertFalse(saved['execution_authorized'])

    def test_full_fifty_state_1024_site_scope_freezes_and_rebuilds_through_ordinary_service(self):
        self.configure()
        from test_structures import SPEC
        self.value['structure']=deepcopy(SPEC)
        self.value['structure'].update(crystal='bcc',repeat=[8,8,8],type_elements=['Cu','Ni'],masses_amu=[63.5,58.7],
            assignment={'mode':'random_counts','counts':[512,512],'seed':43210})
        spec=specification(1024)
        self.value['workflow']=workflow(spec,50,20000)
        operation=thermodynamics_operation(50,1024)
        operation['expected_counts']={'0':512,'1':512}
        self.value['analysis'].update(files=OUTPUTS,plan={'tables':[{'file':'sites.dat','format':'site_scan_array_v1',
            'columns':scan_columns(['Cu','Ni'])}], 'operations':[operation]})
        import ase
        from ase.io.lammpsdata import read_lammps_data
        from io import StringIO
        import numpy as np
        with patch('subprocess.Popen',side_effect=AssertionError('No target physics or real model')), \
             patch.object(ase.Atoms,'get_potential_energy',side_effect=AssertionError('HPC-only target energy')), \
             patch.object(ase.Atoms,'get_forces',side_effect=AssertionError('HPC-only target forces')), \
             patch.object(ase.Atoms,'get_stress',side_effect=AssertionError('HPC-only target stress')):
            result=self.generate();check=candidate_check(result['snapshot'],max_atoms=100000)
            atoms=read_lammps_data(StringIO((result['snapshot'].path/'structure.data').read_text()),
                                  Z_of_type={1:29,2:28},atom_style='atomic',units='metal')
        self.assertTrue(np.isfinite(atoms.positions).all())
        self.assertTrue(np.isfinite(atoms.cell).all())
        self.assertTrue(np.isfinite(atoms.get_masses()).all())
        self.assertEqual(atoms.get_chemical_symbols().count('Cu'),512)
        self.assertEqual(atoms.get_chemical_symbols().count('Ni'),512)
        scan=check['screen']['state_site_scan']['scans'][0]
        self.assertEqual(scan['rows'],204800)
        self.assertEqual(scan['variant_minimizations'],153600)
        self.assertEqual(check['screen']['calculation_commands'],153700)
        saved=json.loads((result['snapshot'].path/'generation.json').read_text())
        self.assertEqual(saved['geometry_receipt']['atom_count'],1024)
        self.assertEqual(saved['script_screen'],check['screen'])
        self.assertEqual(len(check['outputs']),7)
        self.assertEqual(self.transport.call_count,1)  # synthetic response only

    def test_all_explicit_fifty_logarithmic_states_freeze_and_recheck_without_approximating_steps(self):
        self.configure()
        from test_structures import SPEC
        self.value['structure']=deepcopy(SPEC)
        self.value['structure'].update(crystal='bcc',repeat=[8,8,8],type_elements=['Cu','Ni'],masses_amu=[63.5,58.7],
            assignment={'mode':'random_counts','counts':[512,512],'seed':43210})
        # Complete synthetic logarithmic input; no reference code or target values.
        steps=[round(20*1.25**index) for index in range(50)]
        spec=specification(1024)
        self.value['workflow']=scheduled_workflow(spec,steps=steps)
        operation=thermodynamics_operation(50,1024)
        operation['expected_counts']={'0':512,'1':512}
        self.value['analysis'].update(files=OUTPUTS,plan={'tables':[{'file':'sites.dat','format':'site_scan_array_v1',
            'columns':scan_columns(['Cu','Ni'])}], 'operations':[operation]})
        with patch('subprocess.Popen',side_effect=AssertionError('No target physics or real model')):
            result=self.generate();check=candidate_check(result['snapshot'],max_atoms=100000)
        state=check['screen']['state_site_scan']['states'][0]
        scan=check['screen']['state_site_scan']['scans'][0]
        self.assertEqual(state['steps'],steps)
        self.assertEqual(state['total_run_steps'],steps[-1])
        self.assertEqual(scan['source_timesteps'],steps)
        self.assertEqual(scan['rows'],204800)
        self.assertEqual(scan['variant_minimizations'],153600)
        self.assertEqual(check['screen']['calculation_commands'],153700)
        script=(result['snapshot'].path/'in.lammps').read_text()
        self.assertEqual([int(words[1]) for line in script.splitlines()
            if (words:=shlex.split(line)) and words[0]=='run'],
            [last-first for first,last in zip([0,*steps[:-1]],steps)])
        self.assertEqual(script.count('write_dump all custom /output/states.dump'),50)
        self.assertIn('variable __alr_scan_complete_step index '+ ' '.join(map(str,steps)),script)
        self.assertEqual(self.transport.call_count,1)


if __name__=='__main__':
    unittest.main()
