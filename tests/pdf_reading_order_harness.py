"""Reusable golden-baseline harness for the PDF reading-order fixture corpus.

Given a working tree (whatever ``nda_automation.pdf_text`` is importable), this
extracts every fixture in ``tests/fixtures/pdf_reading_order`` and produces a
STABLE, JSON-serializable snapshot of exactly what the AI reviewer would see plus
the extraction quality/confidence signals. The snapshot is written per fixture
under ``baselines/`` and can be diffed against a committed golden.

Two entry points:

  * ``snapshot(name) -> dict``           extract one fixture into a snapshot dict
  * ``capture_baselines()``              (re)write every baselines/<name>.json
  * ``diff_against_baselines()``         extract all, diff vs committed golden,
                                         return {name: None|difftext}

The gate semantics (interpreted by the caller / a pytest wrapper), keyed off the
fixture category recorded in the generator's FIXTURES registry:

  * negative / garble_trap  -> snapshot MUST equal the committed baseline
                               (byte-identical: no false-positive regression).
  * positive / garble_open* -> snapshot is EXPECTED to change once the fix lands;
                               the baseline documents the pre-fix (buggy) state.
  * garble_fixed            -> anchor for the already-shipped glyph-fragment fix.

Run:  python -m tests.pdf_reading_order_harness            # diff report
      python -m tests.pdf_reading_order_harness --capture  # rewrite baselines
"""

from __future__ import annotations

import difflib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE_DIR = os.path.join(HERE, "fixtures", "pdf_reading_order")
BASELINE_DIR = os.path.join(FIXTURE_DIR, "baselines")

# The subset of the quality report that is (a) deterministic and (b) load-bearing
# for the reading-order / confidence contract. The full visual_profile also
# carries transient counts; we keep the fields that express the confidence signal.
_QUALITY_KEYS = (
    "page_count",
    "pages_with_text",
    "pages_without_text",
    "extracted_paragraphs",
    "extracted_characters",
    "repeated_margin_lines_removed",
    "warnings",
)
_VISUAL_PROFILE_KEYS = (
    "status",
    "requires_source_preview",
    "visual_features",
)


# Real PDFs already committed in the repo, folded into the corpus as byte-identity
# regression anchors. name -> (repo-relative path, category, behavior). Only real
# document currently in the tree is the single-column inbound sample; if more real
# NDAs are added, list them here so the gate protects their extraction too.
REAL_ANCHORS = {
    "real_inbound_nda_sample": (
        os.path.join("tests", "fixtures", "inbound_nda_sample.pdf"),
        "real_negative",
        "The one real PDF in the repo: 1 page, single column, word-chunked, no "
        "tables. Extraction must stay byte-identical (no false-positive regression "
        "on a real document).",
    ),
}


def _fixture_names():
    """Return {name: (builder_or_None, category, behavior)} for every corpus item
    (generated fixtures + real anchors)."""
    from tests.fixtures.pdf_reading_order.generate_fixtures import FIXTURES

    combined = dict(FIXTURES)  # name -> (builder, category, behavior)
    for name, (path, cat, beh) in REAL_ANCHORS.items():
        combined[name] = (None, cat, beh)
    return combined


def _fixture_path(name: str) -> str:
    if name in REAL_ANCHORS:
        repo_root = os.path.dirname(HERE)
        return os.path.join(repo_root, REAL_ANCHORS[name][0])
    return os.path.join(FIXTURE_DIR, f"{name}.pdf")


def _quality_snapshot(quality: dict) -> dict:
    out = {k: quality.get(k) for k in _QUALITY_KEYS}
    vp = quality.get("visual_profile")
    if isinstance(vp, dict):
        out["visual_profile"] = {k: vp.get(k) for k in _VISUAL_PROFILE_KEYS}
    else:
        out["visual_profile"] = vp
    return out


def snapshot(name: str) -> dict:
    """Extract one fixture into a stable snapshot dict."""
    from nda_automation.pdf_text import extract_pdf_document

    path = _fixture_path(name)
    with open(path, "rb") as fh:
        data = fh.read()
    doc = extract_pdf_document(data)
    return {
        "name": name,
        # The exact text the AI reviewer reasons over, paragraph by paragraph.
        "paragraphs": [
            {"page_number": p.get("page_number"), "text": p["text"]}
            for p in doc.paragraphs
        ],
        # The single joined string extract_pdf_text() feeds downstream.
        "extracted_text": "\n\n".join(str(p["text"]) for p in doc.paragraphs),
        "quality": _quality_snapshot(doc.quality),
    }


def _dumps(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True)


def capture_baselines() -> list[str]:
    os.makedirs(BASELINE_DIR, exist_ok=True)
    written = []
    for name in _fixture_names():
        snap = snapshot(name)
        path = os.path.join(BASELINE_DIR, f"{name}.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_dumps(snap) + "\n")
        written.append(name)
    return written


def diff_against_baselines() -> dict:
    """Return {name: None if identical else unified-diff string}."""
    results = {}
    for name in _fixture_names():
        base_path = os.path.join(BASELINE_DIR, f"{name}.json")
        current = _dumps(snapshot(name)) + "\n"
        if not os.path.exists(base_path):
            results[name] = f"MISSING BASELINE: {base_path}"
            continue
        with open(base_path, "r", encoding="utf-8") as fh:
            golden = fh.read()
        if golden == current:
            results[name] = None
        else:
            results[name] = "".join(
                difflib.unified_diff(
                    golden.splitlines(keepends=True),
                    current.splitlines(keepends=True),
                    fromfile=f"baseline/{name}.json",
                    tofile=f"current/{name}.json",
                )
            )
    return results


def main(argv):
    if "--capture" in argv:
        names = capture_baselines()
        print(f"captured {len(names)} baselines under {BASELINE_DIR}")
        return 0
    results = diff_against_baselines()
    changed = {n: d for n, d in results.items() if d}
    fixtures = _fixture_names()
    for name in sorted(results):
        cat = fixtures[name][1]
        status = "CHANGED" if results[name] else "identical"
        print(f"[{status:9}] {cat:16} {name}")
    if changed:
        print(f"\n{len(changed)} fixture(s) differ from baseline:")
        for name in sorted(changed):
            print(f"\n===== {name} ({fixtures[name][1]}) =====")
            print(changed[name])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
