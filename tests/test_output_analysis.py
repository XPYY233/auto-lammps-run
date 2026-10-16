"""Normal HTTP analysis of synthetic retained bytes; no API or physics."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock,patch

from fastapi.testclient import TestClient
from auto_lammps.deepseek import DeepSeekClient,DeepSeekConfig,ModelCalls
from auto_lammps.manifest import canonical,sha256
from auto_lammps.output_analysis import OutputAnalysis
from auto_lammps.raw_outputs import RawOutputs
from auto_lammps.scientific_adapters import validate_prepared_messages
from auto_lammps.tasks import TaskError
from auto_lammps.web import create_app
from test_deepseek import response
import test_reference_outputs as reference_fixture
from test_web import ORIGIN,HEADERS


class OutputAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.fx=reference_fixture.ReferenceOutputsTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.tasks=self.fx.fx.tasks;self.ledger=self.fx.fx.ledger;self.task=self.fx.fx.task['id']
        self.source=self.fx.fx.req['id'];self.root=self.fx.root
        self.fx.service.collect(self.task,self.source)
        self.raw=self.fx.raw
        self.calls=ModelCalls(self.root/'analysis-model.sqlite',DeepSeekConfig('synthetic',max_input_bytes=262144),max_requests=8)
        self.transport=Mock(side_effect=lambda *args:(200,response(deepcopy(self.proposal))))
        self.client=DeepSeekClient(self.calls,transport=self.transport,key_reader=lambda:'synthetic-project-key')
        self.connection=lambda task:dict(client=self.client,enabled=True,connection_revision=1,credential_generation=1)
        self.reference=Mock(return_value=dict(sources=[dict(id='method',origin='paper',locator='p1',
            text='Synthetic measurements have dimensionless values; no physical result.')],
            paper=dict(title='Synthetic',doi='10.1234/example'),source_binding=dict(pdf_sha256='a'*64)))
        self.service=OutputAnalysis(self.raw,connection_provider=self.connection,reference_sources=self.reference,model_calls=self.calls)
        self.proposal=dict(status='ready',summary='合成统计与完整直方图',limitations=['Synthetic only'],
            citations=[dict(id='meaning',source_id='method',quote='dimensionless values')],
            column_evidence=[dict(table='samples',column='value',meaning='Synthetic measurement',unit='1',citations=['meaning'])],
            plan=dict(version=1,tables=[dict(id='samples',file='source1',format='whitespace',header_lines=1,
                columns=[dict(name='value',unit='1')])],operations=[
                    dict(id='stats',method='summary',table='samples',column='value'),
                    dict(id='distribution',method='histogram',table='samples',column='value',bins=2,range=[1,3])]))
        self.http=TestClient(create_app(self.tasks,papers=self.fx.fx.papers,output_analysis_service=self.service),base_url=ORIGIN)
        self.addCleanup(self.http.close);self.url=f'/api/tasks/{self.task}/output-analyses'

    def post(self,identifier='a'*32,question='分析合成原始值并绘制完整分布'):
        return self.http.post(self.url,json=dict(request_id=identifier,source_request_id=self.source,question=question),headers=HEADERS)

    def test_real_HTTP_model_adapter_tool_chart_download_and_accounting(self):
        before=self.ledger.get(self.source)
        self.assertEqual(self.http.get(self.url).status_code,200);self.transport.assert_not_called()
        workflow=self.http.get('/api/tasks/'+self.task+'/paper-workflow').json()
        action=next(item for item in workflow['actions'] if item['id']=='analyze_A')
        self.assertTrue(action['enabled']);self.assertEqual(action['endpoint'],self.url)
        self.transport.assert_not_called()
        result=self.post();self.assertEqual(result.status_code,200,result.text)
        report=result.json();self.assertEqual(report['state'],'completed',report)
        summary=report['result']['numeric_results'][0]['values']
        self.assertEqual(summary['mean'],2)
        self.assertEqual(report['result']['numeric_results'][1]['values']['counts'],[1,1])
        self.assertEqual(report['result']['scientific_status'],'not_evaluated')
        self.assertEqual(report['role'],'reference')
        payload=json.loads(self.transport.call_args.args[0]);evidence=json.loads(payload['messages'][-1]['content'])
        original=self.service._stored(self.task,'a'*32)[0]['input']
        validate_prepared_messages('output_postprocessing',payload['messages'],original)
        self.assertIn('retained_output_analysis',evidence)
        self.assertNotIn('private_path',result.text)
        png=next(f for f in report['result']['files'] if f['name'].endswith('.png'))
        downloaded=self.http.get(f"{self.url}/{'a'*32}/files/{png['name']}")
        self.assertEqual(downloaded.status_code,200,downloaded.text[:100] if downloaded.status_code!=200 else '')
        self.assertTrue(downloaded.content.startswith(b'\x89PNG\r\n\x1a\n'))
        self.assertEqual(sha256(downloaded.content),png['sha256'])
        after=self.ledger.get(self.source)
        for field in ('state','actual_core_seconds','dispatch_claimed'):self.assertEqual(after[field],before[field])
        self.assertGreater(after['charge_storage_bytes'],before['charge_storage_bytes'])
        self.assertEqual(self.calls.status()['used_requests'],1)

    def test_same_request_or_completed_question_never_pays_again(self):
        self.assertEqual(self.post().json()['state'],'completed')
        self.assertEqual(self.post().json()['state'],'completed')
        self.assertEqual(self.post('b'*32).json()['request_id'],'a'*32)
        self.assertEqual(self.transport.call_count,1)

    def test_unknown_request_blocks_new_ID_and_keeps_original(self):
        self.transport.side_effect=TimeoutError('SENSITIVE_UPSTREAM_CANARY')
        answer=self.post();self.assertEqual(answer.json()['state'],'unknown',answer.text)
        self.assertNotIn('SENSITIVE_UPSTREAM_CANARY',answer.text)
        self.assertEqual(self.post('b'*32).status_code,422)
        self.assertEqual(self.post().json()['state'],'unknown')
        self.assertEqual(self.transport.call_count,1)

    def test_full_prior_proposal_is_revised_and_three_rounds_are_durable(self):
        self.proposal['citations'][0]['quote']='NOT_IN_SOURCE'
        for key in ('a','b','c'):
            answer=self.post(key*32);self.assertEqual(answer.json()['state'],'invalid_output',answer.text)
        self.assertEqual(self.post('d'*32).status_code,422)
        payload=json.loads(self.transport.call_args.args[0]);previous=json.loads(payload['messages'][-1]['content'])['previous']
        self.assertEqual(previous['complete_previous_answer'],self.proposal)
        self.assertIn('逐字',previous['feedback'])
        service=OutputAnalysis(self.raw,connection_provider=self.connection,reference_sources=self.reference)
        with self.assertRaises(TaskError):service.generate(self.task,'e'*32,self.source,'分析合成原始值并绘制完整分布')
        self.assertEqual(self.calls.status()['used_requests'],3)

    def test_failed_A_can_analyze_partial_bytes_without_changing_scope_or_state(self):
        with self.ledger._transaction() as db:db.execute("UPDATE requests SET state='failed' WHERE id=?",(self.source,))
        report=self.post().json();self.assertEqual(report['state'],'completed',report)
        self.assertEqual(report['result']['scheduler_state'],'failed')
        self.assertEqual(self.ledger.get(self.source)['state'],'failed')

    def test_research_input_never_calls_reference_provider_or_receives_P(self):
        snapshot=self.ledger.evaluation_snapshot
        def research_snapshot(*args,**kwargs):
            value=snapshot(*args,**kwargs);value['identity']['role']='agent';return value
        self.proposal['citations']=[dict(id='meaning',source_id='source1',quote='# Original synthetic output')]
        self.proposal['column_evidence'][0]['unit']='unknown';self.proposal['plan']['tables'][0]['columns'][0]['unit']='unknown'
        with patch.object(self.ledger,'evaluation_snapshot',side_effect=research_snapshot):
            answer=self.post();self.assertEqual(answer.json()['state'],'completed',answer.text)
        self.reference.assert_not_called()
        payload=json.loads(self.transport.call_args.args[0]);evidence=json.loads(payload['messages'][-1]['content'])
        self.assertIsNone(evidence['paper']);self.assertEqual(evidence['role'],'research')
        self.assertFalse(any(s['origin']=='paper' for s in evidence['sources']))

    def test_changed_original_file_blocks_model_and_invalidates_saved_artifacts(self):
        self.assertEqual(self.post().json()['state'],'completed')
        path=next((self.root/'retrieved').glob('*/author-values.txt'));path.write_bytes(b'changed')
        history=self.service.history(self.task);self.assertEqual(history[0]['state'],'source_unavailable')
        with self.assertRaises(TaskError):self.service.download(self.task,'a'*32,'numeric-report.json')
        self.assertEqual(self.transport.call_count,1)

    def test_model_cannot_override_raw_bytes_paths_or_scientific_status(self):
        self.proposal['plan']['tables'][0]['file']='../../secret'
        answer=self.post();self.assertEqual(answer.json()['state'],'invalid_output',answer.text)
        self.assertIsNone(answer.json()['result']);self.assertEqual(self.transport.call_count,1)

    def test_needs_evidence_is_preserved_without_executing_or_claiming_success(self):
        self.proposal.update(status='needs_evidence',plan=None,column_evidence=[],summary='缺少方法依据')
        answer=self.post().json();self.assertEqual(answer['state'],'needs_evidence')
        self.assertIsNone(answer['result']);self.assertEqual(answer['proposal'],self.proposal)

    def test_input_capacity_is_checked_before_new_call(self):
        self.calls.config=DeepSeekConfig('synthetic',max_input_bytes=128)
        with self.assertRaises(ValueError):self.service.generate(self.task,'a'*32,self.source,'Analyze synthetic raw values')
        self.transport.assert_not_called();self.assertEqual(self.calls.status()['used_requests'],0)
