"""Aggregate the three scoring layers into one per-engine report.

Layer 3 (operational) is computed here from results.json (latency, success rate,
output size). Cost/doc and data-handling terms are intentionally left as
clearly-labeled PLACEHOLDERS to be filled per engine from each vendor's pricing
and DPA -- they cannot be derived from a conversion run.

Reads (whichever exist) in the outputs dir:
  results.json           (runner -- operational + conversion success)
  downstream_scores.json (score_downstream -- the key metric)
  intrinsic_scores.json  (score_intrinsic -- LLM fidelity)

Usage:
    python -m tools.pdf_convert_bakeoff.report --out tools/pdf_convert_bakeoff/outputs
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean

# Fill these per engine from vendor pricing / DPA. NOT derivable from a run.
OPERATIONAL_PLACEHOLDERS = {
    "pdf2docx": {"cost_per_doc_usd": "0.00 (in-process, compute only)", "data_handling": "local / in-process; no data leaves the host"},
    "adobe": {"cost_per_doc_usd": "TODO: per Adobe PDF Services pricing tier", "data_handling": "TODO: Adobe DPA; assets transiently stored ~24h"},
    "cloudmersive": {"cost_per_doc_usd": "TODO: per Cloudmersive plan (or $0 self-hosted)", "data_handling": "TODO: public API vs self-hosted container DPA"},
    "ilovepdf": {"cost_per_doc_usd": "TODO: per iLoveAPI plan/credits", "data_handling": "TODO: iLoveAPI DPA; files auto-deleted after task TTL"},
}


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def build_report(output_dir: Path) -> dict:
    results = _load(output_dir / "results.json")
    downstream = _load(output_dir / "downstream_scores.json") or []
    intrinsic = _load(output_dir / "intrinsic_scores.json") or []

    engines: dict[str, dict] = {}

    def eng(name: str) -> dict:
        return engines.setdefault(
            name,
            {
                "engine": name,
                "operational": {**OPERATIONAL_PLACEHOLDERS.get(name, {})},
                "downstream": {},
                "intrinsic": {},
            },
        )

    # Layer 3: operational (from results.json conversion records).
    if results:
        by_engine: dict[str, list[dict]] = {}
        for rec in results.get("records", []):
            by_engine.setdefault(rec["engine"], []).append(rec)
        for name, recs in by_engine.items():
            ok = [r for r in recs if r["status"] == "ok"]
            attempted = [r for r in recs if r["status"] != "skipped"]
            latencies = [r["latency_seconds"] for r in ok if r.get("latency_seconds") is not None]
            sizes = [r["output_bytes"] for r in ok if r.get("output_bytes") is not None]
            op = eng(name)["operational"]
            op["docs_attempted"] = len(attempted)
            op["docs_ok"] = len(ok)
            op["success_rate"] = round(len(ok) / len(attempted), 4) if attempted else None
            op["avg_latency_seconds"] = round(mean(latencies), 3) if latencies else None
            op["avg_output_bytes"] = int(mean(sizes)) if sizes else None
            if all(r["status"] == "skipped" for r in recs):
                op["note"] = recs[0].get("skip_reason", "skipped")

    # Layer 2: downstream (averaged per engine).
    ds_by_engine: dict[str, list[dict]] = {}
    for row in downstream:
        if row["status"] == "ok":
            ds_by_engine.setdefault(row["engine"], []).append(row)
    for name, rows in ds_by_engine.items():
        d = eng(name)["downstream"]
        d["docs_scored"] = len(rows)
        d["avg_clauses_present"] = round(mean(r["clauses_present"] for r in rows), 2)
        d["avg_structure_sections"] = round(mean(r["structure_sections"] for r in rows), 2)
        rates = [r["anchor_success_rate"] for r in rows if r["anchor_success_rate"] is not None]
        d["avg_anchor_success_rate"] = round(mean(rates), 4) if rates else None

    # Layer 1: intrinsic (averaged per engine).
    in_by_engine: dict[str, list[dict]] = {}
    for row in intrinsic:
        if row["status"] == "ok":
            in_by_engine.setdefault(row["engine"], []).append(row)
    for name, rows in in_by_engine.items():
        i = eng(name)["intrinsic"]
        i["docs_scored"] = len(rows)
        i["avg_overall"] = round(mean(r["overall"] for r in rows), 3)
        dims = rows[0]["scores"].keys()
        i["avg_by_dimension"] = {
            d: round(mean(r["scores"][d] for r in rows), 2) for d in dims
        }

    return {"output_dir": str(output_dir), "engines": list(engines.values())}


def _render_table(report: dict) -> str:
    lines = [
        f"{'ENGINE':<13}  {'OK/ATT':>7}  {'SUCC':>6}  {'LAT(s)':>7}  {'BYTES':>9}  "
        f"{'ANCHOR':>7}  {'CLAUSES':>8}  {'SECT':>5}  {'FIDELITY':>9}",
    ]
    for e in report["engines"]:
        op, d, i = e["operational"], e["downstream"], e["intrinsic"]
        ok_att = f"{op.get('docs_ok', '-')}/{op.get('docs_attempted', '-')}"
        succ = op.get("success_rate")
        lat = op.get("avg_latency_seconds")
        size = op.get("avg_output_bytes")
        anchor = d.get("avg_anchor_success_rate")
        clauses = d.get("avg_clauses_present")
        sect = d.get("avg_structure_sections")
        fid = i.get("avg_overall")
        lines.append(
            f"{e['engine']:<13}  {ok_att:>7}  {str(succ):>6}  {str(lat):>7}  "
            f"{str(size):>9}  {str(anchor):>7}  {str(clauses):>8}  {str(sect):>5}  {str(fid):>9}"
        )
    lines.append("")
    lines.append("ANCHOR = avg redline-anchor success rate (the key downstream metric).")
    lines.append("FIDELITY = avg LLM-judge overall /5. cost/data-handling: see report.json placeholders.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="report")
    here = Path(__file__).resolve().parent
    parser.add_argument("--out", type=Path, default=here / "outputs")
    args = parser.parse_args(argv)

    report = build_report(args.out)
    path = args.out / "report.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(_render_table(report))
    print(f"\nWrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
