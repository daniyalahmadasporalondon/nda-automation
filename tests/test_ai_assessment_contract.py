import unittest

from nda_automation.ai_assessment_contract import (
    AI_ASSESSMENT_CONTRACT_VERSION,
    AI_CLAUSE_ASSESSMENT_SCHEMA,
    AI_REDLINE_NO_CHANGE,
    AIAssessmentContractError,
    apply_span,
    clause_proposed_edits,
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
        self.assertNotIn("severity", AI_CLAUSE_ASSESSMENT_SCHEMA["properties"])
        self.assertNotIn("impact", AI_CLAUSE_ASSESSMENT_SCHEMA["properties"])
        self.assertIn("resolution_question", AI_CLAUSE_ASSESSMENT_SCHEMA["properties"])
        self.assertIn("suggested_redline", AI_CLAUSE_ASSESSMENT_SCHEMA["properties"])
        self.assertIn("recommended_option", AI_CLAUSE_ASSESSMENT_SCHEMA["properties"])
        self.assertNotIn("why_it_might_be_a_problem", AI_CLAUSE_ASSESSMENT_SCHEMA["properties"])
        self.assertNotIn("why_it_may_be_fine", AI_CLAUSE_ASSESSMENT_SCHEMA["properties"])
        # v3 advertises BOTH the legacy singular ``proposed_redline`` and the new
        # ``proposed_edits`` list; "at least one" is enforced dynamically by the
        # validator (not via the static ``required`` list, which would otherwise
        # reject a payload that legitimately sends only one of the two).
        self.assertIn("proposed_redline", AI_CLAUSE_ASSESSMENT_SCHEMA["properties"])
        self.assertIn("proposed_edits", AI_CLAUSE_ASSESSMENT_SCHEMA["properties"])
        self.assertNotIn("proposed_redline", AI_CLAUSE_ASSESSMENT_SCHEMA["required"])
        self.assertNotIn("proposed_edits", AI_CLAUSE_ASSESSMENT_SCHEMA["required"])
        self.assertEqual(AI_ASSESSMENT_CONTRACT_VERSION, 3)
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

    def test_optional_ai_analysis_fields_are_normalized(self):
        assessments = validate_ai_clause_assessments(
            [_valid_assessment(
                resolution_question="Should this use England and Wales instead?",
                suggested_redline="This Agreement shall be governed by the laws of England and Wales.",
                recommended_option={
                    "option": "England and Wales",
                    "reason": "It is the default approved playbook option.",
                },
            )],
            valid_clause_ids=_valid_clause_ids(),
            paragraphs=_paragraphs(),
        )

        governing_law = assessments["governing_law"]
        self.assertEqual(governing_law["resolution_question"], "Should this use England and Wales instead?")
        self.assertEqual(
            governing_law["suggested_redline"],
            "This Agreement shall be governed by the laws of England and Wales.",
        )
        self.assertEqual(governing_law["recommended_option"], {
            "option": "England and Wales",
            "reason": "It is the default approved playbook option.",
        })

    def test_invalid_optional_ai_analysis_fields_report_contract_errors(self):
        with self.assertRaises(AIAssessmentContractError) as error:
            validate_ai_clause_assessments(
                [_valid_assessment(
                    recommended_option={"option": "England and Wales"},
                )],
                valid_clause_ids=_valid_clause_ids(),
                paragraphs=_paragraphs(),
            )

        message = str(error.exception)
        self.assertIn("recommended_option: reason must be non-empty text", message)

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

    def test_blank_redline_with_no_template_keeps_fail_and_flags_manual_redline(self):
        # non_circumvention is a prohibited clause: its fix is a deletion, so the
        # Playbook carries no replacement wording. A model that mislabels the fix
        # as a blank replace must not discard every other (correct) assessment.
        # The verdict and the auto-redline are SEPARATE concerns: the clause is bad,
        # so the FAIL STAYS a FAIL (never silently softened to a weaker review just
        # because the auto-fixer produced no text); we record manual_redline_needed
        # so a human writes the fix by hand. The FAIL still blocks send downstream
        # via the CHECK aggregate (requires_redline / blocks_auto_send), so the
        # clause-level blocks_send stays the model's value (False for a fail) and the
        # decision<->blocks_send coupling is not violated.
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
        # (a) the FAIL is PRESERVED — NOT demoted to review.
        self.assertEqual(clause["decision"], "fail")
        # (b) the auto-fix-unavailable metadata flag is set so a human is told to
        #     redline this manually.
        self.assertTrue(clause["manual_redline_needed"])
        # (c) clause-level blocks_send stays False (a fail is not a review); the send
        #     gate still fires via the CHECK aggregate path, not this field.
        self.assertFalse(clause["blocks_send"])
        # The redline itself genuinely collapsed to a no-op (nothing to auto-apply),
        # and the human-readable degrade note is appended to the rationale.
        self.assertEqual(clause["proposed_redline"], {"action": AI_REDLINE_NO_CHANGE})
        self.assertIn("human review", clause["rationale"].lower())

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

        # Use England and Wales so the deterministic governing-law backstop does NOT
        # fire (backstop only triggers on clearly unapproved jurisdictions such as
        # California). The test targets the ungrounded-finding path, not backstop logic.
        source_text = "\n\n".join([
            "This Agreement shall be governed by the laws of England and Wales.",
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
        self.assertEqual(clause["proposed_change"]["action"], "needs_human_choice")
        self.assertEqual(clause["proposed_change"]["safety"]["status"], "needs_human_choice")
        self.assertIn("not grounded enough", clause["proposed_change"]["safety"]["reason"])

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


# Category A: a clause may now carry a LIST of proposed edits, with sentence-level
# strike_span / replace_span sugar lowered to whole-paragraph replaces at parse time.
SPAN_SOURCE_TEXT = "\n\n".join([
    "The Receiving Party shall not solicit, hire, or circumvent the Disclosing Party for two years.",
    "This Agreement shall be governed by the laws of California.",
])


def _span_paragraphs():
    return split_document_paragraphs(SPAN_SOURCE_TEXT)


def _span_assessment(**overrides):
    # non_circumvention is a prohibited/dynamic clause; use it so the catch-all
    # restraint is the natural span target.
    assessment = {
        "clause_id": "non_circumvention",
        "decision": "fail",
        "issue_type": "present_but_wrong",
        "rationale": (
            "The clause imposes a prohibited non-solicit/non-circumvent restraint that is "
            "outside the approved confidentiality scope and must be struck."
        ),
        "evidence": [{
            "paragraph_id": "p1",
            "quote": "shall not solicit, hire, or circumvent",
            "relevance": "States the prohibited restraint.",
        }],
        "confidence": 0.93,
        "blocks_send": False,
    }
    assessment.update(overrides)
    return assessment


class CategoryASpanAndListContractTests(unittest.TestCase):
    def _validate(self, assessment, paragraphs=None):
        return validate_ai_clause_assessments(
            [assessment],
            valid_clause_ids=_valid_clause_ids(),
            paragraphs=paragraphs if paragraphs is not None else _span_paragraphs(),
            playbook_clauses_by_id=_playbook_clauses_by_id(),
        )

    def test_v2_singular_proposed_redline_parses_into_one_element_list(self):
        # Old payloads send proposed_redline; they must parse into proposed_edits=[that].
        cleaned = self._validate(_valid_assessment(), paragraphs=_paragraphs())
        gl = cleaned["governing_law"]
        self.assertIn("proposed_edits", gl)
        self.assertEqual(len(gl["proposed_edits"]), 1)
        self.assertEqual(gl["proposed_edits"][0]["action"], "replace_paragraph")
        # The legacy primary is preserved and equals the first edit.
        self.assertEqual(gl["proposed_redline"]["action"], "replace_paragraph")
        self.assertEqual(gl["proposed_redline"], gl["proposed_edits"][0])
        # The compat accessor returns the same one edit from a v3-shaped clause.
        self.assertEqual(clause_proposed_edits(gl), gl["proposed_edits"])

    def test_v2_singular_with_explicit_schema_version_two_is_accepted(self):
        cleaned = self._validate(_valid_assessment(schema_version=2), paragraphs=_paragraphs())
        gl = cleaned["governing_law"]
        self.assertEqual(len(gl["proposed_edits"]), 1)
        # Cleaned output always stamps the CURRENT contract version.
        self.assertEqual(gl["schema_version"], AI_ASSESSMENT_CONTRACT_VERSION)

    def test_v3_proposed_edits_list_parses_multiple_paragraph_edits(self):
        assessment = _valid_assessment()
        assessment.pop("proposed_redline")
        assessment["schema_version"] = 3
        assessment["proposed_edits"] = [
            {
                "action": "replace_paragraph",
                "paragraph_id": "p2",
                "text": "This Agreement shall be governed by the laws of England and Wales.",
                "jurisdiction": "England and Wales",
            },
        ]
        cleaned = self._validate(assessment, paragraphs=_paragraphs())
        gl = cleaned["governing_law"]
        self.assertEqual(len(gl["proposed_edits"]), 1)
        self.assertEqual(gl["proposed_edits"][0]["paragraph_id"], "p2")

    def test_strike_span_lowers_to_replace_paragraph_minus_the_span(self):
        assessment = _span_assessment(
            schema_version=3,
            proposed_edits=[{
                "action": "strike_span",
                "paragraph_id": "p1",
                "anchor_quote": "solicit, hire, or circumvent",
            }],
        )
        cleaned = self._validate(assessment)
        clause = cleaned["non_circumvention"]
        edits = clause["proposed_edits"]
        self.assertEqual(len(edits), 1)
        # The span action is GONE downstream: it is an ordinary replace_paragraph.
        self.assertEqual(edits[0]["action"], "replace_paragraph")
        self.assertEqual(edits[0]["paragraph_id"], "p1")
        replacement = edits[0]["text"]
        self.assertNotIn("solicit, hire, or circumvent", replacement)
        # The surrounding clean text survives.
        self.assertIn("The Receiving Party shall not", replacement)
        self.assertIn("the Disclosing Party for two years", replacement)

    def test_replace_span_lowers_to_replace_paragraph_with_substitution(self):
        assessment = _span_assessment(
            schema_version=3,
            proposed_edits=[{
                "action": "replace_span",
                "paragraph_id": "p1",
                "anchor_quote": "for two years",
                "replacement": "for one year",
            }],
        )
        cleaned = self._validate(assessment)
        edits = cleaned["non_circumvention"]["proposed_edits"]
        self.assertEqual(edits[0]["action"], "replace_paragraph")
        self.assertIn("for one year", edits[0]["text"])
        self.assertNotIn("for two years", edits[0]["text"])

    def test_unanchorable_span_degrades_to_noop_but_keeps_fail_and_flags_manual(self):
        assessment = _span_assessment(
            schema_version=3,
            proposed_edits=[{
                "action": "strike_span",
                "paragraph_id": "p1",
                "anchor_quote": "this phrase is not present in the paragraph verbatim",
            }],
        )
        cleaned = self._validate(assessment)
        clause = cleaned["non_circumvention"]
        # The fail with no realizable edit KEEPS the fail (the redline being
        # unwritable does not soften the verdict) and flags manual_redline_needed.
        self.assertEqual(clause["decision"], "fail")
        self.assertTrue(clause["manual_redline_needed"])
        self.assertFalse(clause["blocks_send"])
        self.assertEqual(clause["proposed_edits"][0]["action"], AI_REDLINE_NO_CHANGE)

    def test_span_action_rejected_under_schema_version_two(self):
        assessment = _span_assessment(
            schema_version=2,
            proposed_edits=[{
                "action": "strike_span",
                "paragraph_id": "p1",
                "anchor_quote": "solicit, hire, or circumvent",
            }],
        )
        with self.assertRaises(AIAssessmentContractError) as error:
            self._validate(assessment)
        self.assertIn("requires schema_version 3", str(error.exception))

    def test_pass_with_a_noop_edit_list_is_accepted(self):
        assessment = _span_assessment(
            decision="pass",
            issue_type="none",
            rationale="The clause is within scope and compliant.",
            evidence=[],
            schema_version=3,
            proposed_edits=[{"action": "no_change"}],
        )
        cleaned = self._validate(assessment)
        clause = cleaned["non_circumvention"]
        self.assertEqual(clause["decision"], "pass")
        self.assertFalse(any(e["action"] != AI_REDLINE_NO_CHANGE for e in clause["proposed_edits"]))

    def test_pass_with_an_actionable_edit_is_rejected(self):
        assessment = _span_assessment(
            decision="pass",
            issue_type="none",
            rationale="The clause is compliant.",
            evidence=[],
            schema_version=3,
            proposed_edits=[{
                "action": "strike_span",
                "paragraph_id": "p1",
                "anchor_quote": "solicit, hire, or circumvent",
            }],
        )
        with self.assertRaises(AIAssessmentContractError) as error:
            self._validate(assessment)
        self.assertIn("pass decisions must use proposed_redline.action no_change", str(error.exception))

    def test_clause_proposed_edits_accessor_falls_back_to_legacy_singular(self):
        # A stored v2 matter persists only proposed_redline; the accessor wraps it.
        legacy_clause = {"proposed_redline": {"action": "delete_paragraph", "paragraph_id": "p1"}}
        edits = clause_proposed_edits(legacy_clause)
        self.assertEqual(edits, [{"action": "delete_paragraph", "paragraph_id": "p1"}])
        # A clause with neither field yields an empty list (never raises).
        self.assertEqual(clause_proposed_edits({}), [])
        self.assertEqual(clause_proposed_edits(None), [])


class ApplySpanTests(unittest.TestCase):
    def test_strike_span_removes_span_and_one_connector_space(self):
        text = "The Receiving Party shall not solicit, hire, or circumvent the Disclosing Party."
        result = apply_span(text, "solicit, hire, or circumvent ", "")
        # apply_span trims a connector space; the surrounding text reads cleanly.
        self.assertNotIn("solicit, hire, or circumvent", result)
        self.assertIn("shall not the Disclosing Party.", result.replace("  ", " "))

    def test_replace_span_substitutes_preserving_surrounding_text(self):
        text = "The term is five years from the Effective Date."
        result = apply_span(text, "five years", "three years")
        self.assertEqual(result, "The term is three years from the Effective Date.")

    def test_span_anchor_matches_through_typographic_glyphs(self):
        # The paragraph uses a curly apostrophe; the anchor uses a straight one.
        text = "The Disclosing Party’s rights survive termination."
        result = apply_span(text, "Disclosing Party's rights", "obligations")
        self.assertIsNotNone(result)
        self.assertIn("obligations survive termination", result)

    def test_unfound_anchor_returns_none(self):
        text = "The Receiving Party shall keep information confidential."
        self.assertIsNone(apply_span(text, "non-existent phrase", ""))

    def test_empty_anchor_returns_none(self):
        self.assertIsNone(apply_span("any text", "", "x"))

    def test_duplicate_anchor_degrades_and_leaves_paragraph_unchanged(self):
        # A1-01: the anchor appears twice. apply_span must refuse to guess which
        # occurrence to cut (degrade to None); the paragraph stays byte-identical.
        text = "The Receiving Party shall not disclose. The Receiving Party shall not retain."
        self.assertIsNone(apply_span(text, "The Receiving Party shall not", "x"))
        # Strike form degrades identically (no arbitrary first-occurrence cut).
        self.assertIsNone(apply_span(text, "The Receiving Party shall not ", ""))

    def test_substring_inside_word_degrades_but_standalone_word_lowers(self):
        # A1-08: "compete" must NOT cut inside "competent" — degrade to None and
        # leave "competent" untouched.
        text = "The parties remain competent to perform their obligations."
        self.assertIsNone(apply_span(text, "compete", "cooperate"))
        # But "compete" as a STANDALONE word still lowers correctly.
        standalone = "The Receiving Party shall not compete with the Disclosing Party."
        result = apply_span(standalone, "compete", "cooperate")
        self.assertEqual(
            result,
            "The Receiving Party shall not cooperate with the Disclosing Party.",
        )

    def test_unique_boundary_aligned_anchor_still_lowers(self):
        # Regression: a legitimately unique, boundary-aligned anchor must still
        # lower to the correct replacement (no over-degrade from the new guards).
        text = "The term is five years from the Effective Date."
        result = apply_span(text, "five years", "three years")
        self.assertEqual(result, "The term is three years from the Effective Date.")


class ApplySpanControlBidiStripTests(unittest.TestCase):
    """A7-04: control / bidi-override / zero-width chars are stripped from the
    replacement text before it is spliced into the paragraph."""

    def test_rtl_override_stripped_from_replacement(self):
        text = "The term is five years from the Effective Date."
        # Replacement carries an RTL override (U+202E) and a pop (U+202C).
        result = apply_span(text, "five years", "three‮years‬")
        self.assertIsNotNone(result)
        self.assertNotIn("‮", result)
        self.assertNotIn("‬", result)
        self.assertIn("threeyears", result)

    def test_zero_width_chars_stripped_from_replacement(self):
        text = "The term is five years from the Effective Date."
        # ZWSP (U+200B), ZWNJ (U+200C), ZWJ (U+200D), BOM (U+FEFF) inside replacement.
        result = apply_span(text, "five years", "thr​ee‌ye‍ars﻿")
        self.assertIsNotNone(result)
        for ch in ("​", "‌", "‍", "﻿"):
            self.assertNotIn(ch, result)
        self.assertIn("threeyears", result)

    def test_ordinary_whitespace_preserved(self):
        # tabs/newlines in a replacement are legitimate redline text, not stripped.
        text = "The term is five years from the Effective Date."
        result = apply_span(text, "five years", "a\tb")
        self.assertIn("a\tb", result)


class NonStringReplacementContractTests(unittest.TestCase):
    """A6-06: the contract degrades an edit whose replacement/anchor is non-string
    rather than str()-coercing a Python repr into the document."""

    def test_dict_replacement_degrades_to_noop_with_error(self):
        from nda_automation.ai_assessment_contract import _validated_single_edit

        errors: list[str] = []
        cleaned, degraded = _validated_single_edit(
            {"action": REDLINE_REPLACE_PARAGRAPH, "paragraph_id": "p1", "replacement": {"x": 1}},
            {"p1": "Some paragraph text."},
            "clause.proposed_redline",
            errors,
            payload_version=3,
        )
        self.assertTrue(degraded)
        self.assertEqual(cleaned["action"], "no_change")
        # No repr leaked into the cleaned edit text.
        self.assertNotIn("text", cleaned)
        self.assertTrue(any("must be a string" in e for e in errors))

    def test_list_anchor_quote_degrades(self):
        from nda_automation.ai_assessment_contract import _validated_single_edit

        errors: list[str] = []
        cleaned, degraded = _validated_single_edit(
            {
                "action": "replace_span",
                "paragraph_id": "p1",
                "anchor_quote": ["not", "string"],
                "replacement": "ok",
            },
            {"p1": "Some paragraph text."},
            "clause.proposed_redline",
            errors,
            payload_version=3,
        )
        self.assertTrue(degraded)
        self.assertEqual(cleaned["action"], "no_change")


if __name__ == "__main__":
    unittest.main()
