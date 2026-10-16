"""Synthetic author-plan API contract; no actual model, cluster or physics."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading
from unittest.mock import Mock, patch
import unittest

from auto_lammps.author_reference_plan import (AuthorReferencePlan, STAGE, author_reference_input,
    plan_selection_context, validate_author_plan)
from auto_lammps.deepseek import DeepSeekClient, DeepSeekConfig, ModelCalls
from auto_lammps.manifest import canonical, sha256
from auto_lammps.reference_targets import model_ledger_identity
from auto_lammps.scientific_adapters import ScientificAdapterError, validate_prepared_messages
from auto_lammps.tasks import TaskError
import test_author_source_reader as reader_fixtures
import test_reference_scope as scope_fixtures


class AuthorReferencePlanTests(unittest.TestCase):
    def setUp(self):
        self.fixture = scope_fixtures.ReferenceScopeTests()
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.tasks, self.papers, self.scopes = self.fixture.tasks, self.fixture.papers, self.fixture.service
        self.task, self.paper = self.fixture.task, self.fixture.paper
        self.scope = self.fixture.create()
        self.reader = reader_fixtures.AuthorSourceReaderTests()
        self.reader.setUp(); self.addCleanup(self.reader.doCleanups)
        self.reader.policy['reference_task_id'] = self.task['id']
        self.reader.texts['README.md'] = (b'Synthetic invocation: lmp -in workflow/example.txt\n'
            b'Expected output: values.csv\nThis fixture is not a scientific program.\n')
        self.reader._write_sources(); self.reader._bind_receipt()
        self.read_request = self.reader.request([dict(path='README.md', start_line=1, end_line=20),
                                               dict(path='workflow/example.txt', start_line=1, end_line=20)])
        self.read_report = self.reader.worker(self.read_request)
        self.input = dict(task_id=self.task['id'], paper_id=self.paper['id'],
            paper={key:self.paper[key] for key in ('title', 'doi')}, scope='reference_only',
            role='author_reference_human_only', scope_id=self.scope['id'],
            author_source=dict(capability=self.reader.service().capability(self.reader.receipt, purpose='author_reference'),
                acquisition_receipt=deepcopy(self.reader.receipt), reads=[dict(request=self.read_request,response=self.read_report)]),
            invocation_paths=['README.md'], deployment=dict(environments=[dict(id='synthetic-env', label='Synthetic environment',
                sha256='a'*64, executables=[dict(id='synthetic-lmp', label='Synthetic fixture engine', sha256='b'*64, kind='lammps')])],
                analysis_capabilities=[dict(id='table-stats', sha256='c'*64, description='Synthetic existing table operation')],
                resources=dict(gpu_allowed=False, cores=32, execution_authorized=False), missing=[]), missing=[])
        self.calls = ModelCalls(self.fixture.root/'author-models.sqlite',
            DeepSeekConfig('synthetic-author-model', max_output_tokens=16000, max_input_bytes=524288), max_requests=20)
        self.sent = []
        self.client = DeepSeekClient(self.calls, transport=self.transport, key_reader=lambda:'synthetic-project-key')
        self.connection = dict(enabled=True, model=self.client.model, connection_revision=1,
                               credential_generation=1, model_ledger_sha256=model_ledger_identity(self.client))
        self.provider = Mock(side_effect=lambda _:deepcopy(self.input))
        self.connection_provider = Mock(side_effect=lambda _:deepcopy(self.connection))
        self.service = self.make_service()
        self.evidence = self.service._input(self.task['id'])
        source_id = self.evidence['invocation_source_ids'][0]
        call_cite = [dict(source_id=source_id, quote='Synthetic invocation: lmp -in workflow/example.txt')]
        output_cite = [dict(source_id=source_id, quote='Expected output: values.csv')]
        p_cite = [dict(source_id='p-text', quote='Synthetic retained source quote.')]
        file = next(row for row in self.reader.receipt['files'] if row['path']=='workflow/example.txt')
        pin = {key:file[key] for key in ('path','sha256')}
        self.value = dict(status='proposed', summary='合成作者原文件调用方案；非科研结果。', invocations=[dict(
            id='original-call', entrypoint=pin, argv=['-in','workflow/example.txt'], working_directory='.',
            environment_id='synthetic-env', executable_id='synthetic-lmp', dependency_ids=['shared'],
            required_files=[pin], output_files=[dict(path='values.csv',kind='numeric_table',description='Synthetic output',citations=output_cite)],
            citations=call_cite, missing=[])], coverage=[dict(dependency_id=name,
                invocation_ids=['original-call'] if name=='shared' else [],
                analysis_capability_ids=[] if name=='shared' else ['table-stats'], citations=deepcopy(p_cite),missing=[])
                for name in self.scope['scope']['dependency_ids']], questions=[], limitations=['Synthetic engineering evidence only.'])

    def make_service(self):
        return AuthorReferencePlan(self.tasks,self.papers,self.scopes,self.provider,
                                   connection_provider=self.connection_provider)

    def transport(self,body,key,timeout):
        self.sent.append(json.loads(body))
        return 200, canonical(dict(choices=[dict(finish_reason='stop', message=dict(role='assistant',content=json.dumps(self.value)))],
            usage=dict(prompt_tokens=10,completion_tokens=5,total_tokens=15)))

    def prepare(self,request='4'*32,service=None,**kwargs):
        return (service or self.service).prepare(self.task['id'],request,self.client,
            connection_revision=kwargs.get('connection_revision',1),credential_generation=kwargs.get('credential_generation',1),
            revision_note=kwargs.get('revision_note',''))

    def test_real_shared_deepseek_interface_produces_nonexecuting_pinned_plan(self):
        before = self.fixture.snapshot_old_records()
        result = self.prepare()
        self.assertEqual(result['state'],'prepared')
        self.assertEqual(result['round'],1)
        self.assertEqual(len(self.sent),1)
        self.assertFalse(result['approval']); self.assertFalse(result['execution_authorized'])
        self.assertFalse(result['can_submit']); self.assertFalse(result['can_approve']); self.assertFalse(result['B_input'])
        report = result['report']
        self.assertEqual(report['reference_scope']['scope']['selected_ids'],['first','second'])
        self.assertEqual([row['dependency_id'] for row in report['coverage']],['shared','post-a','post-b'])
        self.assertEqual(report['invocations'][0]['environment']['sha256'],'a'*64)
        self.assertEqual(report['invocations'][0]['executable']['sha256'],'b'*64)
        self.assertEqual(report['invocations'][0]['argv'],self.value['invocations'][0]['argv'])
        self.assertFalse(report['original_files_rewritten'])
        self.assertEqual(before,self.fixture.snapshot_old_records())
        self.assertEqual(self.calls.status()['used_requests'],1)

    def test_active_adapter_is_in_actual_messages_and_cannot_be_removed_or_changed(self):
        result = self.prepare()
        saved = self.service._stored(self.task['id'],result['request_id'])[1]
        messages = saved['messages']; payload=json.loads(messages[-1]['content'])
        adapter = payload['scientific_adapter']
        self.assertEqual(adapter['stage'],STAGE)
        self.assertEqual(adapter['rules']['allowed_source_origins'],['paper','author_source'])
        self.assertFalse(adapter['rules']['execution_authorized'])
        self.assertEqual(payload['original_author_invocation_plan'],plan_selection_context(saved['input']))
        self.assertNotIn('B_PRIVATE_CONDITION_CANARY',canonical(messages).decode())
        self.assertNotIn('PRIVATE_AUTHOR_CODE_CANARY',canonical(messages).decode())
        validate_prepared_messages(STAGE,messages,saved['input'])
        for key in ('scientific_adapter','original_author_invocation_plan'):
            altered=deepcopy(messages); data=json.loads(altered[-1]['content']); data.pop(key)
            altered[-1]['content']=canonical(data).decode()
            with self.assertRaises(ScientificAdapterError):
                validate_prepared_messages(STAGE,altered,saved['input'])

    def test_corrupt_mandatory_menu_never_reaches_model_or_creates_null_proof(self):
        valid=plan_selection_context(self.evidence)
        for invalid in (None,{},dict(valid,source_sha256='0'*64),dict(valid,execution_authorized=True)):
            with self.subTest(invalid=invalid),patch('auto_lammps.author_reference_plan.plan_selection_context',return_value=invalid):
                with self.assertRaises((TaskError,ScientificAdapterError)):
                    self.prepare()
        self.assertEqual(self.calls.status()['used_requests'],0)
        self.assertEqual(self.service.status(self.task['id'])['used_rounds'],0)

    def test_missing_sources_or_deployment_can_be_saved_as_clarification(self):
        self.input['author_source']=None; self.input['invocation_paths']=[]
        self.input['deployment']['environments']=[]
        self.input['missing']=['Synthetic original call is unavailable.']
        self.value.update(status='needs_clarification',summary='待核对原作者调用。',invocations=[],questions=['请补齐原作者调用。'])
        for row in self.value['coverage']:
            row.update(invocation_ids=[],analysis_capability_ids=[],citations=[],missing=['原调用尚缺。'])
        result=self.prepare()
        self.assertEqual(result['state'],'needs_clarification')
        self.assertEqual(len(result['report']['coverage']),3)
        self.assertFalse(result['execution_authorized'])
        self.assertEqual(len(self.sent),1)

    def test_missing_execution_approval_does_not_block_preparation(self):
        self.assertFalse(self.input['deployment']['resources']['execution_authorized'])
        self.assertFalse(self.scope['execution_authorized'])
        result = self.prepare()
        self.assertEqual(result['state'],'prepared')
        self.assertEqual(result['report']['inherited_missing'],dict(reference=[],deployment=[]))

    def test_proposed_cannot_ignore_trusted_source_or_deployment_gaps(self):
        for location, gap in (('reference','Synthetic original argument is unresolved.'),
                              ('deployment','Synthetic author interpreter has no verified pin.')):
            evidence = deepcopy(self.evidence)
            target = evidence if location == 'reference' else evidence['deployment']
            target['missing'] = [gap]
            with self.subTest(location=location),self.assertRaisesRegex(TaskError,gap):
                validate_author_plan(evidence,self.value)

    def test_ignored_trusted_gap_is_saved_as_invalid_and_fed_back_with_prior_answer(self):
        gap = 'Synthetic author interpreter has no verified pin.'
        self.input['deployment']['missing'] = [gap]
        first = self.prepare()
        self.assertEqual(first['state'],'invalid_output')
        self.assertIn(gap,first['receipt']['message'])
        original_answer = deepcopy(self.value)
        self.value['status'] = 'needs_clarification'
        second = self.prepare('5'*32)
        self.assertEqual(second['state'],'needs_clarification')
        self.assertEqual(second['revision']['complete_previous_answer'],original_answer)
        self.assertIn(gap,second['revision']['feedback']['message'])
        self.assertEqual(second['report']['inherited_missing'],dict(reference=[],deployment=[gap]))

    def test_trusted_gaps_allow_clarification_and_verified_revision_keeps_previous_answer(self):
        source_gap = 'Synthetic original argument awaits a verified source.'
        deployment_gap = 'Synthetic executable pin awaits deployment evidence.'
        self.input['missing'] = [source_gap]
        self.input['deployment']['missing'] = [deployment_gap]
        self.value['status'] = 'needs_clarification'
        original_answer = deepcopy(self.value)
        first = self.prepare()
        self.assertEqual(first['state'],'needs_clarification')
        self.assertEqual(first['report']['inherited_missing'],dict(reference=[source_gap],deployment=[deployment_gap]))
        self.assertEqual(len(self.sent),1)
        self.input['missing'] = []
        self.input['deployment']['missing'] = []
        self.input['deployment']['environments'][0]['executables'].append(dict(
            id='verified-interpreter',label='Added verified interpreter',sha256='f'*64,kind='python'))
        self.value['status'] = 'proposed'
        second = self.prepare('5'*32)
        self.assertEqual(second['state'],'prepared')
        self.assertEqual(second['revision']['complete_previous_answer'],original_answer)
        self.assertEqual(second['revision']['source_updates']['previous_missing'],[source_gap])
        self.assertEqual(second['report']['inherited_missing'],dict(reference=[],deployment=[]))
        self.assertEqual(self.service.get(self.task['id'],'4'*32),first)
        self.assertEqual(second['round'],2)

    def test_saved_scope_method_gap_can_produce_a_clarification_plan(self):
        scope=self.fixture.create(['uncertain'],scope_id='8'*32)
        self.assertFalse(scope['can_prepare_A_plan'])
        self.input['scope_id']=scope['id']
        self.value.update(status='needs_clarification',invocations=[],questions=['请核对原文方法关联。'],
            coverage=[dict(dependency_id='unknown',invocation_ids=[],analysis_capability_ids=[],citations=[],missing=['方法关联缺失。'])])
        result=self.prepare()
        self.assertEqual(result['state'],'needs_clarification')
        self.assertEqual(result['report']['reference_scope']['scope']['selected_ids'],['uncertain'])

    def test_clarification_can_gain_verified_source_without_rewriting_scope_or_previous_answer(self):
        complete_input, complete_plan=deepcopy(self.input),deepcopy(self.value)
        self.input['author_source']=None;self.input['invocation_paths']=[]
        self.input['missing']=['Synthetic author source not yet verified.']
        self.value.update(status='needs_clarification',invocations=[],questions=['需要原作者源码。'])
        for row in self.value['coverage']:
            row.update(invocation_ids=[],analysis_capability_ids=[],citations=[],missing=['待核对原调用。'])
        clarification=deepcopy(self.value)
        self.assertEqual(self.prepare()['state'],'needs_clarification')
        self.input,self.value=complete_input,complete_plan
        result=self.prepare('5'*32)
        self.assertEqual(result['state'],'prepared')
        self.assertEqual(result['revision']['complete_previous_answer'],clarification)
        self.assertTrue(result['revision']['source_updates']['original_scope_unchanged'])
        self.assertTrue(result['revision']['source_updates']['added_source_ids'])

    def test_unchanged_complete_plan_does_not_trigger_another_charge(self):
        self.assertEqual(self.prepare()['state'],'prepared')
        with self.assertRaisesRegex(TaskError,'已有完整'):
            self.prepare('5'*32)
        self.assertEqual(self.calls.status()['used_requests'],1)

    def test_invalid_full_answer_is_carried_to_next_version_with_exact_error(self):
        self.value['invocations'][0]['citations'][0]['quote']='Invented quotation'
        bad=deepcopy(self.value)
        first=self.prepare()
        self.assertEqual(first['state'],'invalid_output')
        self.value['invocations'][0]['citations'][0]['quote']='Synthetic invocation: lmp -in workflow/example.txt'
        second=self.prepare('5'*32,revision_note='只修正来源引用，保留完整方案。')
        self.assertEqual(second['state'],'prepared')
        revision=second['revision']
        self.assertEqual(revision['complete_previous_answer'],bad)
        self.assertEqual(revision['previous_answer_sha256'],sha256(canonical(bad)))
        self.assertEqual(revision['feedback']['error_code'],'author_plan_invalid')
        self.assertIn('逐字',revision['feedback']['message'])
        self.assertEqual(revision['feedback']['requested_revision'],'只修正来源引用，保留完整方案。')
        self.assertEqual(len(self.sent),2)

    def test_maximum_three_versions_survives_service_restart_and_third_is_usable(self):
        original=deepcopy(self.value)
        self.value['script']='Do not accept generated source'
        self.assertEqual(self.prepare()['state'],'invalid_output')
        self.assertEqual(self.prepare('5'*32)['state'],'invalid_output')
        self.value=original
        third=self.prepare('6'*32,service=self.make_service())
        self.assertEqual(third['state'],'prepared')
        self.assertEqual(third['round'],3)
        with self.assertRaisesRegex(TaskError,'最多三轮'):
            self.prepare('7'*32,service=self.make_service())
        self.assertEqual(self.calls.status()['used_requests'],3)
        self.assertEqual(self.service.status(self.task['id'])['remaining_rounds'],0)
        self.assertEqual(self.prepare('6'*32)['state'],'prepared')
        self.assertEqual(len(self.sent),3)

    def test_idempotent_completed_request_and_conflicting_note(self):
        first=self.prepare(); second=self.prepare()
        self.assertEqual(first,second)
        self.assertEqual(len(self.sent),1)
        with self.assertRaisesRegex(TaskError,'另一修订说明'):
            self.prepare(revision_note='A different request')

    def test_unknown_model_call_blocks_new_identity_and_never_resends(self):
        self.client.transport=Mock(side_effect=TimeoutError('do not publish private transport text'))
        result=self.prepare()
        self.assertEqual(result['state'],'unknown')
        self.assertNotIn('private transport text',canonical(result).decode())
        self.assertEqual(self.client.transport.call_count,1)
        self.assertEqual(self.prepare()['state'],'unknown')
        with self.assertRaisesRegex(TaskError,'待核对'):
            self.prepare('5'*32)
        self.assertEqual(self.client.transport.call_count,1)
        self.assertTrue(self.service.status(self.task['id'])['pending_reconciliation'])

    def test_concurrent_click_reuses_pending_identity_and_blocks_another_id(self):
        entered,release=threading.Event(),threading.Event()
        transport=self.transport
        def blocked(body,key,timeout):
            entered.set()
            if not release.wait(5):raise AssertionError('Synthetic release timed out')
            return transport(body,key,timeout)
        self.client.transport=blocked
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.prepare)
            self.assertTrue(entered.wait(5))
            try:
                pending=self.prepare()
                self.assertEqual(pending['state'],'dispatching')
                with self.assertRaisesRegex(TaskError,'正在准备'):
                    self.prepare('5'*32)
            finally:release.set()
            self.assertEqual(future.result(5)['state'],'prepared')
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.service.status(self.task['id'])['used_rounds'],1)

    def test_unsent_same_identity_recovers_without_consuming_new_round(self):
        with patch.object(self.client,'complete_json',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): self.prepare()
        self.assertEqual(self.calls.status()['used_requests'],0)
        with self.assertRaisesRegex(TaskError,'待核对'):
            self.prepare('5'*32)
        result=self.prepare()
        self.assertEqual(result['state'],'prepared')
        self.assertEqual(result['round'],1)
        self.assertEqual(len(self.sent),1)

    def test_known_complete_reply_recovers_after_service_interrupt_no_new_call(self):
        with patch.object(self.service,'_accept',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): self.prepare()
        self.assertEqual(len(self.sent),1)
        result=self.prepare(service=self.make_service())
        self.assertEqual(result['state'],'prepared')
        self.assertEqual(len(self.sent),1)

    def test_previous_full_answer_survives_an_intermediate_known_provider_failure(self):
        self.value['unknown']='bad output'
        first=self.prepare(); full=deepcopy(self.value)
        self.assertEqual(first['state'],'invalid_output')
        self.client.transport=lambda *args:(402,canonical(dict(error=dict(message='synthetic rejection'))))
        second=self.prepare('5'*32)
        self.assertEqual(second['state'],'failed')
        self.client.transport=self.transport; self.value.pop('unknown')
        third=self.prepare('6'*32)
        self.assertEqual(third['state'],'prepared')
        self.assertEqual(third['revision']['complete_previous_answer'],full)
        self.assertEqual(third['revision']['inherited_answer_from'],'4'*32)

    def test_full_original_scope_and_bidirectional_mapping_are_required(self):
        alterations=[lambda v:v['coverage'].pop(),
            lambda v:v['coverage'][0].update(dependency_id='invented'),
            lambda v:v['invocations'][0].update(dependency_ids=[]),
            lambda v:v['coverage'][0].update(invocation_ids=[],analysis_capability_ids=['table-stats']),
            lambda v:v['invocations'].append({**deepcopy(v['invocations'][0]),'id':'duplicate-call'})]
        for alter in alterations:
            value=deepcopy(self.value); alter(value)
            with self.subTest(alter=alter),self.assertRaises(TaskError):
                validate_author_plan(self.evidence,value)

    def test_no_replacement_source_unknown_environment_paths_or_inline_commands(self):
        mutations=[lambda v:v.update(script='new scientific program'),
            lambda v:v['invocations'][0]['entrypoint'].update(path='new-input.lmp'),
            lambda v:v['invocations'][0]['entrypoint'].update(sha256='f'*64),
            lambda v:v['invocations'][0].update(working_directory='../outside'),
            lambda v:v['invocations'][0].update(environment_id='unknown-env'),
            lambda v:v['invocations'][0].update(argv=['-c','print(1)']),
            lambda v:v['invocations'][0].update(argv=['-in','workflow/example.txt; echo invalid']),
            lambda v:v['invocations'][0].update(argv=['-in','${HOME}/input.lmp']),
            lambda v:v['invocations'][0].update(argv=['-in','missing.txt']),
            lambda v:v['invocations'][0].update(citations=[])]
        for mutation in mutations:
            value=deepcopy(self.value); mutation(value)
            with self.subTest(mutation=mutation),self.assertRaises((TaskError,ValueError)):
                validate_author_plan(self.evidence,value)

    def test_literal_argv_is_not_stripped_or_rewritten(self):
        value=deepcopy(self.value)
        value['invocations'][0]['argv']+=['--literal-label',' preserve these spaces ']
        checked=validate_author_plan(self.evidence,value)
        self.assertEqual(checked['invocations'][0]['argv'][-1],' preserve these spaces ')

    def test_reader_receipt_tampering_is_rejected_before_model(self):
        self.input['author_source']['reads'][0]['response']['files'][0]['text']='forged source'
        with self.assertRaises(TaskError): self.prepare()
        self.assertEqual(self.calls.status()['used_requests'],0)
        self.assertEqual(self.service.status(self.task['id'])['used_rounds'],0)

    def test_provider_cannot_supply_browser_plan_or_foreign_scope(self):
        for mutate in (lambda v:v.update(plan=deepcopy(self.value)),lambda v:v.update(role='independent_B'),
                       lambda v:v.update(scope_id='f'*32),lambda v:v.update(task_id='e'*32)):
            original=deepcopy(self.input); mutate(self.input)
            with self.subTest(mutate=mutate),self.assertRaises(TaskError):self.prepare()
            self.input=original
        self.assertEqual(len(self.sent),0)

    def test_source_or_connection_change_while_model_returns_preserves_answer_no_publish(self):
        original=self.transport
        def change(body,key,timeout):
            result=original(body,key,timeout)
            self.connection['credential_generation']=2
            return result
        self.client.transport=change
        result=self.prepare()
        self.assertEqual(result['state'],'preparation_paused')
        self.assertIsNone(result['receipt'])
        self.assertIsNone(result['report'])
        self.assertEqual(self.calls.lookup('4'*32)['receipt']['structured_output'],self.value)
        self.assertEqual(len(self.sent),1)

    def test_history_is_readable_without_model_credentials_or_live_sources(self):
        first=self.prepare()
        self.provider.side_effect=AssertionError('history must not fetch source')
        self.connection_provider.side_effect=AssertionError('history must not need credentials')
        self.assertEqual(self.service.get(self.task['id'],'4'*32),first)
        self.assertEqual(self.service.history(self.task['id']),[first])

    def test_appending_fixed_program_to_existing_environment_can_resolve_clarification(self):
        complete=deepcopy(self.value)
        self.value.update(status='needs_clarification',invocations=[],questions=['需要额外受信程序。'])
        for row in self.value['coverage']:
            row.update(invocation_ids=[],analysis_capability_ids=[],citations=[],missing=['原程序未提供。'])
        self.assertEqual(self.prepare()['state'],'needs_clarification')
        self.input['deployment']['environments'][0]['executables'].append(dict(
            id='extra-program',label='New verified program',sha256='f'*64,kind='python'))
        self.value=complete
        self.assertEqual(self.prepare('5'*32)['state'],'prepared')

    def test_source_removal_and_resource_changes_cannot_rewrite_a_saved_plan(self):
        self.prepare()
        original=deepcopy(self.input)
        self.input['author_source']['reads'][0]['request']['selections'].pop()
        self.input['author_source']['reads'][0]['response']['files'].pop()
        with self.assertRaises(TaskError):self.prepare('5'*32,revision_note='Attempt to hide old source')
        self.input=original
        self.input['deployment']['resources']['cores']=64
        with self.assertRaisesRegex(TaskError,'资源包络'):
            self.prepare('5'*32,revision_note='Attempt to alter limits')
        self.assertEqual(self.calls.status()['used_requests'],1)

    def test_private_provider_error_is_sanitized_and_not_charged(self):
        self.provider.side_effect=TaskError('PRIVATE_PROVIDER_CANARY secret value')
        with self.assertRaises(TaskError) as error:self.prepare()
        self.assertNotIn('PRIVATE_PROVIDER_CANARY',str(error.exception))
        self.assertEqual(len(self.sent),0)

    def test_history_tables_are_append_only(self):
        self.prepare()
        for table in ('author_plan_intents','author_plan_receipts','author_plan_events'):
            with self.subTest(table=table),self.assertRaises(sqlite3.IntegrityError):
                with self.tasks.transaction() as db:db.execute('DELETE FROM '+table)


if __name__=='__main__': unittest.main()
