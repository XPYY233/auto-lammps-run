"""Task-scoped browser reads from real synthetic collection/analysis receipts."""
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import unittest
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient
from auto_lammps.candidate_jobs import CandidateHistory
from auto_lammps.manifest import canonical, sha256
from auto_lammps.results import ResultsReader
from auto_lammps.tasks import TaskStore
from auto_lammps.web import create_app
import test_analysis as analysis_fixtures
import test_candidate_jobs as task_fixtures
import test_runtime_launcher as runtime_fixtures

ORIGIN='http://127.0.0.1:8765'


def link_synthetic_candidate(tasks, doc, digest, condition_hash=None):
    """Seed isolated fixture preparation history; no generated-model claim."""
    history=CandidateHistory(tasks)
    job=uuid.uuid4().hex
    condition_hash=condition_hash or sha256(tasks.export(doc['id']))
    with tasks.transaction() as db:
        db.execute('INSERT INTO candidate_jobs VALUES (?,?,?,?,?,?)',
            (job,doc['id'],doc['revision'],condition_hash,
             'f'*64,datetime.now(timezone.utc).isoformat()))
        history._event(db,job,'prepared',{'snapshot_sha256':digest,
                                       'summary':'合成结果页验收；未调用模型或运行模拟。'})


class ResultsTests(unittest.TestCase):
    def setUp(self):
        self.analysis=analysis_fixtures.AnalysisIntegrationTests()
        self.analysis.setUp()
        self.addCleanup(self.analysis.doCleanups)
        self.fixture=self.analysis.fixture
        self.tasks=TaskStore(self.analysis.root/'tasks.sqlite')
        self.doc=task_fixtures.frozen_research(self.tasks)
        link_synthetic_candidate(self.tasks,self.doc,self.analysis.snapshot.digest)
        self.reader=ResultsReader(self.tasks,self.fixture.ledger,self.analysis.root/'collected',self.analysis.root/'reports')
        self.client=TestClient(create_app(self.tasks,results_reader=self.reader),base_url=ORIGIN)
        self.addCleanup(self.client.close)
        self.url='/api/tasks/'+self.doc['id']+'/results'

    def request(self):
        response=self.client.get(self.url)
        self.assertEqual(response.status_code,200,response.text)
        return response.json()['evaluations'][0]['requests'][0]

    def test_numeric_results_download_provenance_and_refresh_are_read_only(self):
        saved=self.analysis.run_analysis()
        before=self.fixture.ledger.events(self.fixture.request_id)
        with ExitStack() as stack:
            for target in ('auto_lammps.outputs.transfer','auto_lammps.outputs.OutputCollector.fetch',
                           'auto_lammps.analysis.AnalysisService.run','auto_lammps.ledger.Ledger.begin_dispatch'):
                stack.enter_context(patch(target,side_effect=AssertionError('GET must not execute')))
            first=self.client.get(self.url)
            second=self.client.get(self.url)
            self.assertEqual(first.json(),second.json())
            value=first.json();group=value['evaluations'][0];request=group['requests'][0]
            self.assertEqual((group['dispatch_count'],group['used_attempts'],group['max_attempts']),(1,1,2))
            self.assertEqual(request['dispatch_ordinal'],1)
            report=request['reports'][0]
            self.assertEqual(report['results'][2]['values']['slope'],2)
            self.assertEqual(report['results'][2]['value_units']['slope'],'GPa')
            self.assertEqual(report['results'][2]['source_line_ranges'],[[4,6]])
            self.assertEqual(report['sources'][0]['sha256'],sha256(analysis_fixtures.DATA))
            self.assertEqual(report['scientific_status'],'not_evaluated')
            download=self.client.get(self.url+'/'+saved['context']['analysis_id']+'/download')
            self.assertEqual(download.status_code,200)
            self.assertEqual(download.json(),report)
            for hidden in (str(self.analysis.root),'storage_scope_sha256','payload','job_id','adapter_identity'):
                self.assertNotIn(hidden,first.text)
                self.assertNotIn(hidden,download.text)
        self.assertEqual(before,self.fixture.ledger.events(self.fixture.request_id))

    def test_persistence_reopens_and_reference_roles_never_enter_product_view(self):
        self.analysis.run_analysis()
        ledger=self.fixture.ledger
        for role in ('reference','development','analysis'):
            evaluation=ledger.register_evaluation('synthetic',task_sha256='c'*64,repetition=0,role=role,system_sha256='d'*64)
            row=ledger.reserve(evaluation,role,self.analysis.snapshot.digest,runtime_fixtures.RESOURCES)
            ledger.cancel_intent(row['id'])
        expected=self.client.get(self.url).json()
        self.assertEqual(len(expected['evaluations']),1)
        tasks=TaskStore(self.tasks.path)
        reader=ResultsReader(tasks,ledger,self.analysis.root/'collected',self.analysis.root/'reports')
        with TestClient(create_app(tasks,results_reader=reader),base_url=ORIGIN) as reopened:
            self.assertEqual(reopened.get(self.url).json(),expected)

    def test_different_task_cannot_download_another_report(self):
        saved=self.analysis.run_analysis()
        other=task_fixtures.frozen_research(self.tasks)
        url='/api/tasks/'+other['id']+'/results'
        self.assertEqual(self.client.get(url).json()['evaluations'],[])
        self.assertEqual(self.client.get(url+'/'+saved['context']['analysis_id']+'/download').status_code,409)
        self.assertEqual(self.client.get('/api/tasks/'+'f'*32+'/results').status_code,404)

    def test_changed_report_or_collection_receipt_hides_values(self):
        saved=self.analysis.run_analysis()
        report_path=self.analysis.root/'reports'/(saved['context']['analysis_id']+'.json')
        receipt=next((self.analysis.root/'collected').glob('*/receipt.json'))
        for path in (report_path,receipt):
            raw=path.read_bytes()
            with self.subTest(name=path.name):
                path.write_bytes(b'{}')
                result=self.request()['reports'][0]
                self.assertEqual(result['status'],'unavailable')
                self.assertNotIn('results',result)
                self.assertEqual(self.client.get(self.url+'/'+saved['context']['analysis_id']+'/download').status_code,409)
                path.write_bytes(raw)

    def test_failed_analysis_is_not_scientific_success_and_diagnostics_are_private(self):
        self.fixture.private(self.fixture.case/'output/trajectory.dump',analysis_fixtures.DATA.replace(b'GPa',b'bar'))
        self.analysis.run_analysis()
        result=self.request()['reports'][0]
        self.assertEqual(self.request()['stage'],'分析未完成')
        self.assertEqual(result['status'],'analysis_failed')
        self.assertNotIn('results',result)
        self.assertNotIn('reason',result)

    def test_second_attempt_with_new_manifest_keeps_full_evaluation_and_unknown_counts(self):
        self.analysis.run_analysis()
        ledger=self.fixture.ledger
        row=ledger.reserve(self.fixture.evaluation,'repair','e'*64,runtime_fixtures.RESOURCES)
        group=self.client.get(self.url).json()['evaluations'][0]
        self.assertEqual((group['dispatch_count'],group['pending_attempts'],group['used_attempts']),(1,1,2))
        self.assertIsNone(group['requests'][1]['dispatch_ordinal'])
        ledger.begin_dispatch(row['id']);ledger.uncertain(row['id'],{'private':'never exposed'})
        group=self.client.get(self.url).json()['evaluations'][0]
        self.assertEqual((group['dispatch_count'],group['pending_attempts'],group['used_attempts']),(2,0,2))
        self.assertEqual(group['requests'][1]['state'],'unknown')
        self.assertEqual(group['requests'][1]['dispatch_ordinal'],2)
        self.assertNotIn('never exposed',json.dumps(group))

    def test_scheduler_contradiction_withholds_previously_saved_report(self):
        self.analysis.run_analysis()
        with self.fixture.ledger._transaction() as db:
            db.execute("UPDATE requests SET state='reconcile_required' WHERE id=?",(self.fixture.request_id,))
        request=self.request()
        self.assertEqual(request['reports'][0]['status'],'unavailable')
        self.assertEqual(request['state_label'],'记录存在矛盾 · 待核对')

    def test_no_collection_is_not_report_and_unconfigured_service_is_explicit(self):
        self.assertEqual(self.request()['stage'],'结果待回收')
        self.assertEqual(self.request()['reports'],[])
        with TestClient(create_app(self.tasks),base_url=ORIGIN) as app:
            self.assertFalse(app.get(self.url).json()['configured'])
            self.assertEqual(app.get(self.url+'/'+'a'*64+'/download').status_code,404)

    def test_stale_condition_link_cannot_show_results(self):
        other=task_fixtures.frozen_research(self.tasks)
        link_synthetic_candidate(self.tasks,other,self.analysis.snapshot.digest,'0'*64)
        self.assertEqual(self.client.get('/api/tasks/'+other['id']+'/results').status_code,409)


if __name__=='__main__':unittest.main()
