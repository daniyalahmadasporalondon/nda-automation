"""Corpus index — a read-only filing-cabinet view of a user's whole NDA corpus.

The Corpus tab groups a user's NDAs **Counterparty -> Contract (matter) ->
lifecycle artifacts**. There is no search, no AI, and no write/send/delete action
here; this module only *reads* and *reconciles* two sources of truth:

* **App-state (rich/fast):** ``repository.list_matters`` + :mod:`artifact_registry`
  + :mod:`workflow` — the authoritative, tenant-filtered live state.
* **Drive (durable/complete):** the app-owned ``NDAs`` tree, crawled read-only via
  the four :mod:`drive_integration` listing helpers, with each matter folder's
  ``metadata/matter_summary.json`` as the reconciliation record.

The two are merged by ``matter_id`` so a matter that survives only in Drive (after
a ``/tmp`` wipe wiped app-state) still appears — that is the whole point of the
Drive pass. The app-state pass is the tenant filter and runs every request; only
the (heavier) Drive listing is cached, per-owner, behind a short TTL.

drive.file scope keeps this safe across tenants: the Drive token is the signed-in
user's own, and ``drive.file`` only exposes folders THIS app created for THIS user,
so the Drive crawl can never surface another tenant's documents.

This module is a pure leaf (like ``matter_summary`` / ``workflow``): it takes a
repository + ids + an optional injected ``drive_service`` and ``clock``, so it is
fully testable without HTTP or a live Drive.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from . import (
    app_settings,
    artifact_registry,
    drive_integration,
    governing_law_view,
    playbook_runtime,
    review_state,
    workflow,
)

# How long a per-owner Drive listing stays warm before a fresh crawl. The
# app-state pass is always cheap and runs every request; only the Drive crawl is
# cached so the tab does not hammer Drive on every load.
CORPUS_DRIVE_CACHE_TTL_SECONDS = 300

# drive.reason codes the frontend keys off when reconciled=false.
REASON_NOT_CONNECTED = "not_connected"
REASON_DRIVE_ERROR = "drive_error"
REASON_RATE_LIMITED = "rate_limited"
REASON_DRIVE_SKIPPED = "drive_skipped"
REASON_DRIVE_TIMEOUT = "drive_timeout"

# Wall-clock budget for the whole Drive crawl. The crawl makes a chain of blocking
# Google Drive HTTP calls (find_folder / list_child_folders / find_child_file /
# download_file_bytes), each of which can stall on the Drive client's own (much
# longer) socket timeout when the Drive backend is unhealthy. Without a bound the
# crawl can hang ~30s before failing, and ``/api/corpus`` times out with it. A
# short overall deadline makes the failing/slow case give up fast (reason
# ``drive_timeout``) while a healthy crawl — which is a handful of quick calls —
# finishes well inside the budget and reconciles normally.
CORPUS_DRIVE_CRAWL_DEADLINE_SECONDS = 4.0

# Reconciliation provenance for a matter.
SOURCE_APP = "app"
SOURCE_DRIVE = "drive"
SOURCE_BOTH = "both"

# role -> lifecycle stage label for an artifact. Mirrors artifact_registry.stage_for
# but is kept simple/stable for display (the FE shows it verbatim). An outbound
# artifact (role "sent") reads as "sent".
_ROLE_STAGE_LABELS = {
    artifact_registry.ROLE_ORIGINAL: "received",
    artifact_registry.ROLE_REDLINE: "ai_redline",
    artifact_registry.ROLE_REVIEWED: "legal_review",
    artifact_registry.ROLE_GENERATED: "draft",
    artifact_registry.ROLE_COUNTER: "counter",
    artifact_registry.ROLE_SENT: "sent",
    artifact_registry.ROLE_SIGNED: "signed",
}

# The schema version stamped on a matter_summary.json ``facets`` block. corpus_index
# keys ``facets_available`` off the PRESENCE of this block, so a legacy summary
# written before the facets enrichment degrades gracefully (see _drive_facets).
FACETS_SCHEMA_VERSION = 1

# Clause-presence facets surfaced as their own keys (mirroring how
# ``governing_law`` is its own scalar facet) so the FE rich-facet rail can light
# them up + filter on them. The FE keys ``non_solicit``/``non_compete`` map to the
# engine clause ids ``non_solicitation``/``non_compete`` -- note the
# solicit/solicitation naming mismatch. Each facet resolves to the sentinel string
# ``_CLAUSE_PRESENT`` when its clause id is in ``has_clauses`` (so the FE
# ``matterFacetValue`` reads a single non-empty value -> one "Present" option whose
# count == filtered-result parity, exactly like governing_law), or ``None`` when
# absent (-> the FE treats it as missing and the group skips that matter). NOTE
# (verified 2026-06-17): these clause ids are NOT in the active playbook, so no
# engine emits them today and both facets currently match zero matters; the wiring
# is correct so they populate the moment the playbook gains the clauses. A matter
# never resolves to a false "absent" claim -- absence is None, not a negative match.
_CLAUSE_PRESENT = "present"
# FE rich-facet key -> the engine clause id whose presence resolves it.
_CLAUSE_FACET_IDS: dict[str, str] = {
    "non_solicit": "non_solicitation",
    "non_compete": "non_compete",
}


def _clause_facet(has_clauses: Any, clause_id: str) -> str | None:
    """``_CLAUSE_PRESENT`` when ``clause_id`` is in ``has_clauses``, else ``None``."""
    if isinstance(has_clauses, list) and clause_id in has_clauses:
        return _CLAUSE_PRESENT
    return None


# --- master-filter facet vocabularies -------------------------------------
# Six additional facet dimensions, each derived ONLY from data already on the
# matter / review_result (no new review, no playbook change, no live extraction),
# mirroring how governing_law is derived. Each degrades to None / [] / "" (the
# graceful-degradation default) so a facet filter never positively matches on
# missing data.

# mutuality: from the mutuality review clause's verdict + analysis.
MUTUALITY_MUTUAL = "mutual"
MUTUALITY_ONE_WAY = "one_way"
_MUTUALITY_CLAUSE_ID = "mutuality"

# term_band: bucket the existing term_years scalar (the same scalar the term facet
# surfaces). Boundaries: <=2y, 3-5y (anything >2y and <=5y), >5y.
TERM_BAND_SHORT = "<=2y"
TERM_BAND_MID = "3-5y"
TERM_BAND_LONG = ">5y"

# restraint_types: tag the non_circumvention finding's flagged text by restraint
# family, reusing the EXISTING prohibited_positions regexes (no new clause). Only
# these three families are surfaced as restraint types.
_RESTRAINT_FAMILIES: tuple[str, ...] = ("non_compete", "non_solicit", "non_circumvention")
_NON_CIRCUMVENTION_CLAUSE_ID = "non_circumvention"

# review_outcome: collapse the per-clause review verdicts (via review_state) to one
# document-level outcome. has_fail when any clause fails (check); else needs_review
# when any clause needs review; else clean when at least one clause passed; None when
# the matter was never (AI-)reviewed -- so an unreviewed matter is never claimed clean.
REVIEW_OUTCOME_CLEAN = "clean"
REVIEW_OUTCOME_NEEDS_REVIEW = "needs_review"
REVIEW_OUTCOME_HAS_FAIL = "has_fail"

# origin: where the document came from. generated = the generator wrote it;
# received = a counterparty sent it (manual upload OR Gmail/email inbound); None when
# the source is unknown (never guessed).
ORIGIN_GENERATED = "generated"
ORIGIN_RECEIVED = "received"


def _review_clauses(review_result: Any) -> list[dict[str, Any]]:
    """The clause dicts from a stored review_result, or [] for any odd shape."""
    if not isinstance(review_result, dict):
        return []
    clauses = review_result.get("clauses")
    if not isinstance(clauses, list):
        return []
    return [clause for clause in clauses if isinstance(clause, dict)]


def _find_clause(review_result: Any, clause_id: str) -> dict[str, Any] | None:
    for clause in _review_clauses(review_result):
        if str(clause.get("id") or "") == clause_id:
            return clause
    return None


def _mutuality_from_result(review_result: Any) -> str | None:
    """``mutual`` | ``one_way`` | ``None`` from the mutuality review clause.

    Reads the clause's ``mutuality_analysis`` (the structured paragraph-id buckets
    the deterministic check attaches) plus its decision, mirroring the
    ``checks.mutuality.reason_code`` polarity:

    * one-way confidentiality paragraphs present -> ``one_way`` (a real asymmetry
      signal, regardless of the clause's pass/review/check decision);
    * else strong reciprocal-obligation paragraphs present -> ``mutual``;
    * else ``None`` -- a weak label / role-only / not-present clause is not a
      confident polarity, so the facet stays unknown (never a guessed polarity).

    Never raises; any odd shape -> ``None``.
    """
    clause = _find_clause(review_result, _MUTUALITY_CLAUSE_ID)
    if clause is None:
        return None
    analysis = clause.get("mutuality_analysis")
    if not isinstance(analysis, dict):
        return None
    one_way_ids = analysis.get("one_way_paragraph_ids")
    if isinstance(one_way_ids, list) and any(str(pid or "").strip() for pid in one_way_ids):
        return MUTUALITY_ONE_WAY
    strong_ids = analysis.get("strong_mutuality_paragraph_ids")
    if isinstance(strong_ids, list) and any(str(pid or "").strip() for pid in strong_ids):
        return MUTUALITY_MUTUAL
    return None


def _term_band_from_years(term_years: float | None) -> str | None:
    """Bucket the term_years scalar into a band; ``None`` when term is unknown."""
    if not isinstance(term_years, (int, float)) or isinstance(term_years, bool):
        return None
    if term_years <= 0:
        return None
    if term_years <= 2:
        return TERM_BAND_SHORT
    if term_years <= 5:
        return TERM_BAND_MID
    return TERM_BAND_LONG


def _restraint_types_from_result(review_result: Any) -> list[str]:
    """The restraint families found in the non_circumvention finding's flagged text.

    Runs the EXISTING ``prohibited_positions`` regexes (sourced from the playbook,
    not re-implemented here) against the non_circumvention clause's flagged text
    (``matched_text`` + the joined ``evidence`` paragraphs) and returns the subset of
    {non_compete, non_solicit, non_circumvention} that match, in a stable order. This
    is the "tag the restraint by type" approach -- no new clause, no new review, no
    live extraction; it only re-reads text the review already flagged. Empty list
    when the clause is absent, carries no flagged text, or matches no family.
    Never raises.
    """
    clause = _find_clause(review_result, _NON_CIRCUMVENTION_CLAUSE_ID)
    if clause is None:
        return []
    text = _clause_flagged_text(clause)
    if not text:
        return []
    try:
        from . import prohibited_positions

        found = {
            label
            for label, pattern in prohibited_positions.PROHIBITED_POSITION_PATTERNS
            if label in _RESTRAINT_FAMILIES and pattern.search(text)
        }
    except Exception:  # noqa: BLE001 -- a regex/import hiccup never breaks the index.
        return []
    return [family for family in _RESTRAINT_FAMILIES if family in found]


def _clause_flagged_text(clause: dict[str, Any]) -> str:
    """The flagged text a clause carries: ``matched_text`` + joined ``evidence``.

    Both engines (deterministic ``checks.common._result`` and ``ai_first_review``)
    emit ``matched_text`` (the joined flagged paragraphs) and ``evidence`` (a list of
    paragraph texts or paragraph dicts). We concatenate both so the family scan sees
    the widest flagged text without re-extracting the document.
    """
    parts: list[str] = []
    matched = clause.get("matched_text")
    if isinstance(matched, str) and matched.strip():
        parts.append(matched)
    evidence = clause.get("evidence")
    if isinstance(evidence, list):
        for item in evidence:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                value = item.get("text") or item.get("matched_text") or ""
                if isinstance(value, str):
                    parts.append(value)
    return "\n".join(part for part in parts if part)


def _review_outcome_from_result(review_result: Any) -> str | None:
    """``clean`` | ``needs_review`` | ``has_fail`` | ``None`` from clause verdicts.

    Derived from the canonical ``review_state`` aggregate (the same single source
    the send gate reads), so this never diverges from the per-clause verdicts:

    * any clause fails (check)        -> ``has_fail``
    * else any clause needs review    -> ``needs_review``
    * else at least one clause passed -> ``clean``
    * otherwise (no clause verdicts)  -> ``None`` (unreviewed; never claimed clean).

    Never raises; an odd shape -> ``None``.
    """
    if not isinstance(review_result, dict) or not _review_clauses(review_result):
        return None
    try:
        state = review_state.review_state_from_result(review_result)
    except Exception:  # noqa: BLE001
        return None
    counts = state.get("counts") if isinstance(state, dict) else None
    if not isinstance(counts, dict):
        return None
    if _safe_count(counts.get("check")) > 0:
        return REVIEW_OUTCOME_HAS_FAIL
    if _safe_count(counts.get("review")) > 0:
        return REVIEW_OUTCOME_NEEDS_REVIEW
    if _safe_count(counts.get("pass")) > 0:
        return REVIEW_OUTCOME_CLEAN
    return None


def _origin_from_source(source_type: Any, *, has_gmail: bool = False) -> str | None:
    """``generated`` | ``received`` | ``None`` from a matter's ``source_type``.

    * ``generated`` / ``send_document`` source_type -> ``generated`` (the generator
      wrote it -- mirrors ``ingestion_service``'s outbound classification);
    * ``manual_upload`` / ``gmail*`` / ``email`` source_type, or a Gmail message id
      present -> ``received`` (a counterparty sent it -- mirrors
      ``artifact_service._source_for_matter``);
    * otherwise ``None`` (unknown -- never guessed).
    """
    token = str(source_type or "").strip().casefold()
    if token in {"generated", "send_document"}:
        return ORIGIN_GENERATED
    if token.startswith("gmail") or token in {"manual_upload", "upload", "email"} or has_gmail:
        return ORIGIN_RECEIVED
    return None


# Workflow statuses that resolve the ``signed`` facet. Anything else (intake /
# review / approval, pre-send) resolves to ``None`` -> "unknown", so a signed
# filter never silently includes or excludes a pre-send matter. Mirrors the FE
# ``matterSigned`` polarity exactly.
_SIGNED_TRUE_STATUSES: frozenset[str] = frozenset({workflow.STATUS_FULLY_SIGNED})
_SIGNED_FALSE_STATUSES: frozenset[str] = frozenset(
    {
        workflow.STATUS_SENT_AWAITING_COUNTERPARTY,
        workflow.STATUS_COUNTER_RECEIVED,
        workflow.STATUS_SENDING,
    }
)


def _empty_facets(*, available: bool) -> dict[str, Any]:
    """The all-empty facet block. With ``available=False`` it is the legacy/degraded
    shape: every facet at its empty/null value so a facet filter never positively
    matches (the graceful-degradation linchpin)."""
    return {
        "governing_law": "",
        "signed": None,
        "has_clauses": [],
        # Clause-presence facets keyed for the FE rich-facet rail (derived from
        # has_clauses membership). None -> the FE treats the facet as absent so a
        # facet filter never positively matches (graceful-degradation default).
        "non_solicit": None,
        "non_compete": None,
        "term_years": None,
        # Master-filter facets. Each at its empty/null value so a facet filter never
        # positively matches a legacy/degraded block (the graceful-degradation
        # linchpin), exactly like governing_law/term_years above.
        "mutuality": None,
        "term_band": None,
        "restraint_types": [],
        "review_outcome": None,
        "clauses_present": [],
        "origin": None,
        # The workflow enums (phase/status) the existing status/phase search
        # dimensions filter on, surfaced here so the FE adapter can reconstruct a
        # workflow_state over a corpus matter (which otherwise only carries the
        # board_column + phase_label display strings). "" -> won't match any enum.
        "phase": "",
        "status": "",
        # The workflow_state failure/gate axes + the review requirement counts the
        # FE matchers (matterNeedsAttention / matterHumanGate / matterHasIssues)
        # read, surfaced here so the FE adapter can reconstruct them over a corpus
        # matter. Without them those filters could NEVER positively match on screen
        # even though the Python twin matches over the same source. False/0 ->
        # won't match any of those filters (graceful-degradation default).
        "needs_attention": False,
        "human_gate": False,
        "requirements_failed": 0,
        "requirements_needs_review": 0,
        # Whether an AI (ai_first) review actually ran for this matter. The
        # has_issues consumer (dashboard_search_intent._corpus_matter_has_issues)
        # gates on this so a deterministic-only verdict never leaks into the
        # "matters with issues" filter -- belt-and-suspenders alongside the
        # write-time gate in _app_requirement_counts / _summary_requirement_counts,
        # and it ALSO neutralizes stale persisted facets written before that gate
        # (they lack this key, so it defaults False here). Default False so a
        # legacy/degraded facet block never positively matches has_issues.
        "ai_review_ran": False,
        # True when this matter's counterparty has >=2 distinct matters in the
        # corpus (a repeat entity). Computed in _group_and_wrap once the whole
        # corpus is known, so the per-matter facet builders cannot know it; they
        # default False and _group_and_wrap stamps the real value. False here is
        # the safe default (a repeat-entity filter never falsely matches).
        "repeat_entity": False,
        "facets_available": available,
    }


def _signed_from_status(status: str) -> bool | None:
    token = str(status or "").strip().lower()
    if token in _SIGNED_TRUE_STATUSES:
        return True
    if token in _SIGNED_FALSE_STATUSES:
        return False
    return None


def _flatten_clause_ids(clause_ids: dict[str, Any]) -> list[str]:
    """Union of the pass/review/check clause-id buckets, de-duplicated + ordered.

    Caveat (verified 2026-06-17): ``non_solicitation``/``non_compete`` are NOT in the
    active ``playbook.json`` (which carries 6 clauses: the 5 native + non_circumvention)
    -- they exist only as the demo dynamic ids the search bar advertises
    (``dashboard_search_intent._DEMO_DYNAMIC_CLAUSE_IDS``). NEITHER engine emits them,
    so today no matter lists them and the keyed clause-presence facets (non_solicit /
    non_compete) match zero matters. The wiring below derives those facets from
    ``has_clauses`` membership so they light up the moment the playbook gains the
    clauses; until then the FE renders an honest empty state. We do NOT add the
    clauses here -- the playbook is the single source of truth (a pending decision).
    """
    seen: dict[str, None] = {}
    if isinstance(clause_ids, dict):
        for bucket in ("pass", "review", "check"):
            ids = clause_ids.get(bucket)
            if isinstance(ids, list):
                for clause_id in ids:
                    token = str(clause_id or "").strip()
                    if token:
                        seen.setdefault(token, None)
    return list(seen)


def _app_clause_ids(matter: dict[str, Any]) -> list[str]:
    """The matter's clause-id buckets, preferring the stored ``review_state`` and
    re-deriving from ``review_result`` clauses only when the stored block is absent."""
    stored = matter.get("review_state")
    if isinstance(stored, dict) and isinstance(stored.get("clause_ids"), dict):
        return _flatten_clause_ids(stored["clause_ids"])
    review_result = matter.get("review_result")
    if isinstance(review_result, dict):
        try:
            derived = review_state.review_state_from_result(review_result)
        except Exception:  # noqa: BLE001 -- an odd review shape never breaks the index.
            return []
        if isinstance(derived, dict) and isinstance(derived.get("clause_ids"), dict):
            return _flatten_clause_ids(derived["clause_ids"])
    return []


def _app_term_years(matter: dict[str, Any]) -> float | None:
    """Best-effort term in years from the stored ``term_and_survival`` clause result.

    Reads the clean ``term_years`` scalar the checker persists; absent -> None (the
    term facet degrades to "unknown" rather than guessing). Never raises.
    """
    review_result = matter.get("review_result")
    if not isinstance(review_result, dict):
        return None
    clauses = review_result.get("clauses")
    if not isinstance(clauses, list):
        return None
    for clause in clauses:
        if not isinstance(clause, dict) or str(clause.get("id") or "") != "term_and_survival":
            continue
        value = clause.get("term_years")
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return None


def _app_facets(matter: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Derive the rich-facet block for an app-state matter (live, from review data).

    Any single derivation failure degrades to the empty value for that facet rather
    than breaking the corpus index; ``facets_available`` stays true because the
    matter IS in app-state (it just may carry sparse review data).
    """
    facets = _empty_facets(available=True)
    try:
        facets["governing_law"] = governing_law_view.derive_governing_law(matter)
    except Exception:  # noqa: BLE001
        facets["governing_law"] = ""
    try:
        facets["signed"] = _signed_from_status(str(state.get("status") or ""))
    except Exception:  # noqa: BLE001
        facets["signed"] = None
    try:
        facets["has_clauses"] = _app_clause_ids(matter)
    except Exception:  # noqa: BLE001
        facets["has_clauses"] = []
    # Surface the clause-presence facets keyed for the FE, derived from the
    # has_clauses list just computed (engine id -> FE key via _CLAUSE_FACET_IDS).
    for facet_key, clause_id in _CLAUSE_FACET_IDS.items():
        facets[facet_key] = _clause_facet(facets["has_clauses"], clause_id)
    try:
        facets["term_years"] = _app_term_years(matter)
    except Exception:  # noqa: BLE001
        facets["term_years"] = None
    # --- master-filter facets (all from data already on the matter) ---
    review_result = matter.get("review_result")
    try:
        facets["mutuality"] = _mutuality_from_result(review_result)
    except Exception:  # noqa: BLE001
        facets["mutuality"] = None
    try:
        facets["term_band"] = _term_band_from_years(facets["term_years"])
    except Exception:  # noqa: BLE001
        facets["term_band"] = None
    try:
        facets["restraint_types"] = _restraint_types_from_result(review_result)
    except Exception:  # noqa: BLE001
        facets["restraint_types"] = []
    try:
        facets["review_outcome"] = _review_outcome_from_result(review_result)
    except Exception:  # noqa: BLE001
        facets["review_outcome"] = None
    # clauses_present is the same set as has_clauses (the clause ids the doc has),
    # surfaced under the master-filter name the FE rail keys off.
    facets["clauses_present"] = list(facets["has_clauses"])
    try:
        facets["origin"] = _origin_from_source(
            matter.get("source_type"), has_gmail=bool(matter.get("gmail_message_id"))
        )
    except Exception:  # noqa: BLE001
        facets["origin"] = None
    facets["phase"] = str(state.get("phase") or "")
    facets["status"] = str(state.get("status") or "")
    # The failure/gate axes come straight from the same workflow_state the Python
    # twin reads, so the FE matcher (after adaptCorpusMatter reconstructs them)
    # mirrors the backend matcher exactly.
    facets["needs_attention"] = state.get("needs_attention") is True
    facets["human_gate"] = state.get("human_gate") is True
    # Whether an AI (ai_first) review actually ran -- the has_issues consumer gates
    # on this. Read defensively so a bad review_result can never break the index.
    try:
        facets["ai_review_ran"] = review_state.review_was_ai_executed(matter.get("review_result"))
    except Exception:  # noqa: BLE001
        facets["ai_review_ran"] = False
    failed, needs_review = _app_requirement_counts(matter)
    facets["requirements_failed"] = failed
    facets["requirements_needs_review"] = needs_review
    return facets


def _app_requirement_counts(matter: dict[str, Any]) -> tuple[int, int]:
    """The (failed, needs_review) requirement counts from the stored review result.

    Same source matter_summary's review digest reads; absent/odd shapes degrade to
    (0, 0) so the has_issues facet never falsely matches. Never raises.

    GATE: surface the counts ONLY when an AI (ai_first) review actually ran for this
    matter (``review_state.review_was_ai_executed`` -- the same signal
    ``matter_view`` gates its ``ai_review_ran`` projection on). A deterministic-only
    matter (e.g. outbound generation, which pins the deterministic engine and defers
    AI to on-demand) carries deterministic requirement counts that would otherwise
    leak into the corpus "has issues" search even though no AI reviewed the document;
    gating here returns (0, 0) so the deterministic verdict never surfaces as an issue.
    """
    review_result = matter.get("review_result")
    if not isinstance(review_result, dict):
        return 0, 0
    if not review_state.review_was_ai_executed(review_result):
        return 0, 0
    return (
        _safe_count(review_result.get("requirements_failed")),
        _safe_count(review_result.get("requirements_needs_review")),
    )


def _safe_count(value: object) -> int:
    """Coerce a requirement-count field to a non-negative int (0 on a bad value)."""
    return max(0, _safe_int(value, 0))


def _drive_facets(summary: dict[str, Any]) -> dict[str, Any]:
    """Read the durable ``facets`` block from a matter_summary.json (see §2).

    A legacy summary written before the facets enrichment has no ``facets`` block;
    that matter degrades to ``facets_available=False`` so a facet filter skips it
    (text/counterparty/date search still works). The values are read defensively so
    a hand-edited durable summary can never break the index.
    """
    raw = summary.get("facets") if isinstance(summary, dict) else None
    if not isinstance(raw, dict) or raw.get("schema_version") is None:
        return _empty_facets(available=False)
    signed = raw.get("signed")
    has_clauses = raw.get("has_clauses")
    has_clause_ids = (
        [str(c).strip() for c in has_clauses if str(c or "").strip()]
        if isinstance(has_clauses, list)
        else []
    )
    term_years = raw.get("term_years")
    term_years_value = (
        float(term_years)
        if isinstance(term_years, (int, float)) and not isinstance(term_years, bool) and term_years > 0
        else None
    )
    # Master-filter facets, read back from the durable block (drive_integration persists
    # them at sync). Each read defensively + degrades to None/[] when absent or
    # hand-edited to an odd shape, so a Drive-only matter still filters by them but a
    # legacy summary (written before this enrichment) never falsely matches. term_band
    # is re-derived from the durable term_years so the band and the scalar can never
    # drift apart on disk.
    raw_restraints = raw.get("restraint_types")
    restraint_types = (
        [str(r).strip() for r in raw_restraints if str(r or "").strip() in _RESTRAINT_FAMILIES]
        if isinstance(raw_restraints, list)
        else []
    )
    raw_clauses_present = raw.get("clauses_present")
    clauses_present = (
        [str(c).strip() for c in raw_clauses_present if str(c or "").strip()]
        if isinstance(raw_clauses_present, list)
        else list(has_clause_ids)
    )
    mutuality = raw.get("mutuality")
    review_outcome = raw.get("review_outcome")
    origin = raw.get("origin")
    # phase/status come from the durable workflow_state block, not the facets block
    # (drive_integration writes them there); read defensively so a hand-edited summary
    # can never break the index.
    workflow_state = summary.get("workflow_state") if isinstance(summary.get("workflow_state"), dict) else {}
    # The durable workflow_state already carries the failure/gate axes (it is a full
    # workflow.workflow_state() snapshot). The requirement counts are persisted in the
    # durable facets block; a summary written before they existed degrades to 0
    # (has_issues never falsely matches), mirroring the other facets.
    return {
        "governing_law": str(raw.get("governing_law") or ""),
        "signed": signed if isinstance(signed, bool) else None,
        "has_clauses": has_clause_ids,
        # Clause-presence facets, derived from the durable has_clauses list (engine
        # id -> FE key). None when absent (mirrors the app-state pass + degradation).
        "non_solicit": _clause_facet(has_clause_ids, _CLAUSE_FACET_IDS["non_solicit"]),
        "non_compete": _clause_facet(has_clause_ids, _CLAUSE_FACET_IDS["non_compete"]),
        "term_years": term_years_value,
        # Master-filter facets read back from the durable block (see above).
        "mutuality": mutuality if mutuality in (MUTUALITY_MUTUAL, MUTUALITY_ONE_WAY) else None,
        "term_band": _term_band_from_years(term_years_value),
        "restraint_types": [family for family in _RESTRAINT_FAMILIES if family in restraint_types],
        "review_outcome": review_outcome
        if review_outcome in (REVIEW_OUTCOME_CLEAN, REVIEW_OUTCOME_NEEDS_REVIEW, REVIEW_OUTCOME_HAS_FAIL)
        else None,
        "clauses_present": clauses_present,
        "origin": origin if origin in (ORIGIN_GENERATED, ORIGIN_RECEIVED) else None,
        "phase": str((workflow_state or {}).get("phase") or ""),
        "status": str((workflow_state or {}).get("status") or ""),
        "needs_attention": (workflow_state or {}).get("needs_attention") is True,
        "human_gate": (workflow_state or {}).get("human_gate") is True,
        "requirements_failed": _safe_count(raw.get("requirements_failed")),
        "requirements_needs_review": _safe_count(raw.get("requirements_needs_review")),
        # Read the AI-ran signal back from the durable facets block (the has_issues
        # consumer gates on it). A summary written before this key existed -- or any
        # stale facet block from before the gate -- lacks it, so it defaults False
        # (has_issues never falsely matches), mirroring the other facets.
        "ai_review_ran": raw.get("ai_review_ran") is True,
        "facets_available": True,
    }


# --- per-owner Drive-listing cache ----------------------------------------
_CACHE_LOCK = threading.Lock()
# owner_user_id -> {"built_at": float (monotonic-ish epoch), "built_at_iso": str,
#                   "drive": {...}, "drive_matters": {matter_id: {...}},
#                   "drive_orphans": [ {...} ]}
_DRIVE_CACHE: dict[str, dict[str, Any]] = {}


def invalidate_cache(owner_user_id: str = "") -> None:
    """Drop the cached Drive listing for one owner, or the whole cache when empty."""
    with _CACHE_LOCK:
        if owner_user_id:
            _DRIVE_CACHE.pop(owner_user_id, None)
        else:
            _DRIVE_CACHE.clear()


class _DriveCrawlTimeout(Exception):
    """Internal sentinel: the Drive crawl blew its wall-clock deadline.

    Raised by the crawl's deadline checks and caught in :func:`_drive_pass`, where
    it degrades to an app-state-only corpus with ``drive.reason='drive_timeout'``.
    Kept private to this module so the Drive client / drive_integration contract is
    untouched.
    """


def _now_epoch(clock: Optional[Callable[[], float]]) -> float:
    if clock is not None:
        return float(clock())
    return datetime.now(timezone.utc).timestamp()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _crawl_deadline(clock: Optional[Callable[[], float]]) -> Callable[[], None]:
    """Build a no-arg guard that raises :class:`_DriveCrawlTimeout` once the crawl
    has been running longer than ``CORPUS_DRIVE_CRAWL_DEADLINE_SECONDS``.

    The crawl calls this guard *before* each blocking Drive request, so a slow or
    hung crawl bails out within roughly one request of the budget rather than
    grinding through every remaining call. Uses the injected ``clock`` (so tests
    can drive the deadline deterministically) and falls back to wall-clock time.
    """
    started_at = _now_epoch(clock)

    def _check() -> None:
        if (_now_epoch(clock) - started_at) >= CORPUS_DRIVE_CRAWL_DEADLINE_SECONDS:
            raise _DriveCrawlTimeout()

    return _check


# --- public entrypoint -----------------------------------------------------
def build_corpus(
    repository,
    owner_user_id: str,
    drive_owner_user_id: str,
    *,
    drive_service: Any | None = None,
    force_refresh: bool = False,
    clock: Optional[Callable[[], float]] = None,
) -> dict[str, Any]:
    """Build the corpus index payload for one owner (see module docstring).

    Reads app-state every call (the authoritative tenant filter); the Drive crawl
    is cached per owner under a short TTL. ``force_refresh`` bypasses the cache.
    ``drive_service``/``clock`` are injectable for tests. A Drive hiccup never
    raises out of here — it degrades to an app-state-only corpus with
    ``drive.reconciled=false`` and a ``drive.reason`` code.
    """
    # 1. App-state pass — always runs; it is the tenant filter and the rich source.
    app_matters = _build_app_state_matters(repository, owner_user_id)

    # 2. Drive pass — cached per owner; only when Drive is connected.
    drive_result = _drive_pass(
        drive_owner_user_id,
        drive_service=drive_service,
        force_refresh=force_refresh,
        clock=clock,
    )

    # 3. Merge by matter_id + 4. group/sort.
    return _assemble(app_matters, drive_result)


def flatten_corpus(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a ``build_corpus`` payload's ``groups[].matters[]`` into a flat list.

    The single place the grouped corpus payload is unfolded into the flat matter
    list the search matcher / analytical counts consume, so the FE adapter and the
    assistant share one contract. Tolerant of a malformed payload (returns []).
    """
    groups = payload.get("groups") if isinstance(payload, dict) else None
    if not isinstance(groups, list):
        return []
    flat: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        matters = group.get("matters")
        if isinstance(matters, list):
            flat.extend(matter for matter in matters if isinstance(matter, dict))
    return flat


# --- app-state pass --------------------------------------------------------
def _resolve_playbook_resolvers() -> tuple[Callable[[], dict[str, Any]], Callable[[], str]]:
    """Resolve the active playbook runtime ONCE and return constant resolvers.

    Thin alias over ``playbook_runtime.resolve_playbook_resolvers`` (the shared
    helper that both this corpus build and ``matter_view.public_matters`` use to
    collapse the per-matter playbook.json flock+read+validate to one read per
    batch). Kept as a module-local name so existing call sites/tests stay stable.
    """
    return playbook_runtime.resolve_playbook_resolvers()


def _build_app_state_matters(repository, owner_user_id: str) -> dict[str, dict[str, Any]]:
    """Map matter_id -> a partially-built CorpusMatter from app-state."""
    matters: dict[str, dict[str, Any]] = {}
    # Resolve the active playbook runtime ONCE per build and thread the constant
    # resolvers through every matter's workflow_state, instead of paying a
    # playbook.json flock+read+validate per matter in the approval-gate staleness
    # check.
    runtime_func, hash_func = _resolve_playbook_resolvers()
    for matter in repository.list_matters(owner_user_id=owner_user_id):
        matter_id = str(matter.get("id") or "")
        if not matter_id:
            continue
        counterparty = artifact_registry.derive_counterparty(matter)
        state = workflow.workflow_state(
            matter,
            current_playbook_hash_func=hash_func,
            current_runtime_func=runtime_func,
        )
        artifacts = artifact_registry.matter_artifacts(matter)
        drive_block = matter.get("drive") if isinstance(matter.get("drive"), dict) else {}
        synced_url = str(drive_block.get("matter_folder_url") or "")

        matters[matter_id] = {
            "matter_id": matter_id,
            "counterparty": counterparty,
            "title": _app_title(matter),
            "created_at": str(matter.get("created_at") or ""),
            # Workflow axis = the Repository board column (FE renders it via
            # RepositoryModel.boardColumnLabel). The dead 6-phase phase_label is
            # NOT surfaced; "On file" is a SOURCE state, not a workflow status.
            # An EXECUTED matter rolls up to the off-board sentinel (board_column
            # == ""), so it leaves the WIP board -- but the corpus is the full
            # archive and still lists it. Fall back to the workflow phase
            # ("executed") so its corpus status reads correctly instead of blank.
            "status": str(state.get("board_column") or state.get("phase") or ""),
            "source": SOURCE_APP,
            "in_app": True,
            "open_matter_url": _open_matter_url(matter_id),
            "open_in_drive_url": synced_url,
            "duplicate": False,
            "duplicate_folder_urls": [],
            "facets": _app_facets(matter, state),
            "artifacts": [
                _app_artifact(matter_id, sequence, artifact)
                for sequence, artifact in enumerate(artifacts, start=1)
            ],
        }
    return matters


def _app_title(matter: dict[str, Any]) -> str:
    for key in ("document_title", "subject"):
        value = str(matter.get(key) or "").strip()
        if value:
            return value
    return "NDA"


def _app_artifact(matter_id: str, sequence: int, artifact: artifact_registry.Artifact) -> dict[str, Any]:
    return {
        "artifact_id": artifact.id,
        "sequence": sequence,
        "role": artifact.role,
        "actor": artifact.actor,
        "version": artifact.version,
        "filename": artifact.name,
        "stage_label": _stage_label(artifact.role),
        "created_at": artifact.created_at,
        "drive_file_url": "",
        "download_url": _artifact_download_url(matter_id, artifact.id),
    }


def _stage_label(role: str) -> str:
    return _ROLE_STAGE_LABELS.get(str(role or "").strip().casefold(), str(role or "") or "doc")


def _safe_int(value: object, default: int) -> int:
    """Coerce a Drive summary integer field, falling back to ``default``.

    ``matter_summary.json`` is durable, hand-editable Drive data, so a record may
    carry a non-numeric ``sequence``/``version`` (e.g. ``"v2"``). Mirrors
    :func:`artifact_registry._coerce_version`: a bad value must never raise out of
    the Drive pass — it degrades to a sane default instead.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# --- Drive pass ------------------------------------------------------------
def _drive_pass(
    drive_owner_user_id: str,
    *,
    drive_service: Any | None,
    force_refresh: bool,
    clock: Optional[Callable[[], float]],
) -> dict[str, Any]:
    """Return ``{drive, drive_matters, drive_orphans}`` for the merge step.

    ``drive`` is the response ``drive`` block. ``drive_matters`` maps a Drive
    ``matter_id`` to its reconciliation record + folder bookkeeping;
    ``drive_orphans`` are summary-less folders (degraded entries). On any Drive
    error/disconnection the maps are empty and ``drive.reconciled=false``.
    """
    connected = drive_service is not None or drive_integration.drive_connected(drive_owner_user_id)
    if not connected:
        return {
            "drive": _drive_block(connected=False, reconciled=False, reason=REASON_NOT_CONNECTED),
            "drive_matters": {},
            "drive_orphans": [],
        }

    # Serve from a warm cache unless a refresh is forced.
    if not force_refresh:
        cached = _cached_drive(drive_owner_user_id, clock)
        if cached is not None:
            return cached

    try:
        crawl = _crawl_drive(
            drive_owner_user_id,
            drive_service=drive_service,
            deadline=_crawl_deadline(clock),
        )
    except _DriveCrawlTimeout:
        # The crawl blew its short wall-clock budget — give up fast instead of
        # letting the Drive client's long socket timeout hang /api/corpus.
        return {
            "drive": _drive_block(connected=True, reconciled=False, reason=REASON_DRIVE_TIMEOUT),
            "drive_matters": {},
            "drive_orphans": [],
        }
    except drive_integration.DriveRateLimitError:
        return {
            "drive": _drive_block(connected=True, reconciled=False, reason=REASON_RATE_LIMITED),
            "drive_matters": {},
            "drive_orphans": [],
        }
    except drive_integration.DriveNotConnectedError:
        return {
            "drive": _drive_block(connected=False, reconciled=False, reason=REASON_NOT_CONNECTED),
            "drive_matters": {},
            "drive_orphans": [],
        }
    except drive_integration.DriveIntegrationError:
        return {
            "drive": _drive_block(connected=True, reconciled=False, reason=REASON_DRIVE_ERROR),
            "drive_matters": {},
            "drive_orphans": [],
        }
    except Exception:  # noqa: BLE001
        # The module contract is "a Drive hiccup never raises out of build_corpus; it
        # degrades to an app-state-only corpus". The specific catches above cover the
        # wrapped drive_integration errors, but an UNEXPECTED exception type -- a raw
        # google-api/httplib2 client error or a socket error that was never wrapped --
        # would otherwise escape _drive_pass (and build_corpus, which has no catch),
        # killing the whole /api/corpus request so even the app-state matters drop and
        # the Corpus tab goes down. Degrade ANY unexpected Drive failure to
        # app-state-only (reason=drive_error), mirroring the DriveIntegrationError
        # branch, so the corpus always renders the live matters.
        return {
            "drive": _drive_block(connected=True, reconciled=False, reason=REASON_DRIVE_ERROR),
            "drive_matters": {},
            "drive_orphans": [],
        }

    built_at_iso = _now_iso()
    result = {
        "drive": _drive_block(
            connected=True,
            reconciled=True,
            reason="",
            built_at=built_at_iso,
            from_cache=False,
            stale=False,
        ),
        "drive_matters": crawl["drive_matters"],
        "drive_orphans": crawl["drive_orphans"],
    }
    _store_drive_cache(drive_owner_user_id, result, built_at_iso, clock)
    return result


def _cached_drive(
    drive_owner_user_id: str,
    clock: Optional[Callable[[], float]],
) -> dict[str, Any] | None:
    now = _now_epoch(clock)
    with _CACHE_LOCK:
        entry = _DRIVE_CACHE.get(drive_owner_user_id)
        if entry is None:
            return None
        if (now - float(entry.get("built_at", 0.0))) > CORPUS_DRIVE_CACHE_TTL_SECONDS:
            return None
        # Return a copy so callers cannot mutate the cached entry in place.
        return {
            "drive": {**entry["drive"], "from_cache": True},
            "drive_matters": entry["drive_matters"],
            "drive_orphans": entry["drive_orphans"],
        }


def _store_drive_cache(
    drive_owner_user_id: str,
    result: dict[str, Any],
    built_at_iso: str,
    clock: Optional[Callable[[], float]],
) -> None:
    with _CACHE_LOCK:
        _DRIVE_CACHE[drive_owner_user_id] = {
            "built_at": _now_epoch(clock),
            "drive": dict(result["drive"]),
            "drive_matters": result["drive_matters"],
            "drive_orphans": result["drive_orphans"],
        }


def _crawl_drive(
    drive_owner_user_id: str,
    *,
    drive_service: Any | None,
    deadline: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Read-only crawl of the app-owned ``NDAs`` tree -> reconciliation records.

    Layout: ``{root_parent}/NDAs/{counterparty}/{matter}/metadata/matter_summary.json``.
    Each matter folder's summary is the reconciliation record. Folders without a
    parseable summary become degraded "orphan" entries naming the folder.

    ``deadline`` (when supplied) is a no-arg guard called before each blocking Drive
    request; it raises :class:`_DriveCrawlTimeout` once the crawl's wall-clock budget
    is spent, so a slow/hung Drive backend bails out fast instead of hanging.
    """
    check_deadline = deadline if deadline is not None else (lambda: None)

    settings = app_settings.drive_settings()
    parent_id = str(settings.get("folder_id") or "")
    check_deadline()
    root_id = drive_integration.find_folder(
        name=drive_integration.DEFAULT_ROOT_FOLDER_NAME,
        parent_id=parent_id,
        owner_user_id=drive_owner_user_id,
        service=drive_service,
    )
    drive_matters: dict[str, dict[str, Any]] = {}
    drive_orphans: list[dict[str, Any]] = []
    if not root_id:
        return {"drive_matters": drive_matters, "drive_orphans": drive_orphans}

    check_deadline()
    counterparty_folders = drive_integration.list_child_folders(
        parent_id=root_id, owner_user_id=drive_owner_user_id, service=drive_service
    )
    for cp_folder in counterparty_folders:
        cp_name = str(cp_folder.get("name") or "")
        cp_id = str(cp_folder.get("id") or "")
        if not cp_id:
            continue
        check_deadline()
        matter_folders = drive_integration.list_child_folders(
            parent_id=cp_id, owner_user_id=drive_owner_user_id, service=drive_service
        )
        for matter_folder in matter_folders:
            folder_id = str(matter_folder.get("id") or "")
            folder_name = str(matter_folder.get("name") or "")
            if not folder_id:
                continue
            folder_url = drive_integration.folder_web_url(folder_id)
            summary = _read_matter_summary(
                folder_id,
                drive_owner_user_id,
                drive_service=drive_service,
                deadline=check_deadline,
            )
            if summary is None:
                drive_orphans.append(
                    {
                        "counterparty": cp_name,
                        "folder_name": folder_name,
                        "folder_id": folder_id,
                        "folder_url": folder_url,
                    }
                )
                continue
            matter_id = str(summary.get("matter_id") or "")
            record = {
                "summary": summary,
                "counterparty": cp_name,
                "folder_id": folder_id,
                "folder_url": folder_url,
                "folder_name": folder_name,
            }
            if not matter_id:
                # A summary without a matter_id cannot be a join key; treat the
                # folder as an orphan so it still surfaces (named by the folder).
                drive_orphans.append(
                    {
                        "counterparty": cp_name,
                        "folder_name": folder_name,
                        "folder_id": folder_id,
                        "folder_url": folder_url,
                        "summary": summary,
                    }
                )
                continue
            existing = drive_matters.get(matter_id)
            if existing is None:
                record["duplicate_folder_urls"] = []
                drive_matters[matter_id] = record
            else:
                # Same matter_id in a second Drive folder => duplicate. Keep the
                # first as the canonical folder; list the rest.
                existing["duplicate_folder_urls"].append(folder_url)
    return {"drive_matters": drive_matters, "drive_orphans": drive_orphans}


def _read_matter_summary(
    matter_folder_id: str,
    drive_owner_user_id: str,
    *,
    drive_service: Any | None,
    deadline: Callable[[], None] | None = None,
) -> dict[str, Any] | None:
    check_deadline = deadline if deadline is not None else (lambda: None)
    check_deadline()
    metadata_id = drive_integration.find_folder(
        name=drive_integration.METADATA_FOLDER_NAME,
        parent_id=matter_folder_id,
        owner_user_id=drive_owner_user_id,
        service=drive_service,
    )
    if not metadata_id:
        return None
    check_deadline()
    summary_file_id = drive_integration.find_child_file(
        name=drive_integration.MATTER_SUMMARY_FILENAME,
        parent_id=metadata_id,
        owner_user_id=drive_owner_user_id,
        service=drive_service,
    )
    if not summary_file_id:
        return None
    check_deadline()
    raw = drive_integration.download_file_bytes(
        file_id=summary_file_id, owner_user_id=drive_owner_user_id, service=drive_service
    )
    if not raw:
        return None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


# --- merge + group ---------------------------------------------------------
def _assemble(
    app_matters: dict[str, dict[str, Any]],
    drive_result: dict[str, Any],
) -> dict[str, Any]:
    drive_matters: dict[str, dict[str, Any]] = drive_result["drive_matters"]
    drive_orphans: list[dict[str, Any]] = drive_result["drive_orphans"]

    merged: list[dict[str, Any]] = []

    # App-state matters, enriched with Drive when the matter_id matches.
    for matter_id, matter in app_matters.items():
        drive_record = drive_matters.get(matter_id)
        if drive_record is not None:
            _merge_drive_into_app(matter, drive_record)
        merged.append(matter)

    # Drive-only matters: a summary matter_id not present in app-state.
    for matter_id, drive_record in drive_matters.items():
        if matter_id in app_matters:
            continue
        merged.append(_drive_only_matter(matter_id, drive_record))

    # Summary-less folders: degraded single entries naming the folder.
    for orphan in drive_orphans:
        merged.append(_orphan_matter(orphan))

    return _group_and_wrap(merged, drive_result["drive"])


def _merge_drive_into_app(matter: dict[str, Any], drive_record: dict[str, Any]) -> None:
    """In-both: prefer app-state fields, fill gaps from the summary, add Drive links."""
    matter["source"] = SOURCE_BOTH
    matter["open_in_drive_url"] = drive_record["folder_url"]
    summary = drive_record.get("summary") or {}

    if not matter.get("created_at"):
        matter["created_at"] = str(summary.get("created_at") or "")
    if not matter.get("counterparty"):
        matter["counterparty"] = str(summary.get("counterparty") or "")

    duplicate_urls = list(drive_record.get("duplicate_folder_urls") or [])
    if duplicate_urls:
        matter["duplicate"] = True
        matter["duplicate_folder_urls"] = duplicate_urls

    # Backfill drive_file_url onto app artifacts by artifact_id where the summary
    # carries a Drive URL (a download still goes through the app-state route).
    drive_urls = _summary_artifact_urls(summary)
    if drive_urls:
        for artifact in matter["artifacts"]:
            url = drive_urls.get(artifact["artifact_id"])
            if url:
                artifact["drive_file_url"] = url


def _drive_only_matter(matter_id: str, drive_record: dict[str, Any]) -> dict[str, Any]:
    summary = drive_record.get("summary") or {}
    workflow_state = summary.get("workflow_state") if isinstance(summary.get("workflow_state"), dict) else {}
    duplicate_urls = list(drive_record.get("duplicate_folder_urls") or [])
    counterparty = str(summary.get("counterparty") or "") or str(drive_record.get("counterparty") or "")
    return {
        "matter_id": str(summary.get("matter_id") or ""),
        "counterparty": counterparty or artifact_registry.COUNTERPARTY_UNKNOWN,
        "title": _drive_only_title(summary, drive_record),
        "created_at": str(summary.get("created_at") or ""),
        # Drive-only: board_column from the summary if present, else "" so the
        # status chip renders "—". "On file" lives on the SOURCE badge only.
        "status": str((workflow_state or {}).get("board_column") or ""),
        "source": SOURCE_DRIVE,
        "in_app": False,
        "open_matter_url": "",
        "open_in_drive_url": drive_record["folder_url"],
        "duplicate": bool(duplicate_urls),
        "duplicate_folder_urls": duplicate_urls,
        "facets": _drive_facets(summary),
        "artifacts": _drive_only_artifacts(summary),
    }


def _drive_only_title(summary: dict[str, Any], drive_record: dict[str, Any]) -> str:
    for key in ("document_title", "subject"):
        value = str(summary.get(key) or "").strip()
        if value:
            return value
    folder_name = str(drive_record.get("folder_name") or "").strip()
    return folder_name or "NDA"


def _drive_only_artifacts(summary: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    raw = summary.get("artifacts")
    if not isinstance(raw, list):
        return artifacts
    for record in raw:
        if not isinstance(record, dict):
            continue
        role = str(record.get("role") or "")
        artifacts.append(
            {
                "artifact_id": str(record.get("artifact_id") or ""),
                "sequence": _safe_int(record.get("sequence") or 0, 0),
                "role": role,
                "actor": str(record.get("actor") or ""),
                "version": _safe_int(record.get("version") or 1, 1),
                "filename": str(record.get("filename") or ""),
                "stage_label": _stage_label(role),
                "created_at": str(record.get("created_at") or ""),
                "drive_file_url": str(record.get("drive_file_url") or ""),
                # Drive-only artifacts have no app-state bytes to download.
                "download_url": "",
            }
        )
    return artifacts


def _orphan_matter(orphan: dict[str, Any]) -> dict[str, Any]:
    folder_name = str(orphan.get("folder_name") or "").strip() or "NDA"
    summary = orphan.get("summary") if isinstance(orphan.get("summary"), dict) else {}
    return {
        "matter_id": str(summary.get("matter_id") or ""),
        "counterparty": str(orphan.get("counterparty") or "") or artifact_registry.COUNTERPARTY_UNKNOWN,
        "title": folder_name,
        "created_at": str(summary.get("created_at") or ""),
        # Summary-less orphan: no workflow status; chip renders "—".
        "status": "",
        "source": SOURCE_DRIVE,
        "in_app": False,
        "open_matter_url": "",
        "open_in_drive_url": str(orphan.get("folder_url") or ""),
        "duplicate": False,
        "duplicate_folder_urls": [],
        "facets": _drive_facets(summary),
        "artifacts": _drive_only_artifacts(summary),
    }


def _summary_artifact_urls(summary: dict[str, Any]) -> dict[str, str]:
    urls: dict[str, str] = {}
    raw = summary.get("artifacts")
    if not isinstance(raw, list):
        return urls
    for record in raw:
        if not isinstance(record, dict):
            continue
        artifact_id = str(record.get("artifact_id") or "")
        url = str(record.get("drive_file_url") or "")
        if artifact_id and url:
            urls[artifact_id] = url
    return urls


def _normalized_entity_key(counterparty: str) -> str:
    """Normalize a counterparty name for repeat-entity detection.

    casefold + whitespace-collapse so "Acme Corp" / "acme  corp" read as one
    entity even when they land in separate display groups. The unknown sentinel
    ("Unknown Counterparty") returns "" so it never counts as a repeat entity (a
    bag of unrelated unknowns is not the same counterparty).
    """
    token = " ".join(str(counterparty or "").split()).casefold()
    if not token or token == artifact_registry.COUNTERPARTY_UNKNOWN.casefold():
        return ""
    return token


def _stamp_repeat_entity(matters: list[dict[str, Any]]) -> int:
    """Stamp ``facets.repeat_entity`` per matter; return the repeat-entity count.

    A matter is a repeat entity when its normalized counterparty has >=2 DISTINCT
    matters in the corpus (by matter_id; matters sharing a matter_id -- the same
    record surfaced from both app-state and Drive -- count once). The unknown
    sentinel never qualifies (its normalized key is "").
    """
    distinct_by_key: dict[str, set[str]] = {}
    for index, matter in enumerate(matters):
        key = _normalized_entity_key(str(matter.get("counterparty") or ""))
        if not key:
            continue
        # Fall back to the matter's identity index when it has no matter_id, so two
        # id-less matters of the same entity still count as two distinct matters.
        identity = str(matter.get("matter_id") or "") or f"__idx_{index}"
        distinct_by_key.setdefault(key, set()).add(identity)

    repeat_keys = {key for key, ids in distinct_by_key.items() if len(ids) >= 2}
    count = 0
    for matter in matters:
        facets = matter.get("facets")
        if not isinstance(facets, dict):
            continue
        is_repeat = _normalized_entity_key(str(matter.get("counterparty") or "")) in repeat_keys
        facets["repeat_entity"] = is_repeat
        if is_repeat:
            count += 1
    return count


def _group_and_wrap(matters: list[dict[str, Any]], drive_block: dict[str, Any]) -> dict[str, Any]:
    groups_by_cp: dict[str, list[dict[str, Any]]] = {}
    for matter in matters:
        counterparty = str(matter.get("counterparty") or "") or artifact_registry.COUNTERPARTY_UNKNOWN
        matter["counterparty"] = counterparty
        matter["artifact_count"] = len(matter["artifacts"])
        groups_by_cp.setdefault(counterparty, []).append(matter)

    # Stamp the repeat-entity facet now the whole corpus is known (a >=2-distinct-
    # matters-per-counterparty signal the per-matter facet builders cannot see).
    repeat_entity_count = _stamp_repeat_entity(matters)

    groups: list[dict[str, Any]] = []
    for counterparty in sorted(groups_by_cp, key=lambda name: name.casefold()):
        cp_matters = sorted(
            groups_by_cp[counterparty],
            key=lambda matter: str(matter.get("created_at") or ""),
            reverse=True,
        )
        groups.append(
            {
                "counterparty": counterparty,
                "matter_count": len(cp_matters),
                "matters": cp_matters,
            }
        )

    matter_count = sum(group["matter_count"] for group in groups)
    return {
        "groups": groups,
        "matter_count": matter_count,
        "counterparty_count": len(groups),
        # Top-level facet counts the FE rich-facet rail reads. The master-filter
        # facet counts (mutuality/term_band/restraint_types/review_outcome/
        # clauses_present/origin) PLUS repeat_entity (the number of matters whose
        # counterparty has >=2 distinct matters in corpus, stamped above once the
        # whole corpus is known). Union of both backend folds; count == filtered
        # parity holds per facet.
        "facet_counts": {**_facet_counts(matters), "repeat_entity": repeat_entity_count},
        "drive": drive_block,
    }


# Facet keys whose per-matter value is a LIST of values (each value counts the matter
# once); every other counted facet is a single scalar value.
_LIST_FACET_KEYS: frozenset[str] = frozenset({"restraint_types", "clauses_present"})

# The master-filter facet keys the top-level count block covers. Counts are derived
# from the SAME per-matter facet values the filter reads, so each count is exactly the
# number of matters a filter on that value keeps (count == filtered parity). A null /
# "" / empty-list value is NOT counted (an unknown facet never positively matches), so
# the option list the FE builds from these counts only ever offers real, filterable
# values.
_COUNTED_FACET_KEYS: tuple[str, ...] = (
    "mutuality",
    "term_band",
    "restraint_types",
    "review_outcome",
    "clauses_present",
    "origin",
)


def _facet_counts(matters: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """``{facet_key: {value: matter_count}}`` over the merged matters.

    For each master-filter facet, count how many matters carry each value, mirroring
    exactly what a filter on that value would keep. List facets (restraint_types /
    clauses_present) count a matter once per value it lists; scalar facets count a
    matter under its single value. Null / empty values are skipped so the count block
    only advertises filterable values. This is the backend twin of the FE rail's
    option-count derivation (corpus.js builds the same counts from matter.facets), so
    the AI facet-count tool and the FE rail agree without re-deriving over a live Drive.
    """
    counts: dict[str, dict[str, int]] = {key: {} for key in _COUNTED_FACET_KEYS}
    for matter in matters:
        facets = matter.get("facets") if isinstance(matter, dict) else None
        if not isinstance(facets, dict):
            continue
        for key in _COUNTED_FACET_KEYS:
            value = facets.get(key)
            if key in _LIST_FACET_KEYS:
                if not isinstance(value, list):
                    continue
                for item in value:
                    token = str(item or "").strip()
                    if token:
                        counts[key][token] = counts[key].get(token, 0) + 1
            else:
                token = str(value or "").strip()
                if token:
                    counts[key][token] = counts[key].get(token, 0) + 1
    return counts


# --- url builders ----------------------------------------------------------
def _open_matter_url(matter_id: str) -> str:
    return f"/?tab=corpus&matter={matter_id}" if matter_id else ""


def _artifact_download_url(matter_id: str, artifact_id: str) -> str:
    if not matter_id or not artifact_id:
        return ""
    return f"/api/corpus/artifacts/{matter_id}/{artifact_id}"


def _drive_block(
    *,
    connected: bool,
    reconciled: bool,
    reason: str,
    built_at: str = "",
    from_cache: bool = False,
    stale: bool = False,
) -> dict[str, Any]:
    return {
        "connected": connected,
        "reconciled": reconciled,
        "reason": reason,
        "built_at": built_at,
        "from_cache": from_cache,
        "stale": stale,
    }
