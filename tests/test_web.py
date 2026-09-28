"""Real ASGI requests to the persistent local review app; no HPC/API backend."""
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from auto_lammps.tasks import TaskStore
from auto_lammps.web import create_app
from test_tasks import evidence
from test_literature import export_csv
from test_task_packages import frozen_task
from test_deepseek import response
from test_condition_generation import OUTPUT, SOURCES
from unittest.mock import Mock
from auto_lammps.deepseek import DeepSeekClient, DeepSeekConfig, ModelCalls

ORIGIN='http://127.0.0.1:8765'
HEADERS={'Origin':ORIGIN,'X-Task-Review':'1'}


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=TaskStore(Path(self.tmp.name)/'tasks.sqlite')
        self.client=TestClient(create_app(self.store),base_url=ORIGIN)
        self.addCleanup(self.client.close)

    def create(self):
        response=self.client.post('/api/tasks',json={'title':'合成网页验收','prompt':'只验证保存，不运行引擎。','mode':'reproduction'},headers=HEADERS)
        self.assertEqual(response.status_code,201,response.text)
        return response.json()

    def test_real_routes_create_update_and_reload_persistent_task(self):
        doc=self.create()
        reply=self.client.post(f"/api/tasks/{doc['id']}/conditions/material",json={**evidence('synthetic material'),'revision':doc['revision']},headers=HEADERS)
        self.assertEqual(reply.status_code,200,reply.text)
        self.client.close()
        with TestClient(create_app(TaskStore(self.store.path)),base_url=ORIGIN) as reopened:
            read=reopened.get('/api/tasks/'+doc['id']).json()
            self.assertEqual(read['fields']['material']['candidates'][0]['value'],'synthetic material')
            self.assertEqual(len(reopened.get('/api/tasks').json()['tasks']),1)

    def test_cross_origin_rebinding_and_missing_header_are_rejected(self):
        data={'title':'unsafe','prompt':'no write','mode':'research'}
        for headers in ({'Origin':'https://untrusted.example','X-Task-Review':'1'},
                        {'Origin':ORIGIN}, {**HEADERS,'Host':'untrusted.example'}):
            response=self.client.post('/api/tasks',json=data,headers=headers)
            self.assertEqual(response.status_code,403)
        self.assertEqual(self.store.list(),[])
        self.assertEqual(self.client.get('/api/tasks',headers={'Host':'untrusted.example'}).status_code,403)

    def test_stale_missing_and_bad_input_have_safe_responses(self):
        doc=self.create()
        response=self.client.post('/api/tasks/'+doc['id']+'/confirm',json={'revision':0,'fields':['material']},headers=HEADERS)
        self.assertEqual(response.status_code,422)
        response=self.client.post('/api/tasks/'+doc['id']+'/freeze',json={'revision':doc['revision']},headers=HEADERS)
        self.assertEqual(response.status_code,422)
        response=self.client.get('/api/tasks/'+('f'*32))
        self.assertEqual(response.status_code,404)
        response=self.client.post('/api/tasks',json={'title':'x','prompt':'x','mode':'research','execute':True},headers=HEADERS)
        self.assertEqual(response.status_code,422)
        self.assertEqual(self.client.post('/api/tasks/'+doc['id']+'/submit',json={},headers=HEADERS).status_code,404)

    def test_bounded_body_and_content_type_gate(self):
        response=self.client.post('/api/tasks',content=b'x'*131073,headers={**HEADERS,'Content-Type':'application/json'})
        self.assertEqual(response.status_code,413)
        response=self.client.post('/api/tasks',content='{}',headers={**HEADERS,'Content-Type':'text/plain'})
        self.assertEqual(response.status_code,403)

    def test_local_assets_and_no_cache_or_remote_assets(self):
        response=self.client.get('/')
        self.assertEqual(response.status_code,200)
        self.assertIn('不提交作业',response.text)
        self.assertIn('no-store',response.headers['cache-control'])
        self.assertIn("frame-ancestors 'none'",response.headers['content-security-policy'])
        for path in ('/assets/app.js','/assets/app.css'):
            self.assertEqual(self.client.get(path).status_code,200)
        self.assertEqual(self.client.get('/assets/tasks.sqlite').status_code,404)

    def test_literature_preview_and_atomic_import_keep_result_context_private(self):
        doc=self.create()
        content=export_csv()
        preview=self.client.post('/api/literature/preview',json={'csv_text':content},headers=HEADERS)
        self.assertEqual(preview.status_code,200,preview.text)
        self.assertEqual(self.store.get(doc['id'])['revision'],1)
        data=dict(revision=1,csv_text=content,source_sha256=preview.json()['source_sha256'],
                  column='conditions',field='timestep',evidence_role='result',method_class='unclear',
                  classification_basis='Synthetic Methods paragraph 2')
        url='/api/tasks/'+doc['id']+'/literature'
        self.assertEqual(self.client.post(url,json=data,headers=HEADERS).status_code,422)
        data['evidence_role']='input'
        accepted=self.client.post(url,json=data,headers=HEADERS)
        self.assertEqual(accepted.status_code,200,accepted.text)
        self.assertEqual(accepted.json()['revision'],2)
        self.assertFalse(accepted.json()['fields']['timestep']['confirmed'])
        self.assertEqual(self.client.post(url,json=data,headers=HEADERS).status_code,409)
        for route,payload in (('/api/literature/preview',{'csv_text':content}), (url,data)):
            self.assertEqual(self.client.post(route,json=payload,headers={'Origin':'https://example.test','X-Task-Review':'1'}).status_code,403)

    def test_separate_package_downloads_remain_local_operator_drafts(self):
        doc = frozen_task(self.store)
        base = f"/api/tasks/{doc['id']}/packages/"
        execution = self.client.get(base+'execution')
        reference = self.client.get(base+'reference')
        self.assertEqual(execution.status_code, 200, execution.text)
        self.assertEqual(reference.status_code, 200, reference.text)
        self.assertNotIn('PRIVATE_', execution.text)
        self.assertIn('PRIVATE_PROMPT_CANARY', reference.text)
        self.assertEqual(execution.json()['release_status'], 'operator_review_required')
        self.assertIn('execution-task-draft.json', execution.headers['content-disposition'])
        self.assertEqual(execution.headers['cache-control'], 'no-store')
        self.assertEqual(self.client.get(base+'answers').status_code, 404)
        self.assertEqual(self.client.get(base+'reference', headers={'Host': 'untrusted.example'}).status_code, 403)
        unfinished = self.create()
        self.assertEqual(self.client.get(f"/api/tasks/{unfinished['id']}/packages/execution").status_code, 422)

    def test_condition_generation_requires_server_configuration(self):
        doc = self.create()
        self.assertFalse(self.client.get('/api/schema').json()['model_calls_enabled'])
        url = f"/api/tasks/{doc['id']}/generate-conditions"
        self.assertEqual(self.client.post(url, json={'revision':doc['revision']}, headers=HEADERS).status_code, 422)
        self.assertEqual(len(self.store.history(doc['id'])), 1)

    def test_configured_generation_uses_only_saved_request_and_server_budget(self):
        calls = ModelCalls(Path(self.tmp.name)/'models.sqlite', DeepSeekConfig('synthetic-model'), max_requests=1)
        transport = Mock(return_value=(200, response(OUTPUT)))
        model = DeepSeekClient(calls, transport=transport, key_reader=lambda:'synthetic-key')
        doc = self.store.create('普通研究需求', SOURCES[0]['text'], 'research')
        url = f"/api/tasks/{doc['id']}/generate-conditions"
        with TestClient(create_app(self.store, model_client=model), base_url=ORIGIN) as client:
            self.assertTrue(client.get('/api/schema').json()['model_calls_enabled'])
            self.assertEqual(client.post(url,json={'revision':1},headers={'Origin':'https://untrusted.example','X-Task-Review':'1'}).status_code,403)
            self.assertEqual(client.post(url,json={'revision':1,'max_requests':100},headers=HEADERS).status_code,422)
            self.assertEqual(transport.call_count,0)
            result=client.post(url,json={'revision':1},headers=HEADERS)
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(result.json()['fields']['temperature']['candidates'][0]['value'],'300')
            sent=json.loads(transport.call_args.args[0])
            source=json.loads(sent['messages'][1]['content'])['sources']
            self.assertEqual(source,SOURCES)
            self.assertFalse(client.get('/api/schema').json()['model_calls_enabled'])
            self.assertEqual(client.post(url,json={'revision':1},headers=HEADERS).status_code,422)
            self.assertEqual(client.post(url,json={'revision':2},headers=HEADERS).status_code,422)
            self.assertEqual(transport.call_count,1)
