"""Private runtime assembly never borrows another application's data or keys."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from auto_lammps.manifest import canonical, sha256
from auto_lammps.papers import PaperStore
from auto_lammps.tasks import TaskStore
from auto_lammps.workbench_runtime import (WorkbenchRuntimeError,
    assemble_private_workbench)


class WorkbenchRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.storage = self.root/'workbench'; self.storage.mkdir(mode=0o700)
        self.tasks = TaskStore(self.root/'tasks.sqlite')
        self.papers = PaperStore(self.tasks)
        self.config_path = self.root/'workbench.json'
        self.config = {'version': 1, 'private_root': str(self.storage),
            'session_id': 'stable-project-session', 'bindings': []}
        self.save()

    def save(self):
        self.config_path.write_bytes(canonical(self.config)); self.config_path.chmod(0o600)

    def assemble(self):
        return assemble_private_workbench(self.papers, self.config_path)

    def test_missing_dependency_does_not_create_authority_or_claim_enabled(self):
        with patch('auto_lammps.workbench_runtime.importlib.import_module', side_effect=ImportError):
            with self.assertRaises(WorkbenchRuntimeError):
                self.assemble()
        self.assertEqual(list(self.storage.iterdir()), [])

    def test_unknown_fields_credentials_and_path_imports_are_rejected(self):
        for key in ('api_key', 'dependency_source_root', 'database_path'):
            self.config[key] = 'synthetic-private-value'; self.save()
            with self.assertRaises(WorkbenchRuntimeError):
                self.assemble()
            del self.config[key]
        self.assertEqual(list(self.storage.iterdir()), [])

    def test_public_configuration_is_rejected(self):
        self.config_path.chmod(0o644)
        with self.assertRaises(WorkbenchRuntimeError):
            self.assemble()

    def test_public_or_linked_storage_root_is_rejected(self):
        self.storage.chmod(0o755)
        with self.assertRaises(WorkbenchRuntimeError):
            self.assemble()
        self.storage.chmod(0o700)
        link = self.root/'linked'; link.symlink_to(self.storage)
        self.config['private_root'] = str(link); self.save()
        with self.assertRaises(WorkbenchRuntimeError):
            self.assemble()

    def test_git_worktree_storage_is_rejected(self):
        (self.storage/'.git').mkdir()
        with self.assertRaises(WorkbenchRuntimeError):
            self.assemble()

    def test_duplicate_bindings_and_source_controlled_role_are_rejected(self):
        binding = {'paper_id': 'a'*32, 'workbench_paper_id': 7, 'pdf_sha256': 'b'*64}
        for bindings in ([binding, binding], [{**binding, 'role': 'independent_B'}],
                [binding, {**binding, 'paper_id': 'c'*32}]):
            self.config['bindings'] = bindings; self.save()
            with self.assertRaises(WorkbenchRuntimeError):
                self.assemble()
        self.assertEqual(list(self.storage.iterdir()), [])

    def test_duplicate_JSON_fields_are_rejected(self):
        self.config_path.write_text('{"version":1,"version":1}')
        with self.assertRaises(WorkbenchRuntimeError):
            self.assemble()


@unittest.skipUnless(importlib.util.find_spec('auto_research') is not None,
    'separately installed literature workbench is optional')
class InstalledWorkbenchRuntimeTests(WorkbenchRuntimeTests):
    def test_dedicated_storage_has_original_exports_and_persistent_snapshot_key(self):
        import fitz
        bridge, bindings = self.assemble()
        self.assertEqual(bindings, {})
        self.assertEqual(bridge.runtime.database.path, self.storage/'evidence.sqlite')
        for name in ('seal.key', 'owner.json', 'evidence.sqlite'):
            self.assertEqual((self.storage/name).stat().st_mode & 0o777, 0o600)
        first_key = (self.storage/'seal.key').read_bytes()
        source = self.root/'synthetic.pdf'
        pdf = fitz.open(); page = pdf.new_page(); page.insert_text((72, 72), 'Synthetic PDF facts.')
        pdf.save(source); pdf.close()
        paper = self.papers.add('Synthetic source', '10.1234/source', 'scope', 'test')
        paper = self.papers.select(paper['id'], paper['revision'])
        task = self.tasks.create('Synthetic source task', 'No physics', 'reproduction')
        self.papers.link_task(paper['id'], paper['revision'], task['id'])
        upstream_id = bridge.runtime.database.upsert_paper(title=paper['title'], doi=paper['doi'],
            pdf_path=str(source), pdf_sha256=sha256(source.read_bytes()))
        authority = bridge.runtime.jobs._snapshots
        handle, snapshot = authority.capture(str(source), max_pages=64)
        sealed_files = list((self.storage/'snapshots').glob('*.sealed'))
        self.assertEqual(len(sealed_files), 1)
        self.assertNotIn(b'Synthetic PDF facts', sealed_files[0].read_bytes())
        self.config['bindings'] = [{'paper_id': paper['id'], 'workbench_paper_id': upstream_id,
            'pdf_sha256': sha256(source.read_bytes())}]; self.save()
        restarted, mappings = self.assemble()
        self.assertEqual((self.storage/'seal.key').read_bytes(), first_key)
        restored = restarted.runtime.jobs._snapshots
        restored.restore_persisted(handle, snapshot)
        restored.assert_fresh(handle)
        evidence = restarted.evidence(task['id'], mappings[paper['id']])
        self.assertEqual(evidence['title'], paper['title'])
        self.assertEqual(evidence['items'], [])

    def test_existing_foreign_database_is_not_initialized(self):
        foreign = self.storage/'evidence.sqlite'
        foreign.write_bytes(b'foreign workspace data'); foreign.chmod(0o600)
        with self.assertRaises(WorkbenchRuntimeError):
            self.assemble()
        self.assertEqual(foreign.read_bytes(), b'foreign workspace data')
        self.assertFalse((self.storage/'seal.key').exists())

    def test_missing_or_replaced_key_is_not_regenerated(self):
        self.assemble()
        owner = (self.storage/'owner.json').read_bytes()
        key_path = self.storage/'seal.key'; original_key = key_path.read_bytes(); key_path.unlink()
        with self.assertRaises(WorkbenchRuntimeError):
            self.assemble()
        self.assertFalse(key_path.exists())
        key_path.write_bytes(os.urandom(32)); key_path.chmod(0o600)
        with self.assertRaises(WorkbenchRuntimeError):
            self.assemble()
        self.assertNotEqual(key_path.read_bytes(), original_key)
        self.assertEqual((self.storage/'owner.json').read_bytes(), owner)

    def test_changed_project_or_session_is_rejected(self):
        self.assemble()
        self.config['session_id'] = 'different-project-session'; self.save()
        with self.assertRaises(WorkbenchRuntimeError):
            self.assemble()
        self.config['session_id'] = 'stable-project-session'; self.save()
        other_papers = PaperStore(TaskStore(self.root/'other.sqlite'))
        with self.assertRaises(WorkbenchRuntimeError):
            assemble_private_workbench(other_papers, self.config_path)

    def test_linked_database_and_checkpoint_file_are_rejected(self):
        self.assemble()
        for path in (self.storage/'evidence.sqlite',
                self.storage/'checkpoints'/'literature-task-checkpoints-v1.sqlite'):
            content = path.read_bytes(); path.unlink()
            foreign = self.root/(path.name+'.outside'); foreign.write_bytes(content); foreign.chmod(0o600)
            path.symlink_to(foreign)
            with self.assertRaises(WorkbenchRuntimeError):
                self.assemble()
            self.assertEqual(foreign.read_bytes(), content)
            path.unlink(); path.write_bytes(content); path.chmod(0o600)
