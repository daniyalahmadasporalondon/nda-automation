"""ROUTE-LEVEL governing-law override smoke through the full gen-verify gate.

CRITICAL distinction from test_gen_verify_override_smoke.py: that smoke calls the
INTERNAL API (generate_nda_for_entity with the override as a kwarg), which bypasses
the route payload parser. A real bug lived exactly there — the FE sends the override
NESTED at signing_entity.governing_law.playbook_option_id, and an earlier parser read
it only at the top level, SILENTLY DROPPING the override and falling back to the
entity default. An internal-API smoke passes while the real FE->endpoint path is
broken (the same blind spot that made the generator's own test green).

So this smoke drives the ROUTE PARSER: it feeds the EXACT shape
static/js/modules/draft-intake.mjs:buildDraftPayload emits (override nested under
signing_entity.governing_law.playbook_option_id) through the real
routing workflow intake parser — the function the HTTP handler uses.

LAW + COURT ARE NOW LOCKED TO THE SIGNING ENTITY: the parser still carries the
nested override (so a future top-level-only refactor still fails loudly), but
generation now REJECTS a DIVERGENT override outright. For each sampled entity
overridden to a DIFFERENT approved law, this asserts the parser carries the nested
override AND that ``generate_nda_for_entity`` raises ``NdaGenerationError`` -- the
override path was removed, so a divergent override is refused, never applied.
"""
from __future__ import annotations


import pytest

from nda_automation import nda_generation as gen
from nda_automation import nda_generation_workflow
from nda_automation.checker import load_playbook

PLAYBOOK = load_playbook()

# Each entity overridden to a DIFFERENT approved law than its registry default.
_OVERRIDE_TARGET = {
    "aspora_technology": ("india", "england_and_wales"),  # India default -> England
    "vance_money": ("delaware", "india"),
    "real_transfer": ("england_and_wales", "difc"),
    "vance_techlabs": ("difc", "delaware"),
}

# Entity legal names + a label, just to populate the FE payload realistically.
_ENTITY_LABEL = {
    "aspora_technology": "Aspora Technology Services Private Limited",
    "vance_money": "Vance Money Services LLC",
    "real_transfer": "Real Transfer Limited",
    "vance_techlabs": "Vance Techlabs Limited",
}


def _law_value(option_id: str) -> str:
    for clause in PLAYBOOK.get("clauses", []):
        if clause.get("id") != "governing_law":
            continue
        for opt in clause.get("rules", {}).get("approved_options", []):
            if opt.get("id") == option_id:
                return str(opt.get("value"))
    raise KeyError(option_id)


def _fe_payload(entity_id: str, override_option_id: str) -> dict:
    """The EXACT nested shape draft-ui's buildDraftPayload emits, with the override
    under signing_entity.governing_law.playbook_option_id (the bug surface)."""
    return {
        "counterparty": {"name": "Counterparty Holdings Limited", "email": "legal@counterparty.example"},
        "project_purpose": "evaluating a potential commercial relationship",
        "term": "3 years",
        "nda_type": "mutual",
        "notes": "financial technology services",
        "signing_entity": {
            "id": entity_id,
            "legal_name": _ENTITY_LABEL[entity_id],
            "governing_law": {
                "playbook_option_id": override_option_id,
                "label": _law_value(override_option_id),
            },
            "governing_law_overridden": True,
        },
    }


@pytest.mark.parametrize("entity_id,laws", list(_OVERRIDE_TARGET.items()))
def test_route_override_is_carried_then_rejected_by_the_entity_lock(entity_id, laws):
    default_option, override_option = laws

    payload = _fe_payload(entity_id, override_option)

    # Drive the REAL workflow parser used by the route (where the nesting bug lived).
    parsed_entity_id, intake, governing_law_override, _address_id, _email = nda_generation_workflow.intake_from_payload(payload)

    # The parser must STILL CARRY the nested override (not drop it) -- the nesting
    # regression guard survives the entity lock.
    assert parsed_entity_id == entity_id
    assert governing_law_override == override_option, (
        f"route parser dropped the nested override: got {governing_law_override!r}, "
        f"expected {override_option!r} from signing_entity.governing_law.playbook_option_id"
    )
    assert override_option != default_option

    # LAW LOCKED TO ENTITY: generation now REJECTS the divergent override outright
    # rather than applying it. The override path was removed.
    with pytest.raises(gen.NdaGenerationError):
        gen.generate_nda_for_entity(
            parsed_entity_id, intake, playbook=PLAYBOOK,
            governing_law_override=governing_law_override, use_ai=False,
        )


def test_route_drops_nothing_regression_guard():
    """Direct regression guard for the nesting bug: the route parser MUST extract
    the override from signing_entity.governing_law.playbook_option_id. If a future
    refactor reverts to top-level-only reading, this fails loudly."""
    payload = _fe_payload("aspora_technology", "england_and_wales")
    _eid, _intake, override, _address_id, _email = nda_generation_workflow.intake_from_payload(payload)
    assert override == "england_and_wales", (
        "route parser must read the nested signing_entity.governing_law.playbook_option_id "
        "(the FE shape); a top-level-only parser silently drops the override"
    )
