"""Real local collection/analysis and killed followers; all scheduler data synthetic."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from io import StringIO
from contextlib import redirect_stdout
import json
import multiprocessing as mp
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from auto_lammps import outputs
from auto_lammps.analysis import AnalysisService
from auto_lammps.following import FollowingService
from auto_lammps.ledger import Conflict, Ledger, Policy
from auto_lammps.manifest import canonical
from auto_lammps.reconciliation import ReconciliationService
from auto_lammps.slurm_read import Observation, SlurmReader
from auto_lammps.staging import StageEndpoint
import test_analysis as fixtures
import test_runtime_launcher as runtime_fixtures


def make_follower(root, endpoint, snapshots, *, max_polls=3):
    ledger=Ledger(Path(root)/'follow-ledger.sqlite')
    collector=outputs.OutputCollector(ledger,StageEndpoint(**endpoint),Path(root)/'collected')
    reader=SlurmReader(endpoint['host_alias'],Path(root)/'queries',max_bytes=1024)
    return FollowingService(ledger,ReconciliationService(ledger,reader),AnalysisService(collector,Path(root)/'reports'),
                            snapshots,max_polls=max_polls,interval_seconds=15)


def crash_follower(root, endpoint, snapshots, request_id, local_command, phase):
    service=make_follower(root,endpoint,snapshots)
    real_transfer=outputs.transfer
    def transfer(*args,**kwargs):
        if phase=='transfer':os._exit(23)
        return real_transfer(local_command,args[1],**kwargs)
    original=service.ledger.following_progress
    def progress(request,state,reason=''):
        if phase=='report_saved' and state=='analyzed':os._exit(24)
        return original(request,state,reason)
    with patch.object(outputs,'transfer',side_effect=transfer),patch.object(service.ledger,'following_progress',side_effect=progress):
        service.advance(request_id)


class FollowingTests(unittest.TestCase):
    def setUp(self):
        self.analysis=fixtures.AnalysisIntegrationTests();self.analysis.setUp()
        self.addCleanup(self.analysis.doCleanups)
        self.f=self.analysis.fixture;self.root=self.analysis.root
        # A fresh ledger starts queued; the output bytes are explicitly synthetic.
        self.ledger=Ledger(self.root/'follow-ledger.sqlite')
        self.ledger.create_campaign('synthetic',Policy(1000,8,120,128*1024*1024,16*1024*1024,1,'a'*64))
        self.evaluation=self.ledger.register_evaluation('synthetic',task_sha256='a'*64,repetition=0,
                                                       role='agent',system_sha256='a'*64)
        row=self.ledger.reserve(self.evaluation,'first',self.analysis.snapshot.digest,runtime_fixtures.RESOURCES)
        self.request_id=row['id'];old=self.f.request_id
        new=self.f.case.parent/self.request_id;self.f.case.rename(new);self.f.case=new
        self.f.local_command=[self.request_id if value==old else value for value in self.f.local_command]
        intent=json.loads((new/'execution-intent.json').read_bytes());intent['request_id']=self.request_id
        self.f.private(new/'execution-intent.json',canonical(intent))
        result=json.loads((new/'execution-result.json').read_bytes());result['request_id']=self.request_id
        self.f.private(new/'execution-result.json',canonical(result))
        self.f.ledger=self.ledger;self.f.request_id=self.request_id;self.f.evaluation=self.evaluation
        self.f.collector.ledger=self.ledger
        self.ledger.begin_dispatch(self.request_id);self.ledger.accepted(self.request_id,'123',{'synthetic':True})
        self.service=self.reopen()

    def reopen(self,**kwargs):
        return make_follower(self.root,asdict(self.f.endpoint),self.analysis.snapshot.path.parent,**kwargs)

    def complete(self,state='completed'):
        self.ledger.observe(self.request_id,'123',state,{'synthetic':True})
        self.ledger.account(self.request_id,1,'a'*64)

    def events(self,kind):
        return [e for e in self.ledger.events(self.request_id) if e['kind']==kind]

    def test_scheduler_to_collection_analysis_and_restart_never_resubmits(self):
        observation=Observation('completed','123',1,evidence_sha256='a'*64)
        with (patch.object(self.service.reconciliation.reader,'lookup',return_value=observation) as query,
                self.f.local_transfer() as transfer):
            result=self.service.advance(self.request_id)
        self.assertEqual(result['state'],'analyzed')
        self.assertEqual(result['scientific_status'],'not_evaluated')
        self.assertEqual(query.call_count,1);self.assertEqual(transfer.call_count,1)
        self.assertEqual(len(self.events('accounting_final')),1)
        self.assertEqual(len(self.events('output_fetch_started')),1)
        self.assertEqual(len(self.events('analysis_saved')),1)
        self.assertEqual(len(self.events('dispatch_intent')),1)
        before=self.ledger.events(self.request_id)
        with patch.object(outputs,'transfer',side_effect=AssertionError('No repeat transport')):
            self.assertEqual(self.reopen().advance(self.request_id),result)
        self.assertEqual(self.ledger.events(self.request_id),before)
        self.assertEqual(self.ledger.get(self.request_id)['charge_storage_bytes'],
                         2*runtime_fixtures.RESOURCES.storage_bytes+262144+65536+self.service.poll_storage_bytes)

    def test_poll_cadence_and_limit_persist_across_restart(self):
        service=self.reopen(max_polls=1);now=time.time()
        with patch.object(service.reconciliation.reader,'lookup',return_value=Observation('unknown',reason='not_visible',evidence_sha256='a'*64)):
            with patch('auto_lammps.ledger.time.time',return_value=now):
                self.assertEqual(service.advance(self.request_id)['state'],'waiting')
        result=self.reopen(max_polls=1).advance(self.request_id)
        self.assertEqual(result['reason'],'poll_or_storage_limit')
        self.assertEqual(len(self.events('following_poll')),1)
        with self.assertRaises(Conflict):self.reopen(max_polls=2).advance(self.request_id)
        self.assertEqual(len(self.events('dispatch_intent')),1)

    def test_supervised_loop_advances_running_to_analyzed_automatically(self):
        clock=[time.time()];statuses=[]
        class Timer:
            def is_set(self):return False
            def wait(self,seconds):clock[0]+=seconds
        observations=[Observation('running','123',evidence_sha256='a'*64),
                      Observation('completed','123',1,evidence_sha256='b'*64)]
        with (patch.object(self.service.reconciliation.reader,'lookup',side_effect=observations),
              patch('auto_lammps.ledger.time.time',side_effect=lambda:clock[0]),self.f.local_transfer()):
            result=self.service.run(self.request_id,stop=Timer(),notify=lambda row:statuses.append(row['state']))
        self.assertEqual(statuses,['waiting','analyzed'])
        self.assertEqual(result['state'],'analyzed')
        self.assertEqual(len(self.events('following_poll')),2)
        self.assertEqual(len(self.events('dispatch_intent')),1)

    def test_supervisor_stop_leaves_the_existing_job_untouched(self):
        stop=threading.Event();stop.set();before=self.ledger.events(self.request_id)
        self.assertEqual(self.service.run(self.request_id,stop=stop)['state'],'stopped')
        self.assertEqual(self.ledger.events(self.request_id),before)

    def test_scheduler_conflict_stops_before_output_collection(self):
        observation=Observation('unknown','123',reason='restarted_allocation',evidence_sha256='a'*64)
        with (patch.object(self.service.reconciliation.reader,'lookup',return_value=observation),
              patch.object(outputs,'transfer') as transfer):
            result=self.service.advance(self.request_id)
        self.assertEqual(result['reason'],'scheduler_conflict');transfer.assert_not_called()

    def test_not_due_and_transient_query_failure_do_not_download(self):
        now=time.time()
        with (patch.object(self.service.reconciliation.reader,'lookup',side_effect=OSError('private diagnostic')) as query,
                patch.object(outputs,'transfer') as transfer):
            with patch('auto_lammps.ledger.time.time',return_value=now):
                first=self.service.advance(self.request_id)
                second=self.service.advance(self.request_id)
            self.assertEqual(first['reason'],'scheduler_read_failed')
            self.assertEqual(second['reason'],'poll_not_due')
            self.assertEqual(query.call_count,1);transfer.assert_not_called()
        self.assertNotIn('private diagnostic',json.dumps(self.ledger.events(self.request_id)))

    def test_poll_storage_budget_precedes_remote_query(self):
        with self.ledger._transaction() as db:
            db.execute('UPDATE requests SET charge_storage_bytes=? WHERE id=?',(16*1024*1024,self.request_id))
        with patch.object(self.service.reconciliation.reader,'lookup') as query:
            self.assertEqual(self.service.advance(self.request_id)['reason'],'poll_or_storage_limit')
            query.assert_not_called()

    def test_failed_job_collects_diagnostics_without_numeric_analysis(self):
        self.complete('failed')
        with self.f.local_transfer(),patch.object(self.service.analysis,'run') as analyze:
            result=self.service.advance(self.request_id)
        self.assertEqual(result['state'],'diagnostics_saved');analyze.assert_not_called()
        self.assertEqual(len(self.events('analysis_saved')),0)

    def test_invalid_data_keeps_analysis_failure(self):
        self.complete()
        self.f.private(self.f.case/'output/trajectory.dump',fixtures.DATA.replace(b'GPa',b'bar'))
        with self.f.local_transfer():result=self.service.advance(self.request_id)
        self.assertEqual(result['state'],'analysis_failed')
        self.assertEqual(self.reopen().advance(self.request_id),result)

    def test_failed_download_is_not_automatically_retried(self):
        self.complete()
        with patch.object(outputs,'transfer',side_effect=OSError('synthetic disconnected')) as transfer:
            first=self.service.advance(self.request_id);second=self.reopen().advance(self.request_id)
        self.assertEqual(first,second);self.assertEqual(first['reason'],'collection_failed')
        self.assertEqual(transfer.call_count,1);self.assertEqual(len(self.events('output_fetch_started')),1)

    def test_hard_exit_during_download_keeps_partial_charge_without_retry(self):
        self.complete();self.crash('transfer',23)
        with patch.object(outputs,'transfer') as transfer:result=self.reopen().advance(self.request_id)
        transfer.assert_not_called()
        self.assertEqual(result['reason'],'collection_incomplete')
        self.assertEqual(len(self.events('output_fetch_started')),1)
        self.assertEqual(self.ledger.get(self.request_id)['charge_storage_bytes'],
                         2*runtime_fixtures.RESOURCES.storage_bytes+262144)

    def test_hard_exit_after_report_saved_resumes_without_more_compute_or_download(self):
        self.complete();self.crash('report_saved',24)
        with patch.object(outputs,'transfer',side_effect=AssertionError('Already collected')):
            result=self.reopen().advance(self.request_id)
        self.assertEqual(result['state'],'analyzed')
        self.assertEqual(len(self.events('analysis_saved')),1)
        self.assertEqual(len(self.events('dispatch_intent')),1)

    def crash(self,phase,code):
        process=mp.get_context('spawn').Process(target=crash_follower,args=(str(self.root),asdict(self.f.endpoint),
            str(self.analysis.snapshot.path.parent),self.request_id,self.f.local_command,phase))
        process.start();process.join(10)
        if process.is_alive():process.kill();process.join()
        self.assertEqual(process.exitcode,code)

    def test_concurrent_worker_is_visible_without_duplicate_transfer(self):
        self.complete();started=threading.Event();release=threading.Event()
        original=outputs.transfer
        def transfer(argv,receiver,**kwargs):
            started.set()
            if not release.wait(5):raise RuntimeError('Test synchronization failed')
            return original(self.f.local_command,receiver,**kwargs)
        with patch.object(outputs,'transfer',side_effect=transfer),ThreadPoolExecutor(1) as pool:
            first=pool.submit(self.service.advance,self.request_id)
            try:
                self.assertTrue(started.wait(5))
                self.assertEqual(self.reopen().advance(self.request_id)['reason'],'worker_busy')
            finally:release.set()
            self.assertEqual(first.result(timeout=5)['state'],'analyzed')
        self.assertEqual(len(self.events('output_fetch_started')),1)

    def test_changed_input_or_conflict_prevents_transport(self):
        path=self.analysis.snapshot.path/'analysis.json';path.chmod(0o600);path.write_bytes(b'changed')
        with patch.object(self.service.reconciliation.reader,'lookup') as query:
            self.assertEqual(self.service.advance(self.request_id)['reason'],'input_verification_failed')
            query.assert_not_called()

    def test_prepared_request_cannot_be_started_by_follower(self):
        self.complete()
        row=self.ledger.reserve(self.evaluation,'second',self.analysis.snapshot.digest,runtime_fixtures.RESOURCES)
        with self.assertRaises(Conflict):self.service.advance(row['id'])
        self.assertFalse(self.ledger.get(row['id'])['dispatch_claimed'])

    def test_cli_follow_config_runs_same_services_and_emits_status(self):
        from auto_lammps.worker import main
        self.complete()
        config=dict(snapshots_directory=str(self.analysis.snapshot.path.parent),collections_directory=str(self.root/'collected'),
            reports_directory=str(self.root/'reports'),endpoint=asdict(self.f.endpoint),max_polls=3,interval_seconds=15,query_max_bytes=1024)
        path=self.root/'follow.json';self.f.private(path,canonical(config));output=StringIO()
        with self.f.local_transfer(),redirect_stdout(output):
            code=main(['--ledger',str(self.ledger.path),'--ssh-alias',self.f.endpoint.host_alias,
                       '--audit-directory',str(self.root/'queries'),'--request-id',self.request_id,'--follow-config',str(path)])
        self.assertEqual(code,0);self.assertEqual(json.loads(output.getvalue())['state'],'analyzed')

    def test_same_task_results_expose_automatic_progress_and_one_submission(self):
        from auto_lammps.results import ResultsReader
        from auto_lammps.tasks import TaskStore
        from test_candidate_jobs import frozen_research
        from test_results import link_synthetic_candidate
        self.complete()
        with self.f.local_transfer():self.service.advance(self.request_id)
        tasks=TaskStore(self.root/'tasks.sqlite');doc=frozen_research(tasks)
        link_synthetic_candidate(tasks,doc,self.analysis.snapshot.digest)
        reader=ResultsReader(tasks,self.ledger,self.root/'collected',self.root/'reports')
        group=reader.task(doc['id'])['evaluations'][0]
        self.assertEqual(group['dispatch_count'],1)
        request=group['requests'][0]
        self.assertEqual(request['reports'][0]['results'][2]['values']['slope'],2)
        labels=[event['label'] for event in request['history']]
        self.assertIn('开始自动跟进',labels);self.assertIn('自动回收结果',labels)
        self.assertIn('自动分析结果',labels);self.assertIn('自动分析已完成',labels)


if __name__=='__main__':unittest.main()
