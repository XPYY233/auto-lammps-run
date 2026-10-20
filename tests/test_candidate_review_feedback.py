"""Task-bound complete review feedback, synthetic providers and no physics."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from auto_lammps.agent_candidates import CandidateError, PlanIterationLimit, validate_review_feedback
from auto_lammps.manifest import canonical, sha256
from auto_lammps.research_workflow import ResearchWorkflow
from test_candidate_jobs import frozen_research
from test_deepseek import response
import test_supplier_checked_candidate_review as supplier


class CandidateReviewFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.f = supplier.SupplierCheckedCandidateReviewTests()
        self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.service, self.client = self.f.service, self.f.client
        self.doc, self.tasks = self.f.doc, self.f.tasks
        self.issue = 'Synthetic complete blocking feedback. ' + 'Retain this exact review text. ' * 30 + 'END_OF_FULL_REVIEW'
        def reject(body,*args):
            _,raw = self.f.valid_review(body,*args)
            report = json.loads(json.loads(raw)['choices'][0]['message']['content'])
            report['issues'] = [self.issue]
            return 200,response(report)
        self.f.transport.side_effect = reject
        self.f.enqueue(); self.service.run(self.doc['id'])
        self.before = self.service.history.get(self.doc['id'])
        self.assertEqual(self.before['state'],'failed',self.before['result'])
        self.assertEqual(self.before['proposal_rounds']['used'],5)
        self.previous = self.service.previous_proposal(self.doc['id'])
        self.feedback = self.service.latest_review_feedback(self.doc['id'],self.previous)
        self.old_unknown = deepcopy(self.client.calls.lookup(self.f.original))
        self.f.transport.reset_mock()

    def test_only_completed_exact_draft_feedback_is_exposed_in_full(self):
        self.assertEqual(self.feedback['report']['issues'],[self.issue])
        self.assertNotIn('END_OF_FULL_REVIEW',self.before['result']['detail'])
        self.assertEqual(self.feedback['proposal_sha256'],self.previous['receipt']['output_sha256'])
        actual = self.client.calls.lookup(self.feedback['review_request_id'])
        self.assertEqual(self.feedback['report'],actual['receipt']['structured_output'])
        self.assertIsNone(self.feedback['raw_issues'])
        self.assertEqual(self.feedback['review_output_sha256'],sha256(canonical(self.feedback['report'])))
        self.assertEqual(self.service.plan_review(self.doc['id'])['latest_review_feedback'],self.feedback)
        self.assertEqual(validate_review_feedback(self.feedback,self.previous,self.before['condition_sha256']),self.feedback)
        self.f.transport.assert_not_called()

    def test_existing_issue_alias_normalization_preserves_the_raw_receipt_binding(self):
        def wrapped(body,*args):
            _,raw = self.f.valid_review(body,*args)
            report = json.loads(json.loads(raw)['choices'][0]['message']['content'])
            report['issues'] = [{'error':self.issue}]
            return 200,response(report)
        self.f.transport.side_effect = wrapped
        with patch.object(self.service.pool,'submit'):
            self.service.enqueue_review(self.doc['id'],self.doc['revision'],request_id='b'*32)
        self.service.run(self.doc['id'])
        feedback = self.service.latest_review_feedback(self.doc['id'])
        self.assertEqual(feedback['report']['issues'],[self.issue])
        self.assertEqual(feedback['raw_issues'],[{'error':self.issue}])
        raw_report = {**feedback['report'],'issues':feedback['raw_issues']}
        self.assertEqual(feedback['review_output_sha256'],sha256(canonical(raw_report)))
        self.assertNotEqual(feedback['review_output_sha256'],sha256(canonical(feedback['report'])))
        self.assertEqual(validate_review_feedback(feedback,self.previous,self.before['condition_sha256']),feedback)

    def test_reprepare_keeps_full_review_full_draft_failure_budget_and_last_round(self):
        failure = dict(request_id='synthetic-cancelled',condition_sha256=self.before['condition_sha256'],
            scheduler_state='cancelled',logs=[{'tail':'Synthetic accounted cancellation.'}])
        self.service.failure_context_provider = Mock(return_value=failure)
        self.service.resources_provider = Mock(return_value=dict(remaining_core_seconds=506016,
            approval_sha256='e'*64,gpu_allowed=False))
        diagnosis = dict(summary='Synthetic diagnosis.',evidence=['Synthetic accounted cancellation.'],
            cause='Synthetic evidence only.',repair='Retain the complete synthetic scope.',
            proposed_lesson='Unverified synthetic lesson.')
        revised = {**deepcopy(self.f.f.value),'summary':'Complete synthetic sixth draft.'}
        def respond(body,*args):
            payload = json.loads(body); source = json.loads(payload['messages'][1]['content'])
            if 'failure' in source:
                self.assertEqual(source['failure']['previous_review_feedback'],self.feedback)
                self.assertEqual(source['failure']['execution_budget']['remaining_core_seconds'],506016)
                return 200,response(diagnosis)
            if 'requirement_references' in source:
                return self.f.valid_review(body,*args)
            self.assertEqual(source['previous_review_feedback'],self.feedback)
            self.assertEqual(source['execution_budget']['remaining_core_seconds'],506016)
            self.assertIn({'role':'assistant','content':canonical(self.previous['value']).decode()},payload['messages'])
            self.assertEqual(source['proposal_round_budget']['remaining'],1)
            return 200,response(revised)
        self.f.transport.side_effect = respond
        execution = SimpleNamespace(tasks=self.tasks,controller=SimpleNamespace(snapshots=self.service.snapshots),
            config_sha256='a'*64,evaluation_for=lambda _: 'synthetic-existing-evaluation',enrollment=None)
        flow = ResearchWorkflow(self.service,execution); self.addCleanup(flow.close)
        with patch.object(flow,'availability',return_value={'enabled':True}),patch.object(self.service.pool,'submit'):
            flow.enqueue(self.doc['id'],self.doc['revision'])
            workflow_id = flow.get(self.doc['id'])['id']
            with self.tasks.transaction() as db:
                flow._event(db,workflow_id,'attention','Synthetic retained failure')
            # This is the same enqueue used by the ordinary reprepare button.
            flow.enqueue(self.doc['id'],self.doc['revision'])
        with patch('subprocess.Popen',side_effect=AssertionError('No physical engine or scheduler')):
            self.service.run(self.doc['id'])
        final = self.service.history.get(self.doc['id'])
        self.assertEqual(final['state'],'prepared',final['result'])
        self.assertEqual(final['proposal_rounds']['used'],6)
        self.assertEqual(final['proposal_rounds']['remaining'],0)
        self.assertEqual(final['review_rounds'],self.before['review_rounds'])
        self.assertEqual(final['events'][:len(self.before['events'])],self.before['events'])
        self.assertEqual(self.client.calls.lookup(self.f.original),self.old_unknown)
        self.assertEqual(self.f.transport.call_count,3)  # diagnosis, one revision, static review
        kinds = [e['payload']['kind'] for e in final['events'] if e['state']=='proposal_request']
        self.assertEqual(kinds[-1],'execution_failure_repair')
        generation = json.loads(self.service.file(self.doc['id'],'generation.json'))
        self.assertEqual(generation['input']['review_feedback_sha256'],sha256(canonical(self.feedback)))
        self.assertEqual(generation['proposal'],revised)
        self.assertEqual(generation['failure_recovery']['validation_status'],'proposed_not_verified')
        self.assertEqual(generation['input']['execution_budget']['remaining_core_seconds'],506016)
        self.assertEqual(failure['logs'],[{'tail':'Synthetic accounted cancellation.'}])
        self.assertNotIn('previous_review_feedback',failure)
        with self.assertRaises(PlanIterationLimit):
            self.service.enqueue(self.doc['id'],self.doc['revision'],answers='No seventh proposal.')
        self.assertEqual(self.f.transport.call_count,3)

    def test_unknown_latest_review_never_falls_back_to_old_completed_feedback(self):
        key = 'd'*32
        self.client.calls.reserve(key,b'Synthetic unknown newer review')
        self.f.event('candidate_review_request',{'request_id':key})
        with self.assertRaises(CandidateError): self.service.latest_review_feedback(self.doc['id'])
        self.assertIsNone(self.service.plan_review(self.doc['id'])['latest_review_feedback'])
        self.assertEqual(self.client.calls.lookup(self.f.original),self.old_unknown)
        self.f.transport.assert_not_called()

    def test_other_task_or_newer_original_draft_never_inherits_old_feedback(self):
        other = frozen_research(self.tasks)
        self.assertIsNone(self.service.latest_review_feedback(other['id'],self.previous))
        altered = deepcopy(self.previous)
        altered['request_id'] = 'd'*32
        self.assertIsNone(self.service.latest_review_feedback(self.doc['id'],altered))
        for change in ({'task_id':other['id']},{'condition_sha256':'a'*64},
                       {'proposal_sha256':'b'*64},{'review_output_sha256':'c'*64}):
            changed = {**self.feedback,**change}
            if 'task_id' in change:
                from auto_lammps.agent_candidates import generate_research_candidate
                with self.assertRaises(CandidateError):
                    generate_research_candidate(self.client,self.tasks,self.doc['id'],self.doc['revision'],
                        self.f.f.adapter,resources=self.f.f.resources,store=self.f.f.root/'unused',
                        previous_proposal=self.previous,review_feedback=changed)
            else:
                with self.assertRaises(CandidateError):
                    validate_review_feedback(changed,self.previous,self.before['condition_sha256'])
        self.f.transport.assert_not_called()

    def test_changed_report_or_receipt_proof_is_rejected_before_any_model_call(self):
        for field in ('report','receipt','proposal_sha256','artifact_sha256'):
            changed = deepcopy(self.before)
            event = next(e for e in reversed(changed['events']) if e['state']=='model_review')
            if field == 'report': event['payload']['issues'] = ['Fabricated feedback']
            elif field == 'receipt': event['payload']['receipt']['output_sha256'] = 'a'*64
            elif field == 'proposal_sha256': event['payload'][field] = 'b'*64
            else: event['payload'][field]['rendered_script'] = 'invalid'
            with self.subTest(field=field),patch.object(self.service.history,'get',return_value=changed):
                with self.assertRaises(CandidateError): self.service.latest_review_feedback(self.doc['id'],self.previous)
        self.f.transport.assert_not_called()

    def test_newer_successful_review_does_not_reuse_an_older_rejection(self):
        self.f.transport.side_effect = self.f.valid_review
        with patch.object(self.service.pool,'submit'):
            self.service.enqueue_review(self.doc['id'],self.doc['revision'],request_id='b'*32)
        self.service.run(self.doc['id'])
        final = self.service.history.get(self.doc['id'])
        self.assertEqual(final['state'],'prepared',final['result'])
        self.assertIsNone(self.service.latest_review_feedback(self.doc['id']))
        self.assertEqual(final['proposal_rounds']['used'],5)
