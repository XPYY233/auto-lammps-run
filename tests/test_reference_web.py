"""Reference evidence routes with synthetic transport; no provider or simulation."""
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from auto_lammps.deepseek import DeepSeekClient, DeepSeekConfig, ModelCalls
from auto_lammps.tasks import TaskStore
from auto_lammps.web import create_app
import test_reference_generation as fixtures
from test_web import ORIGIN, HEADERS


class ReferenceWebTests(unittest.TestCase):
    def setUp(self):
        fixtures.ReferenceGenerationTests.setUp(self)
        self.url = f"/api/tasks/{self.doc['id']}/reference-evidence"
        self.http = self.open()

    def open(self, model=None, reference=True, **kwargs):
        http = TestClient(create_app(self.store, model_client=model,
            reference_model_client=self.client if reference else None), base_url=ORIGIN, **kwargs)
        self.addCleanup(http.close)
        return http

    def post(self, http=None, **extra):
        return (http or self.http).post(self.url, json=dict(revision=self.doc['revision'],
            csv_texts=[self.csv], **extra), headers=HEADERS)

    def request_id(self):
        return self.store.reference_requests(self.doc['id'])[0]['request_id']

    def recover(self, http=None, revision=None, task=None, request=None):
        identifier = task or self.doc['id']
        return (http or self.http).post(
            f'/api/tasks/{identifier}/reference-evidence/{request or self.request_id()}/recover',
            json={'revision': revision or self.doc['revision']}, headers=HEADERS)

    def test_reference_configuration_is_explicit_and_server_only(self):
        http = self.open(model=self.client, reference=False)
        self.assertFalse(http.get('/api/schema').json()['reference_generation']['configured'])
        self.assertEqual(self.post(http).status_code, 422)
        self.assertEqual(self.post(api_key='never-use-client-key').status_code, 422)
        self.assertEqual(self.post(max_requests=99).status_code, 422)
        self.assertEqual(self.http.post(self.url, json={'revision':1, 'csv_texts':[self.csv]},
            headers={**HEADERS, 'Origin':'https://untrusted.example'}).status_code, 403)
        self.transport.assert_not_called()
        self.assertEqual(self.store.reference_requests(self.doc['id']), [])

    def test_import_history_and_repeat_use_one_call(self):
        result = self.post()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse(result.json()['fields']['temperature']['confirmed'])
        self.assertEqual(self.post().json(), result.json())
        for _ in range(2):
            history = self.http.get(self.url)
            self.assertEqual(history.status_code, 200)
            self.assertEqual(history.json()['requests'][0]['state'], 'saved')
            self.assertFalse(history.json()['execution_authorized'])
            for private in (str(self.root), 'synthetic-key', 'structured_output', '7.654321', 'accounting_sha256'):
                self.assertNotIn(private, history.text)
        self.assertEqual(self.transport.call_count, 1)
        self.assertEqual(len(self.calls.history()), 1)

    def test_research_and_missing_tasks_cannot_use_reference_flow(self):
        research = self.store.create('Research', 'No paper required', 'research')
        for identifier, expected in ((research['id'],422), ('f'*32,404)):
            url = f'/api/tasks/{identifier}/reference-evidence'
            self.assertEqual(self.http.get(url).status_code, expected)
            self.assertEqual(self.http.post(url, json={'revision':1, 'csv_texts':[self.csv]},
                headers=HEADERS).status_code, expected)
        self.transport.assert_not_called()

    def test_interrupted_import_recovers_at_exhausted_budget_without_key_or_csv(self):
        self.calls = ModelCalls(self.root/'one.sqlite', DeepSeekConfig('synthetic'), max_requests=1)
        self.client = DeepSeekClient(self.calls, transport=self.transport, key_reader=lambda:'synthetic-key')
        http = self.open(raise_server_exceptions=False)
        with patch.object(self.store, 'import_generated_reference', side_effect=OSError('synthetic interruption')):
            self.assertEqual(self.post(http).status_code, 500)
        self.assertEqual(http.get(self.url).json()['requests'][0]['state'], 'completed')
        self.assertEqual(self.calls.status()['remaining_requests'], 0)
        self.store = TaskStore(self.store.path)
        key = Mock(side_effect=AssertionError('recovery must not need a key'))
        self.client = DeepSeekClient(ModelCalls.open_existing(self.calls.path),
                                     transport=self.transport, key_reader=key)
        reopened = self.open()
        result = self.recover(reopened)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.recover(reopened).json(), result.json())
        self.assertEqual(reopened.get(self.url).json()['requests'][0]['state'], 'saved')
        key.assert_not_called()
        self.assertEqual(self.transport.call_count, 1)

    def test_recovery_is_bound_to_task_and_original_accounting(self):
        self.assertEqual(self.post().status_code, 200)
        other = self.store.create('Other paper', 'Synthetic source', 'reproduction')
        self.assertEqual(self.recover(task=other['id']).status_code, 404)
        self.assertEqual(self.recover(request='f'*32).status_code, 404)
        self.client = DeepSeekClient(ModelCalls(self.root/'other.sqlite', DeepSeekConfig('synthetic'),
            max_requests=3), transport=self.transport, key_reader=lambda:'synthetic-key')
        http = self.open()
        self.assertEqual(self.recover(http).status_code, 422)
        self.doc = self.store.get(self.doc['id'])
        self.assertEqual(self.post(http).status_code, 422)
        self.assertEqual(self.client.calls.history(), [])
        self.assertEqual(self.transport.call_count, 1)
        self.assertEqual(http.get(self.url).json()['requests'][0]['state'], 'saved')

    def test_unknown_and_zero_budget_keep_history_without_resending(self):
        self.transport.side_effect = TimeoutError('synthetic uncertainty')
        self.assertEqual(self.post().status_code, 422)
        self.assertEqual(self.http.get(self.url).json()['requests'][0]['state'], 'unknown')
        self.assertEqual(self.recover().status_code, 422)
        self.assertEqual(self.post().status_code, 422)
        self.assertEqual(self.transport.call_count, 1)
        self.doc = self.store.create('Zero budget', 'Synthetic source', 'reproduction')
        self.url = f"/api/tasks/{self.doc['id']}/reference-evidence"
        self.client = DeepSeekClient(ModelCalls(self.root/'zero.sqlite', DeepSeekConfig('synthetic'),
            max_requests=0), transport=self.transport, key_reader=lambda:'synthetic-key')
        http = self.open()
        self.assertEqual(self.post(http).status_code, 422)
        self.assertEqual(http.get(self.url).json()['requests'][0]['state'], 'unresolved')
        self.assertEqual(self.recover(http).status_code, 422)
        self.assertEqual(self.transport.call_count, 1)


if __name__ == '__main__':
    unittest.main()
