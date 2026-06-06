"""Independent correctness harness for generated NDAs (feature/gen-verify).

This module is the adversarial second pair of eyes on the NDA *generator*. It does
not trust the generator's own self-check: it re-runs each generated draft through
the real review engines and a set of structural/entity/drift checks, and tries to
find the draft WRONG.

Engine choice (deliberate):

* Native clauses (mutuality, confidential_information, governing_law,
  term_and_survival, signatures) are judged by the DETERMINISTIC engine
  (``checker.review_nda(text, verify=False)``). This is network-free and is the
  authoritative native-clause oracle. The key-free *stub* AI reviewer is NOT a
  valid oracle here: it rubber-stamps every native clause and, because it emits no
  grounded evidence, the grounding layer then downgrades those passes to ``review``
  -- so a stub self-check would mask real native-clause defects.
* The dynamic ``non_circumvention`` clause is only emitted on the AI-first path, so
  we also run the key-free AI-first pipeline to confirm it surfaces and to catch a
  generator that smuggled in a non-circumvention / non-solicit restriction.

A generated draft is CLEAR only when it passes the deterministic Playbook with zero
fails AND the dynamic non_circumvention clause does not fail AND the entity, drift,
and structural checks all pass.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from nda_automation.ai_assessor import (
    _validate_ai_assessment_response,
    build_ai_assessment_packet,
    stub_ai_assessment_response,
)
from nda_automation.ai_first_review import build_ai_first_review_result
from nda_automation.checker import load_playbook, review_nda, validate_playbook
from nda_automation.docx_text import extract_docx_text

# Template placeholder vocabulary (exact strings as authored in generic_nda.docx).
# Any of these left in a generated draft is an unfilled slot = structural defect.
TEMPLATE_PLACEHOLDERS = (
    "[ASPORA ENTITY LEGAL NAME]",
    "[AUTHORISED SIGNATORY]",
    "[BUSINESS DESCRIPTION]",
    "[COMPANY NAME]",
    "[DESIGNATION]",
    "[FORUM / JURISDICTION]",
    "[GOVERNING LAW]",
    "[JURISDICTION OF INCORPORATION]",
    "[REGISTERED OFFICE ADDRESS]",
    "[YEAR]",
    "[•]",  # the [•] day/month bullet
)

# A catch-all for any *other* leftover bracketed/braced placeholder the generator
# might introduce that is not in the known vocabulary above.
GENERIC_PLACEHOLDER_RE = re.compile(r"\[[^\]\n]{0,60}\]|\{\{.*?\}\}|\{%.*?%\}|<[A-Z][A-Z _/]{2,40}>")

# The Playbook's hard clauses. Native clauses are scored deterministically; the
# dynamic clause is scored on the AI-first path.
NATIVE_CLAUSE_IDS = (
    "mutuality",
    "confidential_information",
    "governing_law",
    "term_and_survival",
    "signatures",
)
DYNAMIC_CLAUSE_IDS = ("non_circumvention",)


@dataclass
class EntityExpectation:
    """Authoritative per-entity values a generated draft must contain verbatim.

    Populate from entity-model's registry (the source of truth), not a hand-copied
    list, so the gate and the generator read from the same record.
    """

    key: str
    legal_name: str
    registered_office: str
    jurisdiction_of_incorporation: str
    governing_law: str  # must be one of the Playbook approved_laws
    # Strings that must NOT appear (e.g. Real Transfer's non-default second address).
    forbidden_substrings: tuple[str, ...] = ()


@dataclass
class Finding:
    severity: str  # "DEFECT" | "WARN"
    check: str
    detail: str


@dataclass
class VerificationReport:
    label: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def clear(self) -> bool:
        return not any(f.severity == "DEFECT" for f in self.findings)

    def defect(self, check: str, detail: str) -> None:
        self.findings.append(Finding("DEFECT", check, detail))

    def warn(self, check: str, detail: str) -> None:
        self.findings.append(Finding("WARN", check, detail))

    def render(self) -> str:
        head = f"[{'CLEAR' if self.clear else 'DEFECTS'}] {self.label}"
        if not self.findings:
            return head + "\n  (no findings)"
        lines = [head]
        for f in self.findings:
            lines.append(f"  {f.severity}: {f.check} -- {f.detail}")
        return "\n".join(lines)


def docx_to_text(docx_bytes: bytes) -> str:
    return extract_docx_text(docx_bytes)


# --------------------------------------------------------------------------- #
# 1. Playbook pass (independent of the generator's self-check)
# --------------------------------------------------------------------------- #
def check_playbook_native(text: str, report: VerificationReport) -> Mapping[str, object]:
    """Deterministic Playbook pass. A generated draft must have zero native fails."""
    result = review_nda(text, verify=False)
    by_id = {str(c.get("id")): c for c in result.get("clauses", [])}
    for clause_id in NATIVE_CLAUSE_IDS:
        clause = by_id.get(clause_id)
        if clause is None:
            report.defect("playbook.native", f"clause '{clause_id}' not emitted by deterministic engine")
            continue
        decision = str(clause.get("decision"))
        reason = str(clause.get("reason_code") or "")
        if decision == "fail":
            report.defect("playbook.native", f"{clause_id} FAILED its own Playbook (reason={reason})")
        elif decision == "review":
            report.warn("playbook.native", f"{clause_id} needs review (reason={reason})")
    status = str(result.get("overall_status"))
    if result.get("requirements_failed"):
        report.defect("playbook.native", f"overall {status}: {result.get('requirements_failed')} clause(s) failed")
    return result


def check_non_circumvention(text: str, report: VerificationReport) -> Mapping[str, object]:
    """AI-first (key-free stub) pass to surface the dynamic non_circumvention clause.

    The generator must never introduce a non-circumvention / non-solicit /
    exclusivity restriction. The stub fails that dynamic clause iff a prohibited
    restriction paragraph is present, so a fail here means the draft smuggled one in.
    """
    playbook = load_playbook()
    validate_playbook(playbook)
    packet = build_ai_assessment_packet(text, playbook=playbook)
    raw = stub_ai_assessment_response(packet)
    assessments = _validate_ai_assessment_response(raw, playbook=playbook, packet=packet)
    result = build_ai_first_review_result(text, assessments, playbook=playbook)
    by_id = {str(c.get("id")): c for c in result.get("clauses", [])}
    for clause_id in DYNAMIC_CLAUSE_IDS:
        clause = by_id.get(clause_id)
        if clause is None:
            report.defect("playbook.dynamic", f"dynamic clause '{clause_id}' not emitted by AI-first engine")
            continue
        if str(clause.get("decision")) == "fail":
            report.defect(
                "playbook.dynamic",
                f"{clause_id} FAILED: generator introduced a prohibited restriction (reason={clause.get('reason_code')})",
            )
    return result


# --------------------------------------------------------------------------- #
# 2. Entity correctness
# --------------------------------------------------------------------------- #
def check_entity(text: str, expect: EntityExpectation, report: VerificationReport) -> None:
    if expect.legal_name and expect.legal_name not in text:
        report.defect("entity.legal_name", f"expected legal name not found verbatim: {expect.legal_name!r}")
    if expect.registered_office and expect.registered_office not in text:
        report.defect("entity.address", f"expected registered office not found verbatim: {expect.registered_office!r}")
    if expect.jurisdiction_of_incorporation and expect.jurisdiction_of_incorporation not in text:
        report.warn(
            "entity.incorp_jurisdiction",
            f"jurisdiction of incorporation not found verbatim: {expect.jurisdiction_of_incorporation!r}",
        )
    for forbidden in expect.forbidden_substrings:
        if forbidden and forbidden in text:
            report.defect("entity.wrong_address", f"forbidden substring present (wrong/non-default value): {forbidden!r}")


def check_governing_law(text: str, expect: EntityExpectation, report: VerificationReport) -> None:
    """The governing-law value must match the entity AND be a Playbook-approved law."""
    approved = _approved_laws()
    if expect.governing_law not in approved:
        report.defect(
            "law.not_approved",
            f"expected governing law {expect.governing_law!r} is not in Playbook approved_laws {approved}",
        )
    # The governing-law sentence must name the entity's law. Use the deterministic
    # engine's own verdict on the governing_law clause as the independent oracle,
    # then additionally assert the *specific* expected jurisdiction is present.
    if expect.governing_law and expect.governing_law not in text:
        report.defect(
            "law.entity_mismatch",
            f"governing-law value {expect.governing_law!r} for entity {expect.key!r} not found in draft",
        )
    # Guard against a draft that names a DIFFERENT approved law than the entity wants.
    for other in approved:
        if other == expect.governing_law:
            continue
        # Only flag if the other law appears in a governing-law context and the
        # expected one does not (avoids false positives from the approved-law menu).
        if other in text and expect.governing_law not in text:
            report.defect("law.wrong_jurisdiction", f"draft names {other!r} instead of expected {expect.governing_law!r}")


def _approved_laws() -> tuple[str, ...]:
    playbook = load_playbook()
    for clause in playbook.get("clauses", []):
        if clause.get("id") == "governing_law":
            return tuple(clause.get("approved_laws", []))
    return ()


# --------------------------------------------------------------------------- #
# 3. Variant asymmetry (mutual vs one-way)
# --------------------------------------------------------------------------- #
def check_variant(text: str, variant: str, report: VerificationReport) -> None:
    """mutual -> deterministic mutuality must pass; one_way -> must NOT read as mutual."""
    result = review_nda(text, verify=False)
    clause = next((c for c in result.get("clauses", []) if c.get("id") == "mutuality"), None)
    decision = str(clause.get("decision")) if clause else "missing"
    if variant == "mutual":
        if decision == "fail":
            report.defect("variant.mutual", f"mutual variant fails mutuality (reason={clause.get('reason_code')})")
    elif variant == "one_way":
        if decision == "pass":
            report.warn(
                "variant.one_way",
                "one-way variant still reads as operationally mutual -- confirm asymmetry is real",
            )
    else:
        report.warn("variant.unknown", f"unrecognized variant {variant!r}")


# --------------------------------------------------------------------------- #
# 4. Clause-text drift (generator must never invent a position)
# --------------------------------------------------------------------------- #
# Minimum contiguous shared run (in normalized chars) for a draft sentence to be
# considered "traceable" to an authoritative fragment. Long enough that a filled
# entity value can't accidentally match a clause body, short enough that a filled
# template sentence (placeholder removed) still anchors to its surrounding skeleton.
_DRIFT_MIN_SHARED_RUN = 40


def check_clause_drift(text: str, authoritative_fragments: Sequence[str], report: VerificationReport) -> None:
    """Flag substantive clause wording in the draft that is NOT traceable to the
    template skeleton or Playbook authoritative wording.

    The generator must never invent a legal position. We split the authoritative
    sources -- (a) the template body with placeholders REMOVED (so only the fixed
    skeleton fragments remain) and (b) the Playbook's acceptable_language /
    preferred_position / approved option phrases -- into normalized fragments. A
    draft sentence is "traceable" when it shares a contiguous run of at least
    ``_DRIFT_MIN_SHARED_RUN`` normalized characters with any authoritative fragment.
    Anything not traceable is surfaced (WARN) for a human to eyeball -- it's a
    candidate invented clause, not an automatic defect, because legitimate filled
    values (names/addresses/dates) are also non-traceable by design.
    """
    fragments = [_normalize_sentence(f) for f in authoritative_fragments if f and f.strip()]
    fragments = [f for f in fragments if len(f) >= _DRIFT_MIN_SHARED_RUN // 2]
    for sentence in _substantive_sentences(text):
        norm = _normalize_sentence(sentence)
        if not norm:
            continue
        if _is_party_or_signature_line(sentence):
            continue
        if _max_shared_run(norm, fragments) >= _DRIFT_MIN_SHARED_RUN:
            continue
        report.warn("drift.candidate", f"clause text not traceable to template/Playbook: {sentence[:160]!r}")


def _is_party_or_signature_line(sentence: str) -> bool:
    """Recital/party/signature scaffolding carries filled values, not legal positions;
    it is checked structurally + by the entity gate, so it is exempt from drift."""
    lowered = sentence.lower()
    if "of the first party" in lowered or "of the second party" in lowered:
        return True
    if lowered.startswith("for and on behalf of") or lowered.startswith("for "):
        return True
    if "registered office at" in lowered or "incorporated under the laws of" in lowered:
        return True
    if "made on this" in lowered and "by and between" in lowered:
        return True
    return False


def _max_shared_run(needle: str, fragments: Sequence[str]) -> int:
    """Longest contiguous substring of ``needle`` that appears in any fragment.

    Uses a sliding window over the draft sentence: probes successively shorter
    windows and returns the first (longest) length that is found in a fragment.
    Cheap because it short-circuits at the first hit and the threshold caps work.
    """
    if not fragments:
        return 0
    n = len(needle)
    # Probe windows from longest meaningful down to the threshold; first hit wins.
    upper = min(n, 200)
    for length in range(upper, _DRIFT_MIN_SHARED_RUN - 1, -1):
        for start in range(0, n - length + 1):
            window = needle[start:start + length]
            if any(window in fragment for fragment in fragments):
                return length
    return 0


def _substantive_sentences(text: str) -> Iterable[str]:
    for raw in re.split(r"(?<=[.;])\s+|\n+", text):
        s = raw.strip()
        # Skip signature/party scaffolding, blank lines, and short fragments.
        if len(s) < 40:
            continue
        yield s


def _normalize_sentence(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip()


def _contains_normalized(haystack: str, needle: str) -> bool:
    return bool(needle) and needle in haystack


# --------------------------------------------------------------------------- #
# 5. Structural completeness
# --------------------------------------------------------------------------- #
def check_structural(text: str, report: VerificationReport) -> None:
    for placeholder in TEMPLATE_PLACEHOLDERS:
        if placeholder in text:
            report.defect("structural.unfilled_slot", f"unfilled template placeholder remains: {placeholder!r}")
    for match in set(GENERIC_PLACEHOLDER_RE.findall(text)):
        report.defect("structural.leftover_placeholder", f"leftover placeholder-like token: {match!r}")
    # Every hard clause heading from the template must survive.
    for heading in ("CONFIDENTIAL INFORMATION", "GOVERNING LAW", "TERM OF THE AGREEMENT"):
        if heading not in text.upper():
            report.defect("structural.dropped_clause", f"expected clause heading missing: {heading!r}")
    # Parties + signature scaffolding present.
    if "FIRST PARTY" not in text.upper() or "SECOND PARTY" not in text.upper():
        report.warn("structural.parties", "first/second party designation not clearly present")
    if text.upper().count("FOR AND ON BEHALF OF") < 2 and text.lower().count("\nfor ") < 2:
        report.warn("structural.signature_blocks", "fewer than two signature blocks detected")


def template_authoritative_sentences(template_bytes: bytes) -> list[str]:
    """Authoritative-wording fragment set used by the drift check.

    A *fragment* is a fixed run of template text with placeholders REMOVED -- the
    skeleton between slots. Splitting the template at placeholder boundaries (rather
    than just blanking them inline) keeps each fragment a contiguous run the draft
    can anchor to, so a filled draft sentence matches its surrounding skeleton even
    though the slot regions now hold entity values. Augmented with the Playbook's
    authoritative clause phrasings so Playbook-sourced wording is also traceable.
    """
    text = extract_docx_text(template_bytes)
    # Split at every placeholder so fragments are the fixed skeleton between slots.
    fragments: list[str] = re.split("|".join(re.escape(p) for p in TEMPLATE_PLACEHOLDERS), text)
    # Also split residual generic placeholder tokens out of each fragment.
    skeleton: list[str] = []
    for fragment in fragments:
        skeleton.extend(GENERIC_PLACEHOLDER_RE.split(fragment))
    fragments = [f.strip() for f in skeleton if f.strip()]
    playbook = load_playbook()
    # check_trigger + rationale name the Playbook's STANDARD carve-outs (public-
    # domain, prior-possession, lawful third-party source, qualified independent-
    # development), so a generator clause that renders one of those approved
    # carve-outs anchors here and is not flagged as invented. The drift check still
    # surfaces genuinely novel positions (e.g. a non-compete) that match none of these.
    for clause in playbook.get("clauses", []):
        for key in (
            "acceptable_language",
            "preferred_position",
            "acceptable_position",
            "requirement",
            "check_trigger",
            "rationale",
        ):
            value = clause.get(key)
            if isinstance(value, str) and value.strip():
                fragments.append(value)
        rules = clause.get("rules", {})
        if isinstance(rules, Mapping):
            position = rules.get("acceptable_position")
            if isinstance(position, str) and position.strip():
                fragments.append(position)
            for option in rules.get("approved_options", []):
                value = option.get("value") if isinstance(option, Mapping) else None
                if isinstance(value, str):
                    fragments.append(value)
    return fragments


# --------------------------------------------------------------------------- #
# Registry adapter: build expectations from entity-model's source of truth
# --------------------------------------------------------------------------- #
def _law_value_for_option_id(option_id: str) -> str:
    """Resolve a registry governing_law.playbook_option_id to the live Playbook
    governing-law *value* string (e.g. 'england_and_wales' -> 'England and Wales')."""
    playbook = load_playbook()
    for clause in playbook.get("clauses", []):
        if clause.get("id") != "governing_law":
            continue
        rules = clause.get("rules", {}) if isinstance(clause.get("rules"), Mapping) else {}
        for option in rules.get("approved_options", []):
            if isinstance(option, Mapping) and str(option.get("id")) == option_id:
                return str(option.get("value") or "")
    return ""


def expectations_from_registry() -> dict[str, EntityExpectation]:
    """Build per-entity expectations directly from entity-model's registry so the
    gate and the generator read from the same source of truth.

    For each bundle: legal_name verbatim, the DEFAULT address (joined), the
    governing-law value resolved from the bundle's playbook_option_id, and any
    NON-default address lines registered as forbidden substrings (e.g. Real
    Transfer's Belfast registered office must not be used as the entity address).
    Requires ``nda_automation.entity_registry`` to be importable (it lands when
    feature/entity-registry merges into the draft branch).
    """
    from nda_automation import entity_registry  # imported lazily; lands on merge

    expectations: dict[str, EntityExpectation] = {}
    for bundle in entity_registry.list_entities():
        default = entity_registry.default_address(bundle) or {}
        default_lines = [str(line) for line in default.get("lines", [])]
        forbidden: list[str] = []
        for address in bundle.get("addresses", []):
            if address.get("default"):
                continue
            # The most identifying line of a non-default address (city/postcode).
            for line in address.get("lines", []):
                if line and line not in default_lines:
                    forbidden.append(str(line))
        option_id = str(bundle.get("governing_law", {}).get("playbook_option_id") or "")
        expectations[bundle["id"]] = EntityExpectation(
            key=bundle["id"],
            legal_name=str(bundle.get("legal_name") or ""),
            # Match on the most identifying default line rather than the full join,
            # since the generator may format the address block differently.
            registered_office=_most_identifying_line(default_lines),
            jurisdiction_of_incorporation=str(default.get("country") or ""),
            governing_law=_law_value_for_option_id(option_id),
            forbidden_substrings=tuple(forbidden),
        )
    return expectations


_POSTCODE_RE = re.compile(r"\b[A-Z]{1,2}\d{1,2}[A-Z]?\s*\d[A-Z]{2}\b|\b\d{5,6}\b|\b\d{5}(?:-\d{4})?\b")


def _most_identifying_line(lines: Sequence[str]) -> str:
    """Pick the address line most likely to be reproduced verbatim, so the
    entity-address check is robust to address-block reformatting.

    Preference order: a line carrying a postcode/ZIP (strongest, e.g. 'London,
    EC2A 3BX' beats a generic 'Corporate office' or '3rd Floor'), then any line
    with a street number, then the first line."""
    for line in lines:
        if _POSTCODE_RE.search(line):
            return line
    for line in lines:
        if any(ch.isdigit() for ch in line):
            return line
    return lines[0] if lines else ""


def verify_generated_draft(
    *,
    label: str,
    docx_bytes: bytes,
    entity: EntityExpectation,
    variant: str,
    authoritative_sentences: Sequence[str],
) -> VerificationReport:
    """Run the full adversarial gate on one generated draft."""
    report = VerificationReport(label=label)
    text = docx_to_text(docx_bytes)
    check_structural(text, report)
    check_playbook_native(text, report)
    check_non_circumvention(text, report)
    check_entity(text, entity, report)
    check_governing_law(text, entity, report)
    check_variant(text, variant, report)
    check_clause_drift(text, authoritative_sentences, report)
    return report


if __name__ == "__main__":  # pragma: no cover - manual smoke against the template
    template_path = Path(
        "/Users/daniyalahmad/Desktop/nda-automation/.claude/worktrees/"
        "feature+nda-generation/nda_automation/templates/generic_nda.docx"
    )
    print("approved_laws:", _approved_laws())
    print("template placeholders present:", sum(1 for p in TEMPLATE_PLACEHOLDERS if p in extract_docx_text(template_path.read_bytes())))
