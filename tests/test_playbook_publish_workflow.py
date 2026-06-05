"""End-to-end Playbook draft/publish coupling to the AI packet and review staleness.

These tests cover the cross-cutting guarantees that the draft/publish foundation
exists to provide, exercised through real draft/publish operations rather than
synthetic hashes:

  * A saved draft must NOT change the AI packet (drafts live in a sidecar; the
    packet is built from the active published playbook only).
  * Publishing a draft MUST change future packets, and the changed survival
    term / jurisdiction / fallback wording must surface in the packet.
  * A review result records the active Playbook version + hash it ran against.
  * A previously-fresh review goes stale once a different Playbook is published,
    and refreshing reruns against the newly published Playbook + hash.

The active playbook lives in an isolated temp file. The handlers and the runtime
helper both accept an explicit ``playbook_path``, and the packet is built from
the active published file the same way ``ai_assessor`` / ``ai_first_review`` do
(they read the active playbook and pass it to ``build_ai_assessment_packet``).
We inject those explicit paths/runtimes rather than patching module globals,
because ``ensure_active_playbook_runtime``'s default ``playbook_path`` is bound
to the production path at import time.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from nda_automation import matter_store, review_engine
from nda_automation.ai_assessment_prompt import build_ai_assessment_packet
from nda_automation.checker import REVIEW_ENGINE_VERSION, load_playbook
from nda_automation.review_staleness import review_result_staleness
from nda_automation.routes import matters as matter_routes
from nda_automation.routes import playbook as playbook_routes


class _JsonHandler:
    def __init__(self, payload: dict | None = None):
        self.payload = payload
        self.status = None
        self.response = None
        self.send_body = None

    def _read_json_payload(self):
        return self.payload

    def _send_json(self, payload, *, status=200, send_body=True):
        self.status = status
        self.response = payload
        self.send_body = send_body


def _clause(playbook: dict, clause_id: str) -> dict:
    return next(clause for clause in playbook["clauses"] if clause["id"] == clause_id)


def _packet_clause(packet: dict, clause_id: str) -> dict:
    return next(clause for clause in packet["playbook"]["clauses"] if clause["clause_id"] == clause_id)


def _set_preferred_law(playbook: dict, preferred_law: str) -> None:
    # Mirror an editor submission: changing the preferred jurisdiction sets
    # preferred_law AND syncs the matching approved_options default, which the
    # rules validator requires to stay consistent.
    governing_law = _clause(playbook, "governing_law")
    governing_law["preferred_law"] = preferred_law
    for option in governing_law["rules"]["approved_options"]:
        option["default"] = option["value"] == preferred_law


class PlaybookPublishWorkflowTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.playbook_path = Path(self._dir.name) / "playbook.json"
        self.original_playbook = deepcopy(load_playbook())
        self.playbook_path.write_text(json.dumps(self.original_playbook), encoding="utf-8")
        playbook_routes.ensure_active_playbook_runtime(playbook_path=self.playbook_path)

    # --- helpers tied to the isolated active playbook ---------------------

    def _active_runtime(self) -> dict:
        return playbook_routes.ensure_active_playbook_runtime(playbook_path=self.playbook_path)

    def _active_playbook(self) -> dict:
        # The active published playbook: the same file publish writes to and the
        # same content the live review path feeds into the packet builder.
        return playbook_routes.read_playbook_from_path(self.playbook_path)

    def _active_packet(self) -> dict:
        return build_ai_assessment_packet("Sample NDA text.", playbook=self._active_playbook())

    def _review_with_active_runtime(self, text, ai_first_review_func):
        # Run the engine against this test's isolated active runtime rather than
        # the production runtime its default would otherwise read.
        return review_engine.review_nda_with_active_engine(
            text,
            ai_first_review_func=ai_first_review_func,
            playbook_runtime_func=lambda: self._active_runtime(),
        )

    def _save_draft(self, draft_playbook: dict) -> str:
        runtime = self._active_runtime()
        handler = _JsonHandler({
            "playbook": draft_playbook,
            "actor": "legal-admin",
            "expected_base_active_hash": runtime["active_hash"],
        })
        playbook_routes.handle_playbook_draft_save(handler, playbook_path=self.playbook_path)
        self.assertEqual(handler.status, 200, handler.response)
        return handler.response["draft"]["metadata"]["draft_id"]

    def _publish_draft(self, draft_id: str) -> dict:
        runtime = self._active_runtime()
        handler = _JsonHandler({
            "draft_id": draft_id,
            "actor": "legal-admin",
            "expected_active_hash": runtime["active_hash"],
        })
        playbook_routes.handle_playbook_publish(handler, playbook_path=self.playbook_path)
        self.assertEqual(handler.status, 200, handler.response)
        return handler.response

    # --- draft does not change the packet; publish does -------------------

    def test_saved_draft_does_not_change_ai_packet(self):
        baseline_packet = self._active_packet()

        draft_playbook = deepcopy(self.original_playbook)
        _clause(draft_playbook, "term_and_survival")["max_term_years"] = 7
        self._save_draft(draft_playbook)

        packet_after_draft = self._active_packet()
        # The draft sidecar must not leak into the packet built from the active playbook.
        self.assertEqual(packet_after_draft["playbook"], baseline_packet["playbook"])
        term_clause = _packet_clause(packet_after_draft, "term_and_survival")
        self.assertIn("five years", term_clause["requirement"])
        self.assertNotIn("seven years", term_clause["requirement"])

    def test_publishing_draft_changes_ai_packet(self):
        baseline_packet = self._active_packet()

        draft_playbook = deepcopy(self.original_playbook)
        _clause(draft_playbook, "term_and_survival")["max_term_years"] = 7
        draft_id = self._save_draft(draft_playbook)
        self._publish_draft(draft_id)

        packet_after_publish = self._active_packet()
        self.assertNotEqual(packet_after_publish["playbook"], baseline_packet["playbook"])
        term_clause = _packet_clause(packet_after_publish, "term_and_survival")
        self.assertIn("seven years", term_clause["requirement"])

    def test_published_survival_jurisdiction_and_fallback_wording_appear_in_packet(self):
        draft_playbook = deepcopy(self.original_playbook)
        _clause(draft_playbook, "term_and_survival")["max_term_years"] = 3
        # Change the preferred (default) jurisdiction within the approved set.
        _set_preferred_law(draft_playbook, "India")

        draft_id = self._save_draft(draft_playbook)
        self._publish_draft(draft_id)

        packet = self._active_packet()

        # Survival term threshold flows into the term clause wording.
        term_clause = _packet_clause(packet, "term_and_survival")
        self.assertIn("three years", term_clause["requirement"])

        # Jurisdiction change flows into the approved options, the default, and the
        # fallback/redline drafting note.
        gl_clause = _packet_clause(packet, "governing_law")
        default_options = [option["value"] for option in gl_clause["rules"]["approved_options"] if option.get("default")]
        self.assertEqual(default_options, ["India"])
        drafting_note = gl_clause["rules"]["redline_guidance"]["drafting_note"]
        self.assertIn("India", drafting_note)

    # --- review result records active version/hash ------------------------

    def test_review_result_records_active_playbook_version_and_hash(self):
        runtime = self._active_runtime()
        stub_review = {
            "review_engine_version": REVIEW_ENGINE_VERSION,
            "clauses": [],
            "review_state": {},
            "ai_first_review": {"status": "completed"},
        }

        result = self._review_with_active_runtime(
            "Mutual NDA text.",
            lambda text, paragraphs=None: deepcopy(stub_review),
        )

        recorded = result["playbook_runtime"]
        self.assertEqual(recorded["active_version_id"], runtime["active_version_id"])
        self.assertEqual(recorded["active_hash"], runtime["active_hash"])
        self.assertEqual(recorded["playbook_name"], runtime["playbook_name"])
        self.assertEqual(recorded["playbook_version"], runtime["playbook_version"])
        self.assertEqual(recorded["source"], "active")

        # After a real publish, a new review records the NEW active version + hash.
        draft_playbook = deepcopy(self.original_playbook)
        _clause(draft_playbook, "term_and_survival")["max_term_years"] = 6
        draft_id = self._save_draft(draft_playbook)
        published = self._publish_draft(draft_id)
        new_runtime = self._active_runtime()

        result_after_publish = self._review_with_active_runtime(
            "Mutual NDA text.",
            lambda text, paragraphs=None: deepcopy(stub_review),
        )
        recorded_after = result_after_publish["playbook_runtime"]
        self.assertEqual(recorded_after["active_hash"], new_runtime["active_hash"])
        self.assertEqual(recorded_after["active_version_id"], new_runtime["active_version_id"])
        self.assertEqual(recorded_after["active_hash"], published["active"]["metadata"]["active_hash"])
        self.assertNotEqual(recorded_after["active_hash"], recorded["active_hash"])

    # --- publish makes an old review stale; refresh reruns with new hash ---

    def test_publish_makes_existing_review_stale_then_refresh_clears_it(self):
        runtime_before = self._active_runtime()

        def review_against_active(text, paragraphs=None):
            return {
                "review_engine_version": REVIEW_ENGINE_VERSION,
                "clauses": [
                    {"id": "mutuality", "decision": "pass", "structure_context": {}, "review_state": {}}
                ],
                "review_state": {},
                "ai_first_review": {"status": "completed"},
            }

        original_review = self._review_with_active_runtime("Mutual NDA text.", review_against_active)

        # Fresh against the playbook it ran on.
        fresh = review_result_staleness(
            original_review,
            current_runtime_func=self._active_runtime,
        )
        self.assertFalse(fresh["stale"], fresh["stale_reasons"])
        self.assertEqual(
            original_review["playbook_runtime"]["active_hash"],
            runtime_before["active_hash"],
        )

        # Publish a different playbook: the active hash changes.
        draft_playbook = deepcopy(self.original_playbook)
        _clause(draft_playbook, "term_and_survival")["max_term_years"] = 9
        draft_id = self._save_draft(draft_playbook)
        published = self._publish_draft(draft_id)
        new_active_hash = published["active"]["metadata"]["active_hash"]
        self.assertNotEqual(new_active_hash, runtime_before["active_hash"])

        # The old review is now stale because its stored hash != active hash.
        stale = review_result_staleness(
            original_review,
            current_runtime_func=self._active_runtime,
        )
        self.assertTrue(stale["stale"])
        self.assertIn("playbook_changed", stale["stale_reasons"])

        # Refresh = rerun against the newly published playbook.
        refreshed_review = self._review_with_active_runtime("Mutual NDA text.", review_against_active)
        self.assertEqual(refreshed_review["playbook_runtime"]["active_hash"], new_active_hash)
        cleared = review_result_staleness(
            refreshed_review,
            current_runtime_func=self._active_runtime,
        )
        self.assertFalse(cleared["stale"], cleared["stale_reasons"])

    def test_existing_matter_goes_stale_after_publish_and_refresh_reruns_with_new_hash(self):
        # Full real cycle on a persisted matter: create -> review -> publish ->
        # stale -> refresh_stale_matter_review -> cleared. Only the LLM call and
        # the active-playbook path resolution are redirected to the temp playbook;
        # the publish, matter store, staleness logic, and refresh are all real.
        def stub_ai_first(text, paragraphs=None):
            return {
                "review_engine_version": REVIEW_ENGINE_VERSION,
                "clauses": [
                    {"id": "mutuality", "decision": "pass", "structure_context": {}, "review_state": {}}
                ],
                "review_state": {},
                "ai_first_review": {"status": "completed"},
            }

        def review_against_temp_active(text, paragraphs=None):
            return review_engine.review_nda_with_active_engine(
                text,
                ai_first_review_func=stub_ai_first,
                playbook_runtime_func=self._active_runtime,
            )

        def is_stale_against_temp_active(review_result):
            return bool(
                review_result_staleness(review_result, current_runtime_func=self._active_runtime)["stale"]
            )

        with tempfile.TemporaryDirectory() as data_dir:
            data_path = Path(data_dir)
            store_patches = [
                patch.object(matter_store, "DATA_DIR", data_path),
                patch.object(matter_store, "MATTERS_PATH", data_path / "matters.json"),
                patch.object(matter_store, "UPLOADS_DIR", data_path / "uploads"),
                # Redirect the matters module's active-playbook reads to the temp runtime.
                patch.object(matter_routes, "review_nda_with_active_engine", review_against_temp_active),
                patch.object(matter_routes, "review_result_is_stale", is_stale_against_temp_active),
            ]
            with store_patches[0], store_patches[1], store_patches[2], store_patches[3], store_patches[4]:
                initial_review = review_against_temp_active("Mutual NDA text.")
                runtime_before_hash = initial_review["playbook_runtime"]["active_hash"]
                matter = matter_store.create_matter(
                    source_filename="Acme NDA.docx",
                    document_bytes=b"source docx bytes",
                    extracted_text="Mutual NDA text.",
                    review_result=initial_review,
                    triage={
                        "triage_status": "ready_to_sign",
                        "next_action": "Ready for signature",
                        "issue_count": 0,
                        "requirements_passed": 1,
                        "requirements_needs_review": 0,
                        "requirements_failed": 0,
                    },
                )
                # Fresh immediately after creation.
                self.assertFalse(is_stale_against_temp_active(matter["review_result"]))

                # Real publish flips the active hash.
                draft_playbook = deepcopy(self.original_playbook)
                _clause(draft_playbook, "term_and_survival")["max_term_years"] = 4
                draft_id = self._save_draft(draft_playbook)
                published = self._publish_draft(draft_id)
                new_active_hash = published["active"]["metadata"]["active_hash"]
                self.assertNotEqual(new_active_hash, runtime_before_hash)

                # The persisted matter is now stale.
                stored_matter = matter_store.get_matter(matter["id"])
                self.assertTrue(is_stale_against_temp_active(stored_matter["review_result"]))

                # Real refresh reruns against the newly published playbook and clears stale.
                refreshed_matter = matter_routes.refresh_stale_matter_review(stored_matter)

        self.assertEqual(refreshed_matter["review_result"]["playbook_runtime"]["active_hash"], new_active_hash)
        self.assertFalse(is_stale_against_temp_active(refreshed_matter["review_result"]))


if __name__ == "__main__":
    unittest.main()
