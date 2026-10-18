"""Real application approval and two-stage routing; synthetic model transport only."""
import importlib.util
import json
import unittest
from fastapi.testclient import TestClient
from auto_lammps.web import create_app
import test_workbench_targets_web as fixture


@unittest.skipUnless(importlib.util.find_spec('auto_research') is not None,
    'separately installed literature workbench is optional')
class WorkbenchTargetRefinementWebTests(unittest.TestCase):
    def test_configured_recovery_refines_complete_inventory_then_complete_dependency_draft(self):
        f=fixture.WorkbenchTargetsWebTests();f.setUp();self.addCleanup(f.doCleanups)
        f.complete_inventory();task=f.task['id'];base='/api/tasks/'+task+'/reference-target-analysis'
        old=['e'*32,'f'*32,'1'*32]
        for request in old:
            result=f.client.post(base,json={'request_id':request},headers=fixture.HEADERS)
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(result.json()['state'],'completed',result.text)
        grant=dict(id='2'*32,task_id=task,prior_target_request_ids=old,
            prior_inventory_request_ids=[f.request],additional_requests=3,approval_sha256='a'*64,
            reason='Synthetic explicit mixed-panel refinement development approval.')
        client=TestClient(create_app(f.tasks,papers=f.papers,model_client=f.f.client,
            model_connections=f.connections,workbench_bridge=f.bridge,
            workbench_bindings={f.paper['id']:f.binding},reference_target_recovery_authorizations=[grant]),
            base_url=fixture.ORIGIN);self.addCleanup(client.close)
        menu=client.get(f.base+'/targets').json()
        self.assertEqual(menu['status']['development_recovery']['remaining_requests'],3)
        result=client.post(f.base+'/targets',json={'request_id':'3'*32},headers=fixture.HEADERS)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['state'],'completed',result.text)
        inventory=json.loads(f.sent[-1]['messages'][-1]['content'])
        revision=inventory['workbench_inventory_revision']
        self.assertEqual(revision['parent_request'],f.request)
        self.assertEqual(revision['complete_previous_answer']['inventory']['targets'][0]['id'],'fig1')
        self.assertIn('split mixed-method',revision['feedback']['instruction'])
        self.assertEqual(client.get(base).json()['development_recovery']['used_requests'],1)
        result=client.post(base,json={'request_id':'4'*32},headers=fixture.HEADERS)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['state'],'completed',result.text)
        dependency=json.loads(f.sent[-1]['messages'][-1]['content'])
        self.assertEqual(dependency['reference_target_revision']['parent_request'],old[-1])
        self.assertEqual(client.get(base).json()['development_recovery']['used_requests'],2)
        self.assertEqual(len(client.get(f.base+'/targets').json()['history']),2)
        self.assertEqual(len(client.get(base).json()['history']),4)
