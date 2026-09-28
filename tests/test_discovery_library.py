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
                      license='MIT',evidence='Synthetic source claim',mirrors=['synthetic/mirror'])
        self.doc=dict(generated='2026-01-01',tier_a=[self.row],tier_b=[],tier_c=[])
        self.save();self.library=DiscoveryLibrary(self.file)

    def save(self):
        self.file.write_text(json.dumps(self.doc));self.file.chmod(0o600)

    def test_source_claim_never_becomes_ready(self):
        report=self.library.get();row=report['entries'][0]
        self.assertEqual(report['repository_count'],2)
        self.assertFalse(row['execution_ready'])
        self.assertEqual(row['elements'],[])
        self.assertIn('元素与原子类型映射待补充',row['gaps'])
        self.assertEqual(row['source_tier'],'tier_a')

    def test_refresh_rereads_exact_handoff(self):
        old=self.library.get()['source']['sha256']
        self.row['title']='Updated synthetic title';self.save()
        report=self.library.get()
        self.assertNotEqual(report['source']['sha256'],old)
        self.assertEqual(report['entries'][0]['title'],self.row['title'])

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
        row=library.get()['entries'][0];self.assertEqual(row['state'],'conflict');self.assertIn(review['notes'][0],row['gaps'])
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
