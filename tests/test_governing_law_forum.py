"""Tests for the Playbook-sourced governing-law -> court/forum pairing.

These prove the pairing is read FROM ``playbook.json`` -- the single source of
truth -- and is NOT a hardcoded duplicate in the helper or its callers:

* ``canonical_forum_for_law`` returns the court/forum carried by the matching
  approved option, and ``None`` for an unknown option id.
* MUTATING an approved option's ``court_name`` / ``forum_jurisdiction`` in a test
  playbook changes the helper's output -- the load-bearing proof that the pairing
  follows the Playbook rather than a hardcode.
* the live Playbook carries a court for every approved governing-law option (so a
  generation forum gate never has to refuse a real option).
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from nda_automation import governing_law_forum as glf
from nda_automation.checker import load_playbook


@pytest.fixture
def playbook():
    return load_playbook()


def _governing_law_options(playbook):
    clause = next(c for c in playbook["clauses"] if c["id"] == "governing_law")
    return list(clause["rules"]["approved_options"])


def _option(playbook, option_id):
    return next(o for o in _governing_law_options(playbook) if o["id"] == option_id)


class TestCanonicalForumForLaw:
    def test_returns_the_exact_contract_shape(self, playbook):
        pairing = glf.canonical_forum_for_law(playbook, "england_and_wales")
        # ENTITY-FORUM: the per-option ``court_name`` was REMOVED from the Playbook
        # (the city-level court is now entity-sourced), so it resolves to "". The
        # jurisdiction-level ``forum_jurisdiction`` (the detector descriptor) stays.
        assert pairing == {
            "option_id": "england_and_wales",
            "law_label": "England and Wales",
            "forum_jurisdiction": "England and Wales",
            "court_name": "",
        }
        # The contract is still exactly these four keys (court_name kept defensively).
        assert set(pairing) == {"option_id", "law_label", "forum_jurisdiction", "court_name"}

    @pytest.mark.parametrize(
        "option_id, forum_jurisdiction",
        [
            ("india", "Mumbai, India"),
            ("delaware", "State of Delaware"),
            ("england_and_wales", "England and Wales"),
            ("difc", "Dubai International Financial Centre"),
            ("ontario_canada", "Province of Ontario, Canada"),
        ],
    )
    def test_forum_jurisdiction_preserved_for_every_approved_option(
        self, playbook, option_id, forum_jurisdiction
    ):
        # The detector depends on ``forum_jurisdiction`` (jurisdiction-level); it is
        # preserved. ``court_name`` was removed, so it resolves empty.
        pairing = glf.canonical_forum_for_law(playbook, option_id)
        assert pairing["forum_jurisdiction"] == forum_jurisdiction
        assert pairing["court_name"] == ""
        assert glf.court_name_for_law(playbook, option_id) == ""

    def test_unknown_option_id_returns_none(self, playbook):
        assert glf.canonical_forum_for_law(playbook, "singapore") is None
        assert glf.canonical_forum_for_law(playbook, "") is None
        assert glf.court_name_for_law(playbook, "singapore") == ""

    def test_case_insensitive_option_id(self, playbook):
        assert glf.canonical_forum_for_law(playbook, "DIFC")["option_id"] == "difc"

    def test_defensive_against_malformed_playbook(self):
        assert glf.canonical_forum_for_law({}, "india") is None
        assert glf.canonical_forum_for_law({"clauses": "nope"}, "india") is None
        assert glf.court_name_for_law({}, "india") == ""


class TestPairingFollowsThePlaybook:
    """The load-bearing proof: the pairing is data, not code. Mutate the Playbook
    option and the helper output tracks the mutation."""

    def test_mutating_court_name_changes_the_helper_output(self, playbook):
        # ENTITY-FORUM: the live Playbook carries no ``court_name`` (removed), so it
        # resolves empty. The helper still READS the field defensively: inject one in
        # a copy and the output tracks it -- proving court_name is data, not a hardcode.
        before = glf.canonical_forum_for_law(playbook, "difc")["court_name"]
        assert before == ""

        mutated = deepcopy(playbook)
        _option(mutated, "difc")["court_name"] = "the courts of Atlantis"
        after = glf.canonical_forum_for_law(mutated, "difc")
        assert after["court_name"] == "the courts of Atlantis"
        assert glf.court_name_for_law(mutated, "difc") == "the courts of Atlantis"
        # The unmutated playbook is unaffected -- nothing is hardcoded/cached.
        assert glf.canonical_forum_for_law(playbook, "difc")["court_name"] == before

    def test_mutating_forum_jurisdiction_changes_the_helper_output(self, playbook):
        mutated = deepcopy(playbook)
        _option(mutated, "india")["forum_jurisdiction"] = "Goa, India"
        assert glf.canonical_forum_for_law(mutated, "india")["forum_jurisdiction"] == "Goa, India"

    def test_missing_court_name_resolves_empty(self, playbook):
        mutated = deepcopy(playbook)
        # The live Playbook already omits ``court_name`` (removed). Removing it again
        # is a no-op; an option with no court resolves to "" -- which the generation
        # forum gate turns into a hard refusal rather than a non-court venue.
        _option(mutated, "delaware").pop("court_name", None)
        assert glf.court_name_for_law(mutated, "delaware") == ""
        assert glf.canonical_forum_for_law(mutated, "delaware")["court_name"] == ""
