"""Synthetic source-to-resource flow, no physics or network."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from auto_lammps.potential_acquisition import MeamAcquisition, AcquisitionError, meam_declarations, prepare_paper_resources
from auto_lammps.papers import PaperStore
from auto_lammps.tasks import TaskStore, TaskError
from test_source_discovery import Reader as SourceReader, SourceDiscovery, response, TITLE, DOI, NAME, COMMIT
from test_meam_potentials import LIBRARY, PARAMETERS

INPUT = b'units metal\npair_style meam\npair_coeff * * library.meam Cu Ni alloy.meam Ni Cu Ni\nrun 100\n'
TREE = 'c'*40


def git_blob(data):
    return hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()


class Reader:
    def __init__(self):
        self.files = {'input.lmp': INPUT, 'library.meam': LIBRARY, 'alloy.meam': PARAMETERS,
                      'LICENSE': b'Synthetic test resource, Apache-2.0'}
        self.mode = '100644'
        self.truncated = False
        self.corrupt = False
        self.fail = False
        self.calls = []

    def get(self, endpoint):
        self.calls.append(endpoint)
        if self.fail:
            return dict(returncode=1, failure='timeout', stdout='', stderr='')
        if '/git/commits/' in endpoint:
            return response({'sha': COMMIT, 'tree': {'sha': TREE}})
        if '/git/trees/' in endpoint:
            return response({'sha': TREE, 'truncated': self.truncated, 'tree': [
                dict(path=p, sha=git_blob(b), size=len(b), type='blob', mode=self.mode)
                for p, b in self.files.items()]})
        identity = endpoint.rsplit('/', 1)[-1]
        data = next(b for b in self.files.values() if git_blob(b) == identity)
        import base64
        return response(dict(sha=identity, size=len(data), encoding='base64',
                             content=base64.b64encode(b'corrupt' if self.corrupt else data).decode()))


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.reader = Reader()
        self.service = MeamAcquisition(self.root/'audit', reader=self.reader)

    def acquire(self):
        return self.service.acquire(NAME, COMMIT)

    def test_pinned_files_and_order_without_source_or_engine_execution(self):
        with patch('subprocess.Popen', side_effect=AssertionError('no execution')):
            report = self.acquire()
        self.assertEqual(report['state'], 'finished')
        self.assertEqual(len(report['files']), 2)
        binding, = report['bindings']
        self.assertEqual(binding['elements'], ['Cu', 'Ni'])
        self.assertEqual(binding['type_elements'], ['Ni', 'Cu', 'Ni'])
        self.assertEqual(binding['static_status'], 'checked')
        self.assertEqual(binding['required_package'], 'MEAM')
        self.assertFalse(report['engine_verified'])
        self.assertFalse(report['catalog_admitted'])
        self.assertNotIn('run 100', json.dumps(report))
        for item in report['files']:
            self.assertEqual((self.service.root/report['id']/(item['sha256']+'.model')).read_bytes(),
                             self.reader.files[item['path']])
        self.assertTrue(all(p.stat().st_mode & 0o077 == 0 for p in (self.service.root/report['id']).iterdir()))
        self.assertIn(COMMIT, self.reader.calls[0])
        self.assertEqual(report['license_status'], 'text_found_scope_unverified')

    def test_missing_license_is_collection_not_redistribution_approval(self):
        del self.reader.files['LICENSE']
        report = self.acquire()
        self.assertEqual(len(report['files']), 2)
        self.assertEqual(report['license_status'], 'not_declared_in_scanned_tree')
        self.assertFalse(report['execution_authorized'])

    def test_corruption_truncation_and_symlink_do_not_yield_binding(self):
        for option in ('corrupt', 'truncated', 'mode'):
            self.reader = Reader()
            setattr(self.reader, option, '120000' if option == 'mode' else True)
            self.service.reader = self.reader
            report = self.acquire()
            self.assertEqual(report['bindings'], [])
            if option != 'mode': self.assertEqual(report['state'], 'partial')

    def test_dynamic_commands_and_ambiguous_paths_are_not_guessed(self):
        for raw in (INPUT.replace(b'library.meam', b'${lib}'),
                    INPUT.replace(b'meam\n', b'hybrid meam lj/cut 5\n'),
                    INPUT+b'include hidden.in\n', INPUT.replace(b'metal', b'real')):
            with self.subTest(raw=raw), self.assertRaises(AcquisitionError): meam_declarations(raw)
        self.reader.files['cases/input.lmp'] = self.reader.files.pop('input.lmp')
        self.reader.files['cases/library.meam'] = LIBRARY
        self.assertEqual(self.acquire()['bindings'], [])

    def test_failed_download_and_static_failure_are_retained(self):
        self.reader.fail = True
        failed = self.acquire()
        self.assertEqual(failed['state'], 'partial')
        self.reader.fail = False
        self.reader.files['library.meam'] = b'broken original model'
        report = self.acquire()
        self.assertEqual(report['bindings'][0]['static_status'], 'needs_review')
        self.assertEqual(len(report['files']), 2)
        self.assertTrue((self.service.root/failed['id']/'result.json').exists())

    def test_repository_scan_bounded_and_absent_models_not_claimed(self):
        self.reader.files = {f'in.case{i}': b'units metal\n' for i in range(20)}
        report = self.acquire()
        self.assertEqual(report['bindings'], [])
        self.assertEqual(report['state'], 'partial')
        self.assertEqual(report['request_count'], 10)
        self.assertIn({'reason': 'input_scan_limit'}, report['failures'])

    def test_discovery_to_paper_history_uses_exact_source_and_is_idempotent(self):
        papers = PaperStore(TaskStore(self.root/'tasks.sqlite'))
        paper = papers.add(TITLE, DOI, 'Synthetic resource acquisition', 'Synthetic test')
        discovery = SourceDiscovery(self.root/'sources', reader=SourceReader()).discover(TITLE, DOI)
        papers.record_source_search(paper['id'], discovery)
        report = prepare_paper_resources(papers, paper['id'], self.root/'flow', reader=self.reader)
        current = papers.get(paper['id'])
        self.assertEqual(current['potential_acquisition'], report)
        revision = current['revision']
        papers.record_potential_acquisition(paper['id'], report)
        self.assertEqual(papers.get(paper['id'])['revision'], revision)
        self.assertEqual(current['status'], 'pending')
        self.assertTrue(any(e['event'].startswith('potential_acquired:') for e in current['history']))
        changed = copy.deepcopy(report)
        changed['commit'] = 'b'*40
        with self.assertRaises(TaskError): papers.record_potential_acquisition(paper['id'], changed)
        changed = copy.deepcopy(report)
        changed['engine_verified'] = True
        with self.assertRaises(TaskError): papers.record_potential_acquisition(paper['id'], changed)

    def test_source_discovery_entrypoint_triggers_resource_preparation(self):
        from auto_lammps.source_discovery import main
        papers = PaperStore(TaskStore(self.root/'cli.sqlite'))
        paper = papers.add(TITLE, DOI, 'Synthetic full task', 'Synthetic test')
        discovery = SourceDiscovery(self.root/'prebuilt', reader=SourceReader()).discover(TITLE, DOI)
        args = ['source-discovery', '--database', str(self.root/'cli.sqlite'), '--paper-id', paper['id'],
                '--audit-directory', str(self.root/'cli-audit')]
        with patch('sys.argv', args), patch('builtins.print'), patch.object(
                SourceDiscovery, 'discover', return_value=discovery), patch(
                'auto_lammps.potential_acquisition.GitHubReader', return_value=self.reader):
            main()
        self.assertEqual(len(papers.get(paper['id'])['potential_acquisition']['files']), 2)
