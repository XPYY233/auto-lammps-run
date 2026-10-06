"""Plan presentation retains verified versions without generation or dispatch."""
import json
import shutil
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from auto_lammps.manifest import freeze, Snapshot
from auto_lammps.plan_workspace import workspace
from auto_lammps.web import create_app
import test_candidate_jobs as candidate_fixture
from test_web import ORIGIN


class PlanWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.f=candidate_fixture.CandidateJobTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.enqueue();self.first=self.f.finished()
        self.service=self.f.service

    def add_version(self, *, wrong_conditions=False):
        digest=self.first['result']['snapshot_sha256']
        old=Snapshot(self.service.snapshots/digest,digest)
        record=old.verify()
        source=self.f.fixture.root/'view-source'
        shutil.copytree(old.path,source)
        path=source/'generation.json';path.chmod(0o600)
        generation=json.loads(path.read_bytes());generation['proposal']['workflow']+='\n# Synthetic changed step'
        if wrong_conditions:generation['input']['condition_record_sha256']='0'*64
        path.write_text(json.dumps(generation))
        new=freeze(source,self.service.snapshots,files={i['path']:i['role'] for i in record['files']},
                   entrypoint=record['entrypoint'],resources=self.service.resources,provenance=record['provenance'])
        with self.f.tasks.transaction() as db:
            self.f.history._event(db,self.first['id'],'prepared',{'snapshot_sha256':new.digest})
        return new

    def test_read_only_endpoint_displays_current_structure_and_true_step_difference(self):
        self.add_version()
        calls=self.f.fixture.transport.call_count
        with patch.object(self.service,'enqueue',side_effect=AssertionError('view cannot prepare')):
            with TestClient(create_app(self.f.tasks,candidate_service=self.service,model_client=self.service.client),base_url=ORIGIN) as web:
                reply=web.get('/api/tasks/'+self.f.doc['id']+'/plan')
        self.assertEqual(reply.status_code,200,reply.text)
        view=reply.json()['workspace']
        self.assertEqual(view['current']['version'],2)
        self.assertIn('计算步骤与脚本',view['versions'][-1]['changes'])
        self.assertIn('+',view['versions'][-1]['workflow_diff'])
        self.assertEqual(self.f.fixture.transport.call_count,calls)
        self.assertNotIn('input',view['current'])
        self.assertNotIn('model_receipts',view['current'])
        self.assertFalse(view['current']['historical'])

    def test_preparing_next_version_keeps_last_verified_plan_as_historical(self):
        with self.f.tasks.transaction() as db:
            self.f.history._event(db,self.first['id'],'checking_plan',{})
        view=workspace(self.service,self.f.doc['id'])
        self.assertEqual(view['current']['version'],1)
        self.assertTrue(view['current']['historical'])

    def test_plan_for_different_frozen_conditions_cannot_be_presented_as_current(self):
        self.add_version(wrong_conditions=True)
        view=workspace(self.service,self.f.doc['id'])
        self.assertFalse(view['versions'][-1]['available'])
        self.assertEqual(view['current']['version'],1)
        self.assertTrue(view['current']['historical'])
