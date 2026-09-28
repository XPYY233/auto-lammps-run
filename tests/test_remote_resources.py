"""Synthetic remote acquisition and recovery; no network or physics."""
import base64
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from auto_lammps.reference_resource_worker import acquire, PublicGitHubReader
from auto_lammps.remote_resources import RemoteResources, tool_bundle
from auto_lammps.potential_acquisition import AcquisitionError, MeamAcquisition, prepare_paper_resources
from auto_lammps.papers import PaperStore
from auto_lammps.tasks import TaskStore
from test_potential_acquisition import Reader, INPUT
from test_source_discovery import Reader as SourceReader, SourceDiscovery, TITLE, DOI, NAME, COMMIT


class RemoteResourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.reader=Reader()
        self.policy=dict(host_alias='example-hpc',remote_directory=str(self.root/'hpc'),
                         operation_id='c'*32,storage_bytes=512*1024**2)

    def capture(self,command,**options):
        self.assertEqual(command[0],'ssh');self.assertIn('StrictHostKeyChecking=yes',command)
        envelope=json.loads(b''.join(options['input_chunks']))
        bundle=base64.b64decode(envelope['bundle'])
        with zipfile.ZipFile(io.BytesIO(bundle)) as z:
            self.assertIn('third_party/SIGA-LAMMPS-LICENSE.txt',z.namelist())
            self.assertNotIn('input.lmp',z.namelist())
        report=acquire(envelope['request'],transport=self.reader)
        return dict(returncode=0,failure='',stdout=base64.b64encode(json.dumps(report).encode()).decode(),stderr='')

    def service(self,capture=None):
        return RemoteResources(self.root/'audit',self.policy,capture=capture or self.capture)

    def test_source_and_models_remote_only_with_reused_static_checks(self):
        service=self.service();report=service.acquire(NAME,COMMIT)
        self.assertEqual(report['state'],'finished');self.assertTrue(report['source_complete'])
        self.assertEqual(len(report['files']),4)
        self.assertEqual(report['request_count'],6)
        self.assertEqual(len(report['potential_acquisition']['files']),2)
        self.assertEqual((Path(report['remote_directory'])/'source/input.lmp').read_bytes(),INPUT)
        self.assertFalse(list(service.root.rglob('*.model')))
        for path in service.root.rglob('*'):
            if path.is_file():
                self.assertNotIn(base64.b64encode(INPUT).decode(),path.read_text())
                self.assertNotIn(INPUT.decode(),path.read_text())
        self.assertEqual(service.acquire(NAME,COMMIT),report)
        self.assertEqual(len(self.reader.calls),6)

    def test_uncertain_transport_reuses_remote_result(self):
        def lost(*a,**kw):
            self.capture(*a,**kw)
            return dict(returncode=None,failure='timeout',stdout='',stderr='')
        self.assertEqual(self.service(lost).acquire(NAME,COMMIT)['state'],'unknown')
        self.assertEqual(self.service().acquire(NAME,COMMIT)['state'],'finished')
        self.assertEqual(len(self.reader.calls),6)

    def test_truncated_tree_and_symbolic_links_never_claim_complete(self):
        self.reader.truncated=True
        result=self.service().acquire(NAME,COMMIT)
        self.assertEqual(result['state'],'partial');self.assertFalse(result['source_complete'])
        self.assertEqual(result['files'],[])
        self.policy['operation_id']='d'*32;self.reader.truncated=False;self.reader.mode='120000'
        result=self.service().acquire(NAME,COMMIT)
        self.assertFalse(result['source_complete']);self.assertEqual(result['files'],[])

    def test_corruption_and_changed_operation_binding_retained(self):
        self.reader.corrupt=True
        result=self.service().acquire(NAME,COMMIT)
        self.assertEqual(result['state'],'partial');self.assertFalse(result['source_complete'])
        with self.assertRaisesRegex(AcquisitionError,'already_bound'):
            self.service().acquire(NAME,'f'*40)

    def test_pending_remote_lock_never_restarts(self):
        def pending(command,**options):
            request=json.loads(b''.join(options['input_chunks']))['request']
            root=Path(request['directory']);root.mkdir(mode=0o700)
            (root/(request['id']+'.lock.json')).write_text(json.dumps(request))
            return self.capture(command,**options)
        self.assertEqual(self.service(pending).acquire(NAME,COMMIT)['state'],'unknown')
        self.assertEqual(self.reader.calls,[])

    def test_production_local_acquisition_and_foreign_endpoints_rejected(self):
        with self.assertRaises(AcquisitionError):MeamAcquisition(self.root/'local')
        reader=PublicGitHubReader(NAME,self.root,transport=self.reader)
        with self.assertRaises(AcquisitionError):reader.get('repos/another/repo/git/blobs/'+'a'*40)
        self.assertEqual(self.reader.calls,[])
        with patch('auto_lammps.reference_resource_worker.sys.platform','darwin'),patch('urllib.request.build_opener') as network:
            with self.assertRaises(AcquisitionError):reader=PublicGitHubReader(NAME,self.root);reader.get('repos/'+NAME+'/git/blobs/'+'a'*40)
            network.assert_not_called()

    def test_real_entry_records_paper_history_and_remote_model_location(self):
        papers=PaperStore(TaskStore(self.root/'tasks.sqlite'))
        paper=papers.add(TITLE,DOI,'Full synthetic task','Synthetic')
        source=SourceDiscovery(self.root/'discovery',reader=SourceReader()).discover(TITLE,DOI)
        papers.record_source_search(paper['id'],source)
        for _ in range(2):
            prepare_paper_resources(papers,paper['id'],self.root/'flow',policy=self.policy,capture=self.capture)
        saved=papers.get(paper['id'])
        self.assertEqual(saved['reference_resources']['location'],'hpc')
        self.assertEqual(saved['potential_acquisition']['location'],'hpc')
        self.assertEqual(len([e for e in saved['history'] if e['event'].startswith('reference_resources_prepared:')]),1)
        self.assertEqual(len([e for e in saved['history'] if e['event'].startswith('potential_acquired:')]),1)
        self.assertEqual(saved['status'],'pending')
