import json
from pathlib import Path
import tempfile
import unittest
from fastapi.testclient import TestClient

from auto_lammps.discovery_library import DiscoveryLibrary
from auto_lammps.tasks import TaskStore
from auto_lammps.web import create_app


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.root.chmod(0o700)
        self.file=self.root/'handoff.json'
        self.row=dict(repo='synthetic/alloy',commit='a'*40,title='Synthetic alloy',doi='10.1234/synthetic',
                      pair_style=['meam'],potentials=['library.meam','alloy.meam'],inputs=['in.alloy'],
                      license='MIT',evidence='Synthetic source claim',mirrors=['synthetic/mirror'],
                      potential_library_elements=['Co','Ni'],type_order=['Ni','Co'],elements_complete=True)
        self.doc=dict(generated='2026-01-01',tier_a=[self.row],tier_b=[],tier_c=[])
        self.save();self.library=DiscoveryLibrary(self.file)

    def save(self):
        self.file.write_text(json.dumps(self.doc));self.file.chmod(0o600)

    def entry(self,kind,report=None,repo=None):
        report=report or self.library.get()
        return next(row for row in report['entries'] if row['kind']==kind and (repo is None or row['repository']==repo))

    def test_bundle_splits_into_potential_and_source_entries(self):
        report=self.library.get()
        self.assertEqual(len(report['entries']),2)
        self.assertEqual(sorted(row['kind'] for row in report['entries']),['author_source','potential'])
        self.assertEqual(report['row_count'],2);self.assertEqual(report['bundle_count'],1)
        self.assertEqual(report['repository_count'],2)
        ids={row['bundle_id'] for row in report['entries']}
        self.assertEqual(len(ids),1)
        for row in report['entries']:
            self.assertFalse(row['execution_ready'])
            self.assertEqual(row['paper_title'],self.row['title'])
        self.assertEqual(report['verified_execution_count'],0)

    def test_splitting_rows_never_inflates_papers_or_duplicates_reviews(self):
        self.doc['tier_a']=[self.row,dict(self.row,repo='synthetic/other',commit='b'*40,doi=self.row['doi'])]
        self.save()
        report=self.library.get()
        self.assertEqual(report['row_count'],4);self.assertEqual(report['bundle_count'],2)
        self.assertEqual(report['paper_doi_count'],1);self.assertEqual(report['verified_doi_count'],1)

    def test_source_claim_never_becomes_ready(self):
        row=self.entry('author_source')
        self.assertIn('结构/依赖闭包待核验',row['gaps'])
        self.assertNotIn('势函数文件或内建势参数依据待补充',row['gaps'])
        potential=self.entry('potential')
        self.assertIn('元素与原子类型映射待补充',potential['gaps'])
        self.assertNotIn('结构/依赖闭包待核验',potential['gaps'])

    def test_naming_follows_element_system_and_process(self):
        self.row['elements']=['Nb','Ti','Zr','Mo','V'];self.row['process']=['单轴/剪切拉伸'];self.save()
        potential=self.entry('potential');source=self.entry('author_source')
        self.assertEqual(potential['title'],'Nb—Ti—Zr—Mo—V · MEAM')
        self.assertEqual(potential['element_label'],'Nb（铌） · Ti（钛） · Zr（锆） · Mo（钼） · V（钒）')
        self.assertEqual(source['title'],'Synthetic alloy · 单轴/剪切拉伸')

    def test_builtin_analytic_potential_is_never_dressed_as_a_file(self):
        self.doc['tier_a']=[dict(self.row,potentials=[],elements=['Si'],potential_label='')];self.save()
        potential=self.entry('potential')
        self.assertEqual(potential['potential_basis'],'built_in_analytic')
        self.assertEqual(potential['title'],'内建解析势 MEAM · 参数依据待补')
        self.assertIn('势函数文件或内建势参数依据待补充',potential['gaps'])

    def test_uncovered_bundle_publishes_only_the_half_it_has(self):
        self.doc['tier_a']=[dict(self.row,potentials=[],pair_style=[])];self.save()
        self.assertEqual([r['kind'] for r in self.library.get()['entries']],['author_source'])
        self.doc['tier_a']=[dict(self.row,inputs=[])];self.save()
        self.assertEqual([r['kind'] for r in self.library.get()['entries']],['potential'])

    def test_candidate_and_artifact_dois_are_not_verified(self):
        self.doc['tier_b']=[dict(self.row,doi='10.1234/candidate')];self.doc['tier_a']=[];self.save()
        row=self.entry('potential')
        self.assertEqual(row['doi_state'],'candidate_unverified')
        self.assertIn('论文题名与 DOI 关联待核验',row['gaps'])
        self.assertEqual(self.library.get()['verified_doi_count'],0)
        self.doc['tier_b']=[dict(self.row,doi='10.11578/dc.20240701.4')];self.save()
        artifact=self.entry('potential')
        self.assertEqual(artifact['doi_state'],'artifact_or_dataset')
        self.assertIn('所附 DOI 属 artifact/数据集，不能作为论文 DOI',artifact['gaps'])
        self.assertEqual(self.library.get()['paper_doi_count'],0)

    def test_repository_role_survives_and_is_not_rewritten(self):
        self.doc['tier_a']=[dict(self.row,repository_role='validation_tests')];self.save()
        for kind in ('potential','author_source'):
            self.assertEqual(self.entry(kind)['repository_role'],'validation_tests')
        self.doc['tier_a']=[dict(self.row,repository_role='invented')];self.save()
        with self.assertRaisesRegex(ValueError,'role'):self.library.get()

    def test_element_orders_stay_separate_and_truncation_is_flagged(self):
        self.row['elements']=['Co','Ni'];self.row['elements_complete']=False;self.save()
        row=self.entry('potential')
        self.assertEqual(row['elements'],['Co','Ni'])
        self.assertEqual(row['potential_library_elements'],['Co','Ni'])
        self.assertEqual(row['type_order'],['Ni','Co'])
        self.assertFalse(row['elements_complete'])
        self.assertIn('元素映射证据被截断，未构成完整映射',row['gaps'])

    def test_external_registry_source_needs_locator_and_version(self):
        ext=dict(self.row,commit='',source={'type':'openkim','locator':'Sim_LAMMPS_MEAN_Example','version':'v1.2.3'},
                 source_url='https://openkim.org/id/Sim_LAMMPS_MEAN_Example')
        self.doc['tier_a']=[ext];self.save()
        row=self.entry('potential')
        self.assertEqual(row['source_type'],'openkim');self.assertEqual(row['repository'],'Sim_LAMMPS_MEAN_Example')
        self.assertEqual(row['commit'],'v1.2.3');self.assertEqual(row['url'],ext['source_url'])
        self.doc['tier_a']=[dict(ext,source={'type':'openkim','locator':'X'})];self.save()
        with self.assertRaisesRegex(ValueError,'version'):self.library.get()
        self.doc['tier_a']=[dict(ext,source={'type':'kdsh'})];self.save()
        with self.assertRaisesRegex(ValueError,'source type'):self.library.get()

    def test_unknown_kind_is_refused(self):
        self.doc['tier_a']=[dict(self.row,kind='mystery')];self.save()
        with self.assertRaisesRegex(ValueError,'kind'):self.library.get()

    def test_legacy_single_entry_catalog_still_reads(self):
        self.assertEqual(self.library.get()['row_count'],2)
        legacy={'generated':'2026-01-01','tier_a':[{'repo':'synthetic/alloy','commit':'a'*40,'title':'Legacy row',
                 'pair_style':['eam'],'potentials':['x.eam'],'inputs':['in.x'],'license':'MIT'}],'tier_b':[],'tier_c':[]}
        self.file.write_text(json.dumps(legacy));self.file.chmod(0o600)
        report=self.library.get()
        self.assertEqual(report['row_count'],2);self.assertEqual(report['bundle_count'],1)
        self.assertEqual(self.entry('potential',report)['title'],'元素待核 · EAM')

    def test_refresh_rereads_exact_handoff(self):
        old=self.library.get()['source']['sha256']
        self.row['title']='Updated synthetic title';self.save()
        report=self.library.get()
        self.assertNotEqual(report['source']['sha256'],old)
        self.assertEqual(self.entry('author_source',report)['paper_title'],self.row['title'])

    def test_rejects_unsafe_repository_and_unpinned_version(self):
        for key,value in [('repo','https://malicious.invalid'),('commit','main')]:
            old=self.row[key];self.row[key]=value;self.save()
            with self.assertRaises(ValueError):self.library.get()
            self.row[key]=old

    def test_duplicate_rejected(self):
        self.doc['tier_b']=[self.row];self.save()
        with self.assertRaisesRegex(ValueError,'Duplicate'):self.library.get()

    def test_reviews_can_flag_but_cannot_authorize(self):
        p=self.root/'reviews.json'
        review=dict(repository=self.row['repo'],commit=self.row['commit'],state='conflict',notes=['Synthetic title mismatch'])
        def save():p.write_text(json.dumps(dict(version=1,reviews=[review])));p.chmod(0o600)
        save();library=DiscoveryLibrary(self.file,p)
        reported=library.get()
        bundle_ids=set()
        for kind in ('potential','author_source'):
            row=self.entry(kind,reported)
            self.assertEqual(row['state'],'conflict');self.assertIn(review['notes'][0],row['gaps'])
            bundle_ids.add(row['bundle_id'])
        self.assertEqual(len(bundle_ids),1)
        review['state']='reference_ready';save()
        with self.assertRaises(ValueError):library.get()

    def test_operator_read_only_route_does_not_create_papers_or_expose_paths(self):
        tasks=TaskStore(self.root/'tasks.sqlite')
        with TestClient(create_app(tasks,discovery_library=self.library),base_url='http://127.0.0.1:8765') as client:
            before=client.get('/api/papers').json()
            response=client.get('/api/resource-discoveries')
            self.assertEqual(response.status_code,200)
            self.assertNotIn(str(self.root),response.text)
            self.assertEqual(client.get('/api/papers').json(),before)
            self.file.write_text('broken')
            self.assertEqual(client.get('/api/resource-discoveries').status_code,409)

    def test_no_configuration_is_explicit(self):
        self.assertFalse(DiscoveryLibrary().get()['configured'])
