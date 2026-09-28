"""页面活动记录与 /api/session/* 端点的离线测试：只写小文件，不涉及任务、账本或计算。"""
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from auto_lammps.session_activity import SessionActivity
from auto_lammps.tasks import TaskStore
from auto_lammps.web import create_app

ORIGIN = 'http://127.0.0.1:8765'
HEADERS = {'Origin': ORIGIN, 'X-Task-Review': '1', 'Content-Type': 'application/json'}


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'session-activity.json'
        self.now = [1000.0]
        self.activity = SessionActivity(self.path, clock=lambda: self.now[0])

    def test_heartbeat_records_one_open_page_and_close_removes_it(self):
        self.assertTrue(self.activity.heartbeat('page-1', hidden=False)['recorded'])
        self.now[0] = 1005.0
        self.activity.heartbeat('page-1', hidden=True)
        snapshot = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(snapshot['sessions']['page-1'], {'at': 1005.0, 'hidden': True})
        self.assertEqual(self.activity.close('page-1')['open_pages'], 0)
        after = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(after['sessions'], {})
        self.assertEqual(after['closed_session'], 'page-1')

    def test_identifiers_are_bounded_and_files_are_owner_only(self):
        self.activity.heartbeat('../../etc/passwd%00', hidden=False)
        snapshot = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(list(snapshot['sessions']), ['etcpasswd00'])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_a_corrupt_file_does_not_break_the_recorder(self):
        self.path.write_text('not json', encoding='utf-8')
        self.assertTrue(self.activity.heartbeat('page', hidden=False)['recorded'])


class SessionEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TaskStore(Path(self.tmp.name) / 'tasks.sqlite')
        self.activity_path = Path(self.tmp.name) / 'session-activity.json'

    def client(self, activity=None):
        return TestClient(create_app(self.store, session_activity=activity), base_url=ORIGIN)

    def test_routes_are_inert_without_the_desktop_entry_flag(self):
        with self.client() as client:
            state = client.get('/api/session/activity').json()
            self.assertFalse(state['enabled'])
            self.assertEqual(client.post('/api/session/heartbeat?session=abc', headers=HEADERS).status_code, 409)
            self.assertFalse(self.activity_path.exists())

    def test_heartbeat_close_and_state_round_trip(self):
        activity = SessionActivity(self.activity_path)
        with self.client(activity) as client:
            reply = client.post('/api/session/heartbeat?session=abc&hidden=1', headers=HEADERS)
            self.assertEqual(reply.status_code, 200, reply.text)
            self.assertTrue(reply.json()['enabled'])
            state = client.get('/api/session/activity').json()
            self.assertEqual(state['sessions']['abc']['hidden'], True)
            self.assertEqual(client.get('/api/session/heartbeat?session=abc').status_code, 200)
            # GET alias: works for a beacon fallback that cannot set custom headers.
            self.assertEqual(client.get('/api/session/close?session=abc&via=beacon').status_code, 200)
            self.assertEqual(client.get('/api/session/activity').json()['sessions'], {})

    def test_activity_endpoint_rejects_wrong_host(self):
        activity = SessionActivity(self.activity_path)
        with self.client(activity) as client:
            self.assertEqual(client.get('/api/session/activity', headers={'Host': 'example.org'}).status_code, 403)

    def test_schema_reports_the_copy_of_the_application_that_is_serving(self):
        """The desktop launcher compares this with the environment its configuration names."""
        import auto_lammps
        with self.client() as client:
            installation = client.get('/api/schema').json()['installation']
        self.assertEqual(installation['package'], str(Path(auto_lammps.__file__).resolve().parent))
        self.assertTrue(Path(installation['python']).is_absolute())

    def test_page_assets_include_the_session_reporter(self):
        with self.client() as client:
            page = client.get('/')
            self.assertIn('/assets/session.js', page.text)
            script = client.get('/assets/session.js')
            self.assertEqual(script.status_code, 200, script.text)
            self.assertIn("base = '/api/session/'", script.text)
            self.assertIn("url('heartbeat'", script.text)
            self.assertIn("url('close'", script.text)
            self.assertIn('pagehide', script.text)
            self.assertEqual(client.get('/assets/app.js').status_code, 200)
            self.assertEqual(client.get('/assets/other.js').status_code, 404)


if __name__ == '__main__':
    unittest.main()
