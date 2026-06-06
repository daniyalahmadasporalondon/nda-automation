"""Runner: generate every entity's NDA and run the independent gate on each.

This is the plug-and-play entry point for the generation correctness check. It is
deliberately decoupled from generation internals: it calls the public
``nda_generation.generate_nda`` contract, takes the resulting ``docx_bytes``, and
hands them to ``gen_verify_harness.verify_generated_draft`` -- the adversarial gate
that re-derives every verdict independently (it does NOT trust the generator's own
self-check or manifest).

v1 is MUTUAL-only by product scope (one-way is out of scope), so the gate checks
that each draft is *properly mutual*. Run as a module:

    python -m tests.gen_verify_runner

Exit status is non-zero if any draft has a DEFECT, so it doubles as a CI gate.
"""
from __future__ import annotations

import sys
from typing import Any

from tests.gen_verify_harness import (
    EntityExpectation,
    VerificationReport,
    docx_to_text,
    expectations_from_registry,
    template_authoritative_sentences,
    verify_generated_draft,
)

# v1 is MUTUAL-only by product scope (one-way is out of scope, not just unbuilt),
# so the gate verifies the single mutual variant per entity.
VARIANTS_V1 = ("mutual",)


def _template_bytes() -> bytes:
    """Read the tracked template asset from the generation package."""
    from nda_automation import nda_generation  # noqa: F401  (ensures pkg present)
    import importlib.resources as resources

    try:
        return (resources.files("nda_automation") / "templates" / "generic_nda.docx").read_bytes()
    except Exception:
        # Fall back to a path relative to the package file.
        from pathlib import Path

        pkg_dir = Path(nda_generation.__file__).resolve().parent
        return (pkg_dir / "templates" / "generic_nda.docx").read_bytes()


# A neutral counterparty for the gate. Deliberately uses a jurisdiction that is
# NOT "England" so it cannot collide with the England-and-Wales governing-law
# sentence (a stray "laws of England" flips governing_law to review/unapproved).
def _default_intake(variant: str):
    from nda_automation.nda_generation import CounterpartyIntake

    return CounterpartyIntake(
        company_name="Counterparty Holdings Limited",
        registered_office="10 Market Street, Singapore 049315",
        jurisdiction_of_incorporation="Singapore",
        business_description="financial technology services",
        purpose="evaluating a potential commercial relationship",
        term_years=3,
        nda_type=variant,
    )


def _generate(entity_id: str, variant: str) -> tuple[bytes, Any]:
    """Call the real generation contract and return (docx_bytes, manifest).

    The contract is ``generate_nda(entity, intake, *, playbook)`` where ``entity``
    is an ``EntityParty`` built from the registry bundle via
    ``entity_party_from_bundle(bundle, playbook)`` -- so the registry stays the
    single source of entity truth on both sides.
    """
    from nda_automation import entity_registry, nda_generation
    from nda_automation.checker import load_playbook

    playbook = load_playbook()
    bundle = entity_registry.get_entity(entity_id)
    if bundle is None:
        raise KeyError(f"entity_registry has no entity {entity_id!r}")
    entity = nda_generation.entity_party_from_bundle(bundle, playbook)
    intake = _default_intake(variant)
    result = nda_generation.generate_nda(entity, intake, playbook=playbook)
    docx_bytes = getattr(result, "docx_bytes", None)
    manifest = getattr(result, "manifest", None)
    if not isinstance(docx_bytes, (bytes, bytearray)):
        raise TypeError(f"generate_nda returned no docx_bytes for {entity_id}/{variant}: {type(result)!r}")
    return bytes(docx_bytes), manifest


import re as _re

# Manifest slots whose value the generator legitimately reformats before rendering,
# so the canonical source string is NOT expected to appear verbatim in the prose.
_TRANSFORMED_SLOT_KEYS = ("agreement_date", "purpose", "business description")
_ISO_DATE_RE = _re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_transformed_fill(slot: str, value: str) -> bool:
    """True when a manifest fill is reformatted into the prose (so a verbatim
    presence check would false-positive). Covers the ISO agreement_date that the
    template renders as 'Nth day of <Month>, <Year>', and free-text deal fields
    (purpose / business description) that may be re-cased or rephrased."""
    slot_l = slot.lower()
    if any(key in slot_l for key in _TRANSFORMED_SLOT_KEYS):
        return True
    if _ISO_DATE_RE.match(value.strip()):
        return True
    return False


def _crosscheck_manifest(manifest: Any, expect: EntityExpectation, text: str, report: VerificationReport) -> None:
    """Adversarially cross-check the generator's self-reported manifest two ways.

    1. manifest-vs-registry: the manifest is the generator's CLAIM about its
       intent; if it disagrees with the registry the generator's intent is wrong.
    2. manifest-vs-prose: a manifest can claim the right value yet the rendered
       document say something else. So every value the manifest claims to have
       filled must actually appear in the rendered text -- catching a generator
       whose ground-truth record and output diverge. This is the check that keeps
       the manifest honest as a ground-truth source.
    """
    if manifest is None:
        report.warn("manifest.absent", "no manifest returned; relying on prose-derived checks only")
        return
    legal = getattr(manifest, "entity_legal_name", None)
    if legal is not None and legal != expect.legal_name:
        report.defect("manifest.legal_name", f"manifest claims {legal!r}, registry expects {expect.legal_name!r}")
    law = getattr(manifest, "governing_law_value", None)
    if law is not None and law != expect.governing_law:
        report.defect("manifest.governing_law", f"manifest claims {law!r}, registry expects {expect.governing_law!r}")

    # manifest-vs-prose: each claimed IDENTITY fill must appear verbatim. We only
    # check values the generator reproduces literally (names, addresses, law,
    # forum). Values it legitimately TRANSFORMS -- the ISO agreement_date becomes
    # "6th day of June, 2026", and the purpose/business prose may be re-cased -- are
    # excluded, since absence of the canonical source string is expected, not a bug.
    for field_name in ("entity_legal_name", "governing_law_value", "counterparty_name", "forum"):
        claimed = getattr(manifest, field_name, None)
        if isinstance(claimed, str) and claimed and claimed not in text:
            report.defect("manifest.prose_mismatch", f"manifest {field_name}={claimed!r} but not found in rendered draft")
    slot_fills = getattr(manifest, "slot_fills", None)
    if isinstance(slot_fills, dict):
        for slot, value in slot_fills.items():
            if not (isinstance(value, str) and value.strip()):
                continue
            if _is_transformed_fill(slot, value):
                continue
            if value not in text:
                report.defect(
                    "manifest.slot_mismatch",
                    f"manifest slot_fills[{slot!r}]={value!r} but not found verbatim in rendered draft",
                )


def run(variants: tuple[str, ...] = VARIANTS_V1) -> list[VerificationReport]:
    expectations = expectations_from_registry()
    authoritative = template_authoritative_sentences(_template_bytes())
    reports: list[VerificationReport] = []
    for entity_id, expect in expectations.items():
        for variant in variants:
            label = f"{entity_id} / {variant}"
            try:
                docx_bytes, manifest = _generate(entity_id, variant)
            except Exception as error:  # generation error is itself a finding
                report = VerificationReport(label=label)
                report.defect("generation.error", f"generate_nda raised: {error!r}")
                reports.append(report)
                continue
            report = verify_generated_draft(
                label=label,
                docx_bytes=docx_bytes,
                entity=expect,
                variant=variant,
                authoritative_sentences=authoritative,
            )
            _crosscheck_manifest(manifest, expect, docx_to_text(docx_bytes), report)
            reports.append(report)
    return reports


def main() -> int:
    reports = run()
    print("=" * 72)
    print("NDA GENERATION CORRECTNESS GATE")
    print("=" * 72)
    any_defect = False
    for report in reports:
        print(report.render())
        print("-" * 72)
        any_defect = any_defect or not report.clear
    clear = sum(1 for r in reports if r.clear)
    print(f"SUMMARY: {clear}/{len(reports)} drafts CLEAR")
    return 1 if any_defect else 0


if __name__ == "__main__":
    sys.exit(main())
