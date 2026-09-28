import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest import mock

from auto_lammps.deepseek import DeepSeekClient, DeepSeekConfig, ModelCalls, ModelError
from auto_lammps.model_connections import ModelConnections
from auto_lammps.tasks import TaskError, TaskStore

SECRET='sk-test-secret-value'
MESSAGES=[{'role':'system','content':'Answer with a JSON object only.'},
          {'role':'user','content':'Reply with the JSON object {"ok": true}.'}]


def synthetic_transport(answer='{"ok": true}', usage=True):
    seen=[]
    def transport(body, key, timeout):
        seen.append({'body':json.loads(body), 'key':key, 'timeout':timeout})
        envelope={'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':answer}}]}
        if usage:
            envelope['usage']={'prompt_tokens':11,'completion_tokens':7,'total_tokens':18}
        return 200, json.dumps(envelope).encode()
    return transport, seen


class ModelRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.root.chmod(0o700)
        self.store=TaskStore(self.root/'tasks.sqlite')
        self.connections=ModelConnections(self.store, credentials_directory=self.root/'credentials')
        self.ledger=ModelCalls(self.root/'model.sqlite', DeepSeekConfig(model='ledger-model'), max_requests=5)

    def ledger_calls(self, ledger=None):
        with (ledger or self.ledger).transaction() as db:
            return db.execute('SELECT count(*) FROM calls').fetchone()[0]

    def save(self, provider='deepseek-official', model='deepseek-flash', key=SECRET):
        return self.connections.save(provider, model, key)

    def test_saved_connection_supplies_the_key_and_the_chosen_model(self):
        self.save()
        transport, seen = synthetic_transport()
        client=self.connections.client('deepseek-official', calls=self.ledger, transport=transport)
        result=client.complete_json(uuid.uuid4().hex, MESSAGES)
        self.assertEqual(seen[0]['key'], SECRET)
        self.assertEqual(seen[0]['body']['model'], 'deepseek-flash')
        self.assertEqual(result['receipt']['requested_model'], 'deepseek-flash')

    def test_missing_credential_is_refused_instead_of_using_the_environment(self):
        transport, seen = synthetic_transport()
        with mock.patch.dict(os.environ, {'DEEPSEEK_API_KEY':'sk-other-project'}, clear=False):
            with self.assertRaises(TaskError):
                self.connections.client('deepseek-official', calls=self.ledger, transport=transport)
        self.assertEqual(seen, [])
        self.assertEqual(self.ledger_calls(), 0)

    def test_only_deepseek_is_routed_to_generation_for_now(self):
        self.save(provider='glm', model='glm-4.6')
        with self.assertRaises(TaskError):
            self.connections.client('glm', calls=self.ledger)

    def test_check_spends_one_accounted_request_and_reports_usage(self):
        self.save()
        transport, seen = synthetic_transport()
        result=self.connections.check('deepseek-official', calls=self.ledger, transport=transport)
        self.assertTrue(result['ok'])
        self.assertEqual(result['model'], 'deepseek-flash')
        self.assertEqual(result['usage'], {'prompt_tokens':11,'completion_tokens':7,'total_tokens':18})
        self.assertTrue(result['ledger']['accounted'])
        self.assertRegex(result['ledger']['request_id'], r'^[0-9a-f]{32}$')
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.ledger_calls(), 1)

    def test_check_without_allowance_reports_budget_without_sending(self):
        self.save()
        empty=ModelCalls(self.root/'empty.sqlite', DeepSeekConfig(model='ledger-model'), max_requests=0)
        transport, seen = synthetic_transport()
        result=self.connections.check('deepseek-official', calls=empty, transport=transport)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_class'], 'budget_exhausted')
        self.assertEqual(seen, [])
        self.assertEqual(self.ledger_calls(empty), 0)

    def test_repeating_a_request_id_never_sends_twice(self):
        self.save()
        transport, seen = synthetic_transport()
        identifier=uuid.uuid4().hex
        first=self.connections.check('deepseek-official', calls=self.ledger, transport=transport, identifier=identifier)
        second=self.connections.check('deepseek-official', calls=self.ledger, transport=transport, identifier=identifier)
        self.assertTrue(first['ok'])
        self.assertFalse(second['ok'])
        self.assertEqual(second['error_class'], 'duplicate_request')
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.ledger_calls(), 1)

    def test_failures_are_reported_without_leaking_the_secret(self):
        self.save()
        def rejecting(body, key, timeout):
            return 401, json.dumps({'error':{'message':'Authentication Fails, Your api key: '+key}}).encode()
        result=self.connections.check('deepseek-official', calls=self.ledger, transport=rejecting)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_class'], 'provider_rejected')
        self.assertNotIn(SECRET, json.dumps(result, ensure_ascii=False))
        self.assertEqual(self.ledger_calls(), 1)

    def test_missing_key_path_reports_a_credential_class(self):
        self.save()
        def broken(body, key, timeout):
            raise ModelError('model_key_missing_or_invalid')
        result=self.connections.check('deepseek-official', calls=self.ledger, transport=broken)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_class'], 'missing_or_invalid_credential')


if __name__ == '__main__':
    unittest.main()
