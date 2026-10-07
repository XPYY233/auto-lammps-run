"""Public task-bound complete-array previews/downloads; only synthetic physics data."""
from copy import deepcopy
import csv
import io
import json
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from fastapi.testclient import TestClient
from auto_lammps import analysis
from auto_lammps import site_thermodynamics as site
from auto_lammps.manifest import canonical, sha256
from auto_lammps.results import ResultsReader, site_projection, site_source_preview, site_curve_preview
from auto_lammps.tasks import TaskStore
from auto_lammps.web import create_app
import test_candidate_jobs as task_fixtures
import test_results as result_fixtures
import test_site_thermodynamics as site_fixtures


class SiteResultsTests(unittest.TestCase):
    def setUp(self):
        self.pipeline=site_fixtures.CollectedSitePipelineTests()
        self.addCleanup(self.pipeline.doCleanups)
        # A private free-text provenance path must not reach public projections.
        with patch.dict(site_fixtures.OPERATION['reservoir_anchor'],source='/private/synthetic/reservoir-note'):
            self.pipeline.prepare();self.saved=self.pipeline.analyze()
        self.assertEqual(self.saved['report']['status'],'analyzed')
        self.f=self.pipeline.f;self.root=self.pipeline.root
        self.tasks=TaskStore(self.root/'tasks.sqlite')
        self.doc=task_fixtures.frozen_research(self.tasks)
        result_fixtures.link_synthetic_candidate(self.tasks,self.doc,self.pipeline.snapshot.digest)
        self.reader=ResultsReader(self.tasks,self.f.ledger,self.root/'collected',self.root/'reports')
        self.client=TestClient(create_app(self.tasks,results_reader=self.reader),base_url=result_fixtures.ORIGIN)
        self.addCleanup(self.client.close)
        self.url='/api/tasks/'+self.doc['id']+'/results'
        self.aid=self.saved['context']['analysis_id']
        self.item=self.saved['report']['site_thermodynamic_results'][0]

    def test_complete_public_summaries_previews_and_downloads_are_read_only(self):
        before=self.f.ledger.events(self.f.request_id)
        with ExitStack() as stack:
            for method in ('analyze','evaluate','parse_array','validate_array'):
                stack.enter_context(patch.object(site,method,side_effect=AssertionError('GET must not analyze')))
            response=self.client.get(self.url)
            report=response.json()['evaluations'][0]['requests'][0]['reports'][0]
            self.assertEqual(report['status'],'analyzed')
            self.assertEqual(report['analysis_format'],'numeric_tables_v4')
            public=report['site_thermodynamic_results'][0]
            self.assertEqual(public['coverage'],self.item['coverage'])
            self.assertEqual(public['temperature_summaries'],self.item['temperature_summaries'])
            self.assertEqual(public['scientific_status'],'not_evaluated')
            self.assertFalse(public['physics_simulation'])
            preview=self.client.get(self.url+'/'+self.aid+'/tables')
            self.assertEqual(preview.status_code,200,preview.text)
            table=preview.json()['tables'][0]
            self.assertEqual(table['format'],site.FORMAT)
            self.assertEqual((table['total_rows'],table['source_lines'][0]),(32,3))
            curve=preview.json()['site_previews'][0]['curves']
            self.assertEqual(curve['total_rows'],32)
            self.assertFalse(curve['sampled'])
            for receipt in self.item['derived_files']:
                result=self.client.get(self.url+'/'+self.aid+'/derived/'+receipt['name'])
                self.assertEqual(result.status_code,200,result.text)
                self.assertEqual(sha256(result.content),receipt['sha256'])
                self.assertEqual(len(list(csv.reader(io.StringIO(result.text))))-1,receipt['rows'])
                self.assertIn(receipt['name'],result.headers['content-disposition'])
            for value in (response.text,preview.text):
                for hidden in ('/private/synthetic',str(self.root),'adapter_identity','storage_scope_sha256'):
                    self.assertNotIn(hidden,value)
        self.assertEqual(before,self.f.ledger.events(self.f.request_id))

    def test_cross_task_foreign_name_and_path_downloads_are_denied(self):
        other=task_fixtures.frozen_research(self.tasks)
        url='/api/tasks/'+other['id']+'/results/'+self.aid+'/derived/'+self.item['derived_files'][0]['name']
        self.assertEqual(self.client.get(url).status_code,409)
        for name in ('other-curves.csv','arbitrary.csv','../data.csv','/tmp/result.csv'):
            with self.subTest(name=name),self.assertRaises(ValueError):
                self.reader.derived(self.doc['id'],self.aid,name)

    def test_changed_source_receipt_report_and_csv_reject_download(self):
        paths=[self.root/'reports'/(self.aid+'.json'),
               next((self.root/'collected').glob('*/receipt.json')),
               next((self.root/'collected').glob('*/payload/output/'+self.item['file'])),
               self.root/'reports'/self.aid/self.item['derived_files'][0]['name']]
        url=self.url+'/'+self.aid+'/derived/'+self.item['derived_files'][0]['name']
        for path in paths:
            raw=path.read_bytes();path.chmod(0o600)
            with self.subTest(name=path.name):
                path.write_bytes(b'changed\n')
                self.assertEqual(self.client.get(url).status_code,409)
                path.write_bytes(raw)

    def test_symlinked_csv_or_directory_is_never_followed(self):
        receipt=self.item['derived_files'][0]
        path=self.root/'reports'/self.aid/receipt['name'];raw=path.read_bytes()
        other=self.root/'unselected.csv';other.write_bytes(raw);other.chmod(0o600)
        path.unlink();path.symlink_to(other)
        self.assertEqual(self.client.get(self.url+'/'+self.aid+'/derived/'+receipt['name']).status_code,409)
        path.unlink();path.write_bytes(raw);path.chmod(0o600)
        folder=path.parent;renamed=folder.with_name(folder.name+'-retained');folder.rename(renamed);folder.symlink_to(renamed,target_is_directory=True)
        self.assertEqual(self.client.get(self.url+'/'+self.aid+'/derived/'+receipt['name']).status_code,409)
        folder.unlink();renamed.rename(folder)

    def test_self_hashed_semantic_tamper_does_not_establish_provenance(self):
        for kind in ('coverage','summary','source','columns','rows','name','parameters','equations'):
            value=deepcopy(self.item)
            if kind=='coverage':value['coverage']['rows']-=1
            if kind=='summary':value['temperature_summaries'][0]['model']='unknown'
            if kind=='source':value['derived_files'][0]['source_sha256']='b'*64
            if kind=='columns':value['derived_files'][0]['columns'][0]['name']='private_path'
            if kind=='rows':value['derived_files'][0]['rows']-=1
            if kind=='name':value['derived_files'][0]['name']='../private.csv'
            if kind=='parameters':value['derived_files'][0]['parameters_sha256']='c'*64
            if kind=='equations':value['equations']['anchor']='arbitrary'
            value['derived_sha256']=sha256(canonical({k:v for k,v in value.items() if k!='derived_sha256'}))
            with self.subTest(kind=kind),self.assertRaises(ValueError):
                site_projection(value,self.item['source'])

    def test_reservation_and_scheduler_contradiction_hide_saved_values(self):
        # Even matching forged report/event proofs cannot remove the full CSV reservation.
        request=deepcopy(self.f.ledger.product_results(self.pipeline.snapshot.digest)[0]['requests'][0])
        event=next(e for e in request['events'] if e['kind']=='analysis_saved')
        reserved=next(e for e in request['events'] if e['kind']=='analysis_reserved')
        value=deepcopy(self.saved);value['context']['storage_bytes']=65536
        raw=canonical(value);path=self.root/'reports'/(self.aid+'.json');original=path.read_bytes()
        proof=json.loads(event['payload']);proof['evidence_sha256']=sha256(raw)
        event['payload']=json.dumps(proof);reserved['payload']=json.dumps(value['context'])
        path.chmod(0o600);path.write_bytes(raw)
        try:
            with self.assertRaises(ValueError):self.reader._report(request,event,request['events'])
        finally:path.write_bytes(original)
        with self.f.ledger._transaction() as db:
            db.execute("UPDATE requests SET state='reconcile_required' WHERE id=?",(self.f.request_id,))
        response=self.client.get(self.url)
        report=response.json()['evaluations'][0]['requests'][0]['reports'][0]
        self.assertEqual(report['status'],'unavailable')
        self.assertNotIn('site_thermodynamic_results',report)
        self.assertEqual(self.client.get(self.url+'/'+self.aid+'/derived/'+self.item['derived_files'][0]['name']).status_code,409)

    def test_site_preview_does_not_inherit_legacy_byte_or_cell_limits(self):
        # Site input has more cells/bytes than a deliberately narrow legacy cap.
        with patch.object(analysis,'MAX_TABLE_BYTES',1):
            preview=self.client.get(self.url+'/'+self.aid+'/tables')
        self.assertEqual(preview.status_code,200,preview.text)
        rows=site_fixtures.synthetic_rows()*10
        raw=site_fixtures.encoded(rows)
        source=dict(site_fixtures.TABLE,sha256=sha256(raw),size=len(raw))
        value=site_source_preview(raw,source)
        self.assertEqual((value['total_rows'],len(value['rows'])),(320,128))
        self.assertEqual((value['source_lines'][0],value['source_lines'][-1]),(3,322))
        # Corruption outside the sampled rows must still be rejected.
        unsampled=next(i for i in range(320) if i+3 not in value['source_lines'])
        lines=raw.splitlines(keepends=True);tokens=lines[unsampled+2].split();tokens[4]=b'1e999'
        lines[unsampled+2]=b' '.join(tokens)+b'\n';bad=b''.join(lines)
        with self.assertRaises(ValueError):site_source_preview(bad,source)
        with patch.object(site,'MAX_ARRAY_CELLS',100),self.assertRaises(ValueError):
            site_source_preview(raw,source)

    def test_curve_preview_retains_each_model_and_grid_endpoints(self):
        receipt=self.item['derived_files'][-1];raw=site.read_derived(self.root/'reports'/self.aid,receipt)
        op=deepcopy(self.item['parameters']);op['beta_grid']['count']=200
        rows=list(csv.reader(io.StringIO(raw.decode())))
        expanded=[rows[0]]
        for model in op['models']:
            expanded.extend(r for r in rows[1:] if r[0]=='temperature' and r[2]==model)
            template=next(r for r in rows[1:] if r[0]=='beta' and r[2]==model)
            expanded.extend([[template[0],str(index),*template[2:]] for index in range(200)])
        stream=io.StringIO();csv.writer(stream,lineterminator='\n').writerows(expanded)
        receipt={**receipt,'rows':len(expanded)-1}
        value=site_curve_preview(stream.getvalue().encode(),receipt,op)
        for model in op['models']:
            indices=[r[1] for r in value['rows'] if r[0]=='beta' and r[2]==model]
            self.assertEqual((len(indices),indices[0],indices[-1]),(128,0,199))
        self.assertTrue(value['sampled'])

    def test_legacy_numeric_report_download_remains_unchanged(self):
        old=result_fixtures.ResultsTests();old.setUp();self.addCleanup(old.doCleanups)
        saved=old.analysis.run_analysis();aid=saved['context']['analysis_id']
        report=old.client.get(old.url+'/'+aid+'/download')
        self.assertEqual(report.status_code,200)
        self.assertEqual(report.json()['results'][2]['values']['slope'],2)
        self.assertNotIn('site_thermodynamic_results',report.json())


if __name__=='__main__':unittest.main()
