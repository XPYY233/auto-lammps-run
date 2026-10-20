"""Synthetic fixed environment metadata; no real SSH, interpreter or physics."""
import base64
from copy import deepcopy
import json
import os
from pathlib import Path
import shlex
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from auto_lammps.author_environment_inventory import (AuthorEnvironmentInventory, FLAGS,
    REMOTE_CODE, _remote_inventory, _validate_report)
from auto_lammps.manifest import canonical, sha256
from auto_lammps.runtime_launcher import ExecutionDenied
from auto_lammps.tasks import TaskError, TaskStore


class OfflineTransport:
    def __init__(self,connections,revision,alias):
        if type(revision)is not int or revision!=connections.revision:raise ValueError('bad fixed revision')
        self.connections=connections;self.identity=dict(revision=revision,host_alias=alias,
            profile=dict(work_directory=str(connections.remote)),credential_sha256=connections.credential_sha256)
    def command(self,alias,remote):
        if alias!=self.identity['host_alias'] or self.connections.credential_sha256!=self.identity['credential_sha256']:
            raise ValueError('changed saved connection')
        return ['synthetic-ssh',shlex.join(remote)]


class AuthorEnvironmentInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.tasks=TaskStore(self.root/'tasks.sqlite')
        self.task=self.tasks.create('Synthetic fixed environment','Not a scientific request','reproduction')['id']
        self.native=self.root/'native';self.native.mkdir(mode=0o700)
        files={}
        for name in ('bash','bootstrap.sh','mpirun','lmp'):
            path=self.native/name;path.write_bytes(('Synthetic metadata bytes '+name).encode());files[str(path)]=sha256(path.read_bytes())
        self.python=self.root/'python-env'/'bin'/'python3';self.python.parent.mkdir(parents=True,mode=0o700)
        self.python.write_bytes(b'\x7fELF synthetic interpreter bytes; never executed')
        profile=dict(requests_root=str(self.root/'requests'),control_root=str(self.root/'control'),
            runtime_tree=str(self.native),execution_mode='native_slurm',native_slurm=dict(
                scope='trusted_research',formal_isolation=False,storage_enforcement='collection_bound_only',max_ranks=32,
                shell_path=str(self.native/'bash'),bootstrap_path=str(self.native/'bootstrap.sh'),
                mpi_path=str(self.native/'mpirun'),engine_path=str(self.native/'lmp'),modules=['synthetic/module'],files=files))
        self.profile=self.root/'profile.json';self.profile.write_bytes(canonical(profile));self.profile.chmod(0o600)
        self.input=dict(profile_path=str(self.profile),profile_sha256=sha256(self.profile.read_bytes()),connection_revision=1,
            host_alias='synthetic-hpc',python_candidates=[dict(id='existing-python',label='Existing synthetic Python',path=str(self.python))],
            required_distributions=['NumPy','lammps'])
        self.hpc=Mock(revision=1,remote=self.root/'hpc',credential_sha256='1'*64)
        self.provider=Mock(side_effect=lambda _:deepcopy(self.input))
        self.probe_count=0;self.network=[];self.missing=set()
        self.capture=Mock(side_effect=self.transport)
        self.patch=patch('auto_lammps.author_environment_inventory.SavedHPCTransport',OfflineTransport)
        self.patch.start();self.addCleanup(self.patch.stop)
        guard=patch('subprocess.Popen',side_effect=AssertionError('No actual process in metadata tests'))
        guard.start();self.addCleanup(guard.stop)
        self.service=self.make()

    def make(self):
        return AuthorEnvironmentInventory(self.tasks,self.hpc,profile_provider=self.provider,
            audit_directory=self.root/'metadata-audit',capture=self.capture)

    def metadata(self,argv):
        self.probe_count+=1
        self.assertEqual(argv[:4],[str(self.python.resolve()),'-I','-S','-c'])
        self.assertNotIn('import numpy',argv[4]);self.assertNotIn('import lammps',argv[4])
        self.assertNotIn('site.addsitedir',argv[4]);self.assertIn('metadata.Distribution.discover',argv[4])
        request=json.loads(argv[5])
        return canonical(dict(version=[3,12,2],executable=str(self.python.resolve()),distributions=[
            dict(name=name,state='missing_metadata' if name in self.missing else 'installed',
                 version=None if name in self.missing else '1.2.3') for name in request['names']]))

    def transport(self,argv,*,timeout,max_bytes,input_chunks):
        self.assertEqual(argv[0],'synthetic-ssh')
        self.assertEqual(shlex.split(argv[1]),['python3','-I','-S','-c',REMOTE_CODE])
        value=json.loads(b''.join(input_chunks));self.network.append(deepcopy(value))
        report=_remote_inventory(value['request'],action=value['action'],_test_capture=self.metadata)
        return dict(returncode=0,failure=None,stdout=base64.b64encode(canonical(report)).decode(),stderr='')

    def inspect(self,key='1'*32):return self.service.inspect(self.task,key)

    def test_explicit_inspection_verifies_bytes_and_metadata_without_scientific_imports(self):
        self.assertEqual(self.service.history(self.task),[])
        self.provider.assert_not_called();self.capture.assert_not_called()
        result=self.inspect()
        self.assertEqual(result['state'],'metadata_ready',result)
        report=result['report'];candidate=report['python_candidates'][0]
        self.assertEqual(candidate['real_path'],str(self.python))
        self.assertEqual(candidate['sha256'],sha256(self.python.read_bytes()))
        self.assertEqual(candidate['version'],[3,12,2])
        self.assertEqual([row['name'] for row in candidate['distributions']],['numpy','lammps'])
        self.assertTrue(all(row['status']=='verified' for row in report['native_files']))
        self.assertEqual(self.probe_count,1);self.assertEqual(self.capture.call_count,1)
        for key,value in FLAGS.items():self.assertEqual(report[key],value)
        menu=self.service.planner_environments(self.task,'1'*32)
        self.assertEqual(menu['environments'][0]['executables'][0]['kind'],'python')
        self.assertEqual(menu['environments'][0]['executables'][0]['sha256'],candidate['sha256'])
        self.assertFalse(menu['child_process_policy_verified']);self.assertFalse(menu['execution_authorized'])
        self.assertGreaterEqual(len(menu['missing']),2)
        self.assertFalse(menu['reads_remote']);self.assertEqual(self.capture.call_count,1)

    def test_same_ID_and_history_survive_missing_current_provider_without_network(self):
        result=self.inspect();self.provider.side_effect=AssertionError('History must not reload the environment')
        self.assertEqual(self.service.get(self.task,'1'*32),result)
        self.assertEqual(self.inspect(),result)
        self.assertEqual(self.make().history(self.task),[result])
        self.assertEqual(self.capture.call_count,1)

    def test_unknown_never_repeats_inspection_and_requires_read_only_reconcile(self):
        def lost(*args,**kwargs):
            self.transport(*args,**kwargs)
            raise OSError('SYNTHETIC_PRIVATE_FAILURE')
        self.capture.side_effect=lost
        result=self.inspect();self.assertEqual(result['state'],'unknown')
        self.assertNotIn('SYNTHETIC_PRIVATE_FAILURE',json.dumps(result))
        self.assertEqual(self.inspect(),result)
        with self.assertRaisesRegex(TaskError,'未知'):self.inspect('2'*32)
        self.assertEqual(self.capture.call_count,1);self.assertEqual(self.probe_count,1)
        self.capture.side_effect=self.transport
        result=self.service.reconcile(self.task,'1'*32)
        self.assertEqual(result['state'],'metadata_ready')
        self.assertEqual(self.network[-1]['action'],'reconcile')
        self.assertEqual(self.probe_count,1);self.assertEqual(len(result['events']),2)
        self.assertEqual(self.service.reconcile(self.task,'1'*32),result)
        self.assertEqual(self.capture.call_count,2)

    def test_reconcile_absence_does_not_create_remote_directory_or_probe(self):
        self.capture.side_effect=OSError('No response before the remote worker')
        self.assertEqual(self.inspect()['state'],'unknown')
        self.capture.side_effect=self.transport
        result=self.service.reconcile(self.task,'1'*32)
        self.assertEqual(result['state'],'not_started')
        self.assertFalse(self.hpc.remote.exists());self.assertEqual(self.probe_count,0)
        self.assertEqual(self.inspect('2'*32)['state'],'metadata_ready')

    def test_changed_native_file_prevents_all_python_execution(self):
        (self.native/'lmp').write_bytes(b'Changed synthetic engine bytes')
        result=self.inspect()
        self.assertEqual(result['state'],'incomplete')
        self.assertEqual(self.probe_count,0)
        self.assertIn('changed',[row['status'] for row in result['report']['native_files']])
        self.assertEqual(self.service.planner_environments(self.task,'1'*32)['environments'],[])

    def test_missing_distribution_is_explicit_unknown_metadata_not_execution_ready(self):
        self.missing={'lammps'}
        result=self.inspect()
        self.assertEqual(result['state'],'incomplete')
        row=result['report']['python_candidates'][0]
        self.assertEqual(row['status'],'dependencies_unverified')
        self.assertEqual(row['distributions'][1],dict(name='lammps',state='missing_metadata',version=None))
        self.assertEqual(self.service.planner_environments(self.task,'1'*32)['environments'],[])
        self.assertFalse(result['execution_ready'])

    def test_source_and_connection_changes_do_not_reinterpret_saved_receipt(self):
        result=self.inspect();self.input['python_candidates'][0]['label']='Changed server menu'
        self.assertEqual(self.service.get(self.task,'1'*32),result)
        with self.assertRaisesRegex(TaskError,'来源已变化'):self.service.planner_environments(self.task,'1'*32)
        self.input['python_candidates'][0]['label']='Existing synthetic Python'
        self.capture.side_effect=OSError('synthetic unknown')
        self.assertEqual(self.inspect('2'*32)['state'],'unknown')
        self.hpc.credential_sha256='2'*64
        with self.assertRaisesRegex(TaskError,'连接已变化'):self.service.reconcile(self.task,'2'*32)

    def test_untrusted_provider_fields_profile_hash_and_distribution_commands_reject_before_intent(self):
        original=deepcopy(self.input)
        for change in (
            lambda value:value.update(shell='arbitrary shell'),
            lambda value:value.update(profile_sha256='f'*64),
            lambda value:value.update(profile_path='relative.json'),
            lambda value:value.update(required_distributions=['numpy;echo private']),
            lambda value:value.update(required_distributions=['numpy','NumPy']),
            lambda value:value['python_candidates'][0].update(argv=['arbitrary']),
            lambda value:value.update(python_candidates=[]),
        ):
            with self.subTest(change=change):
                self.input=deepcopy(original);change(self.input)
                with self.assertRaises((ValueError,KeyError,TypeError)):self.inspect()
        self.capture.assert_not_called();self.assertEqual(self.service.history(self.task),[])

    def test_unsafe_profile_file_and_python_scripts_are_not_accepted(self):
        self.profile.chmod(0o644)
        with self.assertRaises(ExecutionDenied):self.inspect()
        self.profile.chmod(0o600);self.python.write_bytes(b'#!/bin/sh\necho no-author-code\n')
        result=self.inspect()
        self.assertEqual(result['state'],'incomplete');self.assertEqual(self.probe_count,0)
        self.assertEqual(result['report']['python_candidates'][0]['code'],'python_metadata_unverified')

    def test_unknown_identity_or_invented_success_cannot_supply_a_planner_menu(self):
        self.capture.side_effect=lambda *args,**kwargs:dict(returncode=0,failure=None,stdout='not-base64',stderr='')
        self.assertEqual(self.inspect()['state'],'unknown')
        self.assertEqual(self.service.planner_environments(self.task,'1'*32)['environments'],[])
        self.assertFalse(self.service.status(self.task)['checks_on_GET'])

    def test_report_validator_rejects_missing_files_promoted_dependencies_or_policy_flags(self):
        result=self.inspect();request=self.network[0]['request'];original=result['report']
        for change in (
            lambda value:value.update(execution_authorized=True),
            lambda value:value.update(child_process_policy_verified=True),
            lambda value:value.update(binding_sha256='e'*64),
            lambda value:value['native_files'].pop(),
            lambda value:value['python_candidates'][0]['distributions'][0].update(state='missing_metadata',version=None),
            lambda value:value.update(missing=[]),
            lambda value:value.update(extra='Private text is not a protocol field'),
        ):
            with self.subTest(change=change):
                value=deepcopy(original);change(value)
                with self.assertRaises(ValueError):_validate_report(request,value)

    def test_remote_inspection_same_ID_and_reconciliation_never_repeat_metadata_process(self):
        self.inspect();request=self.network[0]['request']
        again=_remote_inventory(request,_test_capture=self.metadata)
        self.assertEqual(again['state'],'metadata_ready');self.assertEqual(self.probe_count,1)
        changed=deepcopy(request);changed['binding_sha256']='f'*64
        with self.assertRaisesRegex(ValueError,'identity_conflict'):_remote_inventory(changed,_test_capture=self.metadata)
        self.assertEqual(self.probe_count,1)

    def test_metadata_history_is_append_only_and_bound_to_original_task(self):
        self.inspect()
        for table in ('author_environment_intents','author_environment_events'):
            with self.subTest(table=table),self.assertRaisesRegex(sqlite3.IntegrityError,'immutable'):
                with self.tasks.transaction() as db:db.execute('DELETE FROM '+table)
        other=self.tasks.create('Other synthetic task','No actual science','reproduction')['id']
        with self.assertRaisesRegex(TaskError,'其他任务'):self.service.get(other,'1'*32)
        with self.assertRaisesRegex(TaskError,'其他任务'):self.service.inspect(other,'1'*32)

    def test_remote_worker_rejects_local_default_execution_and_extra_request_fields(self):
        self.inspect();request=self.network[0]['request']
        with patch.dict(os.environ,{},clear=True),self.assertRaisesRegex(ValueError,'HPC_metadata_session'):
            _remote_inventory(request)
        with self.assertRaisesRegex(ValueError,'invalid_environment_request'):
            _remote_inventory(dict(request,argv=['unsafe']),_test_capture=self.metadata)


if __name__=='__main__':unittest.main()
