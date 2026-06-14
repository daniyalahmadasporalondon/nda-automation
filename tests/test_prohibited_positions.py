"""The prohibited-legal-position pattern set is sourced from the Playbook.

The Playbook (``non_circumvention`` clause's ``prohibited_position_patterns``) is
the single source of truth for the prohibited-position regexes. Three consumers
read from it through ``nda_automation.prohibited_positions``: the in-process
adapter guard, the pre-save ship gate, and gen-verify's independent gate. These
tests prove the set is read from the Playbook (not a hardcoded literal), that all
consumers see the same set, and that a missing/unreadable Playbook degrades to
the literal fallback rather than dropping the guard.
"""

from __future__ import annotations

import json
from pathlib import Path

from nda_automation import prohibited_positions as pp


def _playbook_pattern_pairs() -> tuple[tuple[str, str], ...]:
    playbook = json.loads((Path(pp.__file__).resolve().parent.parent / "playbook.json").read_text())
    clause = next(c for c in playbook["clauses"] if c.get("id") == "non_circumvention")
    return tuple((e["label"], e["pattern"]) for e in clause["prohibited_position_patterns"])


def test_sources_come_from_playbook_non_circumvention_clause():
    # The module's pattern sources are exactly the Playbook's authored set.
    assert pp.PROHIBITED_POSITION_PATTERN_SOURCES == _playbook_pattern_pairs()


def test_harness_shares_the_module_source():
    # gen-verify imports the module set rather than keeping its own copy, so the
    # independent gate and the generator guard can never drift apart.
    from tests import gen_verify_harness

    assert gen_verify_harness._PROHIBITED_POSITION_PATTERNS is pp.PROHIBITED_POSITION_PATTERN_SOURCES


def test_loader_reads_a_changed_playbook(tmp_path, monkeypatch):
    # Editing the Playbook's prohibited_position_patterns flows through to the
    # loader — proving the set is genuinely sourced, not coincidentally equal to
    # the literal fallback.
    playbook = json.loads((Path(pp.__file__).resolve().parent.parent / "playbook.json").read_text())
    clause = next(c for c in playbook["clauses"] if c.get("id") == "non_circumvention")
    clause["prohibited_position_patterns"] = [{"label": "custom_family", "pattern": "unique_marker_xyz"}]
    target = tmp_path / "playbook.json"
    target.write_text(json.dumps(playbook))

    monkeypatch.setattr(pp, "_PLAYBOOK_PATH", target)
    assert pp._load_prohibited_position_sources() == (("custom_family", "unique_marker_xyz"),)


def test_loader_falls_back_when_playbook_unreadable(monkeypatch):
    monkeypatch.setattr(pp, "_PLAYBOOK_PATH", Path("/nonexistent/does-not-exist.json"))
    assert (
        pp._load_prohibited_position_sources()
        == pp._FALLBACK_PROHIBITED_POSITION_PATTERN_SOURCES
    )


def test_loader_falls_back_on_invalid_regex(tmp_path, monkeypatch):
    playbook = json.loads((Path(pp.__file__).resolve().parent.parent / "playbook.json").read_text())
    clause = next(c for c in playbook["clauses"] if c.get("id") == "non_circumvention")
    clause["prohibited_position_patterns"] = [{"label": "broken", "pattern": "([unclosed"}]
    target = tmp_path / "playbook.json"
    target.write_text(json.dumps(playbook))

    monkeypatch.setattr(pp, "_PLAYBOOK_PATH", target)
    assert (
        pp._load_prohibited_position_sources()
        == pp._FALLBACK_PROHIBITED_POSITION_PATTERN_SOURCES
    )


def test_fallback_is_kept_in_sync_with_the_playbook():
    # The literal backstop must stay byte-equal to the Playbook's authored set so
    # a read failure degrades to the SAME guard, not a stale one.
    assert pp._FALLBACK_PROHIBITED_POSITION_PATTERN_SOURCES == _playbook_pattern_pairs()


def test_first_prohibited_position_and_any_match_known_families():
    assert pp.first_prohibited_position("the parties agree to a non-compete") == "non_compete"
    assert pp.first_prohibited_position("hereby assigns all right title and interest in") == "ip_assignment"
    assert pp.ANY_PROHIBITED_POSITION.search("this agreement shall automatically renew")
    assert pp.first_prohibited_position("a perfectly ordinary confidentiality clause") == ""
