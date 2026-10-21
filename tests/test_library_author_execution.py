"""First A uses original library bytes, never a prior job or real calculation."""
import base64
from copy import deepcopy
import io
import json
from pathlib import Path
import shlex
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from auto_lammps.author_lane_policy import InheritedAuthorResourcePolicy
from auto_lammps.author_execution import compile_author_package
from auto_lammps.author_reference_plan import validate_author_plan
from auto_lammps.ledger import Ledger, Policy
from auto_lammps.library_author_execution import LibraryAuthorResourcePolicy, SCHEDULER_CODE, _scheduler_metadata
from auto_lammps.manifest import canonical, sha256
from auto_lammps.remote_stage import receive_author
from auto_lammps.tasks import TaskError
import test_author_lane_deployment as lane_fixture
import test_reference_scope as scope_fixture
import test_selected_author_source_reader as selected_fixture


class LibraryAuthorExecutionTests(unittest.TestCase):
    def setUp(self):
        def empty_campaign(f, **kwargs):
            f.ledger=Ledger(f.root/'execution.sqlite')
            f.ledger.create_campaign('synthetic', Policy(max_cores=32,max_memory_bytes=8*1024**3,
                max_wall_seconds=604800,total_core_seconds=33177600,total_storage_bytes=16*1024**3,
                concurrency=1,approval_sha256='a'*64))
            f.papers.ledger=f.ledger
        self.f=lane_fixture.AuthorLaneDeploymentTests()
        with patch.object(scope_fixture.ReferenceScopeTests,'bind_run',empty_campaign):self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        f=self.f;self.task=f.task;self.lane=f.service;f.transport.revision=1
        f.workspace.planner=f.fx.service;f.workspace.lane_deployment=self.lane;f.workspace.execution=None
        engine=f.fx.input['deployment']['environments'][0]['executables'][0]
        engine['sha256']=f.profile_value['native_slurm']['files'][f.profile_value['native_slurm']['engine_path']]
        self.source=dict(reader=f.source,deployment=deepcopy(f.fx.input['deployment']),invocation_paths=['README.md'])
        f.workspace._metadata.side_effect=lambda _: (dict(self.source,reader=f.source),f.fx.paper,f.fx.reader.receipt,f.fx.scope)
        signing=f.root/'existing-signing';signing.mkdir(mode=0o700)
        (signing/'grant.key').write_bytes(b's'*32);(signing/'grant.key').chmod(0o600)
        f.controller.authorization.directory=signing
        f.controller.environment=SimpleNamespace(python_path=str(f.python),partition='synthetic',account=None)
        f.controller.following.analysis.collector.directory=f.root/'collections'
        helper=f.native_dir/'remote_submit.py';helper.write_bytes(b'Synthetic fixed scheduler helper');helper.chmod(0o400)
        launcher=f.native_dir/'runtime_launcher.py';launcher.write_bytes(b'Synthetic fixed runtime');launcher.chmod(0o400)
        sbatch=f.native_dir/'sbatch';sbatch.write_bytes(b'Synthetic scheduler, never executed');sbatch.chmod(0o400)
        config=dict(runtime_path=str(launcher),runtime_sha256=sha256(launcher.read_bytes()),
            sbatch_path=str(sbatch),sbatch_sha256=sha256(sbatch.read_bytes()))
        (f.native_dir/'submission.json').write_bytes(canonical(config));(f.native_dir/'submission.json').chmod(0o400)
        # Stage, submit and collect share a root but have distinct pinned helpers.
        for adapter,name in ((f.controller.submission.scheduler,helper),(f.controller.following.analysis.collector,launcher)):
            adapter.endpoint=SimpleNamespace(**vars(adapter.endpoint),helper_path=str(name),helper_sha256=sha256(name.read_bytes()))
        self.scheduler_request=dict(helper_path=str(helper),helper_sha256=sha256(helper.read_bytes()),
            runtime_path=str(launcher),runtime_sha256=sha256(launcher.read_bytes()))
        self.library=LibraryAuthorResourcePolicy(self.lane,campaign='synthetic',system_sha256='b'*64)
        self.old_inputs=Mock();self.old_provider=Mock(side_effect=AssertionError('No old A exists'))
        self.lane.policy=InheritedAuthorResourcePolicy(self.lane,self.old_inputs,
            resource_receipt_provider=self.old_provider,library_policy=self.library)
        self.metadata_reads=0
        def transfer(command,**kwargs):
            if shlex.split(command[1])[-1]==SCHEDULER_CODE:
                self.metadata_reads+=1
                result=_scheduler_metadata(json.loads(b''.join(kwargs['input_chunks'])))
                return dict(returncode=0,failure=None,stdout=base64.b64encode(base64.b64encode(canonical(result))+b'\n').decode())
            return f.transfer(command,**kwargs)
        f.capture.side_effect=transfer
        patcher=patch('auto_lammps.author_dispatch_config.SavedHPCTransport',lambda *args,**kwargs:f.transport)
        patcher.start();self.addCleanup(patcher.stop)

    def counts(self):
        with self.f.ledger._transaction() as db:
            return tuple(db.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('campaigns','evaluations','requests'))

    def ensure(self,request='9'*32):return self.lane.ensure_resources(self.task,request)

    def test_first_prepare_registers_reference_identity_without_old_request_or_model(self):
        self.assertEqual(self.counts(),(1,0,0))
        result=self.ensure();self.assertEqual(result['state'],'resources_ready')
        self.assertEqual(result['source'],'fixed_library_author_resource_bundle')
        self.assertEqual(self.counts(),(1,1,0));self.assertEqual(self.metadata_reads,1)
        self.old_provider.assert_not_called();self.old_inputs.status.assert_not_called()
        self.assertEqual(self.f.fx.sent,[]);self.assertEqual(self.f.fx.service.history(self.task),[])
        approved=self.library.approved(self.task)
        self.assertEqual(approved['candidate']['accounted_inputs'],[])
        proof=approved['review_receipt']
        self.assertEqual(proof['acquisition_receipt_sha256'],self.f.source.policy['receipt_sha256'])
        self.assertEqual(proof['scope_sha256'],self.f.fx.scope['scope_sha256'])
        self.assertFalse(proof['execution_authorized']);self.assertFalse(proof['scientific_validation'])
        self.assertEqual(self.ensure('a'*32)['state'],'resources_ready')
        self.assertEqual(self.counts(),(1,1,0));self.assertEqual(self.metadata_reads,1)
        self.assertIsNotNone(self.f.workspace.execution)

    def test_installed_complete_lane_stages_original_bytes_and_creates_private_writable_lock(self):
        self.ensure()
        latest=self.lane.status(self.task)['latest'];candidate=latest['candidate'];profile=candidate['profile']
        root=Path(profile['requests_root']);self.assertFalse((root/'.stage.lock').exists())
        self.assertEqual((root/'policy.json').stat().st_mode&0o777,0o400)
        binding=self.lane.planner_binding(self.task,self.source)
        self.f.fx.input['deployment']=binding['deployment']
        evidence=self.f.fx.service._input(self.task)
        value=deepcopy(self.f.fx.value)
        value['invocations'][0]['environment_id']=binding['deployment']['registered_execution_environment_ids'][0]
        report=validate_author_plan(evidence,value)
        registration=self.lane._registrations[self.task]
        package=compile_author_package(report,evidence=evidence,source_files=registration['source_files'],
            deployment=registration['deployment'],resources=registration['resources'],provenance=registration['provenance'])
        raw=canonical(package['package']);request='c'*32
        receipt=receive_author(str(root),request,package['package_sha256'],
            io.BytesIO(struct.pack('!I',len(raw))+raw),_profile_path=Path(profile['runtime_tree'])/'runtime.json')
        self.assertEqual(receipt['state'],'staged');self.assertFalse(receipt['physical_evaluation_performed'])
        self.assertEqual((root/'.stage.lock').stat().st_mode&0o777,0o600)
        for row in package['package']['files']:
            path=root/request/row['path'];self.assertEqual(sha256(path.read_bytes()),row['sha256'])
            self.assertEqual(path.stat().st_mode&0o777,0o400)
        # Dynamic receiver state does not invalidate or mutate immutable metadata.
        self.assertEqual(self.lane.reconcile(self.task,latest['request_id'])['state'],'metadata_installed')
        self.assertEqual(self.counts(),(1,1,0));self.assertEqual(self.f.fx.sent,[])

    def test_source_license_scope_and_engine_gaps_block_before_identity_or_install(self):
        for change in ('license','invocation','scope','engine'):
            with self.subTest(change=change):
                old=deepcopy((self.f.source.policy,self.source,self.f.fx.scope,self.f.environment))
                if change=='license':self.f.source.policy['license_approved']=False
                elif change=='invocation':self.source['invocation_paths']=['not-retained']
                elif change=='scope':self.f.fx.scope['task_id']='f'*32
                else:self.f.environment['report']['native_files'].pop()
                with self.assertRaises((TaskError,ValueError)):self.library.approved(self.task)
                self.f.source.policy,self.source,self.f.fx.scope,self.f.environment=old
        self.assertEqual(self.counts(),(1,0,0));self.f.capture.assert_not_called()

    def test_changed_scope_does_not_reset_identity_or_allow_old_run_in_first_branch(self):
        self.library.reference_evaluation(self.task,create=True);self.assertEqual(self.counts(),(1,1,0))
        self.f.fx.scope['scope_sha256']='f'*64
        with self.assertRaisesRegex(TaskError,'身份'):self.library.reference_evaluation(self.task,create=True)
        self.assertEqual(self.counts(),(1,1,0))
        self.source['execution_resource_receipt']={'request_id':'e'*32}
        self.assertFalse(self.lane.policy.is_first_library(self.task))
        with self.assertRaisesRegex(TaskError,'继承'):self.library.approved(self.task)

    def test_scheduler_reader_checks_exact_pins_without_invoking_scheduler(self):
        result=_scheduler_metadata(self.scheduler_request)
        self.assertEqual(set(result),{'sbatch_path','sbatch_sha256','original_config_sha256'})
        for field in ('helper_sha256','runtime_sha256'):
            bad=dict(self.scheduler_request,**{field:'f'*64})
            with self.assertRaises(ValueError):_scheduler_metadata(bad)

    def test_selected_library_uses_real_receipt_format_and_actual_selected_file_root(self):
        selected=selected_fixture.SelectedAuthorSourceReaderTests();selected.setUp();self.addCleanup(selected.doCleanups)
        selected.task=self.task;selected.policy['reference_task_id']=self.task
        selected.reader=selected.service();retained=selected.retained()
        # Preserve precisely the same saved identity used for the actual read.
        f=self.f;f.transport.identity=deepcopy(selected.reader.transport.identity)
        f.transport.current=f.transport.identity['revision'];f.transport.revision=f.transport.current
        selected.reader.transport=f.transport;f.source=selected.reader
        for adapter in (f.controller.staging.client,f.controller.submission.scheduler,
                        f.controller.following.analysis.collector,f.controller.following.reconciliation.reader):
            adapter.endpoint.host_alias=f.transport.identity['host_alias']
        self.source.update(reader=selected.reader,invocation_paths=[])
        f.workspace._metadata.side_effect=lambda _: (self.source,f.fx.paper,selected.receipt,f.fx.scope)
        f.workspace._cached_sources.side_effect=lambda *_:deepcopy(retained)
        result=self.ensure();self.assertEqual(result['state'],'resources_ready')
        proof=self.library.approved(self.task)['review_receipt']
        self.assertEqual(proof['inventory_sha256'],selected.receipt['inventory_sha256'])
        self.assertEqual(proof['acquisition_id'],selected.receipt['acquisition_id'])
        self.assertTrue(proof['invocation_paths'])
        candidate=self.lane.status(self.task)['latest']['candidate']
        self.assertEqual(candidate['profile']['author_reference']['source_roots'][0]['path'],selected.receipt['remote_directory']+'/source')
        self.assertEqual(self.counts(),(1,1,0));self.assertEqual(f.fx.sent,[])

    def test_full_original_scope_defers_analysis_but_never_missing_raw_physics(self):
        f=self.f.fx;evidence=f.service._input(self.task);value=deepcopy(f.value)
        post=next(row for row in value['coverage'] if row['dependency_id']=='post-a')
        post.update(analysis_capability_ids=[],missing=['Synthetic analysis Python remains unavailable.'])
        report=validate_author_plan(evidence,value)
        self.assertEqual(report['status'],'proposed');self.assertEqual(len(report['coverage']),3)
        self.assertEqual(report['analysis_pending'],[dict(dependency_id='post-a',missing=post['missing'])])
        self.assertFalse(report['full_paper_success'])
        value['coverage'][0]['missing']=['Synthetic raw input is missing.']
        with self.assertRaisesRegex(TaskError,'raw input'):validate_author_plan(evidence,value)

    def test_fixed_reading_disclaimers_stay_visible_but_real_method_gaps_remain_fatal(self):
        from auto_lammps.author_initial_workspace import _MISSING
        self.ensure();source=deepcopy(self.source)
        source['missing']=list(_MISSING)+['Synthetic original potential is missing.']
        source['deployment']['missing']=list(_MISSING)
        projected=self.lane.planner_binding(self.task,source)
        self.assertEqual(projected['missing'],['Synthetic original potential is missing.'])
        self.assertEqual(projected['deployment']['unavailable_capabilities'],list(_MISSING))
        self.assertEqual(self.counts(),(1,1,0));self.assertEqual(self.f.fx.sent,[])

    def test_new_lane_preserves_other_active_author_controller(self):
        from auto_lammps.author_reference_workspace import AuthorReferenceWorkspace
        previous=Mock();self.f.workspace.execution=previous
        self.ensure()
        current=self.f.workspace.execution_services[self.task]
        self.assertIsNot(current,previous);self.assertIs(self.f.workspace.execution,previous)
        self.assertIs(AuthorReferenceWorkspace._execution(self.f.workspace,self.task),current)
        self.assertIs(AuthorReferenceWorkspace._execution(self.f.workspace,'f'*32),previous)
        self.assertEqual(self.counts(),(1,1,0))

    def test_unconfigured_other_task_has_no_first_author_capability_or_state_changes(self):
        self.f.workspace._metadata.side_effect=TaskError('No approved original resource bundle.')
        self.assertFalse(self.lane.policy.is_first_library(self.task))
        self.assertEqual(self.counts(),(1,0,0));self.f.capture.assert_not_called()

    def test_source_bound_python_entry_is_registered_without_authorizing_child_driver(self):
        from auto_lammps.author_reference_plan import author_source_inputs
        from auto_lammps.author_python_dependencies import requirements_from_sources,project_capabilities
        f=self.f;reader=f.fx.reader
        reader.texts.update({'structure.py':b'import numpy as np\nx = np.array([1, 2])\n',
            'plot.py':b'import matplotlib.pyplot as plt\n',
            'driver.py':b'import subprocess\nsubprocess.run(["ssh", "not-permitted"])\n'})
        reader._write_sources();reader._bind_receipt()
        f.source=reader.service();f.source.transport=f.transport
        selections=[dict(path=name,start_line=1,end_line=20) for name in reader.texts]
        request=reader.request(selections)
        retained=dict(capability=f.source.capability(reader.receipt,purpose='author_reference'),
            acquisition_receipt=deepcopy(reader.receipt),reads=[dict(request=request,response=reader.worker(request))])
        f.workspace._cached_sources.side_effect=lambda *_:deepcopy(retained)
        requirements=requirements_from_sources(author_source_inputs(retained,self.task),self.task)
        raw=f.environment['report']['python_candidates'][0]
        raw.update(status='dependencies_unverified',distributions=[dict(name='numpy',state='installed',version='synthetic'),
            dict(name='matplotlib',state='missing',version=None)])
        f.environment.update(state='incomplete',report_sha256=sha256(canonical(f.environment['report'])),
            capabilities=project_capabilities(requirements,f.environment['report']))
        f.inventory.capabilities.side_effect=lambda *_:deepcopy(f.environment['capabilities'])
        self.source['deployment']['environments'][0]['executables'].append(
            dict(id=raw['id'],label='Synthetic Python',kind='python',sha256=raw['sha256']))
        self.library.prepare_environment(self.task)
        result=self.library.approved(self.task)
        programs=result['candidate']['programs'][0]['executables']
        self.assertEqual({row['kind'] for row in programs},{'lammps','python'})
        python=next(row for row in programs if row['kind']=='python')
        self.assertEqual(python['entrypoints'],[dict(path='structure.py',sha256=sha256(reader.texts['structure.py']))])
        self.assertEqual(result['candidate']['python']['status'],'dependencies_unverified')
        gaps=result['review_receipt']['original_python_missing']
        self.assertEqual({row['path'] for row in gaps},{'plot.py','driver.py'})
        self.assertFalse(result['review_receipt']['execution_authorized'])
        self.assertEqual(self.counts(),(1,0,0));self.assertEqual(f.fx.sent,[])
        prepared=self.ensure()
        self.assertEqual(prepared['state'],'resources_ready')
        lane=self.lane.status(self.task)['latest']['candidate']['profile']['author_reference']
        registered=[program for menu in lane['environments'] for program in menu['executables'] if program['kind']=='python']
        self.assertTrue(registered)
        self.assertTrue(all(row['entrypoints']==python['entrypoints'] for row in registered))
        self.assertEqual(self.counts(),(1,1,0));self.assertEqual(f.fx.sent,[])


if __name__=='__main__':unittest.main()
