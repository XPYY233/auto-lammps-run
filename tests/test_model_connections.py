"""Credentials and accounted result chat; synthetic transport, no real API."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from fastapi.testclient import TestClient
from auto_lammps.tasks import TaskStore, TaskError
from auto_lammps.model_connections import ModelConnections
from auto_lammps.web import create_app


class ModelConnectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store=TaskStore(Path(self.tmp.name)/'tasks.sqlite')
        self.transport=Mock(return_value={'choices':[{'message':{'content':'合成答复'}}], 'usage':{'total_tokens':12}})
        self.connections=ModelConnections(self.store, transport=self.transport, assistant_enabled=True)

    def test_secret_write_only_private_and_invalid_body_never_echoed(self):
        client=TestClient(create_app(self.store, model_connections=self.connections),base_url='http://127.0.0.1:8765')
        headers={'Origin':'http://127.0.0.1:8765','X-Task-Review':'1'}
        value={'provider':'deepseek-official','model':'synthetic','api_key':'test-secret-sentinel'}
        reply=client.post('/api/model-connections',json=value,headers=headers)
        self.assertEqual(reply.status_code,200)
        self.assertNotIn('test-secret-sentinel',reply.text)
        self.assertNotIn('test-secret-sentinel',client.get('/api/model-connections').text)
        path=self.connections.directory/'deepseek-official.json'
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        invalid=client.post('/api/model-connections',json={**value,'api_key':{'secret':'test-secret-sentinel'}},headers=headers)
        self.assertEqual(invalid.status_code,422);self.assertNotIn('test-secret-sentinel',invalid.text)
        self.transport.assert_not_called()
        self.connections.save('deepseek-official','',remove=True)
        self.assertFalse(path.exists())

    def test_connection_discovery_and_failed_transport_do_not_leak(self):
        # A first-time user can save the key and discover IDs without knowing one.
        self.connections.save('openai','','test-key-sentinel')
        self.transport.return_value={'data':[{'id':'model-one'},{'id':'model-two'}]}
        self.assertEqual(self.connections.list_models('openai')['models'],['model-one','model-two'])
        self.assertEqual(self.transport.call_args.args[2:4],('GET','/v1/models'))

    def test_all_protocols_deduplicate_and_preserve_failed_intents(self):
        task=self.store.create(title='synthetic',prompt='synthetic',mode='research')
        for index,provider in enumerate(('deepseek-official','glm','openai','anthropic')):
            self.connections.save(provider,'synthetic-model','test-key-sentinel')
            self.transport.return_value=({'content':[{'type':'text','text':'合成答复'}],'usage':{'input_tokens':4}}
                if provider=='anthropic' else {'choices':[{'message':{'content':'合成答复'}}],'usage':{'total_tokens':12}})
            rid=f'{index:032x}'
            result=self.connections.discuss(task['id'],rid,provider,'分析合成数据',{'synthetic':True})
            self.assertEqual(result['state'],'completed')
            count=self.transport.call_count
            self.connections.discuss(task['id'],rid,provider,'分析合成数据',{'synthetic':True})
            self.assertEqual(self.transport.call_count,count)
            payload=self.transport.call_args.args[4]
            self.assertEqual('system' in payload,provider=='anthropic')
        self.transport.side_effect=RuntimeError('test-key-sentinel')
        failed=self.connections.discuss(task['id'],'f'*32,'openai','失败测试',{})
        self.assertEqual(failed['state'],'failed_or_unknown')
        self.assertNotIn('test-key-sentinel',json.dumps(self.connections.history(task['id'])))
        count=self.transport.call_count
        self.connections.discuss(task['id'],'f'*32,'openai','失败测试',{})
        self.assertEqual(self.transport.call_count,count)

    def test_explicit_shared_project_credential_directory_without_key_copy(self):
        self.connections.save('deepseek-official','synthetic','shared-synthetic-secret')
        preview_store=TaskStore(Path(self.tmp.name)/'preview'/'tasks.sqlite')
        preview=ModelConnections(preview_store,transport=self.transport,
                                 credentials_directory=self.connections.directory)
        self.assertTrue(preview.status()['connections']['deepseek-official']['configured'])
        self.assertNotIn('shared-synthetic-secret',json.dumps(preview.status()))
        self.assertFalse((preview_store.path.parent/'model-connections').exists())
        preview.save('deepseek-official','updated')
        self.assertEqual(self.connections.status()['connections']['deepseek-official']['model'],'updated')
        preview.save('deepseek-official','',remove=True)
        self.assertFalse(self.connections.status()['connections']['deepseek-official']['configured'])
        self.transport.assert_not_called()
