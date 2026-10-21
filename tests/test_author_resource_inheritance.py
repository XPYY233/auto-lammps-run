"""Original approved resource inheritance; no new human gate or computation."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import Mock,patch
import unittest
from types import SimpleNamespace

from auto_lammps.author_lane_policy import InheritedAuthorResourcePolicy
from auto_lammps.author_reference_plan import validate_author_plan
from auto_lammps.manifest import canonical,sha256
from auto_lammps.tasks import TaskError
import test_author_lane_deployment as fixture


class AuthorResourceInheritanceTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixture.AuthorLaneDeploymentTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        f=self.fx;self.task=f.task
        with f.ledger._transaction() as db:request=db.execute('SELECT id FROM requests ORDER BY rowid LIMIT 1').fetchone()[0]
        self.original=f.ledger.get(request);self.original.update(accounted=1,job_id='1234')
        self.get_patch=patch.object(f.ledger,'get',side_effect=lambda _:deepcopy(self.original));self.get_patch.start();self.addCleanup(self.get_patch.stop)
        self.resource=dict(directory=str(f.root/'old-run'),manifest_sha256='d'*64,batch_sha256='e'*64,
            originals=[dict(path=row['path'],run_mode='0o400',source_mode='0o400',content_line_diff=[],
                source_sha256=row['sha256'],run_sha256=row['sha256']) for row in f.fx.input['author_source']['capability']['files']])
        self.path=f.root/'existing-resource-receipt.json';self.save()
        self.inputs=Mock();self.receipt=dict(kind='accounted_reference_output',task_id=self.task,request_id=request,job_id='1234',
            manifest_sha256=self.original['manifest_sha256'],receipt_sha256='a'*64,binding_sha256='b'*64,
            root=self.resource['directory'],files=[dict(path='one.restart',size=1,sha256='c'*64)])
        self.inputs.planner_inputs.return_value=[self.receipt]
        self.inputs.status.return_value=dict(latest=dict(report=dict(submission_environment=dict(
            sbatch_path='/synthetic/sbatch',sbatch_sha256='f'*64,original_config_sha256='e'*64))))
        f.workspace.planner=Mock();f.workspace.planner.history.return_value=[dict(report=validate_author_plan(f.fx.evidence,f.fx.value))]
        f.fx.input['deployment']['environments'][0]['executables'][0]['sha256']=f.profile_value['native_slurm']['files'][str(f.native_dir/'lmp')]
        grants=f.root/'existing-grants';grants.mkdir(mode=0o700);(grants/'grant.key').write_bytes(b's'*32);(grants/'grant.key').chmod(0o400)
        f.controller.authorization.directory=grants
        f.controller.environment=Mock(python_path=str(f.python))
        self.provider=Mock(side_effect=lambda _:dict(path=str(self.path),sha256=sha256(self.path.read_bytes()),request_id=request))
        self.service=InheritedAuthorResourcePolicy(f.service,self.inputs,resource_receipt_provider=self.provider)
        f.service.policy=self.service

    def save(self):
        if self.path.exists():self.path.chmod(0o600)
        self.path.write_bytes(canonical(self.resource));self.path.chmod(0o400)

    def test_unchanged_resource_capabilities_inherit_actual_campaign_and_run_without_new_approval(self):
        before=self.fx.tasks.approvals(self.task)
        result=self.service.approved(self.task)
        self.assertEqual(result['review_receipt']['kind'],'inherited_accounted_author_resource_bundle')
        self.assertFalse(result['review_receipt']['new_user_gate']);self.assertFalse(result['review_receipt']['new_scientific_review'])
        self.assertEqual(result['review_receipt']['original_request_id'],self.original['id'])
        self.assertEqual(before,self.fx.tasks.approvals(self.task));self.fx.capture.assert_not_called()
        self.assertEqual(self.service.approved(self.task),result)
        context,_=self.fx.service._prepared_context(self.task,'a'*32)
        self.assertEqual(len(context['request']['files']),9)
        self.assertFalse(any(row.get('path','').endswith('.stage.lock') for row in context['request']['files']))
        self.assertEqual(context['candidate']['profile']['author_reference']['execution_policy']['review_sha256'],result['review_sha256'])
        self.assertNotIn('base64',json.dumps(context['candidate']))

    def test_changed_original_or_wrong_accounted_root_cannot_inherit(self):
        self.resource['originals'][0]['content_line_diff']=['changed'];self.save()
        with self.assertRaisesRegex(TaskError,'改写'):self.service.approved(self.task)
        self.resource['originals'][0]['content_line_diff']=[];self.resource['directory']='/different/root';self.save()
        with self.assertRaisesRegex(TaskError,'目录'):self.service.approved(self.task)
        self.assertEqual(self.fx.tasks.approvals(self.task),[])

    def test_inherited_resources_install_and_register_existing_author_controller_without_submission(self):
        f=self.fx;f.workspace.execution=None;f.workspace.lane_deployment=f.service
        self.inputs.outputs=SimpleNamespace(raw=SimpleNamespace(collections=f.root/'collections'))
        f.controller.staging.client.transport.revision=1
        f.controller.environment.partition='synthetic-cpu';f.controller.environment.account=None
        candidate=f.service.preview(self.task,'a'*32)
        f.service.approve(self.task,'a'*32,expected_candidate_sha256=candidate['candidate_sha256'])
        self.assertEqual(f.service.install(self.task,'a'*32)['state'],'metadata_installed')
        with patch('auto_lammps.author_dispatch_config.SavedHPCTransport',side_effect=lambda *_:f.transport):
            result=f.service.activate(self.task)
        self.assertEqual(result['state'],'registered_not_submitted');self.assertFalse(result['execution_authorized'])
        self.assertIsNotNone(f.workspace.execution)
        self.assertEqual(len(f.network),1);self.assertEqual(self.fx.tasks.approvals(self.task),[])


if __name__=='__main__':unittest.main()
