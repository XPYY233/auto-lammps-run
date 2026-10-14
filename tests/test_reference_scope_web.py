"""Visible P range choice from saved AI output; synthetic native sources only."""
import importlib.util
import unittest
from unittest.mock import patch

import test_reference_target_analysis_web as target_fixture


@unittest.skipUnless(importlib.util.find_spec('auto_research') is not None, 'native workbench optional')
class ReferenceScopeWebTests(unittest.TestCase):
    def setUp(self):
        self.f = target_fixture.ReferenceTargetAnalysisWebTests()
        self.f.setUp(); self.addCleanup(self.f.doCleanups)
        result = self.f.post()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['state'], 'completed')
        self.url = '/api/tasks/'+self.f.task['id']+'/reference-scopes'
        self.body = dict(target_request_id=self.f.request_id, selected_ids=['fig-5'],
                         exclusion_reason='Keep the other synthetic target in the P inventory.')
        self.body['selected_ids'] = [self.f.document['target_inventory']['targets'][0]['id']]
        self.before = self.f.tasks.get(self.f.task['id'])
        self.calls = self.f.f.calls
        self.used = self.calls.status()['used_requests']

    def preview(self, **changes):
        return self.f.client.post(self.url+'/preview', json={**self.body, **changes}, headers=target_fixture.HEADERS)

    def test_user_preview_and_save_preserve_original_task_targets_and_model_cost(self):
        history = self.f.client.get(self.url)
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.json()['history'], [])
        response = self.preview(); self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        self.assertEqual(preview['scope']['selected_ids'], self.body['selected_ids'])
        self.assertEqual(len(preview['scope']['dependencies']), 1)
        self.assertEqual(preview['scope']['kind'], 'preregistered_scope')
        self.assertFalse(preview['execution_authorized']); self.assertFalse(preview['B_input'])
        body = dict(**self.body, revision=self.before['revision'], scope_id='e'*32,
                    expected_scope_sha256=preview['scope_sha256'])
        saved = self.f.client.post(self.url, json=body, headers=target_fixture.HEADERS)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()['state'], 'recorded')
        self.assertEqual(self.f.client.post(self.url, json=body, headers=target_fixture.HEADERS).json(), saved.json())
        with patch.object(self.f.connections, '_read', side_effect=AssertionError('no model key')):
            reopened = self.f.new_client()
            self.assertEqual(reopened.get(self.url).json()['latest'], saved.json())
        self.assertEqual(self.f.tasks.get(self.f.task['id']), self.before)
        self.assertEqual(self.calls.status()['used_requests'], self.used)
        self.assertEqual(len(self.f.sent), 1)

    def test_scientific_body_injection_or_unrecognized_target_is_rejected_without_save(self):
        for extra in ({'script': 'forbidden'}, {'dependencies': []}, {'source_path': '/private'},
                      {'selected_ids': ['unprovided-target']}):
            response = self.preview(**extra)
            self.assertNotEqual(response.status_code, 200, response.text)
        self.assertEqual(self.f.client.get(self.url).json()['history'], [])
        self.assertEqual(self.calls.status()['used_requests'], self.used)

    def test_cross_origin_and_scope_digest_change_do_not_save(self):
        cross = self.f.client.post(self.url+'/preview', json=self.body,
                                  headers={**target_fixture.HEADERS, 'Origin': 'https://not-local.test'})
        self.assertNotEqual(cross.status_code, 200)
        preview = self.preview().json()
        body = dict(**self.body, revision=self.before['revision'], scope_id='e'*32, expected_scope_sha256='f'*64)
        result = self.f.client.post(self.url, json=body, headers=target_fixture.HEADERS)
        self.assertNotEqual(result.status_code, 200)
        self.assertEqual(self.f.client.get(self.url).json()['history'], [])
        self.assertNotEqual(preview['scope_sha256'], 'f'*64)

    def test_changed_P_package_and_wrong_task_cannot_reuse_the_source(self):
        path = self.f.folder/'manifest.json'; path.write_bytes(path.read_bytes()+b' ')
        self.assertNotEqual(self.preview().status_code, 200)
        other = self.f.tasks.create('Other', 'Permitted input', 'reproduction')
        result = self.f.client.post('/api/tasks/'+other['id']+'/reference-scopes/preview', json=self.body,
                                    headers=target_fixture.HEADERS)
        self.assertNotEqual(result.status_code, 200)
        self.assertEqual(self.calls.status()['used_requests'], self.used)

    def test_paused_task_keeps_read_only_history_but_cannot_preview_or_save(self):
        self.f.tasks.set_paused(self.f.task['id'], self.before['revision'], True)
        self.assertEqual(self.f.client.get(self.url).status_code, 200)
        self.assertNotEqual(self.preview().status_code, 200)
        self.assertEqual(self.calls.status()['used_requests'], self.used)
