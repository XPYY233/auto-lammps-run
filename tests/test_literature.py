"""Synthetic export-format checks; no paper attachments or simulation."""
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest

from auto_lammps.literature import preview_csv
from auto_lammps.manifest import sha256
from auto_lammps.tasks import FIELDS, FrozenTask, StaleTask, TaskError, TaskStore
from test_tasks import evidence


def export_csv(**changes):
    row = dict(source_scope='workspace', source_id='workspace', entity_type='item', entity_uid='1',
               doi='', article_title='Synthetic source for software tests', source_locator='Methods, paragraph 2',
               source_page='3', source_excerpt='Synthetic calculation used a specified 1 fs step.',
               conditions='1 fs', material='Synthetic element', value_text='999999 target marker', unit='K')
    row.update(changes)
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=list(row))
    writer.writeheader(); writer.writerow(row)
    return '\ufeff'+stream.getvalue()


class LiteratureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TaskStore(Path(self.tmp.name)/'tasks.sqlite')
        self.doc = self.store.create('Synthetic import', 'No physical calculation.', 'reproduction')

    def load(self, content=None, **changes):
        content = export_csv() if content is None else content
        mapping = dict(source_sha256=sha256(content.encode()), column='conditions', field='timestep',
                       evidence_role='input', method_class='unclear', classification_basis='Methods paragraph 2 does not identify an engine.')
        mapping.update(changes)
        result = self.store.import_literature(self.doc['id'], self.doc['revision'], content, **mapping)
        self.doc = result
        return result

    def test_bom_multiline_and_result_unit_never_become_input(self):
        content=export_csv(source_excerpt='Line one, comma\nLine two')
        preview=preview_csv(content)
        self.assertEqual(preview['row']['source_excerpt'],'Line one, comma\nLine two')
        self.assertNotIn('value_text',preview['available_columns'])
        self.load(content)
        condition=self.doc['fields']['timestep']
        self.assertEqual(condition['candidates'][0]['value'],'1 fs')
        self.assertEqual(condition['candidates'][0]['unit'],'')
        self.assertFalse(condition['confirmed'])
        self.assertFalse(self.doc['fields']['quantity']['candidates'])
        self.assertEqual(self.doc['literature_sources'][preview['source_sha256']]['csv_text'],content)
        self.assertEqual(TaskStore(self.store.path).get(self.doc['id']),self.doc)

    def test_explicit_role_mapping_and_classification_cannot_be_bypassed(self):
        for changes in ({'evidence_role':'result'}, {'evidence_role':'unclear'},
                        {'column':'value_text'}, {'column':'unit'}, {'field':'quantity'},
                        {'column':'material','field':'temperature'}, {'method_class':'auto'},
                        {'classification_basis':''}, {'source_sha256':'0'*64}):
            with self.subTest(changes=changes), self.assertRaises(TaskError): self.load(**changes)
        self.assertEqual(self.store.get(self.doc['id']),self.doc)
        self.assertEqual(len(self.store.history(self.doc['id'])),1)

    def test_missing_provenance_and_unbounded_value_reject_entire_import(self):
        for changes in ({'source_locator':'','source_page':''}, {'source_excerpt':''}, {'conditions':'x'*4001}):
            with self.subTest(changes=changes), self.assertRaises(TaskError): self.load(export_csv(**changes))
        self.assertNotIn('literature_sources',self.store.get(self.doc['id']))

    def test_changed_source_creates_conflict_and_revokes_confirmation(self):
        self.load()
        self.doc=self.store.confirm(self.doc['id'],self.doc['revision'],['timestep'])
        self.load(export_csv(conditions='2 fs'))
        field=self.doc['fields']['timestep']
        self.assertIsNone(field['selected'])
        self.assertFalse(field['confirmed'])
        self.assertEqual([c['value'] for c in field['candidates']],['1 fs','2 fs'])
        self.assertEqual(len(self.doc['literature_sources']),2)

    def test_retry_and_stale_revision_do_not_create_extra_evidence(self):
        old=self.doc
        self.load()
        with self.assertRaises(TaskError): self.load(classification_basis='Different wording')
        saved=self.doc
        self.doc=old
        with self.assertRaises(StaleTask): self.load(export_csv(conditions='2 fs'))
        self.assertEqual(self.store.get(old['id']),saved)

    def test_candidate_limit_rolls_back_source_and_revision(self):
        for number in range(16):
            self.doc=self.store.add_candidate(self.doc['id'],self.doc['revision'],'timestep',evidence(str(number)))
        with self.assertRaises(TaskError): self.load()
        self.assertEqual(self.store.get(self.doc['id']),self.doc)
        self.assertNotIn('literature_sources',self.doc)

    def test_frozen_export_binds_operator_source_snapshot(self):
        self.load()
        for field in FIELDS:
            if field!='timestep': self.doc=self.store.add_candidate(self.doc['id'],self.doc['revision'],field,evidence())
        self.doc=self.store.confirm(self.doc['id'],self.doc['revision'],list(FIELDS))
        self.doc=self.store.freeze(self.doc['id'],self.doc['revision'])
        exported=self.store.export(self.doc['id'])
        self.assertEqual(sha256(exported),self.doc['record_sha256'])
        self.assertIn('literature_sources',json.loads(exported))
        self.assertIn('999999 target marker',exported.decode())  # Explicitly operator-only.
        with self.assertRaises(FrozenTask): self.load(export_csv(conditions='2 fs'))

    def test_malformed_duplicate_multirow_and_oversize_csv_fail_closed(self):
        valid=export_csv()
        lines=valid.splitlines()
        examples=['',valid+'x'*65536,valid+'\x00',valid+'\ud800',
                  valid+lines[1]+'\n',lines[0]+',doi\n'+lines[1]+',extra\n',
                  lines[0]+'\n"unterminated',lines[0]+'\nshort,row\n',
                  valid.replace('source_scope','unknown_column',1)]
        for content in examples:
            with self.subTest(size=len(content)), self.assertRaises(TaskError): preview_csv(content)

    def test_untrusted_text_is_preserved_without_formula_or_command_execution(self):
        content=export_csv(conditions="'=run_external_command()",source_excerpt='<script>unsafe()</script>')
        self.load(content)
        self.assertEqual(self.doc['fields']['timestep']['candidates'][0]['value'],"'=run_external_command()")
        self.assertEqual(preview_csv(content)['row']['source_excerpt'],'<script>unsafe()</script>')
