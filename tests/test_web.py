"""Real ASGI requests to the persistent local review app; no HPC/API backend."""
import json
import hashlib
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

    def test_math_assets_are_local_and_integrity_checked(self):
        root=Path(__file__).resolve().parents[1]/'auto_lammps/web_assets/katex'
        manifest=json.loads((root/'manifest.json').read_text())
        for name in manifest['files']:
            response=self.client.get('/assets/katex/'+name)
            self.assertEqual(response.status_code,200,name)
            from auto_lammps.manifest import sha256
            self.assertEqual(sha256(response.content),manifest['files'][name],name)
        self.assertEqual(self.client.get('/assets/katex/not-shipped.js').status_code,404)

    def test_answer_renderer_assets_are_pinned_and_locally_available(self):
        root=Path(__file__).resolve().parents[1]/'auto_lammps/web_assets/markdown'
        manifest=json.loads((root/'manifest.json').read_text())
        for name,digest in manifest['files'].items():
            response=self.client.get('/assets/markdown/'+name)
            self.assertEqual(response.status_code,200)
            self.assertEqual(hashlib.sha256(response.content).hexdigest(),digest)
        self.assertEqual(self.client.get('/assets/markdown/unknown.js').status_code,404)
        self.assertEqual(self.client.get('/assets/markdown-view.js').status_code,200)
        self.assertIn('/assets/markdown/markdown-it.min.js',self.client.get('/').text)

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
            context=json.loads(sent['messages'][1]['content'])
            self.assertEqual([s['id'] for s in context['sources']],[s['id'] for s in SOURCES])
            self.assertEqual([s['origin'] for s in context['sources']],['user'])
            self.assertTrue(all('text' not in s for s in context['sources']))
            spans=context['source_locator_adapter']['spans']
            self.assertEqual(''.join(s['quote'] for s in spans if s['source_id']==SOURCES[0]['id']),SOURCES[0]['text'])
            self.assertFalse(client.get('/api/schema').json()['model_calls_enabled'])
            self.assertEqual(client.post(url,json={'revision':1},headers=HEADERS).status_code,422)
            self.assertEqual(client.post(url,json={'revision':2},headers=HEADERS).status_code,422)
            self.assertEqual(transport.call_count,1)

    def test_condition_retry_route_keeps_failed_identity_and_uses_explicit_predecessor(self):
        from copy import deepcopy
        from unittest.mock import patch
        from auto_lammps.condition_generation import condition_messages
        bad = deepcopy(OUTPUT)
        bad['conditions'][0]['quote'] = 'fabricated-source-fragment'
        calls = ModelCalls(Path(self.tmp.name)/'retry-models.sqlite', DeepSeekConfig('synthetic-model'), max_requests=3)
        transport = Mock(return_value=(200, response(bad)))
        model = DeepSeekClient(calls, transport=transport, key_reader=lambda:'synthetic-key')
        doc = self.store.create('普通入口失败后恢复', SOURCES[0]['text'], 'research')
        url = f"/api/tasks/{doc['id']}/generate-conditions"
        with TestClient(create_app(self.store, model_client=model), base_url=ORIGIN) as client:
            self.assertEqual(client.post(url, json={'revision':1}, headers=HEADERS).status_code, 422)
            failed = self.store.condition_requests(doc['id'])[0]
            saved_calls = calls.history()
            messages = condition_messages(SOURCES)
            messages[0]['content'] += ' synthetic installed adapter version change'
            transport.return_value = (200, response(OUTPUT))
            with patch('auto_lammps.condition_generation.condition_messages', return_value=messages):
                invalid = client.post(url, json={'revision':1, 'retry_of':'bad'}, headers=HEADERS)
                self.assertEqual(invalid.status_code, 422)
                reply = client.post(url, json={'revision':1, 'retry_of':failed['request_id']}, headers=HEADERS)
            self.assertEqual(reply.status_code, 200, reply.text)
            rows = self.store.condition_requests(doc['id'])
            self.assertEqual(rows[0], failed)
            self.assertEqual(rows[1]['state'], 'imported')
            self.assertNotEqual(rows[1]['request_id'], failed['request_id'])
            self.assertEqual(calls.history()[:2], saved_calls)
            self.assertEqual(transport.call_count, 3)
            self.assertEqual(client.post(url, json={'revision':1, 'retry_of':failed['request_id']}, headers=HEADERS).status_code, 422)
            self.assertEqual(transport.call_count, 3)

class ActivityFeedTests(unittest.TestCase):
    """AI 活动必须是"给用户看的进度播报"，不能是内部事件名。"""

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=TaskStore(Path(self.tmp.name)/'tasks.sqlite')
        self.client=TestClient(create_app(self.store),base_url=ORIGIN);self.addCleanup(self.client.close)
        self.doc=self.store.create('合成任务','合成需求文本','research')

    def test_steps_are_human_sentences_with_time_and_current_marker(self):
        reply=self.client.get('/api/tasks/'+self.doc['id']+'/ai-activity')
        self.assertEqual(reply.status_code,200,reply.text)
        body=reply.json()
        titles=[step['title'] for step in body['steps']]
        self.assertTrue(any('已收到你的需求' in title for title in titles),titles)
        # 内部事件名不得出现在给用户看的标题里
        for raw in ('created','conditions_generated','guidance_added','config_rebased'):
            self.assertFalse(any(title==raw or title.startswith(raw+':') for title in titles),titles)
        # 每条都要有时间和类型，供界面画时间线
        for step in body['steps']:
            self.assertTrue(step.get('at'))
            self.assertIn(step.get('kind'),('user','ai','compute','system'))
        self.assertIn('now',body)

    def test_guidance_appears_as_the_user_s_own_words(self):
        self.store.add_guidance(self.doc['id'],self.doc['revision'],'势函数请从我们的势函数库中选取')
        body=self.client.get('/api/tasks/'+self.doc['id']+'/ai-activity').json()
        self.assertTrue(any('引导' in step['title'] for step in body['steps']),[s['title'] for s in body['steps']])

    def test_failed_condition_calls_remain_visible_without_import_or_repeat(self):
        from copy import deepcopy
        bad = deepcopy(OUTPUT)
        bad['conditions'][0]['quote'] = 'fabricated-source-fragment'
        calls = ModelCalls(Path(self.tmp.name)/'models.sqlite', DeepSeekConfig('synthetic-model'), max_requests=2)
        transport = Mock(return_value=(200, response(bad)))
        model = DeepSeekClient(calls, transport=transport, key_reader=lambda:'synthetic-key')
        doc = self.store.create('条件失败反例', SOURCES[0]['text'], 'research')
        url = f"/api/tasks/{doc['id']}/generate-conditions"
        with TestClient(create_app(self.store, model_client=model), base_url=ORIGIN) as client:
            self.assertEqual(client.post(url,json={'revision':1},headers=HEADERS).status_code,422)
            self.assertEqual(transport.call_count,2)
            for _ in range(2):
                body=client.get(f"/api/tasks/{doc['id']}/ai-activity").json()
                progress=body['condition_preparation']
                self.assertEqual(progress['state'],'failed')
                self.assertEqual(progress['error_code'],'source_quote_mismatch')
                self.assertEqual(progress['call_count'],2)
                self.assertNotIn('fabricated-source-fragment',json.dumps(body))
                self.assertIn('未完成',body['now'])
                row=next(r for r in client.get('/api/tasks').json()['tasks'] if r['id']==doc['id'])
                self.assertEqual(row['condition_preparation_state'],'failed')
            self.assertEqual(self.store.get(doc['id']),doc)
            self.assertEqual(client.post(url,json={'revision':1},headers=HEADERS).status_code,422)
            self.assertEqual(transport.call_count,2)

    def test_pre_reservation_budget_failure_is_not_displayed_as_a_model_call(self):
        calls=ModelCalls(Path(self.tmp.name)/'zero-models.sqlite',DeepSeekConfig('synthetic-model'),max_requests=0)
        transport=Mock(return_value=(200,response(OUTPUT)))
        model=DeepSeekClient(calls,transport=transport,key_reader=lambda:'synthetic-key')
        doc=self.store.create('预算拒绝反例',SOURCES[0]['text'],'research')
        with TestClient(create_app(self.store,model_client=model),base_url=ORIGIN) as client:
            reply=client.post(f"/api/tasks/{doc['id']}/generate-conditions",json={'revision':1},headers=HEADERS)
            self.assertEqual(reply.status_code,422)
            body=client.get(f"/api/tasks/{doc['id']}/ai-activity").json()
            self.assertEqual(body['condition_preparation']['state'],'failed')
            self.assertEqual(body['condition_preparation']['call_count'],0)
            self.assertIn('未发送',body['condition_preparation']['detail'])
        self.assertEqual(transport.call_count,0)
        self.assertEqual(self.store.get(doc['id']),doc)

    def test_legacy_quotes_are_checked_read_only_and_not_called_imported(self):
        from auto_lammps.condition_generation import condition_messages
        from auto_lammps.manifest import canonical,sha256
        from copy import deepcopy
        doc=self.store.create('旧调用失败回溯',SOURCES[0]['text'],'research')
        bad=deepcopy(OUTPUT);bad['conditions'][0]['quote']='different request'
        calls=ModelCalls(Path(self.tmp.name)/'models.sqlite',DeepSeekConfig('synthetic-model'),max_requests=2)
        transport=Mock(return_value=(200,response(bad)))
        model=DeepSeekClient(calls,transport=transport,key_reader=lambda:'synthetic-key')
        request_id=sha256(canonical(dict(task_id=doc['id'],revision=1,sources=SOURCES,operation='generate-conditions-v1')))[:32]
        model.complete_json(request_id,condition_messages(SOURCES,'research'))
        model.complete_json(sha256(canonical({'base':request_id,'repair':1}))[:32],condition_messages(SOURCES,'research'))
        self.assertEqual(self.store.condition_requests(doc['id']),[])
        with TestClient(create_app(self.store,model_client=model),base_url=ORIGIN) as client:
            for _ in range(2):
                body=client.get(f"/api/tasks/{doc['id']}/ai-activity").json()
                self.assertTrue(body['condition_preparation']['reconstructed'])
                self.assertEqual(body['condition_preparation']['state'],'failed')
                self.assertEqual(body['condition_preparation']['call_count'],2)
                self.assertIn('核对未通过',body['now'])
                self.assertTrue(any('只读' in s['detail'] for s in body['steps']))
        self.assertEqual(transport.call_count,2)
        self.assertEqual(self.store.get(doc['id']),doc)
        self.assertEqual(self.store.condition_requests(doc['id']),[])
