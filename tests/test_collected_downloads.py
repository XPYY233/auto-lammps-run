"""Accounted synthetic collections become HTTP downloads without publication."""
from contextlib import ExitStack
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from auto_lammps.manifest import canonical
from auto_lammps.papers import PaperStore
from auto_lammps.raw_outputs import RawOutputs
from auto_lammps.results import ResultsReader
from auto_lammps.tasks import TaskStore
from auto_lammps.web import create_app
import test_analysis_v2 as native
DATA=native.DATA
from test_candidate_jobs import frozen_research
from test_results import link_synthetic_candidate, ORIGIN
import test_runtime_launcher as runtime


class CollectedDownloadsTests(unittest.TestCase):
    def setUp(self):
        self.pipeline=native.NativePipelineTests();self.pipeline.prepare();self.addCleanup(self.pipeline.doCleanups)
        self.f=self.pipeline.f;self.root=self.f.root
        self.tasks=TaskStore(self.root/'tasks.sqlite');self.doc=frozen_research(self.tasks)
        link_synthetic_candidate(self.tasks,self.doc,self.pipeline.snapshot.digest)
        self.papers=PaperStore(self.tasks,ledger=self.f.ledger)
        self.reader=ResultsReader(self.tasks,self.f.ledger,self.root/'collected',self.root/'reports')
        # Test the existing results-reader-only configuration too (no PaperStore ledger passed).
        self.client=TestClient(create_app(self.tasks,results_reader=self.reader),base_url=ORIGIN)
        self.addCleanup(self.client.close)
        self.url='/api/tasks/'+self.doc['id']+'/raw-files'

    def collect(self):
        with self.f.local_transfer():return self.f.collector.fetch(self.f.request_id)

    def files(self):
        response=self.client.get(self.url)
        self.assertEqual(response.status_code,200,response.text)
        self.assertNotIn(str(self.root),response.text)
        return response.json()['files']

    def test_downloads_appear_after_collection_without_analysis_or_manual_publish(self):
        self.assertEqual(self.files(),[]);self.collect()
        before=self.f.ledger.events(self.f.request_id);usage=self.f.ledger.get(self.f.request_id)
        with ExitStack() as stack:
            for target in ('auto_lammps.outputs.OutputCollector.fetch','auto_lammps.outputs.transfer',
                           'auto_lammps.raw_outputs.RawOutputs.publish','auto_lammps.ledger.Ledger.begin_dispatch'):
                stack.enter_context(patch(target,side_effect=AssertionError('Read only')))
            files=self.files();again=self.files();self.assertEqual(files,again)
            self.assertEqual({f['name'] for f in files},set(runtime.OUTPUTS))
            curve=next(f for f in files if f['name']=='trajectory.dump')
            response=self.client.get(self.url+'/'+curve['id'])
            self.assertEqual(response.status_code,200,response.text);self.assertEqual(response.content,DATA)
            self.assertEqual(response.headers['content-length'],str(len(DATA)))
            self.assertIn('attachment',response.headers['content-disposition'])
        self.assertEqual(self.f.ledger.events(self.f.request_id),before)
        self.assertEqual(self.f.ledger.get(self.f.request_id),usage)
        with self.tasks.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM raw_output_files').fetchone()[0],0)
        self.assertFalse(any(e['kind']=='analysis_saved' for e in before))

    def test_restart_with_raw_directory_only_retains_identical_links(self):
        self.collect();files=self.files()
        with TestClient(create_app(self.tasks,papers=self.papers,collections_directory=self.root/'collected'),base_url=ORIGIN) as reopened:
            self.assertEqual(reopened.get(self.url).json()['files'],files)
            self.assertEqual(reopened.get(self.url+'/'+files[0]['id']).status_code,200)

    def test_raw_available_after_failed_job_and_missing_output_is_not_invented(self):
        with self.f.ledger._transaction() as db:db.execute("UPDATE requests SET state='failed' WHERE id=?",(self.f.request_id,))
        result=json.loads((self.f.case/'execution-result.json').read_bytes());result['returncode']=1
        self.f.private(self.f.case/'execution-result.json',canonical(result))
        (self.f.case/'output/trajectory.dump').unlink();self.collect()
        files=self.files();self.assertNotIn('trajectory.dump',[f['name'] for f in files])
        self.assertTrue(files);self.assertTrue(all(f['state']=='failed' for f in files))
        self.assertEqual(self.client.get(self.url+'/'+files[0]['id']).status_code,200)

    def test_cross_task_and_reference_role_are_not_exposed(self):
        self.collect();files=self.files();identifier=files[0]['id']
        other=frozen_research(self.tasks)
        self.assertEqual(self.client.get('/api/tasks/'+other['id']+'/raw-files').json()['files'],[])
        self.assertEqual(self.client.get('/api/tasks/'+other['id']+'/raw-files/'+identifier).status_code,409)
        ledger=self.f.ledger
        evaluation=ledger.register_evaluation('synthetic',task_sha256='c'*64,repetition=0,role='reference',system_sha256='d'*64)
        row=ledger.reserve(evaluation,'reference',self.pipeline.snapshot.digest,runtime.RESOURCES)
        ledger.cancel_intent(row['id'])
        self.assertEqual(self.files(),files)

    def test_changed_collection_receipt_refuses_listing_and_download(self):
        receipt=self.collect();identifier=self.files()[0]['id']
        from pathlib import Path
        p=Path(receipt['directory'])/'receipt.json';p.write_bytes(b'{}')
        self.assertEqual(self.client.get(self.url).status_code,409)
        self.assertEqual(self.client.get(self.url+'/'+identifier).status_code,409)

    def test_changed_bytes_or_link_refuses_download(self):
        receipt=self.collect();identifier=next(f['id'] for f in self.files() if f['name']=='trajectory.dump')
        from pathlib import Path
        p=Path(receipt['directory'])/'payload/output/trajectory.dump';p.chmod(0o600);p.write_bytes(DATA.replace(b'300 2 5',b'300 2 9'))
        self.assertEqual(self.client.get(self.url+'/'+identifier).status_code,409)
        p.unlink();p.symlink_to(self.f.case/'output/trajectory.dump')
        self.assertEqual(self.client.get(self.url+'/'+identifier).status_code,409)

    def test_changed_terminal_state_or_accounting_hides_original_link(self):
        self.collect();identifier=self.files()[0]['id']
        with self.f.ledger._transaction() as db:db.execute('UPDATE requests SET accounted=0 WHERE id=?',(self.f.request_id,))
        self.assertEqual(self.files(),[])
        self.assertEqual(self.client.get(self.url+'/'+identifier).status_code,409)

    def test_unfinished_transfer_does_not_expose_any_files(self):
        self.f.ledger.begin_output_fetch(self.f.request_id)
        self.assertEqual(self.files(),[])

    def test_legacy_registered_and_collected_entries_are_deduplicated(self):
        from pathlib import Path
        receipt=self.collect();curve=next(f for f in self.files() if f['name']=='trajectory.dump')
        p=self.root/'operator-receipt.json'
        p.write_bytes(canonical(dict(request_id=self.f.request_id,job_id='123',manifest_sha256=self.pipeline.snapshot.digest,
            files=[dict(name=curve['name'],size=curve['size'],sha256=curve['sha256'],kind='output')])));p.chmod(0o600)
        raw=RawOutputs(self.tasks,self.papers)
        identifier=raw.publish(self.doc['id'],self.f.request_id,curve['name'],Path(receipt['directory'])/'payload/output'/curve['name'],p)
        files=self.files();self.assertEqual(len(files),4)
        self.assertEqual(next(f['id'] for f in files if f['name']==curve['name']),identifier)
