"""App-layer user-allowlist predicate tests (http_auth.google_email_allowed).

The allowlist is defense-in-depth over the Google OAuth-app audience:

  * NDA_ALLOWED_EMAIL_DOMAINS -- comma list of domains (e.g. "aspora.com");
  * NDA_ALLOWED_EMAILS        -- comma list of exact-email exceptions.

Invariants under test:
  * CRITICAL FAIL-SAFE: both vars unset/empty -> open (no restriction), so a
    deploy before the env is configured can never lock everyone out;
  * once EITHER var has an entry, matching fails CLOSED (empty/malformed
    emails deny; a subdomain is NOT its parent domain);
  * normalization mirrors persisted admin emails (lowercase/strip) on both the
    env entries and the probed email;
  * ``session_user_allowed`` routes ONLY Google identities through the
    allowlist -- basic-auth sessions are unaffected.
"""

from __future__ import annotations

import pytest

from nda_automation.http_auth import (
    google_email_allowed,
    session_user_allowed,
    user_allowlist_configured,
)


@pytest.fixture(autouse=True)
def _clean_allowlist_env(monkeypatch):
    monkeypatch.delenv("NDA_ALLOWED_EMAIL_DOMAINS", raising=False)
    monkeypatch.delenv("NDA_ALLOWED_EMAILS", raising=False)
    yield


# --- fail-safe open state ----------------------------------------------------


def test_unset_env_means_no_restriction():
    assert not user_allowlist_configured()
    assert google_email_allowed("anyone@anywhere.example")
    assert google_email_allowed("")  # even a blank email passes while OFF
    assert google_email_allowed(None)


@pytest.mark.parametrize("value", ["", "   ", " , ,, "])
def test_blank_or_comma_only_values_stay_open(monkeypatch, value):
    monkeypatch.setenv("NDA_ALLOWED_EMAIL_DOMAINS", value)
    monkeypatch.setenv("NDA_ALLOWED_EMAILS", value)
    assert not user_allowlist_configured()
    assert google_email_allowed("anyone@anywhere.example")


# --- domain list ---------------------------------------------------------------


def test_domain_match_allows_and_others_deny(monkeypatch):
    monkeypatch.setenv("NDA_ALLOWED_EMAIL_DOMAINS", "aspora.com")
    assert user_allowlist_configured()
    assert google_email_allowed("dana@aspora.com")
    assert not google_email_allowed("mallory@evil.example")


def test_domain_match_is_case_insensitive_and_trimmed(monkeypatch):
    monkeypatch.setenv("NDA_ALLOWED_EMAIL_DOMAINS", "  Aspora.COM , @other.example ")
    assert google_email_allowed("Dana@ASPORA.com")
    assert google_email_allowed("bob@other.example")  # leading '@' entries tolerated
    assert not google_email_allowed("dana@nope.example")


def test_subdomain_is_not_its_parent_domain(monkeypatch):
    monkeypatch.setenv("NDA_ALLOWED_EMAIL_DOMAINS", "aspora.com")
    assert not google_email_allowed("dana@sub.aspora.com")
    assert not google_email_allowed("dana@aspora.com.evil.example")


# --- exact-email exceptions ----------------------------------------------------


def test_exact_email_match_allows(monkeypatch):
    monkeypatch.setenv("NDA_ALLOWED_EMAILS", "personal.founder@gmail.com")
    assert user_allowlist_configured()
    assert google_email_allowed("personal.founder@gmail.com")
    assert not google_email_allowed("other@gmail.com")  # domain NOT implied


def test_exact_email_match_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("NDA_ALLOWED_EMAILS", " Personal.Founder@Gmail.com ")
    assert google_email_allowed("PERSONAL.FOUNDER@gmail.COM")


def test_either_list_admits(monkeypatch):
    monkeypatch.setenv("NDA_ALLOWED_EMAIL_DOMAINS", "aspora.com")
    monkeypatch.setenv("NDA_ALLOWED_EMAILS", "personal.founder@gmail.com")
    assert google_email_allowed("dana@aspora.com")
    assert google_email_allowed("personal.founder@gmail.com")
    assert not google_email_allowed("mallory@evil.example")


# --- configured => fail closed ---------------------------------------------------


@pytest.mark.parametrize("bad_email", ["", None, "not-an-email", "a b@aspora.com", "<x>@aspora.com"])
def test_configured_allowlist_denies_malformed_emails(monkeypatch, bad_email):
    monkeypatch.setenv("NDA_ALLOWED_EMAIL_DOMAINS", "aspora.com")
    assert not google_email_allowed(bad_email)


def test_admins_are_not_implicitly_allowed(monkeypatch):
    # Being an admin grants no allowlist bypass: the admin's email must pass too.
    monkeypatch.setenv("NDA_ALLOWED_EMAIL_DOMAINS", "aspora.com")
    monkeypatch.setenv("NDA_ADMIN_USERS", "admin@elsewhere.example")
    assert not google_email_allowed("admin@elsewhere.example")


# --- session gate (provider split) -----------------------------------------------


def test_session_gate_restricts_google_only(monkeypatch):
    monkeypatch.setenv("NDA_ALLOWED_EMAIL_DOMAINS", "aspora.com")
    google_ok = {"id": "google:1", "provider": "google", "email": "dana@aspora.com"}
    google_bad = {"id": "google:2", "provider": "google", "email": "mallory@evil.example"}
    basic = {"id": "nda-admin", "provider": "basic", "email": "nda-admin"}
    assert session_user_allowed(google_ok)
    assert not session_user_allowed(google_bad)
    # Basic-auth identities never route through the allowlist.
    assert session_user_allowed(basic)
    # No user at all is never allowed.
    assert not session_user_allowed(None)


def test_session_gate_open_when_unconfigured():
    google_user = {"id": "google:1", "provider": "google", "email": "anyone@anywhere.example"}
    assert session_user_allowed(google_user)
