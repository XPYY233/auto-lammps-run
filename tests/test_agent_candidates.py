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
from test_tasks import evidence, target_ready


class AgentCandidateTests(unittest.TestCase):
    def test_native_output_layout_is_explicit_and_declared(self):
        body = ('variable strain equal 0\nvariable strain delete\n'
                'compute tensor all reduce sum c_stress[1]\n'
                'run 10\nwrite_data final.data')
        with self.assertRaises(CandidateError):
            validate_body(body, ['final.data'])
        result = validate_body(body, ['final.data'], output_prefix='')
        self.assertEqual(result['calculation_commands'], 1)
        with self.assertRaises(CandidateError):
            validate_body(body.replace('final.data', '../final.data'), ['final.data'], output_prefix='')

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
        self.value['analysis']['plan']={'tables':[{'file':'final.data','columns':[{'name':'x','unit':'1'},{'name':'y','unit':'eV'}]}], 'operations':[{'id':'value','method':'last','file':'final.data','x':'x','y':'y','window':[0,1]}]}
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

    def test_meam_uses_same_candidate_path_and_keeps_index_order(self):
        import test_meam_potentials as fixture
        from test_structures import EXPLICIT
        self.value['structure'] = deepcopy(EXPLICIT)
        source = self.root / 'source'
        (source / fixture.FILES['library']).write_bytes(fixture.LIBRARY)
        (source / fixture.FILES['parameters']).write_bytes(fixture.PARAMETERS)
        pin = self.catalog.import_model(source, metadata=fixture.METADATA, files=fixture.FILES)
        self.value['potential_pin'] = pin
        self.adapter = PotentialAdapter(self.catalog, allowed_pins=[pin], software_sha256='b' * 64,
                                        packages=['ML-SNAP'])
        with self.assertRaisesRegex(CandidateError, 'No allowlisted'):
            self.generate()
        self.transport.assert_not_called()
        self.adapter = PotentialAdapter(self.catalog, allowed_pins=[pin], software_sha256='b' * 64,
                                        packages=['MEAM'])
        with patch('subprocess.Popen', side_effect=AssertionError('no engine')):
            result = self.generate()
        result['snapshot'].verify()
        script = (result['snapshot'].path / 'in.lammps').read_text()
        self.assertIn(f'pair_coeff * * potentials/{pin}/library.meam Cu Ni potentials/{pin}/model.meam Cu\n', script)
        self.assertEqual((result['snapshot'].path / f'potentials/{pin}/model.meam').read_bytes(), fixture.PARAMETERS)
        record = json.loads((result['snapshot'].path / 'generation.json').read_bytes())
        self.assertEqual(record['potential_receipt']['library_index_elements'], ['Cu', 'Ni'])
        self.assertFalse(record['execution_authorized'])
        self.assertEqual(record['geometry_receipt']['builder'], 'ase.Atoms.explicit_cell')
        self.assertEqual(record['geometry_receipt']['atom_count'], 3)
        self.assertEqual(record['input']['generator_version'], 13)
        request = json.loads(self.transport.call_args.args[0])
        self.assertIn('meam', request['messages'][1]['content'])
        self.assertEqual(self.calls.status()['used_requests'], 1)

    def test_invalid_explicit_model_geometry_keeps_response_and_does_not_build(self):
        from test_structures import EXPLICIT
        from auto_lammps.structures import StructureError
        self.value['structure'] = deepcopy(EXPLICIT)
        self.value['structure']['cell_angstrom'][0][1] = 0.1
        with patch('auto_lammps.agent_candidates.build_structure') as build:
            with self.assertRaises(StructureError):
                self.generate()
            build.assert_not_called()
        self.assertEqual(self.calls.status()['used_requests'], 1)
        self.assertEqual(self.calls.history()[0]['receipt']['structured_output'], self.value)
        self.assertFalse((self.root / 'candidates').exists())

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

    def test_equal_expression_must_be_one_lammps_argument(self):
        tail='\nrun 0\nprint "value" file /output/final.data'
        with self.assertRaisesRegex(CandidateError, 'ONE expression'):
            validate_body('variable energy equal v_a - v_b'+tail, ['final.data'])
        validate_body('variable energy equal "v_a - v_b"'+tail, ['final.data'])

    def test_pressure_and_variable_issues_are_reported_together(self):
        body='thermo_style custom step press\nminimize 0 1e-6 10 100\ncompute p all pressure NULL virial\nvariable saved equal $(c_p)\nprint "${step} ${saved}" file /output/final.data'
        with self.assertRaises(CandidateError) as caught:
            validate_body(body,['final.data'])
        self.assertIn('Pressure computes',str(caught.exception))
        self.assertIn('Undefined LAMMPS',str(caught.exception))
        valid=('compute p all pressure NULL virial\n'+body.replace('compute p all pressure NULL virial\n','')).replace('${step}','$(step)')
        with self.assertRaisesRegex(CandidateError,'not current'):
            validate_body(valid,['final.data'])
        validate_body(valid.replace('step press','step press c_p'),['final.data'])
        with self.assertRaisesRegex(CandidateError,'not current'):
            validate_body(valid.replace('step press','step press c_p').replace('variable saved','reset_timestep 0\nvariable saved'),['final.data'])

    def test_review_issue_wrapper_preserves_blocking_content(self):
        from auto_lammps.agent_candidates import normalized_review_issues
        self.assertEqual(normalized_review_issues([{'issue':'actual failure'},{'error':'another failure'}]),
            ['actual failure','another failure'])
        self.assertEqual(normalized_review_issues([{'issue':'failure','extra':True}]),
            [{'issue':'failure','extra':True}])

    def test_immediate_formula_is_not_quoted_or_changed(self):
        body='run 0\nvariable e equal $(pe - 2)\nprint "${e}" file /output/final.data'
        validate_body(body,['final.data'])
        with self.assertRaisesRegex(CandidateError,'prevents'):
            validate_body(body.replace('$(pe - 2)','"$(pe - 2)"'),['final.data'])

    def test_thermo_keyword_is_not_a_named_variable(self):
        body='run 0\nprint "${step} 1" file /output/final.data'
        with self.assertRaisesRegex(CandidateError,'Undefined LAMMPS'):
            validate_body(body,['final.data'])
        validate_body(body.replace('${step}','$(step)'),['final.data'])
        validate_body('variable step equal step\n'+body,['final.data'])

    def test_index_variable_survives_structure_switch(self):
        body='variable n index 1\nload_structure second\nvariable n index 2\nrun 0\nprint "x" file /output/final.data'
        with self.assertRaisesRegex(CandidateError,'survives'):
            validate_body(body,['final.data'],structures={'initial':2,'second':4})
        validate_body(body.replace('variable n index 2','variable n delete\nvariable n index 2'),
                      ['final.data'],structures={'initial':2,'second':4})

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
        doc = target_ready(tasks, doc)
        doc = tasks.freeze(doc['id'], doc['revision'])
        with self.assertRaisesRegex(CandidateError, 'release and isolation'):
            generate_research_candidate(self.client, tasks, doc['id'], doc['revision'], self.adapter,
                                        resources=self.resources, store=self.root / 'candidates')
        self.transport.assert_not_called()


if __name__ == '__main__':
    unittest.main()

class PromptContractConformanceTests(unittest.TestCase):
    """提示词必须覆盖校验器可枚举的规则，否则规则会再次漂移（见 docs/CANDIDATE_CONTRACT_AUDIT.md）。"""

    def prompt(self):
        from auto_lammps.agent_candidates import candidate_messages
        messages = candidate_messages('Synthetic permitted task', units='metal',
                                      resource_summaries=[], max_atoms=100)
        return ' '.join(item['content'] for item in messages)

    def test_every_enumerable_validator_rule_is_stated(self):
        from auto_lammps.analysis import UNITS, METHODS, MAX_TABLES, MIN_COLUMNS, MAX_COLUMNS, MAX_OPERATIONS
        from auto_lammps.agent_candidates import COMMANDS, FIX_STYLES, COMPUTE_STYLES
        text = self.prompt()
        for unit in UNITS:
            self.assertIn(unit, text, '单位未在提示词中列出: ' + unit)
        for method in METHODS:
            self.assertIn(method, text, '分析方法未在提示词中列出: ' + method)
        for name in COMMANDS | FIX_STYLES | COMPUTE_STYLES:
            self.assertIn(name, text, '允许的 LAMMPS 命令/样式未在提示词中列出: ' + name)
        for limit in (MAX_TABLES, MIN_COLUMNS, MAX_COLUMNS, MAX_OPERATIONS):
            self.assertIn(str(limit), text, '数量上限未在提示词中给出: ' + str(limit))
        for phrase in ('columns', 'analysis.files', 'verbatim', 'cubic_axes', '# columns:', '# units:'):
            self.assertIn(phrase, text, '结构性规则未在提示词中说明: ' + phrase)

    def test_limits_come_from_the_validator_not_a_copy(self):
        from auto_lammps import analysis, analysis_v2, agent_candidates
        self.assertEqual(analysis_v2.MAX_TABLES, analysis.MAX_TABLES)
        text = self.prompt()
        self.assertIn(str(analysis.MIN_COLUMNS), text)
        self.assertIn(str(analysis.MAX_COLUMNS), text)

class LammpsAppendIdiomTests(unittest.TestCase):
    """LAMMPS print append takes a filename, not a boolean flag."""
    def test_append_requires_declared_filename(self):
        body='run 0\nprint "# columns: x y" file /output/table.dat\nprint "# units: 1 eV" append /output/table.dat\nprint "0 -1" append /output/table.dat'
        self.assertEqual(validate_body(body,['table.dat'])['declared_outputs'],['table.dat'])
        for suffix in ('append','append yes','append /outside.dat'):
            with self.assertRaises(CandidateError):validate_body('run 0\nprint "0 -1" file /output/table.dat '+suffix,['table.dat'])

class AnalysisFileNormalizationTests(unittest.TestCase):
    """模型常把 /output/ 前缀写进 analysis.files；归一为扁平基名，但仍拒绝重复与保留名。"""

    def proposal(self, files):
        return {'summary': 'synthetic', 'questions': [], 'potential_pin': 'a' * 64,
                'structure': deepcopy(SPEC), 'workflow': 'run 0\nwrite_data /output/final.data',
                'analysis': {'quantity': 'q', 'method': 'm', 'files': files}}

    def test_prefixed_names_are_normalized_to_basenames(self):
        from auto_lammps.agent_candidates import validate_proposal
        value = self.proposal(['/output/final.data'])
        validate_proposal(value, max_atoms=100000)
        self.assertEqual(value['analysis']['files'], ['final.data'])

    def test_duplicates_and_reserved_names_are_still_rejected(self):
        from auto_lammps.agent_candidates import CandidateError, validate_proposal
        with self.assertRaisesRegex(CandidateError, 'distinct flat filenames'):
            validate_proposal(self.proposal(['a.dat', '/output/a.dat']), max_atoms=100000)
        with self.assertRaisesRegex(CandidateError, 'distinct flat filenames'):
            validate_proposal(self.proposal(['stdout.txt']), max_atoms=100000)

class EmptyAnalysisPlanTests(unittest.TestCase):
    """空计划不含信息，按未提供处理；非空但不合法的计划仍被拒绝。"""

    def proposal(self, plan):
        return {'summary': 'synthetic', 'questions': [], 'potential_pin': 'a' * 64,
                'structure': deepcopy(SPEC), 'workflow': 'run 0\nwrite_data /output/final.data',
                'analysis': {'quantity': 'q', 'method': 'm', 'files': ['final.data'], 'plan': plan}}

    def test_empty_plan_is_rejected(self):
        from auto_lammps.agent_candidates import validate_proposal
        value = self.proposal({'tables': [], 'operations': []})
        with self.assertRaises(CandidateError):validate_proposal(value, max_atoms=100000)
        self.assertIn('plan', value['analysis'])

    def test_malformed_nonempty_plan_is_still_rejected(self):
        from auto_lammps.agent_candidates import CandidateError, validate_proposal
        # 有表也有操作，但表只有一列（契约要求 x 与 y 两列）→ 必须拒绝，不能被"空计划"规则放过。
        value = self.proposal({'tables': [{'file': 'final.data', 'columns': [{'name': 'x', 'unit': 'eV'}]}],
                               'operations': [{'id': 'last', 'method': 'last', 'file': 'final.data',
                                               'x': 'x', 'y': 'x', 'window': [0, 1]}]})
        with self.assertRaises(CandidateError):
            validate_proposal(value, max_atoms=100000)
