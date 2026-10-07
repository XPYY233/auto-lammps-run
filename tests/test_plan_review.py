"""Ground reviews in supplied artifacts; mocks never evaluate physics."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock

from auto_lammps.plan_review import requirements, validate_coverage, ReviewEvidenceError


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


class CandidateReviewGroundingTests(unittest.TestCase):
    from test_agent_candidates import AgentCandidateTests
    setUp = AgentCandidateTests.setUp

    def generate(self, review, directory):
        from auto_lammps.agent_candidates import generate_candidate_draft
        from auto_lammps.deepseek import DeepSeekClient, ModelCalls
        from test_deepseek import response
        proposal = deepcopy(self.value)
        proposal['analysis']['files'] = ['result.dat']
        proposal['analysis']['plan']['tables'][0]['file'] = 'result.dat'
        proposal['analysis']['plan']['operations'][0]['file'] = 'result.dat'
        proposal['workflow'] = 'run 0\nemit_table result.dat "0 $(pe)"'
        calls = ModelCalls(self.root / f'{directory}.sqlite', self.calls.config, max_requests=3)
        transport = Mock(side_effect=[(200, response(proposal)), (200, response(review))])
        client = DeepSeekClient(calls, transport=transport, key_reader=lambda: 'synthetic-key')
        saved = []
        result = generate_candidate_draft(client, self.adapter,
            task_text='Synthetic complete request.\nRetain all synthetic stages.', units='metal',
            resources=self.resources, store=self.root / directory,
            require_analysis_plan=True, review_plan=True, on_proposal=saved.append)
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

    def test_empty_or_fabricated_success_never_freezes_or_regenerates_the_proposal(self):
        from auto_lammps.agent_candidates import ReviewContractError
        for index, mutation in enumerate(('empty', 'fabricated')):
            review = self.review()
            if mutation == 'empty': review['coverage'] = []
            else: review['coverage'][0]['evidence'][0]['quote'] = 'fictional tool output'
            # Two responses only: a hidden regeneration would exhaust the mock.
            with self.subTest(mutation=mutation), self.assertRaises(ReviewContractError):
                self.generate(review, f'blocked-{index}')
            self.assertFalse((self.root / f'blocked-{index}').exists())


if __name__ == '__main__':
    unittest.main()
