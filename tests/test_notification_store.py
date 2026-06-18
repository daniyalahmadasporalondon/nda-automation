from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nda_automation import notification_store


class NotificationStoreTests(unittest.TestCase):
    def _patches(self, data_dir: str):
        root = Path(data_dir)
        return (
            patch.object(notification_store, "DATA_DIR", root),
            patch.object(notification_store, "NOTIFICATIONS_PATH", root / "notifications.json"),
        )

    def test_dedupe_active_key_bumps_count_one_row(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            p1, p2 = self._patches(data_dir)
            with p1, p2:
                first = notification_store.emit_event(
                    source="drive",
                    severity="error",
                    title="Drive archive failed",
                    detail="boom",
                    dedupe_key="drive_archive:m1",
                )
                second = notification_store.emit_event(
                    source="drive",
                    severity="error",
                    title="Drive archive failed",
                    detail="boom again",
                    dedupe_key="drive_archive:m1",
                )
                events = notification_store.list_events()
                # Same active dedupe_key -> ONE row, count bumped, no new id.
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["id"], first["id"])
                self.assertEqual(second["id"], first["id"])
                self.assertEqual(events[0]["count"], 2)
                # detail refreshed to the latest occurrence.
                self.assertEqual(events[0]["detail"], "boom again")
                self.assertEqual(notification_store.unread_count(), 1)

    def test_distinct_keys_make_distinct_rows(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            p1, p2 = self._patches(data_dir)
            with p1, p2:
                notification_store.emit_event(
                    source="drive", severity="error", title="A", dedupe_key="k1"
                )
                notification_store.emit_event(
                    source="gmail", severity="warning", title="B", dedupe_key="k2"
                )
                self.assertEqual(len(notification_store.list_events()), 2)
                self.assertEqual(notification_store.unread_count(), 2)

    def test_resolve_moves_active_to_resolved_and_drops_unread(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            p1, p2 = self._patches(data_dir)
            with p1, p2:
                notification_store.emit_event(
                    source="docusign", severity="error", title="reconnect", dedupe_key="ds:owner1"
                )
                self.assertEqual(notification_store.unread_count(), 1)
                resolved = notification_store.resolve_event("ds:owner1")
                self.assertIsNotNone(resolved)
                self.assertEqual(resolved["status"], "resolved")
                self.assertIsNotNone(resolved["resolved_at"])
                self.assertEqual(notification_store.unread_count(), 0)
                # Idempotent: nothing active left to resolve.
                self.assertIsNone(notification_store.resolve_event("ds:owner1"))
                # A resolved key re-emitting creates a FRESH active row (the old
                # condition recurred).
                again = notification_store.emit_event(
                    source="docusign", severity="error", title="reconnect", dedupe_key="ds:owner1"
                )
                self.assertEqual(again["status"], "active")
                self.assertEqual(notification_store.unread_count(), 1)
                self.assertEqual(len(notification_store.list_events(limit=500)), 2)

    def test_dismiss_by_id(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            p1, p2 = self._patches(data_dir)
            with p1, p2:
                event = notification_store.emit_event(
                    source="ai", severity="error", title="key invalid", dedupe_key="ai_key_invalid"
                )
                dismissed = notification_store.dismiss(event["id"])
                self.assertIsNotNone(dismissed)
                self.assertEqual(dismissed["status"], "dismissed")
                self.assertEqual(notification_store.unread_count(), 0)
                self.assertIsNone(notification_store.dismiss("does-not-exist"))

    def test_list_events_filters_and_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            p1, p2 = self._patches(data_dir)
            with p1, p2:
                notification_store.emit_event(
                    source="drive", severity="error", title="one", dedupe_key="a"
                )
                notification_store.emit_event(
                    source="drive", severity="error", title="two", dedupe_key="b"
                )
                notification_store.resolve_event("a")
                active = notification_store.list_events(status="active")
                self.assertEqual([e["title"] for e in active], ["two"])
                all_events = notification_store.list_events()
                # newest-first ordering (b created after a).
                self.assertEqual(all_events[0]["title"], "two")

    def test_cap_sheds_non_active_first(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            p1, p2 = self._patches(data_dir)
            with p1, p2, patch.object(notification_store, "MAX_EVENTS", 3):
                # Two resolved + then push active rows past the cap; the resolved
                # ones must be shed before any active row.
                notification_store.emit_event(
                    source="drive", severity="info", title="old1", dedupe_key="r1"
                )
                notification_store.resolve_event("r1")
                notification_store.emit_event(
                    source="drive", severity="info", title="old2", dedupe_key="r2"
                )
                notification_store.resolve_event("r2")
                for i in range(3):
                    notification_store.emit_event(
                        source="drive", severity="error", title=f"act{i}", dedupe_key=f"act{i}"
                    )
                events = notification_store.list_events(limit=500)
                self.assertLessEqual(len(events), 3)
                statuses = {e["status"] for e in events}
                # All survivors are active; the resolved rows were shed first.
                self.assertEqual(statuses, {"active"})
                self.assertEqual(notification_store.unread_count(), 3)

    def test_emit_event_never_raises_on_write_error(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            p1, p2 = self._patches(data_dir)
            with p1, p2, patch.object(
                notification_store, "_save_events", side_effect=OSError("disk full")
            ), patch("builtins.print"):
                # Forced write error must be swallowed: returns None, no exception.
                result = notification_store.emit_event(
                    source="system", severity="error", title="x", dedupe_key="x"
                )
                self.assertIsNone(result)

    def test_dedupe_key_helpers(self) -> None:
        self.assertEqual(notification_store.drive_archive_key("m1"), "drive_archive:m1")
        self.assertEqual(notification_store.drive_archive_key(""), "drive_archive:unknown")
        self.assertEqual(
            notification_store.docusign_reconnect_key("owner@x.com"),
            "docusign_reconnect:owner@x.com",
        )
        self.assertEqual(notification_store.docusign_reconnect_key(""), "docusign_reconnect:global")
        self.assertEqual(notification_store.ai_key_invalid_key(), "ai_key_invalid")
        self.assertEqual(
            notification_store.gmail_not_ready_key("o1", "no_token"),
            "gmail_not_ready:o1:no_token",
        )

    def test_emit_coerces_invalid_enums(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            p1, p2 = self._patches(data_dir)
            with p1, p2:
                event = notification_store.emit_event(
                    source="bogus", severity="loud", title="t", dedupe_key="k"
                )
                self.assertEqual(event["source"], "system")
                self.assertEqual(event["severity"], "error")


if __name__ == "__main__":
    unittest.main()
