"""One explicit same-draft recovery; synthetic accounting only, no provider or HPC."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

from auto_lammps.agent_candidates import CandidateError
from auto_lammps.candidate_jobs import CandidateService
from auto_lammps.deepseek import DeepSeekClient, ModelCalls
from auto_lammps.manifest import canonical, sha256
from auto_lammps.tasks import TaskStore
import test_agent_candidates as fixtures
from test_candidate_jobs import frozen_research
from test_deepseek import response


class SupplierCheckedCandidateReviewTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.AgentCandidateTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.f.value['analysis']['files'] = ['result.dat']
        self.f.value['analysis']['plan']['tables'][0]['file'] = 'result.dat'
        self.f.value['analysis']['plan']['operations'][0]['file'] = 'result.dat'
        self.f.value['workflow'] = 'run 0\nemit_table result.dat "0 $(pe)"'
        self.tasks = TaskStore(self.f.root/'supplier-tasks.sqlite')
        self.doc = frozen_research(self.tasks)
        calls = ModelCalls(self.f.root/'supplier-models.sqlite',self.f.calls.config,max_requests=30)
        self.transport = Mock(side_effect=lambda *args:(200,response(self.f.value)))
        self.client = DeepSeekClient(calls,transport=self.transport,key_reader=lambda:'synthetic-key')
        self.service = CandidateService(self.tasks,self.client,self.f.adapter,resources=self.f.resources,
            snapshots=self.f.root/'supplier-snapshots',review_plan=True)
        self.addCleanup(lambda:self.service.close(wait=True))
        with patch.object(self.service.pool,'submit'):
            job = self.service.enqueue(self.doc['id'],self.doc['revision'])
        self.job_id = job['id']; keys = []
        for index in range(5):
            key = sha256(str(index).encode())[:32]; keys.append(key)
            if index == 3:
                self.event('failed',{'error':'plan_iteration_limit'})
                self.service.history.authorize_development_recovery(dict(task_id=self.doc['id'],
                    condition_sha256=job['condition_sha256'],original_request_ids=sorted(keys[:3]),
                    authorization_id='f'*32,approved_at='2026-10-11T00:00:00+00:00',additional_rounds=3))
            self.service.history.reserve_proposal(self.doc['id'],key,'synthetic')
            value = self.client.complete_json(key,[{'role':'user','content':'Synthetic proposal JSON.'}])
            self.event('model_proposal',dict(request_id=key,proposal_sha256=value['receipt']['output_sha256'],
                condition_sha256=job['condition_sha256'],resources_sha256=sha256(canonical(vars(self.f.resources)))))
        self.original = 'e'*32
        self.client.calls.reserve(self.original,b'synthetic historical review body, dispatch unconfirmed')
        self.event('candidate_review_request',{'request_id':self.original})
        self.event('interrupted',{'message':'Synthetic historical interruption'})
        self.before = self.service.history.get(self.doc['id'])
        self.transport.reset_mock(); self.transport.side_effect = self.valid_review

    def event(self,state,payload):
        with self.tasks.transaction() as db:
            self.service.history._event(db,self.job_id,state,payload)

    def valid_review(self,body,*args):
        source = json.loads(json.loads(body)['messages'][1]['content'])
        return 200,response(dict(issues=[],summary='Synthetic static review, not scientific success.',
            coverage=[{'requirement':item['id'],'evidence':[
                {'source':'rendered_script','quote':'run 0'}]} for item in source['requirement_references']]))

    def enqueue(self,child='a'*32,*,original=None,note='Supplier record checked; discard the old unknown result.'):
        with patch.object(self.service.pool,'submit'):
            return self.service.enqueue_review_after_supplier_check(self.doc['id'],self.doc['revision'],
                request_id=child,original_request_id=original or self.original,note=note)

    def test_safe_menu_is_local_and_does_not_imply_original_dispatch(self):
        menu = self.service.review_availability(self.doc['id'])['supplier_check_recoveries']
        self.assertEqual(len(menu),1)
        self.assertTrue(menu[0]['can_recover'])
        self.assertEqual(menu[0]['original_request_id'],self.original)
        self.assertTrue(menu[0]['retains_unknown_charge'])
        self.assertFalse(menu[0]['supplier_checked'])
        self.assertNotIn('dispatch_confirmed',menu[0])
        self.transport.assert_not_called()
        self.assertIsNone(self.client.calls.lookup(self.original)['receipt'])

    def test_explicit_recovery_uses_current_adapter_same_fifth_draft_and_keeps_old_cost(self):
        old_call = deepcopy(self.client.calls.lookup(self.original))
        self.service.resources_provider = Mock(side_effect=AssertionError('Do not rebase original resources'))
        self.enqueue(); self.service.run(self.doc['id'])
        result = self.service.history.get(self.doc['id'])
        self.assertEqual(result['state'],'prepared',result['result'])
        self.assertEqual(result['proposal_rounds'],self.before['proposal_rounds'])
        self.assertEqual(result['proposal_rounds']['used'],5)
        self.assertEqual(result['review_rounds'],dict(limit=3,used=1,remaining=2))
        self.assertEqual(result['events'][:len(self.before['events'])],self.before['events'])
        self.assertEqual(self.client.calls.lookup(self.original),old_call)
        self.assertEqual(self.client.calls.status()['used_requests'],7)  # 5 drafts + old unknown + new review
        self.assertEqual(self.transport.call_count,1)
        generation = json.loads(self.service.file(self.doc['id'],'generation.json'))
        self.assertEqual(generation['proposal'],self.f.value)
        self.assertEqual(generation['input']['resources'],vars(self.f.resources))
        sent = json.loads(self.transport.call_args.args[0])
        self.assertIn('mandatory',sent['messages'][0]['content'].lower())
        new_call = next(e['payload']['request_id'] for e in result['events'] if e['state']=='review_request')
        self.assertNotEqual(new_call,self.original)
        self.assertNotEqual(self.client.calls.lookup(new_call)['request_sha256'],old_call['request_sha256'])

    def test_repeated_child_is_idempotent_but_new_child_or_note_is_not_authorized(self):
        first = self.enqueue()
        self.assertEqual(self.enqueue()['id'],first['id'])
        self.service.run(self.doc['id'])
        self.enqueue()
        self.assertEqual(self.transport.call_count,1)
        for changes in ({'child':'b'*32},{'note':'Different authorization note'}):
            with self.subTest(changes=changes),self.assertRaises(CandidateError): self.enqueue(**changes)
        events = self.service.history.get(self.doc['id'])['events']
        self.assertEqual(sum(e['state']=='review_unknown_disposition' for e in events),1)

    def test_unknown_child_remains_blocked_and_cannot_be_reauthorized(self):
        self.enqueue()
        self.transport.side_effect = TimeoutError('Synthetic timeout; no actual provider')
        self.service.run(self.doc['id'])
        job = self.service.history.get(self.doc['id'])
        self.assertTrue(self.service._review_unresolved(job))
        self.assertFalse(self.service.review_availability(self.doc['id'])['can_recheck'])
        child = next(e['payload']['request_id'] for e in job['events'] if e['state']=='review_request')
        self.assertEqual(self.client.calls.lookup(child)['receipt']['state'],'unknown')
        self.assertEqual(self.enqueue()['id'],job['id'])  # same action only returns retained state
        self.assertEqual(self.transport.call_count,1)
        options = self.service.supplier_check_recoveries(self.doc['id'])
        self.assertFalse(next(item for item in options if item['original_request_id']==child)['can_recover'])
        with self.assertRaises(CandidateError): self.enqueue(child='b'*32,original=child)
        self.assertEqual(self.transport.call_count,1)

    def test_other_unknown_or_proposal_unknown_cannot_be_disposed(self):
        key = 'd'*32
        self.client.calls.reserve(key,b'synthetic other unknown proposal')
        self.event('proposal_request',{'request_id':key})
        with self.assertRaises(CandidateError): self.enqueue()
        with self.assertRaises(CandidateError): self.enqueue(original=key)
        self.assertTrue(self.service._review_unresolved(self.service.history.get(self.doc['id'])))
        self.transport.assert_not_called()

    def test_wrong_task_or_non_latest_original_review_is_rejected(self):
        with self.assertRaises(CandidateError): self.enqueue(original='c'*32)
        key = 'd'*32
        self.client.calls.reserve(key,b'synthetic later unknown review')
        self.event('candidate_review_request',{'request_id':key})
        with self.assertRaises(CandidateError): self.enqueue()
        self.transport.assert_not_called()

    def test_new_full_draft_cannot_replace_the_original_proposal_binding(self):
        key = 'd'*32
        self.transport.side_effect = lambda *args:(200,response(self.f.value))
        self.service.history.reserve_proposal(self.doc['id'],key,'synthetic_new_draft')
        value = self.client.complete_json(key,[{'role':'user','content':'Synthetic new JSON draft'}])
        self.event('model_proposal',dict(request_id=key,proposal_sha256=value['receipt']['output_sha256'],
            condition_sha256=self.before['condition_sha256']))
        before = self.transport.call_count
        with self.assertRaises(CandidateError): self.enqueue()
        self.assertEqual(self.transport.call_count,before)

    def test_pending_authorization_after_local_crash_only_allows_its_original_child(self):
        with patch.object(self.service,'enqueue_review',side_effect=CandidateError('Synthetic local interruption')):
            with self.assertRaises(CandidateError): self.enqueue()
        self.assertFalse(self.service._review_unresolved(self.service.history.get(self.doc['id'])))
        with self.assertRaises(CandidateError):
            self.service.enqueue_review(self.doc['id'],self.doc['revision'],request_id='b'*32)
        self.enqueue(); self.service.run(self.doc['id'])
        self.assertEqual(self.transport.call_count,1)

    def test_three_existing_review_rounds_are_shared_and_never_reset(self):
        valid = self.valid_review
        def blocked(body,*args):
            _,raw = valid(body,*args)
            value = json.loads(json.loads(raw)['choices'][0]['message']['content'])
            value['issues'] = ['Synthetic concrete blocker']
            return 200,response(value)
        self.transport.side_effect = blocked
        self.enqueue(); self.service.run(self.doc['id'])
        for key in ('b'*32,'c'*32):
            with patch.object(self.service.pool,'submit'):
                self.service.enqueue_review(self.doc['id'],self.doc['revision'],request_id=key)
            self.service.run(self.doc['id'])
        final = self.service.history.get(self.doc['id'])
        self.assertEqual(final['review_rounds'],dict(limit=3,used=3,remaining=0))
        self.assertEqual(final['proposal_rounds']['used'],5)
        with self.assertRaises(CandidateError):
            self.service.enqueue_review(self.doc['id'],self.doc['revision'],request_id='d'*32)
        self.assertEqual(self.transport.call_count,3)

    def test_resource_digest_and_disposition_proof_tampering_fail_closed(self):
        self.enqueue()
        job = self.service.history.get(self.doc['id'])
        disposition = next(e['payload'] for e in job['events'] if e['state']=='review_unknown_disposition')
        altered = deepcopy(disposition); altered['original_request_sha256'] = 'f'*64
        self.event('review_unknown_disposition',altered)
        with self.assertRaises(CandidateError): self.service._review_unresolved(self.service.history.get(self.doc['id']))
        self.transport.assert_not_called()

