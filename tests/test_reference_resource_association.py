"""Synthetic catalog bindings and receipts; no model, network or physics."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from auto_lammps.discovery_library import DiscoveryLibrary
from auto_lammps.papers import PaperStore
from auto_lammps.potential_acquisition import prepare_paper_resources
from auto_lammps.reference_resource_association import (ReferenceResourceAssociation,
    catalog_association, reference_resource_candidate, reference_resource_matches,
    validate_catalog_association)
from auto_lammps.tasks import TaskError, TaskStore
from test_source_discovery import (COMMIT, DOI, NAME, TITLE, Reader as SyntheticSourceReader,
                                  SourceDiscovery)


class ReferenceResourceAssociationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); self.root.chmod(0o700)
        self.catalog_path = self.root/'synthetic-catalog.json'
        self.row = dict(repo=NAME, commit=COMMIT, title=TITLE, doi=DOI, inputs=['input.lmp'],
            potentials=['library.meam','alloy.meam'], pair_style=['meam'], elements=['Cu','Ni'],
            type_order=['Cu','Ni'], elements_complete=True, license='MIT',
            evidence='Synthetic configured provenance claim; not scientific evidence.')
        self.document = dict(generated='synthetic',tier_a=[self.row],tier_b=[],tier_c=[])
        self.save_catalog()
        self.library = DiscoveryLibrary(self.catalog_path)
        self.tasks = TaskStore(self.root/'tasks.sqlite'); self.papers = PaperStore(self.tasks)
        self.paper = self.papers.add(TITLE,DOI,'Synthetic fixed resource scope','Synthetic test only')
        self.service = ReferenceResourceAssociation(self.papers,self.library)

    def save_catalog(self):
        self.catalog_path.write_text(json.dumps(self.document)); self.catalog_path.chmod(0o600)

    def selections(self, repository=NAME):
        catalog = self.library.get()
        return [row['id'] for row in catalog['entries'] if row['repository']==repository],catalog['source']['sha256']

    def associate(self):
        ids,digest = self.selections()
        return self.service.associate(self.paper['id'],ids,catalog_sha256=digest)

    def receipts(self):
        flags = dict(scientific_validation=False,execution_authorized=False)
        source = dict(schema_version=1,id='c'*32,repository=NAME,commit=COMMIT,location='hpc',
            state='partial',author_identity_verified=False,source_complete=False,files=[],**flags)
        potential = dict(schema_version=1,id='d'*32,repository=NAME,commit=COMMIT,location='hpc',
            state='partial',files=[],bindings=[],engine_verified=False,catalog_admitted=False,**flags)
        source['potential_acquisition'] = potential
        return source,potential

    def test_configured_catalog_binds_both_kinds_without_search_or_approval_claim(self):
        with patch('subprocess.Popen',side_effect=AssertionError('no commands')):
            result = self.associate()
        self.assertEqual(result['repository'],NAME); self.assertEqual(result['commit'],COMMIT)
        self.assertEqual({row['kind'] for row in result['entries']},{'author_source','potential'})
        for flag in ('author_identity_verified','scientific_validation','execution_authorized',
                     'execution_ready','source_ready','B_input'):
            self.assertFalse(result[flag])
        self.assertFalse(result['license']['use_approved'])
        self.assertFalse(result['license']['redistribution_approved'])
        self.assertTrue(result['missing'])
        saved = self.papers.get(self.paper['id'])
        self.assertNotIn('source_discovery',saved)
        self.assertTrue(any(row['event'].startswith('resource_catalog_associated:') for row in saved['history']))
        self.assertFalse(any(row['event'].startswith('source_search_completed:') for row in saved['history']))

    def test_same_selection_is_idempotent_even_when_ids_are_reversed(self):
        result = self.associate(); before = self.papers.get(self.paper['id'])['revision']
        ids,digest = self.selections()
        self.assertEqual(self.service.associate(self.paper['id'],list(reversed(ids)),catalog_sha256=digest),result)
        self.assertEqual(self.papers.get(self.paper['id'])['revision'],before)

    def test_full_title_and_doi_are_required_not_partial_or_different_paper(self):
        original = deepcopy(self.row)
        for change in (dict(title=TITLE+' additional work'),dict(title='synthetic alloy'),dict(doi='10.1234/other')):
            self.row.update(change); self.save_catalog()
            with self.subTest(change=change),self.assertRaisesRegex(TaskError,'完整题名'):
                self.associate()
            self.row.clear();self.row.update(original)

    def test_unverified_candidate_or_example_repository_cannot_be_author_source(self):
        self.document['tier_a']=[];self.document['tier_b']=[self.row];self.save_catalog()
        with self.assertRaises(TaskError):self.associate()
        self.document['tier_a']=[self.row];self.document['tier_b']=[]
        for role in ('validation_tests','example_suite','artifact'):
            self.row['repository_role']=role;self.save_catalog()
            with self.subTest(role=role),self.assertRaises(TaskError):self.associate()

    def test_unknown_or_duplicate_ids_and_wrong_catalog_digest_are_rejected(self):
        ids,digest = self.selections()
        for altered in ([ids[0],ids[0]],[ids[0],'f'*64],[],ids+['e'*64]):
            with self.subTest(ids=altered),self.assertRaises(TaskError):
                self.service.associate(self.paper['id'],altered,catalog_sha256=digest)
        with self.assertRaises(TaskError):
            self.service.associate(self.paper['id'],ids,catalog_sha256='0'*64)
        self.assertIsNone(self.service.get(self.paper['id']))

    def test_same_kind_or_cross_pin_entries_cannot_be_combined(self):
        self.document['tier_a'].append(dict(self.row,repo='synthetic/other',commit='b'*40))
        self.save_catalog();catalog=self.library.get()
        main_source=next(row for row in catalog['entries'] if row['repository']==NAME and row['kind']=='author_source')
        for other in [row for row in catalog['entries'] if row['repository']!=NAME]:
            with self.subTest(kind=other['kind']),self.assertRaises(TaskError):
                self.service.associate(self.paper['id'],[main_source['id'],other['id']],catalog_sha256=catalog['source']['sha256'])

    def test_missing_other_kind_cannot_be_invented(self):
        ids,digest = self.selections()
        self.row['kind']='author_source';self.save_catalog()
        with self.assertRaises(TaskError):
            self.service.associate(self.paper['id'],ids,catalog_sha256=self.library.get()['source']['sha256'])
        self.assertEqual(len(self.library.get()['entries']),1)

    def test_undeclared_license_remains_a_gap_without_blocking_metadata_association(self):
        self.row['license']='NOASSERTION';self.save_catalog()
        report=self.associate()
        self.assertTrue(any('许可证范围' in gap for gap in report['missing']))
        self.assertEqual({row['declared'] for row in report['license']['claims']},{'NOASSERTION'})
        self.assertEqual(report['license']['status'],'declared_scope_unverified')
        self.assertFalse(report['license']['execution_approved'])

    def test_catalog_or_pin_change_invalidates_saved_association(self):
        before=self.associate()
        self.row['commit']='b'*40;self.save_catalog()
        with self.assertRaisesRegex(TaskError,'变化'):self.service.get(self.paper['id'])
        self.assertEqual(self.papers.get(self.paper['id'])['resource_catalog_association'],before)
        with self.assertRaises(TaskError):
            reference_resource_candidate(self.papers.get(self.paper['id']),discovery_library=self.library)

    def test_reviews_are_part_of_current_validation_not_only_catalog_file_digest(self):
        report=self.associate();reviews=self.root/'synthetic-reviews.json'
        reviews.write_text(json.dumps(dict(version=1,reviews=[dict(repository=NAME,commit=COMMIT,
            state='conflict',notes=['Synthetic new conflict'])])));reviews.chmod(0o600)
        changed=DiscoveryLibrary(self.catalog_path,reviews)
        self.assertEqual(changed.get()['source']['sha256'],report['catalog_sha256'])
        with self.assertRaises(TaskError):validate_catalog_association(self.paper,report,changed)

    def test_tampered_report_or_approval_flags_do_not_record(self):
        ids,digest=self.selections();valid=catalog_association(self.paper,self.library,ids,catalog_sha256=digest)
        mutations=(lambda r:r.update(commit='b'*40),lambda r:r.update(author_identity_verified=True),
            lambda r:r['license'].update(use_approved=True),lambda r:r.update(entry_ids=['f'*64,'e'*64]),
            lambda r:r['entries'][0].update(license='Forged license'),lambda r:r.update(paper_id='f'*32))
        for mutate in mutations:
            altered=deepcopy(valid);mutate(altered)
            with self.subTest(mutate=mutate),self.assertRaises(TaskError):
                self.papers.record_catalog_association(self.paper['id'],altered,discovery_library=self.library)
        self.assertIsNone(self.service.get(self.paper['id']))

    def test_updated_association_appends_history_and_keeps_old_snapshot(self):
        first=self.associate();old_revision=self.papers.get(self.paper['id'])['revision']
        self.row['evidence']='Synthetic additional provenance note';self.save_catalog()
        second=self.associate();self.assertNotEqual(first['id'],second['id'])
        with self.tasks.transaction() as db:
            previous=json.loads(db.execute('SELECT document FROM paper_revisions WHERE paper_id=? AND revision=?',
                (self.paper['id'],old_revision)).fetchone()[0])
        self.assertEqual(previous['resource_catalog_association'],first)
        events=[row for row in self.papers.get(self.paper['id'])['history'] if row['event'].startswith('resource_catalog_associated:')]
        self.assertEqual(len(events),2)

    def test_catalog_only_acquisition_accepts_exact_current_pin_and_keeps_idempotence(self):
        self.associate();source,potential=self.receipts()
        for method,report in ((self.papers.record_reference_resources,source),(self.papers.record_potential_acquisition,potential)):
            with self.assertRaises(TaskError):method(self.paper['id'],report)
            method(self.paper['id'],report,discovery_library=self.library)
            revision=self.papers.get(self.paper['id'])['revision']
            method(self.paper['id'],report,discovery_library=self.library)
            self.assertEqual(self.papers.get(self.paper['id'])['revision'],revision)
            for change in (dict(repository='synthetic/foreign'),dict(commit='b'*40)):
                changed=dict(report,**change)
                with self.subTest(change=change),self.assertRaises(TaskError):
                    method(self.paper['id'],changed,discovery_library=self.library)

    def test_expired_catalog_cannot_accept_even_exact_old_pin_receipt(self):
        self.associate();source,potential=self.receipts()
        self.row['license']='Changed declared license';self.save_catalog()
        for method,report in ((self.papers.record_reference_resources,source),(self.papers.record_potential_acquisition,potential)):
            with self.assertRaises(TaskError):method(self.paper['id'],report,discovery_library=self.library)

    def test_existing_resource_preparation_chooses_registered_catalog_and_records_real_receipts(self):
        self.associate();source,potential=self.receipts()
        remote=Mock();remote.acquire.return_value=source
        with patch('auto_lammps.remote_resources.RemoteResources',return_value=remote):
            result=prepare_paper_resources(self.papers,self.paper['id'],self.root/'synthetic-audit',
                policy={'synthetic':'unused by mock'},discovery_library=self.library)
        remote.acquire.assert_called_once_with(NAME,COMMIT)
        self.assertEqual(result,potential)
        saved=self.papers.get(self.paper['id'])
        self.assertEqual(saved['reference_resources'],source)
        self.assertEqual(saved['potential_acquisition'],potential)
        self.assertNotIn('source_discovery',saved)

    def test_no_trusted_catalog_context_does_not_acquire_from_browser_or_stored_claim_alone(self):
        self.associate()
        with patch('auto_lammps.remote_resources.RemoteResources') as remote:
            self.assertEqual(prepare_paper_resources(self.papers,self.paper['id'],self.root/'unused'),
                             {'state':'source_unresolved'})
        remote.assert_not_called()

    def test_legacy_verified_search_remains_usable_without_catalog(self):
        self.associate()
        discovery=SourceDiscovery(self.root/'synthetic-search',reader=SyntheticSourceReader()).discover(TITLE,DOI)
        self.papers.record_source_search(self.paper['id'],discovery)
        self.row['commit']='b'*40;self.save_catalog()
        source,potential=self.receipts()
        # The independent old search proof remains valid for its unchanged pin.
        self.papers.record_reference_resources(self.paper['id'],source)
        self.papers.record_potential_acquisition(self.paper['id'],potential)
        saved=self.papers.get(self.paper['id'])
        self.assertTrue(reference_resource_matches(saved,source))
        self.assertEqual(reference_resource_candidate(saved)['commit'],COMMIT)


if __name__=='__main__':unittest.main()
