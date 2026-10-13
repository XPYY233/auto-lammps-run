"""Synthetic model selection and task-bound chart artifacts; no paid calls or HPC."""
import json
import unittest
from unittest.mock import Mock

from fastapi.testclient import TestClient

from auto_lammps.deepseek import DeepSeekConfig, ModelCalls
from auto_lammps.model_connections import ModelConnections
from auto_lammps.web import create_app
import test_results


class ResultChartTests(unittest.TestCase):
    def setUp(self):
        self.base=test_results.ResultsTests();self.base.setUp();self.addCleanup(self.base.doCleanups)
        self.base.analysis.run_analysis()
        self.task=self.base.doc['id']
        self.analysis_id=self.base.request()['reports'][0]['id']
        table=self.base.reader.tables(self.task,self.analysis_id)['tables'][0]
        self.file=table['file'];self.x,self.y=[column['name'] for column in table['columns'][:2]]
        self.transport=Mock(return_value={'choices':[{'message':{'content':json.dumps(dict(
            analysis_id=self.analysis_id,file=self.file,x=self.x,y=self.y))}}],
            'usage':{'total_tokens':19}})
        self.calls=ModelCalls(self.base.analysis.root/'chart-model.sqlite',DeepSeekConfig('synthetic-model'),max_requests=2)
        self.connections=ModelConnections(self.base.tasks,transport=self.transport,calls=self.calls)
        self.connections.save('deepseek-official','synthetic-model','synthetic-test-key')
        self.client=TestClient(create_app(self.base.tasks,results_reader=self.base.reader,
            model_connections=self.connections),base_url='http://127.0.0.1:8765')
        self.addCleanup(self.client.close)
        self.url='/api/tasks/'+self.task+'/charts'
        self.headers={'Origin':'http://127.0.0.1:8765','X-Task-Review':'1'}

    def send(self,request_id,question='画出数据关系'):
        return self.client.post(self.url,json=dict(request_id=request_id,provider='deepseek-official',question=question),
                                headers=self.headers)

    def test_explicit_chart_is_verified_downloadable_and_idempotent(self):
        self.assertEqual(self.client.get(self.url).json()['charts'],[])
        self.transport.assert_not_called()
        before=self.base.fixture.ledger.events(self.base.fixture.request_id)
        request_id='c'*32
        first=self.send(request_id)
        self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(first.json()['state'],'completed')
        chart=first.json()['chart']
        self.assertEqual((chart['file'],chart['x'],chart['y']),(self.file,self.x,self.y))
        self.assertEqual(chart['scientific_status'] if 'scientific_status' in chart else 'not_evaluated','not_evaluated')
        download=self.client.get(self.url+'/'+request_id+'/download')
        self.assertEqual(download.status_code,200,download.text)
        self.assertEqual(download.headers['x-data-sha256'],chart['csv_sha256'])
        self.assertEqual(download.content,self.base.reader.chart_data(self.task,self.analysis_id,self.file,self.x,self.y)['csv'])
        self.assertEqual(self.send(request_id).json(),first.json())
        self.transport.assert_called_once()
        with TestClient(create_app(self.base.tasks,results_reader=self.base.reader,
                                   model_connections=self.connections),base_url='http://127.0.0.1:8765') as reopened:
            self.assertEqual(reopened.get(self.url).json()['charts'][0]['chart'],chart)
        self.transport.assert_called_once()
        sent=self.transport.call_args.args[4]
        self.assertEqual(json.loads(sent['messages'][-1]['content'])['scientific_adapter']['stage'],'result_chart')
        self.assertEqual(self.calls.status()['used_requests'],1)
        self.assertEqual(before,self.base.fixture.ledger.events(self.base.fixture.request_id))
        other=self.base.tasks.create(title='other',prompt='other',mode='research')['id']
        self.assertEqual(self.client.get('/api/tasks/'+other+'/charts').json()['charts'],[])
        self.assertEqual(self.client.get('/api/tasks/'+other+'/charts/'+request_id+'/download').status_code,409)
        self.connections.save('deepseek-official','',remove=True)
        self.assertEqual(self.send(request_id).json(),first.json())
        self.transport.assert_called_once()

    def test_changed_report_hides_previously_saved_chart_and_download(self):
        request_id='e'*32
        self.assertEqual(self.send(request_id).json()['state'],'completed')
        path=self.base.analysis.root/'reports'/(self.analysis_id+'.json')
        path.write_bytes(path.read_bytes()+b' ')
        saved=self.client.get(self.url).json()['charts'][0]
        self.assertEqual(saved['state'],'source_unavailable')
        self.assertIsNone(saved['chart'])
        self.assertEqual(self.client.get(self.url+'/'+request_id+'/download').status_code,409)

    def test_index_selection_uses_exact_verified_source_and_columns(self):
        self.transport.return_value={'choices':[{'finish_reason':'stop','message':{
            'content':'{"table_index":0,"x_index":0,"y_index":1}'}}]}
        result=self.send('8'*32).json()
        self.assertEqual(result['state'],'completed')
        self.assertEqual((result['chart']['file'],result['chart']['x'],result['chart']['y']),
            (self.file,self.x,self.y))
        payload=self.transport.call_args.args[4]
        contract=json.loads(payload['messages'][-1]['content'])['scientific_adapter']
        self.assertEqual(set(contract['capabilities']),{'chart_selection'})
        self.assertNotIn('site_thermodynamics_contract',json.dumps(payload))
        receipt=self.calls.history()[0]['receipt']
        self.assertEqual(receipt['structured_output'],{'table_index':0,'x_index':0,'y_index':1})
        self.assertEqual(receipt['finish_reason'],'stop')

    def test_invalid_index_keeps_known_output_and_safe_diagnostic(self):
        self.transport.return_value={'choices':[{'finish_reason':'stop','message':{
            'content':'{"table_index":0,"x_index":0,"y_index":999}'}}]}
        result=self.send('9'*32).json()
        self.assertEqual(result['state'],'invalid_output')
        self.assertEqual(result['chart']['error_code'],'chart_axes_not_declared')
        self.assertEqual(self.calls.history()[0]['receipt']['structured_output']['y_index'],999)
        self.assertEqual(self.send('9'*32).json(),result)
        self.transport.assert_called_once()

    def test_truncated_reply_is_not_accepted_even_with_parseable_selection(self):
        self.transport.return_value={'choices':[{'finish_reason':'length','message':{
            'content':'{"table_index":0,"x_index":0,"y_index":1}'}}]}
        result=self.send('7'*32).json()
        self.assertEqual(result['state'],'invalid_output')
        self.assertEqual(result['chart']['error_code'],'chart_incomplete_generation')
        self.assertEqual(self.calls.history()[0]['receipt']['finish_reason'],'length')

    def test_invalid_model_axis_never_creates_chart_or_retries(self):
        self.transport.return_value={'choices':[{'message':{'content':json.dumps(dict(
            analysis_id=self.analysis_id,file=self.file,x=self.x,y='invented'))}}]}
        request_id='d'*32
        first=self.send(request_id)
        self.assertEqual(first.status_code,200)
        self.assertNotEqual(first.json()['state'],'completed')
        self.assertEqual(self.client.get(self.url+'/'+request_id+'/download').status_code,409)
        self.send(request_id)
        self.transport.assert_called_once()
        self.assertEqual(self.calls.status()['used_requests'],1)

    def test_nonstandard_json_constants_keep_known_failure_and_actual_usage(self):
        self.transport.return_value={'choices':[{'finish_reason':'stop','message':{
            'content':'{"table_index":NaN,"x_index":0,"y_index":1}'}}],
            'usage':{'prompt_tokens':10,'completion_tokens':7,'total_tokens':17}}
        result=self.send('6'*32).json()
        self.assertEqual(result['state'],'invalid_output')
        receipt=self.calls.history()[0]['receipt']
        self.assertEqual(receipt['error_code'],'chart_output_json_invalid')
        self.assertEqual(receipt['usage']['total_tokens'],17)
        self.assertIsNone(receipt['structured_output'])
        self.assertEqual(self.send('6'*32).json(),result)
        self.transport.assert_called_once()

    def test_unavailable_request_records_reason_without_fabricating_plot(self):
        self.transport.return_value={'choices':[{'message':{'content':'{"unavailable":"现有表没有所需物理量"}'}}]}
        request_id='a'*32
        result=self.send(request_id).json()
        self.assertEqual(result['state'],'unavailable')
        self.assertIn('没有所需物理量',result['chart']['message'])
        self.assertEqual(self.client.get(self.url+'/'+request_id+'/download').status_code,409)

    def test_failed_transport_has_one_durable_intent_and_no_hidden_retry(self):
        self.transport.side_effect=RuntimeError('synthetic network interruption')
        request_id='f'*32
        first=self.send(request_id).json()
        self.assertEqual(first['state'],'failed_or_unknown')
        self.assertEqual(self.send(request_id).json(),first)
        self.transport.assert_called_once()
        self.assertEqual(self.calls.status()['used_requests'],1)


if __name__=='__main__':unittest.main()
