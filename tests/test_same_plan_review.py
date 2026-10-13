"""Accounted synthetic same-draft review; no real model, scheduler or physics."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
import unittest
from unittest.mock import Mock, patch

from auto_lammps.agent_candidates import CandidateError, PlanIterationLimit
from auto_lammps.authorization import candidate_check
from auto_lammps.candidate_jobs import CandidateHistory, CandidateService
from auto_lammps.deepseek import DeepSeekClient, ModelCalls
from auto_lammps.manifest import canonical, sha256
from auto_lammps.plan_workspace import workspace
from auto_lammps.tasks import TaskStore
import test_agent_candidates as candidate_fixture
from test_candidate_jobs import frozen_research
from test_deepseek import response


class SamePlanReviewTests(unittest.TestCase):
    def setUp(self):
        self.f=candidate_fixture.AgentCandidateTests()
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.value['analysis']['files']=['result.dat']
        self.f.value['analysis']['plan']['tables'][0]['file']='result.dat'
        self.f.value['analysis']['plan']['operations'][0]['file']='result.dat'
        self.f.value['workflow']='run 0\nemit_table result.dat "0 $(pe)"'
        self.tasks=TaskStore(self.f.root/'review-tasks.sqlite')
        self.doc=frozen_research(self.tasks)
        calls=ModelCalls(self.f.root/'review-models.sqlite',self.f.calls.config,max_requests=20)
        self.transport=Mock(side_effect=lambda *args:(200,response(self.f.value)))
        self.client=DeepSeekClient(calls,transport=self.transport,key_reader=lambda:'synthetic-key')
        self.service=self.make_service()
        with patch.object(self.service.pool,'submit'):
            job=self.service.enqueue(self.doc['id'],self.doc['revision'])
        self.job_id=job['id']
        keys=[]
        for index in range(6):
            key=sha256(str(index).encode())[:32];keys.append(key)
            if index==3:
                self.event('failed',{'error':'plan_iteration_limit','detail':'Retained original failure'})
                self.service.history.authorize_development_recovery({
                    'task_id':self.doc['id'],'condition_sha256':job['condition_sha256'],
                    'original_request_ids':sorted(keys[:3]),'authorization_id':'f'*32,
                    'approved_at':'2026-10-11T00:00:00+00:00','additional_rounds':3})
            self.service.history.reserve_proposal(self.doc['id'],key,'synthetic')
            completion=self.client.complete_json(key,[{'role':'user','content':'Synthetic proposal JSON fixture.'}])
            self.event('model_proposal',{'request_id':key,
                'proposal_sha256':completion['receipt']['output_sha256'],
                'condition_sha256':job['condition_sha256']})
        self.event('failed',{'error':'plan_iteration_limit','detail':'Retained sixth-draft rejection'})
        self.original=self.service.history.get(self.doc['id'])
        self.proposal=deepcopy(self.f.value)
        self.proposal_bytes=canonical(self.proposal)
        self.transport.reset_mock()
        self.transport.side_effect=self.valid_response

    def make_service(self,resources=None):
        service=CandidateService(self.tasks,self.client,self.f.adapter,
            resources=resources or self.f.resources,snapshots=self.f.root/'snapshots',review_plan=True)
        self.addCleanup(lambda:service.close(wait=True))
        return service

    def event(self,state,payload=None):
        with self.tasks.transaction() as db:
            self.service.history._event(db,self.job_id,state,payload)

    def valid_response(self,body,*args):
        payload=json.loads(body)
        source=json.loads(payload['messages'][1]['content'])
        self.assertNotIn('proposal_round_budget',source)
        return 200,response({'issues':[],'summary':'Non-blocking descriptive warning retained.',
            'coverage':[{'requirement':item['id'],'evidence':[
                {'source':'rendered_script','quote':'run 0'}]}
                for item in source['requirement_references']]})

    def review(self,key='b'*32,service=None):
        service=service or self.service
        with patch.object(service.pool,'submit'):
            service.enqueue_review(self.doc['id'],self.doc['revision'],request_id=key)
        service.run(self.doc['id'])
        return service.history.get(self.doc['id'])

    def test_sixth_draft_passes_fresh_review_without_seventh_proposal_or_submission(self):
        old_events=deepcopy(self.original['events'])
        self.assertTrue(self.service.plan_review(self.doc['id'])['same_plan_review']['can_recheck'])
        with patch('subprocess.Popen',side_effect=AssertionError('No physical engine')):
            final=self.review()
        self.assertEqual(final['state'],'prepared',final['result'])
        self.assertEqual(final['proposal_rounds'],self.original['proposal_rounds'])
        self.assertEqual(final['proposal_rounds']['used'],6)
        self.assertEqual(final['review_rounds'],{'limit':3,'used':1,'remaining':2})
        self.assertEqual(final['events'][:len(old_events)],old_events)
        generation=json.loads(self.service.file(self.doc['id'],'generation.json'))
        self.assertEqual(canonical(generation['proposal']),self.proposal_bytes)
        self.assertEqual(generation['input']['previous_proposal']['sha256'],sha256(self.proposal_bytes))
        self.assertFalse(generation['execution_authorized'])
        self.assertFalse(final['execution_authorized'])
        self.assertEqual(generation['plan_reviews'][0]['summary'],'Non-blocking descriptive warning retained.')
        self.assertEqual(self.transport.call_count,1)
        self.assertEqual(self.client.calls.status()['used_requests'],7)
        from auto_lammps.manifest import Snapshot
        check=candidate_check(Snapshot(self.service.snapshots/final['result']['snapshot_sha256'],
                                       final['result']['snapshot_sha256']),max_atoms=100000)
        self.assertEqual(check['scientific_status'],'not_evaluated')
        view=workspace(self.service,self.doc['id'])
        self.assertEqual(view['current']['version'],6)
        self.assertTrue(view['current']['same_plan_review'])
        self.assertEqual(len(view['versions']),1)
        self.assertFalse(view['same_plan_review']['can_recheck'])
        self.assertEqual(len(self.service.plan_review(self.doc['id'])['review_history']),3)

    def test_repeated_and_concurrent_operation_identity_is_idempotent(self):
        with patch.object(self.service.pool,'submit'):
            with ThreadPoolExecutor(max_workers=4) as pool:
                jobs=list(pool.map(lambda _:self.service.enqueue_review(self.doc['id'],
                    self.doc['revision'],request_id='b'*32),range(8)))
        self.assertEqual({j['id'] for j in jobs},{self.job_id})
        self.service.run(self.doc['id'])
        restarted=self.make_service()
        restarted.enqueue_review(self.doc['id'],self.doc['revision'],request_id='b'*32)
        self.assertEqual(self.transport.call_count,1)
        job=CandidateHistory(TaskStore(self.tasks.path)).get(self.doc['id'])
        self.assertEqual(sum(e['state']=='review_recovery_requested' for e in job['events']),1)

    def test_real_blocker_retained_and_three_review_calls_remain_bounded_after_restart(self):
        def blocked(body,*args):
            status,envelope=self.valid_response(body,*args)
            data=json.loads(envelope)
            report=json.loads(data['choices'][0]['message']['content'])
            report['issues']=['Synthetic executed artifact is wrong.']
            return status,response(report)
        self.transport.side_effect=blocked
        for key in ('b'*32,'c'*32,'d'*32):
            service=self.make_service()
            final=self.review(key,service)
            self.assertEqual(final['state'],'failed')
            self.assertIn('executed artifact is wrong',final['result']['detail'])
            self.assertEqual(final['proposal_rounds']['used'],6)
        self.assertEqual(self.transport.call_count,3)
        self.assertEqual(final['review_rounds'],{'limit':3,'used':3,'remaining':0})
        self.assertFalse(self.service.review_availability(self.doc['id'])['can_recheck'])
        with self.assertRaises(CandidateError):
            self.service.enqueue_review(self.doc['id'],self.doc['revision'],request_id='e'*32)
        self.assertEqual(self.transport.call_count,3)
        self.assertEqual(len([e for e in final['events'] if e['state']=='model_review']),3)

    def test_report_format_repair_spends_review_slots_without_proposal_regeneration(self):
        valid=self.valid_response
        def repair(body,*args):
            if len(json.loads(body)['messages'])==2:
                return 200,response({'issues':[],'coverage':[],'summary':'Malformed report retained.'})
            self.assertIn('SAME unchanged proposal',json.loads(body)['messages'][-1]['content'])
            return valid(body,*args)
        self.transport.side_effect=repair
        final=self.review()
        self.assertEqual(final['state'],'prepared',final['result'])
        self.assertEqual(final['review_rounds']['used'],2)
        self.assertEqual(final['proposal_rounds']['used'],6)
        generation=json.loads(self.service.file(self.doc['id'],'generation.json'))
        self.assertEqual(canonical(generation['proposal']),self.proposal_bytes)
        self.assertEqual(len(generation['model_receipts']),3)
        self.assertEqual(len([e for e in final['events'] if e['state']=='model_review']),2)
        self.assertEqual(self.transport.call_count,2)

    def test_report_repair_cannot_take_a_fourth_review_slot(self):
        self.transport.side_effect=lambda *args:(200,response(
            {'issues':[],'coverage':[],'summary':'Malformed review retained.'}))
        first=self.review('b'*32)
        self.assertEqual(first['state'],'failed')
        self.assertEqual(first['review_rounds']['used'],2)
        final=self.review('c'*32)
        self.assertEqual(final['state'],'failed')
        self.assertEqual(final['result']['error'],'review_iteration_limit')
        self.assertEqual(final['review_rounds']['used'],3)
        self.assertEqual(final['proposal_rounds']['used'],6)
        self.assertEqual(self.transport.call_count,3)
        checks=[e['payload'] for e in final['events'] if e['state']=='model_review']
        self.assertEqual(len(checks),3)
        self.assertTrue(all(check['status']=='invalid_report' for check in checks))

    def test_unknown_review_outcome_blocks_same_and_new_intent_without_recharging(self):
        self.transport.side_effect=TimeoutError('Synthetic unknown transport')
        final=self.review()
        self.assertEqual(final['state'],'failed')
        self.assertEqual(final['result']['error'],'model_transport_unknown')
        self.assertEqual(final['review_rounds']['used'],1)
        self.assertFalse(self.service.review_availability(self.doc['id'])['can_recheck'])
        self.service.enqueue_review(self.doc['id'],self.doc['revision'],request_id='b'*32)
        with self.assertRaisesRegex(CandidateError,'未知'):
            self.service.enqueue_review(self.doc['id'],self.doc['revision'],request_id='c'*32)
        self.assertEqual(self.transport.call_count,1)

    def test_completed_review_receipt_survives_worker_loss_and_is_reused_exactly(self):
        original=self.client.complete_json
        def interrupted(*args,**kwargs):
            original(*args,**kwargs)
            raise KeyboardInterrupt('Synthetic worker death after recorded completion')
        with patch.object(self.service.pool,'submit'):
            self.service.enqueue_review(self.doc['id'],self.doc['revision'],request_id='b'*32)
        with patch.object(self.client,'complete_json',side_effect=interrupted),self.assertRaises(KeyboardInterrupt):
            self.service.run(self.doc['id'])
        restarted=self.make_service()
        final=self.review('b'*32,restarted)
        self.assertEqual(final['state'],'prepared',final['result'])
        self.assertEqual(self.transport.call_count,1)
        self.assertEqual(final['review_rounds']['used'],1)
        self.assertEqual(final['proposal_rounds']['used'],6)

    def test_reserved_recovery_with_proven_unsent_model_request_resumes_same_identity(self):
        with patch.object(self.service.pool,'submit'):
            self.service.enqueue_review(self.doc['id'],self.doc['revision'],request_id='b'*32)
        with patch.object(self.client,'complete_json',side_effect=KeyboardInterrupt('Before model reservation')):
            with self.assertRaises(KeyboardInterrupt): self.service.run(self.doc['id'])
        self.assertEqual(self.client.calls.status()['used_requests'],6)
        self.transport.assert_not_called()
        final=self.review('b'*32,self.make_service())
        self.assertEqual(final['state'],'prepared',final['result'])
        self.assertEqual(self.transport.call_count,1)
        self.assertEqual(final['review_rounds']['used'],1)

    def test_unresolved_original_review_cannot_be_bypassed_by_new_recovery(self):
        key='a'*32
        self.client.calls.reserve(key,canonical({'synthetic':'unknown original review'}))
        self.event('candidate_review_request',{'request_id':key})
        self.assertFalse(self.service.review_availability(self.doc['id'])['can_recheck'])
        with self.assertRaisesRegex(CandidateError,'未知'):
            self.service.enqueue_review(self.doc['id'],self.doc['revision'],request_id='b'*32)
        self.transport.assert_not_called()

    def test_saved_original_resource_digest_prevents_rebasing_to_changed_resources(self):
        link=next(e['payload'] for e in reversed(self.original['events']) if e['state']=='model_proposal')
        self.event('model_proposal',{**link,'resources_sha256':sha256(canonical(vars(self.f.resources)))})
        changed=self.make_service(replace(self.f.resources,wall_seconds=61))
        self.assertFalse(changed.review_availability(self.doc['id'])['can_recheck'])
        with self.assertRaisesRegex(CandidateError,'资源已变化'):
            changed.enqueue_review(self.doc['id'],self.doc['revision'],request_id='b'*32)
        self.transport.assert_not_called()

    def test_changed_guidance_after_queue_stops_before_model_request(self):
        with patch.object(self.service.pool,'submit'):
            self.service.enqueue_review(self.doc['id'],self.doc['revision'],request_id='b'*32)
        self.tasks.add_guidance(self.doc['id'],self.doc['revision'],'Synthetic new guidance')
        self.service.run(self.doc['id'])
        final=self.service.history.get(self.doc['id'])
        self.assertEqual(final['state'],'failed')
        self.assertEqual(final['review_rounds']['used'],0)
        self.transport.assert_not_called()

    def test_changed_resource_config_after_queue_stops_before_model_request(self):
        with patch.object(self.service.pool,'submit'):
            self.service.enqueue_review(self.doc['id'],self.doc['revision'],request_id='b'*32)
        changed=self.make_service(replace(self.f.resources,wall_seconds=61))
        changed.run(self.doc['id'])
        final=changed.history.get(self.doc['id'])
        self.assertEqual(final['state'],'configuration_changed')
        self.assertEqual(final['review_rounds']['used'],0)
        self.transport.assert_not_called()

    def test_live_model_configuration_change_after_queue_is_rejected_before_sending(self):
        with patch.object(self.service.pool,'submit'):
            self.service.enqueue_review(self.doc['id'],self.doc['revision'],request_id='b'*32)
        self.client.calls.config=replace(self.client.calls.config,max_output_tokens=4097)
        self.service.run(self.doc['id'])
        final=self.service.history.get(self.doc['id'])
        self.assertEqual(final['state'],'failed')
        self.assertEqual(final['review_rounds']['used'],0)
        self.transport.assert_not_called()

    def test_changed_live_resources_after_queue_are_rejected_before_sending(self):
        with patch.object(self.service.pool,'submit'):
            self.service.enqueue_review(self.doc['id'],self.doc['revision'],request_id='b'*32)
        self.service.resources=replace(self.f.resources,wall_seconds=61)
        self.service.run(self.doc['id'])
        final=self.service.history.get(self.doc['id'])
        self.assertEqual(final['state'],'failed')
        self.assertEqual(final['review_rounds']['used'],0)
        self.transport.assert_not_called()

    def test_corrupt_raw_proposal_link_cannot_be_rechecked(self):
        linked=next(e['payload'] for e in reversed(self.original['events']) if e['state']=='model_proposal')
        self.event('model_proposal',{**linked,'proposal_sha256':'0'*64})
        self.assertFalse(self.service.review_availability(self.doc['id'])['can_recheck'])
        with self.assertRaises(CandidateError):
            self.service.enqueue_review(self.doc['id'],self.doc['revision'],request_id='b'*32)
        self.transport.assert_not_called()

    def test_corrupt_static_proposal_never_reaches_review_or_regeneration(self):
        self.f.value['workflow']='shell false'
        key='a'*32
        self.transport.side_effect=lambda *args:(200,response(self.f.value))
        saved=self.client.complete_json(key,[{'role':'user','content':'Synthetic malformed proposal JSON.'}])
        self.event('model_proposal',{'request_id':key,'proposal_sha256':saved['receipt']['output_sha256'],
            'condition_sha256':self.original['condition_sha256']})
        self.transport.reset_mock()
        self.transport.side_effect=self.valid_response
        final=self.review()
        self.assertEqual(final['state'],'failed')
        self.assertEqual(final['review_rounds']['used'],0)
        self.transport.assert_not_called()

    def test_normalization_cannot_change_raw_proposal_bytes_during_recheck(self):
        self.f.value['analysis']['files']=['/output/result.dat']
        self.transport.side_effect=lambda *args:(200,response(self.f.value))
        saved=self.client.complete_json('a'*32,[{'role':'user','content':'Synthetic unnormalized JSON.'}])
        self.event('model_proposal',{'request_id':'a'*32,'proposal_sha256':saved['receipt']['output_sha256'],
            'condition_sha256':self.original['condition_sha256']})
        self.transport.reset_mock();self.transport.side_effect=self.valid_response
        final=self.review()
        self.assertEqual(final['state'],'failed')
        self.assertIn('cannot normalize or alter',final['result']['detail'])
        self.assertEqual(final['review_rounds']['used'],0)
        retained=self.client.calls.lookup('a'*32)['receipt']['structured_output']
        self.assertEqual(retained['analysis']['files'],['/output/result.dat'])
        self.transport.assert_not_called()


if __name__=='__main__':
    unittest.main()
