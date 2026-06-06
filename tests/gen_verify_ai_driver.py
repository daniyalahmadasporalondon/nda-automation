"""AI-path verification driver: run the gen-verify gate against AI-ADAPTER output.

The base ``gen_verify_runner`` calls ``generate_nda`` with no ``clause_adapter``,
so it exercises only the deterministic path. This driver drives the *AI-first*
path through the SAME independent gate, all 4 entities x mutual, in a REPEATABLE
way -- it injects a deterministic ``provider`` callable into
``build_clause_adapter(provider=...)``, so no API key / network is needed and the
output is byte-stable across runs (a frozen stand-in for the live AI).

It runs three suites:

  A. FROZEN ON-POSITION AI: a provider that rephrases the Playbook wording,
     weaves in the deal context, and KEEPS every load-bearing term -- i.e. the
     AI behaving as intended. The gate must return CLEAR for all 4 entities, and
     the AI-adapted clause text must actually reach the document (proving the
     gate is judging AI output, not silently the deterministic fallback).

  B. RED-TEAM ADVERSARIAL AI: a set of hostile providers, each trying to drift
     the position (smuggle non-compete / non-solicit / non-circ / exclusivity /
     IP-assignment / perpetual confidentiality / penalty / evergreen, or gut a
     clause, pad, or refuse). For EACH, the generated doc must STILL pass the
     gate -- because the GuardedClauseAdapter rejects the drift and falls back to
     the deterministic Playbook wording, so the prohibited position never reaches
     the document. This is the load-bearing assertion that AI-first is safe.

  C. GUARD-BYPASS PROBE: to prove the gate (not just the guard) is a real
     backstop, we bypass the GuardedClauseAdapter and feed a raw on-skeleton
     adapter that injects a prohibited position the *guard* would catch but which
     shares enough runs to dodge verbatim-drift. The gate's
     check_prohibited_positions (by MEANING) must still flag it as a DEFECT.

Run:  python -m tests.gen_verify_ai_driver
Exit non-zero if any expectation is violated.
"""
from __future__ import annotations

import sys
from typing import Any, Callable, Mapping

from nda_automation import nda_generation as gen
from nda_automation import nda_generation_ai as gen_ai
from nda_automation.checker import load_playbook
from nda_automation.docx_text import extract_docx_text

from tests.gen_verify_harness import (
    EntityExpectation,
    VerificationReport,
    expectations_from_registry,
    template_authoritative_sentences,
    verify_generated_draft,
)
from tests.gen_verify_runner import (
    _crosscheck_manifest,
    _default_intake,
    _template_bytes,
)

ProviderFn = Callable[[Mapping[str, Any]], str]


# --------------------------------------------------------------------------- #
# Provider library (deterministic stand-ins for the live AI)
# --------------------------------------------------------------------------- #
def frozen_on_position_provider(request: Mapping[str, Any]) -> str:
    """A faithful, deterministic 'AI': rephrase + weave in the deal, keep position.

    It returns the authoritative ``playbook_text`` with a natural-language wrapper
    and the counterparty/purpose woven in, but never drops a load-bearing term and
    never adds a prohibited obligation. This is what an on-position live AI would
    return -- it makes the AI-adapted text VISIBLY DIFFERENT from the deterministic
    wording (so the gate is provably judging AI output) while staying on-position.
    """
    base = str(request.get("playbook_text", "")).strip()
    deal = request.get("deal_context", {})
    counterparty = str(deal.get("counterparty") or "the Counterparty")
    purpose = str(deal.get("purpose") or "the Purpose")
    # Reword by prepending a deal-contextual lead-in and appending an
    # acknowledgement -- the substance (base) is preserved verbatim in the middle,
    # so every load-bearing term survives the guard while the surface text changes.
    return (
        f"For the purposes of {purpose} between the parties (including {counterparty}), {base} "
        f"The parties acknowledge and agree to the foregoing in the context of this engagement."
    )


# Each red-team provider returns text that tries to push a prohibited position or
# otherwise drift. The GuardedClauseAdapter must reject every one and fall back to
# the deterministic Playbook wording, so the doc stays clean.
def _redteam_non_compete(request: Mapping[str, Any]) -> str:
    return (
        str(request.get("playbook_text", ""))
        + " Furthermore, each party shall not compete with the other in any market for two years."
    )


def _redteam_non_solicit(request: Mapping[str, Any]) -> str:
    return (
        str(request.get("playbook_text", ""))
        + " The Receiving Party will not solicit the employees or customers of the Disclosing Party."
    )


def _redteam_non_circumvention(request: Mapping[str, Any]) -> str:
    return (
        str(request.get("playbook_text", ""))
        + " Neither party shall circumvent or bypass the other to deal directly with introduced parties."
    )


def _redteam_exclusivity(request: Mapping[str, Any]) -> str:
    return (
        str(request.get("playbook_text", ""))
        + " The parties agree to deal exclusively with one another for the Purpose."
    )


def _redteam_ip_assignment(request: Mapping[str, Any]) -> str:
    return (
        str(request.get("playbook_text", ""))
        + " The Receiving Party hereby assigns all right, title and interest in any derived intellectual property."
    )


def _redteam_perpetual(request: Mapping[str, Any]) -> str:
    # Tries to make confidentiality perpetual OUTSIDE the permitted trade-secret
    # carve-out (so it is genuine drift, not the allowed long-survival).
    return "The confidentiality obligations of each party shall remain in force in perpetuity and forever."


def _redteam_penalty(request: Mapping[str, Any]) -> str:
    return (
        str(request.get("playbook_text", ""))
        + " Any breach shall incur liquidated damages and a penalty of USD 100,000 per occurrence."
    )


def _redteam_evergreen(request: Mapping[str, Any]) -> str:
    return (
        str(request.get("playbook_text", ""))
        + " This Agreement shall automatically renew and may not be terminated by either party."
    )


def _redteam_gut_clause(request: Mapping[str, Any]) -> str:
    return "This clause is intentionally left blank."


def _redteam_refusal(request: Mapping[str, Any]) -> str:
    return "I'm sorry, but I can't help adapt legal clauses."


def _redteam_padding(request: Mapping[str, Any]) -> str:
    return str(request.get("playbook_text", "")) + " " + ("notwithstanding the foregoing " * 400)


def _redteam_exception(request: Mapping[str, Any]) -> str:
    raise RuntimeError("model timed out")


class _RawSmugglingAdapter:
    """Raw adapter NOT wrapped by the guard (Suite C). It keeps a clause's load-
    bearing terms (so even a guard's required-term check would accept it) but
    smuggles a prohibited position in fresh prose -- used to prove the GATE's
    by-meaning scan is a true backstop, independent of the in-process guard."""

    def adapt(self, clause_id, playbook_text, context):  # noqa: ANN001
        if clause_id == "mutuality":
            return (
                "Each party acts as both a Disclosing Party and a Receiving Party. "
                "In addition, each party agrees it shall not solicit the customers of the other."
            )
        return playbook_text


RED_TEAM_PROVIDERS: dict[str, ProviderFn] = {
    "non_compete": _redteam_non_compete,
    "non_solicit": _redteam_non_solicit,
    "non_circumvention": _redteam_non_circumvention,
    "exclusivity": _redteam_exclusivity,
    "ip_assignment": _redteam_ip_assignment,
    "perpetual_confidentiality": _redteam_perpetual,
    "penalty": _redteam_penalty,
    "evergreen": _redteam_evergreen,
    "gut_clause": _redteam_gut_clause,
    "refusal": _redteam_refusal,
    "padding": _redteam_padding,
    "provider_exception": _redteam_exception,
}

# The prohibited substrings each red-team provider tries to inject. After
# generation, NONE of these may appear in the rendered document (the guard must
# have stripped it via fallback). Keyed to match the provider names above.
_PROHIBITED_MARKERS: dict[str, tuple[str, ...]] = {
    "non_compete": ("shall not compete", "compete with the other"),
    "non_solicit": ("will not solicit", "solicit the employees"),
    "non_circumvention": ("circumvent", "bypass the other", "deal directly"),
    "exclusivity": ("deal exclusively", "exclusively with one another"),
    "ip_assignment": ("hereby assigns all right",),
    "perpetual_confidentiality": ("in perpetuity", "forever"),
    "penalty": ("liquidated damages", "penalty of usd"),
    "evergreen": ("automatically renew", "may not be terminated"),
}


# --------------------------------------------------------------------------- #
# Drivers
# --------------------------------------------------------------------------- #
def _gate_one(
    *,
    label: str,
    entity_id: str,
    expect: EntityExpectation,
    playbook: Mapping[str, Any],
    authoritative: list[str],
    adapter: Any,
) -> tuple[VerificationReport, str, Any]:
    """Generate one draft with the given adapter and run the full gate on it."""
    from nda_automation import entity_registry

    bundle = entity_registry.get_entity(entity_id)
    entity = gen.entity_party_from_bundle(bundle, playbook)
    intake = _default_intake("mutual")
    report = VerificationReport(label=label)
    try:
        result = gen.generate_nda(entity, intake, playbook=playbook, clause_adapter=adapter)
    except Exception as error:  # generation failure is itself a finding
        report.defect("generation.error", f"generate_nda raised: {error!r}")
        return report, "", None
    docx_bytes = result.docx_bytes
    text = extract_docx_text(docx_bytes)
    full = verify_generated_draft(
        label=label,
        docx_bytes=docx_bytes,
        entity=expect,
        variant="mutual",
        authoritative_sentences=authoritative,
    )
    _crosscheck_manifest(result.manifest, expect, text, full)
    return full, text, result.manifest


def run_suite_a(playbook, expectations, authoritative) -> list[tuple[str, VerificationReport, bool]]:
    """Suite A: frozen ON-POSITION AI -> gate must be CLEAR for all 4, and the
    AI-adapted wording must actually be present in the document."""
    results = []
    adapter = gen_ai.build_clause_adapter(provider=frozen_on_position_provider)
    # The frozen provider injects this distinctive lead-in; it proves AI text
    # reached the document (not the deterministic fallback).
    ai_marker = "in the context of this engagement"
    for entity_id, expect in expectations.items():
        label = f"[A:on-position-AI] {entity_id} / mutual"
        report, text, _ = _gate_one(
            label=label, entity_id=entity_id, expect=expect,
            playbook=playbook, authoritative=authoritative, adapter=adapter,
        )
        ai_present = ai_marker in text.lower()
        if not ai_present:
            report.defect(
                "ai.not_applied",
                "AI-adapted wording absent from document -- gate may be judging the deterministic fallback, not AI output",
            )
        results.append((label, report, ai_present))
    return results


def run_suite_b(playbook, expectations, authoritative) -> list[tuple[str, VerificationReport]]:
    """Suite B: each red-team adversarial AI provider, per entity. The gate must
    return CLEAR AND none of the prohibited markers may appear in the document."""
    results = []
    for attack, provider in RED_TEAM_PROVIDERS.items():
        adapter = gen_ai.build_clause_adapter(provider=provider)
        for entity_id, expect in expectations.items():
            label = f"[B:redteam:{attack}] {entity_id} / mutual"
            report, text, _ = _gate_one(
                label=label, entity_id=entity_id, expect=expect,
                playbook=playbook, authoritative=authoritative, adapter=adapter,
            )
            lowered = text.lower()
            for marker in _PROHIBITED_MARKERS.get(attack, ()):
                if marker.lower() in lowered:
                    report.defect(
                        "redteam.leaked",
                        f"prohibited '{attack}' wording reached the document: {marker!r}",
                    )
            results.append((label, report))
    return results


def run_suite_c(playbook, expectations, authoritative) -> list[tuple[str, VerificationReport, bool]]:
    """Suite C: GATE-AS-BACKSTOP probe. Bypass the GuardedClauseAdapter and feed a
    RAW adapter whose output keeps the load-bearing terms (so the guard's required-
    term check would pass) but ALSO carries a prohibited position. We assert the
    INDEPENDENT GATE (check_prohibited_positions, by MEANING) flags it as a DEFECT
    even though the guard let it through -- proving the gate is a true second line,
    not redundant with the guard.

    We attach the prohibited text to mutuality, keeping its required terms intact.
    """

    results = []
    adapter = _RawSmugglingAdapter()
    for entity_id, expect in expectations.items():
        label = f"[C:gate-backstop] {entity_id} / mutual"
        report, text, _ = _gate_one(
            label=label, entity_id=entity_id, expect=expect,
            playbook=playbook, authoritative=authoritative, adapter=adapter,
        )
        # The smuggled text SHOULD be in the document (guard bypassed) AND the gate
        # SHOULD have flagged it. We invert the usual pass/fail: a DEFECT here is the
        # EXPECTED, correct outcome.
        smuggled_present = "shall not solicit" in text.lower()
        gate_caught = any(
            f.severity == "DEFECT" and f.check == "position.prohibited" for f in report.findings
        )
        results.append((label, report, smuggled_present and gate_caught))
    return results


def main() -> int:
    playbook = load_playbook()
    expectations = expectations_from_registry()
    authoritative = template_authoritative_sentences(_template_bytes())

    print("=" * 78)
    print("AI-ADAPTER VERIFICATION THROUGH THE GEN-VERIFY GATE (repeatable / key-free)")
    print("=" * 78)

    any_failure = False

    # ----- Suite A -----
    print("\n## SUITE A -- frozen ON-POSITION AI (gate must be CLEAR + AI text applied)\n")
    a = run_suite_a(playbook, expectations, authoritative)
    for label, report, ai_present in a:
        ok = report.clear and ai_present
        any_failure = any_failure or not ok
        print(f"{'PASS' if ok else 'FAIL'}  {label}  (ai_applied={ai_present})")
        for f in report.findings:
            if f.severity == "DEFECT":
                print(f"      DEFECT: {f.check} -- {f.detail}")

    # ----- Suite B -----
    print("\n## SUITE B -- RED-TEAM adversarial AI (guard+fallback => gate CLEAR, no leak)\n")
    b = run_suite_b(playbook, expectations, authoritative)
    b_fail = 0
    for label, report in b:
        ok = report.clear
        if not ok:
            b_fail += 1
            any_failure = True
            print(f"FAIL  {label}")
            for f in report.findings:
                if f.severity == "DEFECT":
                    print(f"      DEFECT: {f.check} -- {f.detail}")
    print(f"  {len(b) - b_fail}/{len(b)} red-team drafts CLEAR (guard neutralised the attack)")

    # ----- Suite C -----
    print("\n## SUITE C -- GATE-AS-BACKSTOP (guard bypassed; gate must CATCH by meaning)\n")
    c = run_suite_c(playbook, expectations, authoritative)
    for label, report, caught in c:
        # Here a DEFECT is EXPECTED. 'caught' True == correct.
        any_failure = any_failure or not caught
        print(f"{'PASS' if caught else 'FAIL'}  {label}  (smuggled_and_gate_flagged={caught})")
        if not caught:
            for f in report.findings:
                print(f"      {f.severity}: {f.check} -- {f.detail}")

    print("\n" + "=" * 78)
    print(f"OVERALL: {'DEFECTS FOUND' if any_failure else 'CLEAR'}")
    print("=" * 78)
    return 1 if any_failure else 0


if __name__ == "__main__":
    sys.exit(main())
