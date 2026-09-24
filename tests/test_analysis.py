"""Synthetic known-answer arithmetic and real local output collection only."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from auto_lammps.analysis import AnalysisError, AnalysisService, adapter_identity, calculate, parse_table, validate_plan
from auto_lammps.ledger import Conflict, LimitExceeded
from auto_lammps.manifest import canonical, freeze, sha256
import test_outputs as collection_fixtures
import test_runtime_launcher as runtime_fixtures


TABLE={'file':'trajectory.dump','columns':[{'name':'strain','unit':'1'},{'name':'stress','unit':'GPa'}]}
PLAN={'tables':[TABLE],'operations':[
    {'id':method,'method':method,'file':'trajectory.dump','x':'strain','y':'stress','window':[0,2]}
    for method in ('summary','last','linear_fit')]}
DATA=b'# columns: strain stress\n# units: 1 GPa\n-1 -1\n0 1\n1 3\n2 5\n'


class NumericTests(unittest.TestCase):
    def test_hand_calculated_summary_fit_window_and_source_lines(self):
        validate_plan(PLAN,['trajectory.dump'])
        rows=parse_table(DATA,TABLE)
        summary,last,fit=[calculate(rows,TABLE,op) for op in PLAN['operations']]
        self.assertEqual(summary['values'],{'mean':3.0,'sample_std':2.0,'min':1.0,'max':5.0})
        self.assertEqual(last['values'],{'value':5.0,'x':2.0})
        self.assertEqual(fit['values'],{'slope':2.0,'intercept':1.0,'rmse':0.0,'r_squared':1.0})
        self.assertEqual(fit['source_line_ranges'],[[4,6]])
        self.assertEqual(fit['sample_count'],3)
        self.assertEqual(fit['value_units']['slope'],'GPa')
        self.assertEqual(summary['independent_replicate_uncertainty'],'not_estimated')

    def test_declared_plan_rejects_unsupported_method_units_and_posthoc_window(self):
        for change in ('path','unit','method','missing_window','reversed','nan','same_column','duplicate'):
            plan=deepcopy(PLAN)
            if change=='path':plan['tables'][0]['file']='../answer'
            if change=='unit':plan['tables'][0]['columns'][0]['unit']='unknown'
            if change=='method':plan['operations'][0]['method']='exec'
            if change=='missing_window':plan['operations'][0]['window']=None
            if change=='reversed':plan['operations'][0]['window']=[2,1]
            if change=='nan':plan['operations'][0]['window']=[0,float('nan')]
            if change=='same_column':plan['operations'][0]['y']='strain'
            if change=='duplicate':plan['operations'].append(plan['operations'][0])
            with self.subTest(change=change),self.assertRaises(AnalysisError):validate_plan(plan,['trajectory.dump'])

    def test_parser_rejects_wrong_units_headers_nonfinite_and_non_numeric_data(self):
        variants=[DATA.replace(b'GPa',b'bar'),DATA.replace(b'strain stress',b'stress strain'),
                  DATA+b'3 nan\n',DATA+b'3 inf\n',DATA+b'3 1e309\n',DATA+b'3 2 1\n',
                  DATA+b'ERROR simulated failure\n',DATA+b'# unexpected restart\n',DATA+b'\xff',
                  b'# columns: strain stress\n# units: 1 GPa\n']
        for data in variants:
            with self.subTest(data=data[-30:]),self.assertRaises(AnalysisError):parse_table(data,TABLE)
        with patch('auto_lammps.analysis.MAX_TABLE_BYTES',10),self.assertRaises(AnalysisError):parse_table(DATA,TABLE)

    def test_degenerate_fit_single_sample_and_empty_window(self):
        fit=PLAN['operations'][2]
        with self.assertRaises(AnalysisError):calculate([(3,[1.,1.]),(4,[1.,2.]),(5,[1.,3.])],TABLE,fit)
        with self.assertRaises(AnalysisError):calculate([(3,[0.,1.]),(4,[1.,3.])],TABLE,fit)
        constant=calculate([(3,[0.,1.]),(4,[1.,1.]),(5,[2.,1.])],TABLE,fit)
        self.assertEqual(constant['values']['slope'],0)
        self.assertIsNone(constant['values']['r_squared'])
        summary=calculate([(3,[1.,2.])],TABLE,PLAN['operations'][0])
        self.assertIsNone(summary['values']['sample_std'])
        with self.assertRaises(AnalysisError):calculate([(3,[3.,2.])],TABLE,PLAN['operations'][0])


class AnalysisIntegrationTests(unittest.TestCase):
    def setUp(self):
        fixture=collection_fixtures.OutputTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture=fixture
        self.root=fixture.root
        self.spec={'proposal':{'quantity':'synthetic labeled curve','method':'synthetic arithmetic only',
                              'files':['trajectory.dump'],'plan':deepcopy(PLAN)},
                   'outputs':runtime_fixtures.OUTPUTS,'implementation_status':'numeric_tables_v1',
                   'adapter_identity':adapter_identity()}
        source=self.root/'analysis-input'
        source.mkdir()
        (source/'input.in').write_bytes(b'# synthetic placeholder; never executed\n')
        (source/'analysis.json').write_bytes(canonical(self.spec))
        self.snapshot=freeze(source,self.root/'analysis-snapshots',files={'input.in':'lammps_input','analysis.json':'analysis_spec'},
            entrypoint='input.in',resources=runtime_fixtures.RESOURCES,provenance={'task_sha256':'a'*64,
            'analysis_sha256':sha256(canonical(self.spec)),'software_sha256':'b'*64})
        old=fixture.digest;fixture.digest=self.snapshot.digest
        fixture.local_command=[fixture.digest if value==old else value for value in fixture.local_command]
        (fixture.case/'manifest.json').unlink()  # Replace immutable synthetic setup, before any collection.
        fixture.private(fixture.case/'manifest.json',(self.snapshot.path/'manifest.json').read_bytes())
        intent=json.loads((fixture.case/'execution-intent.json').read_bytes())
        intent['manifest_sha256']=fixture.digest
        fixture.private(fixture.case/'execution-intent.json',canonical(intent))
        with fixture.ledger._transaction() as db:
            db.execute('UPDATE requests SET manifest_sha256=? WHERE id=?',(fixture.digest,fixture.request_id))
        fixture.private(fixture.case/'output/trajectory.dump',DATA)
        self.service=AnalysisService(fixture.collector,self.root/'reports')

    def run_analysis(self):
        with self.fixture.local_transfer():
            return self.service.run(self.fixture.request_id,self.snapshot)

    def test_collected_output_to_persistent_analysis_and_idempotent_history(self):
        first=self.run_analysis()
        second=self.run_analysis()
        self.assertEqual(first,second)
        report=first['report']
        self.assertEqual(report['status'],'analyzed')
        self.assertEqual(report['scientific_status'],'not_evaluated')
        self.assertEqual(report['results'][2]['values']['slope'],2)
        self.assertEqual(report['sources'][0]['sha256'],sha256(DATA))
        kinds=[r['kind'] for r in self.fixture.ledger.events(self.fixture.request_id)]
        self.assertEqual(kinds.count('analysis_reserved'),1)
        self.assertEqual(kinds.count('analysis_saved'),1)
        self.assertEqual(self.fixture.ledger.evaluation_snapshot(self.fixture.evaluation)['dispatch_claims'],1)
        self.assertEqual(self.fixture.ledger.get(self.fixture.request_id)['charge_storage_bytes'],
                         2*runtime_fixtures.RESOURCES.storage_bytes+262144+65536)

    def test_failed_execution_and_wrong_units_keep_failed_analysis_record(self):
        self.fixture.private(self.fixture.case/'output/trajectory.dump',DATA.replace(b'GPa',b'bar'))
        result=self.run_analysis()
        self.assertEqual(result['report']['status'],'analysis_failed')
        self.assertIn('units',result['report']['reason'])
        self.assertEqual(self.run_analysis(),result)

    def test_nonzero_execution_cannot_produce_properties(self):
        result=json.loads((self.fixture.case/'execution-result.json').read_bytes());result['returncode']=1
        self.fixture.private(self.fixture.case/'execution-result.json',canonical(result))
        result=self.run_analysis()
        self.assertEqual(result['report']['status'],'analysis_failed')
        self.assertNotIn('results',result['report'])

    def test_post_execution_analysis_implementation_change_is_rejected(self):
        changed={**adapter_identity(),'source_sha256':'f'*64}
        with patch('auto_lammps.analysis.adapter_identity',return_value=changed):result=self.run_analysis()
        self.assertEqual(result['report']['status'],'analysis_failed')
        self.assertIn('frozen plan',result['report']['reason'])

    def test_saved_report_tampering_is_rejected_against_ledger(self):
        result=self.run_analysis()
        path=self.root/'reports'/(result['context']['analysis_id']+'.json')
        value=json.loads(path.read_bytes());value['report']['results'][0]['values']['mean']=999
        path.write_bytes(canonical(value))
        with self.assertRaises(Conflict):self.run_analysis()

    def test_different_report_store_reserves_another_copy(self):
        first=self.run_analysis()
        before=self.fixture.ledger.get(self.fixture.request_id)['charge_storage_bytes']
        second=AnalysisService(self.fixture.collector,self.root/'other-reports').run(self.fixture.request_id,self.snapshot)
        self.assertNotEqual(first['context']['analysis_id'],second['context']['analysis_id'])
        self.assertEqual(first['report'],second['report'])
        self.assertEqual(self.fixture.ledger.get(self.fixture.request_id)['charge_storage_bytes']-before,65536)

    def test_cached_report_does_not_hide_changed_frozen_input(self):
        self.run_analysis()
        path=self.snapshot.path/'analysis.json';path.chmod(0o600);path.write_bytes(b'changed')
        with self.assertRaises(ValueError):self.run_analysis()

    def test_report_write_failure_keeps_reservation_and_never_records_publication(self):
        with patch('auto_lammps.analysis._write_new',side_effect=OSError('synthetic write failure')):
            with self.assertRaises(OSError):self.run_analysis()
        events=self.fixture.ledger.events(self.fixture.request_id)
        self.assertEqual(sum(e['kind']=='analysis_reserved' for e in events),1)
        self.assertEqual(sum(e['kind']=='analysis_saved' for e in events),0)
        recovered=self.run_analysis()
        self.assertEqual(recovered['report']['status'],'analyzed')
        events=self.fixture.ledger.events(self.fixture.request_id)
        self.assertEqual(sum(e['kind']=='analysis_reserved' for e in events),1)
        self.assertEqual(sum(e['kind']=='analysis_saved' for e in events),1)

    def test_report_budget_is_reserved_before_analysis(self):
        with self.fixture.local_transfer():self.fixture.collector.fetch(self.fixture.request_id)
        with self.fixture.ledger._transaction() as db:
            db.execute('UPDATE requests SET charge_storage_bytes=? WHERE id=?',
                       (16*1024*1024-65535,self.fixture.request_id))
        with patch('auto_lammps.analysis.analyze_collected') as analyze:
            with self.assertRaises(LimitExceeded):self.run_analysis()
            analyze.assert_not_called()


if __name__=='__main__':unittest.main()
