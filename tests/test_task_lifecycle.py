from pathlib import Path
import tempfile
import unittest
from fastapi.testclient import TestClient
from auto_lammps.web import create_app
from auto_lammps.tasks import TaskStore
from test_web import HEADERS, ORIGIN


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=TaskStore(Path(self.tmp.name)/'tasks.sqlite')
        self.task=self.store.create('Synthetic history','No physics','research')
        self.client=TestClient(create_app(self.store), base_url=ORIGIN)
        self.url='/api/tasks/'+self.task['id']+'/lifecycle'

    def test_delete_removes_list_row_but_preserves_original_history(self):
        before=self.store.history(self.task['id'])
        reply=self.client.post(self.url,json=dict(revision=1,lifecycle_revision=0,action='delete'),headers=HEADERS)
        self.assertEqual(reply.status_code,200,reply.text)
        self.assertEqual(self.store.list(),[])
        self.assertEqual(self.store.history(self.task['id']),before)
        self.assertTrue(self.store.lifecycle(self.task['id'])['deleted'])

    def test_finish_is_explicit_not_scientific_and_stale_delete_rejected(self):
        reply=self.client.post(self.url,json=dict(revision=1,lifecycle_revision=0,action='finish'),headers=HEADERS)
        self.assertEqual(reply.status_code,200,reply.text)
        row=self.client.get('/api/tasks').json()['tasks'][0]
        self.assertTrue(row['user_finished']);self.assertNotIn('scoped_acceptance',row)
        self.assertEqual(self.client.post(self.url,json=dict(revision=1,lifecycle_revision=0,action='delete'),headers=HEADERS).status_code,409)
        self.assertEqual(len(self.store.list()),1)

    def test_active_preparation_cannot_be_hidden_or_marked_finished(self):
        from auto_lammps.candidate_jobs import CandidateHistory
        history=CandidateHistory(self.store)
        with self.store.transaction() as db:
            db.execute('INSERT INTO candidate_jobs VALUES (?,?,?,?,?,?)',('a'*32,self.task['id'],1,'b'*64,'c'*64,'synthetic'))
            history._event(db,'a'*32,'queued')
        for action in ('finish','delete'):
            reply=self.client.post(self.url,json=dict(revision=1,lifecycle_revision=0,action=action),headers=HEADERS)
            self.assertEqual(reply.status_code,422,reply.text)
        self.assertEqual(len(self.store.list()),1)
