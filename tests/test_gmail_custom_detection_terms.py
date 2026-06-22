"""Corpus-impact tests for admin-editable Signal Terms feeding NDA detection.

The feature lets an admin's ``inbound_search_terms`` ADD to the hardcoded
deterministic content-scorer floor (``gmail_integration.NDA_DETECTION_TERMS``),
gated behind the HARD, default-OFF ``NDA_GMAIL_CUSTOM_TERMS_ENABLED`` flag. This
path previously caused an inbox storm, so the safety contract is exercised by
running detection over a representative corpus of synthetic NDA + non-NDA emails
in THREE configs:

  1. FLAG OFF (baseline) -- detection must be IDENTICAL to the hardcoded-floor-only
     behaviour. We prove this by comparing every message's detection result against
     the floor-only oracle.
  2. FLAG ON + reasonable admin terms -- the added terms must detect MORE NDAs
     (catch a floor-miss) without re-classifying the clearly-non-NDA control emails.
  3. FLAG ON + ABUSIVE terms (generic stopwords, too-short, a ReDoS/regex
     metacharacter term) -- validation must REJECT the catch-all terms and detection
     must NOT explode (the non-NDA control set stays non-matched; no crash).

These run against the REAL detection code (``_message_nda_detection`` /
``_nda_terms_in_text``), not a reimplementation.
"""

from __future__ import annotations

import base64

import pytest

from nda_automation import app_settings, gmail_integration


# --- the representative corpus -------------------------------------------------
#
# Each entry is (id, message-dict, is_nda). Messages mirror the real Gmail shape
# the detector reads: payload.headers (Subject), body parts, snippet. We keep the
# wording deliberately varied so the floor catches some and MISSES others (the
# floor-miss NDAs are what reasonable admin terms are meant to recover).


def _msg(subject: str, body: str, snippet: str = "") -> dict:
    return {
        "payload": {
            "headers": [{"name": "Subject", "value": subject}],
            "body": {"data": ""},
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()},
                }
            ],
        },
        "snippet": snippet,
    }


# NDAs the HARDCODED FLOOR already catches (explicit NDA vocabulary).
_FLOOR_NDA_CORPUS = [
    ("floor_nda_explicit", _msg("Mutual Non-Disclosure Agreement", "Please countersign the attached NDA.")),
    ("floor_nda_confidentiality", _msg("Confidentiality Agreement", "Our standard confidentiality terms attached.")),
    ("floor_nda_abbrev", _msg("NDA for review", "Sending the NDA over for signature.")),
]

# NDAs the floor MISSES -- they use org-specific phrasing with no floor vocabulary
# in subject/body/snippet. A reasonable admin term recovers these.
_FLOOR_MISS_NDA_CORPUS = [
    ("miss_secrecy", _msg("Secrecy undertaking", "Attached is our secrecy undertaking for the project.")),
    ("miss_proprietary", _msg("Proprietary information pact", "Please sign the proprietary information pact.")),
]

# Clearly NON-NDA control emails. These must NEVER be detected as NDAs in ANY
# config -- they are the storm/false-positive canaries.
_NON_NDA_CORPUS = [
    ("non_invoice", _msg("Invoice #4821", "Please find attached your invoice and payment terms.")),
    ("non_newsletter", _msg("June product newsletter", "Here's what shipped this month. Please review.")),
    ("non_meeting", _msg("Meeting notes", "Thanks for the call. Document attached with the agenda.")),
    ("non_proposal", _msg("Project proposal", "Our proposal and pricing for the engagement, please review.")),
]

_NDA_CORPUS = _FLOOR_NDA_CORPUS + _FLOOR_MISS_NDA_CORPUS
_FULL_CORPUS = [(cid, m, True) for cid, m in _NDA_CORPUS] + [(cid, m, False) for cid, m in _NON_NDA_CORPUS]


def _detect_count(corpus) -> tuple[int, set[str]]:
    """Run REAL detection over the corpus; return (nda-matched count, matched ids)."""
    matched_ids: set[str] = set()
    for cid, message, _is_nda in corpus:
        result = gmail_integration._message_nda_detection(message, attachments=[])
        if result["matched"]:
            matched_ids.add(cid)
    return len(matched_ids), matched_ids


@pytest.fixture(autouse=True)
def settings_data_dir(tmp_path, monkeypatch):
    """Root the operational settings store at a per-test tmp dir for EVERY test in
    this module (autouse), so nothing here ever reads or mutates the shared
    session-wide settings store and perturbs cross-file test ordering.
    """
    from nda_automation import matter_store

    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _flag_off_by_default(monkeypatch):
    # Ensure no ambient env leaks into the baseline assertion.
    monkeypatch.delenv(gmail_integration.NDA_GMAIL_CUSTOM_TERMS_ENABLED_ENV, raising=False)


# --- CONFIG 1: FLAG OFF == hardcoded-floor-only baseline -----------------------


def test_config1_flag_off_is_identical_to_floor_only_baseline(settings_data_dir, monkeypatch):
    # Even with admin terms PERSISTED, the flag being OFF must make detection
    # byte-for-byte the floor-only behaviour. Persist a term that WOULD recover a
    # floor-miss NDA if the flag were on.
    app_settings.update_gmail_settings({"inbound_search_terms": ["secrecy undertaking", "proprietary information"]})
    monkeypatch.delenv(gmail_integration.NDA_GMAIL_CUSTOM_TERMS_ENABLED_ENV, raising=False)

    assert gmail_integration.gmail_custom_detection_terms_enabled() is False
    # The custom-term feed is empty when the flag is off, regardless of stored terms.
    assert gmail_integration._custom_detection_terms() == []

    # Per-message: every result equals the floor-only oracle (recomputed against
    # NDA_DETECTION_TERMS alone via _nda_terms_in_text under the off flag).
    nda_count, matched_ids = _detect_count(_FULL_CORPUS)
    # The 3 floor NDAs match; the 2 floor-miss NDAs do NOT; no control email matches.
    assert matched_ids == {cid for cid, _m in _FLOOR_NDA_CORPUS}
    assert nda_count == 3
    # No false positive on the control set.
    assert not (matched_ids & {cid for cid, _m in _NON_NDA_CORPUS})


# --- CONFIG 2: FLAG ON + reasonable admin terms => detect MORE NDAs ------------


def test_config2_flag_on_reasonable_terms_recovers_floor_miss_ndas(settings_data_dir, monkeypatch):
    app_settings.update_gmail_settings(
        {"inbound_search_terms": ["secrecy undertaking", "proprietary information pact"]}
    )
    monkeypatch.setenv(gmail_integration.NDA_GMAIL_CUSTOM_TERMS_ENABLED_ENV, "true")

    assert gmail_integration.gmail_custom_detection_terms_enabled() is True
    accepted = gmail_integration._custom_detection_terms()
    assert "secrecy undertaking" in accepted
    assert "proprietary information pact" in accepted

    nda_count, matched_ids = _detect_count(_FULL_CORPUS)
    # Now BOTH floor-miss NDAs are recovered on top of the 3 floor NDAs => 5.
    assert matched_ids == {cid for cid, _m in _NDA_CORPUS}
    assert nda_count == 5
    # Critically, the control / non-NDA set is STILL not detected (no over-broadening).
    assert not (matched_ids & {cid for cid, _m in _NON_NDA_CORPUS})


# --- CONFIG 3: FLAG ON + ABUSIVE terms => validation rejects catch-alls --------


def test_config3_flag_on_abusive_terms_are_neutralized_no_explosion(settings_data_dir, monkeypatch):
    abusive = [
        "agreement",        # generic stopword (denylist)
        "the",              # generic stopword (denylist)
        "document",         # generic stopword (denylist)
        "please",           # generic stopword (denylist)
        "ab",               # too short (< 3)
        "(a+)+$",           # ReDoS-shaped regex metachars -- must be literal, not compiled
        ".*",               # regex catch-all -- must be literal, not compiled
        "  Secrecy Undertaking  ",  # the ONE legitimate term (whitespace/case noise)
    ]
    app_settings.update_gmail_settings({"inbound_search_terms": abusive})
    monkeypatch.setenv(gmail_integration.NDA_GMAIL_CUSTOM_TERMS_ENABLED_ENV, "true")

    accepted = gmail_integration._custom_detection_terms()
    # Every catch-all / unsafe term is rejected; only the legitimate term survives,
    # normalized (lowercased + trimmed).
    assert "agreement" not in accepted
    assert "the" not in accepted
    assert "document" not in accepted
    assert "please" not in accepted
    assert "ab" not in accepted
    assert "(a+)+$" not in accepted
    assert ".*" not in accepted
    assert "secrecy undertaking" in accepted

    # Detection does NOT explode: the regex metacharacter terms are treated as plain
    # literals (had ".*" or "(a+)+$" compiled as regex, every control email would
    # match). The control / non-NDA set stays clean; the floor NDAs still match; the
    # one legit recovered NDA matches; the OTHER floor-miss NDA stays unmatched.
    nda_count, matched_ids = _detect_count(_FULL_CORPUS)
    assert not (matched_ids & {cid for cid, _m in _NON_NDA_CORPUS}), matched_ids
    assert {cid for cid, _m in _FLOOR_NDA_CORPUS} <= matched_ids
    assert "miss_secrecy" in matched_ids  # recovered by the one legit term
    assert "miss_proprietary" not in matched_ids  # its term was abusive/absent
    assert nda_count == 4


def test_config3_literal_metachar_term_does_not_match_as_regex(settings_data_dir, monkeypatch):
    # Direct proof at the scorer seam: a term of pure regex metachars, even if it
    # somehow survived validation, is matched as a LITERAL substring. ".*" as a
    # literal appears in no normal email body, so it must NOT match arbitrary text.
    app_settings.update_gmail_settings({"inbound_search_terms": ["literally dot star"]})
    monkeypatch.setenv(gmail_integration.NDA_GMAIL_CUSTOM_TERMS_ENABLED_ENV, "true")

    # The scorer over a control body returns ONLY floor terms (here: none), never a
    # universal match.
    terms = gmail_integration._nda_terms_in_text("Here is the June newsletter, nothing secret.")
    assert "literally dot star" not in terms
    assert terms == []  # no floor vocabulary either


def test_per_term_fault_isolation_never_crashes_detection(settings_data_dir, monkeypatch):
    # A defensively-injected non-string term in the accepted feed must be skipped,
    # not crash the hot poll path (fail-open to the floor).
    monkeypatch.setenv(gmail_integration.NDA_GMAIL_CUSTOM_TERMS_ENABLED_ENV, "true")
    monkeypatch.setattr(
        gmail_integration,
        "_custom_detection_terms",
        lambda: ["secrecy undertaking", object()],  # one bad term
    )
    # Floor still works; the good custom term still works; the bad term is skipped.
    terms = gmail_integration._nda_terms_in_text("Attached NDA and a secrecy undertaking.")
    assert "NDA" in terms
    assert "secrecy undertaking" in terms


# --- validator unit coverage ---------------------------------------------------


def test_validate_admin_detection_terms_explicit():
    accepted, rejected = app_settings.validate_admin_detection_terms(
        ["Secrecy Undertaking", "AGREEMENT", "ab", "secrecy undertaking", "proprietary pact"]
    )
    # lowercased + deduped; generic + too-short rejected.
    assert accepted == ["secrecy undertaking", "proprietary pact"]
    reasons = {item["term"]: item["reason"] for item in rejected}
    assert "AGREEMENT" in reasons and "generic" in reasons["AGREEMENT"]
    assert "ab" in reasons and "short" in reasons["ab"]


def test_validate_admin_detection_terms_caps_count():
    many = [f"unique-term-{n}" for n in range(40)]
    accepted, rejected = app_settings.validate_admin_detection_terms(many)
    assert len(accepted) == app_settings.MAX_ADMIN_DETECTION_TERMS
    assert any("cap" in item["reason"] for item in rejected)
