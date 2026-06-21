"""Store-layer unit tests for the three editable admin configs.

Covers, for the NET-NEW settings keys (Config #2 default inbound query, Config #3
AI-review clause-id override) and the existing Config #1 signal-term vocabulary:

  * normalizers substitute the right default on corrupt/missing/oversized input;
  * the readers resolve stored-override-first, fall back to the built-in default;
  * a persistence round-trip survives a re-read;
  * reset restores the constant / full-set sentinel;
  * a corrupt stored blob is read back as the default (fallback-on-corrupt) and
    the consumer-facing reader still works.
"""

from __future__ import annotations

import json

import pytest

from nda_automation import app_settings, gmail_integration, matter_store


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    yield


def _write_raw_settings(blob: dict) -> None:
    (matter_store.DATA_DIR / "app_settings.json").write_text(json.dumps(blob), encoding="utf-8")


# --- Config #2: default inbound query --------------------------------------
def test_inbound_base_query_normalizer_rejects_unsafe_input():
    norm = app_settings.gmail_inbound_base_query_from_payload
    assert norm("") == ""
    assert norm("   ") == ""
    assert norm(None) == ""
    assert norm(123) == ""
    assert norm("a\nb") == ""  # newline
    assert norm("a\tb") == ""  # control char
    assert norm("x" * (app_settings.MAX_GMAIL_INBOUND_BASE_QUERY_LENGTH + 1)) == ""
    assert norm("  in:inbox foo  ") == "in:inbox foo"  # trimmed


def test_inbound_base_query_reader_default_is_the_constant():
    assert app_settings.gmail_inbound_base_query() == gmail_integration.GMAIL_INBOUND_BASE_QUERY


def test_inbound_base_query_override_round_trip_and_reader():
    app_settings.update_gmail_settings({"inbound_base_query": "in:inbox custom newer_than:5d"})
    assert app_settings.gmail_settings()["inbound_base_query"] == "in:inbox custom newer_than:5d"
    assert app_settings.gmail_inbound_base_query() == "in:inbox custom newer_than:5d"


def test_inbound_base_query_reset_restores_constant():
    app_settings.update_gmail_settings({"inbound_base_query": "in:inbox custom"})
    app_settings.reset_gmail_inbound_base_query()
    assert app_settings.gmail_settings()["inbound_base_query"] == ""
    assert app_settings.gmail_inbound_base_query() == gmail_integration.GMAIL_INBOUND_BASE_QUERY


def test_inbound_base_query_corrupt_blob_falls_back_to_constant():
    _write_raw_settings({"gmail": {"inbound_base_query": ["not", "a", "string"]}})
    assert app_settings.gmail_settings()["inbound_base_query"] == ""
    assert app_settings.gmail_inbound_base_query() == gmail_integration.GMAIL_INBOUND_BASE_QUERY


# --- Config #3: AI-review clause-id override --------------------------------
def test_clause_ids_normalizer_shape():
    norm = app_settings.ai_review_clause_ids_from_payload
    assert norm([]) == []
    assert norm(None) == []
    assert norm("confidentiality") == []  # non-list collapses to []
    assert norm(["a", "a", "b", 1, "", "  "]) == ["a", "b"]  # dedupe + drop junk
    assert norm([" term "]) == ["term"]  # trimmed
    big = ["x" * (app_settings.MAX_AI_REVIEW_CLAUSE_ID_LENGTH + 1)]
    assert norm(big) == []  # oversized id dropped


def test_clause_ids_reader_default_is_empty_full_set_sentinel():
    assert app_settings.ai_review_clause_ids() == []


def test_clause_ids_override_round_trip_and_reader():
    app_settings.update_review_runtime_settings({"ai_review_clause_ids": ["confidentiality", "term"]})
    assert app_settings.review_runtime_settings()["ai_review_clause_ids"] == ["confidentiality", "term"]
    assert app_settings.ai_review_clause_ids() == ["confidentiality", "term"]


def test_clause_ids_reset_to_empty_full_set():
    app_settings.update_review_runtime_settings({"ai_review_clause_ids": ["confidentiality"]})
    app_settings.update_review_runtime_settings({"ai_review_clause_ids": []})
    assert app_settings.ai_review_clause_ids() == []


def test_clause_ids_corrupt_blob_falls_back_to_empty():
    _write_raw_settings({"review_runtime": {"ai_review_clause_ids": "garbage"}})
    assert app_settings.review_runtime_settings()["ai_review_clause_ids"] == []
    assert app_settings.ai_review_clause_ids() == []


# --- Config #1: signal-term vocabulary reset (already persisted/validated) --
def test_signal_terms_reset_restores_default_vocabulary():
    app_settings.update_gmail_settings({"inbound_search_terms": ["only-one"]})
    assert app_settings.gmail_inbound_search_terms() == ["only-one"]
    app_settings.reset_gmail_inbound_search_terms()
    assert app_settings.gmail_inbound_search_terms() == list(
        app_settings.DEFAULT_GMAIL_INBOUND_SEARCH_TERMS
    )
