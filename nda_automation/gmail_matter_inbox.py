from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Any

# Per-sync cost cap for the AI intake classifier. Imported here so the budget the
# inbox loop hands down stays in lockstep with the classifier's own cap constant.
from .gmail_intake_classifier import MAX_INTAKE_CALLS_PER_SYNC

LOGGER = logging.getLogger(__name__)

# Cooperative GIL yield between the poll's CPU-heavy steps (attachment download +
# PDF/DOCX extraction, per-message heavy import). The scheduler thread shares one
# GIL with every request thread on the single prod worker; a long extraction burst
# starves static-asset requests into the Render proxy's 502 timeout. A brief sleep
# after each heavy unit of work releases the GIL so pending request threads get
# scheduled, at a bounded cost to sync throughput (~50ms per message/attachment).
# Env-tunable; <= 0 disables the yield entirely.
GMAIL_SYNC_YIELD_MS_ENV = "NDA_GMAIL_SYNC_YIELD_MS"
DEFAULT_GMAIL_SYNC_YIELD_MS = 50.0
# Misconfiguration ceiling: never let a bad env value stall the poll for more than
# a second per yield point.
_MAX_GMAIL_SYNC_YIELD_MS = 1000.0


def _sync_cpu_yield() -> None:
    """Briefly sleep so request threads can run during a CPU-heavy sync loop."""
    raw = str(os.environ.get(GMAIL_SYNC_YIELD_MS_ENV, "") or "").strip()
    try:
        yield_ms = float(raw) if raw else DEFAULT_GMAIL_SYNC_YIELD_MS
    except ValueError:
        yield_ms = DEFAULT_GMAIL_SYNC_YIELD_MS
    if yield_ms <= 0:
        return
    time.sleep(min(yield_ms, _MAX_GMAIL_SYNC_YIELD_MS) / 1000.0)

# When this fraction (or more) of the AI intake calls in a sync fail (error or
# timeout), the classifier is likely degraded (bad model slug, rate-limit,
# OpenRouter down) rather than hitting the occasional bad response, so the sync
# emits a warn-log. Below this the silent per-call fallback is fine.
_AI_DEGRADED_FRACTION = 0.5

# Per-attachment skip reasons that represent a STABLE, DEFINITIVE outcome -- the
# attachment was conclusively evaluated and there is no importable NDA to recover
# from it. These are the only non-import outcomes that make a message safe to mark
# processed in the ledger. This is an ALLOWLIST on purpose (a fail-safe inversion of
# the transient-reason blocklist): any skip reason NOT in this set -- a download
# failure (attachment_unavailable / attachment_too_large), an extraction crash
# (review_failed / pdf_text_unreadable_needs_ocr), or any future/unknown reason -- is
# treated as TRANSIENT, so the message stays UNMARKED and retries next poll. The
# safe bias is "retry", never "wrongly suppress".
#
# Note: a failing AI selector/intake classifier does NOT produce one of these skips
# directly -- resolve_intake_lane falls back to the DETERMINISTic lane on any
# non-ok AI status, so a "non_nda_attachment" / "ai_not_selected_attachment" skip is
# always a stable deterministic-or-confident-AI decision, not a swallowed AI error.
_TERMINAL_STABLE_ATTACHMENT_SKIP_REASONS = frozenset(
    {
        "non_nda_attachment",
        "ai_not_selected_attachment",
        "duplicate_attachment",
    }
)

# QUARANTINE (poison-message retry cap). The transient-skip retry bias above is
# correct for genuine infra blips, but it has no ceiling: a message whose heavy
# import path fails DETERMINISTICALLY every poll (a poisoned attachment that always
# crashes extraction/review) re-runs the PAID gmail_triage selector + gmail_intake
# classifier AI calls on EVERY poll, forever. The cap bounds that spend: each time a
# message's heavy path ends in a transient (non-stable) outcome, a per-message
# attempt counter in the processed ledger is bumped; once it reaches the applicable
# limit the message is marked processed with reason "quarantined" (terminal), so
# future polls skip it before any fetch/AI work. Only the EXPENSIVE stage counts an
# attempt -- a message never reached because the per-poll budget ran out accrues
# nothing, so budget starvation can never quarantine unattempted mail.
#
# The limit is REASON-STRATIFIED. "Transient" is the complement of the terminal
# allowlist above, which lumps together two very different failure classes:
#
# * ENVIRONMENTAL (attachment_unavailable download failures, review_failed
#   create/extraction crashes, unknown/future reasons): a retry can genuinely
#   succeed, so these get the full NDA_GMAIL_TRANSIENT_RETRY_LIMIT (default 5).
# * DETERMINISTIC-PERMANENT (attachment_too_large, pdf_text_unreadable_needs_ocr):
#   the outcome is a pure function of the SAME bytes -- retrying can never change
#   it. These may be a REAL counterparty NDA (a scanned PDF needing OCR!), so they
#   are NOT dropped silently: they quarantine early (NDA_GMAIL_PERMANENT_SKIP_
#   RETRY_LIMIT, default 2 -- one confirm re-run, no infinite burn) and the
#   quarantine record carries the underlying reason + filename so a human can act
#   (OCR it, raise the size limit, or requeue via
#   gmail_processed_ledger.requeue_quarantined_message).
NDA_GMAIL_TRANSIENT_RETRY_LIMIT_ENV = "NDA_GMAIL_TRANSIENT_RETRY_LIMIT"
DEFAULT_TRANSIENT_RETRY_LIMIT = 5
NDA_GMAIL_PERMANENT_SKIP_RETRY_LIMIT_ENV = "NDA_GMAIL_PERMANENT_SKIP_RETRY_LIMIT"
DEFAULT_PERMANENT_SKIP_RETRY_LIMIT = 2

# Skip reasons that are DETERMINISTIC on the attachment bytes: a retry re-runs the
# same computation on the same input and must produce the same skip.
_DETERMINISTIC_PERMANENT_SKIP_REASONS = frozenset(
    {
        "attachment_too_large",
        "pdf_text_unreadable_needs_ocr",
    }
)


# AI PRE-GATE (money follows signal). The deterministic band classifier already
# computes a terminal-precision "skip" lane (score < triage band, no content
# basis); historically the paid Flash-intake classifier still ran on every such
# candidate and the Pro selector ran on every multi-attachment message, so junk
# mail paid full AI freight. The pre-gate suppresses those calls for candidates
# the deterministic lane already terminally skips -- with three MANDATORY
# fail-open exemptions (any one keeps the AI available):
#
#   1. ESCAPE HATCH: an explicit NDA mention in the message metadata (subject/
#      body/snippet via _metadata_has_explicit_nda_signal) or a strong NDA
#      filename -- covers scanned/odd-extraction NDAs announced in the email.
#   2. SCORER-BLIND, two forms -- a blind det-skip means "couldn't judge",
#      never "not an NDA", so such candidates ALWAYS keep the AI overlay:
#      (a) EXTRACTION-BLIND: extracted text empty or under ~200 chars
#          (image-only DOCX extracts EMPTY with no error; a partial-scan PDF
#          extracts only its cover letter);
#      (b) LANGUAGE-BLIND: substantial text but ZERO vocabulary hits
#          (validation detection_hits == 0). Every scoring regex is English, so
#          a text-extractable foreign-language NDA scores ~0 while matching
#          NOTHING -- whereas English junk engages the vocabulary somewhere (a
#          "confidential"/"agreement"/"document" filename signal, an invoice/
#          proposal/deck collateral hit) and still pre-gates.
#   3. SEAM PRESENCE: the pre-gate only activates when the transport exposes the
#      attachment_explicit_nda_signal escape-hatch seam. A transport that cannot
#      answer the escape-hatch question must not pre-gate (fail-open toward
#      paying for AI rather than dropping a real NDA); older fakes therefore
#      behave byte-identically to before.
#
# Candidates ABOVE the skip band are untouched: the selector still ranks them and
# the intake classifier still adjudicates confident/triage exactly as before.
# NDA_GMAIL_AI_PREGATE: default ON; 0/false/no/off restores the always-call path.
NDA_GMAIL_AI_PREGATE_ENV = "NDA_GMAIL_AI_PREGATE"
# Below this much extracted text the deterministic scorer is considered BLIND on
# the attachment (exemption 2 above). Deliberately small: a genuine one-page NDA
# body is far past it, while an image-only/failed extraction sits near zero.
MIN_PREGATE_EXTRACTED_TEXT_CHARS = 200


def _ai_pregate_enabled() -> bool:
    """Whether the AI pre-gate env switch is on (default ON; explicit off wins)."""
    raw = str(os.environ.get(NDA_GMAIL_AI_PREGATE_ENV, "") or "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


# The forced triage reason / provenance marker for e-sign platform notifications
# captured as likely executed NDAs (see the capture block in _scan_pass). Defined
# here (the module that stamps it) and aliased by gmail_integration.
ESIGN_NDA_CAPTURE_TRIAGE_REASON = "esign_notification_nda"


def _resolve_inbound_backfill(transport: Any, owner_user_id: str) -> dict[str, int] | None:
    """The first-sync backfill window for this poll, via the transport seam.

    Returns a well-formed ``{"effective_window_days", "completed_through_days",
    "target_days"}`` dict or ``None`` when the cap does not apply (seam absent on
    older transports, cap disabled/complete, existing user, or a malformed state).
    Defensive: any probe failure returns None so the poll degrades to the full
    window rather than ever raising.
    """
    prober = getattr(transport, "inbound_backfill_state", None)
    if not callable(prober):
        return None
    try:
        state = prober(owner_user_id)
    except Exception:  # pragma: no cover - the probe must never break the poll
        return None
    if not isinstance(state, dict):
        return None
    try:
        effective = int(state.get("effective_window_days") or 0)
        completed = int(state.get("completed_through_days") or 0)
        target = int(state.get("target_days") or 0)
    except (TypeError, ValueError):
        return None
    if effective <= 0 or target <= 0 or effective > target:
        return None
    return {
        "effective_window_days": effective,
        "completed_through_days": max(0, completed),
        "target_days": target,
    }


def _candidate_extraction_blind(candidate: dict[str, Any]) -> bool:
    """True when the candidate's extracted text is too thin to trust a det-skip."""
    paragraphs = candidate.get("paragraphs")
    if not isinstance(paragraphs, list):
        return True
    total = 0
    for paragraph in paragraphs:
        text = paragraph.get("text") if isinstance(paragraph, dict) else None
        total += len(" ".join(str(text or "").split()))
        if total >= MIN_PREGATE_EXTRACTED_TEXT_CHARS:
            return False
    return True


def _candidate_language_blind(candidate: dict[str, Any]) -> bool:
    """True when the scorer matched ZERO vocabulary signals on this candidate.

    The blind exemption's second form (module note, 2b): substantial text where
    the English vocabulary found NOTHING to judge -- either genuinely alien
    content or a non-English document -- so a det-skip is untrustworthy and the
    multilingual AI overlay must stay available. Junk that engaged the
    vocabulary anywhere (an NDA term OR a collateral invoice/proposal/deck hit)
    is a TRUSTED skip and still pre-gates.
    """
    validation = candidate.get("validation") if isinstance(candidate.get("validation"), dict) else {}
    if "detection_hits" in validation:
        return _coerce_int(validation.get("detection_hits")) <= 0
    # Legacy validation shape (no hit count, e.g. an older transport overriding
    # attachment_nda_validation): derive conservatively. Any sign the scorer
    # engaged -- a matched term, a positive score, a collateral reason -- means
    # NOT blind; a fully silent validation is treated as blind so the safe bias
    # stays "pay for the AI look" rather than "drop".
    if validation.get("terms"):
        return False
    if _coerce_int(validation.get("score")) > 0:
        return False
    if "collateral" in str(validation.get("reason") or ""):
        return False
    return True


def _candidate_explicit_nda_signal(
    transport: Any,
    metadata: dict[str, str],
    candidate: dict[str, Any],
) -> bool:
    """The escape-hatch probe via the transport seam (False when it errors)."""
    probe = getattr(transport, "attachment_explicit_nda_signal", None)
    if not callable(probe):
        return False
    try:
        return bool(probe(metadata, candidate))
    except Exception:  # pragma: no cover - the probe must never break the poll
        return False


def _env_retry_limit(env_name: str, default: int) -> int:
    """A fail-open env-tunable retry limit (shared parser for both strata).

    A missing/blank/non-numeric/non-positive override falls back to the default so
    a misconfigured value can never quarantine everything on the first failure (or
    wedge at zero).
    """
    raw = os.environ.get(env_name, "")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if value < 1:
        return default
    return value


def _transient_retry_limit() -> int:
    """The environmental-failure attempt cap before quarantine (default 5)."""
    return _env_retry_limit(NDA_GMAIL_TRANSIENT_RETRY_LIMIT_ENV, DEFAULT_TRANSIENT_RETRY_LIMIT)


def _permanent_skip_retry_limit() -> int:
    """The deterministic-permanent-failure attempt cap before quarantine (default 2)."""
    return _env_retry_limit(
        NDA_GMAIL_PERMANENT_SKIP_RETRY_LIMIT_ENV, DEFAULT_PERMANENT_SKIP_RETRY_LIMIT
    )


def import_inbound_matters(
    *,
    transport: Any,
    limit: int = 10,
    query: str | None = None,
    owner_user_id: str = "",
) -> dict[str, Any]:
    if not transport.gmail_role_enabled("inbound"):
        raise transport.GmailIntegrationError("Gmail inbound is disabled in Admin.")
    owner_user_id = transport.clean_user_token_segment(owner_user_id)
    service = transport.gmail_service_for_owner("inbound", owner_user_id)
    profile = transport.gmail_profile_for_role("inbound", service=service, owner_user_id=owner_user_id)
    explicit_query = isinstance(query, str) and bool(query.strip())
    inbound_query = query.strip() if explicit_query else transport.default_inbound_query()

    # FIRST-SYNC BACKFILL CAP: a newly connected account's early polls scan a
    # capped window (min(configured, NDA_GMAIL_FIRST_SYNC_CAP_DAYS)) that widens
    # per successful poll until it reaches the configured window. Only applies to
    # the DEFAULT query (an explicit caller query is respected verbatim) and only
    # on transports exposing the backfill seams; a transport without drain-cursor
    # support also skips the cap (the widen safety below depends on the cursor).
    backfill = None if explicit_query else _resolve_inbound_backfill(transport, owner_user_id)
    if backfill is not None:
        query_for_window = getattr(transport, "inbound_query_for_window", None)
        if callable(query_for_window):
            try:
                inbound_query = str(query_for_window(backfill["effective_window_days"]))
            except Exception:  # pragma: no cover - fall back to the full window
                backfill = None
        else:
            backfill = None
    try:
        requested_limit = int(limit or 10)
    except (TypeError, ValueError):
        requested_limit = 10
    import_limit = max(1, min(requested_limit, transport.max_import_limit()))

    account_email = str(profile.get("emailAddress") or "")

    # The AI intake classifier reads its criteria block once per sync and shares a
    # single per-sync call budget across every message/attachment so the cost cap
    # bounds the whole sync, not each message. Both are computed defensively: if the
    # classifier transport is unavailable the playbook stays empty and the budget is
    # never drawn down, leaving the deterministic path byte-identical to today.
    intake_playbook = _intake_playbook(transport)
    intake_budget = _IntakeCallBudget(MAX_INTAKE_CALLS_PER_SYNC)

    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    # Roll the per-message AI intake tallies up into a single per-sync total so the
    # sync result carries an honest health signal for the classifier.
    sync_tallies = _IntakeTallies()

    # Gentle catch-up via a paged scan, now with a PERSISTENT DRAIN CURSOR so an
    # arbitrarily large backlog drains to completion WITHOUT unbounded per-poll
    # scanning. import_limit caps the number of NEW (dedup-miss) messages handed to
    # the heavy import path per poll (Pro attachment selector + Flash intake +
    # PyMuPDF extraction + download -- the work that strains the 2 GB worker).
    #
    # THE STALL THIS FIXES: the inbound query has no already-imported exclusion (we
    # only hold the gmail.readonly scope, so we cannot label/archive imported mail),
    # so it re-surfaces the SAME newest messages newest-first on every poll. The old
    # scan paged PAST the already-imported prefix with a fixed max_scan cap; once the
    # imported prefix grew past that cap (backlog > max_scan), every poll exhausted
    # its whole scan budget inside already-imported messages, found ZERO new work,
    # and the loop exited with no forward progress -- PERMANENTLY, silently dropping
    # the (backlog - max_scan) tail until it aged out at 90 days.
    #
    # THE FIX (Option B, readonly-safe -- no new scope, no re-consent): record a
    # per-owner low-water-mark on Gmail's server-assigned internalDate (the deepest,
    # i.e. oldest, message the scan has reached). Each poll runs two bounded passes
    # sharing the one import_limit NEW-work budget:
    #   1. HEAD pass  -- the un-bounded base query (newest-first), bounded by a small
    #      head_window, to ingest newly-arrived mail above the frontier.
    #   2. DRAIN pass -- the SAME base query date-bounded `before:<cursor>` so the
    #      already-drained newest prefix never re-surfaces; the scan reaches the next
    #      un-imported (older) batch DIRECTLY. The cursor is then lowered to the
    #      oldest message examined this poll, so the next poll resumes right below it.
    # Forward progress is guaranteed (the cursor strictly descends until the backlog
    # is drained, then is reset and the head pass alone keeps up), and the per-poll
    # probe is bounded to ~import_limit + head_window get()s -- never the whole
    # backlog. When the transport lacks cursor support (older fakes) or a message
    # carries no internalDate, the scan degrades to the single bounded legacy pass.
    cursor_supported = callable(getattr(transport, "inbound_drain_cursor", None))
    drain_cursor = 0
    if cursor_supported:
        try:
            drain_cursor = int(transport.inbound_drain_cursor(owner_user_id))
        except Exception:  # pragma: no cover - cursor read is best-effort
            drain_cursor = 0

    if backfill is not None and not cursor_supported:
        # Without a drain cursor the widened band below a processed prefix could
        # never be reliably reached, so a capped window would silently strand old
        # mail. Degrade to the full-window behaviour instead.
        backfill = None
        inbound_query = query.strip() if explicit_query else transport.default_inbound_query()

    # BACKFILL CURSOR RE-ARM. The two-pass scan assumes new work arrives at the
    # FRONT (head pass) or below an armed cursor (drain pass). A backfill WIDEN
    # step injects new work at the BACK -- below the already-processed prefix of
    # the previous (narrower) window -- where an UNARMED cursor (reset when the
    # previous band drained to exhaustion) would leave only the single bounded
    # no-cursor pass to page through the whole processed prefix every poll. Arm
    # the cursor at the OLD window boundary (now - already-drained days) so the
    # drain pass jumps straight past the drained prefix into the widened band.
    # Only when UNARMED: an armed cursor is still mid-descent through un-drained
    # mail, and moving it down past that mail would silently lose it.
    if backfill is not None and cursor_supported and drain_cursor <= 0:
        completed_days = int(backfill.get("completed_through_days") or 0)
        if completed_days > 0:
            boundary_ms = int(time.time() * 1000) - completed_days * 86_400_000
            if boundary_ms > 0:
                try:
                    drain_cursor = int(
                        transport.advance_inbound_drain_cursor(owner_user_id, boundary_ms)
                    )
                except Exception:  # pragma: no cover - arm is best-effort
                    drain_cursor = 0

    # One line per poll of the exact query in force (static config only -- window,
    # sender-exclude entries; never subjects/senders/body text; log hygiene).
    LOGGER.info("Gmail inbound poll query: %s", inbound_query)

    # Open the durable per-owner processed-message ledger ONCE for the whole poll
    # (load-once / mark-many / write-once, REFINEMENT A). The scan checks it BEFORE
    # the messages().get + the gmail_intake classifier + the gmail_triage selector AI
    # calls (REFINEMENT C) so an already-processed message costs no fetch and no AI
    # call. The session is obtained through the transport seam (exactly like the
    # drain cursor) so a transport that does not expose it -- older fakes -- degrades
    # to the pre-ledger behaviour, and tests get an isolated in-memory ledger rather
    # than the shared on-disk one. Best-effort: an open failure never fails the poll.
    processed_ledger = _open_processed_ledger(transport, owner_user_id)

    state = _ScanState(import_limit=import_limit)
    # The hard SCAN cap bounds a single pass so a screenful of already-imported (or
    # id-less) stubs can never probe unboundedly; it comfortably exceeds import_limit
    # so a full new batch is reachable past the imported ones.
    max_scan = max(import_limit * 5, import_limit + 100)
    # The head pass only needs to absorb mail that ARRIVED since the last poll, which
    # always lands at the very FRONT (newer than everything imported), so a small
    # window suffices -- it must NOT be large enough to do the backlog draining
    # (that is the drain pass's job, paging below the cursor). Sizing it at
    # import_limit lets a poll-interval's worth of fresh arrivals through while
    # keeping the head probe cheap.
    head_window = import_limit

    context = _ScanContext(
        transport=transport,
        service=service,
        account_email=account_email,
        owner_user_id=owner_user_id,
        intake_playbook=intake_playbook,
        intake_budget=intake_budget,
        imported=imported,
        skipped=skipped,
        sync_tallies=sync_tallies,
        processed_ledger=processed_ledger,
    )

    # The scan runs inside try/finally so the cursor persist + ledger flush below
    # happen EVEN IF a pass raises (e.g. a Gmail listing/get failure escalated by
    # raise_gmail_api_error, or an unexpected store/OS error). Pre-fix, an exception
    # here skipped BOTH, so every terminal outcome already reached this poll was
    # forgotten and re-ran its paid AI calls on the next cycle.
    try:
        if cursor_supported and drain_cursor > 0:
            # Pass 1: head re-scan for newly-arrived mail above the frontier (bounded).
            _scan_pass(inbound_query, max_scan=head_window, state=state, context=context)
            # Pass 2: drain the backlog below the frontier, date-bounded so the drained
            # prefix never re-surfaces.
            if not state.rate_limited and state.new_processed < import_limit:
                drain_query = transport.inbound_query_before(inbound_query, drain_cursor)
                _scan_pass(drain_query, max_scan=max_scan, state=state, context=context, track_floor=True)
        else:
            # No cursor support (or no cursor yet): a single bounded pass over the base
            # query, identical in spirit to the pre-cursor behaviour.
            _scan_pass(inbound_query, max_scan=max_scan, state=state, context=context, track_floor=True)
    finally:
        # Advance (lower) the persistent cursor to the oldest message examined in the
        # drain this poll, so the NEXT poll resumes right below it instead of re-paging
        # the imported prefix. When the drain pass found NO un-imported message older
        # than the cursor and was not cut short by the budget/rate-limit, the backlog
        # below the frontier is exhausted: reset the cursor so future polls run head-only
        # (and a fresh backlog re-arms it). All best-effort -- never raise into the poll.
        # SAFE ON THE EXCEPTION PATH: state.drain_exhausted is only ever set at a
        # CLEAN pass-end (never when a pass raised), so an aborted poll can lower
        # the cursor to its terminal floor but can never spuriously RESET it.
        if cursor_supported:
            try:
                _persist_drain_cursor(transport, owner_user_id, state, drain_cursor)
            except Exception:  # pragma: no cover - cursor write is best-effort
                LOGGER.warning("Failed to persist Gmail inbound drain cursor", exc_info=True)

        # Persist the processed-message ledger exactly ONCE for the whole poll (write-
        # once, REFINEMENT A): a no-op when nothing new reached a terminal outcome.
        # Best-effort -- a flush failure is already logged-and-swallowed inside
        # flush(); the extra guard covers exotic session fakes so persistence can
        # never break (or mask) the poll's own outcome.
        if processed_ledger is not None:
            try:
                processed_ledger.flush()
            except Exception:  # pragma: no cover - flush is best-effort
                LOGGER.warning("Failed to flush Gmail processed-message ledger", exc_info=True)

    # BACKFILL PROGRESS: record the widened band as drained-through ONLY when this
    # poll ended with the band secured -- either the drain ran to exhaustion, or
    # the persistent cursor is armed (it will keep descending through the band on
    # subsequent polls regardless of window width). A poll that secured neither
    # (e.g. rate-limited before any floor was learned) records nothing, so the
    # next poll retries the SAME window and no band is ever skipped past.
    backfill_result: dict[str, Any] | None = None
    if backfill is not None:
        cursor_after = 0
        try:
            cursor_after = int(transport.inbound_drain_cursor(owner_user_id))
        except Exception:  # pragma: no cover - cursor read is best-effort
            cursor_after = 0
        effective_days = int(backfill.get("effective_window_days") or 0)
        target_days = int(backfill.get("target_days") or 0)
        secured = state.drain_exhausted or cursor_after > 0
        if secured:
            try:
                transport.record_inbound_backfill_progress(owner_user_id, effective_days)
            except Exception:  # pragma: no cover - bookkeeping must never fail the poll
                LOGGER.warning("Failed to record Gmail backfill progress", exc_info=True)
        recorded_days = effective_days if secured else int(backfill.get("completed_through_days") or 0)
        backfill_result = {
            "active": effective_days < target_days or not secured,
            "window_days": effective_days,
            "completed_through_days": recorded_days,
            "target_days": target_days,
            "label": f"backfilling: {recorded_days} of {target_days} days",
        }

    result: dict[str, Any] = {
        "account": account_email,
        "imported": imported,
        "query": inbound_query,
        "skipped": skipped,
        "ai_intake": sync_tallies.as_dict(),
        "rate_limited": state.rate_limited,
        # Messages terminally quarantined THIS poll (transient-retry cap reached).
        # Surfaced so the sync summary/status can make a quarantine visible instead
        # of it silently eating mail.
        "quarantined": sum(1 for skip in skipped if str(skip.get("reason") or "") == "quarantined"),
        # HEAVY slots actually consumed this poll (imports + transient-failure
        # attempts). The fan-out charges the cross-user ceiling with this, so a
        # poisoned backlog burning worker cycles draws down the shared budget just
        # like successful imports do -- the ceiling bounds LOAD, not just successes.
        "new_processed": state.new_processed,
    }
    if backfill_result is not None:
        # First-sync backfill progress, surfaced so the sync summary + status
        # payloads can explain why an old thread has not imported yet. The key is
        # present only while the cap applies, keeping the legacy result shape
        # byte-identical for every other poll.
        result["backfill"] = backfill_result
    return result


class _ScanState:
    """Mutable per-poll scan accounting shared across the head + drain passes."""

    def __init__(self, *, import_limit: int) -> None:
        self.import_limit = import_limit
        self.new_processed = 0
        # The oldest internalDate (ms) examined in a floor-tracking (drain) pass this
        # poll -- the resume point for the next poll's cursor. 0 means "none seen".
        self.drain_floor_ms = 0
        # True once a date-bounded drain pass reached the end of the backlog
        # (an empty/zero-progress page) WITHOUT being cut short by the budget or a
        # rate-limit -- i.e. nothing older than the cursor remains to import.
        self.drain_exhausted = False
        self.rate_limited = False
        # Message ids whose heavy path already ran to a TRANSIENT failure THIS poll.
        # Enforces "at most ONE attempt increment per message per poll": the head
        # pass and the drain pass can both surface the same (unmarked, to-retry)
        # message within one poll, and without this guard it would burn a second
        # heavy slot AND double-count -- letting e.g. a needs-OCR PDF hit its
        # 2-attempt quarantine within a single poll, seconds apart, which defeats
        # the point of a confirm re-run. (Successful/terminal messages need no
        # entry here: they are ledger-marked in-memory immediately, so a second
        # encounter this poll already short-circuits on is_processed.)
        self.transient_attempted_ids: set[str] = set()
        # Excluded-sender CONTENT probes run this poll (download + extraction, no
        # AI). Capped at import_limit per poll so a backlog of excluded mail can
        # never do more heavy extraction work than the budget allows for imports;
        # overflow messages are deferred UNMARKED (they retry next poll).
        self.capture_probes_used = 0

    def note_floor(self, internal_date_ms: int) -> None:
        if internal_date_ms <= 0:
            return
        if self.drain_floor_ms == 0 or internal_date_ms < self.drain_floor_ms:
            self.drain_floor_ms = internal_date_ms


class _ScanContext:
    """Immutable-ish bag of the per-poll collaborators a scan pass needs."""

    def __init__(
        self,
        *,
        transport: Any,
        service: Any,
        account_email: str,
        owner_user_id: str,
        intake_playbook: str,
        intake_budget: "_IntakeCallBudget",
        imported: list[dict[str, Any]],
        skipped: list[dict[str, str]],
        sync_tallies: "_IntakeTallies",
        processed_ledger: Any = None,
        transient_retry_limit: int | None = None,
        permanent_skip_retry_limit: int | None = None,
    ) -> None:
        self.transport = transport
        self.service = service
        self.account_email = account_email
        self.owner_user_id = owner_user_id
        self.intake_playbook = intake_playbook
        self.intake_budget = intake_budget
        self.imported = imported
        self.skipped = skipped
        self.sync_tallies = sync_tallies
        # The load-once / mark-many / write-once processed-message ledger for this
        # poll (REFINEMENT A). May be None when the ledger could not be opened, in
        # which case the scan degrades to the pre-ledger behaviour.
        self.processed_ledger = processed_ledger
        # Per-message transient-failure attempt caps before quarantine (env-tunable,
        # resolved once per poll): the full environmental limit and the tighter
        # deterministic-permanent limit (see the stratification note at the top).
        self.transient_retry_limit = (
            transient_retry_limit if transient_retry_limit is not None else _transient_retry_limit()
        )
        self.permanent_skip_retry_limit = min(
            self.transient_retry_limit,
            permanent_skip_retry_limit
            if permanent_skip_retry_limit is not None
            else _permanent_skip_retry_limit(),
        )


def _scan_pass(
    inbound_query: str,
    *,
    max_scan: int,
    state: _ScanState,
    context: _ScanContext,
    track_floor: bool = False,
) -> None:
    """One bounded paged scan over ``inbound_query``.

    Pages newest-first, handing up to the remaining import_limit NEW (dedup-miss)
    messages to the heavy import path and cheaply skipping already-imported ones.
    Stops on: the NEW-work budget, the hard ``max_scan`` cap, an empty next-page
    token, a zero-progress page, or a Gmail rate-limit (429) -- the last keeps what
    was imported this cycle rather than aborting the whole poll. ``track_floor``
    records the oldest internalDate examined (the drain pass's resume point).
    """
    transport = context.transport
    service = context.service
    new_stubs_total = 0
    stubs_scanned = 0
    # Total stubs PAGED this pass, including cheap ledger pre-skips. Ledger
    # pre-skips deliberately do NOT count toward max_scan (they cost one in-memory
    # set lookup, no fetch, no AI) -- otherwise a processed prefix longer than
    # max_scan would permanently wall off the un-processed band behind it (the
    # first-sync backfill widen injects exactly such a band BELOW the processed
    # prefix). This separate, much larger cap bounds the pass's raw paging so a
    # pathological inbox still terminates.
    total_stubs_paged = 0
    max_total_paged = max(max_scan * 10, 500)
    page_token = ""
    page_size = min(state.import_limit, 100) or 1
    saw_pages = False
    while (
        state.new_processed < state.import_limit
        and stubs_scanned < max_scan
        and total_stubs_paged < max_total_paged
    ):
        # Only the list() call is guarded as a "list" error: the per-message work
        # below keeps its own narrow error handling so a genuine processing bug is
        # never mislabeled as a Gmail listing failure. A rate-limit (429) on list()
        # stops THIS pass gracefully (keep what we imported), never re-raises.
        try:
            page = service.users().messages().list(
                userId="me",
                q=inbound_query,
                maxResults=page_size,
                **({"pageToken": page_token} if page_token else {}),
            ).execute()
        except Exception as exc:
            if _is_rate_limited(transport, exc):
                state.rate_limited = True
                return
            transport.raise_gmail_api_error(exc, "Gmail inbound sync could not list messages.")
            raise  # unreachable: raise_gmail_api_error always raises; satisfies type/flow
        saw_pages = True
        new_stubs = page.get("messages") or []
        page_token = str(page.get("nextPageToken") or "")
        new_stubs_total += len(new_stubs)
        for message_stub in new_stubs:
            if (
                state.new_processed >= state.import_limit
                or stubs_scanned >= max_scan
                or total_stubs_paged >= max_total_paged
            ):
                break
            total_stubs_paged += 1
            message_id = str(message_stub.get("id") or "")
            if not message_id:
                # An id-less stub counts toward the hard scan cap exactly as it
                # always did -- max_scan is the backstop that stops an endless
                # stream of junk stubs from probing unbounded.
                stubs_scanned += 1
                continue

            # PROCESSED-LEDGER SKIP (REFINEMENT C): this is the whole point of the
            # ledger -- short-circuit a message that already reached a terminal
            # outcome on a prior poll BEFORE the messages().get below AND before the
            # gmail_intake classifier + the gmail_triage attachment-selector AI calls
            # those downstream paths make. A "processed" skip is a cheap pre-fetch
            # gate (like the dedup short-circuit): it does NOT count toward
            # import_limit, does NOT touch the drain cursor, AND does NOT count
            # toward max_scan (only toward the raw total-paged cap) -- so the scan
            # pages past an arbitrarily long processed prefix to the next
            # un-processed (older) batch exactly as it pages past an already-
            # imported one -- coexisting with the cursor drain (REFINEMENT F),
            # never stalling its forward progress nor hiding genuinely-new mail (an
            # unseen id is simply absent from the ledger and falls through).
            ledger = context.processed_ledger
            if ledger is not None and ledger.is_processed(message_id):
                context.skipped.append({"message_id": message_id, "reason": "processed_message"})
                continue
            stubs_scanned += 1

            # SAME-POLL RE-ATTEMPT GUARD: a message that already ran the heavy path
            # to a TRANSIENT failure THIS poll (head pass) can re-surface in the
            # drain pass of the same poll (it is deliberately unmarked so it retries
            # NEXT poll). Skip it silently: no second heavy slot, no second AI call,
            # and -- critically -- no second attempt increment, so "retry N times"
            # always means N distinct polls, never N passes seconds apart.
            if message_id in state.transient_attempted_ids:
                continue

            try:
                message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
            except Exception as exc:
                if _is_rate_limited(transport, exc):
                    # A 429 mid-scan: keep everything imported so far this poll and
                    # stop -- the next poll resumes from the persisted cursor.
                    state.rate_limited = True
                    return
                if transport.gmail_retry_after_epoch(exc):
                    transport.raise_gmail_api_error(exc, "Gmail inbound sync could not load a message.")
                context.skipped.append({"message_id": message_id, "reason": "message_unavailable"})
                continue

            # CURSOR-FLOOR (drain) -- DO NOT advance the floor here yet. The floor is
            # the resume point the persistent per-owner drain cursor is lowered to; a
            # message must only lower it once we KNOW it reached a terminal/stable
            # outcome (imported or a stable skip). Recording the floor unconditionally
            # right after fetch -- before the import outcome is known -- lets the cursor
            # descend BELOW a message that hit a TRANSIENT failure (stable_outcome is
            # False: review_failed / attachment_unavailable / extraction crash). Such a
            # message is intentionally left unmarked to retry next poll, but a cursor
            # already lowered past it means the next drain (before:<cursor>) and the
            # head pass never re-examine it -> the inbound NDA is silently lost forever.
            # So capture the date now and commit it to the floor only at each
            # terminal/stable outcome below (note_terminal_floor()).
            # Advance the drain floor for a message only at a terminal/stable outcome.
            # ``note_terminal_floor()`` excludes transient-failure (stable_outcome
            # False) messages so the cursor never descends past one still owed a retry.
            message_floor_ms = _message_internal_date_ms(transport, message) if track_floor else 0

            def note_terminal_floor(_floor_ms: int = message_floor_ms) -> None:
                if track_floor:
                    state.note_floor(_floor_ms)

            if transport.is_self_or_outbound_message(message, context.account_email):
                context.skipped.append({"message_id": message_id, "reason": "self_sent_or_outbound"})
                # TERMINAL outcome (REFINEMENT D): a self-sent/outbound message is
                # structurally never reviewable -- mark it so the next poll skips it
                # before the fetch + AI calls. (Marked in-memory; written once at the
                # end of the poll.)
                _mark_processed(context, message_id)
                note_terminal_floor()
                continue

            # E-SIGNATURE / CALENDAR platform notification emails (DocuSign,
            # Adobe Sign, HelloSign, PandaDoc, Google Calendar invites) carry a
            # PDF/DOCX attachment and pass the structural fetch query, but they are
            # never inbound counterparty NDAs -- importing them spawns phantom
            # matters. Skip + ledger-mark them BEFORE the import-budget slot so a
            # skip costs no budget and future polls re-skip cheaply without a
            # re-fetch. This code-level check is the AUTHORITATIVE suppression
            # (the query-level -from: clauses are only a fetch-quota optimization):
            # it also catches FORWARDED notifications the query cannot see, and
            # every drop stays visible here in skipped[] + the ledger. The match is
            # SENDER-ONLY (domain suffix / full address): a real NDA that mentions
            # DocuSign still imports. Callable-guard via getattr so older/fake
            # transports lacking the seams degrade gracefully: the broader
            # excluded_notification_sender seam is preferred, the legacy
            # DocuSign-only predicate is the fallback, neither means no skip.
            matched_exclude_entry = ""
            excluded_sender_probe = getattr(transport, "excluded_notification_sender", None)
            is_docusign_notification = getattr(transport, "is_docusign_notification", None)
            if callable(excluded_sender_probe):
                try:
                    matched_exclude_entry = str(excluded_sender_probe(message) or "")
                except Exception:  # pragma: no cover - the probe must never break the poll
                    matched_exclude_entry = ""
            elif callable(is_docusign_notification) and is_docusign_notification(message):
                matched_exclude_entry = "docusign.net"
            # EXECUTED-NDA CAPTURE: a platform notification that carries an
            # EXPLICIT NDA signal (strong NDA filename, or an explicit NDA term in
            # subject/body/snippet) is a counterparty-initiated envelope's likely
            # only copy of an executed NDA -- let it THROUGH the intake pipeline
            # (clamped to at most the triage lane + provenance-stamped below)
            # instead of terminally dropping it. Platform mail with no explicit
            # signal keeps the terminal drop. Seam-guarded + env-flagged
            # (NDA_GMAIL_ESIGN_NDA_CAPTURE, default on): older transports and the
            # flag-off state keep the unconditional drop.
            esign_capture_entry = ""
            if matched_exclude_entry:
                capture_probe = getattr(transport, "esign_nda_capture_hit", None)
                content_probe = getattr(transport, "excluded_message_capture_probe", None)
                capture_attachments: list[dict[str, str]] = []
                if callable(capture_probe) or callable(content_probe):
                    capture_attachments = list(
                        transport.reviewable_attachments(message.get("payload") or {})
                    )
                # Stage 1 -- explicit-token capture (free: headers + filenames).
                if capture_attachments and callable(capture_probe):
                    try:
                        if capture_probe(message, capture_attachments):
                            esign_capture_entry = matched_exclude_entry
                            matched_exclude_entry = ""
                    except Exception:  # pragma: no cover - probe must never break the poll
                        esign_capture_entry = ""
                # Stage 2 -- deterministic CONTENT probe (download + extract, no
                # AI) before the terminal drop: a genuine NDA envelope without an
                # English NDA token (Adobe Sign "Signature requested on 'Acme -
                # Mutual Agreement'") must not be silently dropped -- the base
                # (pre-exclude) behaviour imported these. Capped per poll so a
                # backlog of excluded mail cannot out-extract the import budget;
                # over-cap messages are DEFERRED unmarked (no terminal drop, no
                # cursor-floor advance) and retry next poll.
                if matched_exclude_entry and capture_attachments and callable(content_probe):
                    if state.capture_probes_used >= state.import_limit:
                        context.skipped.append(
                            {"message_id": message_id, "reason": "excluded_probe_deferred"}
                        )
                        continue
                    state.capture_probes_used += 1
                    try:
                        if content_probe(service, message_id, capture_attachments):
                            esign_capture_entry = matched_exclude_entry
                            matched_exclude_entry = ""
                    except Exception as probe_error:
                        if _is_rate_limited(transport, probe_error):
                            # Provider throttling says nothing about this message:
                            # keep what was imported this poll and stop, exactly
                            # like the download-stage 429 path -- never let a 429
                            # storm terminal-drop a real NDA.
                            state.rate_limited = True
                            return
                        # A broken probe fails toward the AI-era default for
                        # excluded senders (the terminal drop), never a crash.
            if matched_exclude_entry:
                # Keep the live fix's reason label for the DocuSign family so
                # existing telemetry/greps stay continuous; other platforms get
                # the generic label with the STATIC exclude-list entry as detail
                # (config value only -- never the sender address; log hygiene).
                reason = (
                    "docusign_notification"
                    if "docusign" in matched_exclude_entry
                    else "excluded_sender_notification"
                )
                skip_record = {"message_id": message_id, "reason": reason}
                if reason == "excluded_sender_notification":
                    skip_record["detail"] = matched_exclude_entry
                context.skipped.append(skip_record)
                # TERMINAL outcome (mirrors self/outbound above): a platform
                # notification is structurally never reviewable -- mark it so the
                # next poll skips it before the fetch + AI calls.
                _mark_processed(context, message_id)
                note_terminal_floor()
                continue

            attachments = list(transport.reviewable_attachments(message.get("payload") or {}))
            if not attachments:
                context.skipped.append({"message_id": message_id, "reason": "no_reviewable_attachment"})
                # TERMINAL outcome (REFINEMENT D): the message carries no reviewable
                # attachment and never will -- mark it processed.
                _mark_processed(context, message_id)
                note_terminal_floor()
                continue

            # Dedup short-circuit AHEAD of any download/extract: a previously-
            # imported forward (re-surfaced by the inbox query) is skipped here so
            # the content-scan PDF/DOCX extraction never re-downloads + re-parses its
            # attachments. This does NOT count toward import_limit -- the scan pages
            # past these to reach the next un-imported batch. Only genuinely
            # already-imported messages (every attachment matched on a pre-download
            # identity key) are short-circuited; anything not provably imported falls
            # through to the authoritative per-attachment path.
            if message_attachments_all_already_imported(
                message_id,
                attachments,
                transport=transport,
                owner_user_id=context.owner_user_id,
            ):
                context.skipped.append({"message_id": message_id, "reason": "already_imported"})
                # TERMINAL outcome (REFINEMENT D): every attachment is already
                # imported -- mark so future polls skip it before the fetch (the
                # dedup gate stops the heavy re-work; the ledger now also stops the
                # re-fetch). Idempotent with the dedup gate; complementary, not a
                # replacement.
                _mark_processed(context, message_id)
                note_terminal_floor()
                continue

            # QUARANTINE PRE-CHECK: a message whose recorded transient-failure
            # attempts already reached the cap (e.g. the limit was lowered, or the
            # quarantine mark itself failed to persist on the capping poll) is
            # terminally quarantined HERE -- before the budget slot and before the
            # selector/intake AI calls -- instead of burning one more heavy cycle.
            # This backstop deliberately compares against the LARGER environmental
            # limit (the failure reasons are not known pre-run, so the permanent
            # stratum's tighter cap is only applied at failure time below, where
            # the reasons are in hand) and NEVER increments the counter itself.
            prior_attempts = _ledger_attempt_count(context, message_id)
            if prior_attempts >= context.transient_retry_limit:
                _quarantine_message(context, message_id, prior_attempts)
                note_terminal_floor()
                continue

            # This message will hit the heavy import path: count it against the
            # per-poll NEW-work budget that bounds load on the 2 GB worker.
            state.new_processed += 1

            # CRASH CONTAINMENT (the untyped-poison case): the typed error handlers
            # deep inside the heavy path (Pdf/Docx extraction at prepare, the
            # ActiveReviewEngine/extraction/alignment family around create_matter)
            # convert KNOWN failures into transient skips with precise reasons. Any
            # OTHER exception -- a crash in the detection code, a MatterStoreError,
            # an unwrapped PyMuPDF RuntimeError -- previously propagated out of the
            # scan with NO attempt recorded: that one message aborted its owner's
            # poll at the same point every cycle, forever, and the retry cap never
            # engaged. The broad catch below converts such a crash into a synthetic
            # transient "import_crashed" skip (environmental stratum, detail =
            # exception CLASS name only -- log hygiene) and falls through to the
            # SAME single-site attempt counter, so an untyped poison quarantines
            # exactly like a typed one and the scan continues to the next message.
            # A rate-limit surfacing anywhere in the heavy path (e.g. the download
            # stage) aborts the pass UNCOUNTED instead -- a provider 429 storm says
            # nothing about this message and must never walk a real NDA toward
            # quarantine. The server-level per-user catch remains the outer backstop.
            try:
                # Defer this CPU-bound per-message step (download + PDF/DOCX
                # extraction + AI selector/intake) to any in-flight foreground NDA
                # generation so the single prod worker's GIL/CPU is not starved out
                # from under a user-facing generate. Bounded + fail-open: blocks at
                # most a few seconds and never raises, so the poll can never stall
                # unboundedly behind a stuck generate.
                from . import generation_priority  # noqa: PLC0415 - light/local import.

                generation_priority.yield_to_active_generation()

                # Always make the per-message detection content-aware: if subject/
                # body/snippet/filename carry no NDA signal, fall back to scanning
                # attachment content. There is NO terminal drop here anymore -- the
                # deterministic per-attachment band classifier is authoritative, so an
                # attachment-only NDA with a neutral subject is never dropped before
                # its content is judged.
                detection = transport.message_nda_detection(message, attachments)
                if not detection["matched"]:
                    detection = transport.attachment_nda_detection(service, message_id, attachments)

                metadata = message_selector_metadata(
                    message,
                    transport.message_metadata(
                        message, context.account_email, detection=detection if detection["matched"] else None
                    ),
                    transport=transport,
                )
                if esign_capture_entry:
                    # Provenance for the executed-NDA capture path: the matched
                    # platform entry rides the intake metadata so the matter is
                    # identifiable/filterable later (static config value only).
                    metadata = {**metadata, "gmail_esign_notification": esign_capture_entry}
                attachment_result = import_inbound_attachments(
                    service,
                    message_id,
                    attachments,
                    metadata,
                    transport=transport,
                    owner_user_id=context.owner_user_id,
                    intake_playbook=context.intake_playbook,
                    intake_budget=context.intake_budget,
                    esign_capture=bool(esign_capture_entry),
                )
            except Exception as error:
                if _is_rate_limited(transport, error):
                    # Keep everything imported so far this poll and stop -- the next
                    # poll resumes from the persisted cursor. NOT an attempt: the
                    # provider was throttling, the message content was never judged.
                    state.rate_limited = True
                    return
                LOGGER.warning(
                    "Gmail inbound heavy import crashed for message %s (%s); "
                    "recorded as a transient attempt",
                    message_id,
                    error.__class__.__name__,
                    exc_info=True,
                )
                attachment_result = {
                    "imported": [],
                    "skipped": [
                        gmail_attachment_skip(
                            message_id,
                            "",
                            "import_crashed",
                            detail=error.__class__.__name__,
                        )
                    ],
                    "ai_intake": None,
                    "stable_outcome": False,
                }
            context.imported.extend(attachment_result["imported"])
            context.skipped.extend(attachment_result["skipped"])
            context.sync_tallies.merge(attachment_result.get("ai_intake"))
            # TERMINAL outcome (REFINEMENT D + P1-1): mark processed when the heavy
            # import path reached a STABLE, DEFINITIVE outcome for the whole message --
            # every attachment either imported OR hit a terminal non-NDA/duplicate
            # skip, with NO transient failure. This is the fix for the sticky
            # non-NDA message: a message whose only attachment the selector/intake
            # terminally skips as non-NDA imports nothing yet is fully evaluated, so it
            # MUST be marked or it re-runs the gmail_triage + gmail_intake AI calls
            # (and burns an import_limit slot) on EVERY poll forever. We still do NOT
            # mark when stable_outcome is False -- any transient per-attachment failure
            # (attachment_unavailable / too_large / extraction crash / review_failed)
            # leaves the message unmarked so it retries next poll (the safe bias).
            # ORDERING INVARIANT: ledger marking (and attempt counting) happens
            # STRICTLY AFTER import_inbound_attachments returns -- the mark must
            # reflect the message's actual outcome, never precede it. Nothing in
            # the heavy path above may mark/count this message id.
            if attachment_result.get("stable_outcome"):
                _mark_processed(context, message_id)
                # Terminal/stable: safe to lower the drain cursor past this message.
                note_terminal_floor()
            else:
                # A TRANSIENT failure -- leave the message unmarked so it retries
                # next poll (the safe bias), and leave the floor where it is so the
                # cursor never descends past this still-to-retry message (the
                # data-loss fix). BUT count the attempt -- exactly ONE increment per
                # message per poll, and ONLY here (this message just consumed the
                # EXPENSIVE stage: budget slot + selector/intake AI calls). Never
                # counted: ledger pre-skips, budget/max_scan cut-offs, rate-limit
                # early returns, or message_unavailable fetch failures (content
                # never seen). Once the count reaches the applicable stratum's
                # limit the message is quarantined -- a terminal ledger mark with a
                # keyed reason record -- so a poisoned message stops burning AI
                # spend forever. Attempt accounting degrades to a no-op (attempts
                # stay 0, never quarantining) on a ledger session without the
                # counter seam.
                state.transient_attempted_ids.add(message_id)
                transient_reasons, failing_filename = _transient_failure_summary(
                    attachment_result.get("skipped")
                )
                # Reason stratification: a failure set that is ENTIRELY
                # deterministic-permanent (too_large / needs_ocr -- same bytes, same
                # outcome, retrying cannot help) quarantines at the tighter limit;
                # any environmental reason keeps the full retry allowance.
                deterministic_only = bool(transient_reasons) and all(
                    reason in _DETERMINISTIC_PERMANENT_SKIP_REASONS
                    for reason in transient_reasons
                )
                applicable_limit = (
                    context.permanent_skip_retry_limit
                    if deterministic_only
                    else context.transient_retry_limit
                )
                attempts = _record_transient_attempt(context, message_id)
                if attempts >= applicable_limit:
                    _quarantine_message(
                        context,
                        message_id,
                        attempts,
                        reasons=transient_reasons,
                        filename=failing_filename,
                    )
                    # Quarantine IS terminal: the cursor may descend past it (R5 --
                    # without this the drain keeps re-paging the same prefix).
                    note_terminal_floor()

            # Yield the GIL between heavy per-message imports so request threads
            # (static assets, API calls) can be scheduled while a long sync churns.
            # Loop-body level, AFTER the outcome accounting: every message that ran
            # the heavy path -- stable, transient, or quarantined -- yields once.
            _sync_cpu_yield()
        # Stop on an empty next-page token OR a zero-progress page (a page that
        # advanced the token but returned no messages), mirroring the original
        # paged-fetch termination guards.
        if not page_token or not new_stubs:
            break

    # A floor-tracking (drain) pass that consumed every page (ran out of token)
    # WITHOUT hitting the NEW-work budget or a rate-limit has reached the end of the
    # backlog below the cursor: nothing older remains to import.
    if (
        track_floor
        and saw_pages
        and not page_token
        and not state.rate_limited
        and state.new_processed < state.import_limit
    ):
        state.drain_exhausted = True


def _persist_drain_cursor(
    transport: Any,
    owner_user_id: str,
    state: _ScanState,
    previous_cursor: int,
) -> None:
    """Move the persistent per-owner drain cursor after a poll.

    Lowers it to the oldest message examined in the drain pass (the resume point),
    or resets it once the backlog below the frontier is fully drained so future
    polls run head-only. No-op when this poll never ran a date-bounded drain (no
    prior cursor) and saw no floor.
    """
    if state.drain_exhausted:
        # The drain reached the end of the backlog this poll (no un-imported message
        # older than where it scanned). Clear any frontier so future polls run
        # head-only; a fresh backlog re-arms the cursor on the next floor-tracking
        # pass. Covers both the resumed-drain case AND the first poll that drained a
        # below-import_limit backlog in one pass (so no stale cursor is left armed).
        if previous_cursor > 0:
            transport.reset_inbound_drain_cursor(owner_user_id)
        return
    if state.drain_floor_ms > 0:
        transport.advance_inbound_drain_cursor(owner_user_id, state.drain_floor_ms)


def _open_processed_ledger(transport: Any, owner_user_id: str) -> Any:
    """Open the per-owner processed-message ledger session via the transport seam.

    Mirrors how the drain cursor is obtained: the transport exposes
    ``processed_ledger_session(owner)`` (production delegates to
    :class:`gmail_processed_ledger.ProcessedLedgerSession`; tests provide an
    isolated in-memory fake). A transport without the seam -- older fakes -- returns
    ``None`` and the scan runs exactly as it did before the ledger existed. Any
    failure to open is logged and swallowed so it can never break the poll.
    """
    opener = getattr(transport, "processed_ledger_session", None)
    if not callable(opener):
        return None
    try:
        return opener(owner_user_id)
    except Exception:  # pragma: no cover - ledger open is best-effort
        LOGGER.warning(
            "Failed to open Gmail processed-message ledger; continuing without it",
            exc_info=True,
        )
        return None


def _mark_processed(context: "_ScanContext", message_id: str) -> None:
    """Record ``message_id`` as processed for this poll (in-memory, write-once).

    A thin guard so every terminal-outcome call site stays a one-liner and a missing
    ledger (open failed) is a silent no-op. The actual durable write happens ONCE at
    the end of the poll via ``ProcessedLedgerSession.flush``.
    """
    ledger = context.processed_ledger
    if ledger is not None:
        ledger.mark(message_id)


def _ledger_attempt_count(context: "_ScanContext", message_id: str) -> int:
    """The recorded transient-failure attempts for ``message_id`` (0 without a seam).

    Callable-guarded like every other optional transport/ledger seam: an older/fake
    ledger session without attempt counting reads as 0 attempts, so quarantine simply
    never triggers and the scan behaves exactly as before.
    """
    ledger = context.processed_ledger
    counter = getattr(ledger, "attempt_count", None) if ledger is not None else None
    if not callable(counter):
        return 0
    try:
        return max(0, int(counter(message_id)))
    except Exception:  # pragma: no cover - attempt read is best-effort
        return 0


def _record_transient_attempt(context: "_ScanContext", message_id: str) -> int:
    """Count one transient-failure attempt for ``message_id``; returns the new total.

    Returns 0 (never quarantining) when the ledger session lacks the counter seam or
    the record fails -- the retry-forever bias is the safe degradation.
    """
    ledger = context.processed_ledger
    recorder = getattr(ledger, "record_attempt", None) if ledger is not None else None
    if not callable(recorder):
        return 0
    try:
        return max(0, int(recorder(message_id)))
    except Exception:  # pragma: no cover - attempt write is best-effort
        return 0


def _transient_failure_summary(skipped: object) -> tuple[list[str], str]:
    """The distinct TRANSIENT skip reasons + first failing filename for a message.

    Reads a message's per-attachment skip records (the output of
    ``import_inbound_attachments``) and keeps only the non-terminal reasons -- the
    ones that made ``stable_outcome`` False. Used to stratify the retry limit and
    to make the quarantine record actionable (which attachment, why).
    """
    reasons: list[str] = []
    filename = ""
    if not isinstance(skipped, list):
        return reasons, filename
    for skip in skipped:
        if not isinstance(skip, dict):
            continue
        reason = str(skip.get("reason") or "")
        if not reason or reason in _TERMINAL_STABLE_ATTACHMENT_SKIP_REASONS:
            continue
        if reason not in reasons:
            reasons.append(reason)
        if not filename:
            filename = str(skip.get("attachment_filename") or "")
    return reasons, filename


def _quarantine_message(
    context: "_ScanContext",
    message_id: str,
    attempts: int,
    *,
    reasons: list[str] | None = None,
    filename: str = "",
) -> None:
    """Terminally quarantine ``message_id`` after exhausting its transient retries.

    Marks it processed in the ledger (via the dedicated ``quarantine`` seam when
    available, recording a keyed ``{attempts, reason, last_at, filename}`` entry so
    the quarantine is durable, inspectable via ``quarantined_messages()`` without
    log archaeology, and reversible via
    ``gmail_processed_ledger.requeue_quarantined_message``) and surfaces a visible
    ``quarantined`` skip carrying the underlying reason + filename so a human can
    act on it (OCR the scanned PDF, raise the size limit, requeue) instead of the
    message silently disappearing.
    """
    reason_summary = ",".join(reasons or []) or "transient_import_failure"
    ledger = context.processed_ledger
    quarantiner = getattr(ledger, "quarantine", None) if ledger is not None else None
    if callable(quarantiner):
        try:
            quarantiner(message_id, reason=reason_summary, attempts=attempts, filename=filename)
        except TypeError:
            # An older/simpler quarantine seam without the metadata kwargs.
            quarantiner(message_id)
    else:
        _mark_processed(context, message_id)
    context.skipped.append(
        gmail_attachment_skip(
            message_id,
            filename,
            "quarantined",
            detail=(
                f"import failed with [{reason_summary}] across {attempts} attempts; "
                "message quarantined (no further AI retries; requeue via "
                "gmail_processed_ledger.requeue_quarantined_message)"
            ),
            attempts=str(attempts),
        )
    )
    # NOTE (log hygiene): message id + reason codes + counts only -- never
    # subjects/senders/body text in process logs.
    LOGGER.warning(
        "Gmail inbound message %s quarantined after %d transient import failures "
        "(reasons=%s); it will no longer be retried.",
        message_id,
        attempts,
        reason_summary,
    )


def _is_rate_limited(transport: Any, error: Exception) -> bool:
    probe = getattr(transport, "is_rate_limit_error", None)
    if callable(probe):
        try:
            return bool(probe(error))
        except Exception:  # pragma: no cover - probe is best-effort
            return False
    # Fall back to the retry-after probe every inbox transport exposes.
    try:
        return bool(transport.gmail_retry_after_epoch(error))
    except Exception:  # pragma: no cover - probe is best-effort
        return False


def _message_internal_date_ms(transport: Any, message: dict[str, Any]) -> int:
    getter = getattr(transport, "message_internal_date_ms", None)
    if callable(getter):
        try:
            return int(getter(message))
        except Exception:  # pragma: no cover - date read is best-effort
            return 0
    try:
        return max(0, int(str(message.get("internalDate") or "0")))
    except (TypeError, ValueError):
        return 0


def import_inbound_attachments(
    service: Any,
    message_id: str,
    attachments: list[dict[str, Any]],
    metadata: dict[str, str],
    *,
    transport: Any,
    owner_user_id: str = "",
    intake_playbook: str | None = None,
    intake_budget: "_IntakeCallBudget | None" = None,
    esign_capture: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    # The AI intake classifier (when configured) overlays the deterministic band
    # lane below. Compute the playbook/budget defensively so that, when called
    # standalone (e.g. tests, or the classifier transport being absent), the path
    # collapses to the deterministic-only behaviour.
    if intake_playbook is None:
        intake_playbook = _intake_playbook(transport)
    if intake_budget is None:
        intake_budget = _IntakeCallBudget(MAX_INTAKE_CALLS_PER_SYNC)
    intake_configured = _intake_classifier_configured(transport)

    # Per-sync AI intake telemetry. A degraded classifier (bad model slug,
    # rate-limit, OpenRouter down/timeout) silently falls back to the deterministic
    # lane per-call; without these counts a fully-broken classifier is
    # indistinguishable from a healthy one. Accumulated here and surfaced in the
    # result (and merged across messages by import_inbound_matters).
    tallies = _IntakeTallies()

    prepared: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    # Always prepare with the deterministic validation computed (no
    # require_deterministic_acceptance suppression). Classification is decided per
    # candidate by the band classifier below, never short-circuited here.
    for attachment in attachments:
        candidate, skip = prepare_inbound_attachment(
            service,
            message_id,
            attachment,
            metadata,
            transport=transport,
            owner_user_id=owner_user_id,
        )
        if skip is not None:
            skipped.append(skip)
        elif candidate is not None:
            prepared.append(candidate)
        # Yield the GIL after each attachment's download + extraction so request
        # threads (static assets, API calls) can be scheduled during the burst.
        _sync_cpu_yield()

    triage_min_score = _triage_min_score(transport)

    # AI PRE-GATE eligibility (see the module note at NDA_GMAIL_AI_PREGATE_ENV).
    # Computed from the SELECTOR-INDEPENDENT deterministic band so the same answer
    # gates both paid calls symmetrically: suppressing only one would kill one of
    # the two AI rescue paths (selector promote-to-confident / intake det-skip ->
    # triage promotion) while leaving the message half-judged. A candidate is
    # AI-eligible when the deterministic PRE-lane is above the terminal skip band,
    # OR the extraction is too thin to trust (blind), OR the explicit-NDA escape
    # hatch fires. The pre-gate is only ACTIVE when the env switch is on AND the
    # transport exposes the escape-hatch seam (fail-open for older transports).
    pregate_active = _ai_pregate_enabled() and callable(
        getattr(transport, "attachment_explicit_nda_signal", None)
    )
    ai_eligibility: list[bool] = []
    for candidate in prepared:
        if not pregate_active:
            ai_eligibility.append(True)
            continue
        validation = candidate.get("validation") if isinstance(candidate.get("validation"), dict) else {}
        pre_lane, _pre_reason = classify_attachment_lane(
            validation,
            selector_selected=False,
            selector_configured=False,
            triage_min_score=triage_min_score,
        )
        # An e-sign capture message is by definition explicit-signal (that is what
        # let it past the terminal drop), so it always keeps the AI overlay --
        # belt-and-braces on top of the escape hatch so no double-drop path exists.
        ai_eligibility.append(
            esign_capture
            or pre_lane != "skip"
            or _candidate_extraction_blind(candidate)
            or _candidate_language_blind(candidate)
            or _candidate_explicit_nda_signal(transport, metadata, candidate)
        )

    # Pro selector: only pay for the ranking call when at least one prepared
    # candidate reaches the gate (deterministic triage band, extraction-blind, or
    # escape hatch). When it DOES run it still sees every prepared candidate, so
    # messages with any signal behave byte-identically to before.
    if pregate_active and not any(ai_eligibility):
        selected_ids, selector_metadata = None, {}
    else:
        selected_ids, selector_metadata = selected_candidate_attachment_ids(
            metadata, prepared, transport=transport
        )

    imported: list[dict[str, Any]] = []
    for candidate, ai_allowed in zip(prepared, ai_eligibility):
        attachment_id = str(candidate.get("attachment_id") or "")
        validation = candidate.get("validation") if isinstance(candidate.get("validation"), dict) else {}
        selector_selected = selected_ids is not None and attachment_id in selected_ids
        # 1. The deterministic lane is the FALLBACK and the floor (pure + unit-tested).
        det_lane, det_reason = classify_attachment_lane(
            validation,
            selector_selected=selector_selected,
            selector_configured=selected_ids is not None,
            triage_min_score=triage_min_score,
        )
        # 2. + 3. AI overlay: run the classifier when configured and the per-sync cap
        # is not yet exhausted, then reconcile (fail toward triage on ambiguity). Any
        # unconfigured/error/timeout/overflow yields a non-ok status, and
        # resolve_intake_lane returns the deterministic lane verbatim.
        #
        # PRE-GATE: a candidate the deterministic lane terminally skips AND that is
        # not AI-eligible (no escape hatch, not extraction-blind) does not pay for
        # the Flash call -- the synthetic "skipped_pregate" status routes
        # resolve_intake_lane straight to the deterministic skip. A det_lane above
        # skip (including a selector-promoted one) ALWAYS keeps the call.
        if pregate_active and det_lane == "skip" and not ai_allowed:
            ai_result: dict[str, Any] = {"status": "skipped_pregate"}
        else:
            ai_result = _maybe_classify_intake(
                transport,
                metadata,
                candidate,
                intake_playbook,
                intake_budget,
                configured=intake_configured,
            )
        tallies.record(str(ai_result.get("status") or ""))
        lane, triage_reason = transport.resolve_intake_lane(det_lane, det_reason, ai_result)
        ai_ok = ai_result.get("status") == "ok"
        # EXECUTED-NDA CAPTURE clamp: a captured e-sign platform notification may
        # import at most into the TRIAGE lane -- it is usually an EXECUTED document
        # a human must look at, never a silent auto-clean import -- and carries the
        # uniform provenance reason so the operator can filter these matters. A
        # terminal skip (the pipeline judged the content non-NDA) still stands.
        if esign_capture and lane != "skip":
            lane, triage_reason = "triage", ESIGN_NDA_CAPTURE_TRIAGE_REASON
        if lane == "skip":
            # An AI NOT_NDA terminal skip carries the AI reason/model so the
            # skipped-list telemetry explains the drop; otherwise the deterministic
            # / selector skip reasons are emitted byte-identically to before.
            if ai_ok and ai_result.get("verdict") == "NOT_NDA":
                skipped.append(gmail_attachment_skip(
                    message_id,
                    str(candidate.get("filename") or ""),
                    "non_nda_attachment",
                    detail=str(ai_result.get("reason") or ""),
                    model=str(ai_result.get("model") or ""),
                ))
            elif selected_ids is not None and not selector_selected:
                skipped.append(gmail_attachment_skip(
                    message_id,
                    str(candidate.get("filename") or ""),
                    "ai_not_selected_attachment",
                    detail=selector_metadata.get("reason", ""),
                    model=selector_metadata.get("model", ""),
                    confidence=selector_metadata.get("confidence", ""),
                ))
            else:
                skipped.append(gmail_attachment_skip(
                    message_id,
                    str(candidate.get("filename") or ""),
                    "non_nda_attachment",
                    detail=str(validation.get("reason") or ""),
                    score=str(validation.get("score") or "0"),
                    # AUDITABILITY: distinguish "the AI was never consulted (the
                    # pre-gate suppressed the call)" from "the AI agreed" in the
                    # skipped[] telemetry. Reason stays within the terminal-stable
                    # set; only the detail gains the marker.
                    **(
                        {"pregate": "suppressed"}
                        if ai_result.get("status") == "skipped_pregate"
                        else {}
                    ),
                ))
            continue
        # When the AI is what put this candidate into triage, surface the model's
        # confidence on the matter (overriding the deterministic score) so the
        # dashboard triage card shows the model's confidence.
        triage_confidence_override = (
            _ai_triage_confidence(ai_result, triage_reason) if lane == "triage" else None
        )
        matter, skip = create_matter_from_prepared_attachment(
            candidate,
            metadata,
            transport=transport,
            selector_metadata=selector_metadata if selected_ids is not None else None,
            owner_user_id=owner_user_id,
            triage=lane == "triage",
            triage_reason=triage_reason,
            triage_confidence=triage_confidence_override,
        )
        if skip is not None:
            skipped.append(skip)
        elif matter is not None:
            imported.append(matter)
    tallies.warn_if_degraded(LOGGER, message_id, model=_intake_model(transport))
    # STABLE-OUTCOME signal for the processed-message ledger (P1-1, the sticky
    # non-NDA case): the message reached a definitive outcome iff EVERY attachment
    # either imported or hit a TERMINAL-stable non-NDA/duplicate skip -- i.e. no skip
    # carries a transient (download/extraction) reason. When True the caller may mark
    # the message processed even if NOTHING imported (a message whose only attachment
    # is a known non-NDA the selector/intake skips), so it stops re-running the
    # gmail_triage + gmail_intake AI calls every poll. When False (any transient
    # failure) the message stays unmarked and retries.
    stable_outcome = all(
        str(skip.get("reason") or "") in _TERMINAL_STABLE_ATTACHMENT_SKIP_REASONS
        for skip in skipped
    )
    return {
        "imported": imported,
        "skipped": skipped,
        "ai_intake": tallies.as_dict(),
        "stable_outcome": stable_outcome,
    }


class _IntakeTallies:
    """Per-sync counters for the AI intake classifier's health.

    ``ai_calls`` counts attempts that actually reached the model (``ok`` plus the
    degraded ``error`` / ``timeout`` outcomes); ``ai_errors`` / ``ai_timeouts`` are
    the degraded subsets, and ``ai_skipped_cap`` counts candidates that took the
    deterministic lane because the per-sync budget was exhausted. ``not_configured``
    is not counted (no call was attempted).
    """

    def __init__(self) -> None:
        self.ai_calls = 0
        self.ai_errors = 0
        self.ai_timeouts = 0
        self.ai_skipped_cap = 0
        # Candidates whose Flash call the deterministic PRE-GATE suppressed
        # (status "skipped_pregate"): surfaced so an operator can tell "the AI
        # was never consulted" apart from "the AI agreed" per sync.
        self.ai_skipped_pregate = 0

    def record(self, status: str) -> None:
        if status in ("ok", "error", "timeout"):
            self.ai_calls += 1
        if status == "error":
            self.ai_errors += 1
        elif status == "timeout":
            self.ai_timeouts += 1
        elif status == "skipped_cap":
            self.ai_skipped_cap += 1
        elif status == "skipped_pregate":
            self.ai_skipped_pregate += 1

    def as_dict(self) -> dict[str, int]:
        tallies = {
            "ai_calls": self.ai_calls,
            "ai_errors": self.ai_errors,
            "ai_timeouts": self.ai_timeouts,
            "ai_skipped_cap": self.ai_skipped_cap,
        }
        # Included only when non-zero so the legacy tally shape stays
        # byte-identical for every sync that never suppresses a call.
        if self.ai_skipped_pregate:
            tallies["ai_skipped_pregate"] = self.ai_skipped_pregate
        return tallies

    def merge(self, other: "_IntakeTallies | dict[str, Any] | None") -> None:
        if other is None:
            return
        data = other.as_dict() if isinstance(other, _IntakeTallies) else other
        self.ai_calls += int(data.get("ai_calls") or 0)
        self.ai_errors += int(data.get("ai_errors") or 0)
        self.ai_timeouts += int(data.get("ai_timeouts") or 0)
        self.ai_skipped_cap += int(data.get("ai_skipped_cap") or 0)
        self.ai_skipped_pregate += int(data.get("ai_skipped_pregate") or 0)

    def warn_if_degraded(self, logger: logging.Logger, scope: str, *, model: str = "") -> None:
        """Warn when failures are a high fraction of the calls actually attempted."""
        degraded = self.ai_errors + self.ai_timeouts
        if self.ai_calls > 0 and degraded >= self.ai_calls * _AI_DEGRADED_FRACTION:
            logger.warning(
                "Gmail intake classifier degraded over %s: %d/%d calls failed "
                "(errors=%d, timeouts=%d, model=%s); deterministic intake lane used "
                "for those candidates.",
                scope or "sync",
                degraded,
                self.ai_calls,
                self.ai_errors,
                self.ai_timeouts,
                model or "unknown",
            )


def _intake_model(transport: Any) -> str:
    getter = getattr(transport, "intake_classifier_model", None)
    if callable(getter):
        try:
            return str(getter() or "")
        except Exception:  # pragma: no cover - model probe is best-effort
            return ""
    return ""


class _IntakeCallBudget:
    """A mutable per-sync counter that caps how many AI intake calls are made.

    Shared across every message in a single sync so the cost cap bounds the whole
    sync rather than each message. Once exhausted, candidates take the deterministic
    lane (the classifier is reported as ``skipped_cap``).
    """

    def __init__(self, limit: int) -> None:
        self.limit = max(0, int(limit))
        self.used = 0

    def consume(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


def _intake_playbook(transport: Any) -> str:
    getter = getattr(transport, "gmail_intake_playbook", None)
    if callable(getter):
        try:
            return str(getter() or "")
        except Exception:  # pragma: no cover - playbook read is best-effort
            return ""
    return ""


def _intake_classifier_configured(transport: Any) -> bool:
    getter = getattr(transport, "intake_classifier_configured", None)
    if callable(getter):
        try:
            return bool(getter())
        except Exception:  # pragma: no cover - configuration probe is best-effort
            return False
    return False


def _maybe_classify_intake(
    transport: Any,
    metadata: dict[str, str],
    candidate: dict[str, Any],
    intake_playbook: str,
    intake_budget: "_IntakeCallBudget",
    *,
    configured: bool,
) -> dict[str, Any]:
    """Run the AI intake classifier, honouring the configuration + per-sync budget.

    Returns the classifier result dict (``status`` ``ok`` / ``not_configured`` /
    ``error`` / ``timeout`` / ``skipped_cap``). The reconciliation in
    :func:`gmail_intake_classifier.resolve_intake_lane` only acts on ``ok``, so any
    other status transparently falls back to the deterministic lane.
    """
    if not configured:
        return {"status": "not_configured"}
    classify = getattr(transport, "classify_intake_attachment", None)
    if not callable(classify):
        return {"status": "not_configured"}
    if not intake_budget.consume():
        return {"status": "skipped_cap"}
    try:
        result = classify(metadata, candidate, intake_playbook)
    except Exception:  # pragma: no cover - any classifier failure -> deterministic
        return {"status": "error"}
    if not isinstance(result, dict):
        return {"status": "error"}
    return result


def _ai_triage_confidence(ai_result: dict[str, Any], triage_reason: str) -> str | None:
    """The model confidence (0-100, as a string) when the AI drove the triage.

    Only the AI-originated triage reasons override the deterministic score; a
    deterministic/selector triage keeps its own score-derived confidence.
    """
    from .gmail_intake_classifier import (
        REASON_AI_NDA_NO_DET_BASIS,
        REASON_AI_NOT_NDA_VS_DET_NDA,
        REASON_AI_UNCERTAIN,
    )

    if ai_result.get("status") != "ok":
        return None
    if triage_reason not in {
        REASON_AI_UNCERTAIN,
        REASON_AI_NOT_NDA_VS_DET_NDA,
        REASON_AI_NDA_NO_DET_BASIS,
    }:
        return None
    try:
        confidence = float(ai_result.get("confidence") or 0)
    except (TypeError, ValueError):
        return None
    return str(int(round(max(0.0, min(1.0, confidence)) * 100)))


def _triage_min_score(transport: Any) -> int:
    getter = getattr(transport, "triage_min_nda_score", None)
    if callable(getter):
        try:
            return int(getter())
        except (TypeError, ValueError):
            pass
    return 40


def classify_attachment_lane(
    validation: dict[str, Any],
    *,
    selector_selected: bool,
    selector_configured: bool,
    triage_min_score: int = 40,
) -> tuple[str, str]:
    """Band-classify a prepared attachment into one of three lanes.

    Returns ``(lane, triage_reason)`` where ``lane`` is one of:

    - ``"confident"``: auto-ingest, no flag. The deterministic validation
      ``accepted`` bar (score >= MIN_ATTACHMENT_NDA_SCORE AND has_content_basis)
      is met, OR the AI selector is configured and selected this attachment
      (the selector promotes a below-confident attachment).
    - ``"triage"``: import anyway but flag ``needs_triage``. Not accepted, but
      there is a real NDA content basis that is merely uncertain
      (``has_content_basis`` true OR ``triage_min_score <= score < confident``),
      OR it reached here via selector-not-selected while still carrying a
      content basis.
    - ``"skip"``: clearly not an NDA (terminal precision lane).
    """
    score = _coerce_int(validation.get("score"))
    accepted = bool(validation.get("accepted"))
    has_content_basis = bool(validation.get("has_content_basis"))
    deterministic_triage = has_content_basis or score >= triage_min_score

    if selector_configured:
        # The selector is the ranking authority over candidates that already
        # cleared the deterministic floor: a selected attachment is promoted to
        # confident (even below the deterministic confident band), while a
        # non-selected attachment is demoted out of confident. A non-selected
        # attachment with a content basis becomes a flagged-for-human triage
        # matter rather than the old terminal ai_not_selected_attachment drop;
        # one with no content basis stays skip.
        if selector_selected:
            return "confident", ""
        if deterministic_triage:
            return "triage", "ai_selector_not_selected"
        return "skip", ""

    # No selector authority: the deterministic acceptance bar governs the
    # confident lane, the uncertain-but-content-bearing middle band is triaged,
    # and the rest is terminally skipped.
    if accepted:
        return "confident", ""
    if deterministic_triage:
        return "triage", "low_confidence_nda_content"
    return "skip", ""


def _coerce_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def import_inbound_attachment(
    service: Any,
    message_id: str,
    attachment: dict[str, Any],
    metadata: dict[str, str],
    *,
    transport: Any,
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    candidate, skip = prepare_inbound_attachment(service, message_id, attachment, metadata, transport=transport)
    if skip is not None or candidate is None:
        return None, skip
    return create_matter_from_prepared_attachment(candidate, metadata, transport=transport)


def prepare_inbound_attachment(
    service: Any,
    message_id: str,
    attachment: dict[str, Any],
    metadata: dict[str, str],
    *,
    transport: Any,
    owner_user_id: str = "",
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    attachment_id = str(attachment.get("attachment_id") or "")
    attachment_filename = str(attachment.get("filename") or "")
    part_id = str(attachment.get("part_id") or "")

    if gmail_attachment_already_imported(
        message_id,
        attachment_id,
        transport=transport,
        part_id=part_id,
        owner_user_id=owner_user_id,
    ):
        return None, gmail_attachment_skip(message_id, attachment_filename, "duplicate_attachment")

    try:
        document_bytes = transport.attachment_bytes(service, message_id, attachment)
    except transport.GmailIntegrationError as error:
        if _is_rate_limited(transport, error):
            # A Gmail rate-limit at the DOWNLOAD stage is a provider condition, not
            # a property of this message. Re-raise so the scan-level handler aborts
            # the pass UNCOUNTED (mirroring the messages().get 429 path) -- turning
            # it into a per-message attachment_unavailable skip would let a
            # multi-poll 429 storm walk REAL NDAs toward quarantine.
            raise
        return None, gmail_attachment_skip(message_id, attachment_filename, "attachment_unavailable")

    try:
        transport.ensure_document_size(document_bytes)
    except transport.DocumentSizeError:
        return None, gmail_attachment_skip(message_id, attachment_filename, "attachment_too_large")

    attachment_sha256 = hashlib.sha256(document_bytes).hexdigest()
    if gmail_attachment_already_imported(
        message_id,
        attachment_id,
        transport=transport,
        attachment_filename=attachment_filename,
        attachment_sha256=attachment_sha256,
        part_id=part_id,
        owner_user_id=owner_user_id,
    ):
        return None, gmail_attachment_skip(message_id, attachment_filename, "duplicate_attachment")

    # Run the CPU-heavy document parse ONCE per attachment. When the transport
    # exposes the full ``extract_document`` seam (production does), extract here
    # with ``include_visual_profile=False`` -- exactly what the create stage's
    # ``defer_pdf_conversion=True`` ingest would compute (paragraphs are
    # byte-identical either way, see ingestion_service.extract_document) -- and
    # hand the full result through the candidate so
    # ``create_matter_from_prepared_attachment`` never re-extracts the same
    # bytes. Older/fake transports without the seam keep the paragraphs-only
    # extraction and the create stage extracts as before.
    extraction_result: tuple[str, list[dict[str, Any]], dict[str, object] | None] | None = None
    extract_full = getattr(transport, "extract_document", None)
    try:
        if callable(extract_full):
            extraction_result = extract_full(
                attachment_filename,
                document_bytes,
                include_visual_profile=False,
            )
            _document_type, paragraphs = extraction_result[0], extraction_result[1]
        else:
            _document_type, paragraphs = transport.extract_document_paragraphs(attachment_filename, document_bytes)
    except transport.PdfExtractionError as error:
        return None, gmail_attachment_skip(
            message_id,
            attachment_filename,
            transport.pdf_attachment_skip_reason(error),
            detail=str(error),
        )
    except transport.DocxExtractionError as error:
        return None, gmail_attachment_skip(
            message_id,
            attachment_filename,
            "review_failed",
            detail=str(error),
        )

    # Always compute the deterministic validation and return it on the candidate;
    # the caller band-classifies (confident / triage / skip). No terminal skip
    # here, so a below-confident-but-content-bearing attachment is never dropped
    # before classification.
    validation = transport.attachment_nda_validation(
        attachment_filename,
        paragraphs,
        message_metadata=metadata,
    )

    return {
        "attachment": attachment,
        "attachment_id": attachment_id,
        "attachment_sha256": attachment_sha256,
        "document_bytes": document_bytes,
        "filename": attachment_filename,
        "message_id": message_id,
        "paragraphs": paragraphs,
        "part_id": part_id,
        "text_preview": attachment_text_preview(paragraphs),
        "validation": validation,
        # Full single-pass extraction result (document_type, paragraphs, quality)
        # for create_matter_from_prepared_attachment to reuse; None when the
        # transport lacks the extract_document seam (create re-extracts as before).
        "extraction_result": extraction_result,
    }, None


def create_matter_from_prepared_attachment(
    candidate: dict[str, Any],
    metadata: dict[str, str],
    *,
    transport: Any,
    selector_metadata: dict[str, object] | None = None,
    owner_user_id: str = "",
    triage: bool = False,
    triage_reason: str = "",
    triage_confidence: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    message_id = str(candidate.get("message_id") or "")
    attachment_id = str(candidate.get("attachment_id") or "")
    attachment_filename = str(candidate.get("filename") or "")
    attachment_sha256 = str(candidate.get("attachment_sha256") or "")
    document_bytes = candidate.get("document_bytes")
    part_id = str(candidate.get("part_id") or "")
    validation = candidate.get("validation") if isinstance(candidate.get("validation"), dict) else {}

    if not isinstance(document_bytes, bytes):
        return None, gmail_attachment_skip(message_id, attachment_filename, "attachment_unavailable")

    metadata = transport.attachment_validation_metadata(metadata, validation)
    if selector_metadata:
        metadata = transport.attachment_selector_metadata(metadata, selector_metadata)

    # Reuse the prepare stage's single-pass extraction (see
    # prepare_inbound_attachment) so the CPU-heavy parse never runs twice on the
    # same attachment bytes. Only forwarded when the candidate actually carries a
    # well-formed result; otherwise ingest extracts exactly as before.
    extraction_result = candidate.get("extraction_result")
    extraction_kwargs: dict[str, Any] = {}
    if (
        isinstance(extraction_result, tuple)
        and len(extraction_result) == 3
        and isinstance(extraction_result[0], str)
        and isinstance(extraction_result[1], list)
    ):
        extraction_kwargs["precomputed_extraction"] = extraction_result

    try:
        matter = transport.create_matter_from_document(
            filename=attachment_filename or "nda.docx",
            document_bytes=document_bytes,
            source_type="gmail_inbound",
            board_column="gmail_demo",
            intake_metadata={
                **metadata,
                "attachment_filename": attachment_filename or "nda.docx",
                "gmail_attachment_id": attachment_id,
                "gmail_attachment_sha256": attachment_sha256,
                "gmail_part_id": part_id,
                **({
                    "needs_triage": "true",
                    # When the AI drove the triage its confidence overrides the
                    # deterministic score so the dashboard card shows the model's
                    # number; otherwise the deterministic score stands.
                    "triage_confidence": (
                        triage_confidence
                        if triage_confidence is not None
                        else str(validation.get("score") or "0")
                    ),
                    "triage_reason": triage_reason,
                } if triage else {}),
            },
            dedupe_gmail=True,
            owner_user_id=owner_user_id,
            # The inbound poll runs ONLY the fast offline deterministic first-pass
            # review here; the slow AI review (assessor + verifier) is deferred to
            # on-demand (Refresh Review) so it NEVER executes in the poll thread.
            # This removes the Opus+Pro storm and the biggest per-poll memory spike
            # that was OOM-crash-looping the single prod worker.
            defer_ai_review=True,
            # DEFER the PDF->working-DOCX reconstruction off the poll thread too. The
            # pdf2docx reconstruction + in-process PyMuPDF page open + DOCX unzip
            # (which runs INLINE per PDF at ingest) monopolizes the single worker
            # during a poll and makes the app UNRESPONSIVE for users while an import
            # churns -- even a fast empty_body failure still spawns the child + holds
            # bytes. Mirroring defer_ai_review: the poll leaves each PDF as a legacy
            # PDF (page-image view, no working DOCX yet) and the reconstruction is
            # materialized later OFF the request thread by the on-demand/retro
            # conversion (retro_convert_pdf_matter_guarded) that fires when a human
            # clicks Review. Manual uploads keep converting at ingest (they never set
            # this flag). Storm-safe + fully fail-open.
            defer_pdf_conversion=True,
            **extraction_kwargs,
        )
    except (
        transport.ActiveReviewEngineError,
        transport.DocxExtractionError,
        transport.PdfExtractionError,
        transport.ParagraphAlignmentError,
    ):
        return None, gmail_attachment_skip(message_id, attachment_filename, "review_failed")

    if matter.get("_existing_gmail_duplicate"):
        return None, gmail_attachment_skip(message_id, attachment_filename, "duplicate_attachment")

    # Import is done and FAST (deterministic first-pass only, defer_ai_review=True).
    # Inbound NDAs DELIBERATELY do NOT auto-trigger an AI review: the matter imports
    # and stays "Not Reviewed" until a human clicks Review (the on-demand path,
    # POST /api/matters/<id>/review-refresh -> enqueue_on_demand_review). Auto-review
    # on import was the Gmail-storm engine (synchronous Opus+Pro per inbound NDA); it
    # is removed entirely, NOT merely flag-gated, so no inbound matter can ever
    # auto-enqueue a review by any path.
    return matter, None


def selected_candidate_attachment_ids(
    metadata: dict[str, str],
    prepared: list[dict[str, Any]],
    *,
    transport: Any,
) -> tuple[set[str] | None, dict[str, object]]:
    if not prepared or not transport.selector_configured():
        return None, {}
    try:
        selection = transport.select_nda_attachments(
            message_metadata=metadata,
            candidates=prepared,
        )
    except transport.GmailAttachmentSelectorError:
        return None, {}
    if selection.get("status") != "selected":
        return None, {}
    selected_ids = {
        str(attachment_id)
        for attachment_id in selection.get("selected_attachment_ids", [])
        if str(attachment_id)
    }
    return (selected_ids or None), selection


def message_selector_metadata(message: dict[str, Any], metadata: dict[str, str], *, transport: Any) -> dict[str, str]:
    body_preview = transport.message_body_text(message.get("payload") or {})
    if not body_preview:
        return metadata
    return {
        **metadata,
        "message_body_preview": body_preview[:transport.body_preview_limit()],
    }


def attachment_text_preview(paragraphs: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for paragraph in paragraphs[:12]:
        text = " ".join(str(paragraph.get("text") or "").split())
        if text:
            chunks.append(text)
    return "\n".join(chunks)[:3000]


def gmail_attachment_already_imported(
    message_id: str,
    attachment_id: str,
    *,
    transport: Any,
    attachment_filename: str = "",
    attachment_sha256: str = "",
    part_id: str = "",
    owner_user_id: str = "",
) -> bool:
    return transport.gmail_attachment_already_imported(
        message_id,
        attachment_id,
        attachment_filename=attachment_filename,
        attachment_sha256=attachment_sha256,
        part_id=part_id,
        owner_user_id=owner_user_id,
    )


def message_attachments_all_already_imported(
    message_id: str,
    attachments: list[dict[str, Any]],
    *,
    transport: Any,
    owner_user_id: str = "",
) -> bool:
    """True only when EVERY attachment on the message is genuinely already imported.

    Used to short-circuit the message BEFORE the content-scan PDF/DOCX extraction
    (``attachment_nda_detection``), which otherwise downloads + extracts every
    attachment on a previously-imported forward on every poll. The check passes
    ONLY pre-download identity keys (message+attachment id / part id) — never a
    content hash (which would require the very download we are trying to avoid) and
    never a filename-only match (two different documents can share a name). The
    duplicate lookup treats a filename-only overlap as NOT a match unless the byte
    hashes are equal, so omitting the hash here can only make this STRICTER, never
    looser: a non-identity match never short-circuits, preserving the false-negative
    protection. Any attachment that is not provably already imported makes this
    return ``False``, so the full per-attachment path (which adds the post-download
    sha256 dedup) still runs as the authoritative classifier.
    """
    if not attachments:
        return False
    for attachment in attachments:
        attachment_id = str(attachment.get("attachment_id") or "")
        part_id = str(attachment.get("part_id") or "")
        if not gmail_attachment_already_imported(
            message_id,
            attachment_id,
            transport=transport,
            part_id=part_id,
            owner_user_id=owner_user_id,
        ):
            return False
    return True


def gmail_attachment_skip(message_id: str, attachment_filename: str, reason: str, **details: object) -> dict[str, str]:
    skip = {
        "attachment_filename": attachment_filename,
        "message_id": message_id,
        "reason": reason,
    }
    for key, value in details.items():
        cleaned = " ".join(str(value or "").split())
        if cleaned:
            skip[key] = cleaned[:500]
    return skip
