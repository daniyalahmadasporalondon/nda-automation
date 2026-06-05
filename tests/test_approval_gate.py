"""Tests for the post-review human approval gate + reviewer-decision state.

Two layers:
* pure ``approval`` logic (decision validation, resolution summary, block
  reasons, reviewed-DOCX payload translation) — no HTTP, no DOCX;
* the HTTP endpoints (decision, approve with both block reasons + success +
  timeline, reviewed-docx status gate + payload wiring) driven through the real
  request handler against an isolated on-disk store.
"""
from __future__ import annotations

import http.client
import json
import threading
import unittest
from copy import deepcopy
from http.server import ThreadingHTTPServer
from io import BytesIO
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from nda_automation import approval, matter_store
from nda_automation import redline_export_service
from nda_automation import server as server_module
from nda_automation.redline_export_service import RedlineExport
from nda_automation.review_engine import review_nda_with_active_engine
from nda_automation.server import NdaAutomationHandler
from nda_automation.triage import triage_review_result

NDA_PARAGRAPHS = [
    "This Mutual Non-Disclosure Agreement is entered into by both parties.",
    "Each party agrees to keep the other party's Confidential Information secret.",
    "This Agreement shall be governed by the laws of the State of Mars.",
    "The Receiving Party shall not solicit any employees of the Disclosing Party.",
]


def _docx(paragraphs):
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document_xml)
    return buffer.getvalue()


def _review_result_with_runtime() -> dict:
    text = "\n\n".join(NDA_PARAGRAPHS)
    return review_nda_with_active_engine(text)


# --------------------------------------------------------------------------- #
# Pure approval logic
# --------------------------------------------------------------------------- #
class ApprovalLogicTests(unittest.TestCase):
    def _flagged_clause_ids(self, review_result):
        return [
            str(clause.get("id"))
            for clause in review_result.get("clauses", [])
            if clause.get("decision") in ("fail", "review")
        ]

    def test_normalize_reviewer_decision_records_actor_and_timestamp(self):
        decision = approval.normalize_reviewer_decision(
            {"action": "accept"}, actor="alice@example.com"
        )
        self.assertEqual(decision["action"], "accept")
        self.assertEqual(decision["actor"], "alice@example.com")
        self.assertTrue(decision["decided_at"])
        self.assertNotIn("modified_text", decision)

    def test_normalize_reviewer_decision_rejects_unknown_action(self):
        with self.assertRaises(approval.ReviewerDecisionError):
            approval.normalize_reviewer_decision({"action": "approve"}, actor="a")

    def test_modify_requires_modified_text(self):
        with self.assertRaises(approval.ReviewerDecisionError):
            approval.normalize_reviewer_decision({"action": "modify"}, actor="a")
        decision = approval.normalize_reviewer_decision(
            {"action": "modify", "modified_text": "New clause text."}, actor="a"
        )
        self.assertEqual(decision["modified_text"], "New clause text.")

    def test_comment_requires_comment_text(self):
        with self.assertRaises(approval.ReviewerDecisionError):
            approval.normalize_reviewer_decision({"action": "comment"}, actor="a")
        decision = approval.normalize_reviewer_decision(
            {"action": "comment", "comment": "Needs counsel review."}, actor="a"
        )
        self.assertEqual(decision["comment"], "Needs counsel review.")

    def test_resolution_summary_tracks_unresolved_flagged_clauses(self):
        review_result = _review_result_with_runtime()
        flagged = self._flagged_clause_ids(review_result)
        self.assertTrue(flagged, "fixture should flag at least one clause")
        matter = {"id": "m1", "review_result": review_result, "reviewer_decisions": {}}

        summary = approval.resolution_summary(matter)
        self.assertEqual(summary["total"], len(flagged))
        self.assertEqual(summary["resolved"], 0)
        self.assertEqual(set(summary["unresolved"]), set(flagged))

        matter["reviewer_decisions"][flagged[0]] = approval.normalize_reviewer_decision(
            {"action": "accept"}, actor="a"
        )
        summary = approval.resolution_summary(matter)
        self.assertEqual(summary["resolved"], 1)
        self.assertNotIn(flagged[0], summary["unresolved"])

    def test_approval_blocks_flags_stale_playbook(self):
        review_result = _review_result_with_runtime()
        # Resolve every flagged clause so the only remaining block is staleness.
        decisions = {
            clause_id: approval.normalize_reviewer_decision({"action": "accept"}, actor="a")
            for clause_id in self._flagged_clause_ids(review_result)
        }
        stale_result = deepcopy(review_result)
        stale_result["playbook_runtime"]["active_hash"] = "sha256:stale-hash-does-not-match"
        matter = {"id": "m1", "review_result": stale_result, "reviewer_decisions": decisions}

        blocks = approval.approval_blocks(matter)
        self.assertIn(approval.BLOCK_STALE_PLAYBOOK, blocks)
        self.assertFalse([b for b in blocks if b.startswith(approval.UNRESOLVED_CLAUSE_PREFIX)])

    def test_approval_blocks_lists_unresolved_clauses(self):
        review_result = _review_result_with_runtime()
        flagged = self._flagged_clause_ids(review_result)
        matter = {"id": "m1", "review_result": review_result, "reviewer_decisions": {}}

        blocks = approval.approval_blocks(matter)
        self.assertNotIn(approval.BLOCK_STALE_PLAYBOOK, blocks)
        self.assertEqual(
            sorted(blocks),
            sorted(f"{approval.UNRESOLVED_CLAUSE_PREFIX}{cid}" for cid in flagged),
        )

    def test_review_is_stale_honors_playbook_version_hash_contract(self):
        # Fresh by review_staleness (runtime hash matches) but the locked
        # provenance field playbook_version.hash has drifted -> stale.
        review_result = _review_result_with_runtime()
        review_result["playbook_version"] = {
            "id": "pbv_x",
            "hash": "sha256:drifted-provenance-hash",
            "label": "NDA Playbook v1",
        }
        self.assertFalse(
            approval.review_result_staleness(review_result)["stale"],
            "runtime hash should still match the active playbook",
        )
        self.assertTrue(
            approval.review_is_stale(
                review_result,
                current_playbook_hash_func=lambda: "sha256:published-hash",
            )
        )
        # A matching provenance hash is not stale via the contract field.
        review_result["playbook_version"]["hash"] = "sha256:published-hash"
        self.assertFalse(
            approval.review_is_stale(
                review_result,
                current_playbook_hash_func=lambda: "sha256:published-hash",
            )
        )

    def test_no_blocks_when_fresh_and_fully_resolved(self):
        review_result = _review_result_with_runtime()
        decisions = {
            clause_id: approval.normalize_reviewer_decision({"action": "accept"}, actor="a")
            for clause_id in self._flagged_clause_ids(review_result)
        }
        matter = {"id": "m1", "review_result": review_result, "reviewer_decisions": decisions}
        self.assertEqual(approval.approval_blocks(matter), [])

    def test_reviewed_docx_payload_applies_accept_skips_reject(self):
        review_result = {
            "clauses": [
                {"id": "governing_law", "decision": "fail"},
                {"id": "non_solicit", "decision": "review"},
            ],
            "redline_edits": [
                {"id": "r1", "clause_id": "governing_law", "paragraph_id": "p3", "action": "replace_paragraph"},
                {"id": "r2", "clause_id": "non_solicit", "paragraph_id": "p4", "action": "delete_paragraph"},
            ],
        }
        matter = {
            "id": "m1",
            "review_result": review_result,
            "reviewer_decisions": {
                "governing_law": {"action": "accept", "actor": "a", "decided_at": "t"},
                "non_solicit": {"action": "reject", "actor": "a", "decided_at": "t"},
            },
        }
        payload = approval.reviewed_docx_payload(matter)
        included_ids = {edit["id"] for edit in payload["export_redline_edits"]}
        self.assertEqual(included_ids, {"r1"})
        self.assertEqual(payload["manual_redline_edits"], [])

    def test_reviewed_docx_payload_modify_overrides_text_and_comment_attaches(self):
        review_result = {
            "clauses": [{"id": "governing_law", "decision": "fail"}],
            "redline_edits": [
                {
                    "id": "r1",
                    "clause_id": "governing_law",
                    "paragraph_id": "p3",
                    "action": "replace_paragraph",
                    "original_text": "Governed by Mars law.",
                },
            ],
        }
        matter = {
            "id": "m1",
            "review_result": review_result,
            "reviewer_decisions": {
                "governing_law": {
                    "action": "modify",
                    "modified_text": "Governed by Delaware law.",
                    "comment": "Switched to Delaware.",
                    "actor": "alice",
                    "decided_at": "t",
                },
            },
        }
        payload = approval.reviewed_docx_payload(matter)
        self.assertEqual(len(payload["export_redline_edits"]), 1)
        self.assertEqual(len(payload["manual_redline_edits"]), 1)
        manual = payload["manual_redline_edits"][0]
        self.assertEqual(manual["paragraph_id"], "p3")
        self.assertEqual(manual["replacement_text"], "Governed by Delaware law.")
        self.assertEqual(len(payload["review_comments"]), 1)
        self.assertEqual(payload["review_comments"][0]["clause_id"], "governing_law")


# --------------------------------------------------------------------------- #
# HTTP endpoints
# --------------------------------------------------------------------------- #
class ApprovalEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), NdaAutomationHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.host, cls.port = cls.server.server_address

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        server_module._reset_rate_limits()

    def request(self, method, path, body=None):
        headers = {}
        request_body = body
        if isinstance(body, dict):
            request_body = json.dumps(body).encode("utf-8")
            headers = {"Content-Type": "application/json"}
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            connection.request(method, path, body=request_body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            content_type = response.getheader("Content-Type", "")
            payload = json.loads(raw.decode("utf-8")) if "application/json" in content_type else raw
            return response.status, payload, dict(response.getheaders())
        finally:
            connection.close()

    def _seed_matter(self, *, resolve_all=False, source_docx=True):
        review_result = _review_result_with_runtime()
        triage = triage_review_result(review_result)
        document_bytes = _docx(NDA_PARAGRAPHS) if source_docx else b"not-a-docx"
        matter = matter_store.create_matter(
            source_filename="mutual-nda.docx",
            document_bytes=document_bytes,
            extracted_text="\n\n".join(NDA_PARAGRAPHS),
            review_result=review_result,
            triage=triage,
            source_type="manual_upload",
            board_column="in_review",
        )
        matter_store.update_matter_fields(matter["id"], {"status": "in_review"})
        flagged = [
            str(clause.get("id"))
            for clause in review_result.get("clauses", [])
            if clause.get("decision") in ("fail", "review")
        ]
        if resolve_all:
            for clause_id in flagged:
                matter_store.set_clause_reviewer_decision(
                    matter["id"],
                    clause_id,
                    approval.normalize_reviewer_decision({"action": "accept"}, actor="reviewer"),
                )
        return matter["id"], flagged

    # --- decision endpoint -------------------------------------------------
    def test_decision_endpoint_persists_and_returns_resolution(self):
        matter_id, flagged = self._seed_matter()
        clause_id = flagged[0]
        status, payload, _ = self.request(
            "POST",
            f"/api/matters/{matter_id}/clauses/{clause_id}/decision",
            {"action": "modify", "modified_text": "Tighter clause.", "comment": "ok"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["clause"]["id"], clause_id)
        self.assertEqual(payload["clause"]["reviewer_decision"]["action"], "modify")
        self.assertEqual(payload["clause"]["reviewer_decision"]["modified_text"], "Tighter clause.")
        self.assertEqual(payload["resolution"]["total"], len(flagged))
        self.assertEqual(payload["resolution"]["resolved"], 1)
        self.assertNotIn(clause_id, payload["resolution"]["unresolved"])

        stored = matter_store.get_matter(matter_id)
        self.assertEqual(stored["reviewer_decisions"][clause_id]["action"], "modify")
        # The review result payload itself is never mutated.
        for clause in stored["review_result"]["clauses"]:
            self.assertNotIn("reviewer_decision", clause)

    def test_decision_endpoint_rejects_bad_action(self):
        matter_id, flagged = self._seed_matter()
        status, payload, _ = self.request(
            "POST",
            f"/api/matters/{matter_id}/clauses/{flagged[0]}/decision",
            {"action": "approve"},
        )
        self.assertEqual(status, 400)
        self.assertIn("action", payload["error"])

    def test_decision_endpoint_unknown_clause_is_404(self):
        matter_id, _ = self._seed_matter()
        status, payload, _ = self.request(
            "POST",
            f"/api/matters/{matter_id}/clauses/not_a_real_clause/decision",
            {"action": "accept"},
        )
        self.assertEqual(status, 404)

    # --- approve: block reasons -------------------------------------------
    def test_approve_blocked_by_unresolved_clause(self):
        matter_id, flagged = self._seed_matter(resolve_all=False)
        status, payload, _ = self.request("POST", f"/api/matters/{matter_id}/approve")
        self.assertEqual(status, 409)
        self.assertEqual(
            sorted(payload["blocks_approval"]),
            sorted(f"unresolved_clause:{cid}" for cid in flagged),
        )
        self.assertEqual(matter_store.get_matter(matter_id)["status"], "in_review")

    def test_approve_blocked_by_stale_playbook(self):
        matter_id, flagged = self._seed_matter(resolve_all=True)
        # Make the stored review stale by rewriting its playbook hash.
        matter = matter_store.get_matter(matter_id)
        review_result = deepcopy(matter["review_result"])
        review_result["playbook_runtime"]["active_hash"] = "sha256:stale"
        matter_store.update_matter_review(
            matter_id, review_result, triage_review_result(review_result)
        )
        # update_matter_review clears prior sign-off but not reviewer_decisions;
        # re-resolve to isolate the stale block reason.
        for clause_id in flagged:
            matter_store.set_clause_reviewer_decision(
                matter_id,
                clause_id,
                approval.normalize_reviewer_decision({"action": "accept"}, actor="reviewer"),
            )
        status, payload, _ = self.request("POST", f"/api/matters/{matter_id}/approve")
        self.assertEqual(status, 409)
        self.assertIn("stale_playbook", payload["blocks_approval"])
        self.assertEqual(matter_store.get_matter(matter_id)["status"], "in_review")

    # --- approve: success + timeline --------------------------------------
    def test_approve_success_sets_status_and_timeline(self):
        matter_id, _ = self._seed_matter(resolve_all=True)
        status, payload, _ = self.request("POST", f"/api/matters/{matter_id}/approve")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "approved")
        self.assertTrue(payload["approved_at"])
        self.assertEqual(payload["timeline_event"]["type"], "matter_approved")

        stored = matter_store.get_matter(matter_id)
        self.assertEqual(stored["status"], "approved")
        self.assertTrue(stored["approved_at"])
        self.assertEqual(len(stored["matter_timeline"]), 1)
        self.assertEqual(stored["matter_timeline"][0]["type"], "matter_approved")

    # --- reviewed-docx -----------------------------------------------------
    def test_reviewed_docx_requires_approved_status(self):
        matter_id, _ = self._seed_matter(resolve_all=True)
        status, payload, _ = self.request("GET", f"/api/matters/{matter_id}/reviewed-docx")
        self.assertEqual(status, 409)
        self.assertIn("approved", payload["error"])

    def test_reviewed_docx_builds_from_decisions_when_approved(self):
        matter_id, flagged = self._seed_matter(resolve_all=True)
        # Approve first.
        approve_status, _, _ = self.request("POST", f"/api/matters/{matter_id}/approve")
        self.assertEqual(approve_status, 200)

        captured = {}

        def fake_build(mid, payload=None, *, persist=False, repository=None, owner_user_id=""):
            captured["matter_id"] = mid
            captured["payload"] = payload
            return RedlineExport(data=b"PK\x03\x04reviewed-docx", filename="mutual-nda-redlined.docx")

        with patch.object(redline_export_service, "build_matter_redline", side_effect=fake_build):
            status, body, headers = self.request("GET", f"/api/matters/{matter_id}/reviewed-docx")

        self.assertEqual(status, 200)
        self.assertEqual(body, b"PK\x03\x04reviewed-docx")
        self.assertEqual(headers.get("Content-Type"), server_module.DOCX_MIME)
        self.assertEqual(captured["matter_id"], matter_id)
        # All flagged clauses were accepted, so every server redline is included
        # and there are no manual overrides.
        self.assertIn("export_redline_edits", captured["payload"])
        self.assertEqual(captured["payload"]["manual_redline_edits"], [])

    def test_reviewed_docx_missing_matter_is_404(self):
        status, payload, _ = self.request("GET", "/api/matters/matter_missing/reviewed-docx")
        self.assertEqual(status, 404)


class _FakeHandler:
    """Minimal stand-in for NdaAutomationHandler to drive route handlers with a
    chosen authenticated identity and capture their responses."""

    def __init__(self, *, current_user_id: str, body: dict | None = None):
        self.current_user_id = current_user_id
        self.current_user = {"id": current_user_id, "email": current_user_id} if current_user_id else None
        self._body = body
        self.status = None
        self.json = None
        self.download = None

    def _read_json_payload(self):
        return self._body

    def _send_json(self, payload, status=200, headers=None, *, send_body=True):
        self.status = status
        self.json = payload

    def _send_download(self, data, filename, content_type, headers=None, *, send_body=True):
        self.status = 200
        self.download = {"data": data, "filename": filename, "content_type": content_type}


class ApprovalEndpointOwnershipTests(unittest.TestCase):
    """The cross-tenant leak (task #6) must not survive on the new endpoints.

    An ownerless matter must be denied to an authenticated user across the
    decision, approve, and reviewed-docx endpoints.
    """

    def _seed_ownerless_matter(self):
        review_result = _review_result_with_runtime()
        matter = matter_store.create_matter(
            source_filename="ownerless-nda.docx",
            document_bytes=_docx(NDA_PARAGRAPHS),
            extracted_text="\n\n".join(NDA_PARAGRAPHS),
            review_result=review_result,
            triage=triage_review_result(review_result),
            source_type="manual_upload",
            board_column="in_review",
        )  # no owner_user_id -> ownerless
        clause_id = next(
            str(clause.get("id"))
            for clause in review_result["clauses"]
            if clause.get("decision") in ("fail", "review")
        )
        return matter["id"], clause_id

    def test_decision_endpoint_denies_ownerless_matter_to_other_user(self):
        from nda_automation.routes import approval as approval_routes

        matter_id, clause_id = self._seed_ownerless_matter()
        handler = _FakeHandler(current_user_id="attacker@example.com", body={"action": "accept"})
        approval_routes.handle_clause_decision(
            handler, f"/api/matters/{matter_id}/clauses/{clause_id}/decision"
        )
        self.assertEqual(handler.status, 404)
        # No decision was written.
        self.assertEqual(matter_store.get_matter(matter_id).get("reviewer_decisions", {}), {})

    def test_approve_endpoint_denies_ownerless_matter_to_other_user(self):
        from nda_automation.routes import approval as approval_routes

        matter_id, _ = self._seed_ownerless_matter()
        handler = _FakeHandler(current_user_id="attacker@example.com")
        approval_routes.handle_matter_approve(handler, f"/api/matters/{matter_id}/approve")
        self.assertEqual(handler.status, 404)
        self.assertEqual(matter_store.get_matter(matter_id).get("status"), "active")

    def test_reviewed_docx_endpoint_denies_ownerless_matter_to_other_user(self):
        from nda_automation.routes import approval as approval_routes

        matter_id, _ = self._seed_ownerless_matter()
        handler = _FakeHandler(current_user_id="attacker@example.com")
        approval_routes.handle_matter_reviewed_docx(handler, f"/api/matters/{matter_id}/reviewed-docx")
        self.assertEqual(handler.status, 404)
        self.assertIsNone(handler.download)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
