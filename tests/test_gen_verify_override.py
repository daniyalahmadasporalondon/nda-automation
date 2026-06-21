"""Locked-governing-law tests for the gen-verify governing-law check.

LAW + COURT ARE LOCKED TO THE SIGNING ENTITY: the override path has been removed
from generation. The gate validates the draft against the entity's OWN law, and
the ``overridden`` flag survives only as a TRIPWIRE -- a manifest that reports an
override (``overridden == True`` OR ``effective_law != entity_default_law``) is a
DEFECT ``law.override_present`` (the lock was bypassed upstream). These tests drive
the real generator (which now rejects a divergent override) and synthetic
manifests, then assert the gate behaves correctly.

The manifest contract (coordinated with generation):
  governing_law_value: str            # the EFFECTIVE law in the clause
  governing_law_overridden: bool      # always False under the lock; tripwire if True
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
    _law_phrase_for_value,
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
# 1. A manifest reporting an override (law diverged from the entity default) is a
#    DEFECT -- the override path was removed, so this means the lock was bypassed.
# --------------------------------------------------------------------------- #
def test_manifest_reporting_an_override_is_flagged_as_override_present():
    # Synthesise the bypassed state: a draft rendered with England (a non-default
    # law for aspora_technology) plus a manifest that reports the override. Under
    # the lock this must raise law.override_present.
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

    report = VerificationReport(label="override-present")
    check_governing_law(text, expect, report, override=override)
    # The TRIPWIRE fires: a manifest reporting an override is a defect now.
    assert _findings(report, "law.override_present"), [(f.check, f.detail) for f in report.findings]


def test_only_the_entitys_own_law_generates_and_passes_the_gate():
    # Under the lock, generation REJECTS a divergent override and only the entity's
    # OWN option (india for aspora_technology) succeeds. The successful draft names
    # the entity's law, reports overridden=False, and passes the gate cleanly.
    expect = expectations_from_registry()["aspora_technology"]

    for option in _approved_options():
        if option["id"] == "india":
            result = gen.generate_nda_for_entity(
                "aspora_technology",
                _intake(),
                playbook=PLAYBOOK,
                governing_law_override=option["id"],  # == default: harmless no-op
                use_ai=False,
            )
            text = extract_docx_text(result.docx_bytes)
            check = gen.self_check_generated_nda(result.docx_bytes, playbook=PLAYBOOK)
            override = gov_law_override_from_manifest(result.manifest, expect)
            report = VerificationReport(label=f"entity-own-law {option['id']}")
            check_governing_law(text, expect, report, override=override)

            assert result.manifest.governing_law_option_id == "india"
            assert result.manifest.governing_law_value == option["value"]
            assert result.manifest.governing_law_overridden is False
            expected_phrase = _law_phrase_for_value(option["value"])
            assert expected_phrase in text
            assert check.passed, (option["id"], check.native_failures, check.native_reviews)
            assert _findings(report, "law.override_present") == []
            assert _findings(report, "law.entity_mismatch") == []
            assert _findings(report, "law.not_approved") == []
            assert report.clear, [(f.check, f.detail) for f in report.findings]
        else:
            # A divergent override is rejected outright by generation.
            try:
                gen.generate_nda_for_entity(
                    "aspora_technology",
                    _intake(),
                    playbook=PLAYBOOK,
                    governing_law_override=option["id"],
                    use_ai=False,
                )
            except gen.NdaGenerationError:
                continue
            raise AssertionError(
                f"override to {option['id']!r} should have been rejected by the entity lock"
            )


# --------------------------------------------------------------------------- #
# 2. The effective law diverging from the entity default (even with the flag
#    unset) trips the tripwire -- the law must equal the entity's own.
# --------------------------------------------------------------------------- #
def test_effective_law_diverging_from_entity_default_is_flagged():
    # A manifest whose effective law (England) differs from the entity default
    # (India) is a bypassed lock even if the overridden flag is not set.
    result = _generate_with_law("aspora_technology", "england_and_wales")

    text = extract_docx_text(result.docx_bytes)
    expect = expectations_from_registry()["aspora_technology"]
    manifest = _FakeManifest(
        governing_law_value=_law_value("england_and_wales"),  # diverges from India
        governing_law_overridden=False,  # flag unset, but the law still diverges
        entity_default_governing_law_value=expect.governing_law,
    )
    override = gov_law_override_from_manifest(manifest, expect)
    report = VerificationReport(label="effective-diverges")
    check_governing_law(text, expect, report, override=override)
    # The effective law != entity default -> law.override_present DEFECT.
    assert _findings(report, "law.override_present"), [(f.check, f.detail) for f in report.findings]


# --------------------------------------------------------------------------- #
# 3. The entity-default law that is NOT a Playbook-approved position -> DEFECT.
# --------------------------------------------------------------------------- #
def test_entity_law_not_approved_is_flagged():
    # The gate still catches an entity whose own (and effective) law is not in the
    # Playbook-approved set. Build an expectation whose governing law is unapproved.
    expect = expectations_from_registry()["aspora_technology"]
    from dataclasses import replace

    bad_expect = replace(expect, governing_law="Laws of Narnia")
    manifest = _FakeManifest(
        governing_law_value="Laws of Narnia",
        governing_law_overridden=False,
        entity_default_governing_law_value="Laws of Narnia",
    )
    override = gov_law_override_from_manifest(manifest, bad_expect)
    report = VerificationReport(label="entity-law-unapproved")
    check_governing_law("This Agreement is governed by the Laws of Narnia.", bad_expect, report, override=override)
    assert _findings(report, "law.not_approved"), [(f.check, f.detail) for f in report.findings]


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
