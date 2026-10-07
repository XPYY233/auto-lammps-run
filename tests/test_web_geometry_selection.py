"""Ordinary-page geometry routes with synthetic metadata; no network or engines."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call

from fastapi.testclient import TestClient

from auto_lammps.tasks import FIELDS, TaskStore
from auto_lammps.manifest import canonical, sha256
from auto_lammps.web import create_app
from test_task_geometry_selection import selection
from test_tasks import evidence
from test_web import HEADERS, ORIGIN


class WebGeometrySelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TaskStore(Path(self.tmp.name).resolve()/'tasks.sqlite')
        self.selected = selection()
        self.view = dict(schema_version=1, catalog_sha256=self.selected['catalog_sha256'],
                         entries=[deepcopy(self.selected['entry'])])
        self.catalog = Mock()
        self.catalog.list.return_value = self.view
        self.client = TestClient(create_app(self.store, geometry_catalog_client=self.catalog), base_url=ORIGIN)
        self.addCleanup(self.client.close)
        self.doc = self.store.create('Synthetic geometry user flow', 'Use permitted fixed input.', 'research')
        self.url = f"/api/tasks/{self.doc['id']}/initial-geometry"
        self.payload = dict(revision=self.doc['revision'], catalog_sha256=self.view['catalog_sha256'],
                            pin=self.selected['entry']['pin'])

    def test_catalog_get_returns_validated_metadata_without_task_or_model_actions(self):
        before, history = self.store.get(self.doc['id']), self.store.history(self.doc['id'])
        reply = self.client.get('/api/geometry-catalog')
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertTrue(reply.json()['configured'])
        self.assertEqual(reply.json()['entries'], self.view['entries'])
        self.assertNotIn('path', reply.json()['entries'][0])
        self.assertNotIn('positions', reply.json()['entries'][0]['summary'])
        self.assertIs(reply.json()['entries'][0]['summary']['physical_evaluation_performed'], False)
        self.catalog.list.assert_called_once_with()
        self.assertEqual(self.catalog.mock_calls, [call.list()])
        self.assertEqual(self.store.get(self.doc['id']), before)
        self.assertEqual(self.store.history(self.doc['id']), history)
        self.assertIsNone(self.client.get('/api/tasks/'+self.doc['id']+'/candidate').json()['candidate'])

    def test_conditions_changed_after_ordinary_selection_require_resource_review_again(self):
        response = self.client.post(self.url, json=self.payload, headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        doc = response.json()
        self.assertEqual(doc['initial_geometry'], self.selected)
        self.assertEqual(doc['fields'], self.doc['fields'])
        for field in FIELDS:
            if field != 'reference':
                doc = self.store.add_candidate(doc['id'], doc['revision'], field,
                    evidence('metal' if field == 'units' else 'synthetic-'+field))
        fields = [field for field in FIELDS if field != 'reference']
        doc = self.client.post(f"/api/tasks/{doc['id']}/confirm", json={'revision':doc['revision'], 'fields':fields}, headers=HEADERS).json()
        response = self.client.post(f"/api/tasks/{doc['id']}/freeze", json={'revision':doc['revision']}, headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        # Adding relevant scientific evidence invalidates the earlier selection.
        # The user must select again after checking those conditions.
        self.assertNotIn('initial_geometry', response.json())
        self.assertEqual(self.store.history(doc['id'])[2]['event'], 'candidate_added:scope')

    def test_selection_after_confirmed_conditions_is_frozen_without_geometry_bytes(self):
        doc = self.doc
        for field in FIELDS:
            if field != 'reference':
                doc = self.store.add_candidate(doc['id'], doc['revision'], field,
                    evidence('metal' if field == 'units' else 'synthetic-'+field))
        doc = self.store.confirm(doc['id'], doc['revision'], [field for field in FIELDS if field != 'reference'])
        doc = self.client.post(self.url, json={**self.payload, 'revision':doc['revision']}, headers=HEADERS).json()
        frozen = self.client.post(f"/api/tasks/{doc['id']}/freeze", json={'revision':doc['revision']}, headers=HEADERS)
        self.assertEqual(frozen.status_code, 200, frozen.text)
        frozen = frozen.json()
        exported = self.client.get(f"/api/tasks/{doc['id']}/packages/execution")
        self.assertEqual(exported.status_code, 200, exported.text)
        self.assertEqual(exported.json()['initial_geometry'], self.selected)
        self.assertFalse(exported.json()['execution_authorized'])
        self.catalog.reset_mock()
        self.assertEqual(self.client.post(self.url, json={**self.payload,'revision':frozen['revision']}, headers=HEADERS).status_code, 409)
        self.assertEqual(self.client.post(self.url+'/clear', json={'revision':frozen['revision']}, headers=HEADERS).status_code, 409)
        self.catalog.list.assert_not_called()
        self.assertEqual(self.client.get(f"/api/tasks/{doc['id']}/packages/execution").content, exported.content)

    def test_spoofed_payload_stale_revision_and_foreign_pin_cannot_mutate_task(self):
        for extra in ({'path':'PRIVATE_PATH_CANARY'}, {'entry':self.selected['entry']}, {'coordinates':[[0,0,0]]}, {'max_atoms':1000000}):
            reply = self.client.post(self.url, json={**self.payload, **extra}, headers=HEADERS)
            self.assertEqual(reply.status_code, 422, reply.text)
            self.assertNotIn('PRIVATE_PATH_CANARY', reply.text)
        self.catalog.list.assert_not_called()
        changed = self.store.add_candidate(self.doc['id'], self.doc['revision'], 'temperature', evidence('300 K'))
        self.assertEqual(self.client.post(self.url, json=self.payload, headers=HEADERS).status_code, 409)
        self.catalog.list.assert_not_called()
        foreign = self.client.post(self.url, json={**self.payload, 'revision':changed['revision'],'pin':'f'*64}, headers=HEADERS)
        self.assertEqual(foreign.status_code, 422)
        self.assertEqual(self.store.get(self.doc['id']), changed)

    def test_changed_catalog_rejects_old_view_then_clear_retains_previous_version(self):
        doc = self.client.post(self.url, json=self.payload, headers=HEADERS).json()
        revision, old_history = doc['revision'], self.store.history(doc['id'])
        self.catalog.list.return_value = {**self.view, 'catalog_sha256':'c'*64}
        reply = self.client.post(self.url, json={**self.payload,'revision':revision}, headers=HEADERS)
        self.assertEqual(reply.status_code, 409)
        self.assertEqual(self.store.get(doc['id']), doc)
        self.assertEqual(self.store.history(doc['id']), old_history)
        self.catalog.reset_mock()
        cleared = self.client.post(self.url+'/clear', json={'revision':revision}, headers=HEADERS)
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertNotIn('initial_geometry', cleared.json())
        self.assertEqual(self.store.history(doc['id'])[-1]['event'], 'initial_geometry_cleared')
        with self.store.transaction() as db:
            previous = json.loads(db.execute('SELECT document FROM revisions WHERE task_id=? AND revision=?',
                                  (doc['id'], revision)).fetchone()[0])
        self.assertEqual(previous['initial_geometry'], self.selected)
        self.catalog.list.assert_not_called()
        self.assertEqual(self.client.post(self.url+'/clear', json={'revision':revision}, headers=HEADERS).status_code, 409)

    def test_clearing_absent_selection_preserves_public_task_shape_and_history(self):
        before, history = self.store.get(self.doc['id']), self.store.history(self.doc['id'])
        for _ in range(2):
            reply = self.client.post(self.url+'/clear', json={'revision':before['revision']}, headers=HEADERS)
            self.assertEqual(reply.status_code, 200, reply.text)
            self.assertEqual(reply.json(), before)
            self.assertIsInstance(reply.json()['issues'], list)
            self.assertEqual(self.store.history(self.doc['id']), history)
            self.assertEqual(self.store.get(self.doc['id']), before)
        self.catalog.list.assert_not_called()

    def test_empty_or_unconfigured_catalog_does_not_block_unrelated_research(self):
        self.catalog.list.return_value = {**self.view, 'entries':[]}
        for app in (create_app(self.store, geometry_catalog_client=self.catalog), create_app(self.store)):
            with TestClient(app, base_url=ORIGIN) as client:
                reply = client.get('/api/geometry-catalog')
                self.assertEqual(reply.status_code, 200, reply.text)
                self.assertEqual(reply.json()['entries'], [])
                self.assertTrue(reply.json()['reason'])
                self.assertEqual(client.post('/api/tasks', json={'title':'Other research','prompt':'No fixed input required.','mode':'research'}, headers=HEADERS).status_code, 201)
                self.assertEqual(client.post(self.url,json=self.payload,headers=HEADERS).status_code,422)
        self.assertNotIn('initial_geometry', self.store.get(self.doc['id']))

    def test_catalog_rejects_private_fields_and_sanitizes_resource_errors(self):
        private_view = deepcopy(self.view)
        private_view['entries'][0]['path'] = 'PRIVATE_PATH_CANARY'
        for response in (private_view, {**self.view, 'author_source':'PRIVATE_SOURCE_CANARY'}):
            self.catalog.list.return_value = response
            reply = self.client.get('/api/geometry-catalog')
            self.assertEqual(reply.status_code, 422, reply.text)
            self.assertNotIn('PRIVATE_', reply.text)
        self.catalog.list.side_effect = OSError('PRIVATE_PATH_CANARY')
        reply = self.client.get('/api/geometry-catalog')
        self.assertEqual(reply.status_code, 422)
        self.assertNotIn('PRIVATE_', reply.text)
        self.assertNotIn('initial_geometry', self.store.get(self.doc['id']))

    def test_same_origin_and_open_task_boundary_apply_before_catalog_read(self):
        self.assertEqual(self.client.post(self.url, json=self.payload,
            headers={'Origin':'https://other.example','X-Task-Review':'1'}).status_code, 403)
        self.store.manage_lifecycle(self.doc['id'], self.doc['revision'], 0, 'finish')
        self.assertEqual(self.client.post(self.url, json=self.payload, headers=HEADERS).status_code, 422)
        self.catalog.list.assert_not_called()

    def test_high_candidate_limit_cannot_advertise_unsaveable_fixed_geometry(self):
        candidate = SimpleNamespace(tasks=self.store, client=None, max_atoms=200000)
        client = TestClient(create_app(self.store, candidate_service=candidate,
                                      geometry_catalog_client=self.catalog), base_url=ORIGIN)
        self.addCleanup(client.close)
        for atom_count, expected in ((100001, 422), (100000, 200)):
            view = self.counted_view(atom_count)
            self.catalog.list.return_value = view
            before = self.store.get(self.doc['id'])
            reply = client.get('/api/geometry-catalog')
            self.assertEqual(reply.status_code, expected, reply.text)
            payload = {**self.payload, 'revision':before['revision'], 'pin':view['entries'][0]['pin']}
            chosen = client.post(self.url, json=payload, headers=HEADERS)
            self.assertEqual(chosen.status_code, expected, chosen.text)
            if expected == 422:
                self.assertNotIn('entries', reply.json())
                self.assertEqual(self.store.get(self.doc['id']), before)
            else:
                self.assertEqual(reply.json()['entries'][0]['summary']['atom_count'], atom_count)
                self.assertEqual(chosen.json()['initial_geometry']['entry']['summary']['atom_count'], atom_count)
        self.assertEqual(candidate.max_atoms, 200000)

    def test_lower_candidate_limit_still_constrains_fixed_geometry_catalog(self):
        candidate = SimpleNamespace(tasks=self.store, client=None, max_atoms=50000)
        client = TestClient(create_app(self.store, candidate_service=candidate,
                                      geometry_catalog_client=self.catalog), base_url=ORIGIN)
        self.addCleanup(client.close)
        for atom_count, expected in ((50001, 422), (50000, 200)):
            view = self.counted_view(atom_count)
            self.catalog.list.return_value = view
            before = self.store.get(self.doc['id'])
            reply = client.get('/api/geometry-catalog')
            self.assertEqual(reply.status_code, expected, reply.text)
            chosen = client.post(self.url, json={**self.payload,'revision':before['revision'],
                                                'pin':view['entries'][0]['pin']}, headers=HEADERS)
            self.assertEqual(chosen.status_code, expected, chosen.text)
            if expected == 422:
                self.assertNotIn('entries', reply.json())
                self.assertEqual(self.store.get(self.doc['id']), before)
        self.assertEqual(candidate.max_atoms, 50000)

    def counted_view(self, atom_count):
        # Synthetic resource-service metadata only; no large coordinate file or
        # target structure is constructed, parsed or physically evaluated.
        view = deepcopy(self.view)
        entry = view['entries'][0]
        entry['summary'].update(atom_count=atom_count, type_counts={'1':atom_count-1,'2':1},
                                composition={'Ni':atom_count-1,'Cu':1})
        entry['pin'] = sha256(canonical({key:entry[key] for key in ('size','sha256','summary')}))
        return view


if __name__ == '__main__':
    unittest.main()
