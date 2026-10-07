"""Ordinary result projection of collected synthetic geometry, no simulation."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from auto_lammps import analysis
from auto_lammps import coordination_analysis as coordination
from auto_lammps.manifest import canonical, sha256
from auto_lammps.results import ResultsReader, ResultUnavailable, structural_projection
from auto_lammps.tasks import TaskStore
from auto_lammps.web import create_app
import test_analysis_v2 as native_fixtures
import test_candidate_jobs as task_fixtures
import test_coordination_analysis as ordering
import test_results as result_fixtures


@unittest.skipUnless(ordering._ovito_ready(), 'Application OVITO geometry runtime is optional')
class StructuralResultsTests(unittest.TestCase):
    def setUp(self):
        self.pipeline=native_fixtures.StructuralPipelineTests()
        self.addCleanup(self.pipeline.doCleanups)
        self.pipeline.prepare(mixed=True)
        self.saved=self.pipeline.analyze()
        self.assertEqual(self.saved['report']['status'],'analyzed')
        self.f=self.pipeline.f
        self.tasks=TaskStore(self.pipeline.root/'tasks.sqlite')
        self.doc=task_fixtures.frozen_research(self.tasks)
        result_fixtures.link_synthetic_candidate(self.tasks,self.doc,self.pipeline.snapshot.digest)
        self.reader=ResultsReader(self.tasks,self.f.ledger,self.pipeline.root/'collected',self.pipeline.root/'reports')
        self.client=TestClient(create_app(self.tasks,results_reader=self.reader),base_url=result_fixtures.ORIGIN)
        self.addCleanup(self.client.close)
        self.url='/api/tasks/'+self.doc['id']+'/results'
        self.aid=self.saved['context']['analysis_id']

    def test_dump_source_is_not_a_numeric_table_and_derived_values_are_visible(self):
        before=self.f.ledger.events(self.f.request_id)
        with patch.object(coordination,'analyze_trajectory',side_effect=AssertionError('GET must not analyze')):
            response=self.client.get(self.url)
            report=response.json()['evaluations'][0]['requests'][0]['reports'][0]
            self.assertEqual(report['analysis_format'],'numeric_tables_v3')
            self.assertEqual(report['status'],'analyzed')
            source=report['sources'][0]
            self.assertEqual(source['format'],'lammps_dump')
            self.assertNotIn('columns',source)
            self.assertEqual(source['elements'],{'1':'Fe','2':'Ni'})
            result=report['structural_results'][0]
            self.assertEqual(result['chart']['values'],[1.,-1.,1.])
            self.assertEqual(result['frames'][0]['directed'][1],[1,2,216,27,27,1.,.5,-1.])
            self.assertEqual(result['source']['sha256'],sha256(ordering.synthetic_bcc_dump()))
            self.assertEqual(result['parameters']['frames'],{'first':0,'last':0,'stride':1})
            self.assertEqual(result['scientific_status'],'not_evaluated')
            self.assertFalse(result['physics_simulation'])
            preview=self.client.get(self.url+'/'+self.aid+'/tables')
        self.assertEqual(preview.status_code,200,preview.text)
        self.assertEqual([t['file'] for t in preview.json()['tables']],['numeric_0.dat','numeric_1.dat'])
        self.assertEqual(preview.json()['tables'][0]['columns'][0]['name'],'strain')
        self.assertEqual(preview.json()['structural_results'],report['structural_results'])
        for hidden in (str(self.pipeline.root),'adapter_identity','storage_scope_sha256'):
            self.assertNotIn(hidden,response.text)
        self.assertEqual(self.f.ledger.events(self.f.request_id),before)

    def test_v3_previews_keep_per_file_limits_without_old_aggregate_cap(self):
        with patch.object(analysis,'MAX_TABLE_BYTES',80):
            response=self.client.get(self.url+'/'+self.aid+'/tables')
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(len(response.json()['tables']),2)
        with patch.object(analysis,'MAX_TABLE_BYTES',10):
            self.assertEqual(self.client.get(self.url+'/'+self.aid+'/tables').status_code,409)

    def test_changed_dump_bytes_do_not_show_old_chart_preview_or_start_new_analysis(self):
        path=next((self.pipeline.root/'collected').glob('*/payload/output/trajectory.dump'))
        path.chmod(0o600)
        raw=path.read_bytes();path.write_bytes(raw.replace(b'1 1 0 0 0',b'1 1 1 0 0',1))
        self.assertNotEqual(path.read_bytes(),raw)
        before=self.f.ledger.events(self.f.request_id)
        with patch.object(coordination,'analyze_trajectory',side_effect=AssertionError('No repair during GET')):
            response=self.client.get(self.url+'/'+self.aid+'/tables')
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.f.ledger.events(self.f.request_id),before)

    def test_changed_derived_digest_method_pair_chart_and_sampling_are_rejected(self):
        original=self.saved['report']['structural_results'][0]
        for change in ('digest','source','method','pairs','value','chart','frame'):
            item=deepcopy(original)
            if change=='digest':item['derived_sha256']='a'*64
            if change=='source':item['source']['sha256']='b'*64
            if change=='method':item['parameters']['method']='arbitrary_python'
            if change=='pairs':item['aggregates']['symmetric'][0][0]=2
            if change=='value':item['frames'][0]['directed'][0][-1]='not numeric'
            if change=='chart':item['chart']['values'][0]=999
            if change=='frame':item['frames'][0]['frame']=1
            if change!='digest':
                item['derived_sha256']=sha256(canonical({k:v for k,v in item.items() if k!='derived_sha256'}))
            with self.subTest(change=change),self.assertRaises(ValueError):
                structural_projection(item,original['source'])

    def test_other_task_and_changed_receipt_cannot_view_derived_structural_values(self):
        other=task_fixtures.frozen_research(self.tasks)
        self.assertEqual(self.client.get('/api/tasks/'+other['id']+'/results/'+self.aid+'/tables').status_code,409)
        receipt=next((self.pipeline.root/'collected').glob('*/receipt.json'))
        receipt.chmod(0o600);receipt.write_bytes(b'{}')
        report=self.client.get(self.url).json()['evaluations'][0]['requests'][0]['reports'][0]
        self.assertEqual(report['status'],'unavailable')
        self.assertNotIn('structural_results',report)
        self.assertEqual(self.client.get(self.url+'/'+self.aid+'/tables').status_code,409)

    def test_normal_result_discussion_receives_structure_once_with_verified_numeric_sources(self):
        from auto_lammps.model_connections import ModelConnections
        transport=Mock(return_value={'choices':[{'message':{'content':'Synthetic analysis only'}}],
                                     'usage':{'total_tokens':5}})
        connections=ModelConnections(self.tasks,transport=transport,assistant_enabled=True)
        connections.save('deepseek-official','synthetic-model','synthetic-key')
        with TestClient(create_app(self.tasks,results_reader=self.reader,model_connections=connections),
                        base_url=result_fixtures.ORIGIN) as client:
            response=client.post('/api/tasks/'+self.doc['id']+'/discussion',
                json={'provider':'deepseek-official','request_id':'a'*32,'question':'Describe these collected values'},
                headers={'Origin':result_fixtures.ORIGIN,'X-Task-Review':'1'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['state'],'completed')
        payload=transport.call_args.args[4]
        context=json.loads(payload['messages'][-1]['content'])['verified_results']
        report=context['evaluations'][0]['requests'][0]['reports'][0]
        self.assertEqual(report['structural_results'][0]['chart']['values'],[1.,-1.,1.])
        self.assertNotIn('structural_results',context['source_tables'][0])
        self.assertEqual(len(context['source_tables'][0]['tables']),2)
        self.assertIn('frozen_scientific_conditions',context)
        self.assertNotIn(str(self.pipeline.root),json.dumps(context))


if __name__=='__main__':unittest.main()
