"""One append-only shared three-request development grant; fake transports only."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import sqlite3
import unittest
from unittest.mock import Mock, patch

from auto_lammps.manifest import canonical, sha256
from auto_lammps.reference_target_recovery import ReferenceTargetAnalysis
from auto_lammps.scientific_adapters import validate_prepared_messages
from auto_lammps.tasks import TaskError
import test_workbench_target_inventory as inventory_fixture


class ReferenceTargetDevelopmentBudgetTests(unittest.TestCase):
    def setUp(self):
        self.f=inventory_fixture.WorkbenchTargetInventoryTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.inventory=self.f.service;self.task=self.f.task['id']
        self.f.client.transport=self.transport
        self.target_value=dict(targets=[dict(target_id='fig1',data_class='unclear',priority=1,
            reason='Synthetic method requires checking.',citations=[dict(source_id='paper-page-1',quote=self.f.quote)],
            dependency_ids=['shared'],missing=['Synthetic method not verified.'])],
            dependencies=[dict(id='shared',data_class='unclear',label='Synthetic source',description='Synthetic source data.',
                requires=[],citations=[dict(source_id='paper-page-1',quote=self.f.quote)],missing=['Synthetic missing method.'])],
            limitations=['Synthetic budget exercise only.'])
        first=self.f.generate();self.assertEqual(first['state'],'completed',first)
        self.original=deepcopy(self.inventory.reference_target_source(self.task))
        self.service=ReferenceTargetAnalysis(self.f.tasks,self.f.papers,self.inventory.reference_target_source,
            connection_provider=self.f.connection_provider,
            historical_reference_provider=lambda identifier,**pins:deepcopy(self.original))
        self.inventory.development_recovery=self.service
        self.parent,self.replay,self.third='e'*32,'f'*32,'1'*32
        with patch.object(self.f.client,'transport',side_effect=TimeoutError('synthetic original unknown')):
            self.assertEqual(self.analyze(self.parent)['state'],'unknown')
        self.target_value['targets'][0]['citations'][0]['quote']='NOT_A_SOURCE_QUOTE'
        failed=self.service.reissue_unknown(self.task,self.replay,self.f.client,unknown_reissue_of=self.parent,
            supplier_checked=True,retain_unknown_charge=True,connection_revision=1,credential_generation=1)
        self.assertEqual(failed['state'],'unavailable',failed)
        self.target_value['targets'][0]['citations'][0]['quote']=self.f.quote
        complete=self.service.revise_original(self.task,self.third,self.f.client,parent_request=self.replay,
            connection_revision=1,credential_generation=1)
        self.assertEqual(complete['state'],'completed',complete)
        self.grant=dict(id='2'*32,task_id=self.task,prior_target_request_ids=[self.parent,self.replay,self.third],
            prior_inventory_request_ids=[self.f.request],additional_requests=3,approval_sha256='a'*64,
            reason='Synthetic explicit same-task inventory/dependency refinement approval.')

    def transport(self,body,key,timeout):
        payload=json.loads(body);user=json.loads(payload['messages'][-1]['content'])
        if 'reference_target_dependency_menu' not in user:return self.f.transport(body,key,timeout)
        self.f.sent.append(payload)
        return 200,canonical(dict(choices=[dict(finish_reason='stop',message=dict(role='assistant',
            content=json.dumps(self.target_value)))],usage=dict(prompt_tokens=9,completion_tokens=4,total_tokens=13)))

    def analyze(self,request):
        return self.service.analyze(self.task,request,self.f.client,connection_revision=1,credential_generation=1)

    def authorize(self,**changes):
        return self.service.authorize_development_recovery({**deepcopy(self.grant),**changes})

    def refine(self,request='3'*32):
        original=self.f.value['inventory']['targets'][0]
        self.f.value['inventory']['targets']=[{**original,'id':'fig1a','kind':'subfigure'},
            {**original,'id':'table1-column','label':'Synthetic table column','kind':'table'}]
        citation=[dict(source_id='paper-page-1',quote=self.f.quote)]
        self.f.value['target_evidence']=[dict(target_id=t['id'],priority=1,citations=deepcopy(citation))
            for t in self.f.value['inventory']['targets']]
        for section in self.f.value['coverage'].values():
            for row in section:
                if row['target_ids']:row['target_ids']=['fig1a','table1-column']
        # A new published-P receipt may retain exactly the same PDF and sources.
        self.f.inputs['publication']['request_id']='4'*32
        self.f.inputs['publication']['receipt_sha256']='b'*64
        self.f.inputs['source_binding']['source_receipt_sha256']='b'*64
        result=self.f.generate(request=request)
        self.assertEqual(result['state'],'completed',result)
        base=self.target_value['targets'][0]
        self.target_value['targets']=[{**deepcopy(base),'target_id':row['id']} for row in self.f.value['inventory']['targets']]
        return result

    def test_shared_three_requests_across_real_services_and_complete_parent_iteration(self):
        before=deepcopy(self.f.calls.lookup(self.parent));old=self.service._stored(self.task,self.third)
        status=self.authorize();self.assertEqual(status['remaining_requests'],3)
        self.assertEqual(status['parent_target_request'],self.third)
        self.assertEqual(status['parent_inventory_request'],self.f.request)
        self.refine()
        self.assertEqual(self.service.development_status(self.task)['used_requests'],1)
        result=self.analyze('5'*32);self.assertEqual(result['state'],'completed',result)
        context=self.service._stored(self.task,'5'*32)[1]
        payload=json.loads(context['messages'][-1]['content'])
        self.assertEqual(payload['reference_target_revision']['parent_request'],self.third)
        self.assertEqual(payload['reference_target_revision']['complete_previous_answer'],
            self.f.calls.lookup(self.third)['receipt']['structured_output'])
        self.assertEqual(payload['sources'],self.original['sources'])
        self.assertEqual(payload['reference_target_revision']['inventory_refinement']['current_target_ids'],
            ['fig1a','table1-column'])
        menu=payload['verified_inventory_citations']
        inventory_context,inventory_receipt,_=self.inventory._stored(self.task,'3'*32)
        self.assertEqual(menu['inventory_request_id'],'3'*32)
        self.assertEqual(menu['target_evidence'],inventory_receipt['report']['target_evidence'])
        self.assertEqual(menu['citation_check'],'exact_paper_source_membership')
        self.assertEqual(menu['context_sha256'],sha256(canonical(inventory_context)))
        self.assertEqual(menu['receipt_sha256'],sha256(canonical(inventory_receipt)))
        self.assertIn('do not reconstruct captions',context['messages'][0]['content'])
        validate_prepared_messages('reference_target_analysis',context['messages'],context['input'])
        self.assertEqual(self.service.development_status(self.task)['remaining_requests'],1)
        last=self.analyze('6'*32);self.assertEqual(last['state'],'completed',last)
        next_context=self.service._stored(self.task,'6'*32)[1]
        self.assertEqual(next_context['revision_parent']['parent_request'],'5'*32)
        self.assertEqual(self.service.development_status(self.task)['used_requests'],3)
        used=self.f.calls.status()['used_requests']
        with self.assertRaisesRegex(TaskError,'三次开发恢复'):self.analyze('7'*32)
        self.assertEqual(self.f.calls.status()['used_requests'],used)
        self.assertEqual(self.service._stored(self.task,self.third),old)
        self.assertEqual(self.f.calls.lookup(self.parent),before)
        with self.f.tasks.transaction() as db:
            self.assertEqual(db.execute('SELECT max_suggestions FROM reference_target_scopes WHERE task_id=?',
                (self.task,)).fetchone()[0],3)
            self.assertEqual(db.execute('SELECT cap FROM workbench_target_scopes WHERE task_id=?',
                (self.task,)).fetchone()[0],3)

    def test_grant_is_same_task_once_append_only_idempotent_and_has_no_extra_calls(self):
        used=self.f.calls.status()['used_requests'];status=self.authorize()
        self.assertEqual(self.authorize(),status)
        restarted=ReferenceTargetAnalysis(self.f.tasks,self.f.papers,self.inventory.reference_target_source,
            connection_provider=self.f.connection_provider,
            historical_reference_provider=lambda identifier,**pins:deepcopy(self.original))
        self.assertEqual(restarted.development_status(self.task),status)
        for changes in ({'id':'9'*32},{'additional_requests':4},{'additional_requests':True},
                {'prior_target_request_ids':[self.third]},{'prior_inventory_request_ids':[]},
                {'prior_target_request_ids':[self.parent,self.third,self.third]},
                {'approval_sha256':'invalid'},{'execution_authorized':True}):
            with self.subTest(changes=changes),self.assertRaises(TaskError):self.authorize(**changes)
        for action in ('DELETE','UPDATE'):
            with self.subTest(action=action),self.assertRaises(sqlite3.IntegrityError):
                with self.f.tasks.transaction() as db:
                    db.execute('DELETE FROM reference_target_development_grants' if action=='DELETE' else
                        'UPDATE reference_target_development_grants SET document=document')
        self.assertEqual(self.f.calls.status()['used_requests'],used)

    def test_ungranted_task_still_stops_at_original_three(self):
        self.assertFalse(self.service.development_status(self.task)['authorized'])
        with self.assertRaises(TaskError):self.analyze('5'*32)
        self.assertEqual(self.f.calls.status()['used_requests'],4)
        with self.f.tasks.transaction() as db:
            with self.assertRaises(TaskError):self.service.assert_development_request(db,self.task)

    def test_unknown_new_target_blocks_all_additional_calls_without_automatic_reissue(self):
        self.authorize();self.refine()
        with patch.object(self.f.client,'transport',side_effect=TimeoutError('synthetic new unknown')):
            self.assertEqual(self.analyze('5'*32)['state'],'unknown')
        used=self.f.calls.status()['used_requests']
        with self.assertRaises(TaskError):self.analyze('6'*32)
        with self.f.tasks.transaction() as db:
            with self.assertRaises(TaskError):self.service.assert_development_request(db,self.task)
        with self.assertRaises(TaskError):
            self.service.reissue_unknown(self.task,'7'*32,self.f.client,unknown_reissue_of='5'*32,
                supplier_checked=True,retain_unknown_charge=True,connection_revision=1,credential_generation=1)
        self.assertEqual(self.f.calls.status()['used_requests'],used)

    def test_p_source_or_model_output_changes_are_not_permitted_inventory_refinements(self):
        self.authorize();self.refine();used=self.f.calls.status()['used_requests']
        self.f.inputs['sources'][0]['text']+=' Changed original source.'
        changed=self.analyze('5'*32)
        self.assertEqual(changed['state'],'unavailable')
        self.assertEqual(self.f.calls.status()['used_requests'],used)
        self.assertEqual(self.service.development_status(self.task)['used_requests'],2)

    def test_shared_atomic_budget_prevents_both_stages_taking_last_request(self):
        self.authorize();self.refine();self.analyze('5'*32)
        def reserve(stage,request):
            try:
                with self.f.tasks.transaction() as db:
                    self.service.assert_development_request(db,self.task)
                    context=dict(task_id=self.task);raw=canonical(context).decode();digest=sha256(canonical(context))
                    table='reference_target' if stage=='target' else 'workbench_target'
                    db.execute(f'INSERT INTO {table}_intents VALUES (?,?,?,?,?)',
                        (request,self.task,raw,digest,'z-synthetic'))
                return True
            except TaskError:return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            wins=list(pool.map(lambda args:reserve(*args),[('target','6'*32),('inventory','7'*32)]))
        self.assertEqual(sum(wins),1)
        self.assertEqual(self.service.development_status(self.task)['remaining_requests'],0)

    def test_unresolved_inventory_unknown_prevents_authorization_without_discarding_history(self):
        context,receipt,_events=self.inventory._stored(self.task,self.f.request)
        receipt={**deepcopy(receipt),'request_id':'8'*32,'state':'unknown'}
        with self.f.tasks.transaction() as db:
            db.execute('INSERT INTO workbench_target_intents VALUES (?,?,?,?,?)',
                ('8'*32,self.task,canonical(context).decode(),sha256(canonical(context)),'z-synthetic'))
            db.execute('INSERT INTO workbench_target_receipts VALUES (?,?,?)',
                ('8'*32,canonical(receipt).decode(),sha256(canonical(receipt))))
        with self.assertRaises(TaskError):
            self.authorize(prior_inventory_request_ids=[self.f.request,'8'*32])
        self.assertFalse(self.service.development_status(self.task)['authorized'])
        self.assertEqual(self.inventory._stored(self.task,'8'*32)[1]['state'],'unknown')

    def test_fixed_grant_old_request_sha_or_cross_task_identity_cannot_be_changed(self):
        self.authorize()
        original=self.service._development_snapshot
        def changed(db,identifier,stage):
            rows=deepcopy(original(db,identifier,stage))
            if stage=='target':rows[0]['context_sha256']='0'*64
            return rows
        with patch.object(self.service,'_development_snapshot',side_effect=changed),self.assertRaises(TaskError):
            self.service.development_status(self.task)
        other=self.f.tasks.create('Synthetic separate research','Synthetic request','research')
        with self.assertRaises(TaskError):self.authorize(task_id=other['id'])
        self.assertEqual(self.service.development_status(self.task)['used_requests'],0)

    def test_verified_citation_menu_refuses_changed_model_output_or_wrong_publication(self):
        self.authorize();self.refine();evidence=self.inventory.reference_target_source(self.task)
        calls=self.f.calls.lookup
        def altered(identifier):
            call=deepcopy(calls(identifier))
            if identifier=='3'*32:
                call['receipt']['structured_output']['target_evidence'][0]['citations'][0]['quote']='Reconstructed caption.'
            return call
        with patch.object(self.f.calls,'lookup',side_effect=altered),self.assertRaises(TaskError):
            self.service._verified_inventory_citations(self.task,evidence,self.f.client)
        changed=deepcopy(evidence);changed['source_binding']['source_receipt_sha256']='0'*64
        with self.assertRaises(TaskError):self.service._verified_inventory_citations(self.task,changed,self.f.client)
        with patch('auto_lammps.workbench_target_inventory.validate_inventory_output',return_value={}),self.assertRaises(TaskError):
            self.service._verified_inventory_citations(self.task,evidence,self.f.client)
        self.assertEqual(self.service.development_status(self.task)['used_requests'],1)
