"""Real user HTTP protocols with synthetic AI and accounted A fixtures only."""
import json
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from auto_lammps.deepseek import DeepSeekConfig, ModelCalls
from auto_lammps.model_connections import ModelConnections
from auto_lammps.result_charts import ResultCharts
from auto_lammps.web import create_app
import test_reference_chart_sources as reference_fixture


class ReferenceChartUserTests(unittest.TestCase):
    def setUp(self):
        self.base = reference_fixture.ReferenceChartSourceTests(); self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.fixture = self.base.fixture
        self.identifier = self.fixture.task['id']
        option = self.base.selection()
        self.transport = Mock(return_value={'choices': [{'message': {'content': json.dumps(dict(
            analysis_id=option['analysis_id'], file=option['file'], x='cycle', y='A_energy'))}}],
            'usage': {'total_tokens': 17}})
        self.calls = ModelCalls(self.fixture.root/'chart-model.sqlite',
            DeepSeekConfig('synthetic-model'), max_requests=3)
        self.connections = ModelConnections(self.fixture.tasks, transport=self.transport,
            calls=self.calls, assistant_enabled=True)
        self.connections.save('deepseek-official', 'synthetic-model', 'synthetic-test-key')
        self.web = self.client(); self.addCleanup(self.web.close)
        self.url = '/api/tasks/'+self.identifier+'/reference-charts'
        self.headers = {'Origin': 'http://127.0.0.1:8765', 'X-Task-Review': '1'}

    def client(self):
        return TestClient(create_app(self.fixture.tasks, papers=self.fixture.papers,
            reference_evidence_views=self.fixture.views, model_connections=self.connections),
            base_url='http://127.0.0.1:8765')

    def send(self, request_id='c'*32, source=None):
        return self.web.post(self.url, headers=self.headers, json=dict(
            request_id=request_id, provider='deepseek-official', question='绘制已有作者A能量随周期的变化',
            source_sha256=source or self.fixture.document['source_sha256']))

    def test_real_A_route_saves_download_and_restart_without_resend_or_B_leak(self):
        before = self.fixture.ledger.events(self.fixture.request['id'])
        first = self.send(); self.assertEqual(first.status_code, 200, first.text)
        result = first.json(); self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['chart']['scientific_status'], 'not_evaluated')
        self.assertEqual(result['chart']['missing_pair_count'], 1)
        download = self.web.get(self.url+'/'+'c'*32+'/download')
        self.assertEqual(download.content, b'cycle,A_energy\n0,1\n1,\n2,3\n')
        self.assertEqual(download.headers['x-data-sha256'], result['chart']['csv_sha256'])
        self.assertEqual(self.send().json(), result)
        with self.client() as reopened:
            self.assertEqual(reopened.get(self.url).json()['charts'][0], result)
        self.transport.assert_called_once()
        payload = self.transport.call_args.args[4]
        self.assertEqual(json.loads(payload['messages'][-1]['content'])['scientific_adapter']['stage'], 'result_chart')
        for marker in ('P_ANSWER_CANARY', 'P_RECEIPT_CANARY', 'B_FROZEN_PRIVATE_CANARY', 'PRIVATE_'):
            self.assertNotIn(marker, json.dumps(payload)+download.content.decode()+first.text)
        self.assertEqual(before, self.fixture.ledger.events(self.fixture.request['id']))
        ordinary = ResultCharts(self.fixture.tasks, self.connections, None)
        self.assertEqual(ordinary.history(self.identifier), [])
        self.assertEqual(self.calls.status()['used_requests'], 1)

    def test_stale_source_blocks_before_model_and_change_hides_old_plot(self):
        self.assertEqual(self.send(source='a'*64).status_code, 409)
        self.transport.assert_not_called()
        self.assertEqual(self.send().json()['state'], 'completed')
        path = self.fixture.folder/'data.csv'; path.write_bytes(path.read_bytes()+b'3,4,unexpected\n')
        self.assertEqual(self.web.get(self.url).json()['charts'][0]['state'], 'source_unavailable')
        self.assertNotEqual(self.web.get(self.url+'/'+'c'*32+'/download').status_code, 200)
        self.transport.assert_called_once()

    def test_partial_A_diagnostic_stays_partial_and_unscored(self):
        self.base.test_partial_A_keeps_failure_coverage_and_scientific_limits()
        option = self.base.selection()
        self.transport.return_value['choices'][0]['message']['content'] = json.dumps(dict(
            analysis_id=option['analysis_id'], file=option['file'], x='cycle', y='A_energy'))
        result = self.send().json()
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['chart']['output_status'], 'partial')
        self.assertFalse(result['chart']['output_valid'])
        self.assertEqual(result['chart']['coverage'][0]['available'], 1)
        self.assertEqual(result['chart']['scientific_status'], 'not_evaluated')

    def test_invalid_model_source_is_saved_once_never_accepted_or_retried(self):
        self.transport.return_value['choices'][0]['message']['content'] = json.dumps(dict(
            analysis_id='reference-a:'+'a'*64, file='data.csv', x='cycle', y='P_energy'))
        first = self.send().json()
        self.assertEqual(first['state'], 'invalid_output')
        self.assertEqual(self.send().json(), first)
        self.transport.assert_called_once()

    def test_disabled_assistant_blocks_before_model_and_intent(self):
        self.connections.assistant_enabled = False
        result = self.send()
        self.assertEqual(result.status_code, 422)
        self.assertIn('尚未启用', result.json()['detail'])
        self.transport.assert_not_called()
        self.assertEqual(self.web.get(self.url).json()['charts'], [])
        self.assertEqual(self.calls.status()['used_requests'], 0)

    def test_absent_billing_ledger_blocks_before_model_and_intent(self):
        self.connections.calls = None
        result = self.send()
        self.assertEqual(result.status_code, 422)
        self.assertIn('账本尚未配置', result.json()['detail'])
        self.transport.assert_not_called()
        self.assertEqual(self.web.get(self.url).json()['charts'], [])

    def test_corrupt_source_has_safe_conflict_and_zero_model_calls(self):
        (self.fixture.folder/'data.csv').write_bytes(b'not the registered data')
        response = self.send()
        self.assertEqual(response.status_code, 409)
        self.assertNotIn('Traceback', response.text)
        self.transport.assert_not_called()

    def test_source_changed_between_report_and_selection_has_safe_conflict(self):
        original_get = self.fixture.views.get
        def report_then_change(identifier):
            report = original_get(identifier)
            (self.fixture.folder/'data.csv').write_bytes(b'changed after report verification')
            return report
        with patch.object(self.fixture.views, 'get', side_effect=report_then_change):
            response = self.send()
        self.assertEqual(response.status_code, 409)
        self.transport.assert_not_called()
        self.assertEqual(self.calls.status()['used_requests'], 0)

    def test_unavailable_reply_masks_secret_in_browser_and_saved_receipt(self):
        self.transport.return_value['choices'][0]['message']['content'] = json.dumps(
            dict(unavailable='cannot plot synthetic-test-key'))
        result = self.send()
        self.assertEqual(result.json()['state'], 'unavailable')
        self.assertNotIn('synthetic-test-key', result.text+self.web.get(self.url).text)
        with self.fixture.tasks.transaction() as db:
            receipt = db.execute('SELECT document FROM reference_chart_receipts').fetchone()[0]
        self.assertNotIn('synthetic-test-key', receipt)

    def test_known_usage_survives_invalid_completed_response_without_retry(self):
        self.transport.return_value = dict(choices=[], usage=dict(total_tokens=17,
            completion_tokens=True, prompt_tokens=-1, secret='synthetic-test-key'))
        result = self.send().json()
        self.assertEqual(result['state'], 'invalid_output')
        self.assertEqual(result['usage'], dict(total_tokens=17))
        self.assertEqual(self.calls.history()[0]['receipt']['usage'], dict(total_tokens=17))
        self.assertEqual(self.send().json(), result)
        self.transport.assert_called_once()


if __name__ == '__main__':
    unittest.main()
