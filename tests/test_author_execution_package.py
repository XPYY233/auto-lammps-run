"""Metadata-only author execution package tests; never run a scientific engine."""
from copy import deepcopy
from dataclasses import asdict
from pathlib import PurePosixPath
import unittest
from unittest.mock import patch

from auto_lammps.author_execution import (AuthorExecutionError, compile_author_package,
    hpc_copy_plan, validate_bound_author_package)
from auto_lammps.author_reference_plan import validate_author_plan
from auto_lammps.ledger import Resources
from auto_lammps.manifest import canonical, sha256
from auto_lammps import runtime_launcher as runtime
import test_author_reference_plan as plan_fixtures


class AuthorExecutionPackageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = plan_fixtures.AuthorReferencePlanTests()
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.evidence = deepcopy(self.fixture.evidence)
        self.raw = deepcopy(self.fixture.value)
        self.report = deepcopy(validate_author_plan(self.evidence, self.raw))
        self.resources = Resources(32, 3600, 8 * 1024**3, 4 * 1024**2)
        self.provenance = dict(task_sha256='1'*64, analysis_sha256='2'*64, software_sha256='3'*64)
        self.files = [dict(row, binding_sha256='4'*64, source_path=row['path'])
                      for row in self.evidence['author_source']['files']]
        self.deployment = dict(schema_version=1, role='author_reference',
            profile_sha256='5'*64, approval_sha256='6'*64, execution_mode='native_slurm',
            formal_isolation=False, gpu_allowed=False, resources=asdict(self.resources),
            source_roots=[dict(binding_sha256='4'*64, path='/synthetic-hpc/original')],
            environments=[dict(id='synthetic-env', sha256='a'*64, executables=[dict(
                id='synthetic-lmp', sha256='b'*64, kind='lammps', path='/synthetic-runtime/lmp',
                launch_mode='mpi', child_process_policy='no_children', review_sha256='7'*64,
                entrypoints=[deepcopy(self.report['invocations'][0]['entrypoint'])])])])

    def compile(self, **overrides):
        args = dict(evidence=self.evidence, source_files=self.files, deployment=self.deployment,
                    resources=self.resources, provenance=self.provenance)
        args.update(overrides)
        return compile_author_package(self.report, **args)

    def test_fixed_original_recipe_compiles_without_execution_or_local_source(self):
        before = deepcopy((self.report, self.evidence, self.files, self.deployment))
        with patch('subprocess.Popen') as popen, patch('subprocess.run') as run:
            result = self.compile()
        popen.assert_not_called(); run.assert_not_called()
        self.assertEqual(before, (self.report, self.evidence, self.files, self.deployment))
        package = result['package']
        self.assertEqual(result['package_sha256'], sha256(canonical(package)))
        self.assertFalse(result['can_dispatch']); self.assertFalse(result['execution_authorized'])
        self.assertFalse(result['physical_evaluation_performed'])
        self.assertEqual(package['role'], 'author_reference'); self.assertFalse(package['B_input'])
        self.assertFalse(package['original_files_rewritten'])
        self.assertEqual(package['invocations'][0]['argv'], self.raw['invocations'][0]['argv'])
        self.assertEqual(package['outputs'], [dict(path='values.csv', kind='numeric_table')])
        self.assertNotIn('generation.json', canonical(package).decode())
        self.assertNotIn('CandidateHistory', canonical(package).decode())

    def test_exact_HPC_copy_projection_retains_byte_pins(self):
        package = self.compile()
        plan = hpc_copy_plan(package, deployment=self.deployment, request_root='/synthetic-hpc/requests')
        self.assertFalse(plan['execution_authorized'])
        self.assertEqual(plan['total_input_bytes'], sum(row['size'] for row in self.files))
        for row in plan['copies']:
            self.assertEqual(row['source'], str(PurePosixPath('/synthetic-hpc/original') / row['destination']))
            original = next(item for item in self.files if item['path'] == row['destination'])
            self.assertEqual(row['sha256'], original['sha256'])

    def test_recompile_rejects_package_action_resource_and_role_tampering(self):
        package = self.compile()
        args = dict(report=self.report, evidence=self.evidence, source_files=self.files,
                    deployment=self.deployment, resources=self.resources, provenance=self.provenance)
        self.assertEqual(validate_bound_author_package(package, **args), package)
        for change in ('argv', 'resources', 'role', 'dispatch', 'hash'):
            altered = deepcopy(package)
            if change == 'argv': altered['package']['invocations'][0]['argv'].append('changed')
            elif change == 'resources': altered['package']['resources']['wall_seconds'] += 1
            elif change == 'role': altered['package']['role'] = 'agent'
            elif change == 'dispatch': altered['can_dispatch'] = True
            else: altered['package_sha256'] = '0'*64
            with self.subTest(change=change), self.assertRaises(AuthorExecutionError):
                validate_bound_author_package(altered, **args)

    def test_copy_projection_rejects_role_crossing_and_deployment_change(self):
        package = self.compile()
        for change in ('agent', 'grant', 'source', 'digest'):
            altered = deepcopy(package)
            if change == 'agent': altered['package']['role'] = 'agent'
            elif change == 'grant': altered['execution_authorized'] = True
            elif change == 'source': altered['package']['files'][0]['source_path'] = '../outside'
            else: altered['package_sha256'] = '0'*64
            with self.subTest(change=change), self.assertRaises(AuthorExecutionError):
                hpc_copy_plan(altered, deployment=self.deployment, request_root='/synthetic-hpc/requests')
        other = deepcopy(self.deployment); other['environments'][0]['executables'][0]['path'] = '/other/lmp'
        with self.assertRaises(AuthorExecutionError):
            hpc_copy_plan(package, deployment=other, request_root='/synthetic-hpc/requests')

    def test_unknown_environment_or_interpreter_never_guesses_an_executable_path(self):
        for change in ('environment-id', 'environment-sha', 'executable-id', 'executable-sha', 'kind', 'relative-path', 'entrypoint'):
            value = deepcopy(self.deployment)
            environment = value['environments'][0]; executable = environment['executables'][0]
            if change == 'environment-id': environment['id'] = 'unregistered'
            elif change == 'environment-sha': environment['sha256'] = '0'*64
            elif change == 'executable-id': executable['id'] = 'unknown-python'
            elif change == 'executable-sha': executable['sha256'] = '0'*64
            elif change == 'kind': executable['kind'] = 'python'
            elif change == 'relative-path': executable['path'] = 'python'
            else: executable['entrypoints'][0]['sha256'] = '0'*64
            with self.subTest(change=change), self.assertRaises(AuthorExecutionError):
                self.compile(deployment=value)

    def test_python_original_driver_requires_same_allocation_review(self):
        self.raw['invocations'][0]['argv'] = ['workflow/example.txt', 'literal-argument']
        self.evidence['deployment']['environments'][0]['executables'][0]['kind'] = 'python'
        self.report = validate_author_plan(self.evidence, self.raw)
        program = self.deployment['environments'][0]['executables'][0]
        program['kind'] = 'python'; program['launch_mode'] = 'direct'
        with self.assertRaisesRegex(AuthorExecutionError, 'same-allocation'):
            self.compile()
        program['child_process_policy'] = 'reviewed_same_allocation'
        result = self.compile()
        self.assertEqual(result['package']['invocations'][0]['argv'], self.raw['invocations'][0]['argv'])
        self.assertFalse(result['can_dispatch'])  # Review metadata is not runtime enforcement.

    def test_interpreter_actual_script_operand_rejects_inline_module_and_combined_options(self):
        for kind, argv in [('python',['-cprint(1)','workflow/example.txt']),
            ('python',['-m','some_module','workflow/example.txt']),
            ('python',['-msome_module','workflow/example.txt']),
            ('python',['-uB','workflow/example.txt']),('python',['-','workflow/example.txt']),
            ('shell',['-ec','inline command','workflow/example.txt']),
            ('shell',['--command=inline','workflow/example.txt']),('shell',['-s','workflow/example.txt'])]:
            with self.subTest(kind=kind,argv=argv),self.assertRaises(runtime.ExecutionDenied):
                runtime.validate_author_argv(kind,argv,working_directory='.',entrypoint='workflow/example.txt')
            self.evidence['deployment']['environments'][0]['executables'][0]['kind']=kind
            raw=deepcopy(self.raw); raw['invocations'][0]['argv']=argv
            with self.subTest(layer='raw-plan',kind=kind,argv=argv),self.assertRaises(ValueError):
                validate_author_plan(self.evidence,raw)

    def test_arguments_after_the_script_are_not_mistaken_for_interpreter_flags(self):
        for kind,startup in [('python',['-u','-B']),('shell',['-e','--noprofile'])]:
            argv=[*startup,'workflow/example.txt','-c','literal','-m','ordinary-script-parameter']
            self.assertEqual(runtime.validate_author_argv(kind,argv,working_directory='.',
                entrypoint='workflow/example.txt'),len(startup))
            self.evidence['deployment']['environments'][0]['executables'][0]['kind']=kind
            raw=deepcopy(self.raw); raw['invocations'][0]['argv']=argv
            report=validate_author_plan(self.evidence,raw)
            self.assertEqual(report['invocations'][0]['argv'],argv)

    def test_lammps_input_must_be_the_actual_unique_input_operand(self):
        for argv in (['-in','different.txt','workflow/example.txt'],
                     ['-in','different.txt','-in','workflow/example.txt'],
                     ['-inline','workflow/example.txt'],['workflow/example.txt']):
            with self.subTest(argv=argv),self.assertRaises(runtime.ExecutionDenied):
                runtime.validate_author_argv('lammps',argv,working_directory='.',entrypoint='workflow/example.txt')

    def test_original_file_hash_binding_and_path_cannot_be_substituted(self):
        for change in ('hash', 'size', 'binding', 'path', 'source-path', 'duplicate', 'absent-required'):
            rows = deepcopy(self.files)
            if change == 'hash': rows[0]['sha256'] = '0'*64
            elif change == 'size': rows[0]['size'] += 1
            elif change == 'binding': rows[0]['binding_sha256'] = '0'*64
            elif change == 'path': rows[0]['path'] = '../outside'
            elif change == 'source-path': rows[0]['source_path'] = '/absolute/file'
            elif change == 'duplicate': rows.append(deepcopy(rows[0]))
            else: rows = [row for row in rows if row['path'] != self.report['invocations'][0]['entrypoint']['path']]
            with self.subTest(change=change), self.assertRaises(AuthorExecutionError):
                self.compile(source_files=rows)

    def test_CPU_only_single_allocation_and_cluster_hard_limits_are_kept(self):
        for change in ('cores', 'wall', 'memory', 'gpu', 'role', 'isolation', 'mode'):
            deployment = deepcopy(self.deployment); resources = self.resources
            if change == 'cores': resources = Resources(33, 3600, 8*1024**3, 4*1024**2)
            elif change == 'wall': resources = Resources(32, 7*86400 + 1, 8*1024**3, 4*1024**2)
            elif change == 'memory': resources = Resources(32, 3600, 8*1024**3 + 1, 4*1024**2)
            elif change == 'gpu': deployment['gpu_allowed'] = True
            elif change == 'role': deployment['role'] = 'agent'
            elif change == 'isolation': deployment['formal_isolation'] = True
            else: deployment['execution_mode'] = 'isolated'
            with self.subTest(change=change), self.assertRaises(AuthorExecutionError):
                self.compile(resources=resources, deployment=deployment)
        smaller = deepcopy(self.deployment); smaller['resources']['wall_seconds'] -= 1
        with self.assertRaisesRegex(AuthorExecutionError, 'registered deployment'):
            self.compile(deployment=smaller)

    def test_incomplete_or_modified_report_cannot_become_an_executable_plan(self):
        for change in ('question', 'status', 'argv', 'directory', 'extra'):
            self.report = deepcopy(validate_author_plan(self.evidence, self.raw))
            if change == 'question': self.report['questions'].append('Missing source parameter')
            elif change == 'status': self.report['status'] = 'needs_clarification'
            elif change == 'argv': self.report['invocations'][0]['argv'].append('$(sbatch hidden.sh)')
            elif change == 'directory': self.report['invocations'][0]['working_directory'] = 'missing-directory'
            else: self.report['role'] = 'agent'
            with self.subTest(change=change), self.assertRaises(AuthorExecutionError):
                self.compile()

    def test_duplicate_original_actions_and_output_input_collisions_are_rejected(self):
        duplicate = deepcopy(self.raw); duplicate['invocations'].append(deepcopy(duplicate['invocations'][0]))
        duplicate['invocations'][1]['id'] = 'duplicate-action'
        with self.assertRaises(ValueError):
            validate_author_plan(self.evidence, duplicate)
        self.raw['invocations'][0]['output_files'][0]['path'] = self.raw['invocations'][0]['entrypoint']['path']
        self.report = validate_author_plan(self.evidence, self.raw)
        with self.assertRaisesRegex(AuthorExecutionError, 'collides'):
            self.compile()

    def test_source_root_and_request_storage_must_be_disjoint(self):
        package = self.compile()
        for request_root in ('/synthetic-hpc/original', '/synthetic-hpc/original/child', '/synthetic-hpc'):
            with self.subTest(request_root=request_root), self.assertRaises(AuthorExecutionError):
                hpc_copy_plan(package, deployment=self.deployment, request_root=request_root)

    def test_package_and_original_bytes_must_fit_reserved_storage(self):
        with self.assertRaisesRegex(AuthorExecutionError, 'storage reservation'):
            self.compile(resources=Resources(32, 3600, 8 * 1024**3, 1024))

    def test_nested_original_working_directory_and_output_are_preserved(self):
        self.raw['invocations'][0]['working_directory'] = 'workflow'
        self.raw['invocations'][0]['argv'] = ['-in', 'example.txt']
        self.report = validate_author_plan(self.evidence, self.raw)
        result = self.compile()
        invocation = result['package']['invocations'][0]
        self.assertEqual(invocation['working_directory'], 'workflow')
        self.assertEqual(invocation['argv'], ['-in', 'example.txt'])
        self.assertEqual(invocation['output_files'], [dict(path='workflow/values.csv', kind='numeric_table')])

    def runtime_profile(self):
        native = dict(scope='trusted_research', formal_isolation=False,
            storage_enforcement='collection_bound_only', max_ranks=32,
            shell_path='/synthetic-runtime/bash', bootstrap_path='/synthetic-runtime/modules.sh',
            modules=['synthetic/1'], mpi_path='/synthetic-runtime/mpirun', engine_path='/synthetic-runtime/lmp',
            files={'/synthetic-runtime/bash':'8'*64, '/synthetic-runtime/modules.sh':'9'*64,
                   '/synthetic-runtime/mpirun':'0'*64, '/synthetic-runtime/lmp':'b'*64})
        registered = deepcopy(self.deployment); registered.pop('profile_sha256')
        profile = dict(execution_mode='native_slurm', requests_root='/synthetic-hpc/requests',
            control_root='/synthetic-control', runtime_tree='/synthetic-runtime-tree',
            native_slurm=native, author_reference=registered)
        self.deployment['profile_sha256'] = sha256(canonical(profile))
        return profile

    def commands(self, profile, package):
        return runtime.author_reference_commands(profile, package['package'],
            package_sha256=package['package_sha256'], profile_sha256=sha256(canonical(profile)),
            work_root='/synthetic-hpc/requests/fixture/output', resources=asdict(self.resources))

    def test_same_native_launcher_resolves_exact_original_command_and_output(self):
        profile = self.runtime_profile(); package = self.compile()
        with patch.object(runtime.subprocess, 'Popen') as popen, patch.object(runtime.subprocess, 'run') as run:
            plan = self.commands(profile, package)
        popen.assert_not_called(); run.assert_not_called()
        self.assertEqual(plan['actions'][0]['argv'][-6:],
            ['/synthetic-runtime/mpirun', '-np', '32', '/synthetic-runtime/lmp', '-in', 'workflow/example.txt'])
        self.assertEqual(plan['actions'][0]['cwd'], '/synthetic-hpc/requests/fixture/output')
        self.assertEqual(plan['outputs'][0]['absolute_path'], '/synthetic-hpc/requests/fixture/output/values.csv')
        self.assertFalse(plan['execution_authorized']); self.assertFalse(plan['formal_isolation'])
        self.assertEqual(plan['actions'][0]['original_argv'], self.raw['invocations'][0]['argv'])

    def test_runtime_rejects_unregistered_driver_or_changed_package_before_process(self):
        profile = self.runtime_profile(); package = self.compile()
        for change in ('role', 'hash', 'cwd', 'duplicate', 'argv', 'input-output', 'missing-original', 'count'):
            altered = deepcopy(package)
            if change == 'role': altered['package']['role'] = 'agent'
            elif change == 'hash': altered['package_sha256'] = '0'*64
            elif change == 'cwd': altered['package']['invocations'][0]['working_directory'] = '../outside'
            elif change == 'duplicate': altered['package']['invocations'].append(deepcopy(altered['package']['invocations'][0]))
            elif change == 'argv': altered['package']['invocations'][0]['argv'] = ['-c', 'hidden command']
            elif change == 'input-output': altered['package']['invocations'][0]['output_files'][0]['path'] = 'workflow/example.txt'
            elif change == 'missing-original': altered['package']['files'] = []
            else: altered['package']['resources']['cores'] = 33
            if change != 'hash': altered['package_sha256'] = sha256(canonical(altered['package']))
            with self.subTest(change=change), patch.object(runtime.subprocess, 'Popen') as popen:
                with self.assertRaises(runtime.ExecutionDenied): self.commands(profile, altered)
                popen.assert_not_called()
        del profile['author_reference']
        with self.assertRaises(runtime.ExecutionDenied): self.commands(profile, package)

    def test_existing_native_agent_command_is_byte_for_byte_unchanged(self):
        profile = self.runtime_profile(); native = profile['native_slurm']
        command = runtime.native_command(native, entrypoint='workflow/example.txt', cores=32)
        self.assertEqual(command[-10:], ['/synthetic-runtime/mpirun', '-np', '32', '/synthetic-runtime/lmp',
            '-in', 'workflow/example.txt', '-log', 'log.lammps', '-screen', 'none'])

    def test_python_command_needs_a_real_pinned_interpreter_not_a_model_ID(self):
        self.raw['invocations'][0]['argv'] = ['workflow/example.txt']
        self.evidence['deployment']['environments'][0]['executables'][0]['kind'] = 'python'
        self.report = validate_author_plan(self.evidence, self.raw)
        program = self.deployment['environments'][0]['executables'][0]
        program.update(kind='python', path='/synthetic-runtime/python', launch_mode='direct',
                       child_process_policy='reviewed_same_allocation')
        profile = self.runtime_profile(); package = self.compile()
        with self.assertRaisesRegex(runtime.ExecutionDenied, 'byte-verified inventory'):
            self.commands(profile, package)
        profile['native_slurm']['files']['/synthetic-runtime/python'] = 'b'*64
        self.deployment['profile_sha256'] = sha256(canonical(profile)); package = self.compile()
        plan = self.commands(profile, package)
        self.assertEqual(plan['actions'][0]['argv'][-2:], ['/synthetic-runtime/python', 'workflow/example.txt'])
        self.assertNotIn('-np', plan['actions'][0]['argv'])


if __name__ == '__main__':
    unittest.main()
