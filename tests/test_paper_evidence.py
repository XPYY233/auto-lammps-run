"""Human reference views of synthetic workbench files; no model or physics."""
from copy import deepcopy
import json
from pathlib import Path
import os
import tempfile
import unittest
from fastapi.testclient import TestClient
from auto_lammps.tasks import TaskStore, FIELDS
from auto_lammps.papers import PaperStore
from auto_lammps.paper_evidence import PaperEvidenceViews
from auto_lammps.results import ResultUnavailable
from auto_lammps.runtime_launcher import ExecutionDenied
from auto_lammps.manifest import canonical, sha256
from auto_lammps.web import create_app
from test_tasks import target_inventory, evidence
from test_web import ORIGIN, HEADERS


class PaperEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.store = TaskStore(root/'tasks.sqlite')
        self.task = self.store.create('Synthetic reference workspace', 'Synthetic conditions only', 'reproduction')
        self.papers = PaperStore(self.store)
        self.paper = self.papers.add('Synthetic workbench paper', '10.1234/example', 'Synthetic full scope', 'No physics')
        self.paper = self.papers.select(self.paper['id'], self.paper['revision'])
        self.paper = self.papers.link_task(self.paper['id'], self.paper['revision'], self.task['id'])
        self.directory = root/'evidence'; self.directory.mkdir(mode=0o700)
        folder = self.directory/self.task['id']; folder.mkdir(mode=0o700)
        self.folder = folder/'paper-evidence'; self.folder.mkdir(mode=0o700)
        receipt = canonical(dict(paper=dict(title=self.paper['title'], doi=self.paper['doi']), modules=['existing_workbench']))
        self.contents = {'source.json':receipt, 'original.png':b'synthetic-image', 'points.csv':b'x,y\n0,1\n1,2\n'}
        for name, content in self.contents.items():
            path = self.folder/name;path.write_bytes(content);path.chmod(0o600)
        inv = target_inventory();inv['paper'] = dict(title=self.paper['title'], doi=self.paper['doi'])
        inv['source_sha256'] = sha256(receipt)
        self.document = dict(version=1, task_id=self.task['id'], paper_id=self.paper['id'],
            title=self.paper['title'], doi=self.paper['doi'], role='paper_evidence_human_only',
            source_sha256=sha256(receipt), source_receipt_file='source.json', source_note='Existing synthetic workbench export',
            limitations=['Not scientific verification'], target_inventory=inv, priorities={inv['targets'][0]['id']:1},
            files=[dict(name=n,label=n,size=len(b),sha256=sha256(b)) for n,b in self.contents.items()],
            figures=[dict(name='original.png', label='Paper original', caption='Synthetic original image')],
            views=[dict(id='figure-1',title='Figure 1',description='Synthetic P, not A or B',
                figures=[dict(name='original.png',role='paper')],
                tables=[dict(name='points.csv',role='paper',label='Digitized P',columns=[dict(key='x',label='x'),dict(key='y',label='y')])])])
        self.save()
        self.views = PaperEvidenceViews(self.directory, self.papers)
        self.client = TestClient(create_app(self.store,papers=self.papers,paper_evidence_views=self.views),base_url=ORIGIN)
        self.addCleanup(self.client.close)

    def save(self):
        p=self.folder/'manifest.json';p.write_bytes(canonical(self.document));p.chmod(0o600)

    def test_p_visible_before_any_A_with_table_and_no_execution_claim(self):
        report=self.views.get(self.task['id'])
        self.assertFalse(report['execution_authorized']);self.assertEqual(report['scientific_status'],'not_evaluated')
        self.assertEqual(report['views'][0]['tables'][0]['rows'],[['0','1'],['1','2']])
        self.assertEqual(self.views.download(self.task['id'],'points.csv'),self.contents['points.csv'])
        self.assertNotIn('source.json',[f['name'] for f in report['files']])
        with self.assertRaises(ResultUnavailable):self.views.download(self.task['id'],'source.json')

    def test_actual_http_projection_is_not_stored_or_exported_as_B_input(self):
        reply=self.client.get('/api/tasks/'+self.task['id']);self.assertEqual(reply.status_code,200)
        self.assertEqual(reply.json()['paper_evidence']['doi'],self.paper['doi'])
        self.assertNotIn('paper_evidence',self.store.get(self.task['id']))
        url='/api/tasks/'+self.task['id']+'/paper-evidence'
        self.assertEqual(self.client.get(url).status_code,200)
        self.assertEqual(self.client.get(url+'/files/points.csv').content,self.contents['points.csv'])
        self.assertEqual(self.client.get(url+'/files/source.json').status_code,409)
        self.assertEqual(self.client.get(url+'/files/unregistered.csv').status_code,409)
        self.assertNotIn('paper_evidence',self.store.get(self.task['id']))

    def test_user_imports_only_registered_inventory_then_selects_same_revision(self):
        base='/api/tasks/'+self.task['id']
        reply=self.client.post(base+'/paper-evidence/import-targets',json={'revision':self.task['revision']},headers=HEADERS)
        self.assertEqual(reply.status_code,200,reply.text);doc=reply.json()
        self.assertEqual(doc['target_inventory']['paper']['doi'],self.paper['doi'])
        self.assertNotIn('target_selection',doc)
        reply=self.client.post(base+'/targets',json={'revision':doc['revision'],'selected_ids':[doc['target_inventory']['targets'][0]['id']],'exclusion_reason':''},headers=HEADERS)
        self.assertEqual(reply.status_code,200,reply.text)
        self.assertIsNotNone(self.client.get(base).json()['paper_evidence'])
        forged=self.client.post(base+'/paper-evidence/import-targets',json={'revision':reply.json()['revision'],'inventory':self.document['target_inventory']},headers=HEADERS)
        self.assertEqual(forged.status_code,422)

    def test_frozen_history_not_retroactively_registered(self):
        doc=self.store.import_target_inventory(self.task['id'],self.task['revision'],self.document['target_inventory'])
        for field in FIELDS:doc=self.store.add_candidate(doc['id'],doc['revision'],field,evidence())
        doc=self.store.confirm(doc['id'],doc['revision'],list(FIELDS))
        doc=self.store.select_targets(doc['id'],doc['revision'],[doc['target_inventory']['targets'][0]['id']],'')
        doc=self.store.freeze(doc['id'],doc['revision'])
        reply=self.client.post('/api/tasks/'+doc['id']+'/paper-evidence/import-targets',json={'revision':doc['revision']},headers=HEADERS)
        self.assertEqual(reply.status_code,409)
        self.assertEqual(self.store.get(doc['id'])['revision'],doc['revision'])
        self.assertIsNotNone(self.client.get('/api/tasks/'+doc['id']).json()['paper_evidence'])

    def test_finished_or_deleted_task_cannot_import_more_targets(self):
        for action in ('finish', 'delete'):
            other=self.store.create('Ended history', 'No new computation', 'reproduction')
            self.store.manage_lifecycle(other['id'],other['revision'],0,action)
            reply=self.client.post('/api/tasks/'+other['id']+'/paper-evidence/import-targets',
                json={'revision':other['revision']},headers=HEADERS)
            self.assertEqual(reply.status_code,422)
            self.assertEqual(self.store.get(other['id'])['revision'],other['revision'])

    def test_missing_P_cell_is_preserved_not_interpolated(self):
        content=b'x,y\n0,\n1,2\n'
        (self.folder/'points.csv').write_bytes(content)
        item=next(f for f in self.document['files'] if f['name']=='points.csv')
        item.update(size=len(content),sha256=sha256(content));self.save()
        report=self.views.get(self.task['id'])
        self.assertEqual(report['views'][0]['tables'][0]['rows'],[['0',''],['1','2']])
        self.assertEqual(self.views.download(self.task['id'],'points.csv'),content)

    def test_source_bytes_identity_hash_and_role_fail_closed(self):
        original=deepcopy(self.document)
        changes=[lambda d:d.update(doi='10.1234/other'),lambda d:d.update(source_sha256='a'*64),
            lambda d:d['views'][0]['figures'][0].update(role='agent'),lambda d:d['files'][1].update(name='../outside.png'),
            lambda d:d['target_inventory']['paper'].update(title='Wrong source')]
        for change in changes:
            self.document=deepcopy(original);change(self.document);self.save()
            reply=self.client.get('/api/tasks/'+self.task['id']+'/paper-evidence');self.assertEqual(reply.status_code,409)
        self.document=original;self.save();(self.folder/'source.json').write_bytes(b'{}')
        self.assertEqual(self.client.get('/api/tasks/'+self.task['id']+'/paper-evidence').status_code,409)
        self.assertIn('paper_evidence_error',self.client.get('/api/tasks/'+self.task['id']).json())
        self.assertEqual(self.store.get(self.task['id'])['revision'],self.task['revision'])

    def test_symlink_and_cross_task_files_are_not_read(self):
        file=self.folder/'original.png';file.unlink();outside=Path(self.tmp.name).resolve()/'outside';outside.write_bytes(b'synthetic-image');outside.chmod(0o600);file.symlink_to(outside)
        with self.assertRaises((ExecutionDenied,OSError)):self.views.get(self.task['id'])
        self.assertEqual(self.client.get('/api/tasks/'+self.task['id']+'/paper-evidence').status_code,409)
        other=self.store.create('Other user task','No evidence','reproduction')
        self.assertIsNone(self.views.get(other['id']))
        self.assertNotIn('paper_evidence',self.client.get('/api/tasks/'+other['id']).json())
