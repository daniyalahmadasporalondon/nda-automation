"""Intrinsic fidelity scoring -- LLM-judge rubric, converted DOCX vs source PDF.

For each converted DOCX it asks a vision-capable model to score, 0-5 each with a
short note:
  - tables_preserved
  - heading_hierarchy
  - reading_order
  - paragraph_integrity
  - text_accuracy
  - logo_image_presence

The judge sees the source PDF's rendered page images (PyMuPDF, already a repo
dependency) plus the converted DOCX's extracted text/structure. Model is
configurable via --model / BAKEOFF_JUDGE_MODEL. Auth uses the SAME env var the
app uses, OPENROUTER_API_KEY; the call is the OpenRouter chat/completions
endpoint (reuses the repo's endpoint constant). This stage SKIPS cleanly with a
clear message when no key is set, so the harness still runs end-to-end.

Usage:
    python -m tools.pdf_convert_bakeoff.score_intrinsic \
        --out tools/pdf_convert_bakeoff/outputs \
        --corpus tools/pdf_convert_bakeoff/corpus
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import sys
import urllib.request

RUBRIC_DIMENSIONS = (
    "tables_preserved",
    "heading_hierarchy",
    "reading_order",
    "paragraph_integrity",
    "text_accuracy",
    "logo_image_presence",
)

DEFAULT_JUDGE_MODEL = os.environ.get("BAKEOFF_JUDGE_MODEL", "anthropic/claude-opus-4.8-fast")
MAX_JUDGE_PAGES = int(os.environ.get("BAKEOFF_JUDGE_MAX_PAGES", "4"))


@dataclass
class IntrinsicScore:
    docx: str
    engine: str
    status: str  # "ok" | "failed" | "skipped"
    scores: dict[str, int] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)
    overall: float | None = None
    error: str | None = None
    skip_reason: str | None = None


def judge_available() -> tuple[bool, str]:
    if not os.environ.get("OPENROUTER_API_KEY", "").strip():
        return False, "missing OPENROUTER_API_KEY"
    return True, ""


def _engine_from_name(docx_path: Path) -> str:
    stem = docx_path.stem
    return stem.rsplit("__", 1)[-1] if "__" in stem else "unknown"


def _source_pdf_for(docx_path: Path, corpus_dir: Path) -> Path | None:
    # outputs are "<stem>__<engine>.docx"; corpus PDFs are "<original>.pdf" where
    # stem == original.stem with spaces -> underscores. Match by normalised stem.
    base = docx_path.stem.rsplit("__", 1)[0]
    for pdf in corpus_dir.glob("*.pdf"):
        if pdf.stem.replace(" ", "_") == base:
            return pdf
    return None


def _render_pdf_pages(pdf_path: Path, max_pages: int) -> list[bytes]:
    import fitz  # PyMuPDF, already a repo dependency

    images: list[bytes] = []
    with fitz.open(pdf_path) as doc:
        for page in doc[: max_pages]:
            pix = page.get_pixmap(dpi=120)
            images.append(pix.tobytes("png"))
    return images


def _docx_text(docx_path: Path) -> str:
    from nda_automation.docx_text import extract_docx_text

    return extract_docx_text(docx_path.read_bytes())


def _build_prompt(docx_text: str) -> str:
    dims = "\n".join(f"  - {d}" for d in RUBRIC_DIMENSIONS)
    return (
        "You are a document-conversion fidelity judge. The attached images are the "
        "rendered pages of an ORIGINAL PDF. Below is the TEXT extracted from a DOCX "
        "produced by a PDF->DOCX converter for the same document. Score how faithfully "
        "the converted DOCX preserves the original, on each dimension 0 (lost) to 5 "
        "(perfect):\n"
        f"{dims}\n\n"
        "Respond with STRICT JSON only: "
        '{"scores": {"<dim>": <int 0-5>, ...}, "notes": {"<dim>": "<short note>", ...}}.\n\n'
        "Converted DOCX extracted text (may be truncated):\n"
        "------------------------------------------------\n"
        f"{docx_text[:8000]}"
    )


def _call_openrouter(prompt: str, page_images: list[bytes], model: str) -> dict:
    from nda_automation.ai_review import OPENROUTER_CHAT_COMPLETIONS_ENDPOINT

    content: list[dict] = [{"type": "text", "text": prompt}]
    for png in page_images:
        b64 = base64.b64encode(png).decode("ascii")
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
        )
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        OPENROUTER_CHAT_COMPLETIONS_ENDPOINT,
        data=body,
        headers={
            "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        payload = json.loads(resp.read())
    text = payload["choices"][0]["message"]["content"]
    return _parse_json_block(text)


def _parse_json_block(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start : end + 1])


def score_docx(docx_path: Path, corpus_dir: Path, model: str) -> IntrinsicScore:
    engine = _engine_from_name(docx_path)
    pdf = _source_pdf_for(docx_path, corpus_dir)
    if pdf is None:
        return IntrinsicScore(
            docx=docx_path.name, engine=engine, status="failed",
            error=f"no source PDF in {corpus_dir} for {docx_path.name}",
        )
    try:
        images = _render_pdf_pages(pdf, MAX_JUDGE_PAGES)
        text = _docx_text(docx_path)
        result = _call_openrouter(_build_prompt(text), images, model)
        scores = {d: int(result.get("scores", {}).get(d, 0)) for d in RUBRIC_DIMENSIONS}
        notes = {d: str(result.get("notes", {}).get(d, "")) for d in RUBRIC_DIMENSIONS}
        overall = round(sum(scores.values()) / len(scores), 3)
        return IntrinsicScore(
            docx=docx_path.name, engine=engine, status="ok",
            scores=scores, notes=notes, overall=overall,
        )
    except Exception as exc:
        return IntrinsicScore(
            docx=docx_path.name, engine=engine, status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )


def score_outputs(output_dir: Path, corpus_dir: Path, model: str) -> list[IntrinsicScore]:
    available, reason = judge_available()
    docxs = sorted(output_dir.glob("*__*.docx"))
    scores: list[IntrinsicScore] = []
    for docx in docxs:
        engine = _engine_from_name(docx)
        if not available:
            scores.append(
                IntrinsicScore(docx=docx.name, engine=engine, status="skipped", skip_reason=reason)
            )
            print(f"SKIPPED: intrinsic judge on {docx.name} — {reason}")
            continue
        score = score_docx(docx, corpus_dir, model)
        scores.append(score)
        if score.status == "ok":
            print(f"OK:     {docx.name} — overall {score.overall}/5 {score.scores}")
        else:
            print(f"FAILED: {docx.name} — {score.error}")
    if not docxs:
        print(f"WARNING: no converted DOCX (*__*.docx) found in {output_dir}", file=sys.stderr)
    return scores


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="score_intrinsic")
    here = Path(__file__).resolve().parent
    parser.add_argument("--out", type=Path, default=here / "outputs")
    parser.add_argument("--corpus", type=Path, default=here / "corpus")
    parser.add_argument("--model", default=DEFAULT_JUDGE_MODEL)
    args = parser.parse_args(argv)

    scores = score_outputs(args.out, args.corpus, args.model)
    path = args.out / "intrinsic_scores.json"
    path.write_text(json.dumps([asdict(s) for s in scores], indent=2), encoding="utf-8")
    print(f"\nWrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
