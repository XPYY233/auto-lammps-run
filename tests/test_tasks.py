"""Task provenance and persistence tests; synthetic conditions only."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from auto_lammps.tasks import FIELDS, FrozenTask, StaleTask, TaskError, TaskStore
from auto_lammps.manifest import sha256


def evidence(value='合成条件', origin='user', **changes):
    return dict(value=value, unit='', origin=origin, source_locator='合成证据第 1 节', applicability='required',
                evidence_role='input', **changes)


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'tasks.sqlite'
        self.store = TaskStore(self.path)
        self.doc = self.store.create('合成验收任务', '仅测试任务保存，不计算任何物理量。', 'reproduction')

    def add(self, field, value='合成条件', origin='user'):
        self.doc = self.store.add_candidate(self.doc['id'], self.doc['revision'], field, evidence(value, origin))
        return self.doc

    def test_request_keeps_all_fields_missing_without_physical_defaults(self):
        self.assertEqual(len(self.doc['issues']), len(FIELDS))
        self.assertTrue(all(not field['candidates'] for field in self.doc['fields'].values()))
        self.assertEqual(TaskStore(self.path).get(self.doc['id']), self.doc)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_exact_label_lines_extract_without_guesses_or_confirmation(self):
        doc=self.store.create('标签输入测试', '温度：300 K\n预测熔点：999 K\n压力：0 bar\n温度：400 K\n一句提到温度 800 K 的普通话。', 'research')
        temperature=doc['fields']['temperature']
        self.assertEqual([c['value'] for c in temperature['candidates']],['300 K','400 K'])
        self.assertEqual(temperature['candidates'][1]['source_locator'],'原始任务描述第 4 行')
        self.assertIsNone(temperature['selected'])
        self.assertFalse(temperature['confirmed'])
        self.assertFalse(doc['fields']['quantity']['candidates'])
        self.assertEqual(len(doc['fields']['pressure']['candidates']),1)
        self.assertFalse(doc['fields']['pressure']['confirmed'])

    def test_conflict_invalidates_confirmation_and_preserves_both_sources(self):
        self.add('timestep','1','paper')
        self.doc = self.store.confirm(self.doc['id'], self.doc['revision'], ['timestep'])
        self.add('timestep','2','code')
        field = self.doc['fields']['timestep']
        self.assertEqual([x['value'] for x in field['candidates']], ['1','2'])
        self.assertIsNone(field['selected'])
        self.assertFalse(field['confirmed'])
        with self.assertRaises(TaskError): self.store.confirm(self.doc['id'], self.doc['revision'], ['timestep'])
        with self.assertRaises(TaskError):
            self.store.select(self.doc['id'], self.doc['revision'], 'timestep', field['candidates'][0]['id'], '')
        self.doc = self.store.select(self.doc['id'], self.doc['revision'], 'timestep', field['candidates'][0]['id'], '明确采用论文条件，保留差异供后续审查')
        self.assertFalse(self.doc['fields']['timestep']['confirmed'])
        self.doc = self.store.confirm(self.doc['id'], self.doc['revision'], ['timestep'])
        self.assertTrue(self.doc['fields']['timestep']['confirmed'])
        self.assertEqual(len(self.store.history(self.doc['id'])), 6)

    def test_proposed_value_is_never_implicitly_confirmed(self):
        self.add('temperature', '300', 'proposed')
        self.assertFalse(self.doc['fields']['temperature']['confirmed'])
        with self.assertRaises(TaskError): self.store.freeze(self.doc['id'], self.doc['revision'])

    def test_result_evidence_and_unsourced_paper_are_rejected(self):
        for changes in ({'evidence_role':'result'}, {'origin':'paper','source_locator':''}, {'value':''}):
            with self.assertRaises(TaskError):
                self.store.add_candidate(self.doc['id'], self.doc['revision'], 'temperature', {**evidence(), **changes})
        self.assertEqual(len(self.store.history(self.doc['id'])), 1)

    def test_not_applicable_requires_reason_and_cannot_remove_essential_conditions(self):
        data={**evidence('静态任务，不涉及温度控制'), 'applicability':'not_applicable'}
        self.doc=self.store.add_candidate(self.doc['id'],self.doc['revision'],'temperature',data)
        for field in ('potential','reference','resources'):
            with self.assertRaises(TaskError): self.store.add_candidate(self.doc['id'],self.doc['revision'],field,data)

    def test_stale_concurrent_edits_never_silently_replace_evidence(self):
        revision = self.doc['revision']
        def add(value):
            try:
                TaskStore(self.path).add_candidate(self.doc['id'],revision,'material',evidence(value))
                return 'saved'
            except StaleTask:
                return 'stale'
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(pool.map(add,['synthetic A','synthetic B']), ['saved','stale'])
        self.assertEqual(len(self.store.get(self.doc['id'])['fields']['material']['candidates']),1)

    def test_frozen_record_survives_restart_and_cannot_be_edited(self):
        for field in FIELDS: self.add(field)
        self.doc=self.store.confirm(self.doc['id'],self.doc['revision'],list(FIELDS))
        self.doc=self.store.freeze(self.doc['id'],self.doc['revision'])
        content=self.store.export(self.doc['id'])
        self.assertEqual(sha256(content),self.doc['record_sha256'])
        record=json.loads(content)
        self.assertIs(record['execution_authorized'],False)
        self.assertEqual(record['scientific_validation'],'not_performed')
        self.assertEqual(TaskStore(self.path).export(self.doc['id']),content)
        self.assertEqual(self.store.freeze(self.doc['id'],self.doc['revision']),self.doc)
        with self.assertRaises(FrozenTask): self.add('potential','changed')
        with self.assertRaises(FrozenTask): self.store.confirm(self.doc['id'],self.doc['revision'],['potential'])
        with closing(sqlite3.connect(self.path)) as db:
            with self.assertRaises(sqlite3.IntegrityError): db.execute('DELETE FROM revisions')
            with self.assertRaises(sqlite3.IntegrityError): db.execute("UPDATE frozen SET document='{}'")

    def test_other_database_or_linked_file_is_not_adopted(self):
        path=Path(self.tmp.name)/'other.sqlite'
        with closing(sqlite3.connect(path)) as db: db.execute('CREATE TABLE unrelated (id TEXT)')
        path.chmod(0o600)
        with self.assertRaises(TaskError): TaskStore(path)
        link=Path(self.tmp.name)/'linked.sqlite'
        os.link(self.path,link)
        with self.assertRaises(TaskError): TaskStore(link)
