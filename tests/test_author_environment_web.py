"""Browser environment controls never accept executable paths or run on GET."""
import tempfile
from pathlib import Path
import unittest
from fastapi.testclient import TestClient
from auto_lammps.tasks import TaskStore
from auto_lammps.web import create_app
from test_web import ORIGIN,HEADERS

class AuthorEnvironmentWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.tasks=TaskStore(Path(self.tmp.name)/'tasks.sqlite')
        self.doc=self.tasks.create('Synthetic author metadata','No science or SSH','reproduction')
        self.web=TestClient(create_app(self.tasks),base_url=ORIGIN);self.addCleanup(self.web.close)
        self.url=f"/api/tasks/{self.doc['id']}/author-environment"

    def test_get_without_deployment_is_local_and_explicitly_not_ready(self):
        result=self.web.get(self.url);self.assertEqual(result.status_code,200,result.text)
        value=result.json();self.assertIsNone(value['latest']);self.assertFalse(value['configured'])
        for key in ('calls_model','checks_on_GET','submits_HPC','execution_authorized','execution_ready'):
            self.assertFalse(value[key])
        self.assertIn('尚需核定',value['note'])

    def test_browser_cannot_inject_paths_and_missing_deployment_never_creates_receipt(self):
        for extra in ({'profile_path':'/tmp/forbidden'},{'python_candidates':[]},{'required_distributions':['numpy']}):
            reply=self.web.post(self.url,json=dict(request_id='a'*32,**extra),headers=HEADERS)
            self.assertEqual(reply.status_code,422,reply.text)
        reply=self.web.post(self.url,json=dict(request_id='a'*32),headers=HEADERS)
        self.assertEqual(reply.status_code,422,reply.text)
        self.assertEqual(self.web.get(self.url).json()['history'],[])
