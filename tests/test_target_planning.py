"""Reference planning is synthetic here; no physics, API or hidden paper data."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from fastapi.testclient import TestClient
from auto_lammps.tasks import TaskStore, TaskError, FrozenTask, StaleTask, FIELDS
from auto_lammps.target_planning import inventory
from auto_lammps.web import create_app
from test_tasks import evidence, target_inventory, target_ready
from test_web import ORIGIN, HEADERS


class TargetPlanningTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=TaskStore(Path(self.tmp.name)/'tasks.sqlite')
        self.doc=self.store.create('Synthetic review','No computation','reproduction')
        for field in FIELDS:self.doc=self.store.add_candidate(self.doc['id'],self.doc['revision'],field,evidence())
        self.doc=self.store.confirm(self.doc['id'],self.doc['revision'],list(FIELDS))

    def test_conditions_alone_cannot_freeze_new_reproduction(self):
        with self.assertRaisesRegex(TaskError,'目标'):self.store.freeze(self.doc['id'],self.doc['revision'])
        self.assertEqual(self.store.get(self.doc['id'])['status'],'draft')

    def test_invalid_duplicate_inventory_rejected_without_revisions(self):
        for modify in [lambda d:d['targets'].append(deepcopy(d['targets'][0])),
                       lambda d:d.update(source_sha256='bad'),lambda d:d['targets'][0].pop('sampling')]:
            value=target_inventory();modify(value)
            with self.assertRaises(TaskError):self.store.import_target_inventory(self.doc['id'],self.doc['revision'],value)
        self.assertNotIn('target_inventory',self.store.get(self.doc['id']))

    def test_stale_selection_and_reimport_invalidate_old_selection(self):
        d=self.store.import_target_inventory(self.doc['id'],self.doc['revision'],target_inventory())
        selected=self.store.select_targets(d['id'],d['revision'],['fig-1-a'],'')
        with self.assertRaises(StaleTask):self.store.select_targets(d['id'],d['revision'],['fig-1-a'],'')
        updated=target_inventory();updated['targets'][0]['sampling']='changed interval'
        d=self.store.import_target_inventory(selected['id'],selected['revision'],updated)
        self.assertNotIn('target_selection',d)
        with self.assertRaises(TaskError):self.store.freeze(d['id'],d['revision'])

    def test_missing_rules_resources_and_multiple_conditions_block_freeze(self):
        for key,value in [('criterion',''),('availability','missing_resources'),('availability','not_simulation'),('availability','unresolved')]:
            inv=target_inventory();inv['targets'][0][key]=value
            d=self.store.get(self.doc['id']);d=self.store.import_target_inventory(d['id'],d['revision'],inv)
            d=self.store.select_targets(d['id'],d['revision'],['fig-1-a'],'')
            with self.assertRaises(TaskError):self.store.freeze(d['id'],d['revision'])
        inv=target_inventory();inv['targets'].append({**inv['targets'][0],'id':'fig-1-b','condition_group':'second-condition'})
        d=self.store.get(self.doc['id']);d=self.store.import_target_inventory(d['id'],d['revision'],inv)
        d=self.store.select_targets(d['id'],d['revision'],['fig-1-a','fig-1-b'],'')
        with self.assertRaisesRegex(TaskError,'工况'):self.store.freeze(d['id'],d['revision'])

    def test_condition_change_invalidates_selection_and_preserves_history(self):
        d=target_ready(self.store,self.doc)
        selection=deepcopy(d['target_selection'])
        field=next(iter(FIELDS))
        changed=self.store.add_candidate(d['id'],d['revision'],field,evidence('New condition'))
        self.assertNotIn('target_selection',changed)
        with self.store.transaction() as db:
            old=json.loads(db.execute('SELECT document FROM revisions WHERE task_id=? AND revision=?',
                                      (d['id'],d['revision'])).fetchone()[0])
        self.assertEqual(old['target_selection'],selection)
        choice=changed['fields'][field]['candidates'][-1]['id']
        changed=self.store.select(changed['id'],changed['revision'],field,choice,'Changed synthetic scope')
        changed=self.store.confirm(changed['id'],changed['revision'],[field])
        with self.assertRaisesRegex(TaskError,'目标'):self.store.freeze(changed['id'],changed['revision'])
        changed=self.store.select_targets(changed['id'],changed['revision'],['fig-1-a'],'')
        self.assertNotEqual(selection['conditions_sha256'],changed['target_selection']['conditions_sha256'])
        self.assertEqual(self.store.freeze(changed['id'],changed['revision'])['status'],'conditions_frozen')

    def test_scope_exclusions_shared_condition_and_answers_stay_reference_side(self):
        inv=target_inventory();inv['targets'] += [{**inv['targets'][0],'id':'fig-1-b'}, {**inv['targets'][0],'id':'table-2'}]
        d=self.store.import_target_inventory(self.doc['id'],self.doc['revision'],inv)
        with self.assertRaises(TaskError):self.store.select_targets(d['id'],d['revision'],['fig-1-a','fig-1-b'],'')
        d=self.store.select_targets(d['id'],d['revision'],['fig-1-a','fig-1-b'],'Table is outside this synthetic scope.')
        d=self.store.freeze(d['id'],d['revision']);pair=self.store.export_packages(d['id'])
        plan=json.loads(pair['reference'])['condition_review_record']['target_plan']
        self.assertEqual(plan['condition_groups'],['condition-one'])
        self.assertEqual(plan['excluded_ids'],['table-2'])
        self.assertNotIn(b'PRIVATE_SCORE_CANARY',pair['execution'])
        self.assertNotIn(b'PRIVATE_TARGET_SOURCE',pair['execution'])
        self.assertIn(b'PRIVATE_SCORE_CANARY',pair['reference'])
        self.assertEqual(TaskStore(self.store.path).export_packages(d['id']),pair)
        with self.assertRaises(FrozenTask):self.store.import_target_inventory(d['id'],d['revision'],inv)

    def test_browser_can_select_but_cannot_upload_inventory_or_forge_rules(self):
        d=self.store.import_target_inventory(self.doc['id'],self.doc['revision'],target_inventory())
        with TestClient(create_app(self.store),base_url=ORIGIN) as client:
            url=f"/api/tasks/{d['id']}/targets"
            r=client.post(url,json=dict(revision=d['revision'],selected_ids=['fig-1-a'],exclusion_reason=''),headers=HEADERS)
            self.assertEqual(r.status_code,200,r.text)
            self.assertEqual(r.json()['target_selection']['selected_ids'],['fig-1-a'])
            r=client.post(url,json=dict(revision=d['revision'],selected_ids=['fig-1-a'],exclusion_reason='',submission_rule='unlimited'),headers=HEADERS)
            self.assertEqual(r.status_code,422)
            self.assertEqual(client.post(url, json={}, headers={'Origin':'https://outside.example'}).status_code,403)
            self.assertEqual(client.post(url+'/inventory',json={},headers=HEADERS).status_code,404)
