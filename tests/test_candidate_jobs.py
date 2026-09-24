"""Preparation lifecycle with real persistence, threads and a killed worker."""
from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import threading
import time
import unittest
from unittest.mock import Mock

from fastapi.testclient import TestClient

from auto_lammps.candidate_jobs import CandidateHistory, CandidateService
from auto_lammps.deepseek import DeepSeekClient, ModelCalls
from auto_lammps.ledger import Resources
from auto_lammps.potentials import PotentialAdapter, PotentialCatalog
from auto_lammps.tasks import FIELDS, TaskStore
from auto_lammps.web import create_app
import test_agent_candidates as candidate_tests
from test_tasks import evidence
from test_web import HEADERS, ORIGIN


def frozen_research(tasks):
    doc = tasks.create('合成页面验收 · 不运行模拟', 'Synthetic request only', 'research')
    for field in FIELDS:
        if field != 'reference':
            doc = tasks.add_candidate(doc['id'], doc['revision'], field, evidence('metal' if field == 'units' else 'synthetic input'))
    doc = tasks.confirm(doc['id'], doc['revision'], [x for x in FIELDS if x != 'reference'])
    return tasks.freeze(doc['id'], doc['revision'])


def crash_worker(root, pin, identifier, revision):
    root = Path(root)
    def transport(*args):
        os._exit(17)  # Real process exit after the model intent is durably spent.
    tasks = TaskStore(root / 'tasks.sqlite')
    client = DeepSeekClient(ModelCalls.open_existing(root / 'models.sqlite'), transport=transport, key_reader=lambda: 'synthetic-key')
    adapter = PotentialAdapter(PotentialCatalog(root / 'potentials'), allowed_pins=[pin], software_sha256='b' * 64, packages=['ML-SNAP'])
    service = CandidateService(tasks, client, adapter, resources=Resources(1, 60, 1000000, 1000000), snapshots=root / 'snapshots')
    service.enqueue(identifier, revision)
    service.close(wait=True)


class CandidateJobTests(unittest.TestCase):
    def setUp(self):
        fixture = candidate_tests.AgentCandidateTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.tasks = TaskStore(fixture.root / 'tasks.sqlite')
        self.doc = frozen_research(self.tasks)
        self.service = self.make_service()
        self.history = self.service.history

    def make_service(self):
        f = self.fixture
        service = CandidateService(self.tasks, f.client, f.adapter, resources=f.resources, snapshots=f.root / 'snapshots')
        self.addCleanup(lambda: service.close(wait=True))
        return service

    def enqueue(self, service=None):
        return (service or self.service).enqueue(self.doc['id'], self.doc['revision'])

    def finished(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.history.get(self.doc['id'])
            if job and job['state'] in {'prepared', 'clarification', 'failed', 'interrupted', 'configuration_changed'}:
                return job
            time.sleep(.01)
        self.fail('Preparation did not reach a terminal event')

    def test_duplicate_concurrent_clients_and_restart_keep_one_request(self):
        second = self.make_service()
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = list(pool.map(lambda i: self.enqueue(self.service if i % 2 else second), range(8)))
        self.assertEqual(len({job['id'] for job in jobs}), 1)
        final = self.finished()
        self.assertEqual(final['state'], 'prepared')
        self.assertEqual([e['state'] for e in final['events']], ['queued', 'running', 'model_requested', 'preparing_files', 'prepared'])
        restarted = self.make_service()
        restarted.start()
        self.assertEqual(self.enqueue(restarted)['id'], final['id'])
        self.assertEqual(self.fixture.transport.call_count, 1)
        self.assertEqual(CandidateHistory(TaskStore(self.tasks.path)).get(self.doc['id']), final)

    def test_meam_resource_reaches_prepared_history_and_download_without_execution(self):
        import test_meam_potentials as meam
        f = self.fixture
        source = f.root / 'source'
        (source / meam.FILES['library']).write_bytes(meam.LIBRARY)
        (source / meam.FILES['parameters']).write_bytes(meam.PARAMETERS)
        pin = f.catalog.import_model(source, metadata=meam.METADATA, files=meam.FILES)
        f.value['potential_pin'] = pin
        f.adapter = PotentialAdapter(f.catalog, allowed_pins=[pin], software_sha256='b' * 64, packages=['MEAM'])
        self.service.close(wait=True)
        self.service = self.make_service()
        self.assertTrue(self.service.availability()['enabled'])
        self.enqueue()
        final = self.finished()
        self.assertEqual(final['state'], 'prepared')
        self.assertEqual([e['state'] for e in final['events']],
                         ['queued', 'running', 'model_requested', 'preparing_files', 'prepared'])
        self.assertIn(b'pair_style meam', self.service.file(self.doc['id'], 'in.lammps'))
        receipt = json.loads(self.service.file(self.doc['id'], 'generation.json'))
        self.assertFalse(receipt['execution_authorized'])
        self.assertEqual(receipt['potential_receipt']['library_index_elements'], ['Cu', 'Ni'])
        self.assertEqual(f.transport.call_count, 1)
        restarted = self.make_service()
        self.assertEqual(self.enqueue(restarted)['id'], final['id'])
        self.assertEqual(f.transport.call_count, 1)

    def test_live_worker_is_not_interrupted_by_another_server_or_poll(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        transport = self.fixture.transport.side_effect
        def blocked(*args):
            entered.set()
            if not release.wait(5): raise RuntimeError('test timeout')
            return transport(*args)
        self.fixture.transport.side_effect = blocked
        self.enqueue()
        self.assertTrue(entered.wait(3))
        other = self.make_service()
        other.start()
        self.assertEqual(other.history.reconcile(self.doc['id'])['state'], 'model_requested')
        self.assertEqual(self.enqueue(other)['state'], 'model_requested')
        release.set()
        self.assertEqual(self.finished()['state'], 'prepared')
        self.assertEqual(self.fixture.transport.call_count, 1)

    def test_concurrent_intent_wins_over_stale_budget_availability(self):
        other = self.make_service()
        availability = self.service.availability
        def racing_availability():
            # Another request arrives after our initial history read, and uses
            # the final model allowance before this request checks availability.
            other.enqueue(self.doc['id'], self.doc['revision'])
            other.close(wait=True)
            result = availability()
            self.assertFalse(result['enabled'])
            return result
        self.service.availability = racing_availability
        job = self.enqueue()
        self.assertEqual(job['id'], self.history.get(self.doc['id'])['id'])
        self.assertEqual(job['state'], 'prepared')
        self.assertEqual(self.fixture.transport.call_count, 1)

    def test_actual_worker_exit_retains_unknown_model_intent_and_never_resends(self):
        process = multiprocessing.get_context('spawn').Process(target=crash_worker,
            args=(str(self.fixture.root), self.fixture.pin, self.doc['id'], self.doc['revision']))
        process.start()
        process.join(10)
        if process.is_alive():
            process.kill(); process.join(); self.fail('Crash worker did not exit')
        self.assertEqual(process.exitcode, 17)
        self.assertEqual(self.history.get(self.doc['id'])['state'], 'model_requested')
        self.service.start()
        self.assertEqual(self.history.get(self.doc['id'])['state'], 'interrupted')
        self.enqueue()
        self.fixture.transport.assert_not_called()
        calls = self.fixture.calls.history()
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0]['receipt'])

    def test_failure_is_append_only_and_does_not_become_a_new_attempt(self):
        self.fixture.value['workflow'] = 'shell false'
        self.enqueue()
        job = self.finished()
        self.assertEqual(job['state'], 'failed')
        self.assertEqual(job['result']['error'], 'candidate_validation_failed')
        self.enqueue()
        self.assertEqual(self.fixture.transport.call_count, 1)
        with self.tasks.transaction() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('DELETE FROM candidate_events')
        self.assertEqual(self.history.get(self.doc['id'])['events'], job['events'])

    def test_clarification_preserves_questions_without_snapshot(self):
        self.fixture.value.update(questions=['请明确晶格常数。'], structure=None, potential_pin=None, workflow=None, analysis=None)
        self.enqueue()
        job = self.finished()
        self.assertEqual(job['state'], 'clarification')
        self.assertEqual(job['result']['questions'], ['请明确晶格常数。'])
        self.assertNotIn('snapshot_sha256', job['result'])

    def test_queued_intent_survives_restart_but_changed_config_does_not_run(self):
        # Simulate a crash between committing the user intent and dispatching.
        self.service.pool.submit = Mock()
        self.enqueue()
        self.assertEqual(self.history.get(self.doc['id'])['state'], 'queued')
        other = self.make_service()
        other.config_sha256 = 'f' * 64
        other.start()
        self.assertEqual(self.finished()['state'], 'configuration_changed')
        self.fixture.transport.assert_not_called()

    def test_queued_intent_resumes_under_identical_configuration(self):
        self.service.pool.submit = Mock()
        self.enqueue()
        other = self.make_service()
        other.start()
        self.assertEqual(self.finished()['state'], 'prepared')
        self.assertEqual(self.fixture.transport.call_count, 1)

    def test_changed_compatibility_policy_blocks_queued_work_without_model_request(self):
        self.service.pool.submit = Mock()
        self.enqueue()
        f = self.fixture
        f.adapter = PotentialAdapter(f.catalog, allowed_pins=[f.pin], software_sha256='b' * 64,
                                     packages=['ML-SNAP'], legacy_snap_pins=[f.pin])
        other = self.make_service()
        self.assertNotEqual(self.service.config_sha256, other.config_sha256)
        other.start()
        self.assertEqual(self.finished()['state'], 'configuration_changed')
        f.transport.assert_not_called()

    def test_api_history_download_and_origin_boundaries(self):
        base = f"/api/tasks/{self.doc['id']}/candidate"
        app = create_app(self.tasks, model_client=self.fixture.client, candidate_service=self.service)
        with TestClient(app, base_url=ORIGIN) as browser:
            self.assertTrue(browser.get('/api/schema').json()['candidate_preparation']['enabled'])
            self.assertIsNone(browser.get(base).json()['candidate'])
            self.assertEqual(browser.post(base, json={'revision': self.doc['revision']}, headers={'Origin': 'https://untrusted.example'}).status_code, 403)
            self.assertEqual(browser.post(base, json={'revision': self.doc['revision'], 'max_requests': 9}, headers=HEADERS).status_code, 422)
            self.assertEqual(browser.post(base, json={'revision': self.doc['revision']}, headers=HEADERS).status_code, 202)
            self.finished()
            document = browser.get(base)
            self.assertNotIn(str(self.fixture.root), document.text)
            self.assertFalse(document.json()['candidate']['execution_authorized'])
            history = browser.get(f"/api/tasks/{self.doc['id']}/history").json()
            self.assertEqual(history['preparation_events'][-1]['state'], 'prepared')
            artifact = browser.get(base + '/files/in.lammps')
            self.assertEqual(artifact.status_code, 200)
            self.assertIn('attachment', artifact.headers['content-disposition'])
            self.assertEqual(artifact.headers['cache-control'], 'no-store')
            self.assertEqual(browser.get(base + '/files/models.sqlite').status_code, 404)
            self.assertEqual(browser.get(base + '/files/in.lammps', headers={'Host': 'untrusted.example'}).status_code, 403)
            self.assertEqual(browser.post(base, json={'revision': self.doc['revision']}, headers=HEADERS).status_code, 202)
            self.assertEqual(self.fixture.transport.call_count, 1)

    def test_completed_history_readable_when_model_service_is_disabled(self):
        self.enqueue(); self.finished()
        with TestClient(create_app(self.tasks), base_url=ORIGIN) as browser:
            base = f"/api/tasks/{self.doc['id']}/candidate"
            data = browser.get(base).json()
            self.assertEqual(data['candidate']['state'], 'prepared')
            self.assertFalse(data['downloads_enabled'])
            self.assertFalse(browser.get('/api/schema').json()['candidate_preparation']['enabled'])
            self.assertEqual(browser.post(base, json={'revision': self.doc['revision']}, headers=HEADERS).status_code, 422)

    def test_corrupt_snapshot_cannot_be_downloaded(self):
        self.enqueue()
        job = self.finished()
        artifact = self.service.snapshots / job['result']['snapshot_sha256'] / 'in.lammps'
        artifact.chmod(0o600)
        artifact.write_text('changed')
        with TestClient(create_app(self.tasks, model_client=self.fixture.client, candidate_service=self.service), base_url=ORIGIN) as browser:
            self.assertEqual(browser.get(f"/api/tasks/{self.doc['id']}/candidate/files/in.lammps").status_code, 409)


if __name__ == '__main__':
    unittest.main()
