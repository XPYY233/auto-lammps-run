"""Synthetic published P only; real Adapter and fake HTTP, no research calls."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from auto_lammps.deepseek import DeepSeekClient, DeepSeekConfig, ModelCalls, request_body
from auto_lammps.manifest import canonical, sha256
from auto_lammps.papers import PaperStore
from auto_lammps.reference_targets import model_ledger_identity, reference_target_input
from auto_lammps.scientific_adapters import validate_prepared_messages
from auto_lammps.tasks import TaskError, TaskStore
from auto_lammps.workbench_target_inventory import (STAGE, WorkbenchTargetInventory,
    inventory_revision_feedback, target_inventory_context, validate_inventory_output,
    workbench_inventory_input)


class WorkbenchTargetInventoryTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name).resolve()
        self.tasks=TaskStore(self.root/'tasks.sqlite')
        self.task=self.tasks.create('Synthetic native P inventory','B_CONDITION_SECRET','reproduction')
        self.papers=PaperStore(self.tasks)
        paper=self.papers.add('Synthetic published native article','10.1234/native-inventory','Scope','No physics')
        paper=self.papers.select(paper['id'],paper['revision'])
        self.paper=self.papers.link_task(paper['id'],paper['revision'],self.task['id'])
        paper_fields={k:self.paper[k] for k in ('title','doi')}
        self.quote='Synthetic figure and data were generated with LAMMPS.'
        self.inputs=dict(task_id=self.task['id'],paper_id=self.paper['id'],paper=paper_fields,
            role='paper_reference_human_only',
            sources=[dict(id='paper-page-1',origin='paper',locator='Original synthetic PDF page 1',
                text=self.quote+' Synthetic experiment is independent. A body-only finding lacks a figure.')],
            items=[dict(entity_uid='item:data',value_text='SYNTHETIC_P_VALUE',meaning='Synthetic data',unit='eV',
                context_explanation='Retained native projection',source_page=1,source_locator='body',
                source_excerpt=self.quote,quality_gate_status='published'),
                dict(entity_uid='item:finding',value_text='',meaning='Synthetic finding',unit='',
                    context_explanation='Body-only finding',source_page=1,source_locator='body',
                    source_excerpt='A body-only finding lacks a figure.',quality_gate_status='published')],
            visuals=[dict(entity_uid='visual:1',asset_type='figure',label='Fig. 1',display_name='Synthetic figure',
                caption=self.quote,page_start=1,page_end=1,image_sha256='a'*64,quality_gate_status='published')],
            publication=dict(state='published',request_id='b'*32,receipt_sha256='c'*64,artifacts_sha256='d'*64),
            source_binding=dict(pdf_sha256='e'*64,source_receipt_sha256='c'*64))
        self.sync_artifacts()
        citation=[dict(source_id='paper-page-1',quote=self.quote)]
        target=dict(id='fig1',label='Synthetic Fig. 1',parent='Fig. 1',locator='Original synthetic PDF page 1',
            kind='figure',condition_group='synthetic-condition',conditions='Synthetic retained conditions',
            resources='Author resources not inspected by this stage',outputs='Synthetic data',
            sampling='Sampling not stated',analysis='Method awaits dependency assessment',criterion='',
            availability='unresolved',limitations='Fixture only; no scientific validation')
        self.value=dict(inventory=dict(version=1,paper=deepcopy(paper_fields),source_sha256='e'*64,
            coverage_note='All native records mapped; body-only finding explicitly unresolved.',targets=[target]),
            target_evidence=[dict(target_id='fig1',priority=1,citations=deepcopy(citation))],
            coverage=dict(items=[dict(entity_uid='item:data',target_ids=['fig1'],disposition='target',
                reason='Synthetic item maps to the same figure.',citations=deepcopy(citation)),
                dict(entity_uid='item:finding',target_ids=[],disposition='unclear',
                    reason='正文结论没有对应原图表，现有目标类型不能准确容纳。',
                    citations=[dict(source_id='paper-page-1',quote='A body-only finding lacks a figure.')])],
                visuals=[dict(entity_uid='visual:1',target_ids=['fig1'],disposition='target',
                    reason='Synthetic visual maps to its original figure.',citations=deepcopy(citation))]),
            limitations=['Synthetic contract test, not a scientific result.'])
        self.calls=ModelCalls(self.root/'models.sqlite',DeepSeekConfig('synthetic-native-model',
            max_output_tokens=16000,max_input_bytes=262144),max_requests=100)
        self.sent=[]
        self.key=Mock(return_value='synthetic-project-key')
        self.client=DeepSeekClient(self.calls,transport=self.transport,key_reader=self.key)
        self.connection=dict(enabled=True,model=self.client.model,connection_revision=1,
            credential_generation=1,model_ledger_sha256=model_ledger_identity(self.client))
        self.provider=Mock(side_effect=lambda _:deepcopy(self.inputs))
        self.connection_provider=Mock(side_effect=lambda _:dict(self.connection))
        self.service=self.make_service()
        self.request='d'*32

    def sync_artifacts(self):
        self.inputs['publication']['artifacts_sha256']=sha256(canonical({k:self.inputs[k] for k in ('items','visuals')}))

    def make_service(self):
        return WorkbenchTargetInventory(self.tasks,self.papers,self.provider,
            connection_provider=self.connection_provider)

    def transport(self,body,key,timeout):
        self.sent.append(json.loads(body))
        return 200,canonical(dict(choices=[dict(finish_reason='stop',message=dict(role='assistant',
            content=json.dumps(self.value)))],usage=dict(prompt_tokens=9,completion_tokens=4,total_tokens=13)))

    def generate(self,request=None,service=None,client=None):
        return (service or self.service).generate(self.task['id'],request or self.request,client or self.client,
            connection_revision=1,credential_generation=1)

    def test_full_p_real_adapter_and_existing_downstream_contract_without_import(self):
        before=self.tasks.history(self.task['id']);inputs=deepcopy(self.inputs)
        result=self.generate()
        self.assertEqual(result['state'],'completed',result)
        self.assertEqual(result['usage']['total_tokens'],13)
        self.assertFalse(result['execution_authorized']);self.assertFalse(result['B_input'])
        self.assertEqual(result['scientific_adapter']['stage'],STAGE)
        payload=json.loads(self.sent[0]['messages'][-1]['content'])
        self.assertEqual(payload['items'],inputs['items']);self.assertEqual(payload['visuals'],inputs['visuals'])
        self.assertEqual(payload['sources'],inputs['sources'])
        menu=payload['workbench_target_inventory_menu']
        self.assertEqual(menu['item_ids'],['item:data','item:finding'])
        self.assertEqual(menu['visual_ids'],['visual:1'])
        validate_prepared_messages(STAGE,self.sent[0]['messages'],inputs)
        self.assertNotIn('B_CONDITION_SECRET',canonical(self.sent).decode())
        downstream=self.service.reference_target_source(self.task['id'])
        self.assertEqual(reference_target_input(downstream),downstream)
        self.assertEqual(downstream['inventory'],self.value['inventory'])
        self.assertEqual(downstream['source_binding']['source_receipt_sha256'],'c'*64)
        self.assertEqual(self.tasks.history(self.task['id']),before)
        self.assertEqual(self.inputs,inputs)

    def test_full_native_mapping_allows_shared_targets_and_explicit_method_gaps(self):
        value=deepcopy(self.value)
        value['coverage']['items'][1]['disposition']='not_lammps'
        value['coverage']['items'][1]['citations']=[dict(source_id='paper-page-1',quote='Synthetic experiment is independent.')]
        value['coverage']['items'][1]['reason']='模型判断为实验数据；代码不替模型分类。'
        report=validate_inventory_output(self.inputs,value)
        self.assertEqual(report['coverage']['items'][1]['disposition'],'not_lammps')
        self.assertEqual(report['coverage']['items'][0]['target_ids'],report['coverage']['visuals'][0]['target_ids'])
        self.assertEqual(report['citation_check'],'exact_paper_source_membership')
        self.inputs['visuals']=[];self.sync_artifacts();value['coverage']['visuals']=[]
        self.assertEqual(validate_inventory_output(self.inputs,value)['coverage']['visuals'],[])
        self.assertEqual(target_inventory_context(self.inputs)['visual_ids'],[])

    def test_contract_rejects_coverage_omissions_duplicates_new_ids_and_unapproved_fields(self):
        changes=[lambda v:v['coverage']['items'].pop(),
            lambda v:v['coverage']['items'].__setitem__(1,deepcopy(v['coverage']['items'][0])),
            lambda v:v['coverage']['visuals'][0].update(entity_uid='visual:unknown'),
            lambda v:v['coverage']['items'][0].update(target_ids=['unknown']),
            lambda v:v['coverage']['items'][0].update(target_ids=['fig1','fig1']),
            lambda v:v['coverage']['items'][0].update(target_ids=[]),
            lambda v:v['target_evidence'].clear(),
            lambda v:v['target_evidence'][0].update(priority=True),
            lambda v:v['inventory'].update(source_sha256='f'*64),
            lambda v:v.update(approval=True),
            lambda v:v['inventory']['targets'][0].update(kind='body')]
        for change in changes:
            with self.subTest(change=change):
                value=deepcopy(self.value);change(value)
                with self.assertRaises(TaskError):validate_inventory_output(self.inputs,value)

    def test_every_mapping_and_target_requires_literal_original_paper_citation(self):
        for path in ('target','item','visual'):
            value=deepcopy(self.value)
            row=(value['target_evidence'][0] if path=='target' else value['coverage']['items' if path=='item' else 'visuals'][0])
            row['citations'][0]['quote']='Paraphrased synthetic paper, not an exact quote.'
            with self.subTest(path=path):
                with self.assertRaises(TaskError):validate_inventory_output(self.inputs,value)
        value=deepcopy(self.value);value['coverage']['items'][1]['citations']=[]
        with self.assertRaises(TaskError):validate_inventory_output(self.inputs,value)

    def test_nonpublication_bad_artifact_hash_and_source_origin_reject_before_key_or_call(self):
        changes=[lambda v:v['publication'].update(state='running'),
            lambda v:v['publication'].update(artifacts_sha256='0'*64),
            lambda v:v['source_binding'].update(source_receipt_sha256='1'*64),
            lambda v:v['sources'][0].update(origin='user'),
            lambda v:v.update(paper_id='a'*32),
            lambda v:v['items'].append(deepcopy(v['items'][0]))]
        original=deepcopy(self.inputs)
        for change in changes:
            self.inputs=deepcopy(original);change(self.inputs)
            with self.subTest(change=change),self.assertRaises(TaskError):self.generate()
        self.assertEqual(self.sent,[]);self.key.assert_not_called()
        self.assertEqual(self.calls.status()['used_requests'],0)
        self.assertEqual(self.service.history(self.task['id']),[])

    def test_full_oversized_input_rejects_without_truncating_or_reading_key(self):
        self.inputs['items'][0]['context_explanation']='X'*300000;self.sync_artifacts()
        before=deepcopy(self.inputs)
        with self.assertRaises(TaskError):self.generate()
        self.assertEqual(self.inputs,before);self.assertEqual(self.sent,[])
        self.key.assert_not_called();self.assertEqual(self.calls.status()['used_requests'],0)

    def test_completed_idempotence_and_readonly_views_never_read_connection_or_credentials(self):
        result=self.generate()
        self.key.side_effect=AssertionError('No key read')
        self.connection_provider.side_effect=AssertionError('No connection read')
        self.assertEqual(self.generate(),result)
        self.assertEqual(self.service.get(self.task['id'],self.request),result)
        self.assertEqual(self.service.history(self.task['id']),[result])
        self.assertTrue(self.service.status(self.task['id'])['available'])
        projected=self.service.source_report(self.task['id'])
        self.assertEqual(projected['state'],'completed')
        self.assertEqual(projected['report']['priorities'],{'fig1':1})
        self.assertEqual(projected['input_sha256'],sha256(canonical(self.inputs)))
        self.assertEqual(self.calls.status()['used_requests'],1)

    def test_complete_same_source_cannot_start_over_using_another_request_id(self):
        result=self.generate()
        self.key.side_effect=AssertionError('Do not read credentials for duplicate science work')
        with self.assertRaises(TaskError):self.generate('e'*32)
        self.assertEqual(self.calls.status()['used_requests'],1)
        self.assertEqual(self.service.history(self.task['id']),[result])

    def test_three_durable_rounds_preserve_full_previous_answer_specific_errors_and_costs(self):
        self.value['coverage']['items'][0]['citations'][0]['quote']='Invalid first quote.'
        self.value['target_evidence'][0]['citations'][0]['quote']='Invalid second quote.'
        first=deepcopy(self.value)
        for request in ('d'*32,'e'*32):
            result=self.generate(request,service=self.make_service())
            self.assertEqual(result['state'],'invalid_output',result)
            self.assertEqual(result['usage']['total_tokens'],13)
        payload=json.loads(self.sent[1]['messages'][-1]['content'])
        revision=payload['workbench_inventory_revision']
        self.assertEqual(revision['complete_previous_answer'],first)
        self.assertEqual(revision['parent_request'],'d'*32)
        self.assertEqual(revision['source_output_sha256'],sha256(canonical(first)))
        paths=[r['path'] for r in revision['feedback']['errors']]
        self.assertIn(['target_evidence',0,'citations',0],paths)
        self.assertIn(['coverage','items',0,'citations',0],paths)
        self.assertEqual(next(r for r in revision['feedback']['errors']
            if r['path']==['target_evidence',0,'citations',0])['original_citation'],
            first['target_evidence'][0]['citations'][0])
        self.value['coverage']['items'][0]['citations'][0]['quote']=self.quote
        self.value['target_evidence'][0]['citations'][0]['quote']=self.quote
        self.assertEqual(self.generate('f'*32,service=self.make_service())['state'],'completed')
        with self.assertRaisesRegex(TaskError,'三轮'):self.generate('a'*32,service=self.make_service())
        self.assertEqual(self.calls.status()['used_requests'],3)
        self.assertEqual(self.calls.lookup('d'*32)['receipt']['structured_output'],first)
        self.assertEqual(self.service.status(self.task['id'])['remaining_rounds'],0)
        self.assertEqual(len(self.sent),3)

    def test_unknown_transport_blocks_same_and_new_ids_without_resending(self):
        self.client.transport=Mock(side_effect=TimeoutError('private upstream content'))
        result=self.generate();self.assertEqual(result['state'],'unknown')
        self.assertNotIn('private upstream content',canonical(result).decode())
        self.assertEqual(self.generate(),result)
        with self.assertRaises(TaskError):self.generate('e'*32)
        self.assertTrue(self.service.status(self.task['id'])['blocked'])
        self.assertEqual(self.client.transport.call_count,1)

    def test_interrupted_reserved_request_with_no_receipt_stays_unknown(self):
        def interrupted(request,messages):
            self.calls.reserve(request,request_body(self.calls.config,messages,model=self.client.model,
                thinking=self.client.thinking))
            raise KeyboardInterrupt('Synthetic interrupted transport boundary')
        with patch.object(self.client,'complete_json',side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):self.generate()
        self.assertIsNone(self.calls.lookup(self.request)['receipt'])
        result=self.generate();self.assertEqual(result['state'],'unknown',result)
        self.key.assert_not_called();self.assertEqual(self.sent,[])
        with self.assertRaises(TaskError):self.generate('e'*32)

    def test_interrupted_unsent_intent_can_continue_exact_same_id(self):
        with patch.object(self.client,'complete_json',side_effect=KeyboardInterrupt('Synthetic unsent exit')):
            with self.assertRaises(KeyboardInterrupt):self.generate()
        self.assertEqual(self.calls.status()['used_requests'],0)
        result=self.generate();self.assertEqual(result['state'],'completed',result)
        self.assertEqual(len(self.service.history(self.task['id'])),1)
        self.assertEqual(len(self.sent),1)

    def test_known_rejection_and_not_sent_are_failed_not_uncertain(self):
        self.client.transport=Mock(return_value=(429,b'{"error":"synthetic limited"}'))
        result=self.generate();self.assertEqual(result['state'],'failed',result)
        self.assertEqual(result['receipt']['model_receipt']['state'],'rejected')
        self.key.return_value=''
        result=self.generate('e'*32);self.assertEqual(result['state'],'failed',result)
        self.assertEqual(result['receipt']['model_receipt']['state'],'not_sent')
        self.assertEqual(self.client.transport.call_count,1)
        self.assertEqual(self.service.status(self.task['id'])['used_rounds'],2)

    def test_source_change_after_response_pauses_without_import_or_second_call(self):
        transport=self.client.transport
        def changed(*args):
            result=transport(*args)
            self.inputs['sources'][0]['text']+=' Source was revised.'
            return result
        self.client.transport=changed
        before=self.tasks.history(self.task['id'])
        result=self.generate();self.assertEqual(result['state'],'paused',result)
        self.assertEqual(result['usage']['total_tokens'],13)
        self.assertEqual(self.tasks.history(self.task['id']),before)
        self.assertEqual(self.calls.lookup(self.request)['receipt']['structured_output'],self.value)
        self.assertEqual(len(self.sent),1)

    def test_completed_source_changes_are_visible_without_model_access(self):
        result=self.generate();self.assertEqual(result['state'],'completed')
        self.inputs['items'][0]['value_text']='changed';self.sync_artifacts()
        self.connection_provider.side_effect=AssertionError('No connection read')
        self.key.side_effect=AssertionError('No key read')
        projected=self.service.get(self.task['id'],self.request)
        self.assertEqual(projected['state'],'source_unavailable')
        self.assertEqual(projected['original_state'],'completed')
        source=self.service.source_report(self.task['id'])
        self.assertEqual(source['state'],'stale');self.assertIsNone(source['report'])
        self.assertEqual(source['input_sha256'],result['report']['input_sha256'])
        self.assertIsNotNone(projected['report'])
        with self.assertRaises(TaskError):self.service.reference_target_source(self.task['id'])

    def test_parent_source_drift_is_not_silent_clean_rewrite(self):
        self.value['coverage']['items'].pop()
        self.assertEqual(self.generate()['state'],'invalid_output')
        self.inputs['sources'][0]['text']+=' Changed original P.'
        with self.assertRaises(TaskError):self.generate('e'*32)
        self.assertEqual(len(self.sent),1)

    def test_stale_latest_completion_does_not_fall_back_to_an_older_inventory(self):
        self.assertEqual(self.generate()['state'],'completed')
        old=deepcopy(self.inputs)
        self.inputs['items'][0]['value_text']='Synthetic revised native item';self.sync_artifacts()
        self.value['target_evidence'][0]['priority']=2
        self.assertEqual(self.generate('e'*32)['state'],'completed')
        self.inputs=old
        self.assertEqual(self.service.get(self.task['id'],self.request)['state'],'completed')
        current=self.service.source_report(self.task['id'])
        self.assertEqual(current['request_id'],'e'*32)
        self.assertEqual(current['state'],'stale');self.assertIsNone(current['report'])
        with self.assertRaises(TaskError):self.service.reference_target_source(self.task['id'])

    def test_dependency_projection_rechecks_source_after_inventory_read(self):
        self.generate()
        original=deepcopy(self.inputs)
        changed=deepcopy(self.inputs);changed['sources'][0]['text']+=' Concurrent source update.'
        self.provider.side_effect=[original,changed]
        with self.assertRaisesRegex(TaskError,'读取期间'):self.service.reference_target_source(self.task['id'])
        self.assertEqual(len(self.sent),1)

    def test_mandatory_adapter_missing_rejects_before_model_call(self):
        from auto_lammps import scientific_adapters
        real=scientific_adapters.stage_context
        def missing(stage,evidence):
            result=real(stage,evidence)
            result['rules']['model_may_disable_checks']=True
            return result
        with patch.object(scientific_adapters,'stage_context',side_effect=missing):
            # Actual prepare and validate reject the altered contract against
            # the independent implementation, before any fake transport.
            with self.assertRaises(TaskError):self.generate()
        self.key.assert_not_called();self.assertEqual(self.sent,[])

    def test_immutable_history_and_cross_task_request_identity(self):
        self.generate()
        with self.tasks.transaction() as db:
            for table in ('workbench_target_scopes','workbench_target_intents','workbench_target_events','workbench_target_receipts'):
                with self.subTest(table=table),self.assertRaises(sqlite3.IntegrityError):db.execute('DELETE FROM '+table)
        other=self.tasks.create('Other reproduction','Synthetic body','reproduction')
        with self.assertRaises(TaskError):self.service.get(other['id'],self.request)

    def test_feedback_reports_all_native_identity_gaps_without_changing_answer(self):
        value=deepcopy(self.value);value['coverage']['items'].pop()
        before=deepcopy(value)
        feedback=inventory_revision_feedback(self.inputs,value)
        self.assertEqual(value,before)
        gap=next(r for r in feedback['errors'] if r['code']=='native_coverage_mismatch')
        self.assertEqual(gap['expected_entity_uids'],['item:data','item:finding'])
        self.assertFalse(feedback['scope_reduction_allowed'])
        self.assertFalse(feedback['automatic_scientific_repair'])


if __name__=='__main__':unittest.main()
