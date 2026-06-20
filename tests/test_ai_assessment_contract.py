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

    def test_invalid_optional_ai_analysis_field_degrades_the_clause(self):
        # A malformed optional field (recommended_option missing its reason) is a
        # per-clause defect: detected and quarantined into a blocking review, not
        # raised for the whole batch.
        assessments = validate_ai_clause_assessments(
            [_valid_assessment(
                recommended_option={"option": "England and Wales"},
            )],
            valid_clause_ids=_valid_clause_ids(),
            paragraphs=_paragraphs(),
        )

        degraded = assessments["governing_law"]
        self.assertEqual(degraded["decision"], "review")
        self.assertTrue(degraded["blocks_send"])
        self.assertEqual(degraded["validation_status"], "contract_invalid")

    def test_ambiguous_quote_only_evidence_degrades_the_clause(self):
        # A quote-only evidence item that recurs in several paragraphs is genuinely
        # ambiguous. Rather than guess (or raise for the whole document) the clause
        # is quarantined into a blocking review.
        source_text = "\n\n".join([
            "Confidential Information may be disclosed by either party.",
            "Confidential Information means non-public technical information.",
        ])

        assessments = validate_ai_clause_assessments(
            [_valid_assessment(evidence=[{
                "quote": "Confidential Information",
                "relevance": "Short phrase appears more than once.",
            }])],
            valid_clause_ids=_valid_clause_ids(),
            paragraphs=split_document_paragraphs(source_text),
        )

        degraded = assessments["governing_law"]
        self.assertEqual(degraded["decision"], "review")
        self.assertTrue(degraded["blocks_send"])
        self.assertEqual(degraded["validation_status"], "contract_invalid")

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

    def test_defective_clause_degrades_to_blocking_review_not_batch_reject(self):
        # A clause with multiple genuine contract defects (empty rationale, a
        # pass/issue_type coupling violation, an ungroundable quote, no redline)
        # must NOT nuke the whole batch. It is quarantined into a SAFE blocking
        # review fallback, and the other valid clauses survive.
        bad = _valid_assessment(
            rationale="",
            issue_type="none",
            evidence=[{"paragraph_id": "p2", "quote": "laws of France", "relevance": "Wrong quote."}],
            proposed_redline={"action": AI_REDLINE_NO_CHANGE},
            blocks_send=True,
        )

        assessments = validate_ai_clause_assessments(
            [bad],
            valid_clause_ids=_valid_clause_ids(),
            paragraphs=_paragraphs(),
        )

        degraded = assessments["governing_law"]
        # NEVER a silent pass: a malformed-but-hallucinated clause fails safe.
        self.assertEqual(degraded["decision"], "review")
        self.assertNotEqual(degraded["issue_type"], "none")
        self.assertTrue(degraded["blocks_send"])
        self.assertTrue(degraded["manual_redline_needed"])
        self.assertEqual(degraded["validation_status"], "contract_invalid")
        self.assertEqual(degraded["evidence"], [])
        self.assertEqual(degraded["proposed_redline"]["action"], AI_REDLINE_NO_CHANGE)
        self.assertEqual(degraded["reason_code"], "ai_contract_invalid_clause")

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

    def test_invented_paragraph_id_degrades_clause_to_blocking_review(self):
        # A paragraph_id the model invented (points at no reviewed paragraph) is a
        # per-clause structural defect. It must degrade THAT clause to a safe
        # blocking review, not discard the whole document's review.
        ghost = _valid_assessment(evidence=[{
            "paragraph_id": "p999",
            "quote": "laws of california",
            "relevance": "Cites a paragraph that does not exist.",
        }])

        assessments = validate_ai_clause_assessments(
            [ghost],
            valid_clause_ids=_valid_clause_ids(),
            paragraphs=_paragraphs(),
        )

        degraded = assessments["governing_law"]
        self.assertEqual(degraded["decision"], "review")
        self.assertTrue(degraded["blocks_send"])
        self.assertEqual(degraded["validation_status"], "contract_invalid")

    def test_duplicate_clause_keeps_first_and_unknown_clause_is_dropped(self):
        # A duplicate assessment is a per-clause defect: the first (valid) one wins
        # and the duplicate is dropped. An unknown clause_id maps to no playbook
        # clause and is also dropped. NEITHER nukes the batch -- the one valid
        # governing_law assessment survives.
        assessments = validate_ai_clause_assessments(
            [_valid_assessment(), _valid_assessment(), _valid_assessment(clause_id="unknown_clause")],
            valid_clause_ids=_valid_clause_ids(),
            paragraphs=_paragraphs(),
            playbook_clauses_by_id=_playbook_clauses_by_id(),
        )

        # The first valid governing_law assessment is kept (a real fail, NOT the
        # contract-invalid fallback); the duplicate and the unknown clause vanish.
        self.assertIn("governing_law", assessments)
        self.assertEqual(assessments["governing_law"]["decision"], "fail")
        self.assertNotEqual(
            assessments["governing_law"]["validation_status"], "contract_invalid"
        )
        self.assertNotIn("unknown_clause", assessments)

    def test_non_list_response_is_a_batch_level_reject(self):
        # A genuine STRUCTURAL failure (the whole response is not a list) still
        # rejects -- the degrade path is per-clause, not a blanket suppressor.
        with self.assertRaises(AIAssessmentContractError) as error:
            validate_ai_clause_assessments(
                {"clause_id": "governing_law"},  # a dict, not a list of clauses
                valid_clause_ids=_valid_clause_ids(),
                paragraphs=_paragraphs(),
            )
        self.assertIn("assessments must be a list", str(error.exception))

    def _absence_pass(self, clause_id):
        return {
            "clause_id": clause_id,
            "decision": "pass",
            "issue_type": "none",
            "rationale": (
                f"No issue with {clause_id} appears in the supplied text, so this "
                "clause passes on absence with no redline required."
            ),
            "evidence": [],
            "proposed_redline": {"action": AI_REDLINE_NO_CHANGE},
            "confidence": 0.8,
            "blocks_send": False,
        }

    def test_one_bad_clause_in_a_full_batch_keeps_every_other_valid_clause(self):
        # NON-VACUITY: a multi-clause batch with exactly ONE clause carrying an
        # invented paragraph_id would, on the pre-fix code, RAISE and discard
        # ZERO clauses. After the fix it returns all the OTHER valid clauses PLUS
        # the one offender degraded to a blocking review.
        valid_ids = ["mutuality", "confidential_information", "term_and_survival", "non_circumvention", "signatures"]
        batch = [self._absence_pass(clause_id) for clause_id in valid_ids]
        offender = _valid_assessment(evidence=[{
            "paragraph_id": "p999",  # invented: points at no reviewed paragraph
            "quote": "laws of california",
            "relevance": "Cites a paragraph that does not exist.",
        }])
        batch.append(offender)

        assessments = validate_ai_clause_assessments(
            batch,
            valid_clause_ids=_valid_clause_ids(),
            paragraphs=_paragraphs(),
            playbook_clauses_by_id=_playbook_clauses_by_id(),
        )

        # All six clauses are present: the five valid passes survive untouched...
        self.assertEqual(set(assessments), set(valid_ids) | {"governing_law"})
        for clause_id in valid_ids:
            self.assertEqual(assessments[clause_id]["decision"], "pass")
            self.assertNotEqual(
                assessments[clause_id]["validation_status"], "contract_invalid"
            )
        # ...and the single offender is degraded to a SEND-BLOCKING review, never a
        # silent pass, so the document is still gated by it.
        degraded = assessments["governing_law"]
        self.assertEqual(degraded["decision"], "review")
        self.assertTrue(degraded["blocks_send"])
        self.assertEqual(degraded["validation_status"], "contract_invalid")

    def test_each_per_clause_defect_mode_degrades_instead_of_raising(self):
        # Each listed per-clause defect, in isolation, degrades the clause to a
        # blocking review rather than raising for the whole batch.
        cases = {
            # invented paragraph_id
            "invented_paragraph_id": _valid_assessment(evidence=[{
                "paragraph_id": "p999",
                "quote": "laws of california",
                "relevance": "Cites a paragraph that does not exist.",
            }]),
            # fabricated quote (grounds nowhere) leaves a FAIL with no evidence
            "fabricated_quote": _valid_assessment(evidence=[{
                "quote": "this exact phrase does not appear anywhere in the document at all",
                "relevance": "Fabricated.",
            }]),
            # pass with a non-none issue_type (coupling violation)
            "pass_with_issue_type": _valid_assessment(
                decision="pass",
                issue_type="present_but_wrong",
                proposed_redline={"action": AI_REDLINE_NO_CHANGE},
                blocks_send=False,
            ),
            # unsupported field
            "unsupported_field": _valid_assessment(totally_unknown_field="x"),
        }
        for label, offender in cases.items():
            with self.subTest(defect=label):
                assessments = validate_ai_clause_assessments(
                    [offender],
                    valid_clause_ids=_valid_clause_ids(),
                    paragraphs=_paragraphs(),
                    playbook_clauses_by_id=_playbook_clauses_by_id(),
                )
                degraded = assessments["governing_law"]
                self.assertEqual(degraded["decision"], "review", label)
                self.assertNotEqual(degraded["issue_type"], "none", label)
                self.assertTrue(degraded["blocks_send"], label)
                self.assertEqual(
                    degraded["validation_status"], "contract_invalid", label
                )


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

    def test_span_action_under_schema_version_two_degrades_the_clause(self):
        assessment = _span_assessment(
            schema_version=2,
            proposed_edits=[{
                "action": "strike_span",
                "paragraph_id": "p1",
                "anchor_quote": "solicit, hire, or circumvent",
            }],
        )
        # A v3-only span action on a v2 payload is a per-clause defect: it degrades
        # this clause to a blocking review rather than rejecting the whole batch.
        cleaned = self._validate(assessment)
        degraded = cleaned["non_circumvention"]
        self.assertEqual(degraded["decision"], "review")
        self.assertTrue(degraded["blocks_send"])
        self.assertEqual(degraded["validation_status"], "contract_invalid")

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

    def test_pass_with_an_actionable_edit_degrades_the_clause(self):
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
        # A pass that nonetheless carries an actionable edit is a coupling defect.
        # It must degrade to a blocking review -- crucially NOT stay a pass, so a
        # malformed "clean" verdict can never clear the gate.
        cleaned = self._validate(assessment)
        degraded = cleaned["non_circumvention"]
        self.assertEqual(degraded["decision"], "review")
        self.assertTrue(degraded["blocks_send"])
        self.assertEqual(degraded["validation_status"], "contract_invalid")

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


# ---------------------------------------------------------------------------
# blocks_send polarity auto-correction
#
# ``blocks_send`` is a DERIVED bookkeeping field (the rule is
# ``blocks_send == (decision == review)``), not an independent verdict. A real
# model routinely FAILS a clause and also ticks "stop sending", which used to be a
# FATAL contract error that rejected the WHOLE batch -- one such clause discarded
# the review of every other clause. These tests prove the mismatch is now
# auto-corrected in place (the clause keeps its verdict, the field is reconciled to
# the rule and the clause is stamped) while the rest of the batch is preserved, and
# that genuine malformations still reject exactly as before.
# ---------------------------------------------------------------------------

# A 30-paragraph synthetic document. Paragraph p1 holds an overreaching restraint
# the one FAIL clause targets; the rest are innocuous so the PASS clauses ground
# trivially (PASS clauses carry no evidence).
_BATCH_PARAGRAPH_TEXTS = [
    "The Receiving Party shall not solicit or hire any employee of the Disclosing Party for five years."
] + [f"Section {n}. Standard mutual confidentiality boilerplate paragraph number {n}." for n in range(2, 31)]
_BATCH_SOURCE_TEXT = "\n\n".join(_BATCH_PARAGRAPH_TEXTS)


def _batch_paragraphs():
    return split_document_paragraphs(_BATCH_SOURCE_TEXT)


def _batch_clause_ids(count):
    return [f"clause_{index:02d}" for index in range(count)]


def _pass_clause(clause_id):
    return {
        "clause_id": clause_id,
        "decision": "pass",
        "issue_type": "none",
        "rationale": (
            "This clause meets the playbook requirements and is acceptable as drafted "
            "for a standard mutual confidentiality agreement."
        ),
        "evidence": [],
        "proposed_redline": {"action": AI_REDLINE_NO_CHANGE},
        "confidence": 0.91,
        "blocks_send": False,
    }


def _failing_clause_blocks_send_true(clause_id):
    # The natural real-model shape: a clause is FAILED and "stop sending" is ALSO
    # ticked. issue_type=missing exempts it from the evidence requirement; a
    # delete_paragraph is the actionable redline a prohibited restraint needs.
    return {
        "clause_id": clause_id,
        "decision": "fail",
        "issue_type": "missing",
        "rationale": (
            "The clause imposes a prohibited five-year non-solicit restraint that is outside "
            "the approved confidentiality scope and must be struck before this can be sent."
        ),
        "evidence": [],
        "proposed_redline": {"action": "delete_paragraph", "paragraph_id": "p1"},
        "confidence": 0.97,
        # The polarity mismatch under test: a FAIL with blocks_send ticked True.
        "blocks_send": True,
    }


class BlocksSendPolarityAutoCorrectTests(unittest.TestCase):
    def test_one_fail_with_blocks_send_true_does_not_reject_the_whole_batch(self):
        # ~30 clauses; exactly one is FAIL + blocks_send=True (the rest PASS). On
        # PRISTINE main this RAISES and returns ZERO clauses; after the fix the whole
        # batch survives, the offending clause stays FAIL, and it is flagged
        # auto-corrected.
        clause_ids = _batch_clause_ids(30)
        offending_id = clause_ids[7]
        assessments = []
        for clause_id in clause_ids:
            if clause_id == offending_id:
                assessments.append(_failing_clause_blocks_send_true(clause_id))
            else:
                assessments.append(_pass_clause(clause_id))

        cleaned = validate_ai_clause_assessments(
            assessments,
            valid_clause_ids=clause_ids,
            paragraphs=_batch_paragraphs(),
        )

        # The entire batch is preserved -- nothing was discarded.
        self.assertEqual(len(cleaned), 30)
        self.assertEqual(set(cleaned), set(clause_ids))

        offending = cleaned[offending_id]
        # The verdict is UNCHANGED: a FAIL stays a FAIL.
        self.assertEqual(offending["decision"], "fail")
        # The bookkeeping field is reconciled to the rule (a FAIL is not a review).
        self.assertFalse(offending["blocks_send"])
        # The correction is visible in the audit trail.
        self.assertEqual(offending["validation_status"], "blocks_send_autocorrected")

        # Every other (PASS) clause is untouched and keeps the default status.
        for clause_id in clause_ids:
            if clause_id == offending_id:
                continue
            self.assertEqual(cleaned[clause_id]["decision"], "pass")
            self.assertEqual(cleaned[clause_id]["validation_status"], "contract_valid")

    def test_document_stays_send_blocked_after_autocorrect(self):
        # Downstream neutrality: un-ticking the per-clause blocks_send must NOT make
        # a failing document sendable. The deterministic floor (a FAIL -> CHECK
        # state) blocks auto-send regardless of the AI's blocks_send field.
        from nda_automation.review_state import aggregate_review_state, clause_review_state

        clause_ids = _batch_clause_ids(30)
        offending_id = clause_ids[7]
        assessments = [
            _failing_clause_blocks_send_true(clause_id) if clause_id == offending_id else _pass_clause(clause_id)
            for clause_id in clause_ids
        ]
        cleaned = validate_ai_clause_assessments(
            assessments, valid_clause_ids=clause_ids, paragraphs=_batch_paragraphs()
        )

        # review_state RE-DERIVES blocks_send / blocks_auto_send from the verdict, not
        # from the (now-corrected) AI field: a FAIL still blocks send.
        offending_state = clause_review_state({"decision": cleaned[offending_id]["decision"]})
        self.assertTrue(offending_state["blocks_auto_send"])

        # And the aggregate document state is CHECK (a failing document), which the
        # workflow treats as not auto-sendable.
        clause_states = [{"decision": cleaned[clause_id]["decision"]} for clause_id in clause_ids]
        aggregate = aggregate_review_state(clause_states)
        self.assertEqual(aggregate["state"], "check")

    def test_review_with_blocks_send_false_is_auto_corrected_to_true(self):
        # The OTHER polarity: a REVIEW that forgot to tick blocks_send. The rule is
        # reconciled the same way (now True), and the clause is flagged.
        clause_ids = _batch_clause_ids(3)
        review_clause = {
            "clause_id": clause_ids[1],
            "decision": "review",
            "issue_type": "unclear",
            "rationale": (
                "The clause is ambiguous about which party bears the confidentiality "
                "obligation and a human reviewer should resolve it before sending."
            ),
            "evidence": [],
            "proposed_redline": {"action": AI_REDLINE_NO_CHANGE},
            "confidence": 0.55,
            # Polarity mismatch in the other direction: a REVIEW with blocks_send False.
            "blocks_send": False,
        }
        assessments = [
            _pass_clause(clause_ids[0]),
            review_clause,
            _pass_clause(clause_ids[2]),
        ]

        cleaned = validate_ai_clause_assessments(
            assessments, valid_clause_ids=clause_ids, paragraphs=_batch_paragraphs()
        )

        self.assertEqual(len(cleaned), 3)
        corrected = cleaned[clause_ids[1]]
        self.assertEqual(corrected["decision"], "review")
        # Reconciled to the rule: a review DOES block send.
        self.assertTrue(corrected["blocks_send"])
        self.assertEqual(corrected["validation_status"], "blocks_send_autocorrected")

    def test_autocorrect_bumps_telemetry_counter(self):
        # Each auto-corrected clause bumps the dedicated telemetry counter exactly
        # once, so operators can see how often real models trip the polarity rule.
        from nda_automation import telemetry
        from nda_automation.ai_assessment_contract import (
            AI_ASSESSMENT_BLOCKS_SEND_AUTOCORRECTED_COUNTER,
        )

        telemetry.reset()
        clause_ids = _batch_clause_ids(5)
        offending_id = clause_ids[2]
        assessments = [
            _failing_clause_blocks_send_true(clause_id) if clause_id == offending_id else _pass_clause(clause_id)
            for clause_id in clause_ids
        ]
        validate_ai_clause_assessments(
            assessments, valid_clause_ids=clause_ids, paragraphs=_batch_paragraphs()
        )
        counters = telemetry.snapshot()["counters"]
        self.assertEqual(counters.get(AI_ASSESSMENT_BLOCKS_SEND_AUTOCORRECTED_COUNTER), 1)

    def test_correctly_aligned_blocks_send_is_not_flagged_or_counted(self):
        # A clause whose blocks_send already matches the rule is NOT touched: no
        # auto-correct status, no counter bump.
        from nda_automation import telemetry
        from nda_automation.ai_assessment_contract import (
            AI_ASSESSMENT_BLOCKS_SEND_AUTOCORRECTED_COUNTER,
        )

        telemetry.reset()
        clause_ids = _batch_clause_ids(2)
        assessments = [_pass_clause(clause_id) for clause_id in clause_ids]  # pass + blocks_send False == rule
        cleaned = validate_ai_clause_assessments(
            assessments, valid_clause_ids=clause_ids, paragraphs=_batch_paragraphs()
        )
        for clause_id in clause_ids:
            self.assertEqual(cleaned[clause_id]["validation_status"], "contract_valid")
        counters = telemetry.snapshot()["counters"]
        self.assertNotIn(AI_ASSESSMENT_BLOCKS_SEND_AUTOCORRECTED_COUNTER, counters)

    def test_genuine_malformation_degrades_clause_and_keeps_the_rest(self):
        # The validator did NOT become permissive: a clause with an INVALID decision
        # enum is a genuine malformation. Under the per-clause degrade contract it is
        # QUARANTINED into a SAFE blocking review (NEVER a silent pass) while the
        # other clauses survive -- not rejected as a whole batch, but not loosened
        # into a clean pass either.
        clause_ids = _batch_clause_ids(3)
        bad_decision = {
            "clause_id": clause_ids[1],
            "decision": "definitely_not_a_real_decision",
            "issue_type": "present_but_wrong",
            "rationale": "This clause has an invalid decision enum value and must be rejected.",
            "evidence": [],
            "proposed_redline": {"action": AI_REDLINE_NO_CHANGE},
            "confidence": 0.8,
            "blocks_send": False,
        }
        assessments = [_pass_clause(clause_ids[0]), bad_decision, _pass_clause(clause_ids[2])]

        cleaned = validate_ai_clause_assessments(
            assessments, valid_clause_ids=clause_ids, paragraphs=_batch_paragraphs()
        )

        self.assertEqual(set(cleaned), set(clause_ids))
        degraded = cleaned[clause_ids[1]]
        self.assertEqual(degraded["decision"], "review")
        self.assertTrue(degraded["blocks_send"])
        self.assertEqual(degraded["validation_status"], "contract_invalid")
        # The two well-formed passes are untouched.
        self.assertEqual(cleaned[clause_ids[0]]["decision"], "pass")
        self.assertEqual(cleaned[clause_ids[2]]["decision"], "pass")

    def test_missing_required_field_degrades_and_is_not_rescued_by_blocks_send(self):
        # A second malformation flavour: a missing REQUIRED field (rationale)
        # degrades the clause to a blocking review even when that same clause ALSO
        # has a blocks_send polarity mismatch. The polarity auto-correct must NOT
        # "rescue" the malformed clause into a kept, contract-valid result.
        clause_ids = _batch_clause_ids(3)
        missing_rationale = {
            "clause_id": clause_ids[1],
            "decision": "fail",
            "issue_type": "missing",
            # rationale deliberately omitted (required field).
            "evidence": [],
            "proposed_redline": {"action": "delete_paragraph", "paragraph_id": "p1"},
            "confidence": 0.8,
            "blocks_send": True,  # also a polarity mismatch; must NOT rescue the clause
        }
        assessments = [_pass_clause(clause_ids[0]), missing_rationale, _pass_clause(clause_ids[2])]

        cleaned = validate_ai_clause_assessments(
            assessments, valid_clause_ids=clause_ids, paragraphs=_batch_paragraphs()
        )

        degraded = cleaned[clause_ids[1]]
        # Quarantined, NOT rescued: the status is the contract-invalid fallback, NOT
        # the blocks_send auto-correct "success" status.
        self.assertEqual(degraded["validation_status"], "contract_invalid")
        self.assertNotEqual(degraded["validation_status"], "blocks_send_autocorrected")
        self.assertEqual(degraded["decision"], "review")
        self.assertTrue(degraded["blocks_send"])
        # The other clauses survive untouched.
        self.assertEqual(cleaned[clause_ids[0]]["decision"], "pass")
        self.assertEqual(cleaned[clause_ids[2]]["decision"], "pass")


if __name__ == "__main__":
    unittest.main()
