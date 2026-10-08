"""User A endpoints use trusted evidence and isolate B; no real provider calls."""
import unittest
from fastapi.testclient import TestClient
from unittest.mock import Mock

from auto_lammps.model_connections import ModelConnections
from auto_lammps.web import create_app
import test_reference_evidence as reference_fixture


class WebReferenceDiscussionTests(unittest.TestCase):
    def setUp(self):
        reference_fixture.ReferenceEvidenceTests.setUp(self)
        self.transport = Mock(return_value={'choices':[{'message':{'content':'仅解释合成 A 证据'}}],
                                           'usage':{'total_tokens':12}})
        self.connections = ModelConnections(self.tasks, transport=self.transport, assistant_enabled=True)
        self.connections.save('deepseek-official','synthetic-model','synthetic-key')
        self.client = TestClient(create_app(self.tasks,papers=self.papers,
            reference_evidence_views=self.views,model_connections=self.connections),
            base_url='http://127.0.0.1:8765')
        self.addCleanup(self.client.close)
        self.base = '/api/tasks/'+self.task['id']
        self.headers = {'Origin':'http://127.0.0.1:8765','X-Task-Review':'1'}

    save = reference_fixture.ReferenceEvidenceTests.save
    write_manifest = reference_fixture.ReferenceEvidenceTests.write_manifest

    def question(self, **changes):
        return dict(request_id='a'*32,provider='deepseek-official',question='解释已有 A 的数据及局限',
                    source_sha256=self.document['source_sha256']) | changes

    def test_actual_user_A_route_is_separate_and_preserves_all_run_records(self):
        task_before=self.tasks.get(self.task['id']); events=self.ledger.events(self.request['id'])
        reply=self.client.post(self.base+'/reference-discussion',json=self.question(),headers=self.headers)
        self.assertEqual(reply.status_code,200,reply.text)
        self.assertEqual(reply.json()['state'],'completed')
        self.assertEqual(len(self.client.get(self.base+'/reference-discussion').json()['messages']),1)
        self.assertEqual(self.client.get(self.base+'/discussion').json()['messages'],[])
        payload=str(self.transport.call_args)
        for forbidden in ('P_ANSWER_CANARY','B_FROZEN_PRIVATE_CANARY','PRIVATE_AUTHOR_CODE_CANARY'):
            self.assertNotIn(forbidden,payload)
        self.assertIn('author_reference_A_human_only',payload)
        self.assertEqual(self.tasks.get(self.task['id']),task_before)
        self.assertEqual(self.ledger.events(self.request['id']),events)
        self.client.post(self.base+'/reference-discussion',json=self.question(),headers=self.headers)
        self.transport.assert_called_once()

    def test_stale_or_browser_supplied_context_rejected_before_API(self):
        stale=self.client.post(self.base+'/reference-discussion',json=self.question(source_sha256='f'*64),headers=self.headers)
        self.assertEqual(stale.status_code,409)
        forged=self.client.post(self.base+'/reference-discussion',json=self.question(context={'P':'injected'}),headers=self.headers)
        self.assertEqual(forged.status_code,422)
        self.transport.assert_not_called()

    def test_declared_download_and_existing_P_history_contract(self):
        response=self.client.get(self.base+'/reference-evidence')
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['requests'],[])
        self.assertEqual(response.json()['report']['job_id'],'123')
        suffix='?source_sha256='+self.document['source_sha256']
        self.assertEqual(self.client.get(self.base+'/reference-evidence/files/data.csv'+suffix).content,self.contents['data.csv'])
        self.assertEqual(self.client.get(self.base+'/reference-evidence/files/source.json'+suffix).status_code,409)
        self.assertEqual(self.client.get(self.base+'/reference-evidence/files/data.csv?source_sha256='+'f'*64).status_code,409)

    def test_no_source_does_not_enable_AI_or_publish_failure_as_success(self):
        other=self.tasks.create('Other','Unrelated research','research')
        response=self.client.get('/api/tasks/'+other['id']+'/reference-discussion')
        self.assertFalse(response.json()['enabled'])
        self.assertEqual(response.json()['messages'],[])
        self.transport.assert_not_called()
