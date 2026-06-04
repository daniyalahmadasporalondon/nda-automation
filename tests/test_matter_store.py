from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nda_automation import matter_store
from nda_automation.matter_repository import DiskMatterRepository


def _create_kwargs(**overrides):
    kwargs = {
        "source_filename": "Mutual NDA.docx",
        "document_bytes": b"doc bytes",
        "extracted_text": "This Agreement is mutual.",
        "review_result": {"clauses": [{"id": "mutuality", "decision": "pass"}]},
        "triage": {"triage_status": "review"},
        "source_type": "manual_upload",
        "board_column": "intake",
    }
    kwargs.update(overrides)
    return kwargs


class MatterStorePersistenceTests(unittest.TestCase):
    def matter_store_patches(self, data_dir: str):
        root = Path(data_dir)
        return (
            patch.object(matter_store, "DATA_DIR", root),
            patch.object(matter_store, "MATTERS_PATH", root / "matters.json"),
            patch.object(matter_store, "UPLOADS_DIR", root / "uploads"),
        )

    def test_disk_store_migrates_legacy_matters_json_to_record_files(self):
        with tempfile.TemporaryDirectory() as data_dir:
            root = Path(data_dir)
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                matter_store._save_matters([{
                    "id": "matter_legacy",
                    "created_at": "2026-06-01T00:00:00+00:00",
                    "updated_at": "2026-06-01T00:00:00+00:00",
                    "source_filename": "Legacy NDA.docx",
                    "stored_filename": "matter_legacy-Legacy-NDA.docx",
                    "board_column": "gmail_demo",
                    "status": "active",
                }])
                repo = DiskMatterRepository()

                updated = repo.update_matter_stage("matter_legacy", "in_review")

                self.assertEqual(updated["board_column"], "in_review")
                self.assertTrue((root / "matters" / "matter_legacy.json").is_file())
                self.assertFalse(matter_store.MATTERS_PATH.exists())
                self.assertTrue((root / "matters.json.legacy").is_file())
                self.assertEqual(repo.get_matter("matter_legacy")["board_column"], "in_review")

    def test_disk_store_prefers_legacy_file_until_migration_finishes(self):
        with tempfile.TemporaryDirectory() as data_dir:
            root = Path(data_dir)
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                legacy_matter = {
                    "id": "matter_legacy",
                    "created_at": "2026-06-01T00:00:00+00:00",
                    "updated_at": "2026-06-01T00:00:00+00:00",
                    "source_filename": "Legacy NDA.docx",
                    "stored_filename": "matter_legacy-Legacy-NDA.docx",
                    "board_column": "gmail_demo",
                    "status": "active",
                }
                matter_store._save_matters([legacy_matter])
                (root / "matters").mkdir()
                matter_store._write_matter_record({
                    **legacy_matter,
                    "id": "matter_partial",
                    "board_column": "signed_closed",
                })
                repo = DiskMatterRepository()

                listed_before_migration = repo.list_matters()
                updated = repo.update_matter_stage("matter_legacy", "in_review")

                self.assertEqual([matter["id"] for matter in listed_before_migration], ["matter_legacy"])
                self.assertEqual(updated["board_column"], "in_review")
                self.assertEqual(repo.get_matter("matter_legacy")["board_column"], "in_review")

    def test_disk_create_update_delete_do_not_use_monolithic_save(self):
        with tempfile.TemporaryDirectory() as data_dir:
            root = Path(data_dir)
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                repo = DiskMatterRepository()

                with patch.object(matter_store, "_save_matters", side_effect=AssertionError("monolithic save used")):
                    matter = repo.create_matter(**_create_kwargs())
                    updated = repo.update_matter_stage(matter["id"], "in_review")
                    fielded = repo.update_matter_fields(matter["id"], {"human_reviewed": True})
                    deleted = repo.delete_matter(matter["id"])

                self.assertEqual(matter["id"], updated["id"])
                self.assertEqual(matter["id"], fielded["id"])
                self.assertEqual(matter["id"], deleted["id"])
                self.assertFalse(matter_store.MATTERS_PATH.exists())
                self.assertEqual(list((root / "matters").glob("*.json")), [])

    def test_disk_get_update_delete_load_single_record_after_migration(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                repo = DiskMatterRepository()
                matter = repo.create_matter(**_create_kwargs())

                with patch.object(matter_store, "_load_matters", side_effect=AssertionError("full scan used")):
                    fetched = repo.get_matter(matter["id"])
                    updated = repo.update_matter_stage(matter["id"], "in_review")
                    fielded = repo.update_matter_fields(matter["id"], {"human_reviewed": True})
                    deleted = repo.delete_matter(matter["id"])

                self.assertEqual(fetched["id"], matter["id"])
                self.assertEqual(updated["board_column"], "in_review")
                self.assertTrue(fielded["human_reviewed"])
                self.assertEqual(deleted["id"], matter["id"])


if __name__ == "__main__":
    unittest.main()
