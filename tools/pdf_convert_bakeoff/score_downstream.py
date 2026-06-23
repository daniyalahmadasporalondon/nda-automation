"""Downstream scoring (THE KEY METRIC).

For each converted DOCX in the outputs dir, run it through the REAL shipped
review/structure/redline-anchor path and record whether the conversion makes the
production system work better:

  - clauses_total / clauses_present / clauses_review / clauses_pass
        from ``review_nda_with_active_engine`` (forced DETERMINISTIC engine, so
        this stage costs nothing and is reproducible -- the conversion quality,
        not the LLM, is what varies).
  - structure_sections / structure_reference_index
        from the review result's ``contract_structure`` (the same structure the
        Structure tab renders).
  - anchor_mapped / anchor_total / anchor_success_rate
        the redline-anchor success: how many of the review paragraphs the shipped
        ``map_paragraphs_to_reconstruction`` aligner can index-anchor into THIS
        converted DOCX body. This is exactly the gate the redline export uses, so
        a higher rate => fewer "redline could not be placed" fail-closed blocks.

Every function reused here is imported from ``nda_automation`` -- the pipeline is
NOT duplicated.

Usage:
    python -m tools.pdf_convert_bakeoff.score_downstream \
        --out tools/pdf_convert_bakeoff/outputs
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sys


@dataclass
class DownstreamScore:
    docx: str
    engine: str
    status: str  # "ok" | "failed"
    clauses_total: int | None = None
    clauses_present: int | None = None
    clauses_review: int | None = None
    clauses_pass: int | None = None
    structure_sections: int | None = None
    structure_reference_index: int | None = None
    anchor_total: int | None = None
    anchor_mapped: int | None = None
    anchor_success_rate: float | None = None
    error: str | None = None


def _engine_from_name(docx_path: Path) -> str:
    stem = docx_path.stem
    return stem.rsplit("__", 1)[-1] if "__" in stem else "unknown"


def _count_clause_statuses(clauses) -> tuple[int, int, int, int]:
    total = present = review = passed = 0
    for clause in clauses or []:
        if not isinstance(clause, dict):
            continue
        total += 1
        status = str(clause.get("status") or clause.get("verdict") or "").lower()
        if status not in {"not_present", "missing"}:
            present += 1
        if status in {"review", "check"}:
            review += 1
        if status in {"pass", "ok", "approved"}:
            passed += 1
    return total, present, review, passed


def score_docx(docx_path: Path) -> DownstreamScore:
    engine = _engine_from_name(docx_path)
    try:
        from nda_automation.docx_text import extract_docx_text
        from nda_automation.pdf_ingest_conversion import (
            map_paragraphs_to_reconstruction,
            reconstructed_body_index,
        )
        from nda_automation.review_engine import review_nda_with_active_engine

        docx_bytes = docx_path.read_bytes()
        text = extract_docx_text(docx_bytes)

        # Run the REAL review through the deterministic engine (no AI cost, stable).
        review = review_nda_with_active_engine(text, force_engine="deterministic")

        clauses = review.get("clauses", [])
        total, present, review_n, passed = _count_clause_statuses(clauses)

        structure = review.get("contract_structure") or {}
        sections = structure.get("sections") or []
        ref_index = structure.get("reference_index") or {}

        # Redline-anchor success: align the review paragraphs onto THIS converted
        # DOCX body using the shipped aligner. Higher mapped-rate => the redline
        # export's fail-closed anchor gate places more changes.
        review_paragraphs = review.get("paragraphs", [])
        recon_index = reconstructed_body_index(docx_bytes)
        _mapped, mapped_count, unmapped_count = map_paragraphs_to_reconstruction(
            review_paragraphs, recon_index
        )
        anchor_total = mapped_count + unmapped_count
        rate = round(mapped_count / anchor_total, 4) if anchor_total else None

        return DownstreamScore(
            docx=docx_path.name,
            engine=engine,
            status="ok",
            clauses_total=total,
            clauses_present=present,
            clauses_review=review_n,
            clauses_pass=passed,
            structure_sections=len(sections),
            structure_reference_index=len(ref_index),
            anchor_total=anchor_total,
            anchor_mapped=mapped_count,
            anchor_success_rate=rate,
        )
    except Exception as exc:
        return DownstreamScore(
            docx=docx_path.name,
            engine=engine,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )


def score_outputs(output_dir: Path) -> list[DownstreamScore]:
    docxs = sorted(output_dir.glob("*__*.docx"))
    if not docxs:
        print(f"WARNING: no converted DOCX (*__*.docx) found in {output_dir}", file=sys.stderr)
    scores = []
    for docx in docxs:
        score = score_docx(docx)
        scores.append(score)
        if score.status == "ok":
            print(
                f"OK:     {docx.name} — clauses {score.clauses_present}/{score.clauses_total} "
                f"present, {score.structure_sections} sections, "
                f"anchor {score.anchor_mapped}/{score.anchor_total} "
                f"({score.anchor_success_rate})"
            )
        else:
            print(f"FAILED: {docx.name} — {score.error}")
    return scores


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="score_downstream")
    here = Path(__file__).resolve().parent
    parser.add_argument("--out", type=Path, default=here / "outputs")
    args = parser.parse_args(argv)

    scores = score_outputs(args.out)
    path = args.out / "downstream_scores.json"
    path.write_text(json.dumps([asdict(s) for s in scores], indent=2), encoding="utf-8")
    print(f"\nWrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
