"""Synthetic metadata installations only; no SSH, author code or calculation."""
import base64
from copy import deepcopy
import json
import os
from pathlib import Path
import shlex
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from auto_lammps.author_lane_deployment import AuthorLaneDeployment, HELPERS, REMOTE_CODE, _remote_install
from auto_lammps.manifest import canonical, sha256
from auto_lammps.tasks import TaskError
import test_author_reference_plan as plan_fixture


class OfflineTransport:
    def __init__(self, revision=1):
        self.current = revision
        self.identity = dict(revision=revision, host_alias='synthetic-hpc', profile={'work_directory':'/synthetic'},
                             credential_sha256='1'*64)
    def _identity(self): return dict(self.identity, revision=self.current)
    def command(self, alias, remote):
        if alias != self.identity['host_alias'] or self._identity() != self.identity: raise ValueError('changed connection')
        return ['synthetic-saved-ssh', shlex.join(remote)]


class AuthorLaneDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.fx = plan_fixture.AuthorReferencePlanTests(); self.fx.setUp(); self.addCleanup(self.fx.doCleanups)
        self.tasks = self.fx.tasks; self.task = self.fx.task['id']; self.root = self.fx.fixture.root
        self.fx.fixture.bind_run()
        self.ledger = self.fx.fixture.ledger
        self.transport = OfflineTransport()
        self.source = self.fx.reader.service(); self.source.transport = self.transport
        self.native_dir = self.root/'native'; self.native_dir.mkdir(mode=0o700)
        native_files = {}
        for name in ('lmp','bash','bootstrap.sh','mpirun'):
            path = self.native_dir/name; path.write_bytes(('Synthetic unused '+name).encode())
            native_files[str(path)] = sha256(path.read_bytes())
        self.profile_value = dict(requests_root=str(self.root/'old-requests'), control_root=str(self.root/'old-control'),
            runtime_tree=str(self.native_dir), execution_mode='native_slurm', native_slurm=dict(
                scope='trusted_research', formal_isolation=False, storage_enforcement='collection_bound_only',
                max_ranks=32, shell_path=str(self.native_dir/'bash'), bootstrap_path=str(self.native_dir/'bootstrap.sh'),
                engine_path=str(self.native_dir/'lmp'), mpi_path=str(self.native_dir/'mpirun'),
                modules=['synthetic/module'], files=native_files))
        self.profile = self.root/'fixed-profile.json'; self.profile.write_bytes(canonical(self.profile_value)); self.profile.chmod(0o400)
        self.profile_sha = sha256(self.profile.read_bytes())
        self.python = self.root/'python'; self.python.write_bytes(b'Unused synthetic interpreter')
        report = dict(native_files=[dict(path=path,sha256=digest,status='verified') for path,digest in native_files.items()],
            python_candidates=[dict(id='existing-python', path=str(self.python), real_path=str(self.python),
                sha256=sha256(self.python.read_bytes()), status='verified', distributions=[])])
        self.environment = dict(state='metadata_ready', request_id='e'*32, report_sha256=sha256(canonical(report)), report=report)
        self.inventory = Mock()
        self.inventory.status.side_effect = lambda _: dict(latest=deepcopy(self.environment))
        self.inventory.planner_environments.side_effect = lambda *_: dict(report_sha256=self.environment['report_sha256'])
        self.inventory._context.side_effect = lambda _: (dict(profile_sha256=self.profile_sha),self.transport)
        self.workspace = SimpleNamespace(tasks=self.tasks,papers=self.fx.papers,scopes=self.fx.scopes,
            environment_inventory=self.inventory,
            _metadata=Mock(side_effect=lambda _: (dict(reader=self.source,deployment=deepcopy(self.fx.input['deployment'])),
                self.fx.paper,self.fx.reader.receipt,self.fx.scope)),
            _cached_sources=Mock(side_effect=lambda *_:deepcopy(self.fx.input['author_source'])))
        endpoint = SimpleNamespace(host_alias='synthetic-hpc',root_path=str(self.root/'old-requests'),python_path=str(self.python))
        adapter = lambda: SimpleNamespace(endpoint=endpoint,transport=self.transport)
        self.controller = SimpleNamespace(tasks=self.tasks,ledger=self.ledger,runtime_profile_path=self.profile,
            authorization=SimpleNamespace(pins=dict(profile_sha256=self.profile_sha)),
            staging=SimpleNamespace(client=adapter()),submission=SimpleNamespace(scheduler=adapter()),
            following=SimpleNamespace(analysis=SimpleNamespace(collector=adapter()),reconciliation=SimpleNamespace(reader=adapter())))
        self.review = dict(reviewed_commit='a'*40,files={name:sha256(Path(module.__file__).read_bytes()) for name,module in HELPERS.items()})
        self.network=[]; self.capture=Mock(side_effect=self.transfer)
        patcher=patch('auto_lammps.author_lane_deployment.SavedHPCTransport',OfflineTransport);patcher.start();self.addCleanup(patcher.stop)
        guard=patch('subprocess.Popen',side_effect=AssertionError('No subprocess may run in lane tests'));guard.start();self.addCleanup(guard.stop)
        self.service=self.make()

    def make(self, review=True):
        return AuthorLaneDeployment(self.workspace,self.controller,None,audit_directory=self.root/'lane-audit',
            reviewed_helpers=self.review if review else None,capture=self.capture)

    def transfer(self, command, *, timeout, max_bytes, input_chunks):
        self.assertEqual(command[0],'synthetic-saved-ssh')
        self.assertEqual(shlex.split(command[1]),[str(self.python),'-I','-S','-c',REMOTE_CODE])
        envelope=json.loads(b''.join(input_chunks));self.network.append(deepcopy(envelope))
        report=_remote_install(envelope['request'],action=envelope['action'],_test_host=True)
        wire=base64.b64encode(canonical(report))+b'\n'
        return dict(returncode=0,failure=None,stdout=base64.b64encode(wire).decode(),stderr='')

    def preview(self, request='1'*32):return self.service.preview(self.task,request)

    def approve(self, request='1'*32):
        candidate=self.preview(request)
        return self.service.approve(self.task,request,expected_candidate_sha256=candidate['candidate_sha256'])

    def test_preview_is_concrete_local_immutable_and_preserves_original_B_and_rounds(self):
        before=self.profile.read_bytes();old=self.fx.fixture.snapshot_old_records()
        result=self.preview();self.assertEqual(result['state'],'previewed');self.capture.assert_not_called()
        candidate=result['candidate'];profile=candidate['profile']
        self.assertEqual(profile['native_slurm'],self.profile_value['native_slurm'])
        self.assertEqual(profile['author_reference']['environments'],[])
        self.assertEqual(profile['author_reference']['source_roots'][0]['path'],self.fx.read_request['remote_root'])
        self.assertTrue(candidate['can_install_metadata']);self.assertGreater(len(candidate['execution_missing']),3)
        self.assertEqual(len(candidate['files']),5);self.assertNotIn('grant.key',json.dumps(candidate))
        self.assertFalse(result['execution_ready']);self.assertFalse(result['execution_authorized'])
        self.assertEqual(before,self.profile.read_bytes());self.assertEqual(old,self.fx.fixture.snapshot_old_records())
        self.assertEqual(self.fx.service.history(self.task),[]);self.assertEqual(self.fx.sent,[])
        self.workspace._metadata.side_effect=AssertionError('History must not reload provider')
        self.assertEqual(self.preview(),result);self.assertEqual(self.make().status(self.task)['latest'],result)

    def test_install_requires_exact_explicit_approval_and_does_not_register_execution(self):
        candidate=self.preview()
        with self.assertRaises(TaskError):self.service.install(self.task,'1'*32)
        with self.assertRaises(TaskError):self.service.approve(self.task,'1'*32,expected_candidate_sha256='f'*64)
        self.capture.assert_not_called();approved=self.approve();self.assertTrue(approved['can_install'])
        result=self.service.install(self.task,'1'*32);self.assertEqual(result['state'],'metadata_installed',result)
        lane=Path(candidate['candidate']['lane_directory'])
        self.assertTrue((lane/'runtime/runtime_launcher.py').is_file());self.assertFalse((lane/'control/grant.key').exists())
        self.assertFalse((lane/'runtime/submission.json').exists());self.assertEqual(list((lane/'requests').iterdir()),[])
        self.assertEqual(self.profile.read_bytes(),canonical(self.profile_value))
        self.assertFalse(result['execution_ready']);self.assertFalse(result['execution_authorized'])
        self.assertEqual(self.service.install(self.task,'1'*32),result);self.assertEqual(len(self.network),1)
        self.assertEqual([row['kind'] for row in result['events']],
            ['metadata_install_approved','metadata_install_intent','metadata_install_receipt'])

    def test_metadata_install_walks_search_only_ancestors_without_listing_them(self):
        self.approve();real_open=os.open;search_flag=1<<30;ancestor=self.root.parts[1];visited=[]
        def open_search(path,flags,*args,**kwargs):
            if kwargs.get('dir_fd') is not None and str(path)==ancestor and flags&os.O_DIRECTORY:
                visited.append(flags)
                if not flags&search_flag:raise PermissionError('synthetic search-only ancestor')
            return real_open(path,flags&~search_flag,*args,**kwargs)
        with patch.object(os,'O_PATH',search_flag,create=True),patch('os.open',side_effect=open_search):
            self.assertEqual(self.service.install(self.task,'1'*32)['state'],'metadata_installed')
        self.assertTrue(visited);self.assertTrue(all(flags&os.O_NOFOLLOW for flags in visited))

    def test_normal_resource_action_recovers_unknown_then_resumes_original_metadata_request(self):
        inventory=Mock();original='d'*32
        inventory.status.return_value=dict(latest=dict(state='unknown',request_id=original))
        inventory.reconcile.return_value=dict(state='not_started',request_id=original)
        self.service.policy=SimpleNamespace(inputs=inventory,approved=Mock())
        with patch.object(self.workspace.scopes,'get_author_validation',return_value=dict(
                state='recorded',can_prepare_A_plan=True)),patch.object(self.service,'status',return_value=dict(
                latest=dict(state='metadata_installed',candidate=dict(source_review_sha256='f'*64)))),patch.object(self.service,'activate'):
            result=self.service.ensure_resources(self.task,'b'*32)
        inventory.reconcile.assert_called_once_with(self.task,original)
        inventory.inspect.assert_called_once_with(self.task,original)
        self.assertEqual(result['state'],'resources_ready');self.capture.assert_not_called()

    def test_current_helper_review_must_be_real_and_cannot_borrow_old_B_hashes(self):
        service=self.make(False);candidate=service.preview(self.task,'2'*32)
        self.assertEqual(candidate['candidate']['install_missing'],['installed_helper_source_review_manifest'])
        with self.assertRaises(TaskError):service.approve(self.task,'2'*32,expected_candidate_sha256=candidate['candidate_sha256'])
        self.service.reviewed_helpers['files']['remote_submit.py']='f'*64
        with self.assertRaisesRegex(TaskError,'受审'):self.preview()
        self.capture.assert_not_called()

    def test_unknown_transport_never_repeats_install_and_reconcile_is_read_only(self):
        self.approve()
        def lost(*args,**kwargs):
            self.transfer(*args,**kwargs);raise OSError('PRIVATE synthetic failure')
        self.capture.side_effect=lost
        result=self.service.install(self.task,'1'*32);self.assertEqual(result['state'],'unknown')
        self.assertNotIn('PRIVATE',json.dumps(result));self.assertEqual(self.service.install(self.task,'1'*32),result)
        self.approve('2'*32)
        with self.assertRaisesRegex(TaskError,'未知'):self.service.install(self.task,'2'*32)
        self.capture.side_effect=self.transfer
        reconciled=self.service.reconcile(self.task,'1'*32)
        self.assertEqual(reconciled['state'],'metadata_installed');self.assertEqual(self.network[-1]['action'],'reconcile')
        self.assertEqual(self.service.reconcile(self.task,'1'*32),reconciled)

    def test_absent_reconciliation_does_not_create_any_remote_lane(self):
        self.approve();self.capture.side_effect=OSError('synthetic transport unavailable')
        self.assertEqual(self.service.install(self.task,'1'*32)['state'],'unknown')
        self.capture.side_effect=self.transfer
        result=self.service.reconcile(self.task,'1'*32);self.assertEqual(result['state'],'not_started')
        self.assertFalse((self.root/'author-lanes').exists())
        self.assertEqual(self.service.install(self.task,'1'*32),result)

    def test_changed_provider_or_connection_does_not_reuse_saved_approval(self):
        self.approve();self.transport.current=2
        with self.assertRaisesRegex(TaskError,'连接'):self.service.install(self.task,'1'*32)
        self.transport.current=1;self.environment['state']='unknown'
        with self.assertRaisesRegex(TaskError,'未知'):self.service.install(self.task,'1'*32)
        self.capture.assert_not_called()

    def test_environment_endpoint_interpreter_and_complete_native_pins_are_required(self):
        self.environment['report']['python_candidates'][0]['path']='/different/python'
        self.environment['report']['python_candidates'][0]['real_path']='/different/python'
        with self.assertRaisesRegex(TaskError,'解释器'):self.preview()
        self.environment['report']['python_candidates'][0]['real_path']=str(self.python)
        self.environment['report']['native_files'].pop()
        with self.assertRaisesRegex(TaskError,'CPU'):self.preview()
        self.capture.assert_not_called()

    def test_receipt_rejects_forged_execution_permission_and_changed_candidate(self):
        self.approve()
        original=self.transfer
        def forged(*args,**kwargs):
            value=original(*args,**kwargs)
            report=json.loads(base64.b64decode(base64.b64decode(value['stdout'])))
            report['execution_authorized']=True
            value['stdout']=base64.b64encode(base64.b64encode(canonical(report))).decode();return value
        self.capture.side_effect=forged
        self.assertEqual(self.service.install(self.task,'1'*32)['state'],'unknown')
        self.capture.side_effect=self.transfer
        self.assertEqual(self.service.reconcile(self.task,'1'*32)['state'],'metadata_installed')

    def test_remote_reconcile_never_repairs_changed_or_partial_installation(self):
        candidate=self.approve();self.service.install(self.task,'1'*32)
        lane=Path(candidate['candidate']['lane_directory']);helper=lane/'runtime/remote_stage.py'
        helper.chmod(0o600);helper.write_bytes(b'tampered');helper.chmod(0o400)
        request=self.network[0]['request']
        with self.assertRaises(ValueError):_remote_install(request,action='reconcile',_test_host=True)
        self.assertEqual(helper.read_bytes(),b'tampered')
        (lane/'installation.json').unlink()
        self.assertEqual(_remote_install(request,action='reconcile',_test_host=True)['state'],'unknown')

    def test_append_only_records_cross_task_and_untrusted_path_changes(self):
        self.approve()
        with self.tasks.transaction() as db:
            for table in ('author_lane_candidates','author_lane_events'):
                with self.assertRaises(sqlite3.IntegrityError):db.execute('DELETE FROM '+table)
        other=self.tasks.create('Other task','Synthetic intent','reproduction')['id']
        with self.assertRaises(TaskError):self.service.get(other,'1'*32)
        context=self.service._stored(self.task,'1'*32)[0]['context']
        request=deepcopy(context['request']);request['lane_directory']=str(self.root/'arbitrary')
        with self.assertRaisesRegex(ValueError,'namespace'):_remote_install(request,_test_host=True)
        self.assertFalse((self.root/'arbitrary').exists())


if __name__=='__main__':unittest.main()
