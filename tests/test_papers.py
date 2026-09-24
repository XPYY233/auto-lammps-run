"""Paper history against a real local ledger; all jobs are synthetic."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from fastapi.testclient import TestClient
from auto_lammps.ledger import Ledger, LimitExceeded
from auto_lammps.papers import PaperStore
from auto_lammps.tasks import FIELDS, StaleTask, TaskError, TaskStore
from auto_lammps.web import create_app
from test_ledger import H1, H2, POLICY, RESOURCE
from test_tasks import evidence
from test_web import HEADERS, ORIGIN


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tasks = TaskStore(Path(self.tmp.name)/'tasks.sqlite')
        self.ledger = Ledger(Path(self.tmp.name)/'ledger.sqlite')
        self.ledger.create_campaign('synthetic', POLICY)
        self.papers = PaperStore(self.tasks, ledger=self.ledger)
        self.paper = self.papers.add('Synthetic paper', '10.1234/SYNTHETIC', 'Synthetic scope', 'No real computation')

    def bind(self):
        task = self.tasks.create('Synthetic task', 'No simulation', 'reproduction')
        for field in FIELDS:
            task = self.tasks.add_candidate(task['id'], task['revision'], field, evidence())
        task = self.tasks.confirm(task['id'], task['revision'], list(FIELDS))
        self.tasks.freeze(task['id'], task['revision'])
        self.paper = self.papers.select(self.paper['id'], self.paper['revision'])
        self.paper = self.papers.link_task(self.paper['id'], self.paper['revision'], task['id'])
        self.evaluation = self.ledger.register_evaluation('synthetic', task_sha256=H1, repetition=0,
                                                        role='agent', system_sha256=H2)
        self.papers.bind_evaluation(self.paper['id'], task['id'], self.evaluation, H1)
        return task

    def test_candidates_selection_stale_and_history_survive_restart(self):
        self.assertEqual(self.papers.list()['candidate_count'], 1)
        self.assertEqual(sum(self.papers.list()['counts'].values()), 0)
        selected = self.papers.select(self.paper['id'], 1)
        with self.assertRaises(StaleTask): self.papers.select(self.paper['id'], 1)
        with self.assertRaises(TaskError):
            self.papers.add('Renamed', 'https://doi.org/10.1234/synthetic', 'scope', 'note')
        reopened = PaperStore(TaskStore(self.tasks.path)).get(self.paper['id'])
        self.assertEqual(reopened, selected)
        self.assertEqual([e['event'] for e in reopened['history']], ['candidate_added', 'paper_selected'])
        with self.assertRaises(sqlite3.IntegrityError), self.tasks.transaction() as db:
            db.execute('DELETE FROM paper_revisions')

    def test_two_attempts_cannot_be_reset_and_private_payload_is_not_exported(self):
        task = self.bind()
        for key in ('first', 'second'):
            row = self.ledger.reserve(self.evaluation, key, H2, RESOURCE)
            self.ledger.begin_dispatch(row['id'])
            self.ledger.rejected(row['id'], {'private_payload': 'HIDDEN_TARGET_SENTINEL'})
        with self.assertRaises(LimitExceeded):
            self.ledger.reserve(self.evaluation, 'renamed-third', H2, RESOURCE)
        with self.assertRaises(TaskError):
            self.papers.bind_evaluation(self.paper['id'], task['id'], self.evaluation, H1)
        reopened = PaperStore(TaskStore(self.tasks.path), ledger=Ledger(self.ledger.path)).get(self.paper['id'])
        evaluation = reopened['evaluations'][0]
        self.assertEqual((evaluation['reserved_attempts'], evaluation['remaining_attempts'], evaluation['dispatch_claims']), (2, 0, 2))
        self.assertEqual([r['state'] for r in evaluation['requests']], ['rejected', 'rejected'])
        self.assertNotIn('HIDDEN_TARGET_SENTINEL', json.dumps(reopened))
        self.assertEqual(reopened['status'], 'in_progress')

    def test_scheduler_completed_never_publishes_scientific_success(self):
        self.bind()
        row = self.ledger.reserve(self.evaluation, 'first', H2, RESOURCE)
        self.assertEqual(self.papers.get(self.paper['id'])['status'], 'pending')
        self.ledger.begin_dispatch(row['id'])
        self.ledger.uncertain(row['id'], {'synthetic': True})
        self.assertEqual(self.papers.get(self.paper['id'])['status'], 'in_progress')
        self.ledger.accepted(row['id'], '123', {'synthetic': True})
        self.ledger.observe(row['id'], '123', 'completed', {'synthetic': True})
        self.ledger.account(row['id'], 10, H2)
        result = self.papers.get(self.paper['id'])
        self.assertEqual(result['status'], 'in_progress')
        self.assertEqual(result['evaluations'][0]['requests'][0]['actual_core_seconds'], 10)
        self.assertFalse(result['score_publication_available'])
        unavailable = PaperStore(self.tasks).get(self.paper['id'])
        self.assertFalse(unavailable['evaluations'][0]['available'])
        self.assertNotIn('remaining_attempts', unavailable['evaluations'][0])

    def test_browser_can_select_and_link_but_cannot_write_score_or_dispatch(self):
        with TestClient(create_app(self.tasks, papers=self.papers), base_url=ORIGIN) as client:
            url = '/api/papers/'+self.paper['id']
            self.assertEqual(client.get('/api/papers').json()['candidate_count'], 1)
            self.assertEqual(client.post(url+'/select', json={'revision': 1}, headers={'Origin': 'https://example.test'}).status_code, 403)
            self.assertEqual(client.post(url+'/select', json={'revision': 1, 'status': 'reproduced'}, headers=HEADERS).status_code, 422)
            self.assertEqual(client.post(url+'/select', json={'revision': 1}, headers=HEADERS).status_code, 200)
            task = self.tasks.create('Synthetic task', 'No computation', 'reproduction')
            linked = client.post(url+'/tasks', json={'revision': 2, 'task_id': task['id']}, headers=HEADERS)
            self.assertEqual(linked.status_code, 200, linked.text)
            self.assertEqual(len(linked.json()['tasks'][0]['history']), 1)
            for path in ('status', 'submit', 'evaluations'):
                self.assertEqual(client.post(url+'/'+path, json={}, headers=HEADERS).status_code, 404)
