"""Ordinary HTTP unknown -> failed replay -> revision -> scope, with fake API."""
from copy import deepcopy
import importlib.util
import json
import unittest
from unittest.mock import patch

import test_workbench_targets_web as fixture


@unittest.skipUnless(importlib.util.find_spec('auto_research') is not None, 'native workbench optional')
class RecoveredReferenceScopeWebTests(unittest.TestCase):
    def test_same_historical_source_chain_can_preview_and_save_without_new_model_calls(self):
        f = fixture.WorkbenchTargetsWebTests(); f.setUp(); self.addCleanup(f.doCleanups)
        f.complete_inventory()
        base = '/api/tasks/' + f.task['id'] + '/reference-target-analysis'
        scopes = '/api/tasks/' + f.task['id'] + '/reference-scopes'
        parent, recovery, revision = 'e' * 32, 'f' * 32, '1' * 32
        with patch('auto_lammps.model_connections.https_transport', side_effect=TimeoutError('Synthetic unknown')):
            first = f.client.post(base, json=dict(request_id=parent), headers=fixture.HEADERS)
        self.assertEqual(first.json()['state'], 'unknown', first.text)
        before = deepcopy(f.f.calls.lookup(parent))
        publications = f.bridge.history(f.task['id'])
        newer = deepcopy(publications[-1]); newer['request_id'] = 'b' * 32
        def invalid_quote(*args):
            status, body = f.response(*args); value = json.loads(body)
            answer = json.loads(value['choices'][0]['message']['content'])
            answer['targets'][0]['citations'][0]['quote'] = 'SYNTHETIC_QUOTE_NOT_PRESENT'
            value['choices'][0]['message']['content'] = json.dumps(answer)
            return status, json.dumps(value).encode()
        with patch.object(f.bridge, 'history', return_value=publications + [newer]):
            with patch('auto_lammps.model_connections.https_transport', invalid_quote):
                second = f.client.post(base + '/unknown-reissue', json=dict(request_id=recovery,
                    unknown_reissue_of=parent, supplier_checked=True, retain_unknown_charge=True),
                    headers=fixture.HEADERS)
            self.assertEqual(second.json()['state'], 'unavailable', second.text)
            third = f.client.post(base + '/' + recovery + '/revise-original',
                json=dict(request_id=revision), headers=fixture.HEADERS)
            self.assertEqual(third.json()['state'], 'completed', third.text)
            used = f.f.calls.status()['used_requests']; sent = len(f.sent)
            body = dict(target_request_id=revision,
                selected_ids=[t['target_id'] for t in third.json()['report']['targets']])
            with patch('auto_lammps.model_connections.https_transport', side_effect=AssertionError('No API in scope')):
                response = f.client.post(scopes + '/preview', json=body, headers=fixture.HEADERS)
                self.assertEqual(response.status_code, 200, response.text)
                preview = response.json()
                self.assertTrue(preview['original_source_preserved'])
                self.assertTrue(preview['historical_source'])
                data = dict(**body, revision=f.tasks.get(f.task['id'])['revision'], scope_id='2' * 32,
                    expected_scope_sha256=preview['scope_sha256'])
                saved = f.client.post(scopes, json=data, headers=fixture.HEADERS)
                self.assertEqual(saved.status_code, 200, saved.text)
                result = saved.json()
                self.assertEqual(result['state'], 'recorded', result)
                self.assertEqual(result['scope'], preview['scope'])
                self.assertFalse(result['execution_authorized'])
                self.assertEqual(f.client.post(scopes, json=data, headers=fixture.HEADERS).json(), result)
                self.assertEqual(f.client.get(scopes).json()['latest'], result)
                self.assertEqual(f.new_client().get(scopes).json()['latest'], result)
            self.assertEqual(f.f.calls.lookup(parent), before)
            self.assertEqual(f.f.calls.status()['used_requests'], used)
            self.assertEqual(len(f.sent), sent)
