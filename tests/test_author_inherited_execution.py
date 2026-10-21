"""Inherited execution modes and accounted input origins, inert processes only."""
from copy import deepcopy
from pathlib import Path
import unittest

from auto_lammps import runtime_launcher as runtime
from auto_lammps.author_reference_plan import validate_author_plan
from auto_lammps.manifest import sha256
import test_author_execution_package as package_fixture
import test_author_native_runtime as native_fixture
import test_author_validation_subset as validation_fixture


class InheritedAuthorExecutionTests(unittest.TestCase):
    def test_original_working_copy_gets_only_inherited_execute_mode_and_fixed_child_path(self):
        f=native_fixture.AuthorNativeRuntimeTests();f.setUp();self.addCleanup(f.doCleanups)
        profile=f.profile;profile['runtime_tree']=str(f.fixture.deployment_root)
        python=f.fixture.deployment_root/'fixed-python';python.write_bytes(b'inert child executable');python.chmod(0o400)
        profile['native_slurm']['files'][str(python)]=sha256(python.read_bytes())
        child=f.fixture.deployment_root/'child-bin';child.mkdir(mode=0o700);(child/'python').symlink_to(python)
        entry=deepcopy(f.package_fixture.report['invocations'][0]['entrypoint'])
        profile['author_reference']['execution_policy']=dict(schema_version=1,review_sha256='1'*64,
            file_modes=[dict(entry,mode=0o500)],child_bin_path=str(child),
            child_executables=[dict(name='python',path=str(python),sha256=sha256(python.read_bytes()))],accounted_inputs=[])
        f.fixture.rebind();f.package=f.fixture.package;f.request_id='f'*32;f.fixture.request_id=f.request_id
        f.fixture.receive();f.case=f.fixture.requests/f.request_id
        f.payload.update(request_id=f.request_id,manifest_sha256=f.package['package_sha256'],author_package_sha256=f.package['package_sha256'],
                         profile_sha256=sha256(f.fixture.profile_path.read_bytes()));f.sign()
        observed=[]
        def inert(argv,**kwargs):
            observed.append((Path(kwargs['cwd'])/entry['path']).stat().st_mode&0o777)
            return native_fixture.InertProcess(argv,**kwargs)
        stack,popen=f.guards(inert)
        with stack:result=f.execute()
        self.assertEqual(result['returncode'],0);self.assertEqual(observed,[0o500])
        self.assertEqual((f.case/entry['path']).stat().st_mode&0o777,0o400)
        self.assertIn(str(child),popen.call_args.args[0])

    def test_accounted_restart_is_required_input_but_cannot_become_an_author_entrypoint(self):
        f=package_fixture.AuthorExecutionPackageTests();f.setUp();self.addCleanup(f.doCleanups)
        file=dict(path='samples/one.restart',size=12,sha256='a'*64)
        receipt=dict(kind='accounted_reference_output',task_id=f.evidence['task_id'],request_id='b'*32,job_id='123',
            manifest_sha256='c'*64,receipt_sha256='d'*64,binding_sha256='e'*64,root='/synthetic-hpc/old-run',files=[file])
        f.evidence['reference_inputs']=[receipt]
        f.raw['invocations'][0]['required_files'].append(dict(path=file['path'],sha256=file['sha256']))
        f.raw['coverage'][0]['accounted_input_files']=[dict(path=file['path'],sha256=file['sha256'])]
        f.report=validate_author_plan(f.evidence,f.raw)
        f.files.append(dict(file,binding_sha256=receipt['binding_sha256'],source_path=file['path']))
        f.deployment['source_roots'].append(dict(binding_sha256=receipt['binding_sha256'],path=receipt['root']))
        f.deployment['execution_policy']=dict(schema_version=1,review_sha256='f'*64,file_modes=[],
            child_bin_path=None,child_executables=[],accounted_inputs=[receipt])
        package=f.compile();self.assertIn(file['path'],[row['path'] for row in package['package']['files']])
        f.raw['invocations'][0]['entrypoint']=dict(path=file['path'],sha256=file['sha256'])
        with self.assertRaises(ValueError):validate_author_plan(f.evidence,f.raw)
        f.files[-1]['binding_sha256']='4'*64
        with self.assertRaises(ValueError):f.compile()

    def test_bounded_raw_execution_retains_missing_postprocessing_without_claiming_scientific_success(self):
        f=validation_fixture.AuthorValidationPlanTests();f.setUp();self.addCleanup(f.doCleanups)
        post=next(row for row in f.fx.value['coverage'] if row['dependency_id']=='post-a')
        post['analysis_capability_ids']=[];post['missing']=['Synthetic unavailable postprocessing tool.']
        report=f.check()
        self.assertEqual(report['status'],'proposed')
        self.assertEqual(report['analysis_pending'],[dict(dependency_id='post-a',missing=post['missing'])])
        self.assertFalse(report['author_validation']['full_paper_success'])

    def test_unreviewed_or_writable_original_modes_cannot_enable_execution(self):
        f=package_fixture.AuthorExecutionPackageTests();f.setUp();self.addCleanup(f.doCleanups)
        entry=f.report['invocations'][0]['entrypoint']
        f.deployment['execution_policy']=dict(schema_version=1,review_sha256='1'*64,file_modes=[dict(entry,mode=0o777)],
            child_bin_path=None,child_executables=[],accounted_inputs=[])
        with self.assertRaises(runtime.ExecutionDenied):f.compile()


if __name__=='__main__':unittest.main()
