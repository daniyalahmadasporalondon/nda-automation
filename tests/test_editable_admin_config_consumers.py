"""Consumer rewiring tests for the editable admin configs.

Proves the stored overrides actually REACH the code that consumes them:

  * Config #2: a stored gmail.inbound_base_query reaches
    gmail_integration._default_inbound_query(); reset falls back to the constant.
  * Config #3: a stored review_runtime.ai_review_clause_ids subset constrains
    ai_review._targeted_clause_ids; an unknown stored id is dropped by the live
    intersection (no drift); an empty override yields the full live set.
"""

from __future__ import annotations

import pytest

from nda_automation import ai_review, app_settings, gmail_integration, matter_store


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    # Keep the env override out of the way so the stored-override path is exercised.
    monkeypatch.delenv("NDA_AI_REVIEW_CLAUSES", raising=False)
    yield


# --- Config #2: _default_inbound_query honors the stored override ----------
def test_default_inbound_query_uses_stored_override():
    app_settings.update_gmail_settings({"inbound_base_query": "in:inbox custom newer_than:7d"})
    assert gmail_integration._default_inbound_query() == "in:inbox custom newer_than:7d"


def test_default_inbound_query_falls_back_to_constant_when_unset():
    assert gmail_integration._default_inbound_query() == gmail_integration.GMAIL_INBOUND_BASE_QUERY


def test_default_inbound_query_reset_restores_constant():
    app_settings.update_gmail_settings({"inbound_base_query": "in:inbox custom"})
    app_settings.reset_gmail_inbound_base_query()
    assert gmail_integration._default_inbound_query() == gmail_integration.GMAIL_INBOUND_BASE_QUERY


# --- Config #3: stored clause subset constrains _targeted_clause_ids -------
def test_stored_clause_subset_constrains_targeted_clause_ids():
    live = {"confidentiality", "term", "governing_law", "non_circumvention"}
    app_settings.update_review_runtime_settings(
        {"ai_review_clause_ids": ["confidentiality", "term"]}
    )
    settings = ai_review._ai_review_settings()
    targeted = ai_review._targeted_clause_ids(settings, live)
    assert targeted == {"confidentiality", "term"}


def test_unknown_stored_clause_id_dropped_by_live_intersection():
    # A stored id later removed from the playbook is silently dropped (no drift).
    live = {"confidentiality", "term"}
    app_settings.update_review_runtime_settings(
        {"ai_review_clause_ids": ["confidentiality", "ghost_removed_clause"]}
    )
    settings = ai_review._ai_review_settings()
    targeted = ai_review._targeted_clause_ids(settings, live)
    assert targeted == {"confidentiality"}


def test_empty_override_yields_full_live_set():
    live = {"confidentiality", "term", "governing_law"}
    assert app_settings.ai_review_clause_ids() == []
    settings = ai_review._ai_review_settings()
    targeted = ai_review._targeted_clause_ids(settings, live)
    assert targeted == live


def test_stored_override_wins_over_env(monkeypatch):
    monkeypatch.setenv("NDA_AI_REVIEW_CLAUSES", "term")
    live = {"confidentiality", "term"}
    app_settings.update_review_runtime_settings({"ai_review_clause_ids": ["confidentiality"]})
    settings = ai_review._ai_review_settings()
    targeted = ai_review._targeted_clause_ids(settings, live)
    assert targeted == {"confidentiality"}  # stored override beat the env "term"


def test_env_used_when_no_stored_override(monkeypatch):
    monkeypatch.setenv("NDA_AI_REVIEW_CLAUSES", "term")
    live = {"confidentiality", "term"}
    settings = ai_review._ai_review_settings()
    targeted = ai_review._targeted_clause_ids(settings, live)
    assert targeted == {"term"}
