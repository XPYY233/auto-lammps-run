"""Saved environment metadata in the author workspace; entirely synthetic."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

from auto_lammps.author_environment_inventory import AuthorEnvironmentInventory
from auto_lammps.author_reference_workspace import AuthorReferenceWorkspace
from auto_lammps.manifest import canonical
import test_author_environment_inventory as environment_fixture
import test_author_reference_workspace as workspace_fixture


class FixedTransport:
    def __init__(self,connections,revision,alias):
        self.connections,self.revision,self.host_alias=connections,revision,alias
        self.identity=self._identity()

    def _identity(self):
        return dict(revision=self.connections.revision,host_alias=self.host_alias,
            profile=dict(work_directory=str(self.connections.remote)),
            credential_sha256=self.connections.credential_sha256,store=self.connections.store)

    def command(self,alias,remote):
        import shlex
        if alias!=self.host_alias or self._identity()!=self.identity:raise ValueError('changed fixed fixture')
        return ['synthetic-ssh',shlex.join(remote)]


class AuthorEnvironmentWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.fx=workspace_fixture.AuthorWorkspaceTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.env=environment_fixture.AuthorEnvironmentInventoryTests();self.env.setUp();self.addCleanup(self.env.doCleanups)
        self.workspace=self.fx.workspace;self.task=self.fx.task
        self.env.hpc.store=str(self.fx.fx.tasks.path.resolve())
        for target in ('auto_lammps.author_environment_inventory.SavedHPCTransport',
                       'auto_lammps.author_reference_workspace.SavedHPCTransport'):
            patched=patch(target,FixedTransport);patched.start();self.addCleanup(patched.stop)
        self.inventory=AuthorEnvironmentInventory(self.fx.fx.tasks,self.env.hpc,profile_provider=self.env.provider,
            audit_directory=self.env.root/'workspace-audit',capture=self.env.capture)
        self.workspace.environment_inventory=self.inventory
        self.fx.source.transport=FixedTransport(self.env.hpc,1,'author-reading-alias')
        self.original=deepcopy({key:self.fx.binding[key] for key in
            ('paper_id','format','selections','invocation_paths','deployment','missing','receipt_file_sha256')})

    def check(self):return self.workspace._metadata(self.task)[0]

    def inspect(self,key='8'*32):return self.inventory.inspect(self.task,key)

    def unchanged(self):
        self.assertEqual(self.original,{key:self.fx.binding[key] for key in self.original})
        self.assertEqual(self.fx.fx.reader.calls,0);self.assertEqual(self.fx.fx.sent,[])

    def test_unconfigured_inventory_keeps_existing_binding_exactly(self):
        self.workspace.environment_inventory=None
        self.assertIs(self.check(),self.fx.binding);self.unchanged()
        self.env.capture.assert_not_called()

    def test_no_receipt_preserves_existing_menu_without_new_prerequisite_or_remote(self):
        binding=self.check()
        self.assertIs(binding,self.fx.binding)
        self.assertEqual(binding['missing'],self.original['missing'])
        self.assertNotIn('environment_metadata',binding)
        self.env.capture.assert_not_called();self.unchanged()

    def test_current_saved_receipt_appends_python_without_mutating_pins_resources_or_approval(self):
        receipt=self.inspect();before=self.env.capture.call_count
        binding=self.check();again=self.check()
        self.assertEqual(binding,again);self.assertEqual(self.env.capture.call_count,before)
        self.assertEqual(binding['deployment']['environments'][0],self.original['deployment']['environments'][0])
        self.assertEqual(len(binding['deployment']['environments']),2)
        self.assertEqual(binding['deployment']['environments'][1]['executables'][0]['kind'],'python')
        self.assertEqual(binding['deployment']['resources'],self.original['deployment']['resources'])
        self.assertEqual(binding['deployment']['analysis_capabilities'],self.original['deployment']['analysis_capabilities'])
        self.assertEqual(binding['environment_metadata']['report_sha256'],receipt['report_sha256'])
        self.assertFalse(binding['environment_metadata']['execution_authorized'])
        self.assertFalse(binding['environment_metadata']['execution_ready'])
        self.assertFalse(binding['environment_metadata']['author_dependencies_complete'])
        self.assertIs(binding['reader'],self.fx.binding['reader'])
        self.assertIs(binding['load_receipt'],self.fx.binding['load_receipt']);self.unchanged()

    def test_empty_distribution_request_explicitly_leaves_author_dependencies_unverified(self):
        self.env.input['required_distributions']=[];self.inspect()
        binding=self.check()
        self.assertEqual(binding['environment_metadata']['state'],'metadata_ready')
        self.assertEqual(binding['environment_metadata']['checked_distributions'],[])
        self.assertTrue(any('所需依赖尚待核验' in item for item in binding['missing']))
        self.assertTrue(any('子进程策略' in item for item in binding['deployment']['missing']))
        self.unchanged()

    def test_other_task_receipt_is_not_used_and_latest_unknown_never_falls_back(self):
        other=self.fx.fx.tasks.create('Synthetic other reference','Not scientific','reproduction')['id']
        self.inventory.inspect(other,'7'*32)
        self.assertEqual(len(self.check()['deployment']['environments']),1)
        self.inspect();self.env.capture.side_effect=OSError('Synthetic private network failure')
        self.assertEqual(self.inspect('9'*32)['state'],'unknown')
        before=self.env.capture.call_count;binding=self.check()
        self.assertEqual(len(binding['deployment']['environments']),1)
        self.assertEqual(binding['environment_metadata']['state'],'unknown')
        self.assertEqual(self.env.capture.call_count,before);self.unchanged()

    def test_changed_profile_or_project_binding_preserves_old_menu_and_safe_missing(self):
        self.inspect();before=self.env.capture.call_count
        self.env.input['profile_sha256']='0'*64
        binding=self.check();self.assertEqual(len(binding['deployment']['environments']),1)
        self.assertEqual(binding['environment_metadata']['state'],'unavailable')
        self.assertNotIn(str(self.env.profile),json.dumps(binding['environment_metadata']))
        self.assertNotIn(str(self.env.profile),json.dumps(binding['missing']))
        self.assertEqual(self.env.capture.call_count,before);self.unchanged()

    def test_same_alias_cannot_hide_different_saved_revision_profile_or_project(self):
        self.inspect();before=self.env.capture.call_count
        identity=self.fx.source.transport.identity
        for key,value in (('revision',2),('profile',dict(work_directory='/synthetic-other-project')),('store','/synthetic-other-store')):
            with self.subTest(key=key):
                changed=deepcopy(identity);changed[key]=value
                self.fx.source.transport.identity=changed
                with patch.object(self.fx.source.transport,'_identity',return_value=changed):
                    binding=self.check()
                self.assertEqual(len(binding['deployment']['environments']),1)
                self.assertEqual(binding['environment_metadata']['state'],'unavailable')
        self.fx.source.transport.identity=identity
        self.assertEqual(self.env.capture.call_count,before);self.unchanged()

    def test_menu_collision_never_overwrites_original_environment(self):
        self.inspect();menu=self.inventory.planner_environments(self.task,'8'*32)
        self.fx.binding['deployment']['environments'][0]['id']=menu['environments'][0]['id']
        old=deepcopy(self.fx.binding['deployment']['environments'])
        binding=self.check()
        self.assertEqual(binding['deployment']['environments'],old)
        self.assertEqual(binding['environment_metadata']['state'],'unavailable')
        self.assertEqual(self.fx.fx.reader.calls,0);self.assertEqual(self.fx.fx.sent,[])

    def test_initial_binding_and_restart_reuse_saved_metadata_without_network(self):
        self.inspect();before=self.env.capture.call_count
        workspace=AuthorReferenceWorkspace(self.fx.fx.tasks,self.fx.fx.papers,self.fx.fx.scopes,{},
            model_provider=self.fx.model,connection_provider=self.fx.fx.connection_provider)
        workspace.initial=Mock(load_binding=Mock(return_value=self.fx.binding))
        workspace.environment_inventory=self.inventory
        binding=workspace._metadata(self.task)[0]
        self.assertEqual(len(binding['deployment']['environments']),2)
        self.assertEqual(self.env.capture.call_count,before);self.unchanged()

    def test_status_does_not_check_remote_when_provider_has_private_error(self):
        self.inspect();before=self.env.capture.call_count
        self.env.provider.side_effect=OSError('/private/SYNTHETIC_SECRET/config.json')
        status=self.workspace.status(self.task)
        self.assertTrue(status['enabled'])
        self.assertNotIn('SYNTHETIC_SECRET',json.dumps(status))
        self.assertFalse(status['can_submit']);self.assertEqual(self.env.capture.call_count,before);self.unchanged()

    def test_model_receives_current_menu_in_same_normal_prepare_without_extra_read(self):
        self.inspect();before=self.env.capture.call_count
        self.fx.fx.value.update(status='needs_clarification',questions=['作者入口与子进程部署尚须核验。'])
        with patch.object(self.workspace,'_sources',return_value=self.fx.fx.input['author_source']):
            result=self.workspace.prepare(self.task,'4'*32)
        self.assertEqual(result['state'],'needs_clarification',result)
        sent=json.loads(self.fx.fx.sent[0]['messages'][-1]['content'])
        self.assertIn('metadata-python-',json.dumps(sent))
        self.assertEqual(self.env.capture.call_count,before)
        self.assertFalse(result['execution_authorized']);self.assertFalse(result['can_submit'])
        self.assertEqual(self.fx.fx.reader.calls,0)


if __name__=='__main__':unittest.main()
