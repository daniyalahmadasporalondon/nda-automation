"""REAL-PATH adversarial eval for the AI REVIEWER (key-gated, default-off).

Why this exists
---------------
The VERIFIER has a real-path eval (``tests/verifier_real_eval.py``) that drives
the live model over adversarial findings. The REVIEWER -- the primary judgment
the product ships, ``ai_assessor.assess_nda_with_ai`` driving the ``ai_first``
path with the prompt in ``ai_assessment_prompt.py`` -- has NONE. Every existing
reviewer test injects a stub (``InMemoryAssessmentReviewer`` / the
``NDA_AI_ASSESSMENT_STUB`` seam) that echoes a hand-written verdict. Those
exercise the plumbing (the contract validation, the grounding downgrade, the
review_result assembly) but NEVER the real model's judgment: a regression in the
assessment prompt, the model, or the provider routing would sail straight
through to production.

This harness closes that gap. It drives the ACTUAL
``OpenRouterAIAssessmentReviewer`` (the shipping reviewer resolved from the
configured key) over adversarial single-clause NDAs through the real
``assess_nda_with_ai`` seam -- the same function the shipping AI-first path
calls. Each case targets ONE playbook clause and one known trap, and asserts the
reviewer does NOT land in a ``forbidden_decisions`` bucket (e.g. an unapproved
governing law must NOT be ``pass``). It does NOT pin the model to one exact
verdict string where both ``fail`` and ``review`` are defensible -- the safe
answer space is "don't false-clear a real problem", which often spans both.

Cost control
------------
Every case hits a live provider, so the whole layer is gated behind
``NDA_RUN_REAL_REVIEWER_EVAL`` (default OFF) AND the presence of an OpenRouter
key, mirroring the verifier eval exactly. Key-free / flag-free CI skips it
cleanly. It is small (one model call per case) and deliberately not part of the
always-on gate.

The clauses + traps (one adversarial case per clause)
-----------------------------------------------------
1. CONFIDENTIAL_INFORMATION -- DOUBLE-NEGATION / POLARITY carve-out: a
   confidentiality definition whose carve-outs are phrased so a naive read flips
   the polarity ("Information shall not fail to be treated as non-confidential
   unless ...") effectively gutting protection. The reviewer must not clear a
   gutted definition.
2. MUTUALITY -- ONE-WAY trap: only the Recipient is bound; the disclosing party
   has no reciprocal obligation. A one-way NDA must not pass the mutuality clause.
3. GOVERNING_LAW -- UNAPPROVED law: the agreement is governed by the laws of
   California (outside the approved set). An unapproved governing law must not
   pass.
4. GOVERNING_LAW (recital vs operative) -- a party is "incorporated under the
   laws of Delaware" (a benign recital) while the operative governing-law clause
   names an APPROVED law. The reviewer must not FAIL on the incorporation
   recital (precision: recital is not the operative choice).
5. GOVERNING_LAW (law/forum split) -- the operative governing law is approved
   (England and Wales) but the forum/jurisdiction clause sends disputes to a
   DIFFERENT, non-approved venue (New York courts). A reviewer must not blindly
   clear a law/forum split.
6. TERM_AND_SURVIVAL -- OVER-CAP survival: confidentiality obligations survive
   for fifteen (15) years, well past the playbook cap. An over-cap term must not
   pass.
7. NON_CIRCUMVENTION -- prohibited restriction present: a hard introduced-party
   non-solicit. A present prohibited restriction must not pass.
8. SIGNATURES (present + complete) -- a fully-formed mutual signature block.
   The reviewer must not FAIL a present, complete signature block (precision;
   signatures has ZERO automated coverage today).
9. SIGNATURES (missing) -- an NDA body with NO signature block at all. A missing
   signature block must not pass.
10. CONFIDENTIAL_INFORMATION -- CARVE-OUT NEGATION: the CI exclusions sit intact
    on the page but a "notwithstanding ... the exclusions shall not apply" clause
    cancels them. The reviewer must not clear the gutted protection.
11. CONFIDENTIAL_INFORMATION -- INCORPORATION/SUBORDINATION OVERRIDE: the NDA is
    made "subject to" an external Master Services Agreement that "shall prevail in
    the event of any conflict". The reviewer must not clear a silently-overridable
    protection.
12. CONFIDENTIAL_INFORMATION -- POISONED DEFINITION: the definition affirmatively
    INCLUDES publicly-available / already-known / independently-developed
    information. The reviewer must not clear a definition that swallows its own
    exclusions.
13. NON_CIRCUMVENTION -- OVER-BROAD AFFILIATE breadth: an "Affiliate" definition
    drawn beyond corporate control sweeps in arbitrary non-parties/competitors,
    and a non-solicit restraint leans on it. The reviewer must not clear the
    restraint. (No playbook clause owns definition breadth -- the hardest trap.)

Cases 10-13 are the structural-override traps the deterministic review overlays
(notwithstanding_check / incorporation_check / definition_poison_check) catch
today. They are the evidence gate that must go GREEN on the live reviewer BEFORE
any overlay is retired (north star: no deterministic layer in review).

How to run
----------
    # one-shot, with a real key in the environment:
    NDA_RUN_REAL_REVIEWER_EVAL=1 OPENROUTER_API_KEY=sk-... \
        PYTHONPATH=. python -m tests.reviewer_real_eval

    # via pytest (skips cleanly when the flag/key are absent):
    NDA_RUN_REAL_REVIEWER_EVAL=1 OPENROUTER_API_KEY=sk-... \
        pytest tests/test_reviewer_real_eval.py -v

Without the flag (or without a key) the module reports SKIPPED and the pytest
gate is a no-op skip, so the default suite stays green and free.
"""
from __future__ import annotations

import os
from typing import Dict, List, Mapping, Sequence

from nda_automation.ai_assessor import (
    AIAssessorError,
    OpenRouterAIAssessmentReviewer,
    assess_nda_with_ai,
)
from nda_automation.ai_review import (
    AI_REVIEW_ENV_MODEL,
    AI_REVIEW_ENV_TIMEOUT,
    DEFAULT_AI_TIMEOUT_SECONDS,
    DEFAULT_OPENROUTER_MODEL,
)
from nda_automation.review_state import (
    CLAUSE_DECISION_FAIL,
    CLAUSE_DECISION_PASS,
)

# Master flag: the real-path layer only runs when this is truthy AND a key is
# present. Default OFF so CI never spends tokens unless a deploy opts in.
REAL_REVIEWER_EVAL_ENV = "NDA_RUN_REAL_REVIEWER_EVAL"
# The reviewer's transport resolves its key from the same OpenRouter env the rest
# of the AI stack uses (see ai_review._configured_api_key).
OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"

_TRUTHY = {"1", "true", "yes", "on"}


def _flag_enabled() -> bool:
    return str(os.environ.get(REAL_REVIEWER_EVAL_ENV, "")).strip().lower() in _TRUTHY


def _key_present() -> bool:
    return bool(str(os.environ.get(OPENROUTER_API_KEY_ENV, "")).strip())


def real_reviewer_eval_enabled() -> bool:
    """The real-path layer runs only when explicitly flagged AND keyed."""
    return _flag_enabled() and _key_present()


def skip_reason() -> str:
    """Human-readable reason the layer is skipped, or "" when it will run."""
    if not _flag_enabled():
        return f"{REAL_REVIEWER_EVAL_ENV} is not set (default-off real-AI reviewer eval)"
    if not _key_present():
        return f"{OPENROUTER_API_KEY_ENV} is not configured; cannot reach the real reviewer"
    return ""


def resolve_real_reviewer() -> OpenRouterAIAssessmentReviewer:
    """Build the ACTUAL OpenRouter reviewer from the configured key.

    Honours the same model/timeout overrides the shipping resolver reads, so this
    exercises exactly the reviewer prod would run. Raises ``AIAssessorError`` when
    no key is present (callers gate on ``real_reviewer_eval_enabled`` first).
    """
    api_key = str(os.environ.get(OPENROUTER_API_KEY_ENV, "")).strip()
    model = str(os.environ.get(AI_REVIEW_ENV_MODEL, "")).strip() or DEFAULT_OPENROUTER_MODEL
    timeout_raw = str(os.environ.get(AI_REVIEW_ENV_TIMEOUT, "")).strip()
    try:
        timeout = max(1, int(float(timeout_raw))) if timeout_raw else DEFAULT_AI_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        timeout = DEFAULT_AI_TIMEOUT_SECONDS
    return OpenRouterAIAssessmentReviewer(api_key=api_key, model=model, timeout_seconds=timeout)


# --- Adversarial single-clause NDAs ---------------------------------------
#
# Each case is a small, self-contained NDA body engineered so that the SAFE
# verdict for the TARGET clause is an escalation (fail or review) -- except the
# two precision cases (recital-vs-operative governing law, present-and-complete
# signatures) where a wrongful FAIL is the regression we guard. The reviewer
# sees the whole document (the real packet), but each case asserts only on the
# decision of its target clause, so unrelated clauses' verdicts don't matter.
#
# A small reusable, fully-compliant signature block so cases that are NOT about
# signatures don't trip the signatures clause into noise (and so the document
# reads like a real NDA).
_SIGNATURE_BLOCK = (
    "IN WITNESS WHEREOF, the parties have executed this Agreement.\n"
    "For Aspora Limited\nBy: _______________  Name: ____________  Title: Director  Date: __________\n"
    "For Counterparty Ltd\nBy: _______________  Name: ____________  Title: Director  Date: __________"
)


def _doc(*paragraphs: str) -> str:
    return "\n\n".join(paragraph for paragraph in paragraphs if paragraph)


# ---- Case 1: confidential_information double-negation / polarity carve-out ----
# The definition is buried under stacked negations that, read literally, treat
# almost everything as NON-confidential -- gutting the protection. A reviewer
# must not clear a definition this broken.
_CI_DOUBLE_NEGATION = _doc(
    "1. Mutual Disclosure. Each party may disclose Confidential Information to the "
    "other party under this Agreement, and each is bound by the obligations herein.",
    '2. Confidential Information. "Confidential Information" shall not fail to be '
    "treated as non-confidential unless the disclosing party can prove it was not "
    "omitted from being marked as not non-public; provided that no information shall "
    "be deemed confidential where it is not the case that it was not already known.",
    "3. Governing Law. This Agreement shall be governed by the laws of England and Wales.",
    "4. Term. The confidentiality obligations survive for a fixed period of three (3) years.",
    "5. No Restriction. Each party remains free to deal with third parties outside the Purpose.",
    _SIGNATURE_BLOCK,
)

# ---- Case 2: mutuality one-way trap ----
# Only the Recipient is bound; the Disclosing Party owes nothing. A one-way NDA
# must not pass the mutuality clause.
_MUTUALITY_ONE_WAY = _doc(
    "1. Obligations. The Recipient shall hold the Disclosing Party's Confidential "
    "Information in strict confidence and shall not disclose it. The Disclosing Party "
    "has no obligation of confidentiality with respect to any information of the "
    "Recipient, and this Agreement imposes obligations on the Recipient alone.",
    '2. Confidential Information. "Confidential Information" means all non-public '
    "business, financial, technical, customer, supplier, pricing and trade secret "
    "information disclosed by the Disclosing Party to the Recipient.",
    "3. Governing Law. This Agreement shall be governed by the laws of England and Wales.",
    "4. Term. The confidentiality obligations survive for a fixed period of three (3) years.",
    _SIGNATURE_BLOCK,
)

# ---- Case 3: governing_law unapproved law ----
_GOVLAW_UNAPPROVED = _doc(
    "1. Mutual Disclosure. Each party may disclose Confidential Information to the "
    "other party under this Agreement and is bound reciprocally.",
    '2. Confidential Information. "Confidential Information" means all non-public '
    "business, financial, technical and trade secret information disclosed by either "
    "party.",
    "3. Governing Law. This Agreement and any dispute arising out of it shall be "
    "governed by and construed in accordance with the laws of the State of California, "
    "and the parties submit to the exclusive jurisdiction of the courts of California.",
    "4. Term. The confidentiality obligations survive for a fixed period of three (3) years.",
    _SIGNATURE_BLOCK,
)

# ---- Case 4: governing_law incorporation-recital vs operative (precision) ----
# A party is incorporated under Delaware law (a benign recital). The OPERATIVE
# governing-law clause names an APPROVED law (England and Wales). The reviewer
# must NOT read the recital as the governing-law choice and fail the clause.
_GOVLAW_RECITAL = _doc(
    "1. Parties. This Agreement is between Aspora Limited and Acme Inc., a company "
    "incorporated under the laws of the State of Delaware with its principal office "
    "in San Francisco.",
    "2. Mutual Disclosure. Each party may disclose Confidential Information to the "
    "other party under this Agreement and is bound reciprocally.",
    '3. Confidential Information. "Confidential Information" means all non-public '
    "business, financial, technical and trade secret information disclosed by either "
    "party.",
    "4. Governing Law. This Agreement shall be governed by and construed in accordance "
    "with the laws of England and Wales, and the parties submit to the exclusive "
    "jurisdiction of the courts of England and Wales.",
    "5. Term. The confidentiality obligations survive for a fixed period of three (3) years.",
    _SIGNATURE_BLOCK,
)

# ---- Case 5: governing_law law/forum split ----
# Approved governing law (England and Wales) but disputes are sent to a
# DIFFERENT, non-approved forum (New York courts). The reviewer should not
# blindly clear a law/forum split.
_GOVLAW_FORUM_SPLIT = _doc(
    "1. Mutual Disclosure. Each party may disclose Confidential Information to the "
    "other party under this Agreement and is bound reciprocally.",
    '2. Confidential Information. "Confidential Information" means all non-public '
    "business, financial, technical and trade secret information disclosed by either "
    "party.",
    "3. Governing Law. This Agreement shall be governed by and construed in accordance "
    "with the laws of England and Wales; provided, however, that the parties "
    "irrevocably submit to the exclusive jurisdiction of the state and federal courts "
    "located in New York, New York for any dispute arising under this Agreement.",
    "4. Term. The confidentiality obligations survive for a fixed period of three (3) years.",
    _SIGNATURE_BLOCK,
)

# ---- Case 6: term_and_survival over-cap survival ----
_TERM_OVER_CAP = _doc(
    "1. Mutual Disclosure. Each party may disclose Confidential Information to the "
    "other party under this Agreement and is bound reciprocally.",
    '2. Confidential Information. "Confidential Information" means all non-public '
    "business, financial, technical and trade secret information disclosed by either "
    "party.",
    "3. Governing Law. This Agreement shall be governed by the laws of England and Wales.",
    "4. Term and Survival. The confidentiality obligations under this Agreement shall "
    "survive the termination or expiry of this Agreement and shall continue in full "
    "force and effect for a period of fifteen (15) years thereafter.",
    _SIGNATURE_BLOCK,
)

# ---- Case 7: non_circumvention prohibited restriction present ----
_NON_CIRC_PRESENT = _doc(
    "1. Mutual Disclosure. Each party may disclose Confidential Information to the "
    "other party under this Agreement and is bound reciprocally.",
    '2. Confidential Information. "Confidential Information" means all non-public '
    "business, financial, technical and trade secret information disclosed by either "
    "party.",
    "3. Non-Circumvention. For a period of two (2) years following any introduction, "
    "the Recipient shall not, directly or indirectly, solicit for employment, hire, or "
    "otherwise engage any employee, contractor or business contact that the Disclosing "
    "Party introduced to the Recipient, nor circumvent the Disclosing Party to deal "
    "directly with any such introduced party.",
    "4. Governing Law. This Agreement shall be governed by the laws of England and Wales.",
    "5. Term. The confidentiality obligations survive for a fixed period of three (3) years.",
    _SIGNATURE_BLOCK,
)

# ---- Case 8: signatures present + complete (precision) ----
# A fully-formed mutual signature block. The reviewer must not FAIL a present,
# complete signature block.
_SIGNATURES_COMPLETE = _doc(
    "1. Mutual Disclosure. Each party may disclose Confidential Information to the "
    "other party under this Agreement and is bound reciprocally.",
    '2. Confidential Information. "Confidential Information" means all non-public '
    "business, financial, technical and trade secret information disclosed by either "
    "party.",
    "3. Governing Law. This Agreement shall be governed by the laws of England and Wales.",
    "4. Term. The confidentiality obligations survive for a fixed period of three (3) years.",
    _SIGNATURE_BLOCK,
)

# ---- Case 9: signatures missing ----
# A complete NDA body with NO signature block at all. A missing signature block
# must not pass the signatures clause.
_SIGNATURES_MISSING = _doc(
    "1. Mutual Disclosure. Each party may disclose Confidential Information to the "
    "other party under this Agreement and is bound reciprocally.",
    '2. Confidential Information. "Confidential Information" means all non-public '
    "business, financial, technical and trade secret information disclosed by either "
    "party.",
    "3. Governing Law. This Agreement shall be governed by the laws of England and Wales.",
    "4. Term. The confidentiality obligations survive for a fixed period of three (3) years.",
    "5. Entire Agreement. This document constitutes the entire agreement between the "
    "parties and supersedes all prior understandings.",
)


# ---- Cases 10-13: STRUCTURAL-OVERRIDE traps (the retiring-overlay coverage) ----
#
# These four traps are exactly the ones the deterministic review overlays
# (notwithstanding_check / incorporation_check / definition_poison_check) catch
# today by ELEVATING a clean AI pass to review. They are cross-clause /
# whole-document attacks that a naive clause-by-clause read can miss, so they are
# the evidence gate that must go GREEN on the live reviewer BEFORE any overlay is
# retired (north star: no deterministic layer in review). Each asserts the
# reviewer must NOT clear (pass) the undermined confidential_information clause on
# its own. A review or a fail both count as "caught"; only a pass is unsafe.

# ---- Case 10: carve-out negation (notwithstanding_check trap) ----
# The CI exclusions sit intact on the page (Section 2) but a "notwithstanding"
# clause (Section 1.2) cancels them under a condition. The protection is wider
# than it reads; the reviewer must not clear CI.
_CARVEOUT_NEGATION = _doc(
    '1. Confidential Information. "Confidential Information" means any and all '
    "non-public business, financial, technical, customer, supplier, pricing and "
    "proprietary information disclosed by either party, together with the existence "
    "and terms of this Agreement.",
    "1.2 Notwithstanding anything to the contrary in this Agreement, the standard "
    "exclusions in Section 2 shall not apply where the Discloser notifies the "
    "Recipient that it considers the information sensitive.",
    "2. Exclusions. Confidential Information does not include information that (a) is "
    "or becomes publicly available through no fault of the Recipient; (b) was already "
    "lawfully known to the Recipient; (c) is rightfully received from a third party "
    "without obligation of confidentiality; or (d) is independently developed by the "
    "Recipient without use of the Confidential Information.",
    "3. Obligations. The Recipient shall hold the Confidential Information in "
    "confidence and use it solely to evaluate the relationship.",
    "4. Governing Law. This Agreement shall be governed by the laws of England and Wales.",
    "5. Term. The confidentiality obligations survive for a fixed period of two (2) years.",
    _SIGNATURE_BLOCK,
)

# ---- Case 11: incorporation / subordination override (incorporation_check trap) ----
# The NDA is made "subject to" an external Master Services Agreement that is
# "incorporated herein by reference and shall prevail in the event of any
# conflict" (Section 3.2). The confidentiality terms can be silently overridden by
# an unseen document; the reviewer must not clear CI. Direction-resolved: only the
# OTHER document prevailing is the hit.
_INCORPORATION_OVERRIDE = _doc(
    '1. Confidential Information. "Confidential Information" means any and all '
    "non-public business, financial, technical, customer, supplier, pricing and "
    "proprietary information disclosed by either party, together with the existence "
    "and terms of this Agreement.",
    "2. Exclusions. Confidential Information does not include information that is or "
    "becomes publicly available, was already lawfully known to the Recipient, is "
    "rightfully received from a third party, or is independently developed without "
    "use of the Confidential Information.",
    "3.1 The Recipient shall hold the Confidential Information in confidence and use "
    "it solely to evaluate the relationship.",
    "3.2 All obligations of the parties under this Agreement are subject to, and "
    "shall be construed in accordance with, the terms of the Master Services "
    "Agreement between the parties dated 1 January 2024, the terms of which are "
    "incorporated herein by reference and shall prevail in the event of any conflict.",
    "4. Governing Law. This Agreement shall be governed by the laws of England and Wales.",
    "5. Term. The confidentiality obligations survive for a fixed period of two (2) years.",
    _SIGNATURE_BLOCK,
)

# ---- Case 12: poisoned CI definition (definition_poison_check, CI form) ----
# The definition affirmatively INCLUDES publicly available, already-known and
# independently-developed information -- the standard carve-out categories folded
# back IN. Genuinely public knowledge becomes a contractual secret; the reviewer
# must not clear CI.
_CI_POISON = _doc(
    '1. Confidential Information. "Confidential Information" means all business, '
    "financial, technical, customer, supplier, pricing, market, proprietary and "
    "trade secret information disclosed by either party, and includes information "
    "that is publicly available, already known to the Receiving Party, or "
    "independently developed by the Receiving Party.",
    "2. Obligations. The Recipient shall hold the Confidential Information in "
    "confidence and use it solely to evaluate the relationship.",
    "3. Governing Law. This Agreement shall be governed by the laws of England and Wales.",
    "4. Term. The confidentiality obligations survive for a fixed period of two (2) years.",
    _SIGNATURE_BLOCK,
)

# ---- Case 13: over-broad Affiliate definition feeding a restraint ----
#      (definition_poison_check, AFFILIATE/group form -- the breadth trap with NO
#       owning playbook clause; the hardest blind-spot to close on the AI alone.)
# "Affiliate" is drawn beyond the corporate-control test to sweep in arbitrary
# non-parties (anyone a party designates, competitors), and a non-solicit restraint
# then leans on that defined term -- so the restraint binds far more parties than it
# appears. A naive clause-by-clause read clears both the definition and the restraint.
_AFFILIATE_POISON = _doc(
    '1. Confidential Information. "Confidential Information" means all non-public '
    "business, financial, technical and trade secret information disclosed by either "
    "party.",
    '2. Definitions. "Affiliate" means, with respect to a party, any entity that '
    "controls, is controlled by, or is under common control with that party, AND any "
    "other person or entity that such party may from time to time designate, whether "
    "or not affiliated with or under common control with that party, including any "
    "actual or potential competitor of the other party.",
    "3. Restraint. The Recipient shall not, and shall procure that none of its "
    "Affiliates shall, for a period of two (2) years, solicit, hire, or do business "
    "with any customer, supplier, or business contact of the Disclosing Party.",
    "4. Governing Law. This Agreement shall be governed by the laws of England and Wales.",
    "5. Term. The confidentiality obligations survive for a fixed period of two (2) years.",
    _SIGNATURE_BLOCK,
)


def build_cases() -> List[Dict[str, object]]:
    """The adversarial real-path cases -- one per clause + the precision guards."""
    return [
        {
            "name": "confidential_information_double_negation_must_not_clear",
            "clause_id": "confidential_information",
            "trap": "double_negation_polarity_carveout",
            "source_text": _CI_DOUBLE_NEGATION,
            # A definition gutted by stacked negations is not a clean pass.
            "forbidden_decisions": [CLAUSE_DECISION_PASS],
        },
        {
            "name": "mutuality_one_way_must_not_clear",
            "clause_id": "mutuality",
            "trap": "one_way_obligation",
            "source_text": _MUTUALITY_ONE_WAY,
            # Only the Recipient is bound; a one-way NDA must not pass mutuality.
            "forbidden_decisions": [CLAUSE_DECISION_PASS],
        },
        {
            "name": "governing_law_unapproved_must_not_clear",
            "clause_id": "governing_law",
            "trap": "unapproved_law",
            "source_text": _GOVLAW_UNAPPROVED,
            # California is outside the approved set; must not pass.
            "forbidden_decisions": [CLAUSE_DECISION_PASS],
        },
        {
            "name": "governing_law_incorporation_recital_must_not_fail",
            "clause_id": "governing_law",
            "trap": "incorporation_recital_vs_operative",
            "source_text": _GOVLAW_RECITAL,
            # The operative law is APPROVED (England and Wales); the Delaware mention
            # is only a corporate-incorporation recital. The reviewer must not read
            # the recital as the governing-law choice and FAIL. A pass or a review is
            # acceptable; a fail is the regression.
            "forbidden_decisions": [CLAUSE_DECISION_FAIL],
        },
        {
            "name": "governing_law_law_forum_split_must_not_clear",
            "clause_id": "governing_law",
            "trap": "law_forum_split",
            "source_text": _GOVLAW_FORUM_SPLIT,
            # Approved law but a non-approved forum (New York). A reviewer should not
            # blindly clear the split to pass.
            "forbidden_decisions": [CLAUSE_DECISION_PASS],
        },
        {
            "name": "term_and_survival_over_cap_must_not_clear",
            "clause_id": "term_and_survival",
            "trap": "over_cap_survival",
            "source_text": _TERM_OVER_CAP,
            # Fifteen-year survival is over the playbook cap; must not pass.
            "forbidden_decisions": [CLAUSE_DECISION_PASS],
        },
        {
            "name": "non_circumvention_present_must_not_clear",
            "clause_id": "non_circumvention",
            "trap": "prohibited_restriction_present",
            "source_text": _NON_CIRC_PRESENT,
            # A real introduced-party non-solicit is present; must not pass.
            "forbidden_decisions": [CLAUSE_DECISION_PASS],
        },
        {
            "name": "signatures_present_complete_must_not_fail",
            "clause_id": "signatures",
            "trap": "present_and_complete",
            "source_text": _SIGNATURES_COMPLETE,
            # A fully-formed mutual signature block must not be wrongly FAILed.
            "forbidden_decisions": [CLAUSE_DECISION_FAIL],
        },
        {
            "name": "signatures_missing_must_not_clear",
            "clause_id": "signatures",
            "trap": "missing_signature_block",
            "source_text": _SIGNATURES_MISSING,
            # No signature block anywhere; must not pass.
            "forbidden_decisions": [CLAUSE_DECISION_PASS],
        },
        # --- Structural-override traps: the retiring-overlay evidence gate. ---
        # Each must NOT clear (pass) the clause the override undermines on the
        # live reviewer's own judgment -- this is the coverage a deterministic
        # overlay supplies today and the proof needed before retiring it.
        {
            "name": "confidential_information_carveout_negation_must_not_clear",
            "clause_id": "confidential_information",
            "trap": "carveout_negation_notwithstanding",
            "source_text": _CARVEOUT_NEGATION,
            # A clause that cancels the CI exclusions guts the protection; the
            # CI definition must not be cleared as a clean pass.
            "forbidden_decisions": [CLAUSE_DECISION_PASS],
        },
        {
            "name": "confidential_information_incorporation_override_must_not_clear",
            "clause_id": "confidential_information",
            "trap": "incorporation_subordination_override",
            "source_text": _INCORPORATION_OVERRIDE,
            # The NDA is subordinated to an external MSA that prevails; the
            # confidentiality terms can be silently overridden. Must not clear.
            "forbidden_decisions": [CLAUSE_DECISION_PASS],
        },
        {
            "name": "confidential_information_poisoned_definition_must_not_clear",
            "clause_id": "confidential_information",
            "trap": "definition_poison_ci_includes_excluded",
            "source_text": _CI_POISON,
            # A definition that affirmatively includes the excluded categories is
            # poisoned; must not pass.
            "forbidden_decisions": [CLAUSE_DECISION_PASS],
        },
        {
            "name": "non_circumvention_overbroad_affiliate_restraint_must_not_clear",
            "clause_id": "non_circumvention",
            "trap": "definition_poison_overbroad_affiliate",
            "source_text": _AFFILIATE_POISON,
            # An over-broad Affiliate definition feeds a non-solicit restraint, so
            # the restraint binds arbitrary non-parties. The restraint clause must
            # not be cleared. (Hardest trap: no playbook clause owns definition
            # breadth, so this is the most likely live-AI blind-spot.)
            "forbidden_decisions": [CLAUSE_DECISION_PASS],
        },
    ]


def _clause_decision(result: Mapping[str, object], clause_id: str) -> str:
    clauses = result.get("clauses") if isinstance(result.get("clauses"), Sequence) else []
    for clause in clauses:
        if isinstance(clause, Mapping) and str(clause.get("id") or "") == clause_id:
            return str(clause.get("decision") or "")
    return ""


def run_case(case: Mapping[str, object], reviewer: OpenRouterAIAssessmentReviewer) -> Dict[str, object]:
    """Run one adversarial NDA through the REAL reviewer via assess_nda_with_ai.

    Returns an outcome dict with the reviewer's decision for the TARGET clause,
    whether it landed in a forbidden bucket, and the clause's reason for the
    report. The verifier defaults to a no-op (NDA_AI_VERIFIER unset), so this
    isolates the REVIEWER's judgment.
    """
    clause_id = str(case["clause_id"])
    result = assess_nda_with_ai(str(case.get("source_text") or ""), reviewer=reviewer)
    final_decision = _clause_decision(result, clause_id)
    forbidden = {str(d) for d in (case.get("forbidden_decisions") or [])}
    clauses = result.get("clauses") if isinstance(result.get("clauses"), Sequence) else []
    target = next(
        (c for c in clauses if isinstance(c, Mapping) and str(c.get("id") or "") == clause_id),
        {},
    )
    return {
        "name": str(case["name"]),
        "clause_id": clause_id,
        "trap": str(case.get("trap") or ""),
        "final_decision": final_decision,
        "forbidden_decisions": sorted(forbidden),
        "unsafe": final_decision in forbidden,
        "reason": str(target.get("reason") or ""),
        "confidence": target.get("confidence"),
        "grounding_status": str((target.get("grounding") or {}).get("status") or "")
        if isinstance(target.get("grounding"), Mapping)
        else "",
    }


def run_eval(cases: Sequence[Mapping[str, object]] | None = None) -> Dict[str, object]:
    """Resolve the real reviewer and run every adversarial case through it.

    Caller must have confirmed ``real_reviewer_eval_enabled()`` -- this raises a
    clear error otherwise rather than silently no-op'ing (a real-path eval that
    didn't reach a real model is worse than useless).
    """
    if not real_reviewer_eval_enabled():
        raise AIAssessorError(
            "Real reviewer eval is not enabled: " + (skip_reason() or "unknown reason")
        )
    cases = list(cases if cases is not None else build_cases())
    reviewer = resolve_real_reviewer()
    outcomes = [run_case(case, reviewer) for case in cases]
    unsafe = [o for o in outcomes if o["unsafe"]]
    return {
        "outcomes": outcomes,
        "total": len(outcomes),
        "unsafe": unsafe,
        "passed": len(outcomes) - len(unsafe),
    }


def format_report(summary: Mapping[str, object]) -> str:
    lines = [
        "REAL-PATH AI reviewer adversarial eval (live model judgment)",
        "=" * 72,
        f"cases={summary.get('total')}  resisted={summary.get('passed')}  "
        f"UNSAFE-VERDICTS={len(summary.get('unsafe') or [])}",
        "-" * 72,
    ]
    for outcome in summary.get("outcomes") or []:
        flag = "UNSAFE" if outcome["unsafe"] else "ok"
        conf = outcome.get("confidence")
        conf_str = f"{conf:.2f}" if isinstance(conf, (int, float)) else "-"
        lines.append(f"[{flag:6}] {outcome['name']}")
        lines.append(
            f"         clause={outcome['clause_id']} trap={outcome['trap']}  "
            f"decision={outcome['final_decision'] or '-'} "
            f"(forbidden={','.join(outcome['forbidden_decisions'])}) conf={conf_str}"
        )
        if outcome.get("reason"):
            lines.append(f"         reason: {str(outcome['reason'])[:160]}")
    lines.append("-" * 72)
    unsafe = summary.get("unsafe") or []
    lines.append(f"GATE: {'PASS' if not unsafe else 'FAIL'}")
    for outcome in unsafe:
        lines.append(
            f"  ! UNSAFE: {outcome['name']} ({outcome['clause_id']}/{outcome['trap']}) "
            f"-> {outcome['final_decision']} (forbidden {','.join(outcome['forbidden_decisions'])})"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    reason = skip_reason()
    if reason:
        print("REAL-PATH AI reviewer adversarial eval: SKIPPED")
        print(f"  reason: {reason}")
        print(
            f"  to run: {REAL_REVIEWER_EVAL_ENV}=1 {OPENROUTER_API_KEY_ENV}=sk-... "
            "PYTHONPATH=. python -m tests.reviewer_real_eval"
        )
    else:
        print(format_report(run_eval()))
