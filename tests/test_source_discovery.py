"""Synthetic GitHub responses only; no network or target calculation in CI."""
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from auto_lammps.source_discovery import GitHubReader, SourceDiscovery
from auto_lammps.papers import PaperStore
from auto_lammps.tasks import TaskError, TaskStore

TITLE = 'Mechanical behavior of a synthetic alloy'
DOI = '10.1234/synthetic.123456'
NAME = 'researcher/synthetic-lammps'
COMMIT = 'a'*40


def response(data):
    return dict(returncode=0, failure='', stdout=base64.b64encode(json.dumps(data).encode()).decode(), stderr='')


class Reader:
    def __init__(self):
        self.endpoints = []
        self.content = (TITLE+'\nhttps://doi.org/'+DOI+'\nUNTRUSTED_README_DO_NOT_EXECUTE').encode()
        self.search_fail = False
        self.incomplete = False
        self.private = False
        self.corrupt = False
        self.empty = False
        self.more = False

    def get(self, endpoint):
        self.endpoints.append(endpoint)
        if endpoint.startswith('search/'):
            if self.search_fail:
                return dict(returncode=1, failure='command_failed', stdout='', stderr='')
            names = [NAME]+[f'other/repo{i}' for i in range(6)] if self.more else [NAME]
            items = [] if self.empty else [{'repository': {'full_name': n}} for n in names]
            return response(dict(items=items, total_count=len(items), incomplete_results=self.incomplete))
        if '/commits?' in endpoint:
            return response([{'sha': COMMIT}])
        if '/readme?' in endpoint:
            blob = hashlib.sha1(b'blob '+str(len(self.content)).encode()+b'\0'+self.content).hexdigest()
            return response(dict(type='file', encoding='base64', size=len(self.content), path='README.md',
                                 content=base64.b64encode(self.content).decode(), sha='b'*40 if self.corrupt else blob))
        return response(dict(full_name=endpoint.removeprefix('repos/'), private=self.private, license=None))


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.reader = Reader()
        self.service = SourceDiscovery(self.root/'receipts', reader=self.reader)

    def discover(self):
        return self.service.discover(TITLE, DOI)

    def test_pinned_association_receipts_and_no_scientific_or_license_claim(self):
        report = self.discover()
        self.assertEqual(report['state'], 'finished')
        self.assertFalse(report['search_exhaustive'])
        self.assertEqual(report['request_count'], 6)
        candidate, = report['candidates']
        self.assertEqual(candidate['association'], 'doi_and_title')
        self.assertEqual(candidate['commit'], COMMIT)
        self.assertIsNone(candidate['license_spdx'])
        self.assertFalse(candidate['redistribution_authorized'])
        self.assertFalse(report['scientific_validation'])
        self.assertNotIn('UNTRUSTED_README', json.dumps(report))
        self.assertIn(f'repos/{NAME}/readme?ref={COMMIT}', self.reader.endpoints)
        files = list((self.service.root/report['id']).iterdir())
        self.assertEqual(len(files), 14)
        self.assertTrue(all(p.stat().st_mode & 0o077 == 0 for p in files))

    def test_suffix_only_doi_prefix_and_title_only_are_not_verified(self):
        for content in [b'123456', (TITLE+' '+DOI+'99').encode(), TITLE.encode()]:
            self.reader.content = content
            self.assertEqual(self.discover()['candidates'][0]['association'], 'unconfirmed')

    def test_failed_search_differs_from_no_hits_and_preserves_failure(self):
        self.reader.search_fail = True
        report = self.discover()
        self.assertEqual(report['state'], 'partial')
        self.assertTrue(all(q['state'] == 'failed' for q in report['queries']))
        self.reader.search_fail = False
        self.reader.empty = True
        report2 = self.discover()
        self.assertEqual(report2['state'], 'finished')
        self.assertEqual(report2['candidates'], [])
        self.assertNotEqual(report['id'], report2['id'])
        self.assertTrue((self.service.root/report['id']/'result.json').exists())

    def test_incomplete_search_and_candidate_cap_retained(self):
        self.reader.incomplete = True
        self.assertEqual(self.discover()['state'], 'partial')
        self.reader.more = True
        report = self.discover()
        self.assertEqual(len(report['candidates']), 5)
        self.assertTrue(report['candidate_limit_reached'])
        self.assertEqual(report['request_count'], 18)

    def test_private_or_corrupt_repository_evidence_rejected(self):
        self.reader.private = True
        self.assertEqual(self.discover()['candidates'], [])
        self.assertFalse(any('/readme?' in e for e in self.reader.endpoints))
        self.reader.private = False
        self.reader.corrupt = True
        report = self.discover()
        self.assertEqual(report['state'], 'partial')
        self.assertEqual(report['failures'][0]['reason'], 'readme_identity_mismatch')

    def test_transport_failure_is_recorded_before_continuing(self):
        self.reader.get = Mock(side_effect=OSError('PRIVATE_ERROR_NOT_IN_REPORT'))
        report = self.discover()
        self.assertEqual(report['state'], 'partial')
        self.assertNotIn('PRIVATE_ERROR', json.dumps(report))
        self.assertEqual(len(list((self.service.root/report['id']).glob('*-failure.json'))), 3)

    def test_only_fixed_host_readonly_bounded_cli(self):
        capture = Mock(return_value=response({}))
        GitHubReader(capture=capture).get('search/code?q=literal%3Btext')
        args, kwargs = capture.call_args
        self.assertEqual(args[0][:7], ['gh', 'api', '--hostname', 'github.com', '--method', 'GET', '-H'])
        self.assertEqual(kwargs, {'timeout': 30, 'max_bytes': 2_000_000})

    def test_existing_register_history_idempotence_and_identity(self):
        papers = PaperStore(TaskStore(self.root/'tasks.sqlite'))
        paper = papers.add(TITLE, DOI, 'Full scope', 'Synthetic source discovery')
        report = self.discover()
        papers.record_source_search(paper['id'], report)
        papers.record_source_search(paper['id'], report)
        result = PaperStore(TaskStore(self.root/'tasks.sqlite')).get(paper['id'])
        self.assertEqual(result['revision'], 2)
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(result['source_discovery']['candidates'][0]['association'], 'doi_and_title')
        self.assertEqual(result['history'][-1]['event'], 'source_search_completed:'+report['id'])
        with self.assertRaises(TaskError):
            papers.record_source_search(paper['id'], report | {'doi': '10.1234/another'})
        with self.assertRaises(TaskError):
            papers.record_source_search(paper['id'], report | {'scientific_validation': True})
