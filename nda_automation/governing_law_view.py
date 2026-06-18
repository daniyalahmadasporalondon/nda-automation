"""Derive a matter's GOVERNING LAW as a Playbook-approved option id.

This is the per-matter governing-law surface the dashboard smart-search bar reads
(``public_matter['governing_law']``). It exists so a query like "DIFC NDAs" can be
applied DETERMINISTICALLY over the matters already in app-state -- no Drive, no
durable index.

The value is a Playbook ``governing_law`` *approved-option id* (e.g. ``difc`` /
``india`` / ``delaware`` / ``england_and_wales``), never free text, so the FE's
enum allowlist (mirrored from the same Playbook options) can match it exactly.

Two honest sources, mirroring ``artifact_registry.derive_counterparty``:

* GENERATED NDAs -- the exact law the generator wrote, read from the generation
  manifest's ``governing_law_value`` (stashed on the generated artifact's
  ``metadata['generation']``, like the counterparty name). Normalised to its
  option id via the Playbook approved-options map.
* INBOUND matters -- a best-effort detection: the ``governing_law`` review clause's
  ``governing_law_analysis.candidate_records`` carry the law text the checker found
  in the counterparty's document. When that text matches an APPROVED option it is
  surfaced as that option id; an unapproved / unclear / absent law surfaces nothing
  (``""``), because we never imply a precision the data doesn't have. So inbound
  governing-law is only present when the document states an approved law -- a
  non-approved or missing law is simply not filterable by this dimension.

The option-id allowlist is sourced from the Playbook approved options (the same
single source generation uses), cached at module load and re-derivable, so it can
never drift from the real Playbook.
"""
from __future__ import annotations

from typing import Any, Mapping

from . import artifact_registry

# Cache the Playbook-sourced value/alias -> option-id map so per-matter projection
# (the dashboard list view is a hot path) does not re-read the Playbook each call.
_OPTION_LOOKUP_CACHE: dict[str, str] | None = None
_OPTION_IDS_CACHE: tuple[str, ...] | None = None
# option_id (lowercase) -> the Playbook label to show in the UI's interpreted line.
_OPTION_LABELS_CACHE: dict[str, str] | None = None


def governing_law_option_ids() -> tuple[str, ...]:
    """The Playbook ``governing_law`` approved-option ids (the FE/back enum allowlist).

    Sourced from the active Playbook's ``governing_law`` approved_options, mirroring
    how ``dashboard_search_intent.ALLOWED_STATUSES`` is sourced from ``workflow``.
    Empty tuple when the Playbook is unavailable (the dimension simply isn't offered).
    """
    global _OPTION_IDS_CACHE
    if _OPTION_IDS_CACHE is None:
        _build_option_caches()
    return _OPTION_IDS_CACHE or ()


def governing_law_label(option_id: object) -> str:
    """The friendly UI label for a governing-law option id, sourced from the Playbook.

    Reads the matching approved option's ``label`` (falling back to its ``value``)
    so the dashboard's interpreted line ("Governed by ...") shows exactly the label
    the Playbook defines. Falls back to a title-cased id for any option not in the
    active Playbook (a future option, or an unavailable Playbook), so callers always
    get a human-readable string -- never an empty one.
    """
    token = str(option_id or "").strip()
    if not token:
        return ""
    global _OPTION_LABELS_CACHE
    if _OPTION_LABELS_CACHE is None:
        _build_option_caches()
    label = (_OPTION_LABELS_CACHE or {}).get(token.lower())
    return label or token.replace("_", " ").title()


def normalize_governing_law(value: object) -> str:
    """Map a free-text law value (or an option id) to a Playbook approved-option id.

    Matches case-insensitively against each option's id, value, label, and aliases.
    Returns ``""`` when the value matches no approved option (so an unapproved or
    unrecognised law is never surfaced as a filterable governing-law dimension).
    """
    token = str(value or "").strip().lower()
    if not token:
        return ""
    lookup = _option_lookup()
    return lookup.get(token, "")


def derive_governing_law(matter: dict[str, Any]) -> str:
    """Best-available governing-law option id for a matter, or ``""`` when absent.

    Preference: (1) a generated NDA's manifest ``governing_law_value`` normalised to
    its approved-option id, (2) an inbound matter's detected APPROVED governing law
    (from the ``governing_law`` review clause). Returns ``""`` when neither yields an
    approved option -- callers treat the empty string as "governing law unknown for
    this matter" and a ``governing_law`` filter simply won't include it.
    """
    generated = normalize_governing_law(_governing_law_value_from_generation(matter))
    if generated:
        return generated
    return _governing_law_option_from_review(matter)


def _governing_law_value_from_generation(matter: dict[str, Any]) -> str:
    """Pull ``governing_law_value`` from the generated artifact's manifest.

    Mirrors ``artifact_registry.counterparty_from_generation``: the generation
    manifest is stashed on the artifact's ``metadata['generation']`` (the matter-
    level intake_metadata drops unknown keys, so the artifact metadata is the
    reliable source).
    """
    for artifact in artifact_registry.matter_artifacts(matter):
        metadata = artifact.metadata if isinstance(artifact.metadata, dict) else {}
        generation = metadata.get("generation")
        if isinstance(generation, dict):
            value = str(generation.get("governing_law_value") or "").strip()
            if value:
                return value
    return ""


def _governing_law_option_from_review(matter: dict[str, Any]) -> str:
    """Detect an APPROVED governing law from the inbound review result.

    Reads the ``governing_law`` clause's ``governing_law_analysis.candidate_records``
    (the law text the checker matched in the counterparty's document) and returns the
    option id of the first candidate that maps to an approved option. An unapproved /
    unclear / absent law surfaces ``""`` -- we never imply a precision we don't have.
    """
    review_result = matter.get("review_result")
    if not isinstance(review_result, dict):
        return ""
    clauses = review_result.get("clauses")
    if not isinstance(clauses, list):
        return ""
    for clause in clauses:
        if not isinstance(clause, dict) or str(clause.get("id") or "") != "governing_law":
            continue
        analysis = clause.get("governing_law_analysis")
        if not isinstance(analysis, dict):
            continue
        for record in analysis.get("candidate_records") or []:
            if not isinstance(record, dict) or not record.get("approved"):
                continue
            option_id = normalize_governing_law(record.get("value"))
            if option_id:
                return option_id
    return ""


def _option_lookup() -> dict[str, str]:
    global _OPTION_LOOKUP_CACHE
    if _OPTION_LOOKUP_CACHE is None:
        _build_option_caches()
    return _OPTION_LOOKUP_CACHE or {}


def reset_caches() -> None:
    """Drop the cached Playbook option maps (tests / a Playbook republish)."""
    global _OPTION_LOOKUP_CACHE, _OPTION_IDS_CACHE, _OPTION_LABELS_CACHE
    _OPTION_LOOKUP_CACHE = None
    _OPTION_IDS_CACHE = None
    _OPTION_LABELS_CACHE = None


def _build_option_caches() -> None:
    global _OPTION_LOOKUP_CACHE, _OPTION_IDS_CACHE, _OPTION_LABELS_CACHE
    options = _approved_governing_law_options()
    lookup: dict[str, str] = {}
    labels: dict[str, str] = {}
    ids: list[str] = []
    for option in options:
        option_id = str(option.get("id") or "").strip()
        if not option_id:
            continue
        ids.append(option_id.lower())
        lookup[option_id.lower()] = option_id.lower()
        label = str(option.get("label") or option.get("value") or "").strip()
        if label:
            labels[option_id.lower()] = label
        for key in ("value", "label"):
            token = str(option.get(key) or "").strip().lower()
            if token:
                lookup.setdefault(token, option_id.lower())
        aliases = option.get("aliases")
        if isinstance(aliases, (list, tuple)):
            for alias in aliases:
                token = str(alias or "").strip().lower()
                if token:
                    lookup.setdefault(token, option_id.lower())
    _OPTION_LOOKUP_CACHE = lookup
    _OPTION_IDS_CACHE = tuple(ids)
    _OPTION_LABELS_CACHE = labels


def _approved_governing_law_options() -> list[Mapping[str, Any]]:
    """The active Playbook's ``governing_law`` approved_options (best-effort).

    Loaded lazily and tolerant of an unavailable Playbook: any failure yields an
    empty list, so the governing-law dimension is simply not offered rather than
    breaking the matter projection.
    """
    try:
        from . import playbook_runtime  # noqa: PLC0415 -- avoid import cycle at load.

        bundle = playbook_runtime.ensure_active_playbook_bundle()
        playbook = bundle.playbook if bundle is not None else {}
    except Exception:  # noqa: BLE001 -- a missing/broken Playbook just disables the dim.
        return []
    if not isinstance(playbook, Mapping):
        return []
    clause = _governing_law_clause(playbook)
    rules = clause.get("rules") if isinstance(clause, Mapping) else {}
    options = (rules or {}).get("approved_options") if isinstance(rules, Mapping) else []
    if not isinstance(options, list):
        return []
    return [option for option in options if isinstance(option, Mapping)]


def _governing_law_clause(playbook: Mapping[str, Any]) -> Mapping[str, Any]:
    clauses = playbook.get("clauses")
    if isinstance(clauses, list):
        for clause in clauses:
            if isinstance(clause, Mapping) and str(clause.get("id") or "") == "governing_law":
                return clause
    return {}
