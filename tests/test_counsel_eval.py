"""Unit tests for the counsel-eval measurement logic.

These test the metric computation (deterministic, no model) and an offline smoke
run on the bundled synthetic example. They do NOT gate on counsel labels.
"""
from __future__ import annotations

from tests.counsel_eval import (
    EXAMPLE_DIR,
    _mode_metrics,
    compare,
    load_labels,
    run,
)


def test_mode_metrics_names_each_error_cell():
    triples = [
        ("governing_law", "fail", "pass"),       # false clear
        ("governing_law", "review", "pass"),     # review miss
        ("mutuality", "pass", "fail"),           # false flag
        ("confidential_information", "pass", "review"),  # review noise
        ("term_and_survival", "pass", "pass"),   # correct
        ("non_circumvention", "fail", "fail"),   # correct
    ]
    m = _mode_metrics(triples)
    assert m["total"] == 6
    assert m["correct"] == 2
    assert abs(m["accuracy"] - 2 / 6) < 1e-9
    assert m["false_clears"] == 1
    assert m["review_misses"] == 1
    assert m["false_flags"] == 1
    assert m["review_noise"] == 1
    assert m["by_clause"]["governing_law"] == {"correct": 0, "total": 2}


def test_compare_modes_citation_overlap_and_disagreement_usefulness():
    observations = {
        ("d1", "governing_law"): {
            "baseline_decision": "pass",
            "active_decision": "review",
            "cited_ids": {"p3"},
        },
        ("d1", "confidential_information"): {
            "baseline_decision": "pass",
            "active_decision": "pass",
            "cited_ids": {"p9"},
        },
    }
    labels = [
        {"document_id": "d1", "clause_id": "governing_law", "expected_decision": "fail",
         "cited_paragraph_ids": ["p3", "p4"]},
        {"document_id": "d1", "clause_id": "confidential_information", "expected_decision": "pass",
         "cited_paragraph_ids": []},
    ]
    results = compare(observations, labels)

    assert results["scored"] == 2
    # Deterministic baseline: GL fail->pass is a false clear; CI pass->pass correct.
    baseline = results["modes"]["deterministic_baseline"]
    assert baseline["false_clears"] == 1
    assert baseline["correct"] == 1
    # Active engine: GL fail->review (no longer a false clear), CI correct.
    active = results["modes"]["active_engine"]
    assert active["false_clears"] == 0
    assert active["correct"] == 1

    # Citation overlap only over labels that cite paragraphs (the GL one).
    assert results["citation"]["labeled"] == 1
    assert results["citation"]["hit_rate"] == 1.0
    assert abs(results["citation"]["mean_jaccard"] - 0.5) < 1e-9  # {p3} ∩ {p3,p4} / union

    # The active engine escalated GL pass->review and counsel wanted fail -> useful, not noise.
    assert results["active_engine_changes"] == {"changes": 1, "useful": 1, "noise": 0}


def test_unmatched_label_is_reported_not_scored():
    observations = {("d1", "mutuality"): {
        "baseline_decision": "pass", "active_decision": "", "cited_ids": set()}}
    labels = [
        {"document_id": "d1", "clause_id": "mutuality", "expected_decision": "pass", "cited_paragraph_ids": []},
        {"document_id": "missing_doc", "clause_id": "mutuality", "expected_decision": "pass", "cited_paragraph_ids": []},
        {"document_id": "d1", "clause_id": "mutuality", "expected_decision": "", "cited_paragraph_ids": []},  # bad decision
    ]
    results = compare(observations, labels)
    assert results["scored"] == 1
    assert len(results["unmatched"]) == 2


def test_load_example_labels_normalizes():
    labels = load_labels(EXAMPLE_DIR / "labels.json")
    assert len(labels) == 4
    assert all(label["label_source"] == "counsel" for label in labels)
    gl = next(label for label in labels if label["clause_id"] == "governing_law" and label["document_id"] == "nda_alpha")
    assert gl["expected_decision"] == "fail"
    assert gl["cited_paragraph_ids"] == ["p3"]


def test_example_corpus_runs_deterministic_baseline_offline():
    # ai_first_review_func=None -> active-engine columns omitted; baseline still scores every label.
    results = run(EXAMPLE_DIR, ai_first_review_func=None)
    assert results["scored"] == 4
    assert "deterministic_baseline" in results["modes"]
    assert "active_engine" not in results["modes"]
