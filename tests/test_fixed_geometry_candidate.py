"""Ordinary candidates use pinned synthetic HPC geometry, never local physics."""
from copy import deepcopy
import json
import shutil
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from auto_lammps.agent_candidates import (CandidateError, generate_candidate_draft,
    generate_research_candidate, research_inputs, validate_proposal)
from auto_lammps.authorization import candidate_check
from auto_lammps.deepseek import DeepSeekClient, DeepSeekConfig, ModelCalls
from auto_lammps.geometry_catalog import register_atomic_geometry
from auto_lammps.ledger import Conflict, Resources
from auto_lammps.manifest import canonical, freeze, sha256
from auto_lammps.potentials import PotentialAdapter, PotentialCatalog
from test_atomic_structure_data import DATA, OPTIONS
from test_deepseek import response
from test_eam_potentials import FILES, METADATA, MODEL


class FixedGeometryCandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        source = self.root/'source'; source.mkdir()
        (source/'initial.data').write_bytes(DATA)
        asset = register_atomic_geometry(source, 'initial.data', self.root/'geometry',
            **{**OPTIONS, 'type_elements':['Ni', 'Co']})
        self.selected = {'catalog_sha256':asset['catalog_sha256'],
            'entry':{k:asset[k] for k in ('pin','size','sha256','summary')}}
        (source/FILES['model']).write_bytes(MODEL)
        (source/FILES['license']).write_text('Synthetic fixture, not a physical model\n')
        catalog = PotentialCatalog(self.root/'potentials')
        self.pin = catalog.import_model(source, metadata=METADATA, files=FILES)
        self.adapter = PotentialAdapter(catalog, allowed_pins=[self.pin],
            software_sha256='b'*64, packages=['MANYBODY'])
        self.value = dict(summary='Synthetic fixed-input candidate', questions=[],
            structure=dict(builder='frozen_hpc_atomic_data',pin=self.selected['entry']['pin']),
            potential_pin=self.pin, workflow='run 0\nprint "# columns: step energy" file /output/result.dat\n'
                'print "# units: step eV" append /output/result.dat\n'
                'print "$(step) $(pe)" append /output/result.dat',
            analysis=dict(quantity='synthetic value',method='last at the declared stage',files=['result.dat'],
                plan=dict(tables=[dict(file='result.dat',columns=[dict(name='step',unit='step'),
                    dict(name='energy',unit='eV')])],operations=[dict(id='last_energy',method='last',
                    file='result.dat',x='step',y='energy',window=[0,0])])) )
        self.calls = ModelCalls(self.root/'models.sqlite',DeepSeekConfig('synthetic-model'),max_requests=1)
        self.transport = Mock(side_effect=lambda *args:(200,response(self.value)))
        self.client = DeepSeekClient(self.calls,transport=self.transport,key_reader=lambda:'synthetic-key')
        self.resources = Resources(1,60,1_000_000,1_000_000)

    def generate(self, **changes):
        return generate_candidate_draft(self.client,self.adapter,
            **dict(task_text='Synthetic allowed conditions; no reference answer.',units='metal',
                resources=self.resources,store=self.root/'candidates',max_atoms=100,
                condition_record_sha256='a'*64,require_analysis_plan=True,
                initial_geometry=self.selected)|changes)

    def refreeze(self, result, *, generation=None, record_changes=None, script=None):
        """Build a valid hash envelope so independent logical checks are exercised."""
        snapshot = result['snapshot']; manifest = snapshot.verify()
        folder = self.root/'changed'; folder.mkdir()
        files = {}
        for row in manifest['files']:
            if 'external_source' in row: continue
            target = folder/row['path']; target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes((snapshot.path/row['path']).read_bytes()); files[row['path']]=row['role']
        if generation is not None:
            (folder/'generation.json').write_bytes(canonical(generation))
        else:
            generation = result['generation']
        if script is not None: (folder/'in.lammps').write_bytes(script)
        record = deepcopy(next(row for row in manifest['files'] if row['role']=='structure'))
        record.update(record_changes or {})
        return freeze(folder,self.root/'tampered',files=files,entrypoint='in.lammps',
            resources=self.resources,external_files={'structure.data':record},
            provenance={**manifest['provenance'],'task_sha256':sha256(canonical(generation['input']))})

    def test_original_bytes_are_external_counted_and_not_rebuilt(self):
        with (patch('auto_lammps.agent_candidates.build_structure',side_effect=AssertionError('no geometry rebuild')),
              patch('auto_lammps.agent_candidates.geometry_runtime',side_effect=AssertionError('no ASE runtime'))):
            result = self.generate()
        snapshot=result['snapshot']; manifest=snapshot.verify()
        self.assertEqual(manifest['schema_version'],2)
        self.assertFalse((snapshot.path/'structure.data').exists())
        row=next(x for x in manifest['files'] if x['role']=='structure')
        self.assertEqual(row['sha256'],sha256(DATA)); self.assertEqual(row['size'],len(DATA))
        self.assertEqual(row['external_source'],dict(catalog_sha256=self.selected['catalog_sha256'],pin=self.selected['entry']['pin']))
        script=(snapshot.path/'in.lammps').read_text()
        self.assertIn('boundary p p f\nread_data structure.data\n',script)
        self.assertIn('model.eam.alloy Ni Co\n',script)
        self.assertEqual(result['generation']['geometry_receipt']['parser_version'],1)
        self.assertFalse(result['generation']['geometry_receipt']['physical_evaluation_performed'])
        self.assertEqual(candidate_check(snapshot,max_atoms=100)['scientific_status'],'not_evaluated')
        context=json.loads(json.loads(self.transport.call_args.args[0])['messages'][1]['content'])
        self.assertEqual(context['initial_geometry'],self.selected)
        self.assertFalse(context['geometry_adapter']['local_geometry_builder_allowed'])
        self.assertNotIn('scaled_positions',context['geometry_adapter'])

    def test_missing_file_masses_fail_before_a_paid_request(self):
        source=self.root/'missing'; source.mkdir()
        (source/'data').write_bytes(DATA.replace(b'Masses\n\n1 58.7 # deliberately not used to infer an element\n2 63.5\n\n',b''))
        asset=register_atomic_geometry(source,'data',self.root/'missing-catalog',**{**OPTIONS,'type_elements':['Ni','Co']})
        selected=dict(catalog_sha256=asset.pop('catalog_sha256'),entry=asset)
        with self.assertRaisesRegex(CandidateError,'no Masses'):
            self.generate(initial_geometry=selected)
        self.transport.assert_not_called(); self.assertEqual(self.calls.status()['used_requests'],0)

    def test_units_atom_limit_and_invalid_pin_fail_before_paid_request(self):
        for changes in ({'units':'real'},{'max_atoms':1},
            {'initial_geometry':{**self.selected,'entry':{**self.selected['entry'],'pin':'f'*64}}}):
            with self.subTest(changes=changes),self.assertRaises(CandidateError): self.generate(**changes)
        self.transport.assert_not_called()

    def test_model_cannot_swap_or_rebuild_frozen_geometry(self):
        self.value['structure']['pin']='f'*64
        with self.assertRaisesRegex(CandidateError,'exactly the frozen'):
            self.generate()
        self.assertFalse((self.root/'candidates').exists())
        self.assertEqual(self.transport.call_count,1)

    def test_model_cannot_add_new_geometry(self):
        from test_structures import SPEC
        self.value['additional_structures']=[dict(id='replacement',structure=deepcopy(SPEC))]
        with self.assertRaisesRegex(CandidateError,'does not authorize additional'):
            self.generate()
        self.assertFalse((self.root/'candidates').exists())

    def test_bounded_repair_keeps_the_same_asset_in_every_model_request(self):
        rejected=deepcopy(self.value); rejected['structure']['pin']='f'*64
        self.calls=ModelCalls(self.root/'repair.sqlite',DeepSeekConfig('synthetic-model'),max_requests=2)
        self.transport=Mock(side_effect=[(200,response(rejected)),(200,response(self.value))])
        self.client=DeepSeekClient(self.calls,transport=self.transport,key_reader=lambda:'synthetic-key')
        with patch('auto_lammps.agent_candidates.build_structure',side_effect=AssertionError('no rebuild')):
            result=self.generate()
        self.assertEqual(self.transport.call_count,2)
        for call in self.transport.call_args_list:
            context=json.loads(json.loads(call.args[0])['messages'][1]['content'])
            self.assertEqual(context['initial_geometry'],self.selected)
            self.assertEqual(context['geometry_adapter']['structure_contract'],self.value['structure'])
        candidate_check(result['snapshot'],max_atoms=100)

    def test_reset_restores_the_same_file_mapping_without_new_geometry(self):
        self.value['workflow']+='\nreset_structure initial\nrun 0'
        result=self.generate()
        script=(result['snapshot'].path/'in.lammps').read_text()
        self.assertEqual(script.count('read_data structure.data\n'),2)
        self.assertEqual(script.count('boundary p p f\n'),2)
        self.assertEqual(script.count(f'model.eam.alloy Ni Co\n'),2)
        candidate_check(result['snapshot'],max_atoms=100)

    def test_static_review_receives_the_fixed_metadata_and_actual_id_policy(self):
        from auto_lammps.plan_review import requirements
        review=dict(issues=[],coverage=[dict(requirement=item['id'],
                    evidence=[dict(source='rendered_script',quote='read_data structure.data')])
                    for item in requirements('Synthetic allowed conditions; no reference answer.')],
                    summary='Static synthetic check, not physical verification')
        self.calls=ModelCalls(self.root/'review.sqlite',DeepSeekConfig('synthetic-model'),max_requests=2)
        self.transport=Mock(side_effect=[(200,response(self.value)),(200,response(review))])
        self.client=DeepSeekClient(self.calls,transport=self.transport,key_reader=lambda:'synthetic-key')
        result=self.generate(review_plan=True)
        context=json.loads(json.loads(self.transport.call_args_list[1].args[0])['messages'][1]['content'])
        self.assertEqual(context['geometry_adapter']['initial_geometry'],self.selected)
        self.assertEqual(context['geometry_checks']['initial']['atom_count'],2)
        self.assertIn('original data-file particle IDs',context['geometry_order'])
        candidate_check(result['snapshot'],max_atoms=100)

    def test_native_output_cannot_overwrite_external_input(self):
        self.value['analysis']['files'].append('structure.data')
        self.value['workflow']=self.value['workflow'].replace('/output/','')+'\nwrite_data structure.data'
        with self.assertRaisesRegex(CandidateError,'collides with a frozen input'):
            self.generate(output_layout='working_directory')

    def test_authorizer_rechecks_receipt_context_external_pin_and_bytes(self):
        result=self.generate()
        cases=['receipt','message','external_pin','external_size','external_sha','script']
        for kind in cases:
            generation=deepcopy(result['generation']); record_changes={}; script=None
            if kind=='receipt': generation['geometry_receipt']['atom_count']=3
            if kind=='message':
                value=json.loads(generation['input']['messages'][1]['content'])
                value['initial_geometry']['entry']['pin']='f'*64
                generation['input']['messages'][1]['content']=canonical(value).decode()
            if kind=='external_pin': record_changes['external_source']=dict(catalog_sha256=self.selected['catalog_sha256'],pin='f'*64)
            if kind=='external_size': record_changes['size']=len(DATA)+1
            if kind=='external_sha': record_changes['sha256']='f'*64
            if kind=='script': script=(result['snapshot'].path/'in.lammps').read_bytes().replace(b'boundary p p f',b'boundary p p p')
            if (self.root/'changed').exists(): shutil.rmtree(self.root/'changed')
            with self.subTest(kind=kind):
                altered=self.refreeze(result,generation=generation,record_changes=record_changes,script=script)
                with self.assertRaises(Conflict): candidate_check(altered,max_atoms=100)

    def test_source_selection_is_bound_into_research_conditions(self):
        from auto_lammps.tasks import FIELDS, TaskStore
        from test_tasks import evidence
        tasks=TaskStore(self.root/'tasks.sqlite'); doc=tasks.create('unused','PRIVATE_PROMPT','research')
        for field in FIELDS:
            if field!='reference':
                doc=tasks.add_candidate(doc['id'],doc['revision'],field,evidence('metal' if field=='units' else 'synthetic condition'))
        doc=tasks.confirm(doc['id'],doc['revision'],[key for key in FIELDS if key!='reference'])
        doc=tasks.select_initial_geometry(doc['id'],doc['revision'],self.selected)
        doc=tasks.freeze(doc['id'],doc['revision'])
        inputs=research_inputs(tasks,doc['id'],doc['revision'])
        self.assertEqual(inputs['initial_geometry'],self.selected)
        result=generate_research_candidate(self.client,tasks,doc['id'],doc['revision'],self.adapter,
            resources=self.resources,store=self.root/'candidates',max_atoms=100)
        self.assertEqual(result['generation']['input']['initial_geometry'],self.selected)
        self.assertNotIn('PRIVATE_PROMPT',self.transport.call_args.args[0].decode())

    def test_no_selection_retains_legacy_geometry_contract_and_schema_one(self):
        from test_structures import SPEC
        self.value['structure']=deepcopy(SPEC)
        # A one-type synthetic EAM choice is valid for this legacy fixture.
        self.value['structure']['type_elements']=['Ni']; self.value['structure']['elements']=['Ni']
        result=self.generate(initial_geometry=None)
        self.assertEqual(result['snapshot'].verify()['schema_version'],1)
        self.assertNotIn('initial_geometry',result['generation']['input'])
        self.assertTrue((result['snapshot'].path/'structure.data').exists())
        candidate_check(result['snapshot'],max_atoms=100)
