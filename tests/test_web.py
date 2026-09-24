"""Real ASGI requests to the persistent local review app; no HPC/API backend."""
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from auto_lammps.tasks import TaskStore
from auto_lammps.web import create_app
from test_tasks import evidence

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
