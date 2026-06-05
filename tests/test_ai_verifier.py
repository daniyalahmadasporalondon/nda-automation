"""Tests for the adversarial AI verifier pass.

Two layers:
- Unit: apply_ai_verifier / default_verifier / build_verifier_packet in isolation,
  driving each justify-or-refute branch with an injected verifier across the seam.
- Integration: review_nda with the verifier wired in, including the regression case
  that pins the eval gate (the non_circumvention freedom-to-deal carve-out the
  keyword checker false-flags as a restriction).
"""
import os
import unittest
from unittest.mock import patch

from nda_automation import ai_verifier
from nda_automation.ai_verifier import (
    AI_VERIFIER_VERSION,
    VERIFIER_ENV_ENABLED,
    VERIFIER_VERDICT_AFFIRM,
    VERIFIER_VERDICT_REFUTE,
    VERIFIER_VERDICT_UNCERTAIN,
    OpenRouterVerifier,
    apply_ai_verifier,
    build_verifier_packet,
    default_verifier,
    resolve_verifier,
    verifier_enabled,
)
from nda_automation.ai_assessment_contract import AI_REDLINE_NO_CHANGE
from nda_automation.ai_first_review import build_ai_first_review_result
from nda_automation.checker import review_nda
from nda_automation.redline_actions import REDLINE_REPLACE_PARAGRAPH


def _clause(clause_id, decision, *, name=None, requirement="", clause_type="", **overrides):
    clause = {
        "id": clause_id,
        "name": name or clause_id,
        "requirement": requirement,
        "type": clause_type,
        "decision": decision,
        "status": {"pass": "match", "review": "review", "fail": "check"}[decision],
        "passes": decision == "pass",
        "needs_review": decision == "review",
        "decision_source": "deterministic",
        "decision_reason": f"{clause_id} {decision} reason",
        "reason": f"{clause_id} {decision} reason",
        "reason_code": f"{clause_id}_{decision}",
        "reason_codes": [f"{clause_id}_{decision}"],
        "matched_text": "",
        "evidence": [],
        "matched_paragraph_ids": [],
        "confidence": 0.7,
    }
    clause.update(overrides)
    return clause


def _scripted(verdict, *, confidence=0.95, rationale="scripted"):
    def verifier(_packet):
        return {"verdict": verdict, "confidence": confidence, "rationale": rationale}

    return verifier


class ApplyVerifierTests(unittest.TestCase):
    def test_disabled_is_a_no_op_copy(self):
        clauses = [_clause("non_circumvention", "fail", clause_type="prohibited")]
        updated, summary = apply_ai_verifier(clauses, source_text="x", enabled=False)
        self.assertEqual(summary["status"], "disabled")
        self.assertEqual(updated[0]["decision"], "fail")
        # Returned a copy, not the same object.
        self.assertIsNot(updated[0], clauses[0])

    def test_refute_downgrades_a_fail_to_pass(self):
        clauses = [_clause("non_circumvention", "fail", clause_type="prohibited")]
        updated, summary = apply_ai_verifier(
            clauses, source_text="x", verifier=_scripted(VERIFIER_VERDICT_REFUTE)
        )
        clause = updated[0]
        self.assertEqual(clause["decision"], "pass")
        self.assertEqual(clause["decision_source"], "ai_verifier")
        self.assertTrue(clause["passes"])
        self.assertEqual(summary["changed_count"], 1)
        self.assertEqual(clause["ai_verifier"]["outcome"], "downgraded")
        self.assertEqual(clause["ai_verifier"]["original_decision"], "fail")

    def test_refute_escalates_a_suspect_pass_to_review(self):
        # A confidently refuted *pass* (the engine wrongly cleared) must escalate --
        # the verifier never invents a fail, but it won't let a suspect clear stand.
        clauses = [_clause("confidential_information", "pass", confidence=0.4)]
        updated, _ = apply_ai_verifier(
            clauses, source_text="x", verifier=_scripted(VERIFIER_VERDICT_REFUTE)
        )
        self.assertEqual(updated[0]["decision"], "review")
        self.assertEqual(updated[0]["ai_verifier"]["outcome"], "escalated")

    def test_low_confidence_refute_flags_for_review_not_flip(self):
        clauses = [_clause("non_circumvention", "fail", clause_type="prohibited")]
        updated, _ = apply_ai_verifier(
            clauses,
            source_text="x",
            verifier=_scripted(VERIFIER_VERDICT_REFUTE, confidence=0.2),
        )
        self.assertEqual(updated[0]["decision"], "review")
        self.assertEqual(updated[0]["ai_verifier"]["outcome"], "flagged_for_review")

    def test_uncertain_softens_a_fail_to_review(self):
        clauses = [_clause("non_circumvention", "fail", clause_type="prohibited")]
        updated, _ = apply_ai_verifier(
            clauses, source_text="x", verifier=_scripted(VERIFIER_VERDICT_UNCERTAIN)
        )
        self.assertEqual(updated[0]["decision"], "review")
        self.assertEqual(updated[0]["ai_verifier"]["outcome"], "softened_to_review")

    def test_affirm_leaves_the_finding_untouched(self):
        clauses = [_clause("non_circumvention", "fail", clause_type="prohibited")]
        updated, summary = apply_ai_verifier(
            clauses, source_text="x", verifier=_scripted(VERIFIER_VERDICT_AFFIRM)
        )
        self.assertEqual(updated[0]["decision"], "fail")
        self.assertFalse(updated[0]["ai_verifier"]["changed"])
        self.assertEqual(summary["changed_count"], 0)

    def test_high_confidence_pass_is_skipped(self):
        clauses = [_clause("governing_law", "pass", confidence=0.97)]
        called = []

        def spy(packet):
            called.append(packet["clause_id"])
            return {"verdict": VERIFIER_VERDICT_AFFIRM, "confidence": 1.0}

        _, summary = apply_ai_verifier(clauses, source_text="x", verifier=spy)
        self.assertEqual(called, [])  # never spent a call on a confident pass
        self.assertEqual(summary["verified_count"], 0)

    def test_low_confidence_pass_is_verified(self):
        clauses = [_clause("governing_law", "pass", confidence=0.3)]
        called = []

        def spy(packet):
            called.append(packet["clause_id"])
            return {"verdict": VERIFIER_VERDICT_AFFIRM, "confidence": 1.0}

        apply_ai_verifier(clauses, source_text="x", verifier=spy)
        self.assertEqual(called, ["governing_law"])

    def test_verifier_exception_is_recorded_not_raised(self):
        clauses = [_clause("non_circumvention", "fail", clause_type="prohibited")]

        def boom(_packet):
            raise RuntimeError("model exploded")

        updated, summary = apply_ai_verifier(clauses, source_text="x", verifier=boom)
        self.assertEqual(updated[0]["decision"], "fail")  # finding preserved
        self.assertEqual(summary["records"][0]["outcome"], "skipped")
        self.assertIn("model exploded", summary["records"][0]["rationale"])

    def test_invalid_verdict_is_treated_as_affirm(self):
        clauses = [_clause("non_circumvention", "fail", clause_type="prohibited")]
        updated, _ = apply_ai_verifier(
            clauses, source_text="x", verifier=lambda _p: {"verdict": "garbage", "confidence": 1.0}
        )
        self.assertEqual(updated[0]["decision"], "fail")


class BuildPacketTests(unittest.TestCase):
    def test_packet_carries_finding_and_clause_text(self):
        clause = _clause(
            "non_circumvention",
            "fail",
            clause_type="prohibited",
            requirement="No non-circumvention restriction.",
            matched_text="The Recipient must not circumvent the Company.",
            evidence=["The Recipient must not circumvent the Company."],
        )
        packet = build_verifier_packet(clause, source_text="full doc")
        self.assertEqual(packet["clause_id"], "non_circumvention")
        self.assertEqual(packet["engine_decision"], "fail")
        self.assertEqual(packet["clause_type"], "prohibited")
        self.assertIn("circumvent", packet["matched_text"])
        self.assertEqual(packet["source_text"], "full doc")


class DefaultVerifierTests(unittest.TestCase):
    """The offline polarity-aware adversary."""

    def _packet(self, decision, text, *, clause_type="prohibited", finding=None, clause_id="non_circumvention", clause_name="Non-circumvention", requirement=""):
        if finding is None:
            finding = "prohibited non-circumvention restriction found"
        return {
            "engine_decision": decision,
            "clause_type": clause_type,
            "matched_text": text,
            "evidence": [text],
            "engine_finding": finding,
            "requirement": requirement,
            "clause_name": clause_name,
            "clause_id": clause_id,
            "source_text": text,
        }

    def test_refutes_freedom_to_deal_carveout(self):
        verdict = default_verifier(
            self._packet("fail", "Each party shall not be restricted from dealing with introduced contacts.")
        )
        self.assertEqual(verdict["verdict"], VERIFIER_VERDICT_REFUTE)

    def test_refutes_nothing_restricts_carveout(self):
        verdict = default_verifier(
            self._packet("fail", "Nothing in this Agreement restricts either party from contacting introduced parties.")
        )
        self.assertEqual(verdict["verdict"], VERIFIER_VERDICT_REFUTE)

    def test_affirms_genuine_prohibition(self):
        verdict = default_verifier(
            self._packet("fail", "The Recipient must not circumvent the Company or deal directly with introduced parties.")
        )
        self.assertEqual(verdict["verdict"], VERIFIER_VERDICT_AFFIRM)

    def test_affirms_mixed_freedom_and_passive_prohibition(self):
        # Freedom language co-located with a real passive prohibition must NOT refute.
        verdict = default_verifier(
            self._packet(
                "fail",
                "Each party is not restricted from public dealings; however the Recipient "
                "is prohibited from dealing directly with introduced parties.",
            )
        )
        self.assertEqual(verdict["verdict"], VERIFIER_VERDICT_AFFIRM)

    def test_does_not_refute_a_required_clause(self):
        # Freedom-to-deal text is off-topic for a required clause -> never refute it.
        # (A required clause's finding is about a missing/weak obligation, not a
        # restriction, so neither clause_type nor the keyword fallback flags it.)
        verdict = default_verifier(
            self._packet(
                "review",
                "Each party shall not be restricted from dealing with introduced contacts.",
                clause_type="required",
                clause_id="mutuality",
                clause_name="Mutuality",
                requirement="Confidentiality obligations must be mutual.",
                finding="Mutuality of confidentiality obligations is unclear.",
            )
        )
        self.assertEqual(verdict["verdict"], VERIFIER_VERDICT_AFFIRM)

    def test_empty_text_affirms(self):
        verdict = default_verifier(self._packet("fail", ""))
        self.assertEqual(verdict["verdict"], VERIFIER_VERDICT_AFFIRM)


class ReviewNdaIntegrationTests(unittest.TestCase):
    def test_verifier_summary_attached_to_result(self):
        result = review_nda("This Agreement shall be governed by the laws of England and Wales.")
        verifier = result["ai_verifier"]
        self.assertEqual(verifier["version"], AI_VERIFIER_VERSION)
        self.assertIn("records", verifier)

    # The verifier's non_circumvention correction is now exercised end-to-end by
    # AIFirstPathIntegrationTests below, not through review_nda(). non_circumvention
    # migrated to a dynamic (engine=="dynamic") clause that only the AI-first path
    # emits, so the five former integration tests here -- which drove it through the
    # deterministic review_nda() and asserted a deterministic-only correction path
    # (decision_source=="deterministic", reason_code "no_non_circumvention_restriction",
    # etc.) -- tested a pathway that no longer exists. Every behavior they covered now
    # lives elsewhere, on the shipping path:
    #   - refute false-flag -> pass, evidence-trust + audit-trace intact:
    #       AIFirstPathIntegrationTests.test_verifier_corrects_ai_first_false_flag
    #   - a correct AI pass left untouched:
    #       AIFirstPathIntegrationTests.test_verifier_leaves_correct_ai_first_pass_untouched
    #   - affirm a genuine restriction -> stays fail (no over-correction):
    #       AIFirstPathIntegrationTests.test_ai_first_genuine_restriction_stays_failed
    #   - verify=False preserves the AI finding (verifier disabled):
    #       AIFirstPathIntegrationTests.test_verify_false_preserves_ai_first_finding
    #   - a verifier-refuted pass reads as absence grounding (decision_source==ai_verifier):
    #       tests/test_evidence_grounding.py::test_verifier_cleared_pass_on_required_clause_is_absence
    #       and ::test_verifier_cleared_pass_becomes_absence_and_drops_citation


class ResolveVerifierTests(unittest.TestCase):
    """The prod resolver gates the paid Claude pass and fails safe to offline."""

    def test_disabled_resolves_to_offline_adversary(self):
        with patch.dict(os.environ, {VERIFIER_ENV_ENABLED: ""}, clear=False):
            self.assertFalse(verifier_enabled())
            self.assertIs(resolve_verifier(), default_verifier)

    def test_enabled_without_key_falls_back_to_offline(self):
        with patch.dict(os.environ, {VERIFIER_ENV_ENABLED: "1", "OPENROUTER_API_KEY": ""}, clear=False):
            with patch.object(ai_verifier, "_verifier_api_key", return_value=""):
                self.assertTrue(verifier_enabled())
                self.assertIs(resolve_verifier(), default_verifier)

    def test_enabled_with_key_resolves_claude_backed_verifier(self):
        with patch.dict(os.environ, {VERIFIER_ENV_ENABLED: "true"}, clear=False):
            with patch.object(ai_verifier, "_verifier_api_key", return_value="sk-test"):
                resolved = resolve_verifier()
                self.assertIsInstance(resolved, OpenRouterVerifier)
                # Defaults to a strong Claude model.
                self.assertIn("claude", resolved.model.lower())

    def test_summary_reports_offline_kind_by_default(self):
        # No env opt-in -> offline adversary, surfaced for observability.
        with patch.dict(os.environ, {VERIFIER_ENV_ENABLED: ""}, clear=False):
            clauses = [_clause("non_circumvention", "fail", clause_type="prohibited")]
            _, summary = apply_ai_verifier(clauses, source_text="x")
            self.assertEqual(summary["verifier_kind"], "offline")

    def test_summary_reports_injected_kind(self):
        clauses = [_clause("non_circumvention", "fail", clause_type="prohibited")]
        _, summary = apply_ai_verifier(
            clauses, source_text="x", verifier=_scripted(VERIFIER_VERDICT_AFFIRM)
        )
        self.assertEqual(summary["verifier_kind"], "injected")


class AIFirstPathIntegrationTests(unittest.TestCase):
    """The SHIPPING path: the verifier must protect real AI-first reviews, not just
    the deterministic review_nda path the eval gate exercises."""

    SOURCE_TEXT = "\n\n".join([
        "Each party may disclose Confidential Information and both act as Disclosing and Receiving Party.",
        '"Confidential Information" means non-public business, financial, technical, customer, supplier, '
        "pricing, market, product, proprietary and trade secret information disclosed by either party.",
        "This Agreement shall be governed by the laws of England and Wales.",
        "The confidentiality obligations survive for a fixed period of three years.",
        "Each party shall not be restricted from dealing with any contact introduced by the other party.",
        "For Aspora Limited\nBy:\nName:\nTitle:\nDate:\nFor Counterparty Ltd\nBy:\nName:\nTitle:\nDate:",
    ])
    QUOTES = {
        "mutuality": ("p1", "Each party may disclose Confidential Information"),
        "confidential_information": ("p2", '"Confidential Information" means non-public business'),
        "governing_law": ("p3", "laws of England and Wales"),
        "term_and_survival": ("p4", "fixed period of three years"),
        "non_circumvention": ("p5", "shall not be restricted from dealing with any contact introduced"),
        "signatures": ("p6", "For Aspora Limited"),
    }

    def _assessment(self, clause_id, decision, *, issue_type=None, rationale=None):
        paragraph_id, quote = self.QUOTES[clause_id]
        if issue_type is None:
            issue_type = "none" if decision == "pass" else "present_but_wrong"
        # Contract: a fail needs a real redline action; blocks_send is review-only.
        if decision == "fail":
            proposed_redline = {
                "action": REDLINE_REPLACE_PARAGRAPH,
                "paragraph_id": paragraph_id,
                "text": "Each party remains free to deal with any party introduced by the other.",
            }
        else:
            proposed_redline = {"action": AI_REDLINE_NO_CHANGE}
        return {
            "clause_id": clause_id,
            "decision": decision,
            "issue_type": issue_type,
            "rationale": rationale or f"{clause_id} assessed against the playbook with cited text.",
            "evidence": [{"paragraph_id": paragraph_id, "quote": quote, "relevance": "Cited."}],
            "proposed_redline": proposed_redline,
            "confidence": 0.92,
            "blocks_send": decision == "review",
        }

    def _all_assessments(self, non_circ_decision):
        return [
            self._assessment("mutuality", "pass"),
            self._assessment("confidential_information", "pass"),
            self._assessment("governing_law", "pass"),
            self._assessment("term_and_survival", "pass"),
            self._assessment(
                "non_circumvention",
                non_circ_decision,
                issue_type="present_but_wrong" if non_circ_decision == "fail" else "none",
                rationale="AI wrongly read the freedom-to-deal carve-out as a restriction."
                if non_circ_decision == "fail"
                else "No restriction present.",
            ),
            self._assessment("signatures", "pass"),
        ]

    def test_verifier_corrects_ai_first_false_flag(self):
        # The AI got it wrong: it FAILED a freedom-to-deal carve-out. The verifier,
        # running on the shipping path, must refute that fail to pass.
        result = build_ai_first_review_result(self.SOURCE_TEXT, self._all_assessments("fail"))
        nc = next(c for c in result["clauses"] if c["id"] == "non_circumvention")
        self.assertEqual(nc["decision"], "pass")
        self.assertEqual(nc["decision_source"], "ai_verifier")
        self.assertEqual(nc["ai_verifier"]["verdict"], "refute")
        self.assertEqual(nc["ai_verifier"]["original_decision"], "fail")
        # Evidence-trust holds (build raises EvidenceProvenanceError otherwise).
        self.assertEqual(result["evidence_trust"]["status"], "verified")
        self.assertEqual(result["audit_trace"]["decision"] if "audit_trace" in result else nc["audit_trace"]["decision"], "pass")
        self.assertEqual(result["ai_verifier"]["changed_count"], 1)

    def test_verifier_leaves_correct_ai_first_pass_untouched(self):
        # The AI got it right (pass). The verifier must not disturb a confident pass.
        result = build_ai_first_review_result(self.SOURCE_TEXT, self._all_assessments("pass"))
        nc = next(c for c in result["clauses"] if c["id"] == "non_circumvention")
        self.assertEqual(nc["decision"], "pass")
        self.assertEqual(nc["decision_source"], "ai")  # untouched by the verifier
        self.assertEqual(result["ai_verifier"]["changed_count"], 0)

    def test_verify_false_preserves_ai_first_finding(self):
        result = build_ai_first_review_result(self.SOURCE_TEXT, self._all_assessments("fail"), verify=False)
        nc = next(c for c in result["clauses"] if c["id"] == "non_circumvention")
        self.assertEqual(nc["decision"], "fail")  # the AI's false flag, unverified
        self.assertEqual(result["ai_verifier"]["status"], "disabled")

    def test_ai_first_genuine_restriction_stays_failed(self):
        # Swap in a genuine restriction text; an AI fail there must NOT be refuted.
        source = self.SOURCE_TEXT.replace(
            "Each party shall not be restricted from dealing with any contact introduced by the other party.",
            "The Recipient must not circumvent the Company or deal directly with introduced parties.",
        )
        assessments = self._all_assessments("fail")
        # Re-point the non_circ citation at the new p5 text.
        for assessment in assessments:
            if assessment["clause_id"] == "non_circumvention":
                assessment["evidence"] = [{"paragraph_id": "p5", "quote": "must not circumvent the Company", "relevance": "Cited."}]
        result = build_ai_first_review_result(source, assessments)
        nc = next(c for c in result["clauses"] if c["id"] == "non_circumvention")
        self.assertEqual(nc["decision"], "fail")
        self.assertEqual(nc["decision_source"], "ai")


if __name__ == "__main__":
    unittest.main()
