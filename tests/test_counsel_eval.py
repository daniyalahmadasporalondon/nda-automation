"""Unit tests for the counsel-eval measurement logic.

These test the metric computation (deterministic, no model) and a Python-only smoke
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
            "py_decision": "pass",
            "ai_final_decision": "review",   # AI escalated
            "ai_only_decision": "review",
            "cited_ids": {"p3"},
        },
        ("d1", "confidential_information"): {
            "py_decision": "pass",
            "ai_final_decision": "pass",
            "ai_only_decision": "pass",
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
    # Python only: GL fail->pass is a false clear; CI pass->pass correct.
    py = results["modes"]["python_only"]
    assert py["false_clears"] == 1
    assert py["correct"] == 1
    # Python + AI: GL fail->review (no longer a false clear), CI correct.
    ai = results["modes"]["python_plus_ai"]
    assert ai["false_clears"] == 0
    assert ai["correct"] == 1
    assert "ai_only_report" in results["modes"]

    # Citation overlap only over labels that cite paragraphs (the GL one).
    assert results["citation"]["labeled"] == 1
    assert results["citation"]["hit_rate"] == 1.0
    assert abs(results["citation"]["mean_jaccard"] - 0.5) < 1e-9  # {p3} ∩ {p3,p4} / union

    # The AI escalated GL pass->review and counsel wanted fail -> useful, not noise.
    assert results["disagreement"] == {"escalations": 1, "useful": 1, "noise": 0}


def test_unmatched_label_is_reported_not_scored():
    observations = {("d1", "mutuality"): {
        "py_decision": "pass", "ai_final_decision": "", "ai_only_decision": "", "cited_ids": set()}}
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


def test_example_corpus_runs_python_only_offline():
    # reviewer=None -> AI columns omitted; Python-only must still score every label.
    results = run(EXAMPLE_DIR, reviewer=None)
    assert results["scored"] == 4
    assert "python_only" in results["modes"]
    assert "python_plus_ai" not in results["modes"]
