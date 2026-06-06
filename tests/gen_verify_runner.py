"""Runner: generate every entity's NDA and run the independent gate on each.

This is the plug-and-play entry point for the generation correctness check. It is
deliberately decoupled from generation internals: it calls the public
``nda_generation.generate_nda`` contract, takes the resulting ``docx_bytes``, and
hands them to ``gen_verify_harness.verify_generated_draft`` -- the adversarial gate
that re-derives every verdict independently (it does NOT trust the generator's own
self-check or manifest).

v1 generates only the ``mutual`` variant; the one-way asymmetry check stays warm
for when that variant lands. Run as a module:

    python -m tests.gen_verify_runner

Exit status is non-zero if any draft has a DEFECT, so it doubles as a CI gate.
"""
from __future__ import annotations

import sys
from typing import Any, Mapping

from tests.gen_verify_harness import (
    EntityExpectation,
    VerificationReport,
    expectations_from_registry,
    template_authoritative_sentences,
    verify_generated_draft,
)

# Variants to verify. v1 ships mutual only; one_way is appended once generation
# implements it (NDA_TYPE_ONE_WAY).
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


def _generate(entity_id: str, variant: str, intake: Mapping[str, Any] | None = None) -> bytes:
    """Call the public generation contract and return the draft's docx bytes.

    Kept tolerant of the exact keyword shape generation settles on: it passes
    ``entity_id`` + ``variant`` and an optional counterparty ``intake`` and reads
    ``docx_bytes`` off the returned GenerationResult.
    """
    from nda_automation import nda_generation

    kwargs: dict[str, Any] = {"entity_id": entity_id, "variant": variant}
    if intake is not None:
        kwargs["intake"] = intake
    result = nda_generation.generate_nda(**kwargs)
    docx_bytes = getattr(result, "docx_bytes", None)
    if docx_bytes is None and isinstance(result, Mapping):
        docx_bytes = result.get("docx_bytes")
    if not isinstance(docx_bytes, (bytes, bytearray)):
        raise TypeError(f"generate_nda returned no docx_bytes for {entity_id}/{variant}: {type(result)!r}")
    return bytes(docx_bytes)


def run(intake: Mapping[str, Any] | None = None, variants: tuple[str, ...] = VARIANTS_V1) -> list[VerificationReport]:
    expectations = expectations_from_registry()
    authoritative = template_authoritative_sentences(_template_bytes())
    reports: list[VerificationReport] = []
    for entity_id, expect in expectations.items():
        for variant in variants:
            label = f"{entity_id} / {variant}"
            try:
                docx_bytes = _generate(entity_id, variant, intake)
            except Exception as error:  # generation error is itself a finding
                report = VerificationReport(label=label)
                report.defect("generation.error", f"generate_nda raised: {error!r}")
                reports.append(report)
                continue
            reports.append(
                verify_generated_draft(
                    label=label,
                    docx_bytes=docx_bytes,
                    entity=expect,
                    variant=variant,
                    authoritative_sentences=authoritative,
                )
            )
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
