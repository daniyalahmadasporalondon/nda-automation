"""Golden-baseline gate over the PDF reading-order fixture corpus.

This test is the byte-identity gate the reading-order fix must pass. It runs on
EVERY tree.

Contract enforced here:

  * NEGATIVE / real_negative / garble_trap fixtures  -> the extraction snapshot
    MUST equal the committed golden baseline byte-for-byte. Any drift is a
    catastrophic false positive (a document that reads correctly today started
    reading differently) and fails the build.

  * POSITIVE / garble_open* / garble_fixed fixtures  -> the baseline documents
    the PRE-FIX (buggy or already-fixed) state. This test does NOT assert they
    stay identical (the fix is expected to change them); it only asserts a
    baseline EXISTS so the fix has something concrete to diff against. The
    'these must improve' assertions belong in the fix's own test, which will
    re-capture these baselines to the corrected output.

  * Fixtures regenerate to byte-identical PDFs (deterministic corpus).

Also asserts every fixture is present so an empty/partial corpus cannot pass.
"""

from __future__ import annotations

import hashlib
import os

import pytest

from tests import pdf_reading_order_harness as harness
from tests.fixtures.pdf_reading_order import generate_fixtures

MUST_MATCH_CATEGORIES = {"negative", "real_negative", "garble_trap"}
DOCUMENTED_TO_CHANGE = {"positive", "garble_open_a", "garble_open_b", "garble_fixed"}


def _all_items():
    return harness._fixture_names()  # name -> (builder|None, category, behavior)


def test_corpus_is_non_empty_and_complete():
    items = _all_items()
    # Guard against a gate passing on an empty corpus.
    assert len(items) >= 17, f"expected the full corpus, got {len(items)}"
    cats = {cat for _b, cat, _beh in items.values()}
    assert MUST_MATCH_CATEGORIES <= cats
    assert {"positive", "garble_open_a", "garble_open_b"} <= cats


@pytest.mark.parametrize("name", sorted(generate_fixtures.FIXTURES))
def test_fixtures_regenerate_byte_identical(name, tmp_path):
    """The corpus PDFs are reproducible: regenerating yields the committed bytes."""
    builder = generate_fixtures.FIXTURES[name][0]
    regenerated = builder()
    committed_path = os.path.join(generate_fixtures.HERE, f"{name}.pdf")
    with open(committed_path, "rb") as fh:
        committed = fh.read()
    assert hashlib.sha256(regenerated).hexdigest() == hashlib.sha256(committed).hexdigest(), (
        f"{name}.pdf is not reproducible from generate_fixtures; regenerate and recommit"
    )


@pytest.mark.parametrize("name", sorted(_all_items()))
def test_every_fixture_has_a_baseline(name):
    base_path = os.path.join(harness.BASELINE_DIR, f"{name}.json")
    assert os.path.exists(base_path), f"missing golden baseline for {name}"


def _must_match_names():
    return [n for n, (_b, cat, _beh) in _all_items().items() if cat in MUST_MATCH_CATEGORIES]


@pytest.mark.parametrize("name", sorted(_must_match_names()))
def test_negative_and_trap_fixtures_are_byte_identical(name):
    """The core no-false-positive gate: documents that read correctly today must
    extract identically on this tree."""
    diffs = harness.diff_against_baselines()
    assert diffs[name] is None, (
        f"{name} drifted from its golden baseline -- a document that reads "
        f"correctly today now extracts differently:\n{diffs[name]}"
    )
