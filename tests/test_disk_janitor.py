from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from nda_automation import disk_janitor, matter_store


def _write_archived_matter(
    archive_dir: Path,
    matter_id: str,
    *,
    json_bytes: int = 0,
    source_bytes: int | None = None,
    age_seconds_ago: float = 0.0,
    archived_at: str | None = None,
) -> Path:
    """Seed one archived matter: <id>.json (+ optional uploads/<id>.bin source).

    ``json_bytes`` pads the JSON to a known on-disk size; ``source_bytes`` (if
    given) writes a source doc under uploads/ and links it via
    archived_source_document. ``age_seconds_ago`` back-dates the JSON mtime;
    ``archived_at`` (ISO) overrides age via the record field.
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    record: dict = {"id": matter_id, "title": "x"}
    if archived_at is not None:
        record["archived_at"] = archived_at
    if source_bytes is not None:
        rel = f"uploads/{matter_id}.bin"
        source_path = archive_dir / rel
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(b"S" * source_bytes)
        record["archived_source_document"] = {
            "present": True,
            "archive_path": rel,
            "size_bytes": source_bytes,
        }
    payload = json.dumps(record)
    pad = max(0, json_bytes - len(payload))
    record["_pad"] = "P" * pad
    json_path = archive_dir / f"{matter_id}.json"
    json_path.write_text(json.dumps(record), encoding="utf-8")

    if age_seconds_ago:
        when = time.time() - age_seconds_ago
        os.utime(json_path, (when, when))
    return json_path


class ArchiveJanitorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.archive_dir = self.data_dir / matter_store.PRUNED_ARCHIVE_DIRNAME
        # Seed LIVE files that must NEVER be touched.
        (self.data_dir / "users.json").write_text("{}", encoding="utf-8")
        (self.data_dir / "users.json.tmp").write_text("{}", encoding="utf-8")
        (self.data_dir / "matters.lock").write_text("", encoding="utf-8")
        (self.data_dir / "sync-rotation.json").write_text("{}", encoding="utf-8")
        records_dir = self.data_dir / "matter_records"
        records_dir.mkdir(parents=True, exist_ok=True)
        (records_dir / "live-1.json").write_text('{"id":"live-1"}', encoding="utf-8")
        live_uploads = self.data_dir / "uploads"
        live_uploads.mkdir(parents=True, exist_ok=True)
        (live_uploads / "live-doc.bin").write_bytes(b"L" * 4096)

        self._patcher = patch.object(matter_store, "DATA_DIR", self.data_dir)
        self._patcher.start()
        # Reset the process-local rate-limit clock between tests.
        disk_janitor._LAST_ROTATION_MONOTONIC = 0.0

    def tearDown(self) -> None:
        self._patcher.stop()
        self._tmp.cleanup()

    def _live_files_intact(self) -> None:
        self.assertTrue((self.data_dir / "users.json").exists())
        self.assertTrue((self.data_dir / "users.json.tmp").exists())
        self.assertTrue((self.data_dir / "matters.lock").exists())
        self.assertTrue((self.data_dir / "sync-rotation.json").exists())
        self.assertTrue((self.data_dir / "matter_records" / "live-1.json").exists())
        self.assertTrue((self.data_dir / "uploads" / "live-doc.bin").exists())

    # (a) rotation deletes oldest until under the cap ------------------------

    def test_rotation_deletes_oldest_until_under_size_cap(self) -> None:
        # 10 matters, ~1000 bytes each, ages 0..9 days. keep_min=2, cap=5000.
        for i in range(10):
            _write_archived_matter(
                self.archive_dir,
                f"m{i:02d}",
                json_bytes=1000,
                age_seconds_ago=i * 86400.0,  # m09 is oldest
            )
        env = {
            disk_janitor.ARCHIVE_MAX_BYTES_ENV: "5000",
            disk_janitor.ARCHIVE_KEEP_MIN_ENV: "2",
            disk_janitor.ARCHIVE_RETENTION_DAYS_ENV: "0",  # size cap only
            disk_janitor.DISK_HIGH_WATERMARK_PCT_ENV: "0",
        }
        with patch.dict(os.environ, env, clear=False):
            summary = disk_janitor.run_archive_rotation()

        remaining = sorted(p.stem for p in self.archive_dir.glob("m*.json"))
        # Under-cap achieved and the OLDEST were removed first.
        self.assertLessEqual(summary["after_bytes"], 5000)
        self.assertGreater(summary["removed"], 0)
        # The newest survive; the oldest (m09, m08, ...) went first.
        self.assertIn("m00", remaining)
        self.assertNotIn("m09", remaining)
        self._live_files_intact()

    # (b) keeps the newest KEEP_MIN -----------------------------------------

    def test_keep_min_floor_is_never_deleted(self) -> None:
        for i in range(6):
            _write_archived_matter(
                self.archive_dir,
                f"m{i:02d}",
                json_bytes=1000,
                age_seconds_ago=i * 86400.0,
            )
        # Aggressive: tiny cap + everything ancient -> would delete all but
        # keep_min must pin the newest 4.
        env = {
            disk_janitor.ARCHIVE_MAX_BYTES_ENV: "1",
            disk_janitor.ARCHIVE_KEEP_MIN_ENV: "4",
            disk_janitor.ARCHIVE_RETENTION_DAYS_ENV: "1",
            disk_janitor.DISK_HIGH_WATERMARK_PCT_ENV: "0",
        }
        with patch.dict(os.environ, env, clear=False):
            disk_janitor.run_archive_rotation()

        remaining = sorted(p.stem for p in self.archive_dir.glob("m*.json"))
        self.assertEqual(len(remaining), 4)
        # The 4 NEWEST (m00..m03) survive; the 2 oldest are gone.
        self.assertEqual(remaining, ["m00", "m01", "m02", "m03"])
        self._live_files_intact()

    def test_age_cutoff_drops_only_old_entries(self) -> None:
        # 3 old (40 days) + 3 fresh (1 day), keep_min small, no size cap.
        for i in range(3):
            _write_archived_matter(
                self.archive_dir, f"old{i}", json_bytes=500, age_seconds_ago=40 * 86400.0
            )
        for i in range(3):
            _write_archived_matter(
                self.archive_dir, f"new{i}", json_bytes=500, age_seconds_ago=1 * 86400.0
            )
        env = {
            disk_janitor.ARCHIVE_MAX_BYTES_ENV: "0",  # age trigger only
            disk_janitor.ARCHIVE_KEEP_MIN_ENV: "2",
            disk_janitor.ARCHIVE_RETENTION_DAYS_ENV: "30",
            disk_janitor.DISK_HIGH_WATERMARK_PCT_ENV: "0",
        }
        with patch.dict(os.environ, env, clear=False):
            disk_janitor.run_archive_rotation()
        remaining = sorted(p.stem for p in self.archive_dir.glob("*.json"))
        # All fresh survive; old ones beyond keep_min are dropped. keep_min=2
        # pins the 2 newest overall (both "new"), so at most 1 old may survive
        # only if within keep_min -- here newest 2 are new*, so oldest old* go.
        self.assertTrue(all(name.startswith("new") for name in remaining) or
                        len([n for n in remaining if n.startswith("old")]) < 3)
        self.assertIn("new0", remaining)
        self._live_files_intact()

    # (c) never touches live files outside pruned-matters/ -------------------

    def test_live_files_never_touched(self) -> None:
        for i in range(5):
            _write_archived_matter(
                self.archive_dir, f"m{i}", json_bytes=2000, source_bytes=2000,
                age_seconds_ago=i * 86400.0,
            )
        env = {
            disk_janitor.ARCHIVE_MAX_BYTES_ENV: "1",
            disk_janitor.ARCHIVE_KEEP_MIN_ENV: "1",
            disk_janitor.ARCHIVE_RETENTION_DAYS_ENV: "1",
            disk_janitor.DISK_HIGH_WATERMARK_PCT_ENV: "0",
        }
        with patch.dict(os.environ, env, clear=False):
            summary = disk_janitor.run_archive_rotation()
        self.assertGreater(summary["removed"], 0)
        self._live_files_intact()
        # The live uploads/live-doc.bin is a DIFFERENT tree from the archived
        # sources under pruned-matters/uploads/ -- confirm it is byte-intact.
        self.assertEqual(
            (self.data_dir / "uploads" / "live-doc.bin").read_bytes(), b"L" * 4096
        )

    def test_deletes_archived_source_document_with_json(self) -> None:
        jp = _write_archived_matter(
            self.archive_dir, "m0", json_bytes=100, source_bytes=3000,
            age_seconds_ago=99 * 86400.0,
        )
        # Add keep_min padding of newer entries so m0 is deletable.
        for i in range(1, 3):
            _write_archived_matter(self.archive_dir, f"m{i}", json_bytes=100,
                                   age_seconds_ago=(3 - i) * 86400.0)
        source = self.archive_dir / "uploads" / "m0.bin"
        self.assertTrue(source.exists())
        env = {
            disk_janitor.ARCHIVE_MAX_BYTES_ENV: "1",
            disk_janitor.ARCHIVE_KEEP_MIN_ENV: "2",
            disk_janitor.ARCHIVE_RETENTION_DAYS_ENV: "1",
            disk_janitor.DISK_HIGH_WATERMARK_PCT_ENV: "0",
        }
        with patch.dict(os.environ, env, clear=False):
            disk_janitor.run_archive_rotation()
        self.assertFalse(jp.exists())
        self.assertFalse(source.exists())  # source deleted alongside its JSON

    # (d) symlink / .. escape is refused ------------------------------------

    def test_symlink_escape_target_is_refused(self) -> None:
        # A live secret OUTSIDE the archive.
        secret = self.data_dir / "users.json"
        # An archive JSON whose "source" symlinks to the live secret.
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        (self.archive_dir / "uploads").mkdir(parents=True, exist_ok=True)
        evil_link = self.archive_dir / "uploads" / "escape.bin"
        try:
            evil_link.symlink_to(secret)
        except (OSError, NotImplementedError):
            self.skipTest("platform does not support symlinks")
        record = {
            "id": "evil",
            "archived_source_document": {
                "present": True,
                "archive_path": "uploads/escape.bin",
            },
        }
        (self.archive_dir / "evil.json").write_text(json.dumps(record), encoding="utf-8")
        os.utime(self.archive_dir / "evil.json",
                 (time.time() - 99 * 86400, time.time() - 99 * 86400))
        # Fillers so "evil" is beyond keep_min.
        for i in range(3):
            _write_archived_matter(self.archive_dir, f"m{i}", json_bytes=100,
                                   age_seconds_ago=(3 - i) * 86400.0)
        env = {
            disk_janitor.ARCHIVE_MAX_BYTES_ENV: "1",
            disk_janitor.ARCHIVE_KEEP_MIN_ENV: "1",
            disk_janitor.ARCHIVE_RETENTION_DAYS_ENV: "1",
            disk_janitor.DISK_HIGH_WATERMARK_PCT_ENV: "0",
        }
        with patch.dict(os.environ, env, clear=False):
            disk_janitor.run_archive_rotation()
        # The live secret the symlink pointed at is UNTOUCHED.
        self.assertTrue(secret.exists())
        self.assertEqual(secret.read_text(encoding="utf-8"), "{}")
        self._live_files_intact()

    def test_dotdot_symlink_json_is_not_followed_out(self) -> None:
        # A .json symlink inside the archive that points OUT to a live file must
        # never be treated as an archived matter (is_symlink guard).
        live = self.data_dir / "matter_records" / "live-1.json"
        link = self.archive_dir
        link.mkdir(parents=True, exist_ok=True)
        evil_json = self.archive_dir / "sneaky.json"
        try:
            evil_json.symlink_to(live)
        except (OSError, NotImplementedError):
            self.skipTest("platform does not support symlinks")
        for i in range(3):
            _write_archived_matter(self.archive_dir, f"m{i}", json_bytes=100,
                                   age_seconds_ago=i * 86400.0)
        env = {
            disk_janitor.ARCHIVE_MAX_BYTES_ENV: "1",
            disk_janitor.ARCHIVE_KEEP_MIN_ENV: "0",
            disk_janitor.ARCHIVE_RETENTION_DAYS_ENV: "1",
            disk_janitor.DISK_HIGH_WATERMARK_PCT_ENV: "0",
        }
        with patch.dict(os.environ, env, clear=False):
            disk_janitor.run_archive_rotation()
        self.assertTrue(live.exists())
        self.assertEqual(live.read_text(encoding="utf-8"), '{"id":"live-1"}')

    # (e) a missing / failed entry doesn't raise ----------------------------

    def test_missing_entry_does_not_raise(self) -> None:
        for i in range(4):
            _write_archived_matter(self.archive_dir, f"m{i}", json_bytes=1000,
                                   age_seconds_ago=i * 86400.0)
        env = {
            disk_janitor.ARCHIVE_MAX_BYTES_ENV: "1",
            disk_janitor.ARCHIVE_KEEP_MIN_ENV: "1",
            disk_janitor.ARCHIVE_RETENTION_DAYS_ENV: "1",
            disk_janitor.DISK_HIGH_WATERMARK_PCT_ENV: "0",
        }
        real_unlink = Path.unlink

        def flaky_unlink(self, *a, **k):  # first unlink vanishes, then raises
            if self.name == "m03.json":
                raise OSError("simulated race: entry vanished")
            return real_unlink(self, *a, **k)

        with patch.dict(os.environ, env, clear=False):
            with patch.object(Path, "unlink", flaky_unlink):
                # Must NOT raise despite the failing entry.
                summary = disk_janitor.run_archive_rotation()
        self.assertIsInstance(summary, dict)
        self._live_files_intact()

    def test_no_archive_dir_is_noop(self) -> None:
        # No pruned-matters/ at all.
        summary = disk_janitor.run_archive_rotation()
        self.assertEqual(summary["removed"], 0)
        self.assertEqual(summary["skipped"], "no_archive_dir")
        self._live_files_intact()

    # (f) disk-usage read failure -> no deletion (watermark-only) ------------

    def test_disk_usage_read_failure_disables_watermark_deletion(self) -> None:
        # Fresh, under-cap entries: ONLY the watermark could trigger deletion.
        for i in range(5):
            _write_archived_matter(self.archive_dir, f"m{i}", json_bytes=100,
                                   age_seconds_ago=i * 3600.0)
        env = {
            disk_janitor.ARCHIVE_MAX_BYTES_ENV: "0",   # no size trigger
            disk_janitor.ARCHIVE_RETENTION_DAYS_ENV: "0",  # no age trigger
            disk_janitor.ARCHIVE_KEEP_MIN_ENV: "1",
            disk_janitor.DISK_HIGH_WATERMARK_PCT_ENV: "1",  # would fire if usage known
        }
        with patch.dict(os.environ, env, clear=False):
            with patch.object(disk_janitor, "disk_usage", return_value=None):
                summary = disk_janitor.run_archive_rotation()
        self.assertEqual(summary["removed"], 0)
        remaining = sorted(p.stem for p in self.archive_dir.glob("m*.json"))
        self.assertEqual(len(remaining), 5)  # nothing deleted on unknown disk
        self._live_files_intact()

    def test_disk_usage_helper_returns_percent(self) -> None:
        usage = disk_janitor.disk_usage(self.data_dir)
        self.assertIsNotNone(usage)
        assert usage is not None
        self.assertGreater(usage.total, 0)
        self.assertGreaterEqual(usage.percent, 0.0)
        self.assertLessEqual(usage.percent, 100.0)

    def test_disk_usage_bad_path_returns_none(self) -> None:
        self.assertIsNone(disk_janitor.disk_usage(self.data_dir / "does-not-exist"))

    # rate-limit ------------------------------------------------------------

    def test_rate_limit_skips_second_run_but_force_overrides(self) -> None:
        _write_archived_matter(self.archive_dir, "m0", json_bytes=100)
        env = {disk_janitor.ARCHIVE_ROTATION_MIN_INTERVAL_ENV: "3600"}
        with patch.dict(os.environ, env, clear=False):
            first = disk_janitor.maybe_run_archive_rotation()
            self.assertIsNotNone(first)
            second = disk_janitor.maybe_run_archive_rotation()
            self.assertIsNone(second)  # rate-limited
            forced = disk_janitor.maybe_run_archive_rotation(force=True)
            self.assertIsNotNone(forced)  # force bypasses the limit

    def test_maybe_run_never_raises(self) -> None:
        with patch.object(disk_janitor, "run_archive_rotation",
                          side_effect=RuntimeError("boom")):
            # force=True to bypass the rate-limit and actually invoke.
            self.assertIsNone(disk_janitor.maybe_run_archive_rotation(force=True))


if __name__ == "__main__":
    unittest.main()
