from __future__ import annotations

from nda_automation import checker
from nda_automation.review_orchestration import ReviewCommand


def test_checker_review_nda_delegates_to_review_command(monkeypatch):
    captured: dict[str, ReviewCommand] = {}

    def fake_orchestrate(command: ReviewCommand) -> dict[str, object]:
        captured["command"] = command
        return {"review_mode": "captured"}

    semantic_evaluator = object()
    ai_reviewer = object()
    ai_verifier = object()
    playbook = {"clauses": []}
    paragraphs = [{"id": "p1", "text": "Clause text."}]

    monkeypatch.setattr(checker, "orchestrate_review", fake_orchestrate)

    result = checker.review_nda(
        "Clause text.",
        paragraphs=paragraphs,
        playbook=playbook,
        semantic_evaluator=semantic_evaluator,
        ai_reviewer=ai_reviewer,
        ai_verifier=ai_verifier,
        verify=False,
        ai_enabled=False,
    )

    assert result == {"review_mode": "captured"}
    assert captured["command"] == ReviewCommand(
        text="Clause text.",
        paragraphs=paragraphs,
        playbook=playbook,
        semantic_evaluator=semantic_evaluator,
        ai_reviewer=ai_reviewer,
        ai_verifier=ai_verifier,
        verify=False,
        ai_enabled=False,
    )
