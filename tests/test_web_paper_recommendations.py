"""Ordinary reference-menu HTTP actions, using an accounted synthetic model."""
import unittest
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient
import test_paper_recommendations as fixture
from auto_lammps.model_connections import official_request
from auto_lammps.web import create_app


class WebPaperRecommendationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.PaperRecommendationTests('test_fixed_pair_selection_has_original_titles_citations_and_no_compute_task')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.client=TestClient(create_app(self.f.tasks, model_connections=self.f.connections,
            discovery_library=self.f.library),base_url='http://127.0.0.1:8765')
        self.addCleanup(self.client.close)
        self.headers={'Origin':'http://127.0.0.1:8765','X-Task-Review':'1'}
        self.request=dict(scope_id='1'*32,request_id='2'*32,question='请推荐成套候选',exclude_dois=[])

    def test_read_only_menu_and_history_do_not_call_model_or_create_compute_task(self):
        menu=self.client.get('/api/paper-recommendations')
        self.assertEqual(menu.status_code,200,menu.text)
        self.assertEqual(len(menu.json()['verified_candidates']),1)
        history=self.client.get('/api/paper-recommendations/'+'1'*32)
        self.assertEqual(history.json()['history'],[])
        self.assertEqual(history.json()['status']['max_requests'], 3)
        self.assertFalse(history.json()['status']['recovery_authorized'])
        self.assertTrue(history.json()['status']['can_request'])
        self.f.transport.assert_not_called()
        self.assertEqual(self.f.tasks.list(),[])

    def test_real_reference_action_retains_receipt_and_same_id_does_not_charge_twice(self):
        response=self.client.post('/api/paper-recommendations',json=self.request,headers=self.headers)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['state'],'completed')
        self.assertFalse(response.json()['execution_ready'])
        again=self.client.post('/api/paper-recommendations',json=self.request,headers=self.headers)
        self.assertEqual(again.json(),response.json())
        self.f.transport.assert_called_once()
        self.assertEqual(self.f.tasks.list(),[])
        history=self.client.get('/api/paper-recommendations/'+'1'*32).json()['history']
        self.assertEqual(len(history),1)

    def test_browser_cannot_supply_target_answers_or_model_context_and_requires_origin(self):
        invalid={**self.request,'B_answer':'forbidden','context':{'candidate_index':0}}
        self.assertEqual(self.client.post('/api/paper-recommendations',json=invalid,headers=self.headers).status_code,422)
        self.assertEqual(self.client.post('/api/paper-recommendations',json=self.request).status_code,403)
        self.assertEqual(self.client.post('/api/paper-recommendations',json={**self.request,'request_id':'bad'},headers=self.headers).status_code,422)
        self.f.transport.assert_not_called()

    def test_explicit_doi_exclusion_is_bound_and_empty_menu_does_not_call_model(self):
        response=self.client.post('/api/paper-recommendations',json={**self.request,'exclude_dois':[self.f.row['doi']]},headers=self.headers)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['state'],'unavailable')
        self.f.transport.assert_not_called()

    def test_http_official_rejection_retains_receipt_and_history_reads_no_credential(self):
        self.f.preparer.stop()
        self.f.connections.transport = official_request
        response = Mock(status=402)
        response.read.side_effect = AssertionError('synthetic-connection-key raw supplier body')
        connection = Mock()
        connection.getresponse.return_value = response
        with patch('auto_lammps.model_connections.ssl_context', return_value=object()), \
             patch('auto_lammps.model_connections.http.client.HTTPSConnection', return_value=connection):
            reply = self.client.post('/api/paper-recommendations', json=self.request, headers=self.headers)
            self.assertEqual(reply.status_code, 200, reply.text)
            result = reply.json()
            self.assertEqual(result['state'], 'rejected')
            self.assertEqual(result['diagnostics']['http_status'], 402)
            self.assertEqual(result['diagnostics']['transport_attempts'], 1)
            self.assertEqual(result['document']['error_code'], 'provider_request_failed')
            self.f.connections.save('deepseek-official', '', remove=True)
            with patch.object(self.f.connections, '_read', side_effect=AssertionError('History must not read key')):
                history = self.client.get('/api/paper-recommendations/'+'1'*32).json()['history']
                self.assertEqual(history, [result])
                duplicate = self.client.post('/api/paper-recommendations', json=self.request, headers=self.headers)
                self.assertEqual(duplicate.json(), result)
        response.read.assert_not_called()
        connection.request.assert_called_once()
        self.assertEqual(self.f.calls.status()['used_requests'], 1)
        self.assertNotIn('synthetic-connection-key', reply.text + str(history) + str(self.f.calls.history()))
