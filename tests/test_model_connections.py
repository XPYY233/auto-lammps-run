"""Credentials and accounted result chat; synthetic transport, no real API."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient
from auto_lammps.tasks import TaskStore, TaskError
from auto_lammps.model_connections import ModelConnections, OfficialRequestError, official_request
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

    def test_runtime_discussion_shares_model_accounting_without_duplicate_send(self):
        from auto_lammps.deepseek import DeepSeekConfig, ModelCalls
        calls=ModelCalls(Path(self.tmp.name)/'models.sqlite',DeepSeekConfig('synthetic-model'),max_requests=1)
        self.connections.calls=calls
        self.connections.save('deepseek-official','synthetic-model','test-key-sentinel')
        task=self.store.create(title='synthetic',prompt='synthetic',mode='research')
        identifier='a'*32
        result=self.connections.discuss(task['id'],identifier,'deepseek-official','解释数据',{'verified':True})
        self.assertEqual(result['state'],'completed')
        self.assertEqual(calls.status()['used_requests'],1)
        self.assertEqual(calls.lookup(identifier)['receipt']['usage']['total_tokens'],12)
        self.connections.discuss(task['id'],identifier,'deepseek-official','解释数据',{'verified':True})
        self.transport.assert_called_once()

    def test_saving_selected_connection_unlocks_result_questions_without_startup_flag(self):
        task=self.store.create(title='synthetic',prompt='synthetic',mode='research')
        connections=ModelConnections(self.store,transport=self.transport)
        client=TestClient(create_app(self.store,model_connections=connections),base_url='http://127.0.0.1:8765')
        self.addCleanup(client.close)
        url='/api/tasks/'+task['id']+'/discussion'
        self.assertFalse(client.get(url).json()['enabled'])
        self.assertFalse(connections.assistant_enabled)
        connections.save('deepseek-official','synthetic-model','test-key-sentinel')
        self.assertTrue(client.get(url).json()['enabled'])
        self.assertTrue(connections.assistant_enabled)
        result=connections.discuss(task['id'],'b'*32,'deepseek-official','解释合成结果',{'synthetic':True})
        self.assertEqual(result['state'],'completed')
        self.transport.assert_called_once()
        connections.save('deepseek-official','',remove=True)
        self.assertFalse(client.get(url).json()['enabled'])

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

    def test_discussion_preserves_safe_official_rejection_in_both_histories(self):
        from auto_lammps.deepseek import DeepSeekConfig, ModelCalls
        self.connections.calls = ModelCalls(Path(self.tmp.name)/'models.sqlite',
            DeepSeekConfig('synthetic-model'), max_requests=4)
        self.connections.save('deepseek-official', 'synthetic-model', 'test-key-sentinel')
        task = self.store.create(title='synthetic', prompt='synthetic', mode='research')
        self.transport.side_effect = OfficialRequestError('provider_request_failed',
            transport_attempts=1, http_status=402)
        for i, channel in enumerate(('research', 'author_reference')):
            with self.subTest(channel=channel):
                reference = {'evidence_sha256': 'a'*64} if channel == 'author_reference' else {}
                context = ({'source_sha256': 'a'*64, 'role': 'author_reference_A_human_only'}
                           if reference else {})
                request = f'{i+1:032x}'
                result = self.connections.discuss(task['id'], request, 'deepseek-official',
                    '解释合成数据', context, channel=channel, **reference)
                self.assertEqual(result['state'], 'rejected')
                self.assertEqual(result['diagnostics']['http_status'], 402)
                self.assertEqual(result['diagnostics']['transport_attempts'], 1)
                self.assertEqual(self.connections.calls.lookup(request)['receipt']['http_status'], 402)
                count = self.transport.call_count
                self.assertEqual(self.connections.discuss(task['id'], request, 'deepseek-official',
                    '解释合成数据', context, channel=channel, **reference), result)
                self.assertEqual(self.transport.call_count, count)
                with patch.object(self.connections, '_read', side_effect=AssertionError('history reads no key')):
                    self.assertEqual(self.connections.history(task['id'], channel=channel, **reference), [result])
        self.assertEqual(self.connections.calls.status()['used_requests'], 2)
        with self.store.transaction() as db:
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute('UPDATE result_request_outcomes SET document=document')

    def test_preconnection_failure_preserves_reserved_call_without_claiming_transmission(self):
        from auto_lammps.deepseek import DeepSeekConfig, ModelCalls
        self.connections.calls = ModelCalls(Path(self.tmp.name)/'models.sqlite',
            DeepSeekConfig('synthetic-model'), max_requests=1)
        self.connections.save('deepseek-official', 'synthetic-model', 'test-key-sentinel')
        self.transport.side_effect = OfficialRequestError('provider_connection_failed')
        task = self.store.create(title='synthetic', prompt='synthetic', mode='research')
        result = self.connections.discuss(task['id'], 'd'*32, 'deepseek-official', '解释合成数据', {})
        self.assertEqual(result['state'], 'not_sent')
        self.assertEqual(result['diagnostics']['transport_attempts'], 0)
        self.assertEqual(self.connections.calls.lookup('d'*32)['receipt']['transport_attempts'], 0)
        self.assertEqual(self.connections.calls.status()['used_requests'], 1)


class OfficialRequestTests(unittest.TestCase):
    """Mock only HTTPS transport; never contact any model supplier."""
    def setUp(self):
        self.response = Mock(status=200)
        self.response.read.return_value = b'{"data":[{"id":"synthetic-model"}]}'
        self.connection = Mock()
        self.connection.getresponse.return_value = self.response
        constructor = patch('auto_lammps.model_connections.http.client.HTTPSConnection',
                            return_value=self.connection)
        self.constructor = constructor.start()
        self.addCleanup(constructor.stop)
        context = patch('auto_lammps.model_connections.ssl_context', return_value=object())
        self.context = context.start()
        self.addCleanup(context.stop)

    def call(self, provider='deepseek-official'):
        return official_request(provider, 'secret-key-canary', 'POST', '/chat/completions',
                                {'model': 'synthetic-model', 'input': 'private-prompt-canary'})

    def failure(self, code, state, attempts, status=None):
        with self.assertRaises(OfficialRequestError) as raised:
            self.call()
        error = raised.exception
        self.assertEqual(error.error_code, code)
        self.assertEqual(error.state, state)
        self.assertEqual(error.transport_attempts, attempts)
        self.assertEqual(error.http_status, status)
        for canary in ('secret-key-canary', 'private-prompt-canary', '/private/upstream-canary'):
            self.assertNotIn(canary, str(error) + repr(error) + json.dumps(error.diagnostics))
        return error

    def test_402_401_429_rejections_preserve_status_without_reading_error_body(self):
        self.response.read.side_effect = AssertionError('secret-key-canary private-prompt-canary')
        for provider in ('deepseek-official', 'glm', 'openai', 'anthropic'):
            for status in (402, 401, 429):
                with self.subTest(provider=provider, status=status):
                    self.response.status = status
                    with self.assertRaises(OfficialRequestError) as raised:
                        self.call(provider)
                    error = raised.exception
                    self.assertEqual((error.state, error.http_status, error.transport_attempts),
                                     ('rejected', status, 1))
                    self.assertEqual(error.diagnostics['request_outcome'], 'provider_rejected')
                    self.assertNotIn('secret-key-canary', str(error) + repr(error.diagnostics))
        self.response.read.assert_not_called()
        self.assertEqual(self.connection.request.call_count, 12)

    def test_tls_and_connect_failures_prove_no_http_request_was_attempted(self):
        self.context.side_effect = OSError('secret-key-canary /private/upstream-canary')
        self.failure('model_tls_trust_unavailable', 'not_sent', 0)
        self.constructor.assert_not_called()
        self.context.side_effect = None
        self.connection.connect.side_effect = OSError('secret-key-canary')
        self.failure('provider_connection_failed', 'not_sent', 0)
        self.connection.request.assert_not_called()
        self.connection.close.assert_called_once()

    def test_constructor_and_serialization_errors_remain_preconnection(self):
        self.constructor.side_effect = OSError('secret-key-canary')
        self.failure('provider_connection_failed', 'not_sent', 0)
        self.connection.connect.assert_not_called()
        self.constructor.reset_mock()
        with self.assertRaises(OfficialRequestError) as raised:
            official_request('deepseek-official', 'secret-key-canary', 'POST', '/chat/completions', {'bad': object()})
        self.assertEqual(raised.exception.transport_attempts, 0)
        self.assertEqual(raised.exception.error_code, 'provider_request_invalid')
        self.constructor.assert_not_called()

    def test_request_and_response_header_failures_are_unknown_and_never_retried(self):
        for phase in ('request', 'getresponse'):
            with self.subTest(phase=phase):
                self.connection.reset_mock()
                self.connection.request.side_effect = None
                self.connection.getresponse.side_effect = None
                getattr(self.connection, phase).side_effect = ValueError('secret-key-canary private-prompt-canary')
                self.failure('provider_transport_unknown', 'failed_or_unknown', 1)
                self.connection.request.assert_called_once()
                self.connection.close.assert_called_once()

    def test_partial_200_response_retains_status_and_unknown_outcome(self):
        self.response.read.side_effect = TimeoutError('secret-key-canary')
        self.failure('provider_transport_unknown', 'failed_or_unknown', 1, 200)
        self.response.read.assert_called_once()

    def test_received_malformed_or_oversized_200_response_is_not_unknown_or_unsent(self):
        for raw in (b'secret-key-canary invalid-json', b'[]', b'x'*(2*1024*1024+1)):
            with self.subTest(size=len(raw)):
                self.response.read.return_value = raw
                self.failure('provider_response_invalid', 'invalid_output', 1, 200)

    def test_close_failure_does_not_mask_received_rejection_and_success_is_compatible(self):
        self.connection.close.side_effect = RuntimeError('secret-key-canary')
        self.response.status = 402
        self.failure('provider_request_failed', 'rejected', 1, 402)
        self.response.status = 200
        self.assertEqual(self.call(), {'data': [{'id': 'synthetic-model'}]})
