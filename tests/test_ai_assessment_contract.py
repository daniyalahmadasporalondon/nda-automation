import unittest

from nda_automation.ai_assessment_contract import (
    AI_ASSESSMENT_CONTRACT_VERSION,
    AI_CLAUSE_ASSESSMENT_SCHEMA,
    AI_REDLINE_NO_CHANGE,
    AIAssessmentContractError,
    validate_ai_clause_assessments,
)
from nda_automation.checker import load_playbook
from nda_automation.playbook_rules import normalize_playbook_policy
from nda_automation.redline_actions import REDLINE_REPLACE_PARAGRAPH
from nda_automation.review_document import split_document_paragraphs


SOURCE_TEXT = "\n\n".join([
    "Each party may disclose Confidential Information to the other party under this Agreement.",
    "This Agreement shall be governed by the laws of California.",
])


def _playbook_clauses_by_id():
    playbook = normalize_playbook_policy(load_playbook())
    return {str(clause["id"]): clause for clause in playbook["clauses"]}


def _valid_clause_ids():
    return [clause["id"] for clause in load_playbook()["clauses"]]


def _paragraphs():
    return split_document_paragraphs(SOURCE_TEXT)


def _valid_assessment(**overrides):
    assessment = {
        "clause_id": "governing_law",
        "decision": "fail",
        "issue_type": "present_but_wrong",
        "rationale": (
            "The clause selects California law, which is outside the approved governing-law options for this playbook. "
            "A reviewer should treat it as non-compliant even though the clause is otherwise clearly drafted."
        ),
        "evidence": [{
            "quote": "laws of california",
            "relevance": "Shows the governing-law jurisdiction.",
        }],
        "proposed_redline": {
            "action": REDLINE_REPLACE_PARAGRAPH,
            "paragraph_id": "p2",
            "text": "This Agreement shall be governed by the laws of England and Wales.",
            "jurisdiction": "England and Wales",
        },
        "confidence": 0.94,
        "blocks_send": False,
    }
    assessment.update(overrides)
    return assessment


class AIAssessmentContractTests(unittest.TestCase):
    def test_schema_freezes_required_ai_clause_assessment_fields(self):
        self.assertEqual(AI_CLAUSE_ASSESSMENT_SCHEMA["properties"]["schema_version"]["const"], AI_ASSESSMENT_CONTRACT_VERSION)
        self.assertIn("rationale", AI_CLAUSE_ASSESSMENT_SCHEMA["required"])
        self.assertNotIn("why_it_might_be_a_problem", AI_CLAUSE_ASSESSMENT_SCHEMA["properties"])
        self.assertNotIn("why_it_may_be_fine", AI_CLAUSE_ASSESSMENT_SCHEMA["properties"])
        self.assertIn("proposed_redline", AI_CLAUSE_ASSESSMENT_SCHEMA["required"])
        self.assertEqual(
            AI_CLAUSE_ASSESSMENT_SCHEMA["properties"]["decision"]["enum"],
            ["pass", "fail", "review"],
        )

    def test_valid_assessment_is_normalized_and_quote_evidence_is_resolved(self):
        assessments = validate_ai_clause_assessments(
            [_valid_assessment()],
            valid_clause_ids=_valid_clause_ids(),
            paragraphs=_paragraphs(),
        )

        governing_law = assessments["governing_law"]
        self.assertEqual(governing_law["schema_version"], AI_ASSESSMENT_CONTRACT_VERSION)
        self.assertEqual(governing_law["evidence"], [{
            "paragraph_id": "p2",
            "quote": "laws of california",
            "relevance": "Shows the governing-law jurisdiction.",
        }])
        self.assertEqual(governing_law["proposed_redline"]["action"], REDLINE_REPLACE_PARAGRAPH)

    def test_quote_only_evidence_must_resolve_to_one_paragraph(self):
        source_text = "\n\n".join([
            "Confidential Information may be disclosed by either party.",
            "Confidential Information means non-public technical information.",
        ])

        with self.assertRaises(AIAssessmentContractError) as error:
            validate_ai_clause_assessments(
                [_valid_assessment(evidence=[{
                    "quote": "Confidential Information",
                    "relevance": "Short phrase appears more than once.",
                }])],
                valid_clause_ids=_valid_clause_ids(),
                paragraphs=split_document_paragraphs(source_text),
            )

        self.assertIn("quote matches multiple reviewed paragraphs; provide paragraph_id", str(error.exception))

    def test_pass_assessment_requires_no_change_redline_and_none_issue_type(self):
        assessments = validate_ai_clause_assessments(
            [{
                "clause_id": "mutuality",
                "decision": "pass",
                "issue_type": "none",
                "rationale": "The clause supports a pass because it describes each party disclosing Confidential Information to the other party, which aligns with the reciprocal playbook position.",
                "evidence": [{
                    "paragraph_id": "p1",
                    "quote": "Each party may disclose Confidential Information",
                    "relevance": "Shows reciprocal disclosure.",
                }],
                "proposed_redline": {"action": AI_REDLINE_NO_CHANGE},
                "confidence": 0.9,
                "blocks_send": False,
            }],
            valid_clause_ids=_valid_clause_ids(),
            paragraphs=_paragraphs(),
        )

        self.assertEqual(assessments["mutuality"]["proposed_redline"], {"action": AI_REDLINE_NO_CHANGE})

    def test_absence_based_pass_can_have_empty_evidence(self):
        assessments = validate_ai_clause_assessments(
            [{
                "clause_id": "non_circumvention",
                "decision": "pass",
                "issue_type": "none",
                "rationale": "No non-circumvention or substitute-purpose restriction appears in the supplied text, so the prohibited-clause check can pass on absence.",
                "evidence": [],
                "proposed_redline": {"action": AI_REDLINE_NO_CHANGE},
                "confidence": 0.82,
                "blocks_send": False,
            }],
            valid_clause_ids=_valid_clause_ids(),
            paragraphs=_paragraphs(),
        )

        self.assertEqual(assessments["non_circumvention"]["evidence"], [])

    def test_invalid_assessment_reports_contract_errors(self):
        bad = _valid_assessment(
            rationale="",
            issue_type="none",
            evidence=[{"paragraph_id": "p2", "quote": "laws of France", "relevance": "Wrong quote."}],
            proposed_redline={"action": AI_REDLINE_NO_CHANGE},
            blocks_send=True,
        )

        with self.assertRaises(AIAssessmentContractError) as error:
            validate_ai_clause_assessments([bad], valid_clause_ids=_valid_clause_ids(), paragraphs=_paragraphs())

        message = str(error.exception)
        self.assertIn("rationale must be non-empty text", message)
        self.assertIn("fail/review decisions must not use issue_type none", message)
        # "laws of France" appears nowhere in the document, so the ungroundable
        # quote is dropped (a fabrication is not crashed on); the fail then has no
        # surviving evidence, which the decision/evidence coupling rejects.
        self.assertIn("fail decisions require at least one valid evidence item", message)
        self.assertIn("fail decisions require a proposed redline action", message)
        self.assertIn("blocks_send must be true only for review decisions", message)

    def test_blank_redline_text_is_defaulted_from_governing_law_playbook(self):
        # The AI supplies the judgment (fail) but leaves the replacement wording
        # blank. With the Playbook threaded in, the governing-law fix is defaulted
        # from the approved law instead of rejecting the whole document.
        blank = _valid_assessment(proposed_redline={
            "action": REDLINE_REPLACE_PARAGRAPH,
            "paragraph_id": "p2",
        })

        assessments = validate_ai_clause_assessments(
            [blank],
            valid_clause_ids=_valid_clause_ids(),
            paragraphs=_paragraphs(),
            playbook_clauses_by_id=_playbook_clauses_by_id(),
        )

        governing_law = assessments["governing_law"]
        self.assertEqual(governing_law["decision"], "fail")
        self.assertEqual(governing_law["proposed_redline"]["action"], REDLINE_REPLACE_PARAGRAPH)
        self.assertEqual(
            governing_law["proposed_redline"]["text"],
            "This Agreement shall be governed by the laws of England and Wales.",
        )

    def test_ai_authored_redline_text_overrides_the_playbook_default(self):
        authored = _valid_assessment(proposed_redline={
            "action": REDLINE_REPLACE_PARAGRAPH,
            "paragraph_id": "p2",
            "text": "This Agreement shall be governed by the laws of India.",
        })

        assessments = validate_ai_clause_assessments(
            [authored],
            valid_clause_ids=_valid_clause_ids(),
            paragraphs=_paragraphs(),
            playbook_clauses_by_id=_playbook_clauses_by_id(),
        )

        self.assertEqual(
            assessments["governing_law"]["proposed_redline"]["text"],
            "This Agreement shall be governed by the laws of India.",
        )

    def test_blank_redline_with_no_template_degrades_one_clause_to_review(self):
        # non_circumvention is a prohibited clause: its fix is a deletion, so the
        # Playbook carries no replacement wording. A model that mislabels the fix
        # as a blank replace must not discard every other (correct) assessment;
        # the single clause degrades to a human-review flag (never a silent pass).
        source = "\n\n".join([
            "The parties shall not circumvent each other or deal directly with introduced parties.",
            "This Agreement shall be governed by the laws of England and Wales.",
        ])
        paragraphs = split_document_paragraphs(source)
        blank_no_template = {
            "clause_id": "non_circumvention",
            "decision": "fail",
            "issue_type": "present_but_wrong",
            "rationale": "A non-circumvention restriction is present and should be removed.",
            "evidence": [{
                "quote": "shall not circumvent",
                "relevance": "States the prohibited restriction.",
            }],
            "proposed_redline": {"action": REDLINE_REPLACE_PARAGRAPH, "paragraph_id": "p1"},
            "confidence": 0.9,
            "blocks_send": False,
        }

        assessments = validate_ai_clause_assessments(
            [blank_no_template],
            valid_clause_ids=_valid_clause_ids(),
            paragraphs=paragraphs,
            playbook_clauses_by_id=_playbook_clauses_by_id(),
        )

        clause = assessments["non_circumvention"]
        # A blank, untemplatable fail is escalated to review (still blocks send) —
        # never softened to a silent pass.
        self.assertEqual(clause["decision"], "review")
        self.assertTrue(clause["blocks_send"])
        self.assertEqual(clause["proposed_redline"], {"action": AI_REDLINE_NO_CHANGE})

    def test_quote_spanning_two_paragraphs_grounds_and_reanchors(self):
        # The DOCX extractor splits a single sentence across paragraph boundaries.
        # The model quotes the natural span; it is real document text but not a
        # substring of either single paragraph. It must ground document-wide and
        # re-anchor to the first containing paragraph (here p1).
        source_text = "\n\n".join([
            "This Agreement shall be governed by",
            "the laws of California and the parties submit to its courts.",
        ])
        spanning = _valid_assessment(evidence=[{
            "quote": "governed by the laws of California",
            "relevance": "The governing-law selection spans the split sentence.",
        }])

        assessments = validate_ai_clause_assessments(
            [spanning],
            valid_clause_ids=_valid_clause_ids(),
            paragraphs=split_document_paragraphs(source_text),
            playbook_clauses_by_id=_playbook_clauses_by_id(),
        )

        evidence = assessments["governing_law"]["evidence"]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["quote"], "governed by the laws of California")
        # Re-anchored to the first paragraph holding the quote's first segment.
        self.assertEqual(evidence[0]["paragraph_id"], "p1")

    def test_ellipsis_elided_quote_grounds(self):
        # The model elides the middle of a span with "...". Each non-empty segment
        # must appear, in order, in the document; the elided middle is skipped.
        source_text = "\n\n".join([
            "This Agreement shall be governed by the laws of California,",
            "without regard to its conflict-of-laws principles, in all respects.",
        ])
        elided = _valid_assessment(evidence=[{
            "quote": "governed by the laws of California ... conflict-of-laws principles",
            "relevance": "Elided governing-law span.",
        }])

        assessments = validate_ai_clause_assessments(
            [elided],
            valid_clause_ids=_valid_clause_ids(),
            paragraphs=split_document_paragraphs(source_text),
            playbook_clauses_by_id=_playbook_clauses_by_id(),
        )

        evidence = assessments["governing_law"]["evidence"]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["paragraph_id"], "p1")

    def test_ellipsis_grounding_rejects_cross_sentence_stitch(self):
        # BUGFIX: ellipsis grounding must not stitch fragments from different
        # sentences / far-apart paragraphs. "shall not ... solicit" assembled from a
        # "shall not disclose" sentence and a separate "may solicit freely" sentence
        # fabricates a prohibition that isn't in the document; it must NOT ground.
        from nda_automation.ai_assessment_contract import _ellipsis_segments_appear_in_order

        stitched_doc = (
            "The Recipient shall not disclose Confidential Information. "
            "The parties are free to deal with any third party they independently identify. "
            "The Recipient may solicit freely."
        )
        self.assertFalse(_ellipsis_segments_appear_in_order("shall not ... solicit", stitched_doc))

        # A genuine within-sentence elision (short gap, no sentence boundary) still grounds.
        legit_doc = (
            "The Recipient shall not, except as required by law, disclose any Confidential Information."
        )
        self.assertTrue(
            _ellipsis_segments_appear_in_order(
                "shall not ... disclose any Confidential Information", legit_doc
            )
        )

    def test_typographic_glyph_variants_ground(self):
        # Inbound real DOCX carries curly quotes, em-dashes and the ellipsis glyph.
        # The model echoes one glyph while the paragraph holds another; glyph
        # folding before grounding makes the quote resolve regardless.
        source_text = "\n\n".join([
            "This Agreement — the “Agreement” — shall be governed by the laws of California.",
            "Each party may disclose Confidential Information.",
        ])
        # Quote uses straight quotes/hyphen/ellipsis char against curly/em-dash text.
        glyphy = _valid_assessment(evidence=[{
            "quote": "the \"Agreement\" - shall be … laws of California",
            "relevance": "Glyph-variant governing-law span.",
        }])

        assessments = validate_ai_clause_assessments(
            [glyphy],
            valid_clause_ids=_valid_clause_ids(),
            paragraphs=split_document_paragraphs(source_text),
            playbook_clauses_by_id=_playbook_clauses_by_id(),
        )

        evidence = assessments["governing_law"]["evidence"]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["paragraph_id"], "p1")

    def test_fabricated_quote_is_dropped_without_raising(self):
        # A quote that appears NOWHERE (not in the cited paragraph, not document-
        # wide) is a genuine fabrication: it is dropped, NOT raised. The rest of the
        # assessment survives so the whole document review is not crashed. Here the
        # fail keeps a second, real evidence item so the decision is preserved.
        source_text = "\n\n".join([
            "This Agreement shall be governed by the laws of California.",
            "Each party may disclose Confidential Information.",
        ])
        mixed = _valid_assessment(evidence=[
            {"quote": "laws of California", "relevance": "Real governing-law text."},
            {"quote": "laws of the planet Mars", "relevance": "Hallucinated jurisdiction."},
        ])

        assessments = validate_ai_clause_assessments(
            [mixed],
            valid_clause_ids=_valid_clause_ids(),
            paragraphs=split_document_paragraphs(source_text),
            playbook_clauses_by_id=_playbook_clauses_by_id(),
        )

        governing_law = assessments["governing_law"]
        # Only the real quote survived; the fabrication was silently dropped.
        self.assertEqual([item["quote"] for item in governing_law["evidence"]], ["laws of California"])
        # The fail decision is untouched (it still carries grounded evidence).
        self.assertEqual(governing_law["decision"], "fail")

    def test_fully_fabricated_finding_is_surfaced_for_review_not_a_silent_pass(self):
        # GATE-2 coherence: when EVERY evidence item on a non-pass finding is a
        # fabrication, GATE 1 drops them all (no crash) and the downstream
        # evidence-grounding layer must keep/flag the now-unsupported finding for a
        # human -- never let it collapse into a silent pass.
        #
        # A "review" finding is the clean way to observe this: a fail with a real
        # issue_type would be rejected by GATE 1's fail/evidence coupling, and a
        # fail+missing is a LEGITIMATE quote-less absence (the absence is the
        # evidence), so neither exercises the ungrounded path. A review whose only
        # evidence is fabricated grounds nowhere and stays a blocking review.
        from nda_automation.ai_first_review import build_ai_first_review_result

        source_text = "\n\n".join([
            "This Agreement shall be governed by the laws of California.",
            "Each party may disclose Confidential Information.",
        ])
        fabricated_review = {
            "clause_id": "governing_law",
            "decision": "review",
            "issue_type": "unclear",
            "rationale": "The governing-law position is flagged for review on fabricated grounds.",
            "evidence": [{
                "quote": "laws of the planet Mars",
                "relevance": "Hallucinated jurisdiction that appears nowhere.",
            }],
            "proposed_redline": {"action": AI_REDLINE_NO_CHANGE},
            "confidence": 0.4,
            "blocks_send": True,
        }

        validated = validate_ai_clause_assessments(
            [fabricated_review],
            valid_clause_ids=_valid_clause_ids(),
            paragraphs=split_document_paragraphs(source_text),
            playbook_clauses_by_id=_playbook_clauses_by_id(),
        )
        # GATE 1 dropped the fabricated quote without raising.
        self.assertEqual(validated["governing_law"]["evidence"], [])

        # build_ai_first_review_result re-validates its RAW input (mirrors the
        # production path in assess_nda_with_ai), so feed it the raw assessment.
        result = build_ai_first_review_result(
            source_text,
            [fabricated_review],
            verify=False,
        )
        clause = next(c for c in result["clauses"] if c["id"] == "governing_law")
        # GATE 2 kept the ungrounded non-pass finding as a blocking human review --
        # it did NOT silently pass.
        self.assertEqual(clause["decision"], "review")
        self.assertTrue(clause["blocks_send"])
        self.assertNotEqual(clause["decision"], "pass")
        # And it carries the ungrounded reason code so the audit trail shows why.
        self.assertIn("ungrounded_finding", clause.get("reason_codes", []))

    def test_nonexistent_cited_paragraph_id_still_errors(self):
        # A paragraph_id the model invented (points at no reviewed paragraph) is a
        # structural error, distinct from a quote that merely spans boundaries.
        ghost = _valid_assessment(evidence=[{
            "paragraph_id": "p999",
            "quote": "laws of california",
            "relevance": "Cites a paragraph that does not exist.",
        }])

        with self.assertRaises(AIAssessmentContractError) as error:
            validate_ai_clause_assessments(
                [ghost],
                valid_clause_ids=_valid_clause_ids(),
                paragraphs=_paragraphs(),
            )

        self.assertIn("paragraph_id does not exist: p999", str(error.exception))

    def test_duplicate_and_unknown_clause_ids_are_invalid(self):
        with self.assertRaises(AIAssessmentContractError) as error:
            validate_ai_clause_assessments(
                [_valid_assessment(), _valid_assessment(), _valid_assessment(clause_id="unknown_clause")],
                valid_clause_ids=_valid_clause_ids(),
                paragraphs=_paragraphs(),
            )

        message = str(error.exception)
        self.assertIn("duplicate assessment for clause governing_law", message)
        self.assertIn("unknown clause_id unknown_clause", message)


if __name__ == "__main__":
    unittest.main()
