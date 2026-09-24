"""Synthetic model output through real accounting, ASE, resources and freezing."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from auto_lammps.agent_candidates import CandidateError, generate_candidate_draft, generate_research_candidate, validate_body
from auto_lammps.deepseek import DeepSeekClient, DeepSeekConfig, ModelCalls, ModelError
from auto_lammps.ledger import Resources
from auto_lammps.potentials import PotentialAdapter, PotentialCatalog
from test_deepseek import response
from test_structures import SPEC
from auto_lammps.tasks import TaskStore, FIELDS
from test_tasks import evidence


class AgentCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / 'source'
        source.mkdir()
        (source / 'coeff').write_text('1 2\nCu 0.5 1\n0\n0\n')
        (source / 'param').write_text('rcutfac 4\ntwojmax 0\n')
        (source / 'LICENSE').write_text('Synthetic fixture, not a physical model')
        self.catalog = PotentialCatalog(self.root / 'potentials')
        self.pin = self.catalog.import_model(source, files={'coefficients': 'coeff', 'parameters': 'param', 'license': 'LICENSE'},
            metadata={'name': 'synthetic model', 'format': 'snap', 'elements': ['Cu'], 'units': 'metal',
                      'source': {'url': 'https://example.org/synthetic', 'revision': 'a' * 40, 'locator': 'test'},
                      'license': 'Apache-2.0', 'applicability': 'synthetic only',
                      'usage_evidence': 'Synthetic only. REFERENCE_SENTINEL_NOT_FOR_AGENT', 'interaction': 'standalone'})
        self.adapter = PotentialAdapter(self.catalog, allowed_pins=[self.pin], software_sha256='b' * 64, packages=['ML-SNAP'])
        self.value = {'summary': 'Synthetic packaging test only', 'questions': [], 'structure': deepcopy(SPEC),
                      'potential_pin': self.pin, 'workflow': 'thermo 1\nrun 0\nwrite_data /output/final.data',
                      'analysis': {'quantity': 'synthetic structure', 'method': 'geometry inventory only', 'files': ['final.data']}}
        self.calls = ModelCalls(self.root / 'models.sqlite', DeepSeekConfig('synthetic-model'), max_requests=1)
        self.transport = Mock(side_effect=lambda *args: (200, response(self.value)))
        self.client = DeepSeekClient(self.calls, transport=self.transport, key_reader=lambda: 'synthetic-key')
        self.resources = Resources(1, 60, 1000000, 1000000)

    def generate(self):
        return generate_candidate_draft(self.client, self.adapter, task_text='Synthetic permitted task; no expected answer.',
                                        units='metal', resources=self.resources, store=self.root / 'candidates')

    def test_typed_analysis_plan_is_bound_into_generated_snapshot(self):
        from test_analysis import PLAN
        self.value['analysis']={'quantity':'synthetic curve','method':'frozen synthetic arithmetic',
                                'files':['trajectory.dump'],'plan':deepcopy(PLAN)}
        self.value['workflow']='run 0\nprint "# columns: strain stress" file /output/trajectory.dump'
        with patch('subprocess.Popen',side_effect=AssertionError('no engine')):result=self.generate()
        snapshot=result['snapshot'];manifest=snapshot.verify()
        raw=(snapshot.path/'analysis.json').read_bytes()
        from auto_lammps.manifest import sha256
        self.assertEqual(manifest['provenance']['analysis_sha256'],sha256(raw))
        self.assertEqual(json.loads(raw)['implementation_status'],'numeric_tables_v1')
        self.assertEqual(json.loads(raw)['proposal']['plan'],PLAN)

    def test_reviewed_legacy_model_enters_agent_and_snapshot_with_conversion_receipt(self):
        metadata = self.catalog.read(self.pin)[0]['metadata']
        original = b'rcutfac 4\ntwojmax 0\nrfac0 0.99363\nrmin0 0\nbzeroflag 0\nquadraticflag 0\ndiagonalstyle 3\n'
        (self.root / 'source/param').write_bytes(original)
        pin = self.catalog.import_model(self.root / 'source', metadata=metadata,
                    files={'coefficients': 'coeff', 'parameters': 'param', 'license': 'LICENSE'})
        self.adapter = PotentialAdapter(self.catalog, allowed_pins=[pin], software_sha256='b' * 64, packages=['ML-SNAP'])
        self.value['potential_pin'] = pin
        with self.assertRaisesRegex(CandidateError, 'No allowlisted'):
            self.generate()
        self.transport.assert_not_called()
        self.adapter = PotentialAdapter(self.catalog, allowed_pins=[pin], software_sha256='b' * 64,
                                        packages=['ML-SNAP'], legacy_snap_pins=[pin])
        with patch('subprocess.Popen', side_effect=AssertionError('no physical evaluation')):
            result = self.generate()
        result['snapshot'].verify()
        saved = (result['snapshot'].path / f'potentials/{pin}/model.snapparam').read_bytes()
        self.assertEqual(saved, original.replace(b'diagonalstyle 3\n', b''))
        self.assertEqual(self.catalog.read(pin)[1]['parameters'], original)
        receipt = json.loads((result['snapshot'].path / 'generation.json').read_bytes())['potential_receipt']
        self.assertIn('compatibility_conversion', receipt)
        self.assertFalse(receipt['compatibility_conversion']['numerical_equivalence_verified'])
        self.assertFalse(receipt['execution_authorized'])
        self.assertEqual(self.transport.call_count, 1)

    def test_model_to_geometry_potential_and_immutable_candidate(self):
        with patch('subprocess.Popen', side_effect=AssertionError('no local process execution')):
            result = self.generate()
        manifest = result['snapshot'].verify()
        self.assertEqual(result['status'], 'candidate_prepared_review_required')
        self.assertFalse(result['execution_authorized'])
        self.assertEqual(len(manifest['files']), 7)
        script = (result['snapshot'].path / 'in.lammps').read_text()
        self.assertIn('read_data structure.data\npair_style snap\npair_coeff * *', script)
        self.assertTrue(script.endswith(self.value['workflow'] + '\n'))
        evidence = json.loads((result['snapshot'].path / 'generation.json').read_bytes())
        self.assertEqual(evidence['geometry_receipt']['atom_count'], 96)
        self.assertFalse(evidence['scientific_conditions_verified'])
        self.assertFalse(evidence['potential_receipt']['environment_verified'])
        request = json.loads(self.transport.call_args.args[0])
        self.assertNotIn('REFERENCE_SENTINEL_NOT_FOR_AGENT', json.dumps(request))
        self.assertEqual(self.calls.status()['used_requests'], 1)
        self.assertEqual(evidence['model_receipt']['output_sha256'], self.calls.history()[0]['receipt']['output_sha256'])

    def test_duplicate_and_restart_never_generate_again(self):
        self.generate()
        self.client = DeepSeekClient(ModelCalls.open_existing(self.calls.path), transport=self.transport,
                                     key_reader=lambda: 'synthetic-key')
        with self.assertRaisesRegex(ModelError, 'request_already_reserved'):
            self.generate()
        self.assertEqual(self.transport.call_count, 1)
        self.assertEqual(len(list((self.root / 'candidates').glob('[0-9a-f]' * 64))), 1)

    def test_missing_conditions_yield_questions_without_preparing_files(self):
        self.value.update(questions=['Please specify the lattice constant.'], structure=None, potential_pin=None,
                          workflow=None, analysis=None)
        result = self.generate()
        self.assertEqual(result['status'], 'clarification_required')
        self.assertNotIn('snapshot', result)
        self.assertEqual(self.calls.history()[0]['receipt']['structured_output'], self.value)

    def test_invalid_model_workflow_keeps_failed_proposal_without_retry(self):
        self.value['workflow'] = 'shell echo not-allowed\n' + self.value['workflow']
        with self.assertRaisesRegex(CandidateError, 'Unsupported workflow command'):
            self.generate()
        self.assertEqual(self.transport.call_count, 1)
        self.assertEqual(self.calls.history()[0]['receipt']['structured_output']['workflow'], self.value['workflow'])
        self.assertFalse((self.root / 'candidates').exists())

    def test_unavailable_resource_and_zero_budget_prevent_model_request(self):
        self.adapter = PotentialAdapter(self.catalog, allowed_pins=[], software_sha256='b' * 64, packages=['ML-SNAP'])
        with self.assertRaisesRegex(CandidateError, 'No allowlisted'):
            self.generate()
        self.transport.assert_not_called()
        self.adapter = PotentialAdapter(self.catalog, allowed_pins=[self.pin], software_sha256='b' * 64, packages=['ML-SNAP'])
        calls = ModelCalls(self.root / 'zero.sqlite', DeepSeekConfig('synthetic-model'))
        self.client = DeepSeekClient(calls, transport=self.transport, key_reader=Mock(side_effect=AssertionError('no key lookup')))
        with self.assertRaisesRegex(ModelError, 'model_budget_exhausted'):
            self.generate()
        self.transport.assert_not_called()

    def test_unknown_potential_never_reaches_geometry_builder(self):
        self.value['potential_pin'] = 'c' * 64
        with patch('auto_lammps.agent_candidates.build_structure') as build:
            with self.assertRaisesRegex(CandidateError, 'not supplied'):
                self.generate()
            build.assert_not_called()

    def test_output_and_dispatch_screen(self):
        for body in ('include author.in', 'python setup file evil.py', 'pair_style zero 10',
                     '${command} 1', 'jump SELF retry', 'run 0\nwrite_data ../../outside',
                     'run 0\nwrite_data /output/undeclared', 'run 0\nfix x all python/invoke 1 end_of_step fn',
                     'run 0\nvariable x python fn', 'run 0\nprint "x" file /tmp/result'):
            with self.subTest(body=body), self.assertRaises(CandidateError):
                validate_body(body, ['final.data'])
        self.assertEqual(validate_body('run 0\nprint "value" file /output/final.data', ['final.data'])['calculation_commands'], 1)

    def test_existing_research_task_uses_selected_conditions_without_free_prompt(self):
        tasks = TaskStore(self.root / 'tasks.sqlite')
        doc = tasks.create('unused title', 'UNSELECTED_PROMPT_SENTINEL', 'research')
        for field in FIELDS:
            if field != 'reference':
                doc = tasks.add_candidate(doc['id'], doc['revision'], field, evidence('metal' if field == 'units' else 'synthetic input'))
        doc = tasks.confirm(doc['id'], doc['revision'], [x for x in FIELDS if x != 'reference'])
        with self.assertRaisesRegex(CandidateError, 'Freeze'):
            generate_research_candidate(self.client, tasks, doc['id'], doc['revision'], self.adapter,
                                        resources=self.resources, store=self.root / 'candidates')
        self.transport.assert_not_called()
        doc = tasks.freeze(doc['id'], doc['revision'])
        result = generate_research_candidate(self.client, tasks, doc['id'], doc['revision'], self.adapter,
                                             resources=self.resources, store=self.root / 'candidates')
        self.assertFalse(result['execution_authorized'])
        self.assertEqual(result['generation']['input']['condition_record_sha256'], doc['record_sha256'])
        self.assertNotIn('UNSELECTED_PROMPT_SENTINEL', self.transport.call_args.args[0].decode())

    def test_reproduction_task_cannot_bypass_release_gate(self):
        tasks = TaskStore(self.root / 'tasks.sqlite')
        doc = tasks.create('reference-only task', 'private reference prompt', 'reproduction')
        for field in FIELDS:
            doc = tasks.add_candidate(doc['id'], doc['revision'], field, evidence('synthetic input'))
        doc = tasks.confirm(doc['id'], doc['revision'], list(FIELDS))
        doc = tasks.freeze(doc['id'], doc['revision'])
        with self.assertRaisesRegex(CandidateError, 'release and isolation'):
            generate_research_candidate(self.client, tasks, doc['id'], doc['revision'], self.adapter,
                                        resources=self.resources, store=self.root / 'candidates')
        self.transport.assert_not_called()


if __name__ == '__main__':
    unittest.main()
