"""Generic composition tools through the ordinary candidate path; no physics."""
from collections import Counter
from copy import deepcopy
from io import StringIO
import json
import unittest
from unittest.mock import Mock, patch

import numpy as np
from ase.io.lammpsdata import read_lammps_data

from auto_lammps.agent_candidates import candidate_messages, generate_candidate_draft
from auto_lammps.authorization import candidate_check
from auto_lammps.candidate_jobs import CandidateService
from auto_lammps.deepseek import DeepSeekClient, DeepSeekConfig, ModelCalls
from auto_lammps.manifest import Snapshot
from auto_lammps.structures import StructureError, geometry_tool_context, validate_structure
from auto_lammps.tasks import TaskStore
from test_structures import SPEC, EXPLICIT
import test_structures as geometry_fixture
import test_atom_swap_candidates as candidate_fixture
from test_candidate_jobs import frozen_research
from test_deepseek import response


def alloy_spec():
    spec=deepcopy(SPEC)
    spec.update(type_elements=['Ni','Cu'],masses_amu=[58.7,63.5],
                assignment={'mode':'random_counts','counts':[35,61],'seed':42})
    return spec


class AlloyGeometryTests(unittest.TestCase):
    build=geometry_fixture.StructureTests.build_without_physics

    def read(self, geometry):
        return read_lammps_data(StringIO(geometry.data.decode()),Z_of_type={1:28,2:29},
                               atom_style='atomic',units='metal')

    def test_exact_seeded_composition_mass_types_and_geometry_are_preserved(self):
        spec=alloy_spec(); before=deepcopy(spec)
        geometry=self.build(spec)
        atoms=self.read(geometry)
        base=read_lammps_data(StringIO(self.build(SPEC).data.decode()),Z_of_type={1:29},
                             atom_style='atomic',units='metal')
        self.assertEqual(Counter(atoms.get_chemical_symbols()),{'Ni':35,'Cu':61})
        np.testing.assert_allclose(atoms.cell,base.cell,rtol=0,atol=0)
        np.testing.assert_allclose(atoms.positions,base.positions,rtol=0,atol=0)
        np.testing.assert_allclose(atoms.get_masses(),[58.7 if s=='Ni' else 63.5 for s in atoms.symbols],atol=1e-10)
        self.assertEqual(spec,before)
        self.assertEqual(geometry.data,self.build(spec).data)
        self.assertEqual(geometry.receipt['type_elements'],['Ni','Cu'])
        record=geometry.receipt['composition_assignment']
        self.assertEqual(record['algorithm'],'sha256_rank_v1')
        self.assertFalse(record['physical_evaluation_performed'])
        self.assertFalse(geometry.receipt['scientifically_verified'])
        spec['assignment']['counts'][0]=999
        self.assertEqual(record['specification'],before['assignment'])

    def test_different_seeds_change_sites_without_changing_counts(self):
        spec=alloy_spec(); first=self.read(self.build(spec))
        spec['assignment']['seed']=0; second=self.read(self.build(spec))
        self.assertNotEqual(first.get_chemical_symbols(),second.get_chemical_symbols())
        self.assertEqual(Counter(first.symbols),Counter(second.symbols))
        np.testing.assert_array_equal(first.positions,second.positions)

    def test_layers_use_final_cell_axis_and_half_open_boundary(self):
        spec=alloy_spec()
        for axis in range(3):
            spec['assignment']={'mode':'fractional_layers','axis':axis,
                                'breaks':[0,.5,1],'elements':['Cu','Ni']}
            atoms=self.read(self.build(spec))
            fractions=atoms.get_scaled_positions(wrap=False)[:,axis]
            self.assertEqual(atoms.get_chemical_symbols(),['Cu' if p<.5 else 'Ni' for p in fractions])
            self.assertEqual(Counter(atoms.symbols),{'Cu':48,'Ni':48})

    def test_bcc_layers_and_substitution_vacancy_order(self):
        spec=alloy_spec()
        spec.update(crystal='bcc',repeat=[2,2,2],vacancies=[0,8],
                    substitutions=[{'site':1,'element':'Ni'}],
                    assignment={'mode':'fractional_layers','axis':0,
                                'breaks':[0,.5,1],'elements':['Cu','Ni']})
        geometry=self.build(spec); atoms=self.read(geometry)
        self.assertEqual(atoms[0].symbol,'Ni')
        self.assertEqual(geometry.receipt['original_site_count'],16)
        self.assertEqual(geometry.receipt['atom_count'],14)
        self.assertEqual(geometry.receipt['composition_assignment']['original_site_composition'],{'Cu':8,'Ni':8})
        self.assertEqual(geometry.receipt['composition'],{'Cu':6,'Ni':8})

    def test_many_sites_need_no_model_coordinate_or_substitution_enumeration(self):
        spec=alloy_spec()
        spec.update(repeat=[10,20,5],type_elements=['Cu','Ni','Al'],masses_amu=[63.5,58.7,27.0],
                    assignment={'mode':'random_counts','counts':[1000,1500,1500],'seed':123})
        self.assertEqual(validate_structure(spec),4000)
        geometry=self.build(spec)
        self.assertEqual(geometry.receipt['composition'],{'Cu':1000,'Ni':1500,'Al':1500})
        atoms=read_lammps_data(StringIO(geometry.data.decode()),Z_of_type={1:29,2:28,3:13},
                              atom_style='atomic',units='metal')
        self.assertTrue(np.isfinite(atoms.positions).all())
        self.assertTrue(np.isfinite(atoms.cell).all())
        self.assertTrue(np.isfinite(atoms.get_masses()).all())
        self.assertEqual(Counter(atoms.symbols),{'Cu':1000,'Ni':1500,'Al':1500})
        expected=[(np.array([ix,iy,iz])+basis)*4.0
                  for ix in range(10) for iy in range(20) for iz in range(5)
                  for basis in [(0,0,0),(0,.5,.5),(.5,0,.5),(.5,.5,0)]]
        np.testing.assert_allclose(atoms.positions,expected,rtol=0,atol=1e-13)
        np.testing.assert_array_equal(atoms.cell,[[40,0,0],[0,80,0],[0,0,20]])
        self.assertEqual(spec['substitutions'],[])
        self.assertNotIn('scaled_positions',spec)

    def test_invalid_counts_seed_and_layer_contracts_reject_before_allocation(self):
        bad=[None,[],{}, {'mode':'random_counts','counts':[35,60],'seed':42},
             {'mode':'random_counts','counts':[35.0,61],'seed':42},
             {'mode':'random_counts','counts':[0,96],'seed':42},
             {'mode':'random_counts','counts':[35,61],'seed':True},
             {'mode':'random_counts','counts':[35,61],'seed':-1},
             {'mode':'random_counts','counts':[35,61],'seed':4294967296},
             {'mode':'random_counts','counts':[35,61]},
             {'mode':'random_counts','counts':[35,61],'seed':42,'fractions':[.5,.5]},
             {'mode':'fractional_layers','axis':True,'breaks':[0,.5,1],'elements':['Cu','Ni']},
             {'mode':'fractional_layers','axis':3,'breaks':[0,.5,1],'elements':['Cu','Ni']},
             {'mode':'fractional_layers','axis':0,'breaks':[0,.5,.5,1],'elements':['Cu','Ni','Cu']},
             {'mode':'fractional_layers','axis':0,'breaks':[.1,.5,1],'elements':['Cu','Ni']},
             {'mode':'fractional_layers','axis':0,'breaks':[0,float('nan'),1],'elements':['Cu','Ni']},
             {'mode':'fractional_layers','axis':0,'breaks':[0,.5,1],'elements':['Cu','Fe']},
             {'mode':'fractional_layers','axis':0,'breaks':[0,.5,1],'elements':['Cu','Cu']},
             {'mode':'fractions','fractions':[.5,.5]}]
        with patch('ase.build.bulk',side_effect=AssertionError('no allocation')):
            for assignment in bad:
                with self.subTest(assignment=assignment),self.assertRaises(StructureError):
                    self.build({**alloy_spec(),'assignment':assignment})
            for spec in [{**EXPLICIT,'assignment':alloy_spec()['assignment']},
                         {**SPEC,'crystal':'diamond','assignment':{'mode':'random_counts','counts':[192],'seed':1}}]:
                with self.subTest(crystal=spec['crystal']),self.assertRaises(StructureError):self.build(spec)

    def test_thin_layer_with_no_sites_is_not_silently_rounded_or_filled(self):
        spec=alloy_spec(); spec['assignment']={'mode':'fractional_layers','axis':0,
                                             'breaks':[0,.99,1],'elements':['Cu','Ni']}
        with self.assertRaisesRegex(StructureError,'surviving structure species'):
            self.build(spec)

    def test_model_is_told_tool_order_and_does_not_need_a_hidden_template(self):
        message=candidate_messages('Synthetic task only',units='metal',resource_summaries=[],max_atoms=100)
        self.assertIn('sha256_rank_v1',message[0]['content'])
        self.assertIn('sum to all original lattice sites before defects',message[0]['content'])
        self.assertIn('No assignment is supported for explicit_cell',message[0]['content'])
        self.assertIn('Assignment is before substitutions, then vacancies',message[0]['content'])
        supplied=json.loads(message[1]['content'])['geometry_adapter']
        self.assertEqual(supplied,geometry_tool_context(100))
        supplied['cubic']['basis_counts']['bcc']=999
        self.assertEqual(geometry_tool_context(100)['cubic']['basis_counts']['bcc'],2)


class AlloyCandidateFlowTests(unittest.TestCase):
    setUp=candidate_fixture.AtomSwapCandidateTests.setUp
    configure=candidate_fixture.AtomSwapCandidateTests.configure

    def test_ordinary_candidate_freezes_geometry_and_rechecks_the_same_assignment(self):
        self.configure()
        spec=alloy_spec(); spec.update(type_elements=['Cu','Ni'],masses_amu=[63.5,58.7],
                                     assignment={'mode':'random_counts','counts':[61,35],'seed':42})
        self.value['structure']=spec
        tasks=TaskStore(self.root/'tasks.sqlite'); task=frozen_research(tasks)
        service=CandidateService(tasks,self.client,self.adapter,resources=self.resources,snapshots=self.root/'candidates')
        self.addCleanup(lambda:service.close(wait=True))
        with patch.object(service.pool,'submit'),patch('subprocess.Popen',side_effect=AssertionError('no engine or SSH')):
            service.enqueue(task['id'],task['revision']);service.run(task['id'])
            job=service.history.get(task['id'])
            self.assertEqual(job['state'],'prepared',job['result'])
            directory=service.snapshots/job['result']['snapshot_sha256']
            saved=json.loads((directory/'generation.json').read_text())
            self.assertEqual(saved['proposal']['structure']['assignment'],spec['assignment'])
            self.assertEqual(saved['geometry_receipt']['composition'],{'Cu':61,'Ni':35})
            self.assertEqual(saved['input']['geometry_adapter'],geometry_tool_context(100000))
            snapshot=Snapshot(directory,job['result']['snapshot_sha256'])
            checked=candidate_check(snapshot,max_atoms=100000)
            self.assertEqual(checked['screen'],saved['script_screen'])
            self.assertEqual(checked['scientific_status'],'not_evaluated')
        self.assertEqual(self.transport.call_count,1)

    def repaired(self, bad, good, task_text):
        """The same accounted path must turn actual tool errors into AI feedback."""
        calls=ModelCalls(self.root/'repair-models.sqlite',DeepSeekConfig('synthetic-model'),max_requests=3)
        review={'issues':[], 'coverage':[{'requirement':'Synthetic composition',
                                        'evidence':'Declared geometry and synthetic output'}],
                'summary':'Static consistency only'}
        transport=Mock(side_effect=[(200,response(bad)),(200,response(good)),(200,response(review))])
        client=DeepSeekClient(calls,transport=transport,key_reader=lambda:'synthetic-key')
        with patch('subprocess.Popen',side_effect=AssertionError('no physical engine or SSH')):
            result=generate_candidate_draft(client,self.adapter,task_text=task_text,units='metal',
                resources=self.resources,store=self.root/'repaired',max_atoms=100,
                require_analysis_plan=True,review_plan=True)
        self.assertEqual(calls.status()['used_requests'],3)
        requests=[json.loads(call.args[0]) for call in transport.call_args_list]
        for request in requests:
            user=json.loads(next(message['content'] for message in request['messages'] if message['role']=='user'))
            self.assertEqual(user['geometry_adapter'],geometry_tool_context(100))
        repair=json.loads(requests[1]['messages'][-1]['content'])
        self.assertIn('failure',repair)
        review_input=json.loads(requests[2]['messages'][-1]['content'])
        self.assertEqual(review_input['geometry_checks']['initial']['composition'],{'Cu':48,'Ni':48})
        self.assertFalse(review_input['geometry_checks']['initial']['physical_evaluation_performed'])
        saved=json.loads((result['snapshot'].path/'generation.json').read_text())
        self.assertEqual(saved['proposal'],good)
        self.assertEqual(saved['input']['geometry_adapter'],geometry_tool_context(100))
        self.assertEqual(candidate_check(result['snapshot'],max_atoms=100)['scientific_status'],'not_evaluated')
        return repair

    def test_invalid_composition_reaches_bounded_repair_with_same_active_rules(self):
        self.configure()
        good=deepcopy(self.value); good['structure']=alloy_spec()
        good['structure'].update(type_elements=['Cu','Ni'],masses_amu=[63.5,58.7],
                                 assignment={'mode':'random_counts','counts':[48,48],'seed':42})
        bad=deepcopy(good); bad['structure']['assignment']['counts']=[48,47]
        repair=self.repaired(bad,good,'Synthetic 96-site equal Cu/Ni alloy, random seed 42; no target answers.')
        self.assertIn('counts',repair['failure'])

    def test_valid_spec_with_empty_generated_layer_is_feedback_not_unhandled_failure(self):
        self.configure()
        good=deepcopy(self.value); good['structure']=alloy_spec()
        good['structure'].update(type_elements=['Cu','Ni'],masses_amu=[63.5,58.7],
                                 assignment={'mode':'fractional_layers','axis':0,
                                             'breaks':[0,.5,1],'elements':['Cu','Ni']})
        bad=deepcopy(good); bad['structure']['assignment']['breaks']=[0,.99,1]
        repair=self.repaired(bad,good,'Synthetic equal Cu/Ni half layers along x, split at cell fraction 0.5.')
        self.assertIn('surviving structure species',repair['failure'])


if __name__=='__main__':unittest.main()
