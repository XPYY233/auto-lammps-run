"""Ground reviews in supplied artifacts; mocks never evaluate physics."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock

from auto_lammps.plan_review import (
    requirements, normalized_review_issues, validate_coverage, validate_report, ReviewEvidenceError,
)


class RequirementEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.required = requirements('Use the complete synthetic structure.\n\nRetain all states.\n  Include units.')
        self.sources = {'rendered_script': 'units metal\nrun 0', 'analysis_plan': '{"unit":"eV"}'}
        self.coverage = [{'requirement': r['id'], 'evidence': [
            {'source': 'rendered_script', 'quote': 'units metal'}]} for r in self.required]

    def test_every_nonempty_line_is_retained_stably(self):
        self.assertEqual([r['text'] for r in self.required], [
            'Use the complete synthetic structure.', 'Retain all states.', '  Include units.'])
        self.assertEqual(self.required, requirements('Use the complete synthetic structure.\n\nRetain all states.\n  Include units.'))
        duplicate = requirements('identical\nidentical')
        self.assertNotEqual(duplicate[0]['id'], duplicate[1]['id'])
        for value in ('', ' \n', None, 'a' * 24001, '\n'.join(['a'] * 129)):
            with self.subTest(value=str(value)[:20]), self.assertRaises(ReviewEvidenceError):
                requirements(value)

    def test_actual_quotes_cover_complete_inventory_without_certifying_physics(self):
        self.assertEqual(validate_coverage(self.coverage, self.required, self.sources,
            blocking_issues=[]), self.coverage)

    def test_latest_guidance_and_answers_cannot_disappear_from_the_inventory(self):
        note = 'a' * 1900 + '\nRetain the final synthetic stage.'
        required = requirements('Frozen conditions.', guidance=['Earlier direction.', note],
                                answers='Keep all synthetic samples.')
        self.assertEqual(len(required), 4)
        self.assertEqual(required[2]['text'], note)
        self.assertEqual(required[2]['origin'], 'user_guidance')
        old_only = [{'requirement': required[0]['id'], 'evidence': [
            {'source': 'rendered_script', 'quote': 'run 0'}]}]
        with self.assertRaises(ReviewEvidenceError):
            validate_coverage(old_only, required, self.sources, blocking_issues=[])
        from auto_lammps.agent_candidates import candidate_messages
        messages = candidate_messages('Frozen conditions.', units='metal', resource_summaries=[],
            max_atoms=100, guidance=[note], answers='Keep all synthetic samples.')
        self.assertNotIn(note, messages[0]['content'])
        self.assertEqual(json.loads(messages[1]['content'])['guidance'], [note])

    def test_missing_duplicate_unknown_or_invented_evidence_is_rejected(self):
        cases = [self.coverage[:-1], self.coverage + [deepcopy(self.coverage[0])]]
        duplicate = deepcopy(self.coverage); duplicate[-1] = deepcopy(duplicate[0]); cases.append(duplicate)
        unknown = deepcopy(self.coverage); unknown[0]['requirement'] = 'invented'; cases.append(unknown)
        for reference in ({'source': 'absent', 'quote': 'units metal'},
                          {'source': 'rendered_script', 'quote': 'run 1000'},
                          {'source': 'rendered_script', 'quote': '   '},
                          {'source': 'rendered_script', 'quote': 'units metal', 'passed': True}):
            altered = deepcopy(self.coverage); altered[0]['evidence'] = [reference]; cases.append(altered)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ReviewEvidenceError):
                validate_coverage(value, self.required, self.sources, blocking_issues=[])

    def test_missing_implementation_requires_explicit_blocking_issue(self):
        self.coverage[0]['evidence'] = []
        with self.assertRaises(ReviewEvidenceError):
            validate_coverage(self.coverage, self.required, self.sources, blocking_issues=[])
        validate_coverage(self.coverage, self.required, self.sources,
            blocking_issues=['The complete structure was not implemented.'])


class ReportContractTests(unittest.TestCase):
    def setUp(self):
        self.required = requirements('Retain every synthetic stage.\nInclude output units.')
        self.sources = {'rendered_script': 'units metal\nrun 0'}
        self.report = {'issues': [], 'summary': 'Static consistency only.', 'coverage': [
            {'requirement': item['id'], 'evidence': [
                {'source': 'rendered_script', 'quote': 'units metal'}]}
            for item in self.required]}

    def test_complete_report_retains_fields_quotes_and_issue_text_without_mutation(self):
        self.report['issues'] = ['  actual blocking failure  ',
                                 {'issue': 'second failure'}, {'error': 'third failure'}]
        original = deepcopy(self.report)
        result = validate_report(self.report, self.required, self.sources)
        self.assertEqual(result, {**original, 'issues': [
            '  actual blocking failure  ', 'second failure', 'third failure']})
        self.assertEqual(self.report, original)
        self.assertEqual(set(result), {'issues', 'coverage', 'summary'})

    def test_twelve_issues_are_preserved_including_last_blocker(self):
        self.report['issues'] = [f'Blocking failure {index}' for index in range(12)]
        result = validate_report(self.report, self.required, self.sources)
        self.assertEqual(result['issues'], self.report['issues'])
        self.assertEqual(result['issues'][-1], 'Blocking failure 11')

    def test_overlong_report_requests_consolidation_with_actual_count_and_preserves_original(self):
        for count in (13, 14):
            with self.subTest(count=count):
                self.report['issues'] = [{'issue': f'Blocking failure {index}'} for index in range(count)]
                original = deepcopy(self.report)
                with self.assertRaises(ReviewEvidenceError) as caught:
                    validate_report(self.report, self.required, self.sources)
                message = str(caught.exception)
                self.assertIn(f'{count} issues', message)
                self.assertIn('maximum is 12', message)
                self.assertIn('Merge duplicate issues', message)
                self.assertIn('retain every genuine blocking error without truncation', message)
                self.assertEqual(self.report, original)

    def test_exact_report_keys_and_string_summary_remain_required(self):
        invalid = [None, [], {'issues': []}, {**self.report, 'approved': True},
                   {**self.report, 'summary': None}, {**self.report, 'summary': {'text': 'ok'}}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ReviewEvidenceError):
                validate_report(value, self.required, self.sources)
        # The existing contract requires a string, without inventing new semantics.
        validate_report({**self.report, 'summary': ''}, self.required, self.sources)

    def test_only_existing_single_string_issue_aliases_are_accepted(self):
        for issues in (None, 'failure', [''], ['   '], [None], [42], [{'error': None}],
                       [{'message': 'failure'}], [{'issue': 'failure', 'extra': True}],
                       [{'issue': 'failure', 'error': 'failure'}]):
            with self.subTest(issues=issues), self.assertRaises(ReviewEvidenceError):
                validate_report({**self.report, 'issues': issues}, self.required, self.sources)
        invalid_alias = [{'issue': 'failure', 'extra': True}]
        self.assertEqual(normalized_review_issues(invalid_alias), invalid_alias)
        self.assertEqual(normalized_review_issues('failure'), 'failure')

    def test_report_still_requires_complete_literal_coverage(self):
        missing = deepcopy(self.report)
        missing['coverage'] = missing['coverage'][:-1]
        fabricated = deepcopy(self.report)
        fabricated['coverage'][0]['evidence'][0]['quote'] = 'run 1000'
        for value in (missing, fabricated):
            with self.subTest(value=value), self.assertRaises(ReviewEvidenceError):
                validate_report(value, self.required, self.sources)

    def test_no_quotes_require_a_preserved_explicit_blocking_issue(self):
        self.report['coverage'][0]['evidence'] = []
        with self.assertRaises(ReviewEvidenceError):
            validate_report(self.report, self.required, self.sources)
        self.report['issues'] = [{'issue': 'The first synthetic stage is missing.'}]
        result = validate_report(self.report, self.required, self.sources)
        self.assertEqual(result['issues'], ['The first synthetic stage is missing.'])
        self.assertEqual(result['coverage'], self.report['coverage'])


class CandidateReviewGroundingTests(unittest.TestCase):
    from test_agent_candidates import AgentCandidateTests
    setUp = AgentCandidateTests.setUp

    def generate(self, review, directory, *, proposal_budget=None):
        from auto_lammps.agent_candidates import generate_candidate_draft
        from auto_lammps.deepseek import DeepSeekClient, ModelCalls
        from test_deepseek import response
        proposal = deepcopy(self.value)
        proposal['analysis']['files'] = ['result.dat']
        proposal['analysis']['plan']['tables'][0]['file'] = 'result.dat'
        proposal['analysis']['plan']['operations'][0]['file'] = 'result.dat'
        proposal['workflow'] = 'run 0\nemit_table result.dat "0 $(pe)"'
        calls = ModelCalls(self.root / f'{directory}.sqlite', self.calls.config, max_requests=3)
        reports = review if isinstance(review, list) else [review]
        transport = Mock(side_effect=[(200, response(proposal))] + [
            item if isinstance(item, Exception) else (200, response(item)) for item in reports])
        client = DeepSeekClient(calls, transport=transport, key_reader=lambda: 'synthetic-key', thinking=True)
        saved = []
        self.review_calls, self.review_transport, self.review_saved = calls, transport, saved
        self.review_proposal = deepcopy(proposal)
        self.proposal_requests = []
        result = generate_candidate_draft(client, self.adapter,
            task_text='Synthetic complete request.\nRetain all synthetic stages.', units='metal',
            resources=self.resources, store=self.root / directory,
            require_analysis_plan=True, review_plan=True, on_proposal=saved.append,
            before_proposal_request=lambda request_id, kind: self.proposal_requests.append((request_id, kind)),
            proposal_round_budget=proposal_budget)
        return result, transport, saved

    def review(self):
        return {'issues': [], 'summary': 'Static consistency only.', 'coverage': [
            {'requirement': r['id'], 'evidence': [{'source': 'rendered_script', 'quote': 'run 0'}]}
            for r in requirements('Synthetic complete request.\nRetain all synthetic stages.')]}

    def test_actual_requirement_evidence_is_preserved_in_the_frozen_plan(self):
        result, transport, saved = self.generate(self.review(), 'grounded')
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(len(saved), 1)
        self.assertEqual(result['generation']['plan_reviews'][0]['coverage'], self.review()['coverage'])
        self.assertFalse(result['generation']['scientific_conditions_verified'])
        actual = json.loads(json.loads(transport.call_args.args[0])['messages'][1]['content'])
        self.assertEqual(len(actual['requirement_references']), 2)
        self.assertIn('run 0', actual['rendered_script'])
        requests = [json.loads(call.args[0]) for call in transport.call_args_list]
        self.assertEqual(requests[0]['thinking'], {'type': 'enabled'})
        self.assertEqual(requests[1]['thinking'], {'type': 'disabled'})
        self.assertNotIn('workflow', actual['proposal'])
        self.assertNotIn('plan', actual['analysis_description'])
        self.assertIn('analysis_plan', actual)
        self.assertIn('analysis_adapter', actual)
        self.assertIn('scientific_adapter', actual)

    def test_empty_or_fabricated_success_never_freezes_or_regenerates_the_proposal(self):
        from auto_lammps.agent_candidates import ReviewContractError
        for index, mutation in enumerate(('empty', 'fabricated')):
            review = self.review()
            if mutation == 'empty': review['coverage'] = []
            else: review['coverage'][0]['evidence'][0]['quote'] = 'fictional tool output'
            # One proposal plus the sole review-report repair; never a new proposal.
            with self.subTest(mutation=mutation), self.assertRaisesRegex(
                    ReviewContractError, 'one review-report repair exhausted'):
                self.generate([review, deepcopy(review)], f'blocked-{index}')
            self.assertFalse((self.root / f'blocked-{index}').exists())
            self.assertEqual(self.review_transport.call_count, 3)
            self.assertEqual(len(self.review_saved), 1)
            self.assertEqual([kind for _, kind in self.proposal_requests], ['initial'])
            history = self.review_calls.history()
            self.assertEqual(len(history), 3)
            self.assertTrue(all(item['receipt']['state'] == 'completed' for item in history))
            self.assertEqual(sum(item['receipt']['usage']['total_tokens'] for item in history), 90)

    def test_one_report_repair_keeps_same_proposal_and_all_accounted_receipts(self):
        from auto_lammps.manifest import canonical, sha256
        invalid = self.review()
        invalid['issues'] = [f'Nonblocking synthetic caveat {index}' for index in range(14)]
        valid = self.review()
        original = deepcopy(invalid)
        result, transport, saved = self.generate([invalid, valid], 'repaired-report')
        self.assertEqual(transport.call_count, 3)
        self.assertEqual(len(saved), 1)
        self.assertEqual([kind for _, kind in self.proposal_requests], ['initial'])
        self.assertEqual(result['generation']['proposal'], self.review_proposal)
        self.assertEqual(result['generation']['plan_reviews'][0]['coverage'], valid['coverage'])
        receipts = result['generation']['model_receipts']
        self.assertEqual(len(receipts), 3)
        self.assertEqual(len({receipt['request_sha256'] for receipt in receipts}), 3)
        history = self.review_calls.history()
        self.assertEqual(self.review_calls.status()['used_requests'], 3)
        self.assertEqual(sum(item['receipt']['usage']['total_tokens'] for item in history), 90)
        self.assertIn(original, [item['receipt']['structured_output'] for item in history])
        self.assertEqual(invalid, original)
        requests = [json.loads(call.args[0]) for call in transport.call_args_list]
        self.assertEqual(requests[0]['thinking'], {'type': 'enabled'})
        self.assertTrue(all(request['thinking'] == {'type': 'disabled'} for request in requests[1:]))
        self.assertEqual(requests[1]['messages'], requests[2]['messages'][:2])
        self.assertEqual(json.loads(requests[2]['messages'][-2]['content']), original)
        feedback = json.loads(requests[2]['messages'][-1]['content'])
        self.assertIn('14 issues', feedback['review_report_error'])
        self.assertIn('SAME unchanged proposal', feedback['action'])
        self.assertEqual(result['generation']['plan_reviews'][0]['proposal_sha256'],
                         sha256(canonical(self.review_proposal)))
        self.assertFalse(result['execution_authorized'])

    def test_consolidated_report_does_not_remove_the_last_real_blocker_or_freeze(self):
        from auto_lammps.agent_candidates import PlanIterationLimit
        invalid = self.review()
        invalid['issues'] = ['Same synthetic blocker'] * 13 + ['Final synthetic blocker']
        repaired = self.review()
        repaired['issues'] = ['Same synthetic blocker', 'Final synthetic blocker']
        budget = {'limit': 3, 'used': 2, 'remaining': 1, 'historical_count_unknown': False}
        with self.assertRaisesRegex(PlanIterationLimit, 'Final synthetic blocker'):
            self.generate([invalid, repaired], 'still-blocked', proposal_budget=budget)
        self.assertEqual(self.review_transport.call_count, 3)
        self.assertEqual([kind for _, kind in self.proposal_requests], ['initial'])
        self.assertFalse((self.root / 'still-blocked').exists())
        history = self.review_calls.history()
        self.assertEqual(len(history), 3)
        self.assertEqual(sum(item['receipt']['usage']['total_tokens'] for item in history), 90)
        self.assertIn(repaired, [item['receipt']['structured_output'] for item in history])

    def test_unknown_review_transport_stops_without_format_repair(self):
        from auto_lammps.deepseek import ModelError
        with self.assertRaisesRegex(ModelError, 'model_transport_unknown'):
            self.generate([TimeoutError('synthetic timeout'), self.review()], 'unknown-review')
        self.assertEqual(self.review_transport.call_count, 2)
        self.assertEqual(len(self.review_saved), 1)
        self.assertEqual([kind for _, kind in self.proposal_requests], ['initial'])
        self.assertFalse((self.root / 'unknown-review').exists())
        history = self.review_calls.history()
        self.assertEqual(len(history), 2)
        states = {item['receipt']['state'] for item in history}
        self.assertEqual(states, {'completed', 'unknown'})
        unknown = next(item['receipt'] for item in history if item['receipt']['state'] == 'unknown')
        self.assertIsNone(unknown['usage'])
        self.assertIsNone(unknown['output_sha256'])
        self.assertEqual(unknown['transport_attempts'], 1)


if __name__ == '__main__':
    unittest.main()
