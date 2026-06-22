"""Tests for the admin-editable Gmail inbound sync window (days).

Covers the setting's behavioural contract end-to-end at the unit level:

* the ``*_from_payload`` normalizer clamps/falls back to the default (90) on bad
  input and keeps in-band values;
* ``gmail_inbound_window_days()`` round-trips a persisted value through the real
  settings store;
* the EFFECTIVE inbound fetch query (``_default_inbound_query``) actually reflects
  the configured window's ``newer_than:{N}d`` clause -- proven against running code,
  not intent;
* a corrupt/out-of-band stored value falls back to the default 90 (both in the
  reader and in the effective query);
* ``gmail_status()`` exposes the current window + its default/bounds.

The route-level validation, admin-gate (403), and audit behaviour live in
``tests/test_server.py`` (the gmail settings update is added to the shared
ADMIN_SETTINGS_MUTATORS list and gets a dedicated validation test there).
"""

from __future__ import annotations

import pytest

from nda_automation import app_settings, gmail_integration


@pytest.fixture
def settings_data_dir(tmp_path, monkeypatch):
    """Root the operational settings store at an isolated tmp dir.

    The settings repository roots at ``matter_store.DATA_DIR``; repoint that module
    attribute so these unit tests never touch the shared session tmp dir.
    """
    from nda_automation import matter_store

    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    return tmp_path


# --- normalizer (pure) ------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, 90),          # absent => default
        ("", 90),            # blank => default
        ("   ", 90),         # whitespace => default
        (0, 90),             # zero would fetch nothing => default
        (-5, 90),            # negative => default
        (9999, 90),          # over the 365 cap => default (not a silent clamp)
        (366, 90),           # just over the cap => default
        ("abc", 90),         # non-numeric => default
        (True, 90),          # bool is never a day-count => default
        (False, 90),         # bool is never a day-count => default
        (1, 1),              # band floor kept
        (365, 365),          # band ceiling kept
        (30, 30),            # in-band kept
        ("30", 30),          # numeric string coerced + kept
        (90, 90),            # the default kept
    ],
)
def test_window_from_payload_clamps_and_falls_back(raw, expected):
    assert app_settings.gmail_inbound_window_days_from_payload(raw) == expected


def test_window_band_constants_are_sane():
    assert app_settings.MIN_GMAIL_INBOUND_WINDOW_DAYS == 1
    assert app_settings.MAX_GMAIL_INBOUND_WINDOW_DAYS == 365
    assert app_settings.DEFAULT_GMAIL_INBOUND_WINDOW_DAYS == 90
    # The default constant must mirror the integration's fallback constant so the
    # two halves of the system agree on "90".
    assert (
        gmail_integration.GMAIL_INBOUND_WINDOW_DAYS
        == app_settings.DEFAULT_GMAIL_INBOUND_WINDOW_DAYS
    )


# --- reader + persistence round-trip ---------------------------------------

def test_default_window_is_90_when_unset(settings_data_dir):
    assert app_settings.gmail_inbound_window_days() == 90


def test_window_round_trips_through_the_store(settings_data_dir):
    app_settings.update_gmail_settings({"inbound_window_days": 30})
    assert app_settings.gmail_inbound_window_days() == 30
    # The stored value survives a fresh read of the whole section.
    assert app_settings.gmail_settings()["inbound_window_days"] == 30


def test_reader_falls_back_to_90_on_corrupt_stored_value(settings_data_dir):
    # Persist a clean value first, then poke a corrupt value straight into the
    # stored section to simulate disk/manual corruption, bypassing the write-time
    # normalizer. The reader must re-derive the safe default rather than trust it.
    app_settings.update_gmail_settings({"inbound_window_days": 30})
    repo = app_settings._repository()
    section = repo.read_section("gmail", app_settings.gmail_settings_from_payload)
    section["inbound_window_days"] = -1  # corrupt, out-of-band

    assert app_settings.gmail_inbound_window_days(section) == 90


# --- effective query reflects the window (running code, not intent) --------

def test_effective_query_reflects_configured_window(settings_data_dir):
    # Default first.
    assert "newer_than:90d" in gmail_integration._default_inbound_query()

    app_settings.update_gmail_settings({"inbound_window_days": 30})
    query = gmail_integration._default_inbound_query()
    assert "newer_than:30d" in query
    assert "newer_than:90d" not in query
    # The rest of the structural envelope is intact (the window swap is surgical).
    assert "in:inbox has:attachment (filename:docx OR filename:pdf) -from:me" in query


def test_effective_query_falls_back_to_90_on_corrupt_setting(settings_data_dir, monkeypatch):
    # If the settings read itself raises, the effective query must degrade to the
    # static default envelope rather than ever raising on the hot inbound path.
    def boom(*_args, **_kwargs):
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(app_settings, "gmail_inbound_window_days", boom)
    query = gmail_integration._default_inbound_query()
    assert query == gmail_integration.GMAIL_INBOUND_BASE_QUERY
    assert "newer_than:90d" in query


def test_base_query_constant_is_the_default_window():
    # The import-time static constant (the fallback) is pinned to the default 90.
    assert "newer_than:90d" in gmail_integration.GMAIL_INBOUND_BASE_QUERY


# --- status payload ---------------------------------------------------------

def test_status_exposes_window_and_default(settings_data_dir):
    app_settings.update_gmail_settings({"inbound_window_days": 45})
    status = gmail_integration.gmail_status()
    assert status["inbound_window_days"] == 45
    assert status["inbound_window_days_default"] == 90
    assert status["inbound_window_days_min"] == 1
    assert status["inbound_window_days_max"] == 365
    # The raw stored value is also visible under settings for the FE fallback.
    assert status["settings"]["inbound_window_days"] == 45
