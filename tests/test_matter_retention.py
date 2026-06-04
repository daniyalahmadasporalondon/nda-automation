import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nda_automation import matter_store


class MatterRetentionArchiveTests(unittest.TestCase):
    def test_pruned_matters_are_archived_before_deletion(self):
        pruned = [
            {
                "id": "m-closed",
                "status": "closed",
                "board_column": "signed_closed",
                "extracted_text": "confidential nda body",
            },
        ]

        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            with patch.object(matter_store, "DATA_DIR", data_dir):
                archived = matter_store._archive_pruned_matters(pruned)

                archive_dir = data_dir / matter_store.PRUNED_ARCHIVE_DIRNAME
                closed_archive = archive_dir / "m-closed.json"
                self.assertTrue(archived)
                self.assertTrue(closed_archive.is_file())
                restored = json.loads(closed_archive.read_text(encoding="utf-8"))
                self.assertEqual(restored["extracted_text"], "confidential nda body")

    def test_archive_pruned_matters_fails_closed_on_write_error(self):
        pruned = [{"id": "m-closed", "status": "closed", "board_column": "signed_closed"}]

        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            with patch.object(matter_store, "DATA_DIR", data_dir):
                with patch.object(Path, "replace", side_effect=OSError("boom")):
                    with patch("builtins.print"):
                        archived = matter_store._archive_pruned_matters(pruned)

        self.assertFalse(archived)


if __name__ == "__main__":
    unittest.main()
