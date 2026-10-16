"""Author collector to ordinary raw/AI-source HTTP, synthetic bytes only."""
import json
import os
from pathlib import Path
import struct
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from auto_lammps.author_dispatch import AuthorOutputCollector
from auto_lammps.author_reference_plan import validate_author_plan
from auto_lammps.ledger import Ledger, Policy
from auto_lammps.manifest import canonical, sha256
from auto_lammps.output_analysis import OutputAnalysis
from auto_lammps.papers import PaperStore
from auto_lammps.raw_outputs import RawOutputs
from auto_lammps.staging import StageEndpoint
from auto_lammps.tasks import TaskError
from auto_lammps.web import create_app
import test_author_execution_package as package_fixtures
from test_web import ORIGIN


class AuthorRawOutputTests(unittest.TestCase):
    def setUp(self):
        self.fx=package_fixtures.AuthorExecutionPackageTests()
        self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.root=self.fx.fixture.fixture.root.resolve()
        self.tasks=self.fx.fixture.tasks;self.task=self.fx.fixture.task['id']
        self.fx.raw['invocations'][0].update(working_directory='workflow',argv=['-in','example.txt'])
        self.fx.report=validate_author_plan(self.fx.evidence,self.fx.raw)
        compiled=self.fx.compile();self.package=compiled['package'];self.digest=compiled['package_sha256']
        self.snapshots=self.root/'raw-author-snapshots';self.snapshots.mkdir(mode=0o700)
        folder=self.snapshots/self.digest;folder.mkdir(mode=0o700)
        self.private(folder/'author-package.json',canonical(self.package))
        self.ledger=Ledger(self.root/'author-raw-ledger.sqlite')
        self.ledger.create_campaign('author-raw-synthetic',Policy(1000000,32,7*86400,
            8*1024**3,64*1024**2,1,'1'*64))
        self.evaluation=self.ledger.register_evaluation('author-raw-synthetic',
            task_sha256=self.package['provenance']['task_sha256'],repetition=0,
            role='reference',system_sha256='2'*64)
        self.papers=PaperStore(self.tasks,ledger=self.ledger)
        self.papers.bind_reference_evaluation(self.fx.fixture.paper['id'],self.task,self.evaluation)
        row=self.ledger.reserve(self.evaluation,'synthetic-completed-author',self.digest,self.fx.resources)
        self.request=row['id']
        self.ledger.begin_dispatch(self.request)
        self.ledger.accepted(self.request,'123',{'synthetic':True})
        self.ledger.observe(self.request,'123','completed',{'synthetic':True})
        self.ledger.account(self.request,1,'3'*64)
        self.payload={
            'execution-intent.json':canonical(dict(request_id=self.request,manifest_sha256=self.digest,
                author_package_sha256=self.digest,job_id='123',role='author_reference',
                outputs=['stdout.txt','stderr.txt','workflow/values.csv'])),
            'execution-result.json':canonical(dict(request_id=self.request,job_id='123',returncode=0,
                timed_out=False,scientific_status='not_evaluated',role='author_reference')),
            'output/stdout.txt':b'# Synthetic original process output\r\n',
            'output/stderr.txt':b'',
            'output/workflow/values.csv':b'value\r\n1\r\n2\r\n'}
        self.header=dict(schema_version=1,request_id=self.request,manifest_sha256=self.digest,job_id='123',
            execution=dict(state='finished',returncode=0,timed_out=False),scientific_status='not_evaluated',
            missing_outputs=[],files=[dict(path=name,size=len(data),sha256=sha256(data))
                                      for name,data in self.payload.items()])
        self.collections=self.root/'author-collected'
        self.transfer=Mock(side_effect=self.synthetic_transfer)
        endpoint=StageEndpoint('synthetic-hpc','/usr/bin/python3','/synthetic/runtime_launcher.py',
                              '4'*64,'/synthetic/requests')
        self.collector=AuthorOutputCollector(self.ledger,endpoint,self.collections,
            snapshots=self.snapshots,transfer_call=self.transfer)
        # The real framing receiver, payload verifier and ledger finish are
        # exercised. Only the external process boundary is replaced.
        self.receipt=self.collector.fetch(self.request)
        self.assertEqual(self.receipt['state'],'collected',self.receipt)
        self.raw=RawOutputs(self.tasks,self.papers,collections=self.collections)
        self.model=Mock(side_effect=AssertionError('No model request belongs in a raw-source GET'))
        self.analysis=OutputAnalysis(self.raw,connection_provider=self.model,reference_sources=None)
        self.http=TestClient(create_app(self.tasks,papers=self.papers,collections_directory=self.collections,
            output_analysis_service=self.analysis),base_url=ORIGIN,raise_server_exceptions=False)
        self.addCleanup(self.http.close)
        self.url='/api/tasks/'+self.task+'/raw-files'
        self.analysis_url='/api/tasks/'+self.task+'/output-analyses'

    @staticmethod
    def private(path,data):
        path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        if path.exists():path.chmod(0o600)
        path.write_bytes(data);path.chmod(0o400)

    def synthetic_transfer(self,argv,receiver,**options):
        self.assertEqual(argv[0],'ssh')
        self.assertIn('--author-reference',argv[-1]);self.assertIn('--collect',argv[-1])
        self.assertNotIn('sbatch',argv[-1])
        header=canonical(self.header)
        wire=struct.pack('!I',len(header))+header+b''.join(self.payload.values())
        for offset in range(0,len(wire),113):receiver.feed(wire[offset:offset+113])
        return dict(returncode=0,failure='',stdout_bytes=len(wire),stdout_sha256=sha256(wire),stderr='')

    def test_HTTP_nested_original_listing_download_and_AI_source_are_reference_only(self):
        before=self.ledger.events(self.request)
        response=self.http.get(self.url)
        self.assertEqual(response.status_code,200,response.text)
        files=response.json()['files']
        self.assertEqual({item['name'] for item in files},{'stdout.txt','stderr.txt','workflow/values.csv'})
        self.assertTrue(all(item['source']=='accounted_collection' for item in files))
        self.assertTrue(all(item['request_id']==self.request and item['job_id']=='123' for item in files))
        self.assertNotIn(str(self.root),response.text)
        for item in files:
            downloaded=self.http.get(self.url+'/'+item['id'])
            self.assertEqual(downloaded.status_code,200,downloaded.text)
            self.assertEqual(downloaded.content,self.payload['output/'+item['name']])
            self.assertEqual(sha256(downloaded.content),item['sha256'])
        visible=self.http.get(self.analysis_url)
        self.assertEqual(visible.status_code,200,visible.text)
        run,=visible.json()['runs']
        self.assertEqual((run['request_id'],run['job_id'],run['role']),(self.request,'123','reference'))
        self.assertEqual({item['name'] for item in run['files']},{item['name'] for item in files})
        evidence,source_bytes=self.analysis._input(self.task,self.request,'Inspect synthetic retained outputs')
        self.assertEqual(evidence['role'],'reference')
        self.assertEqual(evidence['manifest_sha256'],self.digest)
        self.assertEqual(evidence['scientific_status'],'not_evaluated')
        self.assertEqual({item['locator'] for item in evidence['sources'] if item['origin']=='output'},
                         {item['name'] for item in files})
        self.assertEqual(list(source_bytes.values()),[self.payload['output/'+item['name']]
                                                    for item in sorted(files,key=lambda item:item['name'])])
        self.model.assert_not_called();self.assertEqual(self.transfer.call_count,1)
        self.assertEqual(self.ledger.events(self.request),before)

    def test_author_receipt_is_rejected_if_ledger_role_becomes_agent(self):
        snapshot=self.ledger.evaluation_snapshot
        def agent_role(*args,**kwargs):
            result=snapshot(*args,**kwargs);result['identity']['role']='agent';return result
        with patch.object(self.ledger,'evaluation_snapshot',side_effect=agent_role):
            with self.assertRaisesRegex(TaskError,'参考账本'):
                self.raw.listing(self.task)
            with self.assertRaises(TaskError):self.analysis._input(self.task,self.request,'No reference answers')
        # The HTTP instance constructs its own RawOutputs, sharing the actual
        # ledger path. Patch the ledger method, rather than changing identity.
        original=Ledger.evaluation_snapshot
        def HTTP_agent_role(ledger,*args,**kwargs):
            result=original(ledger,*args,**kwargs);result['identity']['role']='agent';return result
        with patch.object(Ledger,'evaluation_snapshot',HTTP_agent_role):
            self.assertEqual(self.http.get(self.url).status_code,409)
            self.assertEqual(self.http.get(self.analysis_url).status_code,409)
        self.model.assert_not_called();self.assertEqual(self.transfer.call_count,1)

    def test_another_task_cannot_download_or_supply_author_source_to_AI(self):
        item=self.raw.listing(self.task)['files'][0]
        other=self.tasks.create('Synthetic independent research','No reference source permitted','research')
        url='/api/tasks/'+other['id']
        self.assertEqual(self.http.get(url+'/raw-files').json()['files'],[])
        self.assertEqual(self.http.get(url+'/raw-files/'+item['id']).status_code,409)
        self.assertEqual(self.http.get(url+'/output-analyses').json()['runs'],[])
        with self.assertRaises(TaskError):self.analysis._input(other['id'],self.request,'Independent synthetic input')
        self.model.assert_not_called()

    def test_changed_receipt_and_original_byte_hash_block_download_and_AI_input(self):
        item=next(row for row in self.raw.listing(self.task)['files'] if row['name'].endswith('.csv'))
        folder=Path(self.receipt['directory'])
        path=folder/'payload/output/workflow/values.csv'
        self.private(path,b'value\r\n1\r\n9\r\n')
        self.assertEqual(self.http.get(self.url+'/'+item['id']).status_code,409)
        with self.assertRaisesRegex(TaskError,'原始文件摘要不符'):
            self.analysis._input(self.task,self.request,'Cannot consume changed bytes')
        proof=folder/'receipt.json';self.private(proof,b'{}')
        self.assertEqual(self.http.get(self.url).status_code,409)
        self.assertEqual(self.http.get(self.analysis_url).status_code,409)
        self.model.assert_not_called();self.assertEqual(self.transfer.call_count,1)

    def test_private_regular_file_and_no_follow_checks_still_guard_nested_downloads(self):
        item=next(row for row in self.raw.listing(self.task)['files'] if row['name'].endswith('.csv'))
        path=Path(self.receipt['directory'])/'payload/output/workflow/values.csv'
        raw=path.read_bytes();outside=self.root/'outside-synthetic.csv';self.private(outside,raw)
        path.unlink();path.symlink_to(outside)
        self.assertEqual(self.http.get(self.url+'/'+item['id']).status_code,409)
        path.unlink();os.link(outside,path)
        self.assertEqual(self.http.get(self.url+'/'+item['id']).status_code,409)
        path.unlink();self.private(path,raw);path.chmod(0o644)
        self.assertEqual(self.http.get(self.url+'/'+item['id']).status_code,409)
        self.model.assert_not_called()

    def test_unaccounted_or_nonterminal_author_request_is_not_an_AI_source(self):
        item=self.raw.listing(self.task)['files'][0]
        for state,accounted in [('running',0),('completed',0),('reconcile_required',1)]:
            with self.subTest(state=state,accounted=accounted):
                with self.ledger._transaction() as db:
                    db.execute('UPDATE requests SET state=?,accounted=? WHERE id=?',(state,accounted,self.request))
                self.assertEqual(self.http.get(self.url).json()['files'],[])
                self.assertEqual(self.http.get(self.analysis_url).json()['runs'],[])
                self.assertEqual(self.http.get(self.url+'/'+item['id']).status_code,409)
                with self.assertRaises(TaskError):self.analysis._input(self.task,self.request,'Unverified state')
        self.model.assert_not_called()

    def test_author_receipt_uses_bounded_extended_reader_without_changing_agent_protocol(self):
        from auto_lammps import raw_outputs as raw_module
        from auto_lammps import author_dispatch
        with patch.object(raw_module,'read_regular',wraps=raw_module.read_regular) as read, \
                patch.object(author_dispatch,'validate_author_header',wraps=author_dispatch.validate_author_header) as validate:
            self.raw.listing(self.task)
        self.assertEqual(read.call_args.args[1],2*1024**2)
        validate.assert_called_once()
        proof=Path(self.receipt['directory'])/'receipt.json'
        self.assertEqual(sha256(proof.read_bytes()),
            json.loads([event for event in self.ledger.events(self.request)
                        if event['kind']=='output_fetch_finished'][-1]['payload'])['evidence_sha256'])


if __name__=='__main__':unittest.main()
