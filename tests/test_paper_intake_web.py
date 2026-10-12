"""The PDF route retains the existing loopback, origin and body boundary."""
from unittest.mock import patch
import unittest
from fastapi.testclient import TestClient

from auto_lammps.web import create_app
import test_paper_intake as fixture
from test_paper_intake import PDF, TITLE, DOI, SCOPE, metadata


class PaperIntakeWebTests(unittest.TestCase):
    reopen = fixture.PaperIntakeTests.reopen

    def setUp(self):
        fixture.PaperIntakeTests.setUp(self)
        self.addCleanup(patch.stopall)
        patch('auto_lammps.paper_intake.inspect_pdf', metadata).start()
        # The constructor's default is bound at definition, so bind a subclass
        # through the app's existing composition point without altering runtime.
        from auto_lammps.paper_intake import PaperIntake
        patch('auto_lammps.web.PaperIntake', lambda *args:PaperIntake(*args, pdf_reader=metadata)).start()
        self.client = TestClient(create_app(self.tasks, papers=self.papers,
            workbench_bridge=self.bridge), base_url='http://127.0.0.1:8765')
        self.headers = {'Origin':'http://127.0.0.1:8765','X-Task-Review':'1'}
        self.url = '/api/paper-imports/'+'a'*32

    def test_browser_upload_confirm_create_and_reload(self):
        upload = self.client.post(self.url+'/pdf', content=PDF,
            headers={**self.headers, 'Content-Type':'application/pdf'})
        self.assertEqual(upload.status_code, 200)
        confirm = self.client.post(self.url+'/confirm', json=dict(title=TITLE, doi=DOI, scope=SCOPE),headers=self.headers)
        self.assertEqual(confirm.status_code, 200)
        task = self.client.post(self.url+'/task', json={}, headers=self.headers)
        self.assertEqual(task.status_code, 200)
        repeated = self.client.post(self.url+'/task', json={}, headers=self.headers)
        self.assertEqual(repeated.json(), task.json())
        saved = self.client.get(self.url).json()
        self.assertEqual(saved['task_id'], task.json()['id'])
        self.assertNotIn(str(self.root), str(self.client.get('/api/paper-imports').json()))
        self.assertTrue(self.client.get('/api/schema').json()['paper_intake']['configured'])

    def test_cross_site_and_missing_review_header_cannot_upload(self):
        for headers in ({'Origin':'http://other.example','X-Task-Review':'1'},
                        {'Origin':'http://127.0.0.1:8765'}):
            r = self.client.post(self.url+'/pdf', content=PDF, headers={**headers,'Content-Type':'application/pdf'})
            self.assertEqual(r.status_code, 403)
        self.assertEqual(self.intake.list()['imports'], [])

    def test_pdf_mime_exception_applies_only_to_exact_upload_route(self):
        for url in ('/api/tasks', self.url+'/confirm', '/api/paper-imports/not-an-id/pdf'):
            r = self.client.post(url, content=PDF, headers={**self.headers,'Content-Type':'application/pdf'})
            self.assertEqual(r.status_code, 403)

    def test_large_json_remains_bounded_on_original_routes(self):
        r = self.client.post('/api/tasks', content=b' '*131073,
            headers={**self.headers,'Content-Type':'application/json'})
        self.assertEqual(r.status_code, 413)

    def test_identity_and_unknown_metadata_fields_are_rejected(self):
        self.client.post(self.url+'/pdf', content=PDF, headers={**self.headers,'Content-Type':'application/pdf'})
        r = self.client.post(self.url+'/confirm',json=dict(title=TITLE,doi=DOI,scope=SCOPE,
            workbench_paper_id=1,role='independent_B'),headers=self.headers)
        self.assertEqual(r.status_code,422)
        self.assertEqual(self.intake.get('a'*32)['state'],'uploaded')
