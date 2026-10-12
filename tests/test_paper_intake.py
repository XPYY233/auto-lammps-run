"""Registration is durable/idempotent, does not replace sources or call AI."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from auto_lammps.manifest import sha256
from auto_lammps.paper_intake import PaperIntake, MAX_PDF_BYTES
from auto_lammps.papers import PaperStore
from auto_lammps.tasks import TaskStore, TaskError
from auto_lammps.workbench_bridge import WorkbenchPaperBinding

TITLE = 'Mechanical properties of a synthetic alloy specimen'
DOI = '10.1234/synthetic.001'
SCOPE = 'Extract paper figures and conditions before choosing targets.'
PDF = ('%PDF-1.7\n'+TITLE+'\nhttps://doi.org/'+DOI).encode()


def metadata(data):
    return dict(pages=2, title=TITLE, dois=[DOI], content=data.decode())


class NativeDatabase:
    def __init__(self):
        self.rows = {}; self.inserts = 0; self.interrupt = False

    def list_papers(self):
        return list(self.rows.values())

    def get_paper(self, identifier):
        return self.rows.get(identifier)

    def upsert_paper(self, **data):
        self.inserts += 1
        identifier = len(self.rows)+1
        self.rows[identifier] = dict(data, id=identifier)
        if self.interrupt:
            self.interrupt = False
            raise OSError('synthetic interruption after native commit')
        return identifier


class PaperIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); self.root.chmod(0o700)
        self.tasks = TaskStore(self.root/'tasks.sqlite'); self.papers = PaperStore(self.tasks)
        self.native = NativeDatabase()
        self.runtime = SimpleNamespace(pdf_intake_root=self.root/'imports', database=self.native,
            native_source_reader=lambda identifier:sha256(Path(self.native.rows[identifier]['pdf_path']).read_bytes()))
        self.bridge = SimpleNamespace(runtime=self.runtime)
        self.intake = self.reopen()

    def reopen(self, configured=None):
        return PaperIntake(self.papers, self.bridge, configured, pdf_reader=metadata)

    def upload(self, identifier='a'*32, data=PDF):
        return self.intake.upload(identifier, data)

    def confirm(self, identifier='a'*32, **changes):
        return self.intake.confirm(identifier, **dict(title=TITLE, doi=DOI, scope=SCOPE, **changes))

    def test_new_paper_upload_bind_task_and_restart_preserve_identity(self):
        saved = self.upload(); self.assertEqual(saved['state'], 'uploaded')
        self.assertEqual(self.papers.list()['papers'], [])
        bound = self.confirm(); self.assertEqual(bound['state'], 'bound')
        task = self.intake.create_task(saved['id'])
        reopened = self.reopen()
        self.assertEqual(reopened.create_task(saved['id']), task)
        self.assertEqual(reopened.binding(bound['paper_id']).pdf_sha256, sha256(PDF))
        self.assertEqual(len(self.tasks.list()), 1)
        self.assertEqual(self.native.inserts, 1)
        self.assertEqual(self.papers.get(bound['paper_id'])['tasks'][0]['id'], task['id'])
        self.assertEqual(task['mode'], 'reproduction')

    def test_duplicate_pdf_returns_same_record_not_new_request_or_task(self):
        original = self.upload()
        duplicate = self.upload('b'*32)
        self.assertEqual(original, duplicate)
        self.assertEqual(len(self.intake.list()['imports']), 1)
        self.confirm(); task = self.intake.create_task(original['id'])
        self.assertEqual(self.intake.create_task(original['id']), task)

    def test_same_request_cannot_replace_pdf(self):
        self.upload()
        with self.assertRaises(TaskError):
            self.upload(data=PDF+b'changed')
        self.assertEqual(self.intake.get('a'*32)['pdf_sha256'], sha256(PDF))

    def test_invalid_pdf_and_size_leave_no_registration(self):
        for data in (b'not PDF', b'', b'%PDF-'+b'x'*MAX_PDF_BYTES):
            with self.assertRaises(TaskError):self.upload(data=data)
        self.assertEqual(self.intake.list()['imports'], [])
        self.assertEqual(list((self.root/'imports').iterdir()), [])

    def test_identity_mismatch_keeps_upload_editable_without_paper(self):
        self.upload()
        for title, doi in ((TITLE+'not present', DOI), (TITLE, '10.1234/elsewhere')):
            with self.assertRaises(TaskError):
                self.intake.confirm('a'*32, title=title, doi=doi, scope=SCOPE)
        self.assertEqual(self.intake.get('a'*32)['state'], 'uploaded')
        self.assertEqual(self.papers.list()['papers'], [])
        self.assertEqual(self.native.inserts, 0)

    def test_interrupted_native_registration_resumes_same_paper_and_bytes(self):
        self.upload(); self.native.interrupt = True
        with self.assertRaises(OSError):self.confirm()
        prepared = self.intake.get('a'*32)
        self.assertEqual(prepared['state'], 'prepared')
        self.intake = self.reopen(); bound = self.confirm()
        self.assertEqual(bound['paper_id'], prepared['paper_id'])
        self.assertEqual(self.native.inserts, 1)
        self.assertEqual(len(self.papers.list()['papers']), 1)

    def test_fixed_identity_cannot_change_during_recovery(self):
        self.upload(); self.native.interrupt = True
        with self.assertRaises(OSError):self.confirm()
        with self.assertRaises(TaskError):
            self.intake.confirm('a'*32, title=TITLE, doi=DOI, scope='different scope')
        self.assertEqual(self.native.inserts, 1)

    def test_existing_doi_title_conflict_is_not_overwritten(self):
        paper = self.papers.add('Another registered title', DOI, SCOPE, 'Keep history')
        self.upload()
        with self.assertRaises(TaskError):self.confirm()
        self.assertEqual(self.papers.get(paper['id'])['revision'], paper['revision'])
        self.assertEqual(self.native.inserts, 0)

    def test_existing_bound_source_is_reused_without_upsert(self):
        self.upload(); bound = self.confirm(); before = self.native.inserts
        again = self.confirm()
        self.assertEqual(bound, again); self.assertEqual(self.native.inserts, before)

    def test_different_source_for_existing_doi_is_not_rebound(self):
        self.upload(); bound = self.confirm()
        self.upload('b'*32, PDF+b'\nSecond copy')
        with self.assertRaises(TaskError):self.confirm('b'*32)
        self.assertEqual(self.intake.binding(bound['paper_id']).pdf_sha256, sha256(PDF))

    def test_existing_unbound_native_source_conflict_does_not_overwrite_evidence(self):
        self.native.rows[1] = dict(id=1, title=TITLE, doi=DOI, pdf_sha256='b'*64, pdf_path='not read')
        self.upload()
        with self.assertRaises(TaskError):self.confirm()
        self.assertEqual(self.native.inserts, 0)
        self.assertEqual(self.native.rows[1]['pdf_sha256'], 'b'*64)

    def test_changed_source_cannot_create_task(self):
        saved = self.upload(); self.confirm()
        (self.root/'imports'/(saved['pdf_sha256']+'.pdf')).write_bytes(PDF+b'changed')
        with self.assertRaises(TaskError):self.intake.create_task(saved['id'])
        self.assertEqual(self.tasks.list(), [])

    def test_immutable_binding_and_history_and_no_private_paths_in_views(self):
        self.upload(); self.confirm()
        with self.tasks.transaction() as db:
            for sql in ('UPDATE user_workbench_bindings SET pdf_sha256=\'changed\'',
                        'DELETE FROM paper_import_revisions'):
                with self.assertRaises(Exception):db.execute(sql)
        self.assertNotIn(str(self.root), str(self.intake.list()))
        self.assertEqual((self.root/'imports'/(sha256(PDF)+'.pdf')).stat().st_mode&0o777, 0o600)

    def test_unconfigured_intake_is_disabled_not_fake_success(self):
        other = PaperIntake(self.papers, None)
        self.assertFalse(other.availability()['configured'])
        with self.assertRaises(TaskError):other.upload('a'*32, PDF)

    def test_configuration_cannot_override_persistent_binding(self):
        self.upload(); bound = self.confirm()
        conflicting = WorkbenchPaperBinding(bound['paper_id'], 99, 'c'*64)
        with self.assertRaises(TaskError):self.reopen({bound['paper_id']:conflicting}).binding(bound['paper_id'])


if __name__ == '__main__':unittest.main()
