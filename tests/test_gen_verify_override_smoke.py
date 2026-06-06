"""4-entity governing-law OVERRIDE smoke through the full gen-verify gate.

Overrides EACH entity to a DIFFERENT approved law than its registry default and
asserts, through the full adversarial gate, the team-lead's success criteria:
  * the clause names the OVERRIDE law (not the entity default),
  * the gate is CLEAR with NO false law.entity_mismatch / law.override_mismatch,
  * the effective law is one of the 4 approved.

Calls the REAL override API generation shipped (e2c4c82):
``generate_nda_for_entity(entity_id, intake, governing_law_override=<option_id>)``,
so the smoke exercises the production injection path and reads the real manifest
(governing_law_value / governing_law_overridden / entity_default_governing_law_value).
"""
from __future__ import annotations

import datetime

import pytest

from nda_automation import nda_generation as gen
from nda_automation.checker import load_playbook
from nda_automation.docx_text import extract_docx_text

from tests.gen_verify_harness import (
    expectations_from_registry,
    gov_law_override_from_manifest,
    template_authoritative_sentences,
    verify_generated_draft,
)
from tests.gen_verify_runner import _crosscheck_manifest, _template_bytes

PLAYBOOK = load_playbook()

# Each entity overridden to a DIFFERENT approved law than its registry default
# (defaults: aspora=india, vance_money=delaware, real_transfer=england_and_wales,
# vance_techlabs=difc). Rotate so every override genuinely differs from the default.
_OVERRIDE_TARGET = {
    "aspora_technology": "delaware",
    "vance_money": "england_and_wales",
    "real_transfer": "difc",
    "vance_techlabs": "india",
}


def _law_value(option_id: str) -> str:
    for clause in PLAYBOOK.get("clauses", []):
        if clause.get("id") != "governing_law":
            continue
        for opt in clause.get("rules", {}).get("approved_options", []):
            if opt.get("id") == option_id:
                return str(opt.get("value"))
    raise KeyError(option_id)


def _intake():
    return gen.CounterpartyIntake(
        company_name="Counterparty Holdings Limited",
        registered_office="10 Market Street, Singapore 049315",
        jurisdiction_of_incorporation="Singapore",
        business_description="financial technology services",
        purpose="evaluating a potential commercial relationship",
        term_years=3,
        nda_type="mutual",
        agreement_date=datetime.date(2026, 6, 6),
    )


@pytest.mark.parametrize("entity_id,override_option_id", list(_OVERRIDE_TARGET.items()))
def test_override_smoke_through_full_gate(entity_id, override_option_id):
    override_value = _law_value(override_option_id)
    # Use the REAL override API; use_ai=False keeps the body deterministic/repeatable.
    result = gen.generate_nda_for_entity(
        entity_id,
        _intake(),
        playbook=PLAYBOOK,
        governing_law_override=override_option_id,
        use_ai=False,
    )
    manifest = result.manifest
    text = extract_docx_text(result.docx_bytes)
    expect = expectations_from_registry()[entity_id]
    authoritative = template_authoritative_sentences(_template_bytes())

    # The generator recorded the override provenance on the real manifest.
    assert manifest.governing_law_overridden is True
    assert manifest.governing_law_value == override_value
    assert manifest.entity_default_governing_law_value == expect.governing_law
    # The override genuinely differs from the entity default.
    assert override_value != expect.governing_law
    # The clause names the OVERRIDE law.
    assert override_value in text, f"draft does not name the override law {override_value!r}"

    override = gov_law_override_from_manifest(manifest, expect)
    assert override is not None and override.overridden
    assert override.effective_law == override_value

    report = verify_generated_draft(
        label=f"override {entity_id}->{override_option_id}",
        docx_bytes=result.docx_bytes,
        entity=expect,
        variant="mutual",
        authoritative_sentences=authoritative,
        gov_law_override=override,
    )
    _crosscheck_manifest(manifest, expect, text, report)

    # No false law mismatch of any kind, and no not-approved.
    law_defects = [
        (f.check, f.detail)
        for f in report.findings
        if f.severity == "DEFECT"
        and f.check.startswith(("law.", "manifest.governing", "manifest.override"))
    ]
    assert not law_defects, law_defects
    assert report.clear, [(f.check, f.detail) for f in report.findings if f.severity == "DEFECT"]
