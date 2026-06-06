"""WIRED regression for AI-first NDA generation through the gen-verify gate.

This is the deployment of the safety net: the gen-verify gate was an offline
harness; here it becomes a permanent pytest so AI-first generation cannot regress
silently. It drives the SAME three suites as ``tests/gen_verify_ai_driver.py``
(its programmatic twin), parametrized per entity, with deterministic providers so
it is repeatable and key-free.

The contract these tests ENCODE (what "AI-first is safe" means):

  A. On-position AI  -> every entity's draft is CLEAR through the full gate, and
     the AI-adapted wording actually reaches the document.
  B. Red-team AI     -> every prohibited-position attack is CONTAINED: the gate is
     CLEAR and no prohibited wording leaks into the rendered document.
  C. Gate-as-backstop-> when the in-process guard is bypassed, the gate's
     by-meaning prohibited-position scan still flags the smuggled position.

KNOWN-FAIL (xfail, strict): five red-team families currently LEAK past the
``GuardedClauseAdapter`` because its ``_PROHIBITED_PATTERN`` only covers 3/8
position families (DEFECT 1), and nothing gates the ship path (DEFECT 2). Those
parametrizations are marked ``xfail(strict=True)`` referencing the defects, so:
  * the suite is GREEN today (the leaks are expected failures), and
  * the moment generation lands the fix, the xfail will XPASS and -- because it is
    STRICT -- turn the suite RED, forcing whoever fixed it to delete the xfail and
    lock the contained behaviour in. That is what keeps the net deployed.
"""
from __future__ import annotations

import pytest

from nda_automation import nda_generation as gen
from nda_automation import nda_generation_ai as gen_ai
from nda_automation.checker import load_playbook
from nda_automation.docx_text import extract_docx_text

from tests.gen_verify_ai_driver import (
    RED_TEAM_PROVIDERS,
    _PROHIBITED_MARKERS,
    frozen_on_position_provider,
)
from tests.gen_verify_ai_driver import _RawSmugglingAdapter  # type: ignore[attr-defined]
from tests.gen_verify_harness import (
    expectations_from_registry,
    gov_law_override_from_manifest,
    template_authoritative_sentences,
    verify_generated_draft,
)
from tests.gen_verify_runner import _crosscheck_manifest, _default_intake, _template_bytes

# The five red-team families that currently leak past the guard (DEFECT 1: the
# GuardedClauseAdapter prohibited-pattern covers only non_circumvention /
# exclusivity / perpetual_confidentiality; DEFECT 2: the ship path never gates).
# These are xfail(strict) so they flip the suite RED once the fix lands.
_KNOWN_LEAKING_ATTACKS = frozenset(
    {"non_compete", "non_solicit", "ip_assignment", "penalty", "evergreen"}
)

_ENTITY_IDS = ("aspora_technology", "vance_money", "real_transfer", "vance_techlabs")


@pytest.fixture(scope="module")
def playbook():
    return load_playbook()


@pytest.fixture(scope="module")
def expectations():
    return expectations_from_registry()


@pytest.fixture(scope="module")
def authoritative():
    return template_authoritative_sentences(_template_bytes())


def _gate(entity_id, adapter, playbook, expectations, authoritative):
    """Generate one draft with ``adapter`` and run the full gate; return (report, text)."""
    from nda_automation import entity_registry

    bundle = entity_registry.get_entity(entity_id)
    entity = gen.entity_party_from_bundle(bundle, playbook)
    result = gen.generate_nda(entity, _default_intake("mutual"), playbook=playbook, clause_adapter=adapter)
    text = extract_docx_text(result.docx_bytes)
    expect = expectations[entity_id]
    report = verify_generated_draft(
        label=f"{entity_id}/mutual",
        docx_bytes=result.docx_bytes,
        entity=expect,
        variant="mutual",
        authoritative_sentences=authoritative,
        gov_law_override=gov_law_override_from_manifest(result.manifest, expect),
    )
    _crosscheck_manifest(result.manifest, expect, text, report)
    return report, text


def _defects(report):
    return [f"{f.check}: {f.detail}" for f in report.findings if f.severity == "DEFECT"]


# --------------------------------------------------------------------------- #
# Suite A -- on-position AI is CLEAR + the AI wording reaches the document
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("entity_id", _ENTITY_IDS)
def test_on_position_ai_is_clear(entity_id, playbook, expectations, authoritative):
    adapter = gen_ai.build_clause_adapter(provider=frozen_on_position_provider)
    report, text = _gate(entity_id, adapter, playbook, expectations, authoritative)
    assert report.clear, _defects(report)
    # Prove the gate judged AI output, not the silent deterministic fallback.
    assert "in the context of this engagement" in text.lower()


# --------------------------------------------------------------------------- #
# Suite B -- red-team AI is CONTAINED (gate CLEAR + no prohibited leak).
# The 5 known-leaking families are xfail(strict) pending DEFECT 1/2.
# --------------------------------------------------------------------------- #
def _redteam_params():
    params = []
    for attack in RED_TEAM_PROVIDERS:
        marks = ()
        if attack in _KNOWN_LEAKING_ATTACKS:
            marks = (
                pytest.mark.xfail(
                    strict=True,
                    reason=(
                        f"DEFECT 1: GuardedClauseAdapter._PROHIBITED_PATTERN does not cover "
                        f"{attack!r}; it leaks into the document. DEFECT 2: generate_and_save_nda "
                        f"never gates. Remove this xfail once both are fixed."
                    ),
                ),
            )
        for entity_id in _ENTITY_IDS:
            params.append(pytest.param(attack, entity_id, marks=marks, id=f"{attack}-{entity_id}"))
    return params


@pytest.mark.parametrize("attack,entity_id", _redteam_params())
def test_redteam_attack_is_contained(attack, entity_id, playbook, expectations, authoritative):
    """Each adversarial AI output must be neutralised: the gate is CLEAR AND none of
    the attack's prohibited markers appear in the rendered document."""
    adapter = gen_ai.build_clause_adapter(provider=RED_TEAM_PROVIDERS[attack])
    report, text = _gate(entity_id, adapter, playbook, expectations, authoritative)
    lowered = text.lower()
    leaked = [m for m in _PROHIBITED_MARKERS.get(attack, ()) if m.lower() in lowered]
    assert not leaked, f"prohibited {attack!r} wording leaked into doc: {leaked}"
    assert report.clear, _defects(report)


# --------------------------------------------------------------------------- #
# Suite C -- gate is a true backstop: with the guard bypassed, the by-meaning
# prohibited-position scan still flags a smuggled position.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("entity_id", _ENTITY_IDS)
def test_gate_catches_smuggled_position_when_guard_bypassed(
    entity_id, playbook, expectations, authoritative
):
    report, text = _gate(entity_id, _RawSmugglingAdapter(), playbook, expectations, authoritative)
    # The smuggled non-solicit kept its load-bearing terms, so a guard would accept
    # it -- it reaches the document...
    assert "shall not solicit" in text.lower()
    # ...and the gate's meaning-based scan flags it as a prohibited position.
    flagged = [f for f in report.findings if f.severity == "DEFECT" and f.check == "position.prohibited"]
    assert flagged, "gate failed to flag a smuggled prohibited position by meaning"
