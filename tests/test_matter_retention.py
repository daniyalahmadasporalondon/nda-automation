import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nda_automation import matter_store, telemetry


class MatterRetentionArchiveTests(unittest.TestCase):
    def test_pruned_matters_are_archived_before_deletion(self):
        pruned = [
            {
                "id": "m-active",
                "status": "active",
                "board_column": "in_review",
                "extracted_text": "confidential nda body",
            },
            {"id": "m-closed", "status": "closed", "board_column": "signed_closed"},
        ]
        before = telemetry.snapshot()["counters"].get("active_matters_pruned", 0)

        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            with patch.object(matter_store, "DATA_DIR", data_dir):
                matter_store._archive_pruned_matters(pruned)

                archive_dir = data_dir / matter_store.PRUNED_ARCHIVE_DIRNAME
                active_archive = archive_dir / "m-active.json"
                closed_archive = archive_dir / "m-closed.json"
                self.assertTrue(active_archive.is_file())
                self.assertTrue(closed_archive.is_file())
                restored = json.loads(active_archive.read_text(encoding="utf-8"))
                self.assertEqual(restored["extracted_text"], "confidential nda body")

        after = telemetry.snapshot()["counters"].get("active_matters_pruned", 0)
        # Only the active matter should count toward the active-pruning warning.
        self.assertEqual(after - before, 1)


if __name__ == "__main__":
    unittest.main()
