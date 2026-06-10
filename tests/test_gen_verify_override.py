"""Override-awareness tests for the gen-verify governing-law check.

The product allows a user to OVERRIDE an entity's default governing law with a
different one, constrained to the Playbook-approved options. The gate must
validate the draft against the CHOSEN law, not mechanically flag "law != entity
default" as drift. These tests drive the real generator (which renders the chosen
law into the clause) and a manifest carrying the override fields generation is
adding, then assert the gate behaves correctly.

The manifest contract (coordinated with generation):
  governing_law_value: str            # the EFFECTIVE (chosen) law in the clause
  governing_law_overridden: bool      # True iff chosen != entity default
  entity_default_governing_law_value: str  # the entity's default (optional)
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass

from nda_automation import entity_registry, nda_generation as gen
from nda_automation.checker import load_playbook
from nda_automation.docx_text import extract_docx_text

from tests.gen_verify_harness import (
    VerificationReport,
    check_governing_law,
    expectations_from_registry,
    gov_law_override_from_manifest,
)


PLAYBOOK = load_playbook()


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


def _generate_with_law(entity_id: str, option_id: str):
    """Generate a draft for ``entity_id`` but with the governing law forced to
    ``option_id`` -- the mechanism a real override uses (the FE sends a different
    playbook_option_id). We build the EntityParty from the bundle, then swap the
    governing-law option so the rendered clause names the chosen law."""
    bundle = dict(entity_registry.get_entity(entity_id))
    bundle["governing_law"] = {"playbook_option_id": option_id}
    entity = gen.entity_party_from_bundle(bundle, PLAYBOOK)
    result = gen.generate_nda(entity, _intake(), playbook=PLAYBOOK)
    return result


@dataclass
class _FakeManifest:
    governing_law_value: str
    governing_law_overridden: bool = False
    entity_default_governing_law_value: str = ""


def _law_value(option_id: str) -> str:
    for clause in PLAYBOOK.get("clauses", []):
        if clause.get("id") != "governing_law":
            continue
        rules = clause.get("rules", {})
        for opt in rules.get("approved_options", []):
            if opt.get("id") == option_id:
                return opt.get("value")
    raise KeyError(option_id)


def _approved_options() -> list[dict]:
    for clause in PLAYBOOK.get("clauses", []):
        if clause.get("id") == "governing_law":
            return list(clause.get("rules", {}).get("approved_options", []))
    return []


def _findings(report: VerificationReport, check: str) -> list:
    return [f for f in report.findings if f.check == check and f.severity == "DEFECT"]


# --------------------------------------------------------------------------- #
# 1. Override to a DIFFERENT approved law -> no mismatch DEFECT
# --------------------------------------------------------------------------- #
def test_override_to_different_approved_law_is_clean():
    # aspora defaults to India; override to England and Wales (both approved).
    override_value = _law_value("england_and_wales")
    result = _generate_with_law("aspora_technology", "england_and_wales")

    text = extract_docx_text(result.docx_bytes)
    expect = expectations_from_registry()["aspora_technology"]  # default = India
    manifest = _FakeManifest(
        governing_law_value=override_value,
        governing_law_overridden=True,
        entity_default_governing_law_value=expect.governing_law,
    )
    override = gov_law_override_from_manifest(manifest, expect)
    assert override is not None and override.overridden and override.effective_law == override_value

    report = VerificationReport(label="override-clean")
    check_governing_law(text, expect, report, override=override)
    # No mismatch of any kind, and no not-approved defect.
    assert _findings(report, "law.override_mismatch") == []
    assert _findings(report, "law.entity_mismatch") == []
    assert _findings(report, "law.override_not_approved") == []
    assert _findings(report, "law.not_approved") == []
    assert report.clear, [(f.check, f.detail) for f in report.findings]


def test_every_approved_override_law_is_clean_through_generation_self_check_and_gate():
    expect = expectations_from_registry()["aspora_technology"]

    for option in _approved_options():
        result = gen.generate_nda_for_entity(
            "aspora_technology",
            _intake(),
            playbook=PLAYBOOK,
            governing_law_override=option["id"],
            use_ai=False,
        )
        text = extract_docx_text(result.docx_bytes)
        check = gen.self_check_generated_nda(result.docx_bytes, playbook=PLAYBOOK)
        override = gov_law_override_from_manifest(result.manifest, expect)
        report = VerificationReport(label=f"approved-law-carryover {option['id']}")
        check_governing_law(text, expect, report, override=override)

        assert result.manifest.governing_law_option_id == option["id"]
        assert result.manifest.governing_law_value == option["value"]
        assert option["value"] in text
        assert check.passed, (option["id"], check.native_failures, check.native_reviews)
        assert _findings(report, "law.override_mismatch") == []
        assert _findings(report, "law.entity_mismatch") == []
        assert _findings(report, "law.override_not_approved") == []
        assert _findings(report, "law.not_approved") == []
        assert report.clear, [(f.check, f.detail) for f in report.findings]


# --------------------------------------------------------------------------- #
# 2. Override claimed, but the draft still names the entity default -> DEFECT
# --------------------------------------------------------------------------- #
def test_override_but_draft_names_default_is_flagged():
    # Manifest CLAIMS an England override, but the generated draft was actually
    # rendered with India (the entity default) -- a real generator bug.
    result = _generate_with_law("aspora_technology", "india")  # draft says India

    text = extract_docx_text(result.docx_bytes)
    expect = expectations_from_registry()["aspora_technology"]
    manifest = _FakeManifest(
        governing_law_value=_law_value("england_and_wales"),  # claims England
        governing_law_overridden=True,
        entity_default_governing_law_value=expect.governing_law,
    )
    override = gov_law_override_from_manifest(manifest, expect)
    report = VerificationReport(label="override-mismatch")
    check_governing_law(text, expect, report, override=override)
    # The chosen (override) law is absent from the draft -> override_mismatch DEFECT.
    assert _findings(report, "law.override_mismatch"), [(f.check, f.detail) for f in report.findings]


# --------------------------------------------------------------------------- #
# 3. Override to a NON-approved law -> DEFECT (defense in depth)
# --------------------------------------------------------------------------- #
def test_override_to_non_approved_law_is_flagged():
    # The FE constrains overrides to the live Playbook-approved options, but the gate must still
    # catch an out-of-band override to an unapproved law. We don't render it; we
    # only feed the manifest claim, since the assertion is on the manifest intent.
    expect = expectations_from_registry()["aspora_technology"]
    manifest = _FakeManifest(
        governing_law_value="Laws of Narnia",  # not one of the 4
        governing_law_overridden=True,
        entity_default_governing_law_value=expect.governing_law,
    )
    override = gov_law_override_from_manifest(manifest, expect)
    report = VerificationReport(label="override-unapproved")
    # Use the entity's own draft text (India) -- irrelevant; the not-approved check
    # fires on the override value regardless of prose.
    check_governing_law("This Agreement is governed by the laws of India.", expect, report, override=override)
    assert _findings(report, "law.override_not_approved"), [(f.check, f.detail) for f in report.findings]


# --------------------------------------------------------------------------- #
# 4. NOT overridden -> original entity-default behaviour preserved
# --------------------------------------------------------------------------- #
def test_no_override_keeps_entity_default_behaviour():
    result = _generate_with_law("real_transfer", "england_and_wales")  # its own default

    text = extract_docx_text(result.docx_bytes)
    expect = expectations_from_registry()["real_transfer"]  # default = England and Wales
    manifest = _FakeManifest(
        governing_law_value=expect.governing_law,
        governing_law_overridden=False,
    )
    override = gov_law_override_from_manifest(manifest, expect)
    assert override is not None and not override.overridden
    report = VerificationReport(label="no-override")
    check_governing_law(text, expect, report, override=override)
    assert report.clear, [(f.check, f.detail) for f in report.findings]


# --------------------------------------------------------------------------- #
# 5. No manifest at all -> None resolver -> original behaviour (back-compat)
# --------------------------------------------------------------------------- #
def test_absent_manifest_falls_back_to_entity_default():
    expect = expectations_from_registry()["real_transfer"]
    assert gov_law_override_from_manifest(None, expect) is None
    # And a manifest without the governing_law_value field also yields None.
    assert gov_law_override_from_manifest(object(), expect) is None
