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

D1/D2 history: five red-team families (non_compete, non_solicit, ip_assignment,
penalty, evergreen) once LEAKED past the ``GuardedClauseAdapter`` because its
``_PROHIBITED_PATTERN`` covered only 3/8 position families (DEFECT 1), and the ship
path saved without a gate (DEFECT 2). Both are fixed: the guard now covers all 8
families, and ``generate_and_save_nda`` runs a meaning-based prohibited-position
gate that raises before save. These tests (formerly strict-xfails that flipped the
suite RED when the fix landed) now assert the contained behaviour directly, locking
it in so a regression in either layer turns the suite RED again.
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
    # D1 widened the guard to all 8 prohibited families, so every red-team attack is
    # now CONTAINED for every entity -- no xfails. (History: 5 families
    # non_compete/non_solicit/ip_assignment/penalty/evergreen used to leak; the guard
    # fix + ship-path gate closed them, and these assertions now lock that in.)
    params = []
    for attack in RED_TEAM_PROVIDERS:
        for entity_id in _ENTITY_IDS:
            params.append(pytest.param(attack, entity_id, id=f"{attack}-{entity_id}"))
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


# --------------------------------------------------------------------------- #
# DEFECT 1 contract -- the GUARD itself must BLOCK each leaking family (strip the
# prohibited text via fallback), not merely rely on the downstream gate to flag it.
# These assert at the GuardedClauseAdapter boundary: the adapted clause text must
# NOT contain the prohibited wording. xfail(strict) until the pattern is widened.
# --------------------------------------------------------------------------- #
# An on-position base clause for mutuality that keeps the guard's required terms,
# so the only reason to reject is the appended prohibited position (not a missing
# load-bearing term) -- isolating the prohibited-pattern's coverage.
_MUTUALITY_BASE = (
    "Each party acts as both a Disclosing Party and a Receiving Party with respect to "
    "Confidential Information it discloses or receives."
)

# Each leaking family -> a clause-appended prohibited sentence the guard must strip.
_GUARD_LEAK_APPENDIX = {
    "non_compete": " Furthermore, each party shall not compete with the other in any market.",
    "non_solicit": " The Receiving Party will not solicit the employees of the Disclosing Party.",
    "ip_assignment": " The Receiving Party hereby assigns all right, title and interest in derived IP.",
    "penalty": " Any breach shall incur liquidated damages and a penalty of USD 100,000.",
    "evergreen": " This Agreement shall automatically renew and may not be terminated.",
}
_GUARD_LEAK_MARKER = {
    "non_compete": "shall not compete",
    "non_solicit": "will not solicit",
    "ip_assignment": "hereby assigns all right",
    "penalty": "liquidated damages",
    "evergreen": "automatically renew",
}


@pytest.mark.parametrize("attack", list(_GUARD_LEAK_APPENDIX))
def test_guard_blocks_prohibited_position(attack):
    """The GUARD (not just the downstream gate) must strip a prohibited position from
    adapted clause text: when the AI appends one, the guard's on-position check must
    fail and fall back to the Playbook wording, so the prohibited marker is absent."""

    def provider(_request):
        return _MUTUALITY_BASE + _GUARD_LEAK_APPENDIX[attack]

    adapter = gen_ai.build_clause_adapter(provider=provider)
    adapted = adapter.adapt("mutuality", _MUTUALITY_BASE, {"counterparty": "X"})
    marker = _GUARD_LEAK_MARKER[attack]
    assert marker not in adapted.lower(), (
        f"guard accepted prohibited {attack!r}: {marker!r} survived in adapted text"
    )


# --------------------------------------------------------------------------- #
# DEFECT 2 contract -- the SHIP PATH must GATE: if a prohibited position reaches the
# rendered document, generate_and_save_nda must RAISE and NOT persist the artifact.
#
# We exercise D2 INDEPENDENTLY of D1: a guard-bypassing adapter (_RawSmugglingAdapter)
# puts a non-solicit into the document without going through the GuardedClauseAdapter,
# so the prohibited position genuinely reaches the rendered doc. The ship-path gate is
# the LAST line -- it must catch it by meaning and refuse to save, even though the
# guard never saw it. This is what makes the gate a real backstop, not a duplicate of
# the guard: a position the guard can't see (smuggled outside an adapted clause, or via
# any future code path) is still stopped before it becomes a signable artifact.
# --------------------------------------------------------------------------- #
def test_ship_path_gate_raises_and_does_not_save_on_leak(monkeypatch, playbook):
    """A prohibited position that reaches the document (here via a guard-bypassing
    adapter) must be BLOCKED by the ship path: generate_and_save_nda raises and the
    artifact is NOT saved."""

    saved = {"called": False}

    def fake_add_artifact(*args, **kwargs):
        saved["called"] = True
        class _A:
            id = "artifact-should-not-exist"
        return _A()

    # Intercept the persistence boundary so we can assert "no save happened".
    monkeypatch.setattr("nda_automation.artifact_service.add_artifact", fake_add_artifact)

    with pytest.raises(gen.NdaGenerationError):
        gen.generate_and_save_nda(
            "aspora_technology",
            _default_intake("mutual"),
            matter_id="m-test",
            playbook=playbook,
            clause_adapter=_RawSmugglingAdapter(),  # bypasses the guard
            use_ai=False,
        )
    assert not saved["called"], "ship path persisted an artifact containing a prohibited position"
