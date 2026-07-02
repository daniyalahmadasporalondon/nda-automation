"""Tests for the post-review human approval gate + reviewer-decision state.

Two layers:
* pure ``approval`` logic (decision validation, resolution summary, block
  reasons, reviewed-DOCX payload translation) — no HTTP, no DOCX;
* the HTTP endpoints (decision, approve — staleness is the sole block reason now
  + success + timeline, reviewed-docx status gate + payload wiring) driven
  through the real request handler against an isolated on-disk store.
"""
from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from copy import deepcopy
from http.server import ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from nda_automation import approval, matter_store
from nda_automation import pdf_docx_reconstruction, pdf_export_service
from nda_automation import redline_export_service
from nda_automation.matter_repository import InMemoryMatterRepository
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
        stale_result = deepcopy(review_result)
        stale_result["playbook_runtime"]["active_hash"] = "sha256:stale-hash-does-not-match"
        # Staleness blocks regardless of any per-clause decisions: the single
        # "Approve Review" gate signs off the whole matter once the review is
        # fresh, so the *only* block reason that can appear is stale_playbook.
        matter = {"id": "m1", "review_result": stale_result, "reviewer_decisions": {}}

        blocks = approval.approval_blocks(matter)
        self.assertEqual(blocks, [approval.BLOCK_STALE_PLAYBOOK])

    def test_fresh_review_with_unresolved_clauses_is_not_blocked(self):
        # One approval covers the whole matter: unresolved fail/review clauses no
        # longer gate approval. A fresh review is approvable with no decisions.
        review_result = _review_result_with_runtime()
        self.assertTrue(
            self._flagged_clause_ids(review_result),
            "fixture should flag at least one clause that would have required a decision",
        )
        matter = {"id": "m1", "review_result": review_result, "reviewer_decisions": {}}
        self.assertEqual(approval.approval_blocks(matter), [])

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
                    "paragraph_index": 3,
                    "source_index": 3,
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
        # The modify redline must carry the source redline's block indexes so the
        # export content-coverage gate can locate the block it replaces; without them
        # the gate keeps the original text as expected and rejects the legitimate edit.
        self.assertEqual(manual["paragraph_index"], 3)
        self.assertEqual(manual["source_index"], 3)
        self.assertEqual(len(payload["review_comments"]), 1)
        self.assertEqual(payload["review_comments"][0]["clause_id"], "governing_law")


# --------------------------------------------------------------------------- #
# Modify decision -> reviewed-DOCX / send-redline export through the real
# content-coverage gate. The modify edit must pass the gate (no HTTP 500) and the
# exported document must reflect the modified text, while a genuinely dropped
# redline is still caught by the same gate.
# --------------------------------------------------------------------------- #
class ModifyExportCoverageTests(unittest.TestCase):
    """End-to-end: a reviewer "modify" decision exports a reviewed DOCX whose
    coverage gate accepts the intentional edit, without weakening drop-detection."""

    SOURCE_PARAGRAPHS = [
        "Intro paragraph that stays unchanged.",
        "This Agreement shall be governed by the laws of California.",
        "The confidentiality obligations survive for three years.",
    ]
    # Deliberately a value no AI/playbook redline would propose, so the assertions
    # below isolate the reviewer's own edit from any engine-proposed replacement.
    MODIFIED_TEXT = "This Agreement is governed exclusively by the laws of Singapore."

    def _matter_and_source(self):
        from tests.test_docx_export import make_source_docx
        from nda_automation.checker import review_nda
        from nda_automation.docx_text import extract_docx_paragraphs

        source_docx = make_source_docx(list(self.SOURCE_PARAGRAPHS))
        extracted = extract_docx_paragraphs(source_docx)
        source_text = "\n\n".join(str(paragraph["text"]) for paragraph in extracted)
        review_result = review_nda(source_text, paragraphs=extracted)
        review_result["extracted_text"] = source_text
        governing_law_redline = next(
            redline
            for redline in review_result["redline_edits"]
            if redline.get("clause_id") == "governing_law"
        )
        matter = {
            "id": "m-modify",
            "review_result": review_result,
            "reviewer_decisions": {
                "governing_law": {
                    "action": "modify",
                    "modified_text": self.MODIFIED_TEXT,
                    "actor": "reviewer",
                    "decided_at": "t",
                },
            },
        }
        return matter, source_docx, review_result, governing_law_redline

    def _render_with_payload(self, source_docx, review_result, payload):
        from nda_automation import docx_package_renderer, export_service

        rendered_result = deepcopy(review_result)
        export_service.apply_selected_export_redlines(
            rendered_result, payload.get("export_redline_edits")
        )
        export_service.apply_manual_export_redlines(
            rendered_result, payload.get("manual_redline_edits")
        )
        export_service.apply_review_comments(rendered_result, payload.get("review_comments"))
        return docx_package_renderer.render_source_redline_package(
            source_docx,
            rendered_result,
            expected_source_text=str(review_result.get("extracted_text") or ""),
            expected_redline_edits=rendered_result.get("redline_edits", []),
        )

    def test_modify_decision_export_passes_coverage_gate_and_reflects_edit(self):
        from nda_automation import docx_health

        matter, source_docx, review_result, _ = self._matter_and_source()
        payload = approval.reviewed_docx_payload(matter)

        result = self._render_with_payload(source_docx, review_result, payload)

        self.assertEqual(result.health_errors, [])
        self.assertEqual(result.content_errors, [])
        # The reviewer's modified text is applied as a tracked change. The accepted
        # view (changes accepted -> what the recipient ends up with) must reconstruct
        # the reviewer's exact replacement paragraph.
        accepted = [
            record["accepted"]
            for record in docx_health._export_revision_paragraphs(result.data)
        ]
        self.assertIn(self.MODIFIED_TEXT, accepted)
        # The original wording is gone from the accepted view (it survives only as a
        # tracked deletion), so the edit truly replaced the source clause.
        self.assertNotIn(
            "This Agreement shall be governed by the laws of California.", accepted
        )

    def test_modify_decision_send_redline_export_succeeds(self):
        # send-redline and reviewed-DOCX share build_matter_redline; exercise it
        # through the persisted-matter path used by the send endpoint to prove the
        # export does not 500 after a modify.
        matter, source_docx, review_result, _ = self._matter_and_source()
        repository = InMemoryMatterRepository()
        stored = repository.create_matter(
            source_filename="Mutual NDA.docx",
            document_bytes=source_docx,
            extracted_text=str(review_result.get("extracted_text") or ""),
            review_result=review_result,
            triage={"triage_status": "ready_to_sign"},
        )
        stored["reviewer_decisions"] = matter["reviewer_decisions"]
        payload = approval.reviewed_docx_payload(stored)
        with patch.object(
            redline_export_service, "review_result_staleness", return_value={"stale": False}
        ):
            export = redline_export_service.build_matter_redline(
                stored["id"],
                payload,
                repository=repository,
            )
        self.assertTrue(export.data)
        self.assertTrue(export.filename.endswith(".docx"))

    def test_dropped_redline_is_still_caught_by_coverage_gate(self):
        # Protection guard (the fix must NOT weaken drop-detection): if the reviewer's
        # modify replacement is SILENTLY DROPPED on export (the prior P0 defect), the
        # coverage gate must still flag it. Render the UNMODIFIED source (no redline
        # applied at all -- a true drop) while telling the gate to EXPECT the modify.
        from nda_automation import docx_package_renderer

        matter, source_docx, review_result, _ = self._matter_and_source()
        payload = approval.reviewed_docx_payload(matter)
        manual = payload["manual_redline_edits"][0]
        expected_with_modify = [
            {
                "action": manual["action"],
                "paragraph_id": manual["paragraph_id"],
                "paragraph_index": manual["paragraph_index"],
                "source_index": manual["source_index"],
                "replacement_text": manual["replacement_text"],
            }
        ]

        # rendered_result carries NO redline edits: the export equals the plain source,
        # so the reviewer's intended modify never reaches the document.
        rendered_result = deepcopy(review_result)
        rendered_result["redline_edits"] = []
        result = docx_package_renderer.render_source_redline_package(
            source_docx,
            rendered_result,
            expected_source_text=str(review_result.get("extracted_text") or ""),
            expected_redline_edits=expected_with_modify,
        )
        self.assertEqual(result.health_errors, [])
        self.assertTrue(
            result.content_errors,
            "A dropped modify redline must still be caught by the coverage gate.",
        )


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

    def _seed_matter(self, *, resolve_all=False, source_docx=True, with_redline=False):
        review_result = _review_result_with_runtime()
        if with_redline:
            # The stub AI engine emits zero redlines, so a seeded PDF matter has nothing
            # to apply -- which now (correctly) serves the original PDF and never runs the
            # reconstruction. To exercise the reconstruction path (and its
            # engine-unavailable error) we inject one redline keyed to a flagged clause so
            # the accepted decision yields a non-empty export_redline_edits.
            flagged_for_redline = [
                str(clause.get("id"))
                for clause in review_result.get("clauses", [])
                if clause.get("decision") in ("fail", "review")
            ]
            target_clause = flagged_for_redline[0]
            review_result["redline_edits"] = [
                {
                    "id": "seeded-redline",
                    "clause_id": target_clause,
                    "paragraph_id": "p1",
                    "action": "replace_paragraph",
                    "original_text": NDA_PARAGRAPHS[2],
                    "replacement_text": NDA_PARAGRAPHS[2] + " (amended)",
                }
            ]
        triage = triage_review_result(review_result)
        document_bytes = _docx(NDA_PARAGRAPHS) if source_docx else b"%PDF-1.7\nsource pdf\n%%EOF\n"
        matter = matter_store.create_matter(
            source_filename="mutual-nda.docx" if source_docx else "mutual-nda.pdf",
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
    def test_approve_succeeds_with_unresolved_clauses_when_fresh(self):
        # The single "Approve Review" gate signs off the whole matter; unresolved
        # fail/review clauses no longer block a fresh review.
        matter_id, flagged = self._seed_matter(resolve_all=False)
        self.assertTrue(flagged, "fixture should flag at least one clause")
        status, payload, _ = self.request("POST", f"/api/matters/{matter_id}/approve")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "approved")
        self.assertEqual(matter_store.get_matter(matter_id)["status"], "approved")

    def test_approve_blocked_by_stale_playbook(self):
        # Staleness is the sole remaining approval blocker (a data-freshness
        # guard), independent of any per-clause decisions.
        matter_id, _ = self._seed_matter(resolve_all=False)
        # Make the stored review stale by rewriting its playbook hash.
        matter = matter_store.get_matter(matter_id)
        review_result = deepcopy(matter["review_result"])
        review_result["playbook_runtime"]["active_hash"] = "sha256:stale"
        matter_store.update_matter_review(
            matter_id, review_result, triage_review_result(review_result)
        )
        status, payload, _ = self.request("POST", f"/api/matters/{matter_id}/approve")
        self.assertEqual(status, 409)
        self.assertEqual(payload["blocks_approval"], ["stale_playbook"])
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

    def test_approve_atomicity_build_failure_blocks_approval(self):
        # ATOMICITY (P0): an edited matter whose reviewed-DOCX build fails must NOT be
        # marked approved (cleared-to-send). Otherwise the send path would find no
        # reviewed artifact and could sign the un-redlined original. The pre-flight
        # surfaces the failure LOUD and the matter stays in_review.
        matter_id, _ = self._seed_matter(resolve_all=True, with_redline=True)

        def boom(*args, **kwargs):
            raise RuntimeError("reviewed build blew up")

        with patch.object(redline_export_service, "build_matter_redline", side_effect=boom):
            status, payload, _ = self.request("POST", f"/api/matters/{matter_id}/approve")

        self.assertEqual(status, 500)
        self.assertIn("reviewed document", payload["error"])
        # Fail-closed: the matter was NOT approved.
        self.assertEqual(matter_store.get_matter(matter_id)["status"], "in_review")

    def test_approve_blocks_when_source_text_unverifiable(self):
        # COVERAGE-GATE HOLE (P1): the DOCX content-coverage gate is skipped when the
        # expected source text is empty, so a redline that dropped clauses could
        # register as an UNVERIFIED "reviewed" doc. An edited matter with no
        # extracted_text is refused at approval rather than minting an unverifiable
        # reviewed doc that becomes signable.
        matter_id, _ = self._seed_matter(resolve_all=True, with_redline=True)
        with matter_store._locked_store():
            record = matter_store._load_matter_record_by_id(matter_id)
            record["extracted_text"] = ""
            matter_store._save_matter_record(record)

        status, payload, _ = self.request("POST", f"/api/matters/{matter_id}/approve")

        self.assertEqual(status, 409)
        self.assertIn("content coverage", payload["error"])
        self.assertEqual(matter_store.get_matter(matter_id)["status"], "in_review")

    def test_approve_no_edits_does_not_fabricate_reviewed_artifact(self):
        # DON'T FABRICATE REVIEWED (P1): a matter with NO reviewer edits (nothing to
        # redline) approves without minting a reviewed artifact -- the original is the
        # faithful signable doc, and a fabricated "reviewed" slot could later mask an
        # edit-without-reregister. Approval succeeds; no reviewed artifact is added.
        from nda_automation.artifact_registry import ROLE_REVIEWED, latest_artifact_for_role

        matter_id, _ = self._seed_matter(resolve_all=True)  # no with_redline => no edits
        status, _payload, _ = self.request("POST", f"/api/matters/{matter_id}/approve")
        self.assertEqual(status, 200)
        stored = matter_store.get_matter(matter_id)
        self.assertEqual(stored["status"], "approved")
        self.assertIsNone(latest_artifact_for_role(stored, ROLE_REVIEWED))

    # --- reviewed-docx -----------------------------------------------------
    def test_reviewed_docx_preview_serves_reviewed_but_unapproved_without_registering(self):
        # A reviewed-but-unapproved matter (status "in_review", review_result
        # present) serves the faithful redline pre-approval (200) WITHOUT minting
        # a durable role="reviewed" artifact -- approval is what registers it.
        from nda_automation import artifact_service

        matter_id, _ = self._seed_matter(resolve_all=True)
        self.assertEqual(matter_store.get_matter(matter_id)["status"], "in_review")

        def fake_build(mid, payload=None, *, persist=False, repository=None, owner_user_id=""):
            return RedlineExport(data=b"PK\x03\x04reviewed-docx", filename="mutual-nda-redlined.docx")

        with patch.object(redline_export_service, "build_matter_redline", side_effect=fake_build), \
                patch.object(
                    artifact_service, "register_reviewed_docx", return_value=None
                ) as register_spy:
            status, body, headers = self.request("GET", f"/api/matters/{matter_id}/reviewed-docx")

        self.assertEqual(status, 200)
        self.assertEqual(body, b"PK\x03\x04reviewed-docx")
        self.assertEqual(headers.get("Content-Type"), server_module.DOCX_MIME)
        # Preview path: nothing registered, no reviewed artifact on the matter.
        register_spy.assert_not_called()
        self.assertNotIn("X-Reviewed-Artifact-ID", headers)
        stored = matter_store.get_matter(matter_id)
        self.assertEqual(stored.get("artifacts", []), [])

    def test_reviewed_docx_preview_accepted_mode_serves_unapproved(self):
        from nda_automation.routes import approval as approval_routes

        matter_id, _ = self._seed_matter(resolve_all=True)

        def fake_build(mid, payload=None, *, persist=False, repository=None, owner_user_id=""):
            return RedlineExport(data=b"PK\x03\x04reviewed-docx", filename="mutual-nda-redlined.docx")

        # The accepted path flattens revisions; stub it since the fake export bytes
        # above are not a real DOCX archive.
        with patch.object(redline_export_service, "build_matter_redline", side_effect=fake_build), \
                patch.object(approval_routes, "accept_all_revisions", side_effect=lambda b: b), \
                patch.object(approval_routes, "normalize_docx_emf_wmf_images", side_effect=lambda b: b):
            status, _body, headers = self.request(
                "GET", f"/api/matters/{matter_id}/reviewed-docx?changes=accepted"
            )

        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Reviewed-Changes"), "accepted")

    def test_reviewed_docx_without_completed_review_is_409(self):
        # Strip the review_result -> no completed review -> still gated (don't
        # serve garbage). Status stays "in_review" (not approved).
        matter_id, _ = self._seed_matter(resolve_all=True)
        with matter_store._locked_store():
            record = matter_store._load_matter_record_by_id(matter_id)
            record.pop("review_result", None)
            matter_store._save_matter_record(record)
        status, payload, _ = self.request("GET", f"/api/matters/{matter_id}/reviewed-docx")
        self.assertEqual(status, 409)
        self.assertIn("reviewed", payload["error"])

    def test_reviewed_docx_approved_registers_durable_artifact(self):
        # The approved path STILL persists/registers the reviewed artifact.
        from nda_automation import artifact_service

        matter_id, _ = self._seed_matter(resolve_all=True, with_redline=True)

        def fake_build(mid, payload=None, *, persist=False, repository=None, owner_user_id=""):
            return RedlineExport(data=b"PK\x03\x04reviewed-docx-approved", filename="mutual-nda-redlined.docx")

        # Approval now ATOMICALLY builds+registers the reviewed DOCX for an edited
        # matter (the pre-flight): a real build failure would block approval loud.
        # This edited matter's synthetic redline does not round-trip the content-
        # coverage gate, so we stub the builder around BOTH approve and download to
        # exercise the registration wiring rather than the (separately tested) gate.
        with patch.object(redline_export_service, "build_matter_redline", side_effect=fake_build):
            approve_status, _, _ = self.request("POST", f"/api/matters/{matter_id}/approve")
        self.assertEqual(approve_status, 200)

        with patch.object(redline_export_service, "build_matter_redline", side_effect=fake_build), \
                patch.object(
                    artifact_service, "register_reviewed_docx", return_value=None
                ) as register_spy:
            status, _body, _headers = self.request("GET", f"/api/matters/{matter_id}/reviewed-docx")

        self.assertEqual(status, 200)
        register_spy.assert_called_once()

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

    def test_reviewed_pdf_requires_approved_status(self):
        matter_id, _ = self._seed_matter(resolve_all=True)
        status, payload, _ = self.request("GET", f"/api/matters/{matter_id}/reviewed-pdf")
        self.assertEqual(status, 409)
        self.assertIn("approved", payload["error"])

    def test_reviewed_pdf_builds_from_reviewed_docx_when_approved(self):
        matter_id, _flagged = self._seed_matter(resolve_all=True)
        approve_status, _, _ = self.request("POST", f"/api/matters/{matter_id}/approve")
        self.assertEqual(approve_status, 200)

        reviewed_docx = RedlineExport(data=b"PK\x03\x04reviewed-docx", filename="mutual-nda-redlined.docx")
        tmp_pdf = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        tmp_pdf.write(b"%PDF-1.7\nreviewed\n%%EOF\n")
        tmp_pdf.close()
        pdf_path = Path(tmp_pdf.name)
        pdf_export = pdf_export_service.MatterPdfExport(
            path=pdf_path,
            filename="mutual-nda-redlined.pdf",
            content_type=pdf_export_service.PDF_EXPORT_MIME,
            headers={
                "X-PDF-Export-Verified": pdf_export_service.PDF_EXPORT_VERIFICATION_HEADER,
                "X-PDF-Export-Source-Kind": "docx",
            },
        )
        try:
            with patch.object(redline_export_service, "build_matter_redline", return_value=reviewed_docx):
                with patch.object(pdf_export_service, "build_docx_pdf_export", return_value=pdf_export) as build_pdf:
                    status, body, headers = self.request("GET", f"/api/matters/{matter_id}/reviewed-pdf")
        finally:
            pdf_path.unlink(missing_ok=True)

        self.assertEqual(status, 200)
        self.assertEqual(body, b"%PDF-1.7\nreviewed\n%%EOF\n")
        self.assertEqual(headers.get("Content-Type"), pdf_export_service.PDF_EXPORT_MIME)
        self.assertIn('filename="mutual-nda-redlined.pdf"', headers.get("Content-Disposition", ""))
        self.assertEqual(headers.get("X-PDF-Export-Verified"), pdf_export_service.PDF_EXPORT_VERIFICATION_HEADER)
        self.assertEqual(headers.get("X-PDF-Export-Source-Kind"), "docx")
        build_pdf.assert_called_once_with(
            b"PK\x03\x04reviewed-docx",
            "mutual-nda-redlined.docx",
            owner_user_id="",
        )

    def test_reviewed_pdf_reports_converter_unavailable(self):
        matter_id, _flagged = self._seed_matter(resolve_all=True)
        approve_status, _, _ = self.request("POST", f"/api/matters/{matter_id}/approve")
        self.assertEqual(approve_status, 200)

        reviewed_docx = RedlineExport(data=b"PK\x03\x04reviewed-docx", filename="mutual-nda-redlined.docx")
        unavailable = pdf_export_service.PdfExportError(
            {"error": pdf_export_service.PDF_CONVERTER_UNAVAILABLE_MESSAGE},
            status=503,
        )

        with patch.object(redline_export_service, "build_matter_redline", return_value=reviewed_docx):
            with patch.object(pdf_export_service, "build_docx_pdf_export", side_effect=unavailable):
                status, payload, _ = self.request("GET", f"/api/matters/{matter_id}/reviewed-pdf")

        self.assertEqual(status, 503)
        self.assertIn("LibreOffice/soffice", payload["error"])

    def test_approve_blocked_when_pdf_reconstruction_engine_unavailable(self):
        # ATOMICITY (P0): a PDF-source matter WITH reviewer edits needs a faithful
        # reviewed/redlined document to sign. When the pdf2docx reconstruction engine
        # is unavailable (as in the test env) that reviewed DOCX cannot be built, so
        # approval now FAILS CLOSED at the pre-flight -- surfacing the same recovery
        # payload the download route returns -- rather than marking the matter
        # cleared-to-send with no reviewed artifact (which the send path would satisfy
        # by signing the un-redlined ORIGINAL). The matter stays in_review.
        matter_id, _flagged = self._seed_matter(
            resolve_all=True, source_docx=False, with_redline=True
        )
        status, payload, _ = self.request("POST", f"/api/matters/{matter_id}/approve")

        self.assertEqual(status, 503)
        self.assertIn("pdf2docx", payload["error"])
        reconstruction = payload["pdf_docx_reconstruction"]
        self.assertEqual(reconstruction["status"], "unavailable")
        self.assertEqual(reconstruction["converter"]["mode"], "pdf_to_docx_reconstruction")
        self.assertEqual(reconstruction["fidelity"]["output"], "reviewed_docx")
        self.assertEqual(
            reconstruction["fidelity"]["message"],
            pdf_docx_reconstruction.PDF_DOCX_RECONSTRUCTION_FIDELITY_MESSAGE,
        )
        # Fail-closed: not approved.
        self.assertEqual(matter_store.get_matter(matter_id)["status"], "in_review")

    def test_reviewed_pdf_reports_unavailable_pdf_reconstruction_engine(self):
        # Same fail-closed contract via the reviewed-PDF lens: with a redline the
        # reviewed DOCX reconstruction is attempted, and an unavailable pdf2docx engine
        # blocks approval before any reviewed artifact is minted.
        matter_id, _flagged = self._seed_matter(
            resolve_all=True, source_docx=False, with_redline=True
        )
        status, payload, _ = self.request("POST", f"/api/matters/{matter_id}/approve")

        self.assertEqual(status, 503)
        self.assertIn("pdf2docx", payload["error"])
        reconstruction = payload["pdf_docx_reconstruction"]
        self.assertEqual(reconstruction["status"], "unavailable")
        self.assertEqual(reconstruction["converter"]["mode"], "pdf_to_docx_reconstruction")
        self.assertEqual(reconstruction["fidelity"]["output"], "reviewed_docx")
        self.assertEqual(matter_store.get_matter(matter_id)["status"], "in_review")

    def test_reviewed_docx_zero_redline_pdf_serves_original_not_verified_reconstruction(self):
        # End-to-end through the real /reviewed-docx route: a PDF-source matter with NO
        # accepted redlines (the stub engine emits none) must serve the ORIGINAL PDF
        # unchanged -- NO lossy reconstruction, and NEVER stamped as a verified
        # reconstruction. (pdf2docx is unavailable in the test env, so if the old code
        # path ran it would 503 instead of returning the original.)
        original_pdf = b"%PDF-1.7\nsource pdf\n%%EOF\n"
        matter_id, _flagged = self._seed_matter(resolve_all=True, source_docx=False)
        approve_status, _, _ = self.request("POST", f"/api/matters/{matter_id}/approve")
        self.assertEqual(approve_status, 200)

        status, body, headers = self.request("GET", f"/api/matters/{matter_id}/reviewed-docx")

        self.assertEqual(status, 200)
        # Faithful output IS the original PDF bytes, served unchanged.
        self.assertEqual(body, original_pdf)
        self.assertEqual(headers.get("Content-Type"), "application/pdf")
        self.assertIn('filename="mutual-nda.pdf"', headers.get("Content-Disposition", ""))
        # Honest verified value: original, NOT a verified reconstruction.
        self.assertEqual(
            headers.get("X-Export-Verified"),
            redline_export_service.ORIGINAL_UNCHANGED_EXPORT_HEADER,
        )
        self.assertEqual(
            headers.get(redline_export_service.ORIGINAL_EXPORT_MARKER_HEADER),
            redline_export_service.ORIGINAL_UNCHANGED_EXPORT_HEADER,
        )
        self.assertEqual(headers.get("X-Reviewed-Redline-Count"), "0")
        self.assertNotIn("X-PDF-DOCX-Reconstruction", headers)

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
