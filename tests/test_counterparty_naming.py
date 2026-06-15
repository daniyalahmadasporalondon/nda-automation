"""Tests for the deterministic email-subject -> counterparty-name normalizer.

The canonical test matrix below is the vetted spec fixture set, transcribed
verbatim (input -> expected). Every row is asserted exactly. The
``must_not_change`` rows are the regression guards against over-aggressive
splitting. Idempotency (``normalize(normalize(x)) == normalize(x)``) is asserted
on every input.
"""
from __future__ import annotations

import pytest

from nda_automation.counterparty_naming import normalize_counterparty

# (input, expected, must_not_change) — verbatim from the spec canonical_test_matrix.
CANONICAL_MATRIX = [
    ("03-REVIEW-mutual-nda", "03-REVIEW-mutual-nda", True),
    ("Acme_Fintech", "Acme_Fintech", True),
    (
        "Air India - Mutual NDA Template (Updated as on 23.04.2025) (4)",
        "Air India - Mutual NDA Template (Updated as on 23.04.2025) (4)",
        True,
    ),
    ("Beacon_Pay", "Beacon_Pay", True),
    ("Fwd: Air India <> Aspora", "Air India", False),
    ("Fwd: Aspora - Reem bank - Introduction - Bin Sponsorship", "Reem bank", False),
    ("Fwd: Aspora <> Coverstack", "Coverstack", False),
    ("Fwd: Aspora | NetSuite Follow Up", "NetSuite", False),
    ("Fwd: CALL: Aspora x Pismo", "Pismo", False),
    ("Fwd: Moorwand Aspora", "Moorwand Aspora", False),
    ("Fwd: slice BAV stack - Aspora", "slice BAV stack", False),
    (
        "Fwd: Tax Filing Integration for Aspora Users by TaxBuddy - Integration, Timelines",
        "Tax Filing Integration for Aspora Users by TaxBuddy - Integration, Timelines",
        False,
    ),
    (
        "Fwd: Updated invitation: Aspora <> NymCard @ Wed May 13, 2026 7:30pm - 8:00pm",
        "NymCard",
        False,
    ),
    ("Fwd: Zeta | Aspora Connect: Mehul<>Neha", "Zeta", False),
    ("Re: Fwd: Aspora x Beacon Pay - follow up", "Beacon Pay", False),
    ("RE: NDA - Aspora and Acme Fintech", "Acme Fintech", False),
    ("Fwd: Aspora <> Vance Money", "Aspora <> Vance Money", False),
    ("Fwd: Vance Techlabs <> Real Transfer", "Vance Techlabs <> Real Transfer", False),
    ("Nesse Technologies <> Beacon Pay", "Beacon Pay", False),
    ("Coverstack <> Aspora <> Beacon Pay", "Coverstack", False),
    ("Vance Money Transfer Co", "Vance Money Transfer Co", True),
    ("Asporable Widgets Inc", "Asporable Widgets Inc", True),
    ("Fwd: Asporable <> Aspora", "Asporable", False),
    ("Fwd: Coverstack – Aspora", "Coverstack", False),
    ("Fwd: Aspora — Coverstack", "Coverstack", False),
    ("Fwd: Stark Industries / Aspora", "Stark Industries", False),
    ("NDA between Acme and Globex", "NDA between Acme and Globex", True),
    ("Aspora|Pismo", "Pismo", False),
    ("vs Aspora", "vs Aspora", False),
    ("Fwd: Aspora <>", "Aspora <>", False),
    ("Aspora", "Aspora", False),
    ("Fwd:", "", False),
    ("Re: Re: Fwd:", "", False),
    ("", "", False),
    ("   ", "", False),
]


@pytest.mark.parametrize(
    "subject, expected",
    [(row[0], row[1]) for row in CANONICAL_MATRIX],
    ids=[repr(row[0]) for row in CANONICAL_MATRIX],
)
def test_canonical_matrix(subject, expected):
    assert normalize_counterparty(subject) == expected


@pytest.mark.parametrize(
    "subject",
    [row[0] for row in CANONICAL_MATRIX],
    ids=[repr(row[0]) for row in CANONICAL_MATRIX],
)
def test_idempotent(subject):
    once = normalize_counterparty(subject)
    twice = normalize_counterparty(once)
    assert twice == once


@pytest.mark.parametrize(
    "subject",
    [row[0] for row in CANONICAL_MATRIX if row[2]],
    ids=[repr(row[0]) for row in CANONICAL_MATRIX if row[2]],
)
def test_must_not_change(subject):
    # The over-split regression guards: an FP-free name passes through verbatim
    # (modulo whitespace collapse, which these inputs already satisfy).
    assert normalize_counterparty(subject) == subject


# --- the load-bearing pinned regressions (called out in the spec) ----------
def test_slash_connector_pinned():
    # The '/' connector only works because normalize runs BEFORE the Drive
    # sanitizer converts '/' -> space. Pin it.
    assert normalize_counterparty("Fwd: Stark Industries / Aspora") == "Stark Industries"


def test_simultaneous_strong_split_pinned():
    # The single combined STRONG_RE (all connectors at once) is load-bearing;
    # a rank-by-connector scheme would wrongly split the inner '<>' first.
    assert normalize_counterparty("Fwd: Zeta | Aspora Connect: Mehul<>Neha") == "Zeta"


# --- robustness ------------------------------------------------------------
def test_never_raises_on_none():
    assert normalize_counterparty(None) == ""  # type: ignore[arg-type]


def test_redos_probe_collapses_fast():
    import time

    subject = (" " * 200000) + "Aspora <> Z"
    start = time.perf_counter()
    result = normalize_counterparty(subject)
    elapsed = time.perf_counter() - start
    assert result == "Z"
    # Collapse-whitespace-first + the 4096 cap keep this well under a second.
    assert elapsed < 1.0


def test_custom_first_party_tokens_override():
    # A caller-supplied token list overrides the registry default.
    assert normalize_counterparty("Fwd: Globex <> Initech", ["Initech"]) == "Globex"


def test_empty_token_list_falls_back_to_default():
    # An empty/blank list is treated as "use the default registry-derived set".
    assert normalize_counterparty("Fwd: Air India <> Aspora", []) == "Air India"
