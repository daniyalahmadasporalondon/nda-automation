"""The AI assistant answers corpus-wide count/search questions via a thin tool.

These tests prove the AI-ON path can now query the FULL owner-scoped corpus
(app-state + Drive-reconciled) with exact facet matching — the capability that
previously only fired when AI was OFF (see test_dashboard_assistant.py). The AI
client is mocked to emit the count_corpus_matches tool call; the tool does the
deterministic match over context.corpus_matters and the model phrases the answer.
"""
from __future__ import annotations

import json

from nda_automation import corpus_index, dashboard_assistant, dashboard_assistant_ai
from nda_automation.matter_repository import InMemoryMatterRepository


def _corpus_matter(
    matter_id,
    title,
    *,
    governing_law="",
    signed=None,
    has_clauses=None,
    status="",
    phase="",
    source="app",
    available=True,
):
    return {
        "matter_id": matter_id,
        "title": title,
        "counterparty": title.split()[0],
        "source": source,
        "facets": {
            "governing_law": governing_law,
            "signed": signed,
            "has_clauses": list(has_clauses or []),
            "term_years": None,
            "phase": phase,
            "status": status,
            "facets_available": available,
        },
    }


class _ToolThenPhraseModel:
    """A fake provider model: first turn emits the tool call, second turn phrases.

    The second turn reads the tool output the orchestrator appended as the last
    message (role=tool) and renders the final assistant_response from those facts —
    the thin-tool contract. ``filter_spec`` is whatever the test asks it to emit.
    """

    def __init__(self, filter_spec):
        self.filter_spec = filter_spec
        self.requests = []

    def __call__(self, request_body):
        self.requests.append(request_body)
        if len(self.requests) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call_corpus",
                                    "type": "function",
                                    "function": {
                                        "name": "count_corpus_matches",
                                        "arguments": json.dumps({"filter_spec": self.filter_spec}),
                                    },
                                }
                            ]
                        }
                    }
                ],
            }
        facts = json.loads(self.requests[1]["messages"][-1]["content"])
        noun = "NDA" if facts["count"] == 1 else "NDAs"
        return {
            "assistant_response": {
                "intent": "repository_question",
                "domain": "repository",
                "question": "count_corpus_matches",
                "answer": {
                    "text": f"{facts['count']} {noun} {facts['interpreted']}.",
                    "count": facts["count"],
                    "filters": facts["filters"],
                    "corpus_total": facts["corpus_total"],
                    "matches": facts["matches"],
                },
                "citations": [],
            }
        }


def test_count_corpus_tool_is_registered_in_the_ai_catalog():
    context = dashboard_assistant.AssistantContext(
        "how many unsigned DIFC NDAs",
        repository=InMemoryMatterRepository(),
    )

    tools = dashboard_assistant_ai.dashboard_assistant_tool_registry(context)

    assert "count_corpus_matches" in tools
    tool = tools["count_corpus_matches"]
    assert tool.domain == "repository"
    schema = tool.provider_tool()["function"]["parameters"]
    assert schema["additionalProperties"] is False
    assert "filter_spec" in schema["properties"]


def test_ai_answers_unsigned_difc_count_over_full_corpus_via_tool():
    # The headline: an AI-ON corpus-wide facet count, answered by the tool matching
    # over the full corpus (app-state + Drive-reconciled), then phrased by the model.
    corpus = [
        _corpus_matter("1", "Acme DIFC NDA", governing_law="difc", signed=True),
        _corpus_matter("2", "Globex DIFC NDA", governing_law="difc", signed=False, source="both"),
        _corpus_matter("3", "Initech India NDA", governing_law="india", signed=False),
        # A legacy Drive matter with no facets -> excluded from facet counts.
        _corpus_matter("4", "Legacy NDA", source="drive", available=False),
    ]
    model = _ToolThenPhraseModel({"governing_law": "difc", "signed": False})

    response = dashboard_assistant.handle_dashboard_assistant_command(
        "how many unsigned DIFC NDAs",
        repository=InMemoryMatterRepository(),
        owner_user_id="tenant-a",
        corpus_provider=lambda: corpus,
        ai_model=model,
    )

    assert response["intent"] == "repository_question"
    assert response["question"] == "count_corpus_matches"
    assert response["answer"]["count"] == 1
    assert response["answer"]["filters"]["governing_law"] == "difc"
    assert response["answer"]["filters"]["signed"] is False
    # The corpus total proves the tool ran over the WHOLE corpus, including the
    # Drive-reconciled and legacy entries, not just app-state.
    assert response["answer"]["corpus_total"] == 4
    # The model was given a SECOND turn carrying the tool facts (thin-tool pattern):
    # the corpus was never dumped into the prompt context.
    assert len(model.requests) == 2
    assert model.requests[1]["messages"][-1]["role"] == "tool"


def test_ai_corpus_tool_sees_drive_only_matter_not_in_app_state():
    # A matter that exists ONLY in the Drive-reconciled corpus (in_app=False, not in
    # the repository) is counted by the tool — the capability the desk-only path lacked.
    drive_only = _corpus_matter(
        "drive-9",
        "Vance DIFC NDA",
        governing_law="difc",
        signed=True,
        source="drive",
    )
    model = _ToolThenPhraseModel({"governing_law": "difc", "signed": True})

    response = dashboard_assistant.handle_dashboard_assistant_command(
        "how many signed DIFC NDAs do we have",
        repository=InMemoryMatterRepository(),  # empty app-state on purpose
        owner_user_id="tenant-a",
        corpus_provider=lambda: [drive_only],
        ai_model=model,
    )

    assert response["answer"]["count"] == 1
    assert response["answer"]["matches"][0]["title"] == "Vance DIFC NDA"


def test_ai_corpus_tool_search_returns_matching_matters_capped():
    # The same tool answers a SEARCH ("which ...") by returning the matching matters,
    # not just a count. The match list is capped (MAX_CITATIONS) so the corpus is
    # never dumped wholesale.
    corpus = [
        _corpus_matter(str(i), f"India NDA {i}", governing_law="india", signed=True)
        for i in range(10)
    ]
    model = _ToolThenPhraseModel({"governing_law": "india", "signed": True})

    response = dashboard_assistant.handle_dashboard_assistant_command(
        "which signed India NDAs do we have",
        repository=InMemoryMatterRepository(),
        owner_user_id="tenant-a",
        corpus_provider=lambda: corpus,
        ai_model=model,
    )

    assert response["answer"]["count"] == 10
    assert len(response["answer"]["matches"]) == dashboard_assistant.MAX_CITATIONS


def test_corpus_tool_is_owner_scoped_to_the_signed_in_users_corpus(monkeypatch):
    # Owner-scoping holds: with no injected provider, the tool reads the corpus
    # corpus_index.build_corpus produces for the REQUEST owner ids. We assert the
    # build is invoked with exactly those owner ids (the same owner scope /api/corpus
    # uses), so another tenant's matters can never enter the match.
    seen = {}

    def fake_build_corpus(repository, owner_user_id, drive_owner_user_id, **_kwargs):
        seen["owner_user_id"] = owner_user_id
        seen["drive_owner_user_id"] = drive_owner_user_id
        return {
            "groups": [
                {
                    "counterparty": "Acme",
                    "matters": [_corpus_matter("a", "Acme India NDA", governing_law="india", signed=True)],
                }
            ]
        }

    monkeypatch.setattr(corpus_index, "build_corpus", fake_build_corpus)
    model = _ToolThenPhraseModel({"governing_law": "india", "signed": True})

    response = dashboard_assistant.handle_dashboard_assistant_command(
        "how many signed India NDAs",
        repository=InMemoryMatterRepository(),
        owner_user_id="tenant-a",
        drive_owner_user_id="google-tenant-a",
        ai_model=model,
    )

    assert seen["owner_user_id"] == "tenant-a"
    assert seen["drive_owner_user_id"] == "google-tenant-a"
    assert response["answer"]["count"] == 1


def test_ai_corpus_tool_falls_back_to_query_when_model_sends_empty_spec():
    # A model that emits an empty/all-null filter_spec must not collapse to counting
    # the whole corpus: the tool re-parses the NL query deterministically so the
    # answer still reflects the question.
    corpus = [
        _corpus_matter("1", "Acme DIFC NDA", governing_law="difc", signed=True),
        _corpus_matter("2", "Initech India NDA", governing_law="india", signed=True),
    ]
    model = _ToolThenPhraseModel({})

    response = dashboard_assistant.handle_dashboard_assistant_command(
        "how many DIFC NDAs",
        repository=InMemoryMatterRepository(),
        owner_user_id="tenant-a",
        corpus_provider=lambda: corpus,
        ai_model=model,
    )

    assert response["answer"]["filters"]["governing_law"] == "difc"
    assert response["answer"]["count"] == 1
