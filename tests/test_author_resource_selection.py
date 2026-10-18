"""Reference AI file selection, synthetic model receipts and Git metadata only."""
from copy import deepcopy
import json
import sqlite3
import unittest
from unittest.mock import Mock, patch

from auto_lammps.author_resource_inventory_worker import CACHE_KEYS, classify_entries, inventory_summary
from auto_lammps.author_resource_selection import AuthorResourceSelection, STAGE, selection_context, validate_selection
from auto_lammps.deepseek import DeepSeekConfig, DeepSeekClient, ModelCalls
from auto_lammps.manifest import canonical, sha256
from auto_lammps.reference_targets import model_ledger_identity
from auto_lammps.scientific_adapters import validate_prepared_messages
from auto_lammps.tasks import TaskError
from test_deepseek import response
import test_author_reference_plan as fixtures


class AuthorResourceSelectionTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.AuthorReferencePlanTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.task=self.fx.task['id'];self.root=self.fx.fixture.root
        self.fx.value.update(status='needs_clarification',questions=['Synthetic companion source is still needed.'])
        for request,note in [('4'*32,''),('5'*32,'Keep the complete original clarification.')]:
            result=self.fx.service.prepare(self.task,request,self.fx.client,connection_revision=1,
                                          credential_generation=1,revision_note=note)
            self.assertEqual(result['state'],'needs_clarification',result)
        self.baseline=self.fx.calls.status()['used_requests']
        self.old_author=deepcopy(self.fx.service.history(self.task))
        capability=self.fx.evidence['author_source']['capability']
        request=dict(id='a'*32,repository=capability['repository'],commit=capability['commit'],
                     directory='/synthetic-hpc/author-resources',storage_bytes=512*1024**2,bundle_sha256='b'*64)
        receipt=dict(schema_version=1,id=request['id'],repository=request['repository'],commit=request['commit'],
            location='hpc',state='partial',request_sha256=sha256(canonical(request)),bundle_sha256=request['bundle_sha256'],
            remote_directory=request['directory']+'/'+request['id'],files=[],inventory_sha256=sha256(canonical([])),
            source_complete=False,tree_sha1='c'*40,scientific_validation=False,execution_authorized=False,
            author_identity_verified=False,failures=[dict(reason='source_budget_exceeded')])
        tree_request=dict(version=1,acquisition_request=request,receipt_sha256=sha256(canonical(receipt)),tree_sha1='c'*40)
        existing=deepcopy(self.fx.evidence['author_source']['files'])
        entries=[dict(path=row['path'],type='blob',mode='100644',size=row['size'],sha='d'*40) for row in existing]
        entries += [dict(path='scripts',type='tree',mode='040000',sha='e'*40),
            dict(path='scripts/helper.py',type='blob',mode='100644',size=200,sha='f'*40),
            dict(path='inputs/companion.in',type='blob',mode='100755',size=400,sha='1'*40),
            dict(path='large.data',type='blob',mode='100644',size=16*1024**2+1,sha='2'*40),
            dict(path='linked',type='blob',mode='120000',size=20,sha='3'*40)]
        entries=classify_entries(entries)
        tree=dict(version=1,state='inspected',request_sha256=sha256(canonical(tree_request)),
            acquisition_id=request['id'],repository=request['repository'],commit=request['commit'],tree_sha1='c'*40,
            receipt_sha256=tree_request['receipt_sha256'],scope='reference_only',B_input=False,
            execution_authorized=False,physical_evaluation_performed=False,scientific_validation=False,
            entries=entries,summary=inventory_summary(entries,{key:'4'*64 for key in CACHE_KEYS}))
        self.input=dict(task_id=self.task,scope='reference_only',role='author_reference_human_only',B_input=False,
            author_plan_request_id='5'*32,tree_request=tree_request,tree_report=tree,acquisition_receipt=receipt,
            existing_files=existing,binding=dict(transport_identity_sha256='5'*64,license_approval_sha256='6'*64))
        self.provider=Mock(side_effect=lambda _:deepcopy(self.input))
        self.proposal=dict(status='selected',summary='合成原件补充选取建议',selections=[
            dict(path='scripts/helper.py',reason='Inspect the synthetic missing companion.',dependency_ids=['shared'])],
            missing=['The original author environment is still unverified.'],limitations=['Tree paths are metadata only.'])
        self.transport=Mock(side_effect=lambda *args:(200,response(deepcopy(self.proposal))))
        self.fx.client.transport=self.transport
        self.service=self.make()
        self.addCleanup(self.assert_author_history_unchanged)
        guard=patch('auto_lammps.slurm_read.subprocess.Popen',side_effect=AssertionError('No SSH/process belongs here'))
        guard.start();self.addCleanup(guard.stop)

    def make(self,connection=None):
        return AuthorResourceSelection(self.fx.service,self.provider,
            connection_provider=connection or self.fx.connection_provider)

    def prepare(self,request='7'*32,service=None,**kwargs):
        return (service or self.service).prepare(self.task,request,self.fx.client,connection_revision=1,
            credential_generation=1,revision_note=kwargs.get('revision_note',''))

    def assert_author_history_unchanged(self):
        self.assertEqual(self.fx.service.history(self.task),self.old_author)
        self.assertEqual(self.fx.service.status(self.task)['used_rounds'],2)

    def test_model_selects_only_pinned_paths_and_all_metadata_and_costs_are_retained(self):
        before=deepcopy(self.input)
        value=self.prepare()
        self.assertEqual(value['state'],'selected',value)
        report=value['report']
        self.assertEqual(report['paths'],['scripts/helper.py'])
        self.assertEqual(report['selections'][0],dict(path='scripts/helper.py',reason=self.proposal['selections'][0]['reason'],
            dependency_ids=['shared'],blob_sha1='f'*40,size=200,git_mode='100644'))
        self.assertEqual(report['total_bytes'],200)
        self.assertEqual(report['missing'],self.proposal['missing'])
        for key in ('execution_authorized','physical_evaluation_performed','B_input',
                    'source_complete','execution_ready','scientific_applicability_verified'):
            self.assertIs(report[key],False)
        self.assertEqual(report['scope'],'reference_only')
        self.assertEqual(self.input,before)
        self.assertEqual(self.fx.calls.status()['used_requests'],self.baseline+1)
        self.assertEqual(self.transport.call_count,1)
        original=self.service._stored(self.task,'7'*32)[1]
        body=json.loads(self.transport.call_args.args[0])
        validate_prepared_messages(STAGE,body['messages'],original['input'])
        payload=json.loads(body['messages'][-1]['content'])
        self.assertEqual(payload['author_plan']['report'],self.old_author[-1]['report'])
        self.assertEqual(payload['reference_scope']['scope']['selected_ids'],['first','second'])
        self.assertEqual(payload['author_resource_selection'],selection_context(original['input']))
        self.assertEqual(payload['scientific_adapter']['stage'],STAGE)

    def test_read_only_history_and_duplicate_same_ID_do_not_call_provider_or_model(self):
        self.assertEqual(self.service.status(self.task)['used_rounds'],0)
        self.provider.assert_not_called();self.transport.assert_not_called()
        value=self.prepare()
        self.provider.reset_mock();self.transport.reset_mock()
        self.assertEqual(self.prepare(service=self.make()),value)
        self.assertEqual(self.service.get(self.task,'7'*32),value)
        self.assertEqual(self.service.status(self.task)['used_rounds'],1)
        self.provider.assert_not_called();self.transport.assert_not_called()
        with self.assertRaises(TaskError):self.prepare(revision_note='Different text cannot overwrite an ID')

    def test_fetch_projection_revalidates_current_pins_and_same_selection_cannot_pay_again(self):
        value=self.prepare()
        self.assertEqual(self.service.selection(self.task,'7'*32),value['report'])
        with self.assertRaisesRegex(TaskError,'已有原件选取'):self.prepare('8'*32)
        self.assertEqual(self.transport.call_count,1)
        self.input['binding']['transport_identity_sha256']='a'*64
        self.assertEqual(self.service.get(self.task,'7'*32),value)
        with self.assertRaisesRegex(TaskError,'已变化'):self.service.selection(self.task,'7'*32)
        self.assertEqual(self.transport.call_count,1)

    def test_three_versions_inherit_complete_prior_answers_without_consuming_author_third_draft(self):
        self.proposal['unrecognized_but_retained']='FULL_INVALID_REPLY_'*1000
        for key in ('7','8','9'):
            value=self.prepare(key*32,revision_note='Preserve the complete prior selection')
            self.assertEqual(value['state'],'invalid_output',value)
        self.assertEqual(self.service.status(self.task)['used_rounds'],3)
        with self.assertRaisesRegex(TaskError,'最多三轮'):self.prepare('e'*32)
        self.assertEqual(self.transport.call_count,3)
        context=self.service._stored(self.task,'9'*32)[1]
        self.assertEqual(context['revision']['complete_previous_answer'],self.proposal)
        self.assertEqual(context['revision']['parent_request'],'8'*32)
        self.assertFalse(context['revision']['target_scope_reduction_allowed'])
        self.assertEqual(self.fx.calls.status()['used_requests'],self.baseline+3)

    def test_unknown_call_blocks_all_new_IDs_and_restart_retains_one_transport(self):
        self.transport.side_effect=TimeoutError('SYNTHETIC_PRIVATE_ERROR')
        value=self.prepare()
        self.assertEqual(value['state'],'unknown',value)
        self.assertNotIn('SYNTHETIC_PRIVATE_ERROR',json.dumps(value))
        with self.assertRaisesRegex(TaskError,'未知'):self.prepare('8'*32,service=self.make())
        self.assertEqual(self.prepare(service=self.make())['state'],'unknown')
        self.assertEqual(self.transport.call_count,1)
        self.assertTrue(self.service.status(self.task)['pending_reconciliation'])

    def test_no_model_allowance_has_zero_selection_intents_and_no_transport(self):
        remaining=self.fx.calls.status()['remaining_requests']
        for index in range(remaining):self.fx.calls.reserve(format(index,'032x'),b'synthetic unrelated intent')
        used=self.fx.calls.status()['used_requests']
        with self.assertRaisesRegex(TaskError,'模型额度'):self.prepare()
        self.assertEqual(self.service.history(self.task),[])
        self.provider.assert_not_called();self.transport.assert_not_called()
        self.assertEqual(self.fx.calls.status()['used_requests'],used)

    def test_wrong_reference_role_tree_binding_or_existing_pin_rejected_before_call(self):
        original=deepcopy(self.input)
        for mutate in [lambda value:value.update(role='research'),lambda value:value.update(B_input=True),
            lambda value:value['tree_report'].update(commit='f'*40),
            lambda value:value['tree_request'].update(receipt_sha256='e'*64),
            lambda value:value['acquisition_receipt'].update(bundle_sha256='e'*64),
            lambda value:value['binding'].update(transport_identity_sha256='bad'),
            lambda value:value['existing_files'][0].update(sha256='f'*64),
            lambda value:value.update(existing_files=[]),lambda value:value.update(author_plan_request_id='4'*32)]:
            with self.subTest(mutation=mutate):
                self.input=deepcopy(original);mutate(self.input)
                with self.assertRaises(ValueError):self.prepare()
        self.transport.assert_not_called();self.assertEqual(self.service.history(self.task),[])

    def test_coherent_replacement_repository_or_commit_cannot_replace_original_author_binding(self):
        original=deepcopy(self.input)
        for field,value in [('repository','another/synthetic-repo'),('commit','a'*40)]:
            with self.subTest(field=field):
                self.input=deepcopy(original)
                request=self.input['tree_request']['acquisition_request'];request[field]=value
                receipt=self.input['acquisition_receipt'];receipt[field]=value
                receipt['request_sha256']=sha256(canonical(request))
                self.input['tree_request']['receipt_sha256']=sha256(canonical(receipt))
                tree=self.input['tree_report'];tree[field]=value
                tree['receipt_sha256']=self.input['tree_request']['receipt_sha256']
                tree['request_sha256']=sha256(canonical(self.input['tree_request']))
                with self.assertRaisesRegex(TaskError,'另一作者仓库或commit'):self.prepare()
        self.transport.assert_not_called()

    def test_arbitrary_URL_directory_symlink_large_existing_or_duplicate_paths_never_pass(self):
        for path in ('https://example.invalid/file','../outside','scripts','linked','large.data','README.md','not-in-tree'):
            with self.subTest(path=path):
                proposal=deepcopy(self.proposal);proposal['selections'][0]['path']=path
                with self.assertRaises(TaskError):validate_selection(self.service._input(self.task),proposal)
        proposal=deepcopy(self.proposal);proposal['selections']*=2
        with self.assertRaises(TaskError):validate_selection(self.service._input(self.task),proposal)
        proposal=deepcopy(self.proposal);proposal['selections'][0]['sha256']='f'*64
        with self.assertRaises(TaskError):validate_selection(self.service._input(self.task),proposal)
        self.transport.assert_not_called()

    def test_count_byte_and_dependency_limits_are_enforced_without_truncation(self):
        evidence=self.service._input(self.task)
        evidence['available_files']=[dict(path='extra'+str(index),size=16*1024**2,sha='f'*40,mode='100644',
                                           type='blob',status='supported_blob') for index in range(129)]
        for count in (5,129):
            proposal=deepcopy(self.proposal);proposal['selections']=[dict(path='extra'+str(index),reason='Synthetic',
                                                                       dependency_ids=['shared']) for index in range(count)]
            with self.subTest(count=count),self.assertRaises(TaskError):validate_selection(evidence,proposal)
        for row in evidence['available_files']:row['size']=1
        proposal=deepcopy(self.proposal);proposal['selections']=[dict(path='extra'+str(index),reason='Synthetic',
                                                                   dependency_ids=['shared']) for index in range(127)]
        with self.assertRaisesRegex(TaskError,'128唯一文件'):validate_selection(evidence,proposal)
        proposal=deepcopy(self.proposal);proposal['selections'][0]['dependency_ids']=['invented']
        with self.assertRaises(TaskError):validate_selection(self.service._input(self.task),proposal)
        evidence=self.service._input(self.task)
        evidence['available_files'][0]['size']=16*1024**2+1
        with self.assertRaises(TaskError):validate_selection(evidence,self.proposal)

    def test_no_remaining_selectable_blob_has_explicit_clarification_schema(self):
        evidence=self.service._input(self.task);evidence['available_files']=[]
        context=selection_context(evidence)
        path=context['output_types']['properties']['selections']['items']['properties']['path']
        self.assertEqual(path,{'not':{}})
        value=dict(status='needs_clarification',summary='No additional selectable original exists',
                   selections=[],missing=['No supported candidate is in this fixed tree.'],limitations=[])
        self.assertEqual(validate_selection(evidence,value)['paths'],[])

    def test_clarification_is_saved_with_missing_and_cannot_claim_acquisition_or_execution(self):
        self.proposal.update(status='needs_clarification',selections=[],missing=['Source necessity cannot be established.'])
        value=self.prepare()
        self.assertEqual(value['state'],'needs_clarification',value)
        self.assertEqual(value['report']['paths'],[])
        self.assertFalse(value['report']['source_complete'])
        self.assertEqual(self.transport.call_count,1)

    def test_valid_nonempty_clarification_can_acquire_without_resolving_environment(self):
        self.proposal.update(status='needs_clarification',missing=[
            'The Python environment and complete author invocation remain unverified.'])
        value=self.prepare()
        self.assertEqual(value['state'],'needs_clarification',value)
        before=deepcopy(self.service.history(self.task))
        fetched=self.service.selection(self.task,'7'*32)
        self.assertEqual(fetched,value['report'])
        self.assertEqual(fetched['status'],'needs_clarification')
        self.assertEqual(fetched['missing'],self.proposal['missing'])
        self.assertTrue(fetched['paths'])
        for key in ('source_complete','execution_ready','execution_authorized',
                    'scientific_applicability_verified','physical_evaluation_performed'):
            self.assertIs(fetched[key],False)
        self.assertEqual(self.service.history(self.task),before)
        self.assertEqual(self.transport.call_count,1)
        self.assertEqual(self.fx.service.status(self.task)['used_rounds'],2)

    def test_empty_clarification_invalid_and_unknown_answers_cannot_acquire(self):
        self.proposal.update(status='needs_clarification',selections=[],missing=['No concrete subset is selectable.'])
        self.assertEqual(self.prepare()['state'],'needs_clarification')
        with self.assertRaisesRegex(TaskError,'非空'):self.service.selection(self.task,'7'*32)
        self.proposal['extra']='Invalid but retained'
        self.assertEqual(self.prepare('8'*32,revision_note='Fix concrete selection')['state'],'invalid_output')
        with self.assertRaises(TaskError):self.service.selection(self.task,'8'*32)
        self.transport.side_effect=TimeoutError('synthetic unknown')
        self.assertEqual(self.prepare('9'*32,revision_note='Correct invalid schema')['state'],'unknown')
        with self.assertRaises(TaskError):self.service.selection(self.task,'9'*32)
        self.assertEqual(self.transport.call_count,3)

    def test_current_connection_change_blocks_new_context_and_old_ledger_cannot_be_replaced(self):
        self.fx.connection['credential_generation']=2
        with self.assertRaisesRegex(TaskError,'变化'):self.prepare()
        self.transport.assert_not_called()
        self.fx.connection['credential_generation']=1
        self.proposal['selections'][0]['path']='absent'
        self.assertEqual(self.prepare()['state'],'invalid_output')
        calls=ModelCalls(self.root/'unrelated-ledger.sqlite',DeepSeekConfig('synthetic-author-model'),max_requests=4)
        client=DeepSeekClient(calls,transport=Mock(),key_reader=lambda:'synthetic')
        changed=dict(enabled=True,model=client.model,connection_revision=1,credential_generation=1,
                     model_ledger_sha256=model_ledger_identity(client))
        with self.assertRaisesRegex(TaskError,'模型账本'):
            self.make(lambda _:changed).prepare(self.task,'8'*32,client,connection_revision=1,credential_generation=1)
        client.transport.assert_not_called()

    def test_completed_reply_recovers_original_ID_without_second_model_after_transient_provider_error(self):
        def received(*args):
            self.provider.side_effect=TaskError('Transient synthetic provider failure')
            return 200,response(deepcopy(self.proposal))
        self.transport.side_effect=received
        paused=self.prepare()
        self.assertEqual(paused['state'],'preparation_paused',paused)
        self.assertTrue(self.service.get(self.task,'7'*32)['recoverable']);self.assertEqual(self.transport.call_count,1)
        self.provider.side_effect=lambda _:deepcopy(self.input)
        restored=self.prepare(service=self.make())
        self.assertEqual(restored['state'],'selected',restored)
        self.assertEqual(restored['context_sha256'],paused['context_sha256'])
        self.assertEqual(self.transport.call_count,1)
        self.assertEqual(self.service.status(self.task)['used_rounds'],1)

    def test_history_is_immutable_and_same_task_cannot_borrow_another_action_ID(self):
        with self.assertRaisesRegex(TaskError,'其他动作'):self.prepare('5'*32)
        self.transport.assert_not_called()
        self.prepare()
        for table in ('author_resource_selection_intents','author_resource_selection_events','author_resource_selection_receipts'):
            with self.subTest(table=table),self.assertRaisesRegex(sqlite3.IntegrityError,'immutable'):
                with self.fx.tasks.transaction() as db:db.execute('DELETE FROM '+table)
        other=self.fx.tasks.create('Other reference task','Synthetic only','reproduction')
        with self.assertRaisesRegex(TaskError,'另一任务'):self.service.get(other['id'],'7'*32)

    def test_changed_pinned_inputs_block_revision_without_spending_another_request(self):
        self.assertEqual(self.prepare()['state'],'selected')
        self.input['binding']['transport_identity_sha256']='a'*64
        with self.assertRaisesRegex(TaskError,'已变化'):self.prepare('8'*32)
        self.assertEqual(self.transport.call_count,1)
        self.assertEqual(self.service.status(self.task)['used_rounds'],1)

    def test_large_fixed_tree_uses_complete_compact_menu_without_truncating_scope_or_prior_answer(self):
        evidence=self.service._input(self.task)
        evidence['sources']=[dict(text='原文全页不在路径选择阶段重新解释。'*15000)]
        evidence['reference_scope']['scope']['limitations']=['完整范围原文限制。'*1500]
        evidence['author_plan']['report']['limitations']=['完整前稿不能截断。'*1500]
        evidence['available_files']=[dict(path=f'inputs/alloy-{i:04d}/original-scientific-input.in',
            size=100+i,type='blob',mode='100644',sha='f'*40,status='supported_blob') for i in range(1300)]
        evidence['tree_report']['entries']=deepcopy(evidence['available_files'])
        expected=deepcopy(evidence)
        from auto_lammps.deepseek import request_body
        with patch.object(self.service,'_input',return_value=evidence):
            context=self.service._context(self.task,'7'*32,self.fx.client,1,1,'')
        payload=json.loads(context['messages'][-1]['content'])
        self.assertEqual(payload['reference_scope'],expected['reference_scope'])
        self.assertEqual(payload['author_plan'],expected['author_plan'])
        self.assertEqual(payload['available_files'],{r['path']:r['size'] for r in expected['available_files']})
        self.assertEqual(payload['source_pages_binding']['sha256'],sha256(canonical(expected['sources'])))
        self.assertNotIn('tree_report',payload)
        self.assertEqual(context['input'],expected)
        self.assertEqual(evidence,expected)
        validate_prepared_messages(STAGE,context['messages'],expected)
        self.assertLess(len(request_body(self.fx.calls.config,context['messages'],model=self.fx.client.model)),524288)
        self.assertEqual(self.transport.call_count,0)
        self.assertEqual(self.fx.calls.status()['used_requests'],self.baseline)


if __name__=='__main__':unittest.main()
