"""Human workflow integration with synthetic provenance; no API/HPC physics."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from auto_lammps.discovery_library import DiscoveryLibrary
from auto_lammps.ledger import Ledger
from auto_lammps.manifest import canonical, sha256
from auto_lammps.paper_evidence import PaperEvidenceViews
from auto_lammps.paper_workflow import PaperWorkflowService, STATES
from auto_lammps.papers import PaperStore
from auto_lammps.runtime_launcher import ExecutionDenied
from auto_lammps.source_discovery import SourceDiscovery
from auto_lammps.tasks import FIELDS, StaleTask, TaskError, TaskStore
from test_ledger import H1, H2, POLICY, RESOURCE
from test_source_discovery import Reader, TITLE, DOI, NAME, COMMIT
from test_tasks import evidence, target_inventory


class PaperWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.tasks = TaskStore(self.root/'tasks.sqlite')
        self.task = self.tasks.create('Synthetic reproduction', 'No physics or author solution', 'reproduction')
        self.papers = PaperStore(self.tasks)
        paper = self.papers.add(TITLE, DOI, 'Synthetic declared scope', 'No real paper data')
        paper = self.papers.select(paper['id'], paper['revision'])
        self.paper = self.papers.link_task(paper['id'], paper['revision'], self.task['id'])
        self.catalog_file = self.root/'catalog.json'
        self.catalog = dict(generated='synthetic', tier_a=[], tier_b=[], tier_c=[])
        self.library = DiscoveryLibrary(self.catalog_file)
        self.save_catalog()
        self.reader = Reader()
        self.discovery = SourceDiscovery(self.root/'discovery', reader=self.reader)
        self.service = PaperWorkflowService(self.tasks, self.papers, discovery_library=self.library,
                                           source_discovery=self.discovery)

    def save_catalog(self):
        self.catalog_file.write_bytes(canonical(self.catalog)); self.catalog_file.chmod(0o600)

    def row(self, **changes):
        return dict(repo=NAME, commit=COMMIT, title=TITLE, doi=DOI, pair_style=['meam'],
            potentials=['library.meam', 'alloy.meam'], inputs=['in.alloy'], elements=['Co', 'Ni'],
            type_order=['Co', 'Ni'], elements_complete=True, license='MIT', **changes)

    def attach_P(self):
        directory = self.root/'evidence'; directory.mkdir(mode=0o700)
        parent = directory/self.task['id']; parent.mkdir(mode=0o700)
        folder = parent/'paper-evidence'; folder.mkdir(mode=0o700)
        receipt = canonical(dict(paper=dict(title=TITLE, doi=DOI), modules=['existing_workbench']))
        contents = {'receipt.json':receipt, 'P.csv':b'x,y\n0,1\n1,\n'}
        for name, raw in contents.items():
            path = folder/name; path.write_bytes(raw); path.chmod(0o600)
        inv = target_inventory(); inv['paper'] = dict(title=TITLE, doi=DOI)
        inv['source_sha256'] = sha256(receipt)
        self.P = dict(version=1, task_id=self.task['id'], paper_id=self.paper['id'], title=TITLE, doi=DOI,
            role='paper_evidence_human_only', source_sha256=sha256(receipt), source_receipt_file='receipt.json',
            source_note='Existing synthetic workbench', limitations=['No scientific acceptance'],
            target_inventory=inv, priorities={'fig-1-a':1}, figures=[],
            files=[dict(name=n, label=n, size=len(raw), sha256=sha256(raw)) for n, raw in contents.items()],
            views=[dict(id='figure-1', title='P synthetic table', description='Paper result only', figures=[],
                tables=[dict(name='P.csv', role='paper', label='P result canary',
                             columns=[dict(key='x', label='x'), dict(key='y', label='y')])])])
        path = folder/'manifest.json'; path.write_bytes(canonical(self.P)); path.chmod(0o600)
        self.service.evidence = PaperEvidenceViews(directory, self.papers)

    def ready_conditions(self):
        doc = self.tasks.get(self.task['id'])
        for field in FIELDS:
            doc = self.tasks.add_candidate(doc['id'], doc['revision'], field,
                evidence('REFERENCE_ANSWER_CANARY' if field == 'reference' else 'allowed-'+field, origin='paper'))
        return self.tasks.confirm(doc['id'], doc['revision'], list(FIELDS))

    def action(self, name, report=None):
        return next(a for a in (report or self.service.get(self.task['id']))['actions'] if a['id'] == name)

    def bind_reference(self):
        ledger = Ledger(self.root/'ledger.sqlite'); ledger.create_campaign('synthetic', POLICY)
        self.papers.ledger = ledger
        evaluation = ledger.register_evaluation('synthetic', task_sha256=H1, repetition=0,
                                                role='reference', system_sha256=H2)
        self.papers.bind_reference_evaluation(self.paper['id'], self.task['id'], evaluation)
        return ledger, evaluation

    def test_research_is_independent_of_paper_and_all_reference_providers(self):
        task = self.tasks.create('Ordinary research', 'No paper required', 'research')
        self.service.library = Mock(get=Mock(side_effect=AssertionError('must not load P resources')))
        report = self.service.get(task['id'])
        self.assertFalse(report['applicable']); self.assertIsNone(report['state'])
        self.assertEqual(report['actions'], [])
        with self.assertRaises(TaskError): self.service.get_B_draft(task['id'])

    def test_unlinked_reproduction_prompts_paper_selection(self):
        task = self.tasks.create('Unlinked reproduction', 'No association', 'reproduction')
        report = self.service.get(task['id'])
        self.assertTrue(report['applicable']); self.assertIsNone(report['paper'])
        self.assertEqual(report['state'], 'resource_check'); self.assertEqual(report['actions'], [])

    def test_exact_identity_and_both_kinds_are_candidates_not_execution_readiness(self):
        self.catalog['tier_a'] = [self.row()]; self.save_catalog()
        report = self.service.prepare_resources(self.task['id'], self.task['revision'])
        bundle, = report['resources']['bundles']
        self.assertTrue(bundle['candidate_complete']); self.assertFalse(bundle['execution_ready'])
        self.assertFalse(report['resources']['source_ready']); self.assertFalse(report['execution_authorized'])
        self.assertFalse(self.action('approve_A', report)['enabled'])
        self.service.search_github(self.task['id'], self.task['revision'])
        self.assertEqual(self.reader.endpoints, [])
        self.assertEqual(self.tasks.get(self.task['id'])['revision'], self.task['revision'])

    def test_mismatched_title_artifact_doi_or_test_repository_cannot_match_author_bundle(self):
        for change in ({'title':'Different paper'}, {'doi':'10.11578/artifact'},
                       {'repository_role':'validation_tests'}):
            row = self.row(); row.update(change)
            self.catalog['tier_a'] = [row]; self.save_catalog()
            resources = self.service.get(self.task['id'])['resources']
            self.assertFalse(any(g['candidate_complete'] for g in resources['bundles']))
            self.assertFalse(resources['source_ready'])
        self.catalog['tier_a'] = []; self.catalog['tier_b'] = [self.row()]; self.save_catalog()
        self.assertFalse(self.service.get(self.task['id'])['resources']['bundles'][0]['candidate_complete'])

    def test_source_and_potential_at_different_pins_do_not_form_complete_bundle(self):
        source = self.row(); source.update(kind='author_source')
        potential = self.row(); potential.update(kind='potential', commit='b'*40)
        self.catalog['tier_a'] = [source, potential]; self.save_catalog()
        bundles = self.service.get(self.task['id'])['resources']['bundles']
        self.assertEqual(len(bundles), 2); self.assertTrue(all(not b['candidate_complete'] for b in bundles))

    def test_configured_Github_adapter_really_searches_then_reuses_retained_record(self):
        report = self.service.search_github(self.task['id'], self.task['revision'])
        self.assertEqual(len(self.reader.endpoints), 6)
        self.assertEqual(report['resources']['github']['state'], 'finished')
        self.assertFalse(report['resources']['github']['search_exhaustive'])
        self.assertEqual(report['resources']['github']['matched_repositories'][0]['association'], 'doi_and_title')
        self.assertFalse(report['resources']['source_ready'])
        self.service.search_github(self.task['id'], self.task['revision'])
        self.assertEqual(len(self.reader.endpoints), 6)
        self.assertEqual(len([e for e in self.papers.get(self.paper['id'])['history']
                              if e['event'].startswith('source_search_completed:')]), 1)

    def test_unconfigured_or_failed_search_never_claims_no_Github_resource_exists(self):
        self.service.discovery = None
        report = self.service.get(self.task['id'])
        self.assertEqual(report['resources']['github']['state'], 'not_searched')
        self.assertFalse(self.action('search_github', report)['enabled'])
        with self.assertRaises(TaskError): self.service.search_github(self.task['id'], self.task['revision'])
        self.service.discovery = self.discovery; self.reader.search_fail = True
        report = self.service.search_github(self.task['id'], self.task['revision'])
        self.assertEqual(report['resources']['github']['state'], 'partial')
        self.assertFalse(report['resources']['github']['search_exhaustive'])
        self.assertFalse(report['resources']['source_ready'])

    def test_registered_workbench_P_exposes_priority_without_mutating_conditions(self):
        self.attach_P(); old = self.tasks.get(self.task['id']); history = self.tasks.history(self.task['id'])
        report = self.service.get(self.task['id'])
        self.assertEqual(report['state'], 'evidence_ready')
        self.assertEqual(report['evidence']['priorities'][0]['priority'], 1)
        self.assertTrue(report['targets']['can_import']); self.assertFalse(report['targets']['imported'])
        self.assertFalse(self.action('extract_P', report)['enabled'])
        self.assertNotIn('PRIVATE_SCORE_CANARY', json.dumps(report))
        self.assertNotIn('P result canary', json.dumps(report))
        self.assertEqual(self.tasks.get(self.task['id']), old); self.assertEqual(self.tasks.history(self.task['id']), history)

    def test_existing_target_tools_freeze_and_package_projection_stays_human_only(self):
        self.attach_P(); doc = self.ready_conditions()
        doc = self.service.import_targets(doc['id'], doc['revision'])
        doc = self.service.select_targets(doc['id'], doc['revision'], ['fig-1-a'], '')
        report = self.service.get(doc['id']); self.assertTrue(report['targets']['can_freeze'])
        doc = self.service.freeze(doc['id'], doc['revision'])
        self.service.evidence = Mock(get=Mock(side_effect=AssertionError('P must not enter B draft')))
        self.service.references = Mock(get=Mock(side_effect=AssertionError('A must not enter B draft')))
        reply = self.service.get_B_draft(doc['id']); encoded = canonical(reply)
        self.assertNotIn(b'REFERENCE_ANSWER_CANARY', encoded); self.assertNotIn(b'PRIVATE_SCORE_CANARY', encoded)
        self.assertNotIn(TITLE.encode(), encoded); self.assertNotIn(DOI.encode(), encoded)
        self.assertFalse(reply['released']); self.assertEqual(reply['draft']['release_status'], 'operator_review_required')
        for key in ('input_semantics_verified','resources_verified','runtime_isolation_verified','execution_authorized'):
            self.assertFalse(reply['draft'][key])
        self.assertEqual(reply['draft_sha256'], sha256(self.tasks.export_packages(doc['id'])['execution']))

    def test_reservation_unknown_or_unreadable_ledger_cannot_create_posthoc_targets(self):
        self.attach_P(); doc = self.ready_conditions()
        doc = self.service.import_targets(doc['id'], doc['revision'])
        ledger, evaluation = self.bind_reference()
        row = ledger.reserve(evaluation, 'synthetic', H2, RESOURCE)
        for unknown in (False, True):
            if unknown:
                ledger.begin_dispatch(row['id']); ledger.uncertain(row['id'], {'synthetic':True})
            report = self.service.get(doc['id']); self.assertFalse(report['targets']['can_select'])
            self.assertTrue(report['evidence']['available'])
            for operation in (lambda:self.service.import_targets(doc['id'], doc['revision']),
                              lambda:self.service.select_targets(doc['id'], doc['revision'], ['fig-1-a'], ''),
                              lambda:self.service.freeze(doc['id'], doc['revision'])):
                with self.assertRaises(TaskError): operation()
            self.assertEqual(self.tasks.get(doc['id'])['revision'], doc['revision'])
        self.papers.ledger = None
        report = self.service.get(doc['id']); self.assertFalse(report['targets']['can_select'])
        self.assertTrue(report['evidence']['available'])

    def test_stale_and_finished_tasks_cannot_start_new_resources_or_targets(self):
        with self.assertRaises(StaleTask): self.service.prepare_resources(self.task['id'], 0)
        self.tasks.manage_lifecycle(self.task['id'], self.task['revision'], 0, 'finish')
        for operation in (lambda:self.service.prepare_resources(self.task['id'], self.task['revision']),
                          lambda:self.service.search_github(self.task['id'], self.task['revision'])):
            with self.assertRaises(TaskError): operation()
        self.assertFalse(self.action('check_resources')['enabled']); self.assertEqual(self.reader.endpoints, [])

    def test_bad_or_wrong_task_evidence_remains_separate_from_user_conditions(self):
        other = dict(task_id='f'*32, title=TITLE, doi=DOI)
        self.service.evidence = Mock(get=Mock(return_value=other))
        report = self.service.get(self.task['id'])
        self.assertFalse(report['evidence']['available']); self.assertEqual(report['errors'][0]['code'], 'paper_evidence_unavailable')
        self.service.evidence = Mock(get=Mock(side_effect=ExecutionDenied('synthetic unsafe file')))
        report = self.service.get(self.task['id']); self.assertFalse(report['evidence']['available'])
        self.assertNotIn('synthetic unsafe file', json.dumps(report))

    def test_retained_reference_A_is_viewable_even_without_candidate_B(self):
        ledger, evaluation = self.bind_reference()
        row = ledger.reserve(evaluation, 'synthetic', H2, RESOURCE)
        ledger.begin_dispatch(row['id']); ledger.accepted(row['id'], '123', {'synthetic':True})
        report = self.service.get(self.task['id'])
        self.assertEqual(report['state'], 'reference_active')
        self.assertEqual(report['reference']['progress']['entries'][0]['evaluation']['requests'][0]['job_id'], '123')
        self.assertFalse(report['reference']['report_available'])
        self.assertFalse(report['b_draft']['preview_available'])
        self.service.references = Mock(get=Mock(return_value=dict(task_id=self.task['id'], title=TITLE, doi=DOI,
                                                                scientific_status='diagnostic')))
        ledger.observe(row['id'], '123', 'completed', {'synthetic':True}); ledger.account(row['id'], 10, H2)
        report = self.service.get(self.task['id']); self.assertEqual(report['state'], 'reference_results')
        self.assertTrue(self.action('view_A', report)['enabled']); self.assertFalse(self.action('start_B', report)['enabled'])
        self.assertIn(report['state'], STATES); self.assertLessEqual(len(STATES), 8)

    def test_ready_remote_static_files_require_matching_pins_bindings_and_inventory_digest(self):
        self.service.search_github(self.task['id'], self.task['revision'])
        files = [dict(path='in.alloy', size=10, sha256='1'*64),
                 dict(path='library.meam', size=20, sha256='2'*64),
                 dict(path='alloy.meam', size=30, sha256='3'*64)]
        acquisition = dict(schema_version=1, id='2'*32, repository=NAME, commit=COMMIT, state='finished', location='hpc',
            scientific_validation=False, engine_verified=False, catalog_admitted=False, execution_authorized=False,
            files=files[1:], bindings=[dict(input_path='in.alloy', input_sha256='1'*64, static_status='checked',
                                          files={'library':'library.meam', 'parameters':'alloy.meam'})])
        report = dict(schema_version=1, id='1'*32, repository=NAME, commit=COMMIT, state='finished', location='hpc',
            scientific_validation=False, execution_authorized=False, author_identity_verified=False,
            source_complete=True, files=files, inventory_sha256=sha256(canonical(files)), potential_acquisition=acquisition,
            remote_directory='PRIVATE_HPC_DIRECTORY_CANARY')
        self.papers.record_reference_resources(self.paper['id'], report)
        resources = self.service.get(self.task['id'])['resources']
        self.assertTrue(resources['source_ready']); self.assertFalse(resources['execution_authorized'])
        self.assertNotIn('PRIVATE_HPC_DIRECTORY_CANARY', json.dumps(resources))
        for index, change in enumerate((lambda r:r.update(inventory_sha256='f'*64),
                       lambda r:r['potential_acquisition'].update(commit='b'*40),
                       lambda r:r['potential_acquisition']['bindings'][0].update(input_sha256='f'*64),
                       lambda r:r['potential_acquisition']['bindings'][0].update(static_status='blocked'))):
            changed = deepcopy(report); changed['id'] = format(index+3, '032x'); change(changed)
            self.papers.record_reference_resources(self.paper['id'], changed)
            self.assertFalse(self.service.get(self.task['id'])['resources']['source_ready'])
