"""matter_timeline growth control (F5): stored cap + list-payload exclusion.

* ``matter_store.capped_timeline`` keeps the NEWEST ``TIMELINE_MAX_EVENTS`` entries,
  replacing the dropped oldest with ONE leading ``timeline_truncated`` marker whose
  ``dropped_count`` accumulates (never a stack of markers). Every timeline writer
  (append_timeline_event / record_matter_approval / the approval_reset append) caps
  through it, in both the disk store and the in-memory repository.
* ``matter_view.public_matter(detail=False)`` -- the board LIST projection -- excludes
  the full ``matter_timeline`` log (a 5000-event matter added ~950KB to EVERY
  /api/matters poll); the DETAIL projection keeps it, and the list card still carries
  ``workflow_state.timeline_summary``.
"""

from __future__ import annotations

import unittest

from nda_automation import matter_store, matter_view
from nda_automation.matter_repository import InMemoryMatterRepository

CAP = matter_store.TIMELINE_MAX_EVENTS
MARKER = matter_store.TIMELINE_TRUNCATED_EVENT_TYPE


def _events(n: int, *, start: int = 0) -> list[dict]:
    return [{"type": "poll", "at": f"2026-06-30T00:00:{i:02d}", "seq": start + i} for i in range(n)]


class CappedTimelineTests(unittest.TestCase):
    def test_under_the_cap_is_returned_unchanged(self):
        timeline = _events(CAP)
        self.assertIs(matter_store.capped_timeline(timeline), timeline)

    def test_over_the_cap_keeps_newest_with_one_marker(self):
        timeline = _events(CAP + 250)
        capped = matter_store.capped_timeline(timeline)
        self.assertEqual(len(capped), CAP)
        self.assertEqual(capped[0]["type"], MARKER)
        self.assertEqual(capped[0]["dropped_count"], 251)  # 250 over + 1 for the marker slot
        # The kept events are exactly the NEWEST cap-1, in order.
        self.assertEqual(capped[1]["seq"], 251)
        self.assertEqual(capped[-1]["seq"], CAP + 249)

    def test_successive_truncations_accumulate_one_marker(self):
        capped = matter_store.capped_timeline(_events(CAP + 10))
        first_dropped = capped[0]["dropped_count"]
        # Append past the cap again: the OLD marker folds into the new one.
        capped2 = matter_store.capped_timeline(capped + _events(5, start=90000))
        self.assertEqual(len(capped2), CAP)
        self.assertEqual(capped2[0]["type"], MARKER)
        self.assertEqual(capped2[0]["dropped_count"], first_dropped + 5)
        # Never two stacked markers.
        self.assertNotEqual(capped2[1].get("type"), MARKER)

    def test_marker_with_bad_dropped_count_degrades_to_zero(self):
        timeline = [{"type": MARKER, "dropped_count": "garbage"}] + _events(CAP + 3)
        capped = matter_store.capped_timeline(timeline)
        self.assertEqual(len(capped), CAP)
        self.assertEqual(capped[0]["dropped_count"], 4)  # 3 over + 1 marker slot; garbage -> 0


class RepositoryTimelineCapTests(unittest.TestCase):
    def setUp(self):
        self.repo = InMemoryMatterRepository()
        self.matter = self.repo.create_matter(
            source_filename="NDA.docx",
            document_bytes=b"PK\x03\x04 fake docx",
            extracted_text="Mutual NDA text.",
            review_result={"clauses": []},
            triage={"triage_status": "review"},
            source_type="manual_upload",
            board_column="in_review",
            intake_metadata={"subject": "NDA"},
            owner_user_id="o",
        )

    def test_append_timeline_event_caps_the_stored_log(self):
        # Pre-load a log at the cap, then append one more: the store must hold the
        # cap exactly, newest kept, marker in front.
        for stored in self.repo._matters:
            if stored["id"] == self.matter["id"]:
                stored["matter_timeline"] = _events(CAP)
        updated = self.repo.append_timeline_event(
            self.matter["id"], {"type": "sent", "at": "2026-07-01T00:00:00"}, owner_user_id="o"
        )
        timeline = updated["matter_timeline"]
        self.assertEqual(len(timeline), CAP)
        self.assertEqual(timeline[0]["type"], MARKER)
        self.assertEqual(timeline[-1]["type"], "sent")

    def test_record_matter_approval_caps_the_stored_log(self):
        for stored in self.repo._matters:
            if stored["id"] == self.matter["id"]:
                stored["matter_timeline"] = _events(CAP + 40)
        updated = self.repo.record_matter_approval(
            self.matter["id"],
            approver="counsel@example.com",
            approved_at="2026-07-01T00:00:00",
            timeline_event={"type": "approved", "at": "2026-07-01T00:00:00"},
            owner_user_id="o",
        )
        timeline = updated["matter_timeline"]
        self.assertEqual(len(timeline), CAP)
        self.assertEqual(timeline[0]["type"], MARKER)
        self.assertEqual(timeline[-1]["type"], "approved")


class ListPayloadTimelineExclusionTests(unittest.TestCase):
    def _matter(self) -> dict:
        return {
            "id": "m1",
            "status": "active",
            "board_column": "in_review",
            "subject": "NDA request",
            "source_type": "manual_upload",
            "created_at": "2026-06-30T00:00:00+00:00",
            "updated_at": "2026-06-30T00:00:00+00:00",
            "matter_timeline": _events(120),
        }

    def test_list_projection_excludes_the_full_timeline(self):
        card = matter_view.public_matter(self._matter(), detail=False)
        self.assertNotIn("matter_timeline", card)
        # The compact summary still rides the list card for "last moved" display.
        summary = card["workflow_state"]["timeline_summary"]
        self.assertEqual(summary["event_count"], 120)

    def test_detail_projection_keeps_the_full_timeline(self):
        card = matter_view.public_matter(self._matter(), detail=True)
        self.assertEqual(len(card["matter_timeline"]), 120)

    def test_board_list_batch_excludes_the_timeline(self):
        cards = matter_view.public_matters([self._matter()])
        self.assertEqual(len(cards), 1)
        self.assertNotIn("matter_timeline", cards[0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
