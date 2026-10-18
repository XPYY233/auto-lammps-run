"""Explicit unknown-call recovery keeps all historical evidence immutable.

Synthetic PDFs and fake HTTP exercise the original installed native pipeline;
no real provider, customer data, HPC evaluation or scientific result is used.
"""
from copy import deepcopy
from dataclasses import replace
import importlib.util
import json
import unittest
from unittest.mock import patch

from auto_lammps.deepseek import DeepSeekClient, DeepSeekConfig, ModelCalls
from auto_lammps.manifest import canonical, sha256
from auto_lammps.tasks import TaskError
from auto_lammps.workbench_jobs import WorkbenchExtractionJobs
import test_workbench_recovery as fixtures


def native_available():
    try:
        return importlib.util.find_spec('auto_research.evidence.literature_known_length_recovery') is not None
    except ModuleNotFoundError:
        return False


@unittest.skipUnless(native_available(), 'optional native workbench dependency unavailable')
class ExplicitUnknownReissueTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.NativeWorkbenchRecoveryTests(methodName='test_native_single_page_partition_preserves_full_source_and_budget')
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.calls = ModelCalls(self.fixture.root/'explicit-calls.sqlite', DeepSeekConfig('synthetic-model',
            max_output_tokens=65536, max_input_bytes=262144), max_requests=200)
        self.outgoing=[];self.phase='length';self.length_at=3
        self.client=DeepSeekClient(self.calls,transport=self.transport,key_reader=lambda:'synthetic-key')
        self.parent_id,self.unknown_id,self.new_id,self.other_id = (c*32 for c in 'abcd')

    @property
    def bridge(self):return self.fixture.bridge

    def transport(self,body,key,timeout):
        payload=json.loads(body);self.outgoing.append(payload)
        if self.phase=='published':return self.fixture.transport(body,key,timeout)
        if self.phase=='unknown':raise TimeoutError('synthetic unknown provider response')
        length=self.phase=='length' and len(self.outgoing)==self.length_at
        content='{"data":[' if length else json.dumps(dict(data=[],findings=[],pending_tasks=[]))
        return 200,canonical(dict(choices=[dict(finish_reason='length' if length else 'stop',
            message=dict(role='assistant',content=content))],
            usage=dict(prompt_tokens=4,completion_tokens=payload['max_tokens'] if length else 2,
                total_tokens=4+(payload['max_tokens'] if length else 2))))

    def extract(self,request_id,**kwargs):
        return self.bridge.extract(self.fixture.task['id'],request_id,self.fixture.binding,self.client,
            connection_revision=1,credential_generation=1,**kwargs)

    def token(self,request_id):
        with self.fixture.tasks.transaction() as db:
            return self.bridge._continuation(db,request_id)['job_token']

    def native_bytes(self,request_id):
        from auto_research.evidence.literature_task_checkpoint_codec import encode_checkpoint
        return encode_checkpoint(self.bridge.runtime.native_recovery_reader(self.token(request_id))['checkpoint'])

    def fail_unknown(self):
        parent=self.extract(self.parent_id)
        self.assertEqual(parent['state'],'failed',parent)
        self.phase='unknown'
        unknown=self.extract(self.unknown_id,resume_of=self.parent_id)
        self.assertEqual(unknown['state'],'failed_or_unknown',unknown)
        self.assertEqual(len(unknown['model_request_ids']),1)
        self.assertEqual(len(self.outgoing),self.length_at+1)
        self.old_parent=self.native_bytes(self.parent_id);self.old_unknown=self.native_bytes(self.unknown_id)
        self.old_model={r:self.calls.lookup(r) for record in self.bridge.history(self.fixture.task['id'])
            for r in record['model_request_ids']}
        return unknown

    def options(self):
        return self.bridge.unknown_reissue_options(self.fixture.task['id'],self.fixture.binding,self.client,
            connection_revision=1,credential_generation=1)

    def authorize(self,request_id=None,**kwargs):
        return self.bridge.authorize_unknown_reissue(self.fixture.task['id'],request_id or self.new_id,
            self.fixture.binding,self.client,unknown_reissue_of=self.unknown_id,
            supplier_checked=kwargs.get('supplier_checked',True),retain_unknown_charge=kwargs.get('retain_unknown_charge',True),
            connection_revision=kwargs.get('connection_revision',1),credential_generation=1)

    def assert_history_untouched(self):
        self.assertEqual(self.native_bytes(self.parent_id),self.old_parent)
        self.assertEqual(self.native_bytes(self.unknown_id),self.old_unknown)
        self.assertEqual({k:self.calls.lookup(k) for k in self.old_model},self.old_model)

    def test_preview_is_read_only_and_exposes_no_private_tokens(self):
        self.fail_unknown()
        before=list(self.fixture.storage.rglob('*'))
        option=self.options()[0]
        self.assertTrue(option['enabled'],option);self.assertEqual(option['reused_model_calls'],2)
        self.assertEqual(option['inherited_unknown_reserved_tokens'],32000)
        self.assertNotIn('job_token',option)
        self.assertEqual(before,list(self.fixture.storage.rglob('*')))
        self.assertEqual(len(self.outgoing),4);self.assert_history_untouched()

    def publish_reissue(self):
        self.fail_unknown();self.authorize();self.phase='published'
        result=self.extract(self.new_id,unknown_reissue_of=self.unknown_id)
        self.assertIn(result['state'],{'completed','completed_with_limitations'},result)
        self.assertGreater(result['counts']['published_item_count'],0)
        return result

    def test_completed_projection_requires_authenticated_publication_and_preserves_unknown(self):
        self.publish_reissue()
        before=self.bridge.history(self.fixture.task['id'])
        proof=self.bridge.completed_unknown_reissues(self.fixture.task['id'])
        self.assertEqual(len(proof),1,proof)
        self.assertEqual(proof[0]['unknown_request_id'],self.unknown_id)
        self.assertEqual(proof[0]['superseded_by'],self.new_id)
        self.assertTrue(proof[0]['unknown_charge_unsettled'])
        self.assertTrue(proof[0]['repair_visuals_only'])
        self.assertNotIn('job_token',json.dumps(proof))
        self.assertEqual(self.bridge.history(self.fixture.task['id']),before)
        self.assertEqual(before[1]['state'],'failed_or_unknown')
        self.assert_history_untouched()
        self.fixture.restart()
        self.assertEqual(self.bridge.completed_unknown_reissues(self.fixture.task['id']),proof)

    def test_published_reissue_allows_only_separate_visual_repair_not_rescan_or_retry(self):
        self.publish_reissue();calls=len(self.outgoing)
        for kwargs in ({},{'force_rescan':True},{'resume_of':self.unknown_id}):
            with self.subTest(kwargs=kwargs),self.assertRaises(TaskError):self.extract(self.other_id,**kwargs)
        self.assertEqual(len(self.outgoing),calls)
        result=self.extract(self.other_id,repair_visuals=True)
        # This synthetic PDF has no visuals: the native free preflight must
        # fail specifically, after admission, without rescanning numeric P.
        self.assertEqual(result['state'],'failed',result)
        self.assertEqual(result['error_code'],'literature_visual_unavailable')
        self.assertIn('未创建模型调用',result['error'])
        self.assertEqual(len(self.outgoing),calls)
        self.assert_history_untouched()

    def test_queue_and_dispatch_allow_proven_visual_repair_but_new_unknown_still_blocks(self):
        self.publish_reissue();calls=len(self.outgoing)
        context=lambda _:dict(enabled=True,binding=self.fixture.binding,client=self.client,
            connection_revision=1,credential_generation=1)
        jobs=WorkbenchExtractionJobs(self.bridge,context,max_workers=1);self.addCleanup(jobs.close)
        self.assertEqual(jobs.completed_unknown_reissues(self.fixture.task['id']),
            self.bridge.completed_unknown_reissues(self.fixture.task['id']))
        with patch.object(jobs.pool,'submit'):
            for kwargs in ({},{'force_rescan':True}):
                with self.assertRaises(TaskError):jobs.enqueue(self.fixture.task['id'],self.other_id,
                    source_sha256=self.fixture.source_sha,**kwargs)
            queued=jobs.enqueue(self.fixture.task['id'],self.other_id,
                source_sha256=self.fixture.source_sha,repair_visuals=True)
        self.assertEqual(queued['state'],'queued')
        jobs.run(self.fixture.task['id'],self.other_id)
        self.assertEqual(jobs.get(self.fixture.task['id'],self.other_id)['receipt']['error_code'],
            'literature_visual_unavailable')
        # A distinct unresolved request for this PDF must block even after
        # the explicitly authorized old unknown has a published continuation.
        unresolved='e'*32
        intent=self.bridge.extraction_identity(self.fixture.task['id'],unresolved,self.fixture.binding,
            self.client,repair_visuals=True,connection_revision=1,credential_generation=1)
        with self.fixture.tasks.transaction() as db:
            db.execute('INSERT INTO workbench_extraction_intents VALUES (?,?,?,?,?)',
                (unresolved,self.fixture.task['id'],self.fixture.paper['id'],
                    sha256(canonical(intent)),canonical(intent).decode()))
        with self.assertRaises(TaskError):jobs.enqueue(self.fixture.task['id'],'f'*32,
            source_sha256=self.fixture.source_sha,repair_visuals=True)
        with self.assertRaises(TaskError):self.extract('f'*32,repair_visuals=True)
        self.assertEqual(len(self.outgoing),calls)

    def test_changed_native_or_source_or_model_cannot_use_published_unknown_exception(self):
        self.publish_reissue();calls=len(self.outgoing)
        runtime=self.bridge.runtime
        for kwargs in ({'native_recovery_reader':lambda _:None},
                {'native_source_reader':lambda _:'f'*64},
                {'data_items':lambda *args:[]},
                {'native_capability_reader':lambda:{**runtime.native_capability_reader(),
                    'modules':{'changed_module':'f'*64}}}):
            with self.subTest(kwargs=tuple(kwargs)),patch.object(self.bridge,'runtime',replace(runtime,**kwargs)):
                self.assertEqual(self.bridge.completed_unknown_reissues(self.fixture.task['id']),[])
                with self.assertRaises(TaskError):self.extract(self.other_id,repair_visuals=True)
        with self.assertRaises(TaskError):self.bridge.extract(self.fixture.task['id'],self.other_id,
            self.fixture.binding,self.client,repair_visuals=True,connection_revision=2,credential_generation=1)
        self.assertEqual(len(self.outgoing),calls)
        self.assert_history_untouched()

    def test_operator_old_capability_allowlist_reads_only_completed_authenticated_publication(self):
        self.publish_reissue();calls=len(self.outgoing)
        runtime=self.bridge.runtime
        old_cap=sha256(canonical(runtime.native_capability_reader()))
        changed=lambda:{**runtime.native_capability_reader(),'modules':{'changed_module':'f'*64}}
        approved=replace(runtime,native_capability_reader=changed,
            completed_native_capability_allowlist=(old_cap,))
        with patch.object(self.bridge,'runtime',approved):
            self.assertEqual(len(self.bridge.completed_unknown_reissues(self.fixture.task['id'])),1)
            result=self.extract(self.other_id,repair_visuals=True)
            self.assertEqual(result['error_code'],'literature_visual_unavailable')
        child_token=self.token(self.new_id)
        for bad in ('completion','summary','pdf'):
            def reader(token):
                native=runtime.native_recovery_reader(token)
                if token!=child_token:return native
                if bad=='completion':
                    native={**native,'checkpoint':replace(native['checkpoint'],state='paused')}
                elif bad=='summary':
                    native={**native,'execution':replace(native['execution'],
                        completion_result={**native['execution'].completion_result,'published_item_count':999})}
                else:
                    native={**native,'job':replace(native['job'],snapshot={**native['job'].snapshot,'pdf_sha256':'f'*64})}
                return native
            with self.subTest(bad=bad),patch.object(self.bridge,'runtime',
                    replace(approved,native_recovery_reader=reader)):
                self.assertEqual(self.bridge.completed_unknown_reissues(self.fixture.task['id']),[])
        self.assertEqual(len(self.outgoing),calls);self.assert_history_untouched()

    def test_completed_capability_allowlist_never_authorizes_unknown_reissue(self):
        self.fail_unknown();runtime=self.bridge.runtime
        old_cap=sha256(canonical(runtime.native_capability_reader()))
        changed=lambda:{**runtime.native_capability_reader(),'modules':{'changed_module':'f'*64}}
        with patch.object(self.bridge,'runtime',replace(runtime,native_capability_reader=changed,
                completed_native_capability_allowlist=(old_cap,))):
            self.assertEqual(self.bridge.completed_unknown_reissues(self.fixture.task['id']),[])
            with self.assertRaises(TaskError):self.authorize()
        self.assertEqual(len(self.outgoing),4);self.assert_history_untouched()

    def test_unpublished_completed_child_never_supersedes_unknown(self):
        self.fail_unknown();self.authorize();self.phase='success'
        self.extract(self.new_id,unknown_reissue_of=self.unknown_id)
        self.assertEqual(self.bridge.completed_unknown_reissues(self.fixture.task['id']),[])
        with self.assertRaises(TaskError):self.extract(self.other_id,repair_visuals=True)

    def test_unbound_or_incomplete_consent_never_supersedes_unknown(self):
        self.publish_reissue()
        with self.fixture.tasks.transaction() as db:
            claim=self.bridge._unknown_reissue_claim(db,self.unknown_id)
        for changes in ({'supplier_checked':False},{'retain_unknown_charge':False},
                {'authorization_kind':'automatic_retry'},{'request_id':'e'*32},
                {'task_id':'f'*32},{'source_sha256':'f'*64}):
            with self.subTest(changes=changes),patch.object(self.bridge,'_unknown_reissue_claim',
                    return_value={**claim,**changes}):
                self.assertEqual(self.bridge.completed_unknown_reissues(self.fixture.task['id']),[])
                with self.assertRaises(TaskError):self.extract(self.other_id,repair_visuals=True)
        second=self.fixture.tasks.create('Same PDF separate task','No physics','reproduction')
        paper=self.fixture.papers.get(self.fixture.paper['id'])
        self.fixture.papers.link_task(paper['id'],paper['revision'],second['id'])
        self.assertEqual(self.bridge.completed_unknown_reissues(second['id']),[])
        with self.assertRaises(TaskError):self.bridge.extract(second['id'],self.other_id,self.fixture.binding,
            self.client,repair_visuals=True,connection_revision=1,credential_generation=1)

    def test_explicit_consent_resumes_original_native_pipeline_without_rescan(self):
        self.fail_unknown();self.authorize();self.phase='success'
        result=self.extract(self.new_id,unknown_reissue_of=self.unknown_id)
        self.assertEqual(result['state'],'no_evidence_published',result)
        self.assertEqual(result['reused_model_calls'],2)
        self.assertEqual(result['inherited_unknown_model_calls'],1)
        self.assertEqual(result['inherited_unknown_reserved_tokens'],32000)
        self.assertEqual(result['inherited_failed_reserved_tokens'],16000)
        self.assertTrue(result['retain_unknown_charge']);self.assertFalse(result['automatic_retry'])
        self.assertEqual(self.outgoing[4]['max_tokens'],32000)
        self.assertEqual(self.outgoing[3]['messages'][1],self.outgoing[4]['messages'][1])
        self.assertTrue(all(p['max_tokens']<=16000 for p in self.outgoing[5:]))
        count=len(self.outgoing)
        self.assertEqual(self.extract(self.new_id,unknown_reissue_of=self.unknown_id),result)
        self.assertEqual(len(self.outgoing),count)
        with self.assertRaises(TaskError):self.authorize(self.other_id)
        self.assert_history_untouched()
        self.fixture.restart()
        native=self.bridge.runtime.native_recovery_reader(self.token(self.new_id))
        self.assertEqual(native['checkpoint'].state,'completed')
        self.assertEqual(native['checkpoint'].continuation_lineage.parent_task_id,
            self.bridge.runtime.native_recovery_reader(self.token(self.parent_id))['checkpoint'].manifest.task_id)

    def test_explicit_new_authorization_after_expiry_keeps_original_timestamps(self):
        import time
        self.fail_unknown()
        native=self.bridge.runtime.native_recovery_reader(self.token(self.unknown_id))
        future=native['checkpoint'].manifest.expires_at+10
        self.phase='success'
        with patch('time.time',return_value=future):
            self.assertTrue(self.options()[0]['enabled'])
            self.authorize()
            result=self.extract(self.new_id,unknown_reissue_of=self.unknown_id)
        self.assertEqual(result['state'],'no_evidence_published',result)
        child=self.bridge.runtime.native_recovery_reader(self.token(self.new_id))
        self.assertEqual(child['checkpoint'].manifest.issued_at,int(future))
        self.assertGreater(child['checkpoint'].manifest.expires_at,future)
        self.assert_history_untouched()

    def test_coverage_gap_reuses_prior_stage_and_current_stage_prefix(self):
        self.length_at=15
        self.fail_unknown()
        native=self.bridge.runtime.native_recovery_reader(self.token(self.unknown_id))
        self.assertEqual(native['checkpoint'].stage,'coverage_gap')
        self.assertGreater(native['execution'].receipt_offset,0)
        self.assertEqual(self.options()[0]['reused_model_calls'],14)
        self.authorize();self.phase='success'
        result=self.extract(self.new_id,unknown_reissue_of=self.unknown_id)
        self.assertEqual(result['state'],'no_evidence_published',result)
        self.assertEqual(result['reused_model_calls'],14)
        self.assertEqual(result['inherited_failed_reserved_tokens'],12000)
        self.assertEqual(self.outgoing[16]['max_tokens'],32000)
        self.assertEqual(self.outgoing[15]['messages'][1],self.outgoing[16]['messages'][1])
        self.assert_history_untouched()

    def test_long_same_request_renews_live_owner_lease_without_replayed_calls(self):
        import time
        self.fail_unknown();self.authorize();self.phase='success'
        clock=[time.time()]
        transport=self.client.transport
        def slow_transport(body,key,timeout):
            result=transport(body,key,timeout)
            clock[0]+=610  # A full run exceeds the original single 1800s lease.
            return result
        self.client.transport=slow_transport
        with patch('time.time',side_effect=lambda:clock[0]):
            result=self.extract(self.new_id,unknown_reissue_of=self.unknown_id)
        self.assertEqual(result['state'],'no_evidence_published',result)
        self.assertEqual(len(result['model_request_ids']),len(self.outgoing)-4)
        self.assertGreater(clock[0]-time.time(),1800)
        self.assert_history_untouched()

    def test_expired_lease_is_never_reacquired_or_replayed_automatically(self):
        import time
        self.fail_unknown();self.authorize();self.phase='success'
        clock=[time.time()]
        transport=self.client.transport
        def expired_transport(body,key,timeout):
            result=transport(body,key,timeout)
            clock[0]+=1900
            return result
        self.client.transport=expired_transport
        with patch('time.time',side_effect=lambda:clock[0]):
            result=self.extract(self.new_id,unknown_reissue_of=self.unknown_id)
        self.assertEqual(result['state'],'failed',result)
        self.assertEqual(result['error_code'],'literature_checkpoint_lease_lost',result)
        self.assertEqual(len(self.outgoing),5)
        child=self.bridge.runtime.native_recovery_reader(self.token(self.new_id))
        self.assertEqual(child['checkpoint'].receipts[-1].state,'in_flight')
        self.assertEqual(len(result['model_request_ids']),1)
        self.assert_history_untouched()

    def test_unknown_cannot_be_reissued_without_explicit_both_confirmations(self):
        self.fail_unknown()
        for field in ('supplier_checked','retain_unknown_charge'):
            for value in (False,None,1,'true'):
                with self.assertRaises(TaskError):self.authorize(**{field:value})
        with self.assertRaises(TaskError):self.extract(self.new_id,unknown_reissue_of=self.unknown_id)
        self.assertEqual(len(self.outgoing),4);self.assert_history_untouched()

    def test_changed_source_connection_prefix_and_unknown_record_are_denied(self):
        unknown=self.fail_unknown()
        with self.assertRaises(TaskError):self.authorize(connection_revision=2)
        reader=self.bridge.runtime.native_recovery_reader
        def changed(token):
            data=reader(token)
            if token==self.token(self.unknown_id):
                data={**data,'execution':replace(data['execution'],completed_results=({'changed':True},))}
            return data
        self.fixture.bridge.runtime=replace(self.bridge.runtime,native_recovery_reader=changed)
        self.assertFalse(self.options()[0]['enabled'])
        self.fixture.bridge.runtime=replace(self.bridge.runtime,native_recovery_reader=reader)
        lookup=self.calls.lookup
        def wrong_receipt(identifier):
            row=lookup(identifier)
            if identifier==unknown['model_request_ids'][0]:
                row=deepcopy(row);row['receipt']['state']='completed'
            return row
        with patch.object(self.calls,'lookup',wrong_receipt):
            with self.assertRaises(TaskError):self.authorize()
        self.fixture.source.write_bytes(self.fixture.source.read_bytes()+b'changed')
        self.assertFalse(self.options()[0]['enabled']);self.assertEqual(len(self.outgoing),4)

    def test_second_unknown_stops_without_third_reissue(self):
        self.fail_unknown();self.authorize()
        result=self.extract(self.new_id,unknown_reissue_of=self.unknown_id)
        self.assertEqual(result['state'],'failed_or_unknown',result)
        self.assertEqual(len(self.outgoing),5)
        self.assertFalse(any(option['enabled'] for option in self.options()))
        with self.assertRaises(TaskError):self.authorize(self.other_id)
        self.assertEqual(len(self.outgoing),5);self.assert_history_untouched()

    def test_job_queue_claims_once_and_is_idempotent(self):
        self.fail_unknown()
        def context(identifier):
            return dict(enabled=True,binding=self.fixture.binding,client=self.client,
                connection_revision=1,credential_generation=1)
        jobs=WorkbenchExtractionJobs(self.bridge,context,max_workers=1);self.addCleanup(jobs.close)
        with patch.object(jobs.pool,'submit') as submit:
            first=jobs.enqueue_unknown_reissue(self.fixture.task['id'],self.new_id,
                source_sha256=self.fixture.source_sha,unknown_reissue_of=self.unknown_id,
                supplier_checked=True,retain_unknown_charge=True)
            again=jobs.enqueue_unknown_reissue(self.fixture.task['id'],self.new_id,
                source_sha256=self.fixture.source_sha,unknown_reissue_of=self.unknown_id,
                supplier_checked=True,retain_unknown_charge=True)
            self.assertEqual(first,again);self.assertEqual(submit.call_count,1)
            with self.assertRaises(TaskError):jobs.enqueue_unknown_reissue(self.fixture.task['id'],self.other_id,
                source_sha256=self.fixture.source_sha,unknown_reissue_of=self.unknown_id,
                supplier_checked=True,retain_unknown_charge=True)
        self.phase='success';jobs.run(self.fixture.task['id'],self.new_id)
        final=jobs.get(self.fixture.task['id'],self.new_id)
        self.assertEqual(final['state'],'no_evidence_published',final)
        self.assert_history_untouched()
