"""Durable dispatch tests with synthetic native ports; no paid requests or HPC."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
import importlib.util
import multiprocessing
import os
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from auto_lammps.deepseek import DeepSeekClient, ModelCalls
from auto_lammps.manifest import canonical, sha256
from auto_lammps.papers import PaperStore
from auto_lammps.tasks import TaskError, TaskStore
from auto_lammps.workbench_bridge import WorkbenchExtractionBridge, WorkbenchPaperBinding, WorkbenchRuntime
from auto_lammps.workbench_jobs import WorkbenchExtractionJobs
import test_workbench_bridge as bridge_fixture
import test_workbench_recovery as recovery_fixture


def crash_dispatcher(root,identifier,binding_fields,source,session):
    tasks=TaskStore(Path(root)/'tasks.sqlite')
    runtime=WorkbenchRuntime(SimpleNamespace(get_paper=lambda _:deepcopy(source)),None,None,session,
        None,None,None,None,None,None,native_capability_reader=bridge_fixture.synthetic_capabilities,
        native_source_reader=lambda _:source['pdf_sha256'])
    bridge=WorkbenchExtractionBridge(PaperStore(tasks),runtime)
    binding=WorkbenchPaperBinding(**binding_fields)
    client=DeepSeekClient(ModelCalls.open_existing(Path(root)/'models.sqlite'),key_reader=lambda:'synthetic-key')
    bridge.extract=lambda *args,**kwargs:os._exit(17)
    jobs=WorkbenchExtractionJobs(bridge,lambda _:dict(enabled=True,binding=binding,client=client,
        connection_revision=1,credential_generation=1))
    jobs.start();jobs.close(wait=True)


class WorkbenchJobTests(unittest.TestCase):
    def setUp(self):
        self.f=bridge_fixture.WorkbenchBridgeTests()
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.runtime=replace(self.f.runtime,native_source_reader=lambda _:self.f.source['pdf_sha256'])
        self.bridge=WorkbenchExtractionBridge(self.f.papers,self.f.runtime)
        self.identifier=self.f.task['id'];self.request_id=self.f.identifier
        self.context=dict(enabled=True,binding=self.f.binding,client=self.f.client,
                          connection_revision=1,credential_generation=1)
        self.jobs=self.make_jobs()

    def make_jobs(self,provider=None):
        jobs=WorkbenchExtractionJobs(self.bridge,provider or (lambda _:dict(self.context)))
        self.addCleanup(lambda:jobs.close(wait=True))
        return jobs

    def enqueue(self,jobs=None,request_id=None,**kwargs):
        return (jobs or self.jobs).enqueue(self.identifier,request_id or self.request_id,
            source_sha256=self.f.binding.pdf_sha256,**kwargs)

    def pending(self,jobs=None,request_id=None,**kwargs):
        jobs=jobs or self.jobs
        with patch.object(jobs.pool,'submit'):return self.enqueue(jobs,request_id,**kwargs)

    def finished(self,jobs=None,identifier=None,request_id=None):
        jobs=jobs or self.jobs;identifier=identifier or self.identifier;request_id=request_id or self.request_id
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            job=jobs.get(identifier,request_id)
            if job and job['state'] not in {'queued','dispatching'}:return job
            time.sleep(.01)
        self.fail('Synthetic background extraction did not stop')

    def seed_native_intent(self,request_id=None):
        request_id=request_id or self.request_id
        intent=self.bridge.extraction_identity(self.identifier,request_id,self.f.binding,self.f.client,
            connection_revision=1,credential_generation=1)
        with self.f.tasks.transaction() as db:
            db.execute('INSERT INTO workbench_extraction_intents VALUES (?,?,?,?,?)',
                (request_id,self.identifier,self.f.paper['id'],sha256(canonical(intent)),canonical(intent).decode()))
        return intent

    def test_enqueue_returns_while_native_chain_runs_and_closing_caller_does_not_cancel_it(self):
        entered,release=threading.Event(),threading.Event()
        self.addCleanup(release.set)
        transport=self.f.client.transport
        def slow(*args):
            entered.set()
            if not release.wait(5):raise RuntimeError('Synthetic timeout')
            return transport(*args)
        self.f.client.transport=slow
        first=self.enqueue()
        self.assertIn(first['state'],{'queued','dispatching'})
        self.assertFalse(release.is_set())
        self.assertTrue(entered.wait(3))
        provider=self.jobs.context_provider
        self.jobs.context_provider=Mock(side_effect=AssertionError('Status must not read credentials'))
        self.assertEqual(self.jobs.get(self.identifier,self.request_id)['state'],'dispatching')
        self.assertEqual(self.jobs.history(self.identifier)[0]['request_id'],self.request_id)
        self.jobs.context_provider=provider
        # No browser lifetime or HTTP request object is retained by the worker.
        self.jobs.close(wait=False)
        release.set()
        final=self.finished()
        self.assertEqual(final['state'],'completed',final)
        self.assertEqual(final['receipt']['state'],'completed')
        self.assertEqual(len(self.f.outgoing),1)
        self.assertFalse(final['execution_authorized'])

    def test_concurrent_duplicate_requests_and_restarted_service_never_repeat_dispatch(self):
        with patch.object(self.jobs.pool,'submit'):
            with ThreadPoolExecutor(max_workers=4) as pool:
                items=list(pool.map(lambda _:self.enqueue(),range(8)))
        self.assertEqual({item['request_id'] for item in items},{self.request_id})
        self.jobs.run(self.identifier,self.request_id)
        first=self.finished()
        restarted=self.make_jobs();restarted.start()
        self.assertEqual(self.enqueue(restarted),first)
        self.assertEqual(len(self.f.outgoing),1)
        with self.f.tasks.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM workbench_jobs').fetchone()[0],1)
            for table in ('workbench_jobs','workbench_job_events'):
                with self.assertRaises(sqlite3.IntegrityError):db.execute('DELETE FROM '+table)

    def test_same_pdf_different_request_is_blocked_before_second_queue_is_saved(self):
        self.pending()
        with self.assertRaisesRegex(TaskError,'同一 PDF'):
            self.pending(request_id='d'*32,force_rescan=True)
        with self.f.tasks.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM workbench_jobs').fetchone()[0],1)
        self.assertEqual(self.f.calls.status()['used_requests'],0)

    def test_unresolved_legacy_native_request_blocks_new_queue_even_with_force_rescan(self):
        self.seed_native_intent()
        with self.assertRaisesRegex(TaskError,'原提取状态未知'):
            self.pending(request_id='d'*32,force_rescan=True)
        with self.f.tasks.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM workbench_jobs').fetchone()[0],0)
        self.assertEqual(self.f.outgoing,[])
        legacy=self.jobs.get(self.identifier,self.request_id)
        self.assertTrue(legacy['legacy']);self.assertEqual(legacy['state'],'unknown')

    def test_completed_legacy_history_remains_readable_without_credentials_or_new_queue(self):
        result=self.bridge.extract(self.identifier,self.request_id,self.f.binding,self.f.client,
            connection_revision=1,credential_generation=1)
        self.jobs.context_provider=Mock(side_effect=AssertionError('No credential read'))
        legacy=self.enqueue()
        self.assertTrue(legacy['legacy']);self.assertEqual(legacy['receipt'],result)
        self.assertEqual(self.jobs.history(self.identifier),[legacy])
        self.assertEqual(len(self.f.outgoing),1)
        with self.f.tasks.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM workbench_jobs').fetchone()[0],0)

    def test_restart_only_executes_queued_request_with_no_native_registration(self):
        pending=self.pending();self.jobs.close(wait=True)
        restarted=self.make_jobs();restarted.start()
        final=self.finished(restarted)
        self.assertEqual(final['state'],'completed')
        self.assertEqual(final['events'][0],pending['events'][0])
        self.assertEqual(len(self.f.outgoing),1)

    def test_queued_request_registered_by_native_bridge_is_never_replayed_after_restart(self):
        self.pending();self.seed_native_intent()
        restarted=self.make_jobs();restarted.start()
        final=self.finished(restarted)
        self.assertEqual(final['state'],'interrupted')
        self.assertEqual(final['receipt']['state'],'unknown')
        self.assertEqual(self.f.outgoing,[])
        restarted.start();self.assertEqual(self.f.outgoing,[])

    def test_unknown_other_native_request_registered_after_queue_pauses_before_dispatch(self):
        self.pending();self.seed_native_intent(request_id='d'*32)
        self.jobs.run(self.identifier,self.request_id)
        self.assertEqual(self.finished()['state'],'paused')
        self.assertEqual(self.bridge.history(self.identifier)[0]['state'],'unknown')
        self.assertEqual(self.f.calls.status()['used_requests'],0)
        self.assertEqual(self.f.outgoing,[])

    def test_real_worker_exit_after_dispatch_marker_never_replays_or_relabels_from_lock_file(self):
        self.pending()
        process=multiprocessing.get_context('spawn').Process(target=crash_dispatcher,
            args=(str(self.f.root),self.identifier,vars(self.f.binding),self.f.source,self.f.runtime.session_id))
        process.start();process.join(5)
        if process.is_alive():
            process.kill();process.join();self.fail('Synthetic worker did not exit')
        self.assertEqual(process.exitcode,17)
        self.assertEqual(self.jobs.get(self.identifier,self.request_id)['state'],'dispatching')
        restarted=self.make_jobs();restarted.start()
        self.assertEqual(restarted.get(self.identifier,self.request_id)['state'],'interrupted')
        self.assertEqual(self.f.calls.status()['used_requests'],0)
        self.assertEqual(self.f.outgoing,[])

    def test_live_os_lease_prevents_false_interruption_by_other_service(self):
        entered,release=threading.Event(),threading.Event();self.addCleanup(release.set)
        transport=self.f.client.transport
        def slow(*args):
            entered.set();release.wait(5);return transport(*args)
        self.f.client.transport=slow;self.enqueue()
        self.assertTrue(entered.wait(3))
        other=self.make_jobs();other.start()
        self.assertEqual(other.reconcile(self.identifier,self.request_id)['state'],'dispatching')
        release.set();self.assertEqual(self.finished()['state'],'completed')
        self.assertEqual(len(self.f.outgoing),1)

    def test_known_receipt_written_before_worker_loss_is_adopted_without_model_call(self):
        self.pending()
        extract=self.bridge.extract
        def saved_then_exit(*args,**kwargs):
            extract(*args,**kwargs);raise KeyboardInterrupt('Synthetic exit after commit')
        with patch.object(self.bridge,'extract',side_effect=saved_then_exit),self.assertRaises(KeyboardInterrupt):
            self.jobs.run(self.identifier,self.request_id)
        restarted=self.make_jobs();restarted.start()
        final=restarted.get(self.identifier,self.request_id)
        self.assertEqual(final['state'],'completed')
        self.assertEqual(len(self.f.outgoing),1)

    def test_changed_model_connection_or_credential_generation_pauses_before_dispatch(self):
        self.pending();self.context['credential_generation']=2
        self.jobs.run(self.identifier,self.request_id)
        self.assertEqual(self.finished()['state'],'paused')
        self.assertEqual(self.f.calls.status()['used_requests'],0)
        self.assertEqual(self.f.outgoing,[])

    def test_missing_explicit_authorization_cannot_queue_or_send(self):
        del self.context['enabled']
        with self.assertRaisesRegex(TaskError,'未排队或收费'):self.pending()
        self.assertEqual(self.jobs.history(self.identifier),[])
        self.assertEqual(self.f.outgoing,[])

    def test_external_model_policy_amendment_pauses_queued_work_without_erasing_old_policy(self):
        self.pending()
        ModelCalls.open_existing(self.f.calls.path).amend_allowance(101,reason_sha256='e'*64)
        self.jobs.run(self.identifier,self.request_id)
        self.assertEqual(self.finished()['state'],'paused')
        self.assertEqual(self.f.calls.status()['used_requests'],0)

    def test_native_module_identity_change_pauses_before_any_model_request(self):
        self.pending()
        changed=bridge_fixture.synthetic_capabilities();changed['modules']['synthetic_module']='f'*64
        self.bridge.runtime=replace(self.bridge.runtime,native_capability_reader=lambda:changed)
        self.jobs.run(self.identifier,self.request_id)
        self.assertEqual(self.finished()['state'],'paused')
        self.assertEqual(self.f.outgoing,[])

    def test_source_changes_stop_queued_work(self):
        self.pending();self.f.source['pdf_sha256']='b'*64
        self.jobs.run(self.identifier,self.request_id)
        self.assertEqual(self.finished()['state'],'paused')
        self.assertEqual(self.f.outgoing,[])

    def test_user_pause_stops_queued_work_before_model_reservation(self):
        self.pending()
        self.f.tasks.set_paused(self.identifier,self.f.task['revision'],True)
        self.jobs.run(self.identifier,self.request_id)
        self.assertEqual(self.finished()['state'],'paused')
        self.assertEqual(self.f.calls.status()['used_requests'],0)
        self.assertEqual(self.f.outgoing,[])

    def test_runtime_authorization_change_stops_next_call_before_model_reservation(self):
        self.pending()
        execute=self.f.runtime.ports.executor.execute
        def revoke(*,action,ai_client):
            self.context['enabled']=False
            return execute(action=action,ai_client=ai_client)
        self.f.runtime.ports.executor.execute=revoke
        self.jobs.run(self.identifier,self.request_id)
        final=self.finished()
        self.assertEqual(final['state'],'paused')
        self.assertEqual(final['receipt']['error_code'],'workbench_context_changed')
        self.assertEqual(self.f.calls.status()['used_requests'],0)
        self.assertEqual(self.f.outgoing,[])

    def test_unknown_transport_keeps_original_native_failure_and_never_requeues(self):
        self.f.client.transport=Mock(side_effect=TimeoutError('synthetic unknown'))
        self.enqueue();final=self.finished()
        self.assertEqual(final['state'],'failed_or_unknown')
        self.assertEqual(final['receipt']['state'],'failed_or_unknown')
        self.assertEqual(self.f.calls.status()['used_requests'],1)
        self.assertEqual(self.enqueue(),final)
        with self.assertRaisesRegex(TaskError,'原提取状态未知'):
            self.pending(request_id='d'*32,force_rescan=True)
        self.make_jobs().start();self.f.client.transport.assert_called_once()

    def test_recovery_admission_uses_existing_native_rule_without_creating_replacement_pipeline(self):
        with patch.object(self.bridge,'_recovery_context',side_effect=TaskError('Original recovery rule')) as recover:
            with self.assertRaisesRegex(TaskError,'Original recovery rule'):
                self.pending(resume_of='a'*32)
        recover.assert_called_once_with(self.identifier,'a'*32,self.f.binding,self.f.client,
            connection_revision=1,credential_generation=1)
        self.assertEqual(self.jobs.history(self.identifier),[])
        self.assertEqual(self.f.outgoing,[])

    def test_queue_metadata_contains_neither_credentials_paths_nor_scientific_outputs(self):
        self.pending()
        with self.f.tasks.transaction() as db:
            documents=[row[0] for row in db.execute('SELECT document FROM workbench_jobs')]
        serialized=json.dumps(documents)+json.dumps(self.jobs.history(self.identifier))
        for value in ('synthetic-project-key','private-synthetic.pdf',str(self.f.root),'frozen_scientific_conditions'):
            self.assertNotIn(value,serialized)
        self.assertEqual(self.f.tasks.get(self.identifier),self.f.task)

    def test_other_source_can_finish_while_first_worker_is_waiting(self):
        other=self.f.tasks.create('Synthetic second task','No physics','reproduction')
        paper=self.f.papers.add('Second synthetic article','10.1234/second','Scope','Test')
        paper=self.f.papers.select(paper['id'],paper['revision'])
        paper=self.f.papers.link_task(paper['id'],paper['revision'],other['id'])
        binding=WorkbenchPaperBinding(paper['id'],8,'b'*64)
        sources={7:self.f.source,8:dict(title=paper['title'],doi=paper['doi'],pdf_sha256='b'*64,pdf_path='synthetic-two.pdf')}
        self.bridge.runtime=replace(self.bridge.runtime,database=SimpleNamespace(get_paper=lambda n:deepcopy(sources[n])),
            native_source_reader=lambda n:sources[n]['pdf_sha256'])
        def contexts(identifier):
            return {**self.context,'binding':self.f.binding if identifier==self.identifier else binding}
        jobs=self.make_jobs(contexts)
        entered,release=threading.Event(),threading.Event();self.addCleanup(release.set)
        transport=self.f.client.transport
        def slow(*args):
            entered.set();release.wait(5);return transport(*args)
        self.f.client.transport=slow;self.enqueue(jobs)
        self.assertTrue(entered.wait(3))
        jobs.enqueue(other['id'],'d'*32,source_sha256=binding.pdf_sha256)
        # The synthetic second port fails its source check promptly; it is not
        # starved behind the unrelated blocked model call or falsely completed.
        second=self.finished(jobs,other['id'],'d'*32)
        self.assertEqual(second['state'],'failed')
        self.assertEqual(jobs.get(self.identifier,self.request_id)['state'],'dispatching')
        release.set();self.assertEqual(self.finished(jobs)['state'],'completed')


@unittest.skipUnless(importlib.util.find_spec('auto_research') is not None,
    'separately installed literature workbench is optional')
class NativeWorkbenchJobRecoveryTests(unittest.TestCase):
    def test_durable_queue_reuses_original_native_prefix_and_preserves_failure(self):
        f=recovery_fixture.NativeWorkbenchRecoveryTests()
        f.setUp();self.addCleanup(f.doCleanups)
        failure=f.fail_after_coverage()
        original={request_id:f.calls.lookup(request_id) for request_id in failure['model_request_ids']}
        f.restart()
        jobs=WorkbenchExtractionJobs(f.bridge,lambda _:dict(enabled=True,binding=f.binding,
            client=f.client,connection_revision=1,credential_generation=1))
        self.addCleanup(lambda:jobs.close(wait=True))
        with patch.object(jobs.pool,'submit'):
            queued=jobs.enqueue(f.task['id'],f.resume_id,source_sha256=f.source_sha,resume_of=f.original_id)
        self.assertEqual(queued['state'],'queued')
        self.assertEqual(len(f.outgoing),24)
        with patch.object(f.bridge.runtime.ports.assembler._starter,'start',
                side_effect=AssertionError('Recovery must reuse native state')):
            jobs.run(f.task['id'],f.resume_id)
        final=jobs.get(f.task['id'],f.resume_id)
        self.assertIn(final['state'],{'completed','completed_with_limitations'},final)
        self.assertEqual(final['receipt']['reused_model_calls'],24)
        self.assertEqual(len(f.outgoing),24+final['receipt']['new_model_calls'])
        self.assertEqual(original,{request_id:f.calls.lookup(request_id) for request_id in original})
        self.assertEqual(f.bridge.history(f.task['id'])[0],failure)
        self.assertEqual(jobs.enqueue(f.task['id'],f.resume_id,source_sha256=f.source_sha,
            resume_of=f.original_id),final)
        restarted=WorkbenchExtractionJobs(f.bridge,Mock(side_effect=AssertionError('No repeated dispatch')))
        self.addCleanup(lambda:restarted.close(wait=True));restarted.start()
        self.assertEqual(restarted.get(f.task['id'],f.resume_id),final)


if __name__=='__main__':
    unittest.main()
