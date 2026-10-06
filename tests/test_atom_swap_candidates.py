"""MC candidate contracts and grant recheck; no physical model or engine call."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from auto_lammps.agent_candidates import CandidateError, candidate_messages, validate_body
from auto_lammps.authorization import candidate_check
from auto_lammps.candidate_jobs import CandidateService
from auto_lammps.deepseek import DeepSeekClient, DeepSeekConfig, ModelCalls
from auto_lammps.potentials import PotentialAdapter
from auto_lammps.tasks import TaskStore
import test_agent_candidates as candidate_fixture
from test_candidate_jobs import frozen_research


SWAP = 'fix exchange all atom/swap 7 3 34567 450.0 types 1 2 ke yes'
TAIL = '\nrun 21\nprint "0 -1" file /output/result.dat'


class AtomSwapSyntaxTests(unittest.TestCase):
    def check(self, body=SWAP+TAIL, *, packages=('MC',), type_count=3, **kwargs):
        return validate_body(body,['result.dat'],packages=packages,type_count=type_count,**kwargs)

    def test_record_independent_pair_schedules_and_counters_without_rewriting(self):
        other='fix other_pair all atom/swap 11 5 45678 650 ke no semi-grand no types 2 3'
        record=self.check(SWAP+'\n'+other+TAIL)
        req=record['workflow_requirements']
        self.assertEqual(req['required_packages'],['MC'])
        self.assertFalse(req['environment_verified'])
        self.assertEqual(req['atom_swap_operations'][0],dict(fix_id='exchange',every_steps=7,
            attempts_per_event=3,seed=34567,temperature=450.0,types=[1,2],
            conserve_kinetic_energy=True,composition_preserved=True))
        self.assertEqual(req['atom_swap_operations'][1]['types'],[2,3])
        self.assertFalse(req['atom_swap_operations'][1]['conserve_kinetic_energy'])
        self.assertFalse(record['execution_authorized'])

    def test_missing_engine_package_or_types_rejects_before_preparation(self):
        for count,packages in [(3,()),(3,('MANYBODY',)),(None,('MC',)),(1,('MC',))]:
            with self.subTest(count=count,packages=packages),self.assertRaises(CandidateError):
                self.check(type_count=count,packages=packages)

    def test_invalid_different_ensemble_and_ambiguous_parameters_reject(self):
        bad=[SWAP.replace('types 1 2','types 1 1'),SWAP.replace('types 1 2','types 1 4'),
             SWAP.replace('types 1 2','types 1 2 3'),SWAP.replace('types 1 2','types Cu Ni'),
             SWAP.replace('types 1 2','types 1 -2'),SWAP+' semi-grand yes',SWAP+' mu 0 0',
             SWAP+' region selected',SWAP+' ke no',SWAP.replace('ke yes','ke maybe'),
             SWAP.replace('ke yes',''),SWAP.replace('all atom/swap','selected atom/swap'),
             SWAP.replace('450.0','nan'),SWAP.replace('450.0','inf'),SWAP.replace('450.0','0'),
             SWAP.replace('450.0','${T}'),SWAP.replace(' 7 3 ',' 0 3 '),
             SWAP.replace(' 7 3 ',' 7 0 '),SWAP.replace('34567','2147483648'),
             SWAP.replace('34567','9'*5000),SWAP.replace('types 1 2','types 1 '+('9'*5000)),
             SWAP.replace('34567','-1'),SWAP.replace('34567','1.5')]
        for body in bad:
            with self.subTest(body=body[:160]),self.assertRaises(CandidateError):self.check(body+TAIL)

    def test_schedule_reset_and_redefinition_require_explicit_unfix(self):
        for line in [SWAP,'fix exchange all nvt temp 450 450 0.1','reset_timestep 0']:
            with self.subTest(line=line),self.assertRaises(CandidateError):self.check(SWAP+'\n'+line+TAIL)
        self.check(SWAP+'\nunfix exchange\nreset_timestep 0\n'+SWAP+TAIL)

    def test_geometry_switch_clears_MC_lifecycle_but_retains_declared_operations(self):
        r=self.check(SWAP+'\nrun 21\nload_structure next_case\nreset_timestep 0\n'+SWAP+TAIL,
                     structures={'initial':3,'next_case':3})
        self.assertEqual(len(r['workflow_requirements']['atom_swap_operations']),2)

    def test_no_MC_keeps_old_screen_shape(self):
        r=self.check('run 0\nprint "0 -1" file /output/result.dat',packages=(),type_count=1)
        self.assertNotIn('workflow_requirements',r)

    def test_static_numeric_fix_ID_remains_supported(self):
        r=self.check(SWAP.replace('fix exchange','fix 2')+TAIL)
        self.assertEqual(r['workflow_requirements']['atom_swap_operations'][0]['fix_id'],'2')
        with self.assertRaisesRegex(CandidateError,'static identifier'):
            self.check(SWAP.replace('fix exchange','fix external/path')+TAIL)

    def test_prompt_exposes_actual_capability_and_composition_semantics(self):
        m=candidate_messages('Synthetic task',units='metal',resource_summaries=[],max_atoms=100,
                             packages=['MC','MANYBODY'])
        self.assertEqual(json.loads(m[1]['content'])['configured_engine_packages'],['MANYBODY','MC'])
        self.assertIn('X is attempts per event (NOT total cycles)',m[0]['content'])
        self.assertIn('ke must be explicit',m[0]['content'])
        self.assertIn('atom/swap is not invoked by minimize',m[0]['content'])


class AtomSwapCandidateTests(unittest.TestCase):
    setUp=candidate_fixture.AgentCandidateTests.setUp
    generate=candidate_fixture.AgentCandidateTests.generate

    def configure(self, packages=('MEAM','MC')):
        import test_meam_potentials as fixture
        from test_structures import EXPLICIT
        source=self.root/'source'
        for role in ('library','parameters'):
            (source/fixture.FILES[role]).write_bytes(getattr(fixture,role.upper()))
        pin=self.catalog.import_model(source,files=fixture.FILES,metadata=fixture.METADATA)
        self.value['structure']=deepcopy(EXPLICIT)
        self.value['structure'].update(site_elements=['Cu','Ni','Cu'],type_elements=['Cu','Ni'],masses_amu=[63.5,58.7])
        self.value['potential_pin']=pin
        self.value['workflow']=SWAP+'\nrun 21\nunfix exchange\nprint "# columns: x y" file /output/final.data\nprint "# units: 1 eV" append /output/final.data\nprint "0 -1" append /output/final.data'
        self.adapter=PotentialAdapter(self.catalog,allowed_pins=[pin],software_sha256='b'*64,packages=packages)

    def test_generation_freeze_and_automatic_submission_recheck_share_contract(self):
        self.configure()
        with patch('subprocess.Popen',side_effect=AssertionError('no engine or SSH')):
            result=self.generate()
            rechecked=candidate_check(result['snapshot'],max_atoms=100000)
        saved=json.loads((result['snapshot'].path/'generation.json').read_text())
        self.assertEqual(saved['input']['configured_engine_packages'],['MC','MEAM'])
        self.assertEqual(rechecked['screen'],saved['script_screen'])
        self.assertIn(SWAP,(result['snapshot'].path/'in.lammps').read_text())
        self.assertEqual(self.transport.call_count,1)
        self.assertFalse(saved['execution_authorized'])
        self.assertEqual(rechecked['scientific_status'],'not_evaluated')

    def test_missing_MC_preserves_raw_response_and_never_builds_or_freezes(self):
        self.configure(packages=('MEAM',))
        self.calls=ModelCalls(self.root/'rejected-models.sqlite',DeepSeekConfig('synthetic-model'),max_requests=2)
        self.client=DeepSeekClient(self.calls,transport=self.transport,key_reader=lambda:'synthetic-key')
        with patch('auto_lammps.agent_candidates.build_structure') as build,self.assertRaisesRegex(
                CandidateError,'unchanged rejected plan: atom/swap requires MC'):
            self.generate()
        build.assert_not_called()
        self.assertEqual(self.transport.call_count,2)
        self.assertEqual(self.calls.status()['used_requests'],2)
        self.assertEqual(self.calls.history()[0]['receipt']['structured_output'],self.value)
        self.assertFalse((self.root/'candidates').exists())

    def test_ordinary_candidate_service_keeps_MC_receipt_and_preparation_history(self):
        self.configure()
        tasks=TaskStore(self.root/'tasks.sqlite')
        task=frozen_research(tasks)
        service=CandidateService(tasks,self.client,self.adapter,resources=self.resources,
                                 snapshots=self.root/'candidates')
        self.addCleanup(lambda:service.close(wait=True))
        with patch.object(service.pool,'submit'),patch('subprocess.Popen',side_effect=AssertionError('no engine or SSH')):
            service.enqueue(task['id'],task['revision'])
            service.run(task['id'])
        job=service.history.get(task['id'])
        self.assertEqual(job['state'],'prepared',job['result'])
        self.assertIn('model_proposal',[event['state'] for event in job['events']])
        directory=service.snapshots/job['result']['snapshot_sha256']
        saved=json.loads((directory/'generation.json').read_text())
        self.assertEqual(saved['input']['configured_engine_packages'],['MC','MEAM'])
        self.assertTrue(saved['script_screen']['workflow_requirements']['atom_swap_operations'])
        self.assertIn(SWAP,(directory/'in.lammps').read_text())
        self.assertEqual(self.transport.call_count,1)
