"""Compile a synthetic bounded author recipe without changing runtime schema."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from auto_lammps.author_execution import AuthorExecutionError, _raw_plan, validate_bound_author_package
from auto_lammps.author_reference_plan import validate_author_plan
from auto_lammps.manifest import canonical, sha256
import test_author_execution_package as execution_fixture
import test_author_validation_subset as validation_fixture


class AuthorValidationExecutionTests(unittest.TestCase):
    def setUp(self):
        self.execution = execution_fixture.AuthorExecutionPackageTests(); self.execution.setUp()
        self.addCleanup(self.execution.doCleanups)
        self.validation = validation_fixture.AuthorValidationPlanTests(); self.validation.setUp()
        self.addCleanup(self.validation.doCleanups)
        self.original_keys = set(self.execution.compile()['package'])
        e = self.execution
        e.evidence = deepcopy(self.validation.evidence); e.raw = deepcopy(self.validation.fx.value)
        e.report = validate_author_plan(e.evidence, e.raw)
        e.files = [dict(row, binding_sha256='4' * 64, source_path=row['path'])
                   for row in e.evidence['author_source']['files']]

    def test_round_trip_restores_literal_selection_and_revalidates_complete_proof(self):
        e = self.execution; before = deepcopy(e.report)
        raw = _raw_plan(e.report)
        self.assertEqual(raw, e.raw)
        self.assertEqual(validate_author_plan(e.evidence, raw), e.report)
        self.assertEqual(e.report, before)
        self.assertNotIn('binding', raw['validation_selection'])
        for state in raw['validation_selection']['states']:
            self.assertEqual(set(state['citations'][0]), {'source_id', 'quote'})

    def test_package_pins_full_proof_and_original_parent_without_new_runtime_keys(self):
        e = self.execution
        with patch('subprocess.Popen') as popen, patch('subprocess.run') as run:
            compiled = e.compile()
        popen.assert_not_called(); run.assert_not_called()
        package = compiled['package']
        self.assertEqual(set(package), self.original_keys)
        self.assertEqual(package['plan_sha256'], sha256(canonical(e.report)))
        self.assertEqual(package['source_binding_sha256'], sha256(canonical(e.evidence)))
        self.assertEqual(package['reference_scope'], e.report['reference_scope'])
        self.assertEqual(package['reference_scope']['scope']['selected_ids'], ['first', 'second'])
        self.assertEqual([row['argv'][-1] for row in package['invocations']], ['7', '9'])
        self.assertFalse(compiled['execution_authorized']); self.assertFalse(package['B_input'])

    def test_existing_exact_runtime_schema_accepts_the_compiled_subset_metadata(self):
        e = self.execution; profile = e.runtime_profile(); package = e.compile()
        commands = e.commands(profile, package)
        self.assertEqual(len(commands['actions']), 2)
        self.assertEqual([row['original_argv'][-1] for row in commands['actions']], ['7', '9'])
        self.assertFalse(commands['execution_authorized'])

    def test_proof_or_selection_tampering_cannot_survive_full_revalidation(self):
        e = self.execution; original = deepcopy(e.report)
        for change in ('count', 'value', 'argument', 'target', 'binding', 'full-success', 'missing-proof', 'extra-state-field'):
            e.report = deepcopy(original)
            validation = e.report['author_validation']
            if change == 'count': validation['states'].pop()
            if change == 'value': validation['states'][0]['id'] = 'unsupported-50'
            if change == 'argument': validation['states'][0]['argument_bindings'][0]['argv_index'] = 0
            if change == 'target': validation['selected_ids'] = ['second']
            if change == 'binding': validation['binding']['receipt_sha256'] = 'f' * 64
            if change == 'full-success': validation['full_paper_success'] = True
            if change == 'missing-proof': e.report.pop('author_validation')
            if change == 'extra-state-field': validation['states'][0]['execution_authorized'] = True
            with self.subTest(change=change), self.assertRaises(AuthorExecutionError): e.compile()

    def test_reference_input_cannot_drop_the_authorization_overlay(self):
        e = self.execution
        missing = deepcopy(e.evidence); missing.pop('author_validation')
        with self.assertRaises(AuthorExecutionError): e.compile(evidence=missing)
        invalid = deepcopy(e.evidence); invalid['author_validation']['contract']['unit_count'] = 50
        with self.assertRaises(AuthorExecutionError): e.compile(evidence=invalid)

    def test_full_report_SHA_changes_even_when_invocations_are_unchanged(self):
        e = self.execution; before = e.compile(); original = deepcopy(e.report)
        raw = deepcopy(e.raw); raw['validation_selection']['states'][0]['label'] += ' reviewed label'
        e.report = validate_author_plan(e.evidence, raw); after = e.compile()
        self.assertEqual(before['package']['invocations'], after['package']['invocations'])
        self.assertNotEqual(before['package']['plan_sha256'], after['package']['plan_sha256'])
        self.assertNotEqual(before['package_sha256'], after['package_sha256'])
        with self.assertRaises(AuthorExecutionError):
            validate_bound_author_package(before, report=e.report, evidence=e.evidence, source_files=e.files,
                deployment=e.deployment, resources=e.resources, provenance=e.provenance)
        self.assertNotEqual(original['author_validation'], e.report['author_validation'])


if __name__ == '__main__':
    unittest.main()
