"""Category A: dynamic-clause AI-edit honoring + same-paragraph span coalescing.

Covers ``clause_outcomes`` (U2): the AI's per-span edit list wins over the
force-delete last resort, the force-delete still runs when AI edits are empty or
malformed (degrade-safe), multiple spans on one paragraph coalesce to a single
replace, and the deterministic (non-AI) path is unchanged.
"""
import unittest

from nda_automation.clause_outcomes import (
    _clause_has_span_edits,
    _dynamic_clause_redlines,
    _redlines_from_ai_edits,
    build_redline_edits,
    redline_edits_for_clause,
)

PROHIBITED_TEXT = (
    "The Receiving Party shall not solicit, hire, or circumvent the Disclosing "
    "Party for two years."
)


def _paragraphs():
    return [{"id": "p1", "index": 1, "text": PROHIBITED_TEXT}]


def _paragraphs_by_id(paragraphs=None):
    paragraphs = paragraphs if paragraphs is not None else _paragraphs()
    return {p["id"]: p for p in paragraphs}


def _prohibited_clause(**overrides):
    clause = {
        "id": "non_circumvention",
        "name": "Non-circumvention",
        "status": "check",
        "issue_type": "present_but_wrong",
        "matched_paragraph_ids": ["p1"],
        "fallback": {"redline_action": "delete_paragraph"},
        "what_to_fix": "Remove the prohibited restraint.",
        "reason": "Prohibited restraint present.",
    }
    clause.update(overrides)
    return clause


def _strike_edit(anchor, lowered_text, paragraph_id="p1"):
    """A lowered strike-span edit as the contract would emit it."""
    return {
        "action": "replace_paragraph",
        "paragraph_id": paragraph_id,
        "text": lowered_text,
        "span_action": "strike_span",
        "span_anchor_quote": anchor,
    }


class DynamicClauseHonorTests(unittest.TestCase):
    def test_force_delete_fallback_runs_when_no_ai_edits(self):
        # The deterministic dynamic path is unchanged: a present-but-wrong prohibited
        # clause with NO proposed_edits force-deletes the matched paragraph.
        clause = _prohibited_clause()
        edits = _dynamic_clause_redlines(clause, _paragraphs_by_id(), 1)
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["action"], "delete_paragraph")
        self.assertEqual(edits[0]["paragraph_id"], "p1")

    def test_ai_edits_win_over_force_delete(self):
        # When the AI authored a surgical strike, it is honored INSTEAD of the
        # whole-paragraph force-delete (the model's wording is preserved).
        clause = _prohibited_clause(
            proposed_edits=[
                _strike_edit(
                    "solicit, hire, or circumvent ",
                    "The Receiving Party shall not the Disclosing Party for two years.",
                )
            ]
        )
        edits = _dynamic_clause_redlines(clause, _paragraphs_by_id(), 1)
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["action"], "replace_paragraph")
        self.assertNotIn("solicit, hire, or circumvent", edits[0]["replacement_text"])
        self.assertIn("the Disclosing Party for two years", edits[0]["replacement_text"])

    def test_malformed_ai_edits_fall_back_to_force_delete_without_crashing(self):
        # Every AI edit unusable (missing paragraph) -> force-delete fallback runs.
        clause = _prohibited_clause(
            proposed_edits=[
                {"action": "replace_paragraph", "paragraph_id": "p999", "text": "x"},
                {"action": "no_change"},
            ]
        )
        edits = _dynamic_clause_redlines(clause, _paragraphs_by_id(), 1)
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["action"], "delete_paragraph")
        # The dropped edit is noted for the audit trail (telemetry, never raises).
        self.assertTrue(clause.get("catA_dropped_edits"))

    def test_empty_proposed_edits_list_falls_back_to_force_delete(self):
        clause = _prohibited_clause(proposed_edits=[])
        edits = _dynamic_clause_redlines(clause, _paragraphs_by_id(), 1)
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["action"], "delete_paragraph")

    def test_legacy_singular_proposed_redline_is_honored_via_compat_accessor(self):
        # A stored v2 matter carries only proposed_redline; it still routes through
        # the AI-honor path (delete_paragraph here) rather than re-deriving.
        clause = _prohibited_clause(
            proposed_redline={"action": "delete_paragraph", "paragraph_id": "p1"}
        )
        clause.pop("proposed_edits", None)
        edits = _dynamic_clause_redlines(clause, _paragraphs_by_id(), 1)
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["action"], "delete_paragraph")


class SameParagraphCoalesceTests(unittest.TestCase):
    def test_two_spans_on_one_paragraph_coalesce_to_single_replace(self):
        clause = _prohibited_clause(
            proposed_edits=[
                _strike_edit("solicit, hire, or circumvent ", "IGNORED_FULL_1"),
                {
                    "action": "replace_paragraph",
                    "paragraph_id": "p1",
                    "text": "IGNORED_FULL_2",
                    "span_action": "replace_span",
                    "span_anchor_quote": "for two years",
                    "span_replacement": "for one year",
                },
            ]
        )
        edits = _redlines_from_ai_edits(clause, _paragraphs_by_id(), 1)
        # ONE replace per changed paragraph (keeps the coverage gate 1:1 valid).
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["action"], "replace_paragraph")
        result = edits[0]["replacement_text"]
        # BOTH cuts landed, composed onto the ORIGINAL text (the bogus full
        # replacements were correctly ignored in favor of span composition).
        self.assertNotIn("solicit, hire, or circumvent", result)
        self.assertIn("for one year", result)
        self.assertNotIn("for two years", result)

    def test_distinct_paragraph_edits_stay_distinct(self):
        paragraphs = [
            {"id": "p1", "index": 1, "text": PROHIBITED_TEXT},
            {"id": "p2", "index": 2, "text": "The Receiving Party shall also not poach staff."},
        ]
        clause = _prohibited_clause(
            matched_paragraph_ids=["p1", "p2"],
            proposed_edits=[
                _strike_edit("solicit, hire, or circumvent ", "x", paragraph_id="p1"),
                _strike_edit("also not poach staff", "y", paragraph_id="p2"),
            ],
        )
        edits = _redlines_from_ai_edits(clause, _paragraphs_by_id(paragraphs), 1)
        self.assertEqual({e["paragraph_id"] for e in edits}, {"p1", "p2"})
        self.assertEqual(len(edits), 2)


class CrossClauseMutationBoundTests(unittest.TestCase):
    """A7-03: an edit may only redline the clause's OWN matched paragraphs."""

    def _doc(self):
        return [
            {"id": "p11", "index": 1, "text": "Recipient shall not circumvent the Discloser."},
            {"id": "p_sig", "index": 2, "text": "Signed: Daniyal Ahmad, Aspora."},
            {"id": "p_gov", "index": 3, "text": "Governed by the laws of England and Wales."},
        ]

    def test_cross_clause_edit_targeting_signature_and_govlaw_is_dropped(self):
        # The non_circ clause cites only p11, but its edits target p_sig and p_gov.
        clause = _prohibited_clause(
            matched_paragraph_ids=["p11"],
            proposed_edits=[
                {
                    "action": "replace_paragraph",
                    "paragraph_id": "p_sig",
                    "text": "Attacker Name",
                },
                {
                    "action": "delete_paragraph",
                    "paragraph_id": "p_gov",
                },
            ],
        )
        paragraphs_by_id = _paragraphs_by_id(self._doc())
        edits = _redlines_from_ai_edits(clause, paragraphs_by_id, 1)
        # BOTH cross-clause edits dropped: nothing built, no foreign paragraph touched.
        self.assertEqual(edits, [])
        targeted = {e["paragraph_id"] for e in edits if "paragraph_id" in e}
        self.assertNotIn("p_sig", targeted)
        self.assertNotIn("p_gov", targeted)
        notes = " ".join(clause.get("catA_dropped_edits", []))
        self.assertIn("p_sig", notes)
        self.assertIn("p_gov", notes)
        self.assertIn("outside the clause's matched set", notes)

    def test_legitimate_edit_on_own_matched_paragraph_still_applies(self):
        clause = _prohibited_clause(
            matched_paragraph_ids=["p11"],
            proposed_edits=[
                {
                    "action": "replace_paragraph",
                    "paragraph_id": "p11",
                    "text": "Recipient may deal freely with the Discloser.",
                    "span_action": "strike_span",
                    "span_anchor_quote": "shall not circumvent the Discloser",
                }
            ],
        )
        edits = _redlines_from_ai_edits(clause, _paragraphs_by_id(self._doc()), 1)
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["paragraph_id"], "p11")
        self.assertNotIn("shall not circumvent", edits[0]["replacement_text"])

    def test_empty_matched_set_fails_safe_by_dropping_every_edit(self):
        # Fail-safe: no matched_paragraph_ids means NO paragraph is owned; do NOT
        # widen to the whole document — every edit is dropped.
        clause = _prohibited_clause(
            matched_paragraph_ids=[],
            proposed_edits=[
                {"action": "replace_paragraph", "paragraph_id": "p11", "text": "x"},
                {"action": "replace_paragraph", "paragraph_id": "p_sig", "text": "Attacker Name"},
            ],
        )
        edits = _redlines_from_ai_edits(clause, _paragraphs_by_id(self._doc()), 1)
        self.assertEqual(edits, [])


class LengthAndCountCapTests(unittest.TestCase):
    """A6-05: oversized anchor/replacement and runaway edit counts are dropped."""

    def test_oversized_replacement_is_dropped(self):
        from nda_automation.clause_outcomes import CATA_MAX_REPLACEMENT_CHARS

        clause = _prohibited_clause(
            proposed_edits=[
                {
                    "action": "replace_paragraph",
                    "paragraph_id": "p1",
                    "text": "x" * (CATA_MAX_REPLACEMENT_CHARS + 1),
                }
            ]
        )
        edits = _redlines_from_ai_edits(clause, _paragraphs_by_id(), 1)
        self.assertEqual(edits, [])
        self.assertIn("exceeds", " ".join(clause.get("catA_dropped_edits", [])))

    def test_oversized_anchor_quote_is_dropped(self):
        from nda_automation.clause_outcomes import CATA_MAX_ANCHOR_QUOTE_CHARS

        clause = _prohibited_clause(
            proposed_edits=[
                {
                    "action": "replace_paragraph",
                    "paragraph_id": "p1",
                    "text": "ok",
                    "anchor_quote": "a" * (CATA_MAX_ANCHOR_QUOTE_CHARS + 1),
                }
            ]
        )
        edits = _redlines_from_ai_edits(clause, _paragraphs_by_id(), 1)
        self.assertEqual(edits, [])

    def test_too_many_edits_drops_all(self):
        from nda_automation.clause_outcomes import CATA_MAX_EDITS_PER_CLAUSE

        clause = _prohibited_clause(
            proposed_edits=[
                {"action": "replace_paragraph", "paragraph_id": "p1", "text": "ok"}
                for _ in range(CATA_MAX_EDITS_PER_CLAUSE + 1)
            ]
        )
        edits = _redlines_from_ai_edits(clause, _paragraphs_by_id(), 1)
        self.assertEqual(edits, [])
        self.assertIn("exceeds", " ".join(clause.get("catA_dropped_edits", [])))


class NonStringReplacementTests(unittest.TestCase):
    """A6-06: a non-string replacement is dropped, never str()-coerced to a repr."""

    def test_dict_replacement_is_dropped_no_repr_in_output(self):
        clause = _prohibited_clause(
            proposed_edits=[
                {
                    "action": "replace_paragraph",
                    "paragraph_id": "p1",
                    "replacement": {"evil": "payload"},
                }
            ]
        )
        edits = _redlines_from_ai_edits(clause, _paragraphs_by_id(), 1)
        self.assertEqual(edits, [])
        # No Python repr of the dict leaked into any built redline text.
        rendered = " ".join(str(e.get("replacement_text", "")) for e in edits)
        self.assertNotIn("evil", rendered)
        self.assertNotIn("{", rendered)

    def test_list_anchor_is_dropped(self):
        clause = _prohibited_clause(
            proposed_edits=[
                {
                    "action": "replace_paragraph",
                    "paragraph_id": "p1",
                    "text": "ok",
                    "anchor_quote": ["not", "a", "string"],
                }
            ]
        )
        edits = _redlines_from_ai_edits(clause, _paragraphs_by_id(), 1)
        self.assertEqual(edits, [])


class NativeClauseAndDeterministicPathTests(unittest.TestCase):
    def test_clause_has_span_edits_detects_span_provenance(self):
        self.assertTrue(
            _clause_has_span_edits(
                {"proposed_edits": [_strike_edit("a", "b")]}
            )
        )
        self.assertFalse(
            _clause_has_span_edits(
                {"proposed_edits": [{"action": "replace_paragraph", "paragraph_id": "p1", "text": "x"}]}
            )
        )
        self.assertFalse(_clause_has_span_edits({}))

    def test_native_whole_paragraph_replace_defers_to_native_builder(self):
        # A native clause (governing_law) with a NON-span whole-paragraph replace must
        # still flow through the registered builder so it keeps template_options.
        clause = {
            "id": "governing_law",
            "name": "Governing law",
            "status": "check",
            "issue_type": "present_but_wrong",
            "matched_paragraph_ids": ["p1"],
            "approved_laws": ["India", "England and Wales"],
            "preferred_law": "England and Wales",
            "what_to_fix": "Use an approved law.",
            "reason": "Unapproved law.",
            # A plain whole-paragraph replace (no span_action): must NOT preempt.
            "proposed_edits": [
                {
                    "action": "replace_paragraph",
                    "paragraph_id": "p1",
                    "text": "This Agreement shall be governed by the laws of England and Wales.",
                }
            ],
        }
        paragraphs = [{"id": "p1", "index": 1, "text": "Governed by the laws of California."}]
        edits = redline_edits_for_clause(clause, _paragraphs_by_id(paragraphs), 1)
        self.assertEqual(len(edits), 1)
        # The native builder added template_options (the AI-honor path would not).
        self.assertIn("template_options", edits[0])

    def test_deterministic_non_ai_path_unchanged(self):
        # A clause with NO proposed_edits/proposed_redline (the deterministic engine's
        # output) behaves exactly as before: force-delete for the prohibited clause.
        clause = _prohibited_clause()
        self.assertNotIn("proposed_edits", clause)
        deterministic = build_redline_edits([clause], _paragraphs())
        self.assertEqual(len(deterministic), 1)
        self.assertEqual(deterministic[0]["action"], "delete_paragraph")


if __name__ == "__main__":
    unittest.main()
