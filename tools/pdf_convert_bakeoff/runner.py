"""Bake-off runner: run every available engine on every corpus PDF.

For each (doc, engine) it records success/failure, wall-clock latency, output
size, and any error, writes ``<doc>__<engine>.docx`` into the outputs dir, and
emits ``results.json`` + a human-readable summary table. One engine/doc failure
never aborts the run (bounded + resilient).

Usage:
    python -m tools.pdf_convert_bakeoff.runner convert \
        --corpus tools/pdf_convert_bakeoff/corpus \
        --out    tools/pdf_convert_bakeoff/outputs
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import sys
import time
import traceback

from .engines import ENGINES


@dataclass
class ConversionRecord:
    doc: str
    engine: str
    status: str  # "ok" | "failed" | "skipped"
    latency_seconds: float | None = None
    output_path: str | None = None
    output_bytes: int | None = None
    error: str | None = None
    skip_reason: str | None = None


@dataclass
class RunResult:
    corpus_dir: str
    output_dir: str
    engines_available: dict[str, bool] = field(default_factory=dict)
    engine_skip_reasons: dict[str, str] = field(default_factory=dict)
    records: list[dict] = field(default_factory=list)


def _list_pdfs(corpus_dir: Path) -> list[Path]:
    return sorted(p for p in corpus_dir.glob("*.pdf") if p.is_file())


def _safe_stem(pdf: Path) -> str:
    return pdf.stem.replace(" ", "_")


def run_conversions(corpus_dir: Path, output_dir: Path) -> RunResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    result = RunResult(corpus_dir=str(corpus_dir), output_dir=str(output_dir))

    availability = {}
    for engine in ENGINES:
        ok, reason = engine.available()
        availability[engine.NAME] = (ok, reason)
        result.engines_available[engine.NAME] = ok
        if not ok:
            result.engine_skip_reasons[engine.NAME] = reason

    pdfs = _list_pdfs(corpus_dir)
    if not pdfs:
        print(f"WARNING: no .pdf files found in {corpus_dir}", file=sys.stderr)

    for pdf in pdfs:
        stem = _safe_stem(pdf)
        for engine in ENGINES:
            ok, reason = availability[engine.NAME]
            if not ok:
                rec = ConversionRecord(
                    doc=pdf.name,
                    engine=engine.NAME,
                    status="skipped",
                    skip_reason=reason,
                )
                print(f"SKIPPED: {engine.NAME} on {pdf.name} — {reason}")
                result.records.append(asdict(rec))
                continue

            out_path = output_dir / f"{stem}__{engine.NAME}.docx"
            start = time.monotonic()
            try:
                engine.convert(pdf, out_path)
                latency = time.monotonic() - start
                size = out_path.stat().st_size if out_path.exists() else 0
                rec = ConversionRecord(
                    doc=pdf.name,
                    engine=engine.NAME,
                    status="ok",
                    latency_seconds=round(latency, 3),
                    output_path=str(out_path),
                    output_bytes=size,
                )
                print(f"OK:      {engine.NAME} on {pdf.name} — {latency:.2f}s, {size} bytes")
            except Exception as exc:  # bounded: never aborts the whole run
                latency = time.monotonic() - start
                rec = ConversionRecord(
                    doc=pdf.name,
                    engine=engine.NAME,
                    status="failed",
                    latency_seconds=round(latency, 3),
                    error=f"{type(exc).__name__}: {exc}",
                )
                print(f"FAILED:  {engine.NAME} on {pdf.name} — {type(exc).__name__}: {exc}")
                traceback.print_exc(file=sys.stderr)
            result.records.append(asdict(rec))

    return result


def _write_results(result: RunResult, output_dir: Path) -> Path:
    path = output_dir / "results.json"
    path.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
    return path


def _summary_table(result: RunResult) -> str:
    rows = result.records
    width_doc = max([len("DOC")] + [len(r["doc"]) for r in rows], default=3)
    width_eng = max([len("ENGINE")] + [len(r["engine"]) for r in rows], default=6)
    lines = [
        f"{'DOC':<{width_doc}}  {'ENGINE':<{width_eng}}  {'STATUS':<8}  {'LATENCY':>8}  {'BYTES':>9}  NOTE",
    ]
    for r in rows:
        latency = f"{r['latency_seconds']:.2f}s" if r["latency_seconds"] is not None else "-"
        size = str(r["output_bytes"]) if r["output_bytes"] is not None else "-"
        note = r.get("error") or r.get("skip_reason") or ""
        lines.append(
            f"{r['doc']:<{width_doc}}  {r['engine']:<{width_eng}}  "
            f"{r['status']:<8}  {latency:>8}  {size:>9}  {note}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pdf_convert_bakeoff")
    sub = parser.add_subparsers(dest="command", required=True)

    conv = sub.add_parser("convert", help="run all available engines on the corpus")
    here = Path(__file__).resolve().parent
    conv.add_argument("--corpus", type=Path, default=here / "corpus")
    conv.add_argument("--out", type=Path, default=here / "outputs")

    args = parser.parse_args(argv)
    if args.command == "convert":
        result = run_conversions(args.corpus, args.out)
        path = _write_results(result, args.out)
        print("\n" + _summary_table(result))
        print(f"\nWrote {path}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
