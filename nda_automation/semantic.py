from __future__ import annotations

import importlib
import os
from functools import lru_cache
from typing import Callable, Dict, List, Optional

from .checks.common import (
    ISSUE_TYPE_PRESENT_BUT_WRONG,
    ClauseResult,
    Paragraph,
    _check,
    _match,
)

SEMANTIC_EVALUATOR_ENV = "NDA_SEMANTIC_EVALUATOR"
SemanticEvaluateFn = Callable[..., Optional[Dict[str, object]]]


def apply_semantic_fallback(
    *,
    text: str,
    normalized: str,
    clause: Dict[str, object],
    paragraphs: List[Paragraph],
    current_result: ClauseResult,
    evaluator: SemanticEvaluateFn | None = None,
) -> ClauseResult:
    if not _should_run_semantic_fallback(clause, current_result):
        return current_result

    semantic_evaluator = evaluator or _load_configured_semantic_evaluator()
    if semantic_evaluator is None:
        return current_result

    try:
        decision = semantic_evaluator(
            text=text,
            normalized=normalized,
            clause=dict(clause),
            paragraphs=[dict(paragraph) for paragraph in paragraphs],
            current_result=dict(current_result),
        )
    except Exception:
        return current_result

    if not isinstance(decision, dict):
        return current_result

    semantic_result = _semantic_clause_result(clause, paragraphs, current_result, decision)
    if not semantic_result:
        return current_result
    semantic_result["semantic_fallback"] = True
    if "semantic_confidence" in decision:
        semantic_result["semantic_confidence"] = decision["semantic_confidence"]
    elif "confidence" in decision:
        semantic_result["semantic_confidence"] = decision["confidence"]
    for field in ["decision", "needs_review", "review_reason", "decision_reason"]:
        if field in decision:
            semantic_result[field] = decision[field]
    return semantic_result


def _should_run_semantic_fallback(clause: Dict[str, object], current_result: ClauseResult) -> bool:
    return current_result.get("status") == "not_present" and bool(clause.get("semantic_signals"))


@lru_cache(maxsize=1)
def _load_configured_semantic_evaluator() -> SemanticEvaluateFn | None:
    evaluator_path = os.environ.get(SEMANTIC_EVALUATOR_ENV, "").strip()
    if not evaluator_path:
        return None

    module_name, separator, attribute_name = evaluator_path.partition(":")
    attribute_name = attribute_name if separator else "evaluate_clause"
    if not module_name or not attribute_name:
        return None

    try:
        module = importlib.import_module(module_name)
        evaluator = getattr(module, attribute_name)
    except Exception:
        return None
    return evaluator if callable(evaluator) else None


def _semantic_clause_result(
    clause: Dict[str, object],
    paragraphs: List[Paragraph],
    current_result: ClauseResult,
    decision: Dict[str, object],
) -> ClauseResult | None:
    status = str(decision.get("status", "")).strip()
    reason = str(decision.get("reason") or "Semantic fallback found relevant clause evidence.").strip()
    matched_paragraphs = _matched_semantic_paragraphs(paragraphs, decision)

    if status == "match":
        if not matched_paragraphs:
            return None
        return _match(clause, reason, matched_paragraphs)
    if status == "check":
        if not matched_paragraphs:
            return None
        issue_type = str(decision.get("issue_type") or ISSUE_TYPE_PRESENT_BUT_WRONG)
        fallback_fix = str(current_result.get("what_to_fix") or "").strip()
        if fallback_fix == "No change needed.":
            fallback_fix = ""
        what_to_fix = str(decision.get("what_to_fix") or fallback_fix).strip()
        if what_to_fix:
            return _check(clause, reason, matched_paragraphs, issue_type=issue_type, what_to_fix=what_to_fix)
        return _check(clause, reason, matched_paragraphs, issue_type=issue_type)
    return None


def _matched_semantic_paragraphs(paragraphs: List[Paragraph], decision: Dict[str, object]) -> List[Paragraph]:
    paragraphs_by_id = {str(paragraph["id"]): paragraph for paragraph in paragraphs}
    paragraph_ids = decision.get("matched_paragraph_ids", [])
    if not isinstance(paragraph_ids, list):
        return []
    return [
        paragraph
        for paragraph_id in paragraph_ids
        if (paragraph := paragraphs_by_id.get(str(paragraph_id))) is not None
    ]
