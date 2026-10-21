"""Task-bound CPU limits in generation, mandatory adapters and immutable drafts."""
from copy import deepcopy
from dataclasses import asdict
import json
import unittest
from unittest.mock import Mock, patch

from auto_lammps.agent_candidates import CandidateError, generate_candidate_draft, validate_execution_budget
from auto_lammps.candidate_jobs import CandidateService
from auto_lammps.deepseek import DeepSeekClient, ModelCalls
from auto_lammps.ledger import Resources
from auto_lammps.manifest import canonical, sha256
from auto_lammps.tasks import TaskStore
import test_agent_candidates as fixtures
from test_candidate_jobs import frozen_research
from test_deepseek import response


class CandidateExecutionBudgetTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.AgentCandidateTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.f.value['analysis']['files'] = ['result.dat']
        self.f.value['analysis']['plan']['tables'][0]['file'] = 'result.dat'
        self.f.value['analysis']['plan']['operations'][0]['file'] = 'result.dat'
        self.f.value['workflow'] = 'run 0\nemit_table result.dat "0 $(pe)"'
        self.tasks = TaskStore(self.f.root/'budget-tasks.sqlite')
        self.doc = frozen_research(self.tasks)
        self.base = Resources(32,604800,8<<30,1<<30)
        self.limit = {'remaining_core_seconds':506016,'approval_sha256':'e'*64,'gpu_allowed':False}
        self.provider = Mock(return_value=self.limit)
        calls = ModelCalls(self.f.root/'budget-models.sqlite',self.f.calls.config,max_requests=20)
        self.transport = Mock(side_effect=self.respond)
        self.client = DeepSeekClient(calls,transport=self.transport,key_reader=lambda:'synthetic-key')
        self.service = self.make_service()

    def make_service(self, *, provider=True, review=False):
        service = CandidateService(self.tasks,self.client,self.f.adapter,resources=self.base,
            snapshots=self.f.root/'budget-snapshots',review_plan=review,
            resources_provider=self.provider if provider else None)
        self.addCleanup(lambda:service.close(wait=True))
        return service

    def respond(self, body, *args):
        payload = json.loads(body)
        source = json.loads(payload['messages'][1]['content'])
        if 'requirement_references' in source:
            return 200,response({'issues':[],'summary':'Synthetic static consistency only.',
                'coverage':[{'requirement':item['id'],'evidence':[
                    {'source':'rendered_script','quote':'run 0'}]} for item in source['requirement_references']]})
        return 200,response(self.f.value)

    def run_job(self, service=None):
        service = service or self.service
        with patch.object(service.pool,'submit'):
            service.enqueue(self.doc['id'],self.doc['revision'])
        service.run(self.doc['id'])
        return service.history.get(self.doc['id'])

    def event(self, state, payload):
        job = self.service.history.get(self.doc['id'])
        with self.tasks.transaction() as db:
            self.service.history._event(db,job['id'],state,payload)

    def test_approved_140_56_hours_rounds_down_without_changing_cores_or_defaults(self):
        resources,budget = self.service._new_execution_inputs(self.doc['id'])
        self.assertEqual(resources,Resources(32,15780,8<<30,1<<30))
        self.assertEqual(budget['reserved_core_seconds'],504960)
        self.assertEqual(budget['remaining_core_seconds'],506016)
        self.assertEqual(self.service.resources,self.base)
        self.assertEqual(self.limit,{'remaining_core_seconds':506016,'approval_sha256':'e'*64,'gpu_allowed':False})

    def test_model_adapter_snapshot_and_history_share_the_same_actual_resources(self):
        from auto_lammps.scientific_adapters import prepare_stage_messages
        self.service = self.make_service(review=True)
        with patch('auto_lammps.scientific_adapters.prepare_stage_messages',wraps=prepare_stage_messages) as stages:
            job = self.run_job()
        self.assertEqual(job['state'],'prepared',job['result'])
        self.assertEqual(self.transport.call_count,2)
        payloads = [json.loads(call.args[0]) for call in self.transport.call_args_list]
        model_input = json.loads(payloads[0]['messages'][1]['content'])
        effective,budget = self.service._new_execution_inputs(self.doc['id'])
        self.assertEqual(model_input['execution_resources'],asdict(effective))
        self.assertEqual(model_input['execution_budget'],budget)
        review_input = json.loads(payloads[1]['messages'][1]['content'])
        review_metadata = json.loads(review_input['resource_metadata'])
        self.assertEqual(review_metadata['execution_resources'],asdict(effective))
        self.assertEqual(review_metadata['execution_budget'],budget)
        for call in stages.call_args_list:
            if call.args[0] in ('candidate_proposal','candidate_review'):
                self.assertEqual(call.args[2]['execution_budget'],budget)
                self.assertEqual(call.args[2]['execution_resources'],asdict(effective))
        generation = json.loads(self.service.file(self.doc['id'],'generation.json'))
        self.assertEqual(generation['input']['resources'],asdict(effective))
        self.assertEqual(generation['input']['execution_budget'],budget)
        from auto_lammps.manifest import Snapshot
        manifest = Snapshot(self.service.snapshots/job['result']['snapshot_sha256'],
                            job['result']['snapshot_sha256']).verify()
        self.assertEqual(manifest['resources'],asdict(effective))
        link = [e['payload'] for e in job['events'] if e['state']=='model_proposal'][-1]
        self.assertEqual(link['resources_sha256'],sha256(canonical(asdict(effective))))
        self.assertEqual(link['execution_budget_sha256'],sha256(canonical(budget)))
        self.assertNotIn('formal_b_budget',canonical(model_input).decode())
        self.assertNotIn('REFERENCE_SENTINEL_NOT_FOR_AGENT',canonical(payloads).decode())

    def test_default_provider_retains_legacy_model_input_and_manifest(self):
        service = self.make_service(provider=False)
        job = self.run_job(service)
        self.assertEqual(job['state'],'prepared',job['result'])
        source = json.loads(json.loads(self.transport.call_args.args[0])['messages'][1]['content'])
        self.assertNotIn('execution_budget',source)
        self.assertNotIn('execution_resources',source)
        self.assertEqual(service._new_execution_inputs(self.doc['id']),(self.base,None))

    def test_invalid_empty_gpu_or_answer_bearing_provider_fails_before_model(self):
        cases = [None,{}, {**self.limit,'remaining_core_seconds':0},
            {**self.limit,'remaining_core_seconds':True}, {**self.limit,'remaining_core_seconds':1919},
            {**self.limit,'gpu_allowed':True}, {**self.limit,'approval_sha256':'invalid'},
            {**self.limit,'formal_b_budget':{'reference_core_seconds':123}}]
        for value in cases[1:]:
            with self.subTest(value=value):
                self.provider.return_value = value
                with self.assertRaises(CandidateError): self.service._new_execution_inputs(self.doc['id'])
        self.assertEqual(self.transport.call_count,0)
        self.assertEqual(self.client.calls.status()['used_requests'],0)

    def test_same_draft_review_uses_saved_resources_even_if_current_budget_disappears(self):
        self.service = self.make_service(review=True)
        first = self.run_job()
        self.assertEqual(first['state'],'prepared',first['result'])
        old_events = deepcopy(first['events'])
        self.event('failed',{'error':'candidate_validation_failed','detail':'Synthetic later review needed'})
        self.provider.side_effect = AssertionError('Same-draft review must not read current resource allowance')
        self.assertTrue(self.service.review_availability(self.doc['id'])['can_recheck'])
        with patch.object(self.service.pool,'submit'):
            self.service.enqueue_review(self.doc['id'],self.doc['revision'],request_id='a'*32)
        self.service.run(self.doc['id'])
        final = self.service.history.get(self.doc['id'])
        self.assertEqual(final['state'],'prepared',final['result'])
        self.assertEqual(final['events'][:len(old_events)],old_events)
        self.assertEqual(final['proposal_rounds']['used'],1)
        generation = json.loads(self.service.file(self.doc['id'],'generation.json'))
        self.assertEqual(generation['input']['resources']['wall_seconds'],15780)
        self.assertEqual(generation['input']['execution_budget']['remaining_core_seconds'],506016)

    def test_new_budget_never_recovers_or_resends_an_unresolved_old_review(self):
        self.service = self.make_service(review=True)
        first = self.run_job()
        key = 'f'*32
        self.client.calls.reserve(key,b'synthetic already-reserved request')
        self.event('candidate_review_request',{'request_id':key})
        self.event('interrupted',{'message':'Synthetic unknown review'})
        before = self.transport.call_count
        self.provider.return_value = {**self.limit,'remaining_core_seconds':1000000}
        self.assertFalse(self.service.review_availability(self.doc['id'])['can_recheck'])
        with self.assertRaises(CandidateError):
            self.service.enqueue_review(self.doc['id'],self.doc['revision'],request_id='b'*32)
        self.assertEqual(self.transport.call_count,before)
        self.assertIsNone(self.client.calls.lookup(key)['receipt'])

    def test_legacy_draft_only_reuses_the_matching_original_resource_hash(self):
        link = {'request_id':'a'*32,'resources_sha256':sha256(canonical(asdict(self.base)))}
        job = {'events':[{'state':'model_proposal','payload':link}]}
        self.assertEqual(self.service._proposal_execution_inputs(job,'a'*32),(self.base,None))
        link['resources_sha256'] = 'f'*64
        with self.assertRaises(CandidateError): self.service._proposal_execution_inputs(job,'a'*32)

    def test_saved_resource_and_budget_tampering_is_rejected(self):
        resources,budget = self.service._new_execution_inputs(self.doc['id'])
        link = {'request_id':'a'*32,'resources':asdict(resources),
            'resources_sha256':sha256(canonical(asdict(resources))),
            'execution_budget':budget,'execution_budget_sha256':sha256(canonical(budget))}
        job = {'events':[{'state':'model_proposal','payload':link}]}
        self.assertEqual(self.service._proposal_execution_inputs(job,'a'*32),(resources,budget))
        for field in ('resources_sha256','execution_budget_sha256'):
            altered = deepcopy(job); altered['events'][0]['payload'][field] = 'f'*64
            with self.subTest(field=field),self.assertRaises(CandidateError):
                self.service._proposal_execution_inputs(altered,'a'*32)
        with self.assertRaises(CandidateError): validate_execution_budget(self.base,budget)

    def test_failure_iteration_uses_complete_old_draft_and_only_one_remaining_proposal_round(self):
        self.transport.side_effect = lambda *args:(200,response(self.f.value))
        previous = self.client.complete_json('c'*32,[{'role':'user','content':'Synthetic complete prior JSON draft.'}])
        self.transport.reset_mock()
        diagnosis = dict(summary='Synthetic cancelled execution diagnosis',evidence=['synthetic timeout'],
            cause='Synthetic timeout fixture',repair='Retain the full synthetic scope',
            proposed_lesson='Unverified synthetic suggestion')
        def recovering(body,*args):
            content = json.loads(json.loads(body)['messages'][1]['content'])
            return 200,response(diagnosis if 'failure' in content else self.f.value)
        self.transport.side_effect = recovering
        resources,budget = self.service._new_execution_inputs(self.doc['id'])
        requests = []
        evidence = {'condition_sha256':'d'*64,'request_id':'synthetic-cancelled',
            'logs':[{'tail':'synthetic timeout'}],
            'failed_execution_resources':asdict(self.base),
            'failed_execution_accounting':{'accounted':True,'actual_core_seconds':1234}}
        result = generate_candidate_draft(self.client,self.f.adapter,task_text='Synthetic complete scope.',
            units='metal',resources=resources,execution_budget=budget,store=self.f.root/'repair-snapshots',
            condition_record_sha256='d'*64,previous_proposal=previous,failure_context=evidence,
            require_analysis_plan=True,proposal_round_budget={'limit':6,'used':5,'remaining':1,
                'historical_count_unknown':False},
            before_proposal_request=lambda key,kind:requests.append((key,kind)))
        self.assertEqual([kind for _,kind in requests],['execution_failure_repair'])
        self.assertEqual(self.transport.call_count,2)  # diagnosis + one complete revision
        bodies = [json.loads(call.args[0]) for call in self.transport.call_args_list]
        failure = json.loads(bodies[0]['messages'][1]['content'])['failure']
        self.assertEqual(failure['current_planning_execution_budget'],budget)
        self.assertEqual(failure['current_planning_resources'],asdict(resources))
        self.assertNotIn('execution_budget',failure)
        self.assertEqual(failure['failed_execution_resources'],asdict(self.base))
        self.assertNotEqual(failure['failed_execution_resources']['wall_seconds'],resources.wall_seconds)
        self.assertEqual(failure['failed_execution_accounting']['actual_core_seconds'],1234)
        self.assertIn('never treat them as the failed run reservation',bodies[0]['messages'][0]['content'])
        self.assertIn('SIGTERM/cancelled alone does not establish',bodies[0]['messages'][0]['content'])
        self.assertIn('throughput from minimization',bodies[1]['messages'][-1]['content'])
        self.assertIn('does not require an additional benchmark',bodies[1]['messages'][0]['content'])
        revision = bodies[1]['messages']
        self.assertIn({'role':'assistant','content':canonical(previous['value']).decode()},revision)
        self.assertEqual(json.loads(revision[1]['content'])['execution_budget'],budget)
        self.assertEqual(evidence,{'condition_sha256':'d'*64,'request_id':'synthetic-cancelled',
            'logs':[{'tail':'synthetic timeout'}],
            'failed_execution_resources':asdict(self.base),
            'failed_execution_accounting':{'accounted':True,'actual_core_seconds':1234}})
        self.assertEqual(result['snapshot'].verify()['resources'],asdict(resources))
        from auto_lammps.authorization import candidate_check
        self.assertEqual(candidate_check(result['snapshot'],max_atoms=100000)['scientific_status'],'not_evaluated')

