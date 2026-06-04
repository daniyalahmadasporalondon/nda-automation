"""Counsel-labeled eval harness — measure the system against real legal judgment.

Report-only. NOT a CI gate (keep it report-only until the label set is stable and
counsel-approved), and it makes NO model or threshold changes — it only measures.

Where the deterministic fixture eval scores authored traps and the real-API eval
checks "does the active engine behave sensibly on traps we already know about", THIS asks
the only question that yields real rates: "are our decisions right on real NDAs,
as judged by counsel?"

It ingests:
  * a corpus of real NDA documents      (<corpus>/documents/<doc_id>.{docx,pdf,txt})
  * counsel labels                       (<corpus>/labels.json or labels.csv)
and compares three modes against the counsel labels:
  * Deterministic baseline (rules engine directly, AI overlay off)
  * Active engine          (current runtime engine, normally AI-first/fail-closed) [if a key is configured]

Metrics (per the agreed readout):
  false clears / false flags / review misses / review noise, per-clause accuracy,
  citation overlap with counsel citations, and active-engine change usefulness.

Confidentiality: real NDAs and counsel labels are client-sensitive and MUST NOT be
committed. Point the harness at an out-of-tree corpus:

    NDA_COUNSEL_EVAL_DIR=/path/to/corpus python -m tests.counsel_eval

With no corpus configured it runs the bundled SYNTHETIC example so the harness is
demonstrable without real data.
"""
from __future__ import annotations

import csv
import json
import os
from collections import Counter, defaultdict
from pathlib import Path

from tests.real_api_eval import active_engine_review, build_real_ai_first_review_func, deterministic_review

DECISIONS = ("pass", "review", "fail")
EXAMPLE_DIR = Path(__file__).parent / "fixtures" / "counsel_eval_example"


# --------------------------------------------------------------------------- #
# Ingestion
# --------------------------------------------------------------------------- #
def corpus_dir() -> Path:
    configured = os.environ.get("NDA_COUNSEL_EVAL_DIR", "").strip()
    return Path(configured).expanduser() if configured else EXAMPLE_DIR


def load_labels(path: Path) -> list[dict]:
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        return [_normalize_label(row) for row in rows]
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError("counsel labels JSON must be a list of label records")
    return [_normalize_label(row) for row in payload]


def _normalize_label(row: dict) -> dict:
    cited = row.get("cited_paragraph_ids")
    if isinstance(cited, str):
        cited = [piece.strip() for piece in cited.replace(";", ",").split(",") if piece.strip()]
    elif not isinstance(cited, list):
        cited = []
    return {
        "document_id": str(row.get("document_id") or "").strip(),
        "clause_id": str(row.get("clause_id") or "").strip(),
        "expected_decision": str(row.get("expected_decision") or "").strip().lower(),
        "cited_paragraph_ids": [str(c).strip() for c in cited if str(c).strip()],
        "cited_text": str(row.get("cited_text") or ""),
        "legal_reason": str(row.get("legal_reason") or ""),
        "label_source": str(row.get("label_source") or "counsel"),
        "confidence": str(row.get("confidence") or ""),
        "notes": str(row.get("notes") or ""),
    }


def load_documents(documents_dir: Path) -> dict[str, str]:
    """Return {document_id: extracted_text} for every file under documents/."""
    documents: dict[str, str] = {}
    if not documents_dir.is_dir():
        return documents
    for path in sorted(documents_dir.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        suffix = path.suffix.lower()
        if suffix == ".txt":
            documents[path.stem] = path.read_text(encoding="utf-8")
        elif suffix in {".docx", ".pdf"}:
            from nda_automation.ingestion_service import extract_document_paragraphs

            _type, paragraphs = extract_document_paragraphs(path.name, path.read_bytes())
            documents[path.stem] = "\n\n".join(str(p.get("text", "")) for p in paragraphs)
    return documents


# --------------------------------------------------------------------------- #
# Observation (run the system on each document)
# --------------------------------------------------------------------------- #
def _system_cited_ids(clause: dict) -> set[str]:
    ids: set[str] = set()
    evidence = clause.get("structured_evidence")
    if isinstance(evidence, list):
        for record in evidence:
            if isinstance(record, dict) and record.get("paragraph_id"):
                ids.add(str(record["paragraph_id"]))
    matched = clause.get("matched_paragraph_ids")
    if isinstance(matched, list):
        ids.update(str(pid) for pid in matched if pid)
    return ids


def _clause_by_id(result: dict, clause_id: str) -> dict | None:
    for clause in result.get("clauses", []):
        if str(clause.get("id")) == clause_id:
            return clause
    return None


def observe(documents: dict[str, str], ai_first_review_func) -> dict[tuple[str, str], dict]:
    """Map (document_id, clause_id) -> the system's decisions across modes."""
    observations: dict[tuple[str, str], dict] = {}
    for document_id, text in documents.items():
        baseline_result = deterministic_review(text)
        active_result = None
        if ai_first_review_func is not None:
            try:
                active_result = active_engine_review(text, ai_first_review_func)
            except Exception:  # noqa: BLE001 - provider/network error -> active-engine columns absent for this doc
                active_result = None
        for clause in baseline_result.get("clauses", []):
            clause_id = str(clause.get("id"))
            active_clause = _clause_by_id(active_result, clause_id) if active_result else None
            evidence_clause = active_clause if isinstance(active_clause, dict) else clause
            observations[(document_id, clause_id)] = {
                "baseline_decision": str(clause.get("decision") or ""),
                "active_decision": str(active_clause.get("decision") or "") if active_clause else "",
                "cited_ids": _system_cited_ids(evidence_clause),
            }
    return observations


# --------------------------------------------------------------------------- #
# Comparison against counsel labels (pure — unit-tested without the model)
# --------------------------------------------------------------------------- #
def _mode_metrics(triples: list[tuple[str, str, str]]) -> dict:
    """triples: (clause_id, counsel_decision, system_decision)."""
    confusion: Counter = Counter()
    by_clause: dict[str, dict[str, int]] = defaultdict(lambda: {"correct": 0, "total": 0})
    for clause_id, counsel, system in triples:
        confusion[(counsel, system)] += 1
        by_clause[clause_id]["total"] += 1
        if counsel == system:
            by_clause[clause_id]["correct"] += 1
    total = len(triples)
    correct = sum(count for (counsel, system), count in confusion.items() if counsel == system)
    return {
        "total": total,
        "correct": correct,
        "accuracy": (correct / total) if total else 0.0,
        "false_clears": confusion.get(("fail", "pass"), 0),     # missed a hard problem
        "review_misses": confusion.get(("review", "pass"), 0),  # missed a needs-review
        "false_flags": confusion.get(("pass", "fail"), 0),      # over-blocked a clean clause
        "review_noise": confusion.get(("pass", "review"), 0),   # unnecessary review
        "confusion": {f"{c}->{s}": n for (c, s), n in sorted(confusion.items())},
        "by_clause": {cid: dict(stats) for cid, stats in sorted(by_clause.items())},
    }


def compare(observations: dict[tuple[str, str], dict], labels: list[dict]) -> dict:
    baseline_triples: list[tuple[str, str, str]] = []
    active_triples: list[tuple[str, str, str]] = []
    citation_overlaps: list[float] = []
    citation_hits = 0
    citation_labeled = 0
    changes = useful_changes = noise_changes = 0
    unmatched: list[dict] = []

    for label in labels:
        key = (label["document_id"], label["clause_id"])
        obs = observations.get(key)
        counsel = label["expected_decision"]
        if obs is None or counsel not in DECISIONS:
            unmatched.append(label)
            continue

        baseline_triples.append((label["clause_id"], counsel, obs["baseline_decision"]))
        if obs["active_decision"]:
            active_triples.append((label["clause_id"], counsel, obs["active_decision"]))

        # citation overlap (only for labels that cite paragraphs)
        counsel_ids = set(label["cited_paragraph_ids"])
        if counsel_ids:
            citation_labeled += 1
            system_ids = obs["cited_ids"]
            union = counsel_ids | system_ids
            citation_overlaps.append(len(counsel_ids & system_ids) / len(union) if union else 0.0)
            if counsel_ids & system_ids:
                citation_hits += 1

        # change usefulness: where the active engine moved off the deterministic verdict
        if obs["active_decision"] and obs["active_decision"] != obs["baseline_decision"]:
            changes += 1
            if counsel in {"review", "fail"}:
                useful_changes += 1
            else:
                noise_changes += 1

    modes = {"deterministic_baseline": _mode_metrics(baseline_triples)}
    if active_triples:
        modes["active_engine"] = _mode_metrics(active_triples)

    return {
        "labels": len(labels),
        "scored": len(baseline_triples),
        "unmatched": unmatched,
        "modes": modes,
        "citation": {
            "labeled": citation_labeled,
            "hit_rate": (citation_hits / citation_labeled) if citation_labeled else None,
            "mean_jaccard": (sum(citation_overlaps) / len(citation_overlaps)) if citation_overlaps else None,
        },
        "active_engine_changes": {
            "changes": changes,
            "useful": useful_changes,
            "noise": noise_changes,
        },
    }


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def format_report(provider: str, key_source: str, results: dict) -> str:
    lines = []
    lines.append(f"Counsel-labeled eval — labels={results['labels']} scored={results['scored']}")
    lines.append(f"(Active engine uses provider={provider or '?'} key={key_source or 'NONE'}; report-only, no gate)")
    lines.append("")
    header = f"{'mode':22}{'acc':>6}{'false_clear':>13}{'review_miss':>13}{'false_flag':>12}{'review_noise':>14}"
    lines.append(header)
    for mode, label in (
        ("deterministic_baseline", "Deterministic"),
        ("active_engine", "Active engine"),
    ):
        metrics = results["modes"].get(mode)
        if not metrics:
            continue
        lines.append(
            f"{label:22}{metrics['accuracy'] * 100:>5.0f}%{metrics['false_clears']:>13}"
            f"{metrics['review_misses']:>13}{metrics['false_flags']:>12}{metrics['review_noise']:>14}"
        )

    cit = results["citation"]
    if cit["labeled"]:
        hit = f"{cit['hit_rate'] * 100:.0f}%" if cit["hit_rate"] is not None else "n/a"
        jac = f"{cit['mean_jaccard']:.2f}" if cit["mean_jaccard"] is not None else "n/a"
        lines.append("")
        lines.append(f"Citation overlap with counsel ({cit['labeled']} cited labels): hit-rate {hit}, mean Jaccard {jac}")

    dis = results["active_engine_changes"]
    lines.append("")
    lines.append(
        f"Active-engine change usefulness: {dis['changes']} active-engine changes "
        f"-> useful (counsel review/fail): {dis['useful']}, noise (counsel pass): {dis['noise']}"
    )

    if results["unmatched"]:
        lines.append("")
        lines.append(f"Unmatched/invalid labels (no review output or bad decision): {len(results['unmatched'])}")
        for label in results["unmatched"][:10]:
            lines.append(f"    ? {label['document_id']}::{label['clause_id']} (expected={label['expected_decision'] or '∅'})")
    return "\n".join(lines)


def run(corpus: Path, ai_first_review_func) -> dict:
    documents = load_documents(corpus / "documents")
    labels_path = corpus / "labels.json"
    if not labels_path.is_file():
        labels_path = corpus / "labels.csv"
    labels = load_labels(labels_path) if labels_path.is_file() else []
    observations = observe(documents, ai_first_review_func)
    return compare(observations, labels)


def main() -> int:
    corpus = corpus_dir()
    if corpus == EXAMPLE_DIR:
        print("NDA_COUNSEL_EVAL_DIR not set — running the bundled SYNTHETIC example.")
        print("Point it at a real corpus (documents/ + labels.json) for counsel-labeled measurement.\n")

    ai_first_review_func, provider, key_source, _model = build_real_ai_first_review_func()
    if ai_first_review_func is None:
        print(f"No AI provider configured (provider={provider or '?'}, key={key_source or 'NONE'}); "
              "active-engine columns will be omitted.\n")

    results = run(corpus, ai_first_review_func)
    print(format_report(provider, key_source, results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
