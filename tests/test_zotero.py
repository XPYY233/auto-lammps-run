import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from auto_lammps.__main__ import save_private
from auto_lammps.zotero import LocalZotero, NoRedirect, Page, ZoteroError, reference_counts


def item(key, kind="journalArticle", parent=None):
    data = {"itemType": kind, "title": "Synthetic example"}
    if parent:
        data["parentItem"] = parent
    return {"key": key, "data": data}


class PaginationTests(unittest.TestCase):
    def setUp(self):
        self.client = LocalZotero()

    def test_collects_multiple_pages_without_truncation(self):
        first = [item(f"A{i:07d}") for i in range(100)]
        with patch.object(self.client, "page", side_effect=[Page(first, 101, "7"),
                Page([item("B0000000")], 101, "7"), Page(first[:1], 101, "7")]):
            records, version = self.client.collect({})
        self.assertEqual(len(records), 101)
        self.assertEqual(version, "7")

    def test_drift_and_duplicates_fail_closed(self):
        first = Page([item("A0000000")], 2, "7")
        for second in [Page([], 2, "7"), Page([item("A0000001")], 3, "7"),
                       Page([item("A0000001")], 2, "8"), Page(first.items, 2, "7")]:
            with self.subTest(second=second), patch.object(self.client, "page", side_effect=[first, second]):
                with self.assertRaises(ZoteroError):
                    self.client.collect({})

    def test_changed_final_version_rejected(self):
        with patch.object(self.client, "page", side_effect=[Page([], 0, "7"), Page([], 0, "8")]):
            with self.assertRaises(ZoteroError):
                self.client.collect({})

    def test_budget_is_not_silent_truncation(self):
        with patch.object(self.client, "page", return_value=Page([], 5, "1")):
            with self.assertRaises(ZoteroError):
                self.client.collect({}, max_items=4)

    def test_resolves_annotation_attachment_and_deduplicates(self):
        hits = [item("NOTE0001", "annotation", "ATTACH01"),
                item("ATTACH02", "attachment", "PAPER001")]
        with patch.object(self.client, "collect", side_effect=[(hits, "1"),
                ([item("ATTACH01", "attachment", "PAPER001"), item("PAPER001")], "1")]), \
                patch.object(self.client, "page", return_value=Page(hits[:1], 2, "1")):
            result = self.client.discover("LAMMPS")
        self.assertEqual(result["reference_records"], 1)
        self.assertEqual(result["papers"][0]["source_class"], "unresolved")

    def test_missing_parent_and_version_drift_rejected(self):
        hit = item("ATTACH01", "attachment", "PAPER001")
        for parents in [([], "1"), ([item("PAPER001")], "2")]:
            with self.subTest(parents=parents), patch.object(self.client, "collect", side_effect=[([hit], "1"), parents]):
                with self.assertRaises(ZoteroError):
                    self.client.discover("LAMMPS")

    def test_absent_version_is_disclosed(self):
        with patch.object(self.client, "collect", return_value=([], None)), \
                patch.object(self.client, "page", return_value=Page([], 0, None)):
            self.assertEqual(self.client.discover("LAMMPS")["consistency"], "count_only")

    def test_extra_descendants_do_not_expand_search_corpus(self):
        hit = item("ATTACH01", "attachment", "PAPER001")
        extras = [item("PAPER001"), item("ATTACH02", "attachment", "PAPER001"), item("PAPER999")]
        with patch.object(self.client, "collect", side_effect=[([hit], "1"), (extras, "1")]), \
                patch.object(self.client, "page", return_value=Page([hit], 1, "1")):
            result = self.client.discover("LAMMPS")
        self.assertEqual(result["reference_records"], 1)
        self.assertEqual(result["papers"][0]["local_item_key"], "PAPER001")

    def test_query_and_port_validation(self):
        for query in ["", " ", "a" * 201]:
            with self.assertRaises(ValueError):
                self.client.discover(query)
        for port in [True, 0, 65536, "23119"]:
            with self.assertRaises(ValueError):
                LocalZotero(port=port)

    def test_redirects_blocked(self):
        with self.assertRaises(ZoteroError):
            NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.org")

    def test_doi_duplicates_are_disclosed_without_dropping_records(self):
        records = [dict(local_item_key="PAPER001", doi="https://doi.org/10.1234/EXAMPLE"),
                   dict(local_item_key="PAPER002", doi="DOI:10.1234/example"),
                   dict(local_item_key="PAPER003", doi="not a DOI")]
        result = reference_counts(records)
        self.assertEqual(result["reference_records"], 3)
        self.assertEqual(result["distinct_doi_strings"], 1)
        self.assertEqual(result["records_without_valid_doi"], 1)
        self.assertEqual(result["duplicate_doi_groups"][0]["local_item_keys"], ["PAPER001", "PAPER002"])


class ExportTests(unittest.TestCase):
    def test_private_mode_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "export.json"
            save_private(path, {"synthetic": True})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(json.loads(path.read_text())["synthetic"])
            with self.assertRaises(FileExistsError):
                save_private(path, {})

    def test_git_root_and_worktree_file_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").write_text("synthetic worktree marker")
            with self.assertRaises(ValueError):
                save_private(root / "nested" / "export.json", {})

    def test_symlink_into_git_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            (root / "alias").symlink_to(repo, target_is_directory=True)
            with self.assertRaises(ValueError):
                save_private(root / "alias" / "export.json", {})


if __name__ == "__main__":
    unittest.main()
