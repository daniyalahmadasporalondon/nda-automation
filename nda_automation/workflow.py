"""The canonical Matter workflow state machine (the workflow half of a Matter).

A matter today carries several overlapping, ad-hoc status signals -- an
overloaded ``status`` flag, the kanban ``board_column``, ``triage_status`` plus a
free-text ``next_action``, ``human_reviewed``, ``approved_at``, the derived
``review_state`` (review_state.py), an approval-only ``matter_timeline``, and the
``last_outbound_*`` send stamps. None of them is *the* source of truth for "where
is this matter in its lifecycle and who owns the next move."

This module promotes those signals into ONE canonical, **purely derived**
workflow state. It reads the matter dict and returns a ``workflow_state`` object;
it never persists a parallel status machine that could drift from the underlying
fields (the same design that makes ``review_state`` trustworthy). The only thing
the workflow layer ever *writes* is the append-only ``matter_timeline`` event log
and, on failure paths only, a small ``workflow_error`` marker -- both via
dedicated matter_store writers, never from here.

The model:

* ``phase`` -- the coarse lifecycle stage: Intake -> Review -> Approval -> Sent ->
  Negotiation -> Executed.
* ``status`` -- the fine machine-state within a phase (e.g. Review ->
  ``ai_reviewing`` | ``awaiting_human``).
* ``next_action`` -- the canonical "what happens next and who owns it"
  ``{label, owner, blocked}`` (``owner`` is ``human`` or ``system``). This
  SUPERSEDES triage's free-text next_action so the UI/automation read one source.
* ``human_gate`` -- True when the matter is blocked on a person; False when a
  machine is working. The rule is mechanical: any in-progress ("-ing") status is
  machine work; the explicit waiting statuses are human gates.
* ``needs_attention`` -- the ORTHOGONAL failure axis. A render/AI/send failure
  flips this True (with an ``attention_reason``) so a matter can't silently die.
  needs_attention does not move the board column -- it's a flag/overlay.
* ``board_column`` -- the fine status rolled up into the existing kanban columns.

Negotiation entry is an explicit "counter-received" transition for now (the real
inbound-thread detection is Track C); Executed is a manual "mark signed" terminal
with a DocuSign-shaped seam but no e-signature wiring yet.
"""
from __future__ import annotations

from typing import Any, Callable, Dict

from .review_state import REVIEW_STATE_CHECK, review_state_from_result

WORKFLOW_STATE_VERSION = 1

# --- Phases (coarse lifecycle stages) -------------------------------------
PHASE_INTAKE = "intake"
PHASE_REVIEW = "review"
PHASE_APPROVAL = "approval"
PHASE_SENT = "sent"
PHASE_NEGOTIATION = "negotiation"
PHASE_EXECUTED = "executed"

PHASE_ORDER = (
    PHASE_INTAKE,
    PHASE_REVIEW,
    PHASE_APPROVAL,
    PHASE_SENT,
    PHASE_NEGOTIATION,
    PHASE_EXECUTED,
)

# --- Statuses (fine machine-state within a phase) -------------------------
# Intake
STATUS_RECEIVED = "received"
STATUS_EXTRACTING = "extracting"
STATUS_EXTRACTED = "extracted"
STATUS_INTAKE_FAILED = "intake_failed"
# Review
STATUS_RENDERING = "rendering"
STATUS_AI_REVIEWING = "ai_reviewing"
STATUS_AWAITING_HUMAN = "awaiting_human"
STATUS_AUTO_CLEARED = "auto_cleared"
STATUS_REVIEW_FAILED = "review_failed"
# Approval
STATUS_AWAITING_APPROVAL = "awaiting_approval"
STATUS_APPROVAL_BLOCKED = "approval_blocked"
STATUS_APPROVED = "approved"
# Sent
STATUS_SENDING = "sending"
STATUS_SENT_AWAITING_COUNTERPARTY = "sent_awaiting_counterparty"
STATUS_SEND_FAILED = "send_failed"
# Signature terminal-not-signed: the counterparty REFUSED (declined) or the
# envelope was CANCELLED (voided). Both clear the awaiting-signature limbo so a
# dead/cancelled deal stops reading as "awaiting counterparty" forever. They are
# split deliberately: declined is a flagged "needs attention" state the user
# renegotiates/closes; voided returns the matter to a re-sendable state.
STATUS_SIGNATURE_DECLINED = "signature_declined"
STATUS_SIGNATURE_VOIDED = "signature_voided"
# Negotiation
STATUS_COUNTER_RECEIVED = "counter_received"
STATUS_RE_REVIEWING = "re_reviewing"
# Executed
STATUS_FULLY_SIGNED = "fully_signed"

# The human-gate set: a person owns the next move. Everything NOT in this set
# (and not a failure) is machine work -- mechanically, the in-progress statuses.
HUMAN_GATE_STATUSES = frozenset({
    STATUS_AWAITING_HUMAN,
    STATUS_AWAITING_APPROVAL,
    STATUS_SENT_AWAITING_COUNTERPARTY,
    STATUS_COUNTER_RECEIVED,
})

# In-progress ("a machine is working") statuses -- never a human gate.
MACHINE_WORKING_STATUSES = frozenset({
    STATUS_EXTRACTING,
    STATUS_RENDERING,
    STATUS_AI_REVIEWING,
    STATUS_SENDING,
    STATUS_RE_REVIEWING,
})

# The orthogonal failure axis: any of these flips needs_attention True so the
# matter surfaces as stuck instead of dying silently. They do NOT move the board.
FAILURE_STATUSES = frozenset({
    STATUS_INTAKE_FAILED,
    STATUS_REVIEW_FAILED,
    STATUS_SEND_FAILED,
    # A DECLINED signature is a flagged "needs attention" state: the counterparty
    # refused, so the deal needs a human (renegotiate / re-send / close). It stays
    # visible on the board (Sent column) rather than dropping off. VOIDED is NOT
    # here: a voided envelope is a benign "cancelled to reissue" state that returns
    # the matter to re-sendable, not a failure.
    STATUS_SIGNATURE_DECLINED,
})

# Terminal statuses -- no further machine move is expected.
TERMINAL_STATUSES = frozenset({STATUS_FULLY_SIGNED})

# --- Board columns (existing kanban vocabulary; do not invent new columns) -
BOARD_INBOX = "gmail_demo"
BOARD_IN_REVIEW = "in_review"
BOARD_REVIEWED = "reviewed"
BOARD_SENT = "sent"
# Terminal, off-board state. An EXECUTED (fully-signed, 2/2) matter is done work
# and drops OFF the active board entirely -- it belongs to no kanban column. The
# rollup emits this sentinel ("") for executed matters and the board endpoint
# excludes them. A HALF-signed (1/2, not executed) matter is NOT terminal: it is
# still active outbound work and stays in BOARD_SENT.
BOARD_NONE = ""
# Legacy board columns the frontend still canonicalizes (redline_ready->reviewed,
# signed_closed->sent). We never emit these from the rollup; we only tolerate
# them as an existing board_column when deriving the phase.
LEGACY_BOARD_REVIEWED = "redline_ready"
LEGACY_BOARD_SENT = "signed_closed"

OWNER_HUMAN = "human"
OWNER_SYSTEM = "system"

# --- Timeline event types (the append-only backbone) ----------------------
# The existing approval flow already appends ``matter_approved``; these extend
# the same log into the canonical lifecycle backbone. The log is append-only and
# never rewritten.
EVENT_CREATED = "created"
EVENT_EXTRACTED = "extracted"
EVENT_REVIEW_STARTED = "review_started"
EVENT_REVIEW_COMPLETED = "review_completed"
EVENT_FLAGGED_FOR_HUMAN = "flagged_for_human"
EVENT_APPROVED = "matter_approved"  # matches the existing approval event type
EVENT_SENT = "sent"
EVENT_COUNTER_RECEIVED = "counter_received"
EVENT_EXECUTED = "executed"
EVENT_ERRORED = "errored"

MAX_EVENT_ACTOR_CHARS = 240
MAX_EVENT_DETAIL_CHARS = 2000


def workflow_state(
    matter: Dict[str, Any],
    *,
    current_playbook_hash_func: Callable[[], str] | None = None,
    current_runtime_func: Callable[[], Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Derive the canonical workflow state for a matter (pure, read-only).

    Returns ``{version, phase, status, next_action, human_gate, needs_attention,
    attention_reason, board_column, timeline_summary}``. Never mutates ``matter``
    and never persists anything.

    ``current_playbook_hash_func``/``current_runtime_func`` are injectable so a
    batch caller (corpus_index.build_corpus) can resolve the active playbook
    runtime ONCE and thread the constant resolvers down through the approval-gate
    staleness check, instead of re-reading playbook.json per matter. When omitted
    the staleness check resolves the runtime itself (the unbatched default).
    """
    if not isinstance(matter, dict):
        matter = {}

    error = _workflow_error(matter)
    phase, status = _derive_phase_and_status(
        matter,
        error,
        current_playbook_hash_func=current_playbook_hash_func,
        current_runtime_func=current_runtime_func,
    )
    needs_attention = status in FAILURE_STATUSES
    attention_reason = _attention_reason(error) if needs_attention else ""
    next_action = _next_action_for(status, matter)
    human_gate = status in HUMAN_GATE_STATUSES
    board_column = board_column_for(phase, status, matter)

    return {
        "version": WORKFLOW_STATE_VERSION,
        "phase": phase,
        "status": status,
        "label": _status_label(status),
        "phase_label": _phase_label(phase),
        "next_action": next_action,
        "human_gate": human_gate,
        "needs_attention": needs_attention,
        "attention_reason": attention_reason,
        "board_column": board_column,
        "timeline_summary": timeline_summary(matter),
    }


def _derive_phase_and_status(
    matter: Dict[str, Any],
    error: Dict[str, Any],
    *,
    current_playbook_hash_func: Callable[[], str] | None = None,
    current_runtime_func: Callable[[], Dict[str, Any]] | None = None,
) -> tuple[str, str]:
    """Compute (phase, status) from the matter's existing signals.

    Resolution order, latest-lifecycle-stage first, so a matter that has already
    moved forward never reads as an earlier phase:

    1. Executed: an explicit ``executed_at`` / executed marker (manual "mark
       signed"; DocuSign-shaped seam later). Checked BEFORE ``workflow_error`` so
       a matter that was marked executed while still carrying a stale
       ``workflow_error`` (e.g. a send failed, then it was signed/executed without
       the error being cleared) reads as DONE rather than as an active failed-send.
       This keeps the derived state consistent with ``is_matter_executed`` (which
       the board uses to drop executed matters), so the two readers can't disagree.
       Any already-contradictory matter self-heals the moment it's viewed.
    2. A recorded ``workflow_error`` short-circuits to that phase's failed status.
    3. Negotiation: an explicit ``negotiation`` marker (counter-received). The
       real inbound-thread detection is Track C; we only honor the explicit flag.
    4. Sent: a recorded outbound (``last_outbound_*``) or a ``sent`` board column.
    5. Approval: ``status == "approved"`` -> approved; otherwise, once the review
       is human-resolvable, awaiting_approval / approval_blocked.
    6. Review: a ``review_result`` exists -> derive from review_state
       (blocks_send -> awaiting_human, else auto_cleared); a review-in-flight
       marker -> ai_reviewing/rendering.
    7. Intake: nothing reviewed yet -> received/extracting/extracted.
    """
    if _is_executed(matter):
        return PHASE_EXECUTED, STATUS_FULLY_SIGNED

    if error:
        failed_status = _FAILURE_STATUS_BY_PHASE.get(str(error.get("phase") or ""))
        if failed_status:
            return _PHASE_BY_STATUS[failed_status], failed_status

    negotiation = _negotiation_status(matter)
    if negotiation is not None:
        return PHASE_NEGOTIATION, negotiation

    # A VOIDED signature envelope (sender cancelled, usually to reissue) returns
    # the matter to a RE-SENDABLE state: it drops out of the Sent/awaiting limbo and
    # back to the Approval phase where Send is available again. Checked BEFORE
    # _sent_status because the recorded outbound / sent board column would otherwise
    # pin it in "awaiting counterparty" forever.
    if _truthy(matter.get("signature_voided")):
        return PHASE_APPROVAL, STATUS_SIGNATURE_VOIDED

    sent = _sent_status(matter)
    if sent is not None:
        return PHASE_SENT, sent

    approval = _approval_status(
        matter,
        current_playbook_hash_func=current_playbook_hash_func,
        current_runtime_func=current_runtime_func,
    )
    if approval is not None:
        return PHASE_APPROVAL, approval

    review = _review_status(matter)
    if review is not None:
        return PHASE_REVIEW, review

    return PHASE_INTAKE, _intake_status(matter)


# ---- per-phase status derivation -----------------------------------------

def is_matter_executed(matter: Dict[str, Any]) -> bool:
    """Public predicate: is this matter EXECUTED (fully-signed, 2/2, work done)?

    The shared contract: a matter is executed when ``matter.executed == true``
    (status ``fully_signed``), set by DocuSign completion or a manual mark. The
    board endpoint uses this to drop executed matters off the WIP board. A
    half-signed (1/2) matter never sets this flag, so it stays on the board.
    """
    if not isinstance(matter, dict):
        return False
    if _truthy(matter.get("executed")) or matter.get("executed_at"):
        return True
    return _phase_marker(matter) == PHASE_EXECUTED


def _is_executed(matter: Dict[str, Any]) -> bool:
    return is_matter_executed(matter)


def _negotiation_status(matter: Dict[str, Any]) -> str | None:
    """Explicit counter-received entry (Track C will drive this automatically).

    A re-review-in-flight marker reads as ``re_reviewing`` (machine working);
    otherwise a counter awaiting human triage reads as ``counter_received``.
    """
    if not (_truthy(matter.get("counter_received")) or matter.get("counter_received_at") or _phase_marker(matter) == PHASE_NEGOTIATION):
        return None
    if _truthy(matter.get("re_reviewing")):
        return STATUS_RE_REVIEWING
    return STATUS_COUNTER_RECEIVED


def _sent_status(matter: Dict[str, Any]) -> str | None:
    if _truthy(matter.get("sending")):
        return STATUS_SENDING
    # A DECLINED signature is a terminal-not-signed state the counterparty refused.
    # It out-ranks the awaiting-counterparty default so a dead deal stops reading as
    # "awaiting" forever. It stays in the Sent phase (still on the board) and trips
    # needs_attention via FAILURE_STATUSES.
    if _truthy(matter.get("signature_declined")):
        return STATUS_SIGNATURE_DECLINED
    if _has_outbound(matter) or _canonical_board(matter.get("board_column")) == BOARD_SENT:
        return STATUS_SENT_AWAITING_COUNTERPARTY
    return None


def _approval_status(
    matter: Dict[str, Any],
    *,
    current_playbook_hash_func: Callable[[], str] | None = None,
    current_runtime_func: Callable[[], Dict[str, Any]] | None = None,
) -> str | None:
    """Approval-phase status, or None if the matter isn't at the approval gate yet.

    A matter reaches the approval gate once it carries a review_result whose
    flagged clauses the reviewer has resolved (``approval.approval_blocks`` is the
    canonical gate -- it returns unresolved-clause / stale-playbook reason codes).

    * ``approved`` -- already signed off (``status == "approved"`` / ``approved_at``).
    * ``approval_blocked`` -- the reviewer has engaged (``human_reviewed``) but a
      document-level blocker remains (stale playbook). Per-clause "unresolved"
      blocks keep the matter in Review (awaiting_human), where the reviewer
      resolves them; only once those are cleared does Approval surface.
    * ``awaiting_approval`` -- no blockers remain; just needs the sign-off click.
    """
    if str(matter.get("status") or "") == STATUS_APPROVED or matter.get("approved_at"):
        return STATUS_APPROVED
    review_result = matter.get("review_result")
    if not isinstance(review_result, dict):
        return None
    blocks = _approval_blocks(
        matter,
        current_playbook_hash_func=current_playbook_hash_func,
        current_runtime_func=current_runtime_func,
    )
    if not blocks:
        # Clean review (auto-cleared all-pass) sits in Review/auto_cleared until a
        # human engages; an engaged reviewer with nothing left to resolve is at
        # the approval gate.
        if _truthy(matter.get("human_reviewed")):
            return STATUS_AWAITING_APPROVAL
        return None
    # There are blockers. Unresolved per-clause decisions belong to Review; a
    # document-level blocker (stale playbook) with the reviewer already engaged is
    # an Approval-phase block.
    if _only_unresolved_clause_blocks(blocks):
        return None
    if _truthy(matter.get("human_reviewed")):
        return STATUS_APPROVAL_BLOCKED
    return None


def _review_status(matter: Dict[str, Any]) -> str | None:
    if _truthy(matter.get("ai_reviewing")):
        return STATUS_AI_REVIEWING
    if _truthy(matter.get("rendering")):
        return STATUS_RENDERING
    review_result = matter.get("review_result")
    if isinstance(review_result, dict):
        state = review_state_from_result(review_result)
        # An unresolved fail (check) state belongs in Review/awaiting_human just like
        # needs-review: a human must resolve the flagged clauses before it can move
        # on. aggregate_review_state's blocks_send/requires_human_review are review>0
        # only, so consume the already-computed blocks_auto_send/requires_redline
        # (and the CHECK state) so a pure-fail matter is no longer auto_cleared. Once
        # the human engages (human_reviewed) the earlier _approval_status branch
        # advances it to awaiting_approval, so this never wedges a fail-state matter.
        if (
            bool(state.get("blocks_send"))
            or bool(state.get("requires_human_review"))
            or bool(state.get("blocks_auto_send"))
            or bool(state.get("requires_redline"))
            or str(state.get("state") or "") == REVIEW_STATE_CHECK
        ):
            return STATUS_AWAITING_HUMAN
        return STATUS_AUTO_CLEARED
    return None


def _approval_blocks(
    matter: Dict[str, Any],
    *,
    current_playbook_hash_func: Callable[[], str] | None = None,
    current_runtime_func: Callable[[], Dict[str, Any]] | None = None,
) -> list[str]:
    """The approval gate's reason codes, via the canonical approval module.

    Imported lazily because ``approval`` imports several review modules at load
    time; reaching into it only at call time keeps workflow.py's import graph
    light and avoids a cycle. Failing closed (treat an unreadable gate as "no
    derivable Approval status") just leaves the matter in Review, never advances
    it past a real block.

    The optional resolvers are threaded into ``approval.approval_blocks`` so a
    batch caller can supply a once-resolved runtime/hash instead of paying a
    per-matter playbook.json read in the staleness check.
    """
    try:
        from . import approval

        kwargs: Dict[str, Any] = {}
        if current_playbook_hash_func is not None:
            kwargs["current_playbook_hash_func"] = current_playbook_hash_func
        if current_runtime_func is not None:
            kwargs["current_runtime_func"] = current_runtime_func
        return list(approval.approval_blocks(matter, **kwargs))
    except Exception:
        return []


def _only_unresolved_clause_blocks(blocks: list[str]) -> bool:
    from . import approval

    return bool(blocks) and all(
        str(block).startswith(approval.UNRESOLVED_CLAUSE_PREFIX) for block in blocks
    )


def _intake_status(matter: Dict[str, Any]) -> str:
    if _truthy(matter.get("extracting")):
        return STATUS_EXTRACTING
    if str(matter.get("extracted_text") or "").strip():
        return STATUS_EXTRACTED
    return STATUS_RECEIVED


# ---- next_action (the single canonical "what next + who owns it") ----------

def _next_action_for(status: str, matter: Dict[str, Any]) -> Dict[str, Any]:
    label, owner, blocked = _NEXT_ACTION_BY_STATUS.get(
        status, ("Review matter", OWNER_HUMAN, False)
    )
    return {"label": label, "owner": owner, "blocked": blocked}


# (label, owner, blocked) per fine status. ``blocked`` means the matter cannot
# advance until the owner acts (a hard gate), distinct from ``owner`` (who acts).
_NEXT_ACTION_BY_STATUS: Dict[str, tuple[str, str, bool]] = {
    STATUS_RECEIVED: ("Extract document text", OWNER_SYSTEM, False),
    STATUS_EXTRACTING: ("Extracting document text", OWNER_SYSTEM, False),
    STATUS_EXTRACTED: ("Run review", OWNER_SYSTEM, False),
    STATUS_INTAKE_FAILED: ("Re-import document (extraction failed)", OWNER_HUMAN, True),
    STATUS_RENDERING: ("Rendering document", OWNER_SYSTEM, False),
    STATUS_AI_REVIEWING: ("AI review in progress", OWNER_SYSTEM, False),
    STATUS_AWAITING_HUMAN: ("Resolve flagged clauses", OWNER_HUMAN, True),
    STATUS_AUTO_CLEARED: ("Approve matter", OWNER_HUMAN, False),
    STATUS_REVIEW_FAILED: ("Re-run review (review failed)", OWNER_HUMAN, True),
    STATUS_AWAITING_APPROVAL: ("Approve matter", OWNER_HUMAN, False),
    STATUS_APPROVAL_BLOCKED: ("Resolve approval blockers", OWNER_HUMAN, True),
    STATUS_APPROVED: ("Send redline to counterparty", OWNER_HUMAN, False),
    STATUS_SENDING: ("Sending redline", OWNER_SYSTEM, False),
    STATUS_SENT_AWAITING_COUNTERPARTY: ("Await counterparty response", OWNER_HUMAN, False),
    STATUS_SEND_FAILED: ("Retry send (send failed)", OWNER_HUMAN, True),
    # Counterparty refused: a human must renegotiate / re-send / close. Flagged
    # (needs_attention) and blocked (the matter cannot advance until they act).
    STATUS_SIGNATURE_DECLINED: ("Counterparty declined — renegotiate or close", OWNER_HUMAN, True),
    # Envelope cancelled: Send is available again (re-sendable, not blocked).
    STATUS_SIGNATURE_VOIDED: ("Re-send for signature", OWNER_HUMAN, False),
    STATUS_COUNTER_RECEIVED: ("Triage counterparty changes", OWNER_HUMAN, False),
    STATUS_RE_REVIEWING: ("Re-reviewing counterparty changes", OWNER_SYSTEM, False),
    STATUS_FULLY_SIGNED: ("Matter executed", OWNER_HUMAN, False),
}


# ---- board rollup (fine status -> existing kanban column) -----------------

def board_column_for(phase: str, status: str, matter: Dict[str, Any]) -> str:
    """Roll the fine phase/status up into one of the existing kanban columns.

    A failure does NOT move the column (needs_attention is a separate flag), so a
    failed matter stays visible where it already was. Inbox (gmail_demo) is the
    raw-arrival column for un-reviewed gmail imports; once a matter is being
    reviewed it moves to in_review.

    An EXECUTED (fully-signed) matter is terminal work-done: it resolves to the
    off-board sentinel (BOARD_NONE) and the board endpoint drops it. The board is
    WIP only, so a finished matter leaves it. A half-signed (sent, not executed)
    matter is still active and rolls up to BOARD_SENT below.
    """
    if phase == PHASE_EXECUTED:
        return BOARD_NONE
    if status in FAILURE_STATUSES:
        # Keep the matter in the column it already occupied; fall back to the
        # phase rollup if no usable board_column is recorded.
        existing = _canonical_board(matter.get("board_column"))
        if existing:
            return existing
    if phase == PHASE_INTAKE:
        # Un-reviewed gmail arrivals stay in Inbox; a manual upload that declared
        # a stage keeps it. Otherwise default to in_review once intake completes.
        existing = _canonical_board(matter.get("board_column"))
        if existing == BOARD_INBOX:
            return BOARD_INBOX
        return existing or BOARD_IN_REVIEW
    if phase == PHASE_REVIEW:
        return BOARD_IN_REVIEW
    if phase == PHASE_APPROVAL:
        return BOARD_REVIEWED
    if phase in (PHASE_SENT, PHASE_NEGOTIATION):
        return BOARD_SENT
    return _canonical_board(matter.get("board_column")) or BOARD_IN_REVIEW


# ---- timeline events (the append-only backbone) ---------------------------

def build_timeline_event(
    event_type: str,
    *,
    phase: str = "",
    status: str = "",
    actor: str = "",
    detail: str = "",
    at: str = "",
) -> Dict[str, Any]:
    """Construct one canonical, immutable timeline event.

    The single shape every transition appends: ``{type, at, phase?, status?,
    actor?, detail?}``. ``at`` defaults to now (UTC ISO-8601). Optional fields are
    omitted when empty so the log stays compact. matter_store.append_timeline_event
    persists the returned event (append-only, under the store lock).
    """
    from datetime import datetime, timezone

    event: Dict[str, Any] = {
        "type": str(event_type or "").strip() or EVENT_ERRORED,
        "at": str(at or "").strip() or datetime.now(timezone.utc).isoformat(),
    }
    phase = str(phase or "").strip()
    if phase:
        event["phase"] = phase
    status = str(status or "").strip()
    if status:
        event["status"] = status
    actor = str(actor or "").strip()[:MAX_EVENT_ACTOR_CHARS]
    if actor:
        event["actor"] = actor
    detail = str(detail or "").strip()[:MAX_EVENT_DETAIL_CHARS]
    if detail:
        event["detail"] = detail
    return event


# ---- timeline summary -----------------------------------------------------

def timeline_summary(matter: Dict[str, Any]) -> Dict[str, Any]:
    """A compact view of the append-only timeline for the aggregate.

    The full event log lives in ``matter_timeline``; here we surface the count and
    the most recent event so the UI/board can show "last moved" without walking
    the whole log.
    """
    timeline = matter.get("matter_timeline")
    events = [event for event in timeline if isinstance(event, dict)] if isinstance(timeline, list) else []
    last = events[-1] if events else None
    summary: Dict[str, Any] = {"event_count": len(events)}
    if last is not None:
        summary["last_event"] = {
            "type": str(last.get("type") or ""),
            "at": str(last.get("at") or ""),
        }
    return summary


# ---- helpers --------------------------------------------------------------

def _workflow_error(matter: Dict[str, Any]) -> Dict[str, Any]:
    error = matter.get("workflow_error")
    if isinstance(error, dict) and str(error.get("phase") or ""):
        return error
    return {}


def _attention_reason(error: Dict[str, Any]) -> str:
    message = str(error.get("message") or "").strip()
    if message:
        return message
    code = str(error.get("code") or "").strip()
    if code:
        return code
    return "A processing step failed; this matter needs attention."


def _phase_marker(matter: Dict[str, Any]) -> str:
    """An explicit ``workflow_phase`` override, if a writer recorded one.

    Pure derivation is the default; this is only honored for the phases whose
    entry is an explicit human action with no other derivable signal
    (negotiation, executed). It is never required for the inbound happy path.
    """
    return str(matter.get("workflow_phase") or "").strip().lower()


def _has_outbound(matter: Dict[str, Any]) -> bool:
    return any(
        str(matter.get(field) or "").strip()
        for field in ("last_outbound_at", "last_outbound_message_id", "last_outbound_to")
    )


def _canonical_board(board_column: object) -> str:
    column = str(board_column or "").strip()
    if column == LEGACY_BOARD_REVIEWED:
        return BOARD_REVIEWED
    if column == LEGACY_BOARD_SENT:
        return BOARD_SENT
    if column in (BOARD_INBOX, BOARD_IN_REVIEW, BOARD_REVIEWED, BOARD_SENT):
        return column
    return ""


def _truthy(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _phase_label(phase: str) -> str:
    return {
        PHASE_INTAKE: "Intake",
        PHASE_REVIEW: "Review",
        PHASE_APPROVAL: "Approval",
        PHASE_SENT: "Sent",
        PHASE_NEGOTIATION: "Negotiation",
        PHASE_EXECUTED: "Executed",
    }.get(phase, "Intake")


def _status_label(status: str) -> str:
    explicit = _STATUS_LABELS.get(status)
    if explicit:
        return explicit
    return status.replace("_", " ").title()


# Human-facing labels that differ from the auto-titlecase default. The two
# signature terminal-not-signed states carry distinct, action-oriented copy so the
# board card + matter detail communicate what happened and what to do next.
_STATUS_LABELS: Dict[str, str] = {
    STATUS_SIGNATURE_DECLINED: "Declined — needs attention",
    STATUS_SIGNATURE_VOIDED: "Voided — ready to re-send",
}


_PHASE_BY_STATUS: Dict[str, str] = {
    STATUS_RECEIVED: PHASE_INTAKE,
    STATUS_EXTRACTING: PHASE_INTAKE,
    STATUS_EXTRACTED: PHASE_INTAKE,
    STATUS_INTAKE_FAILED: PHASE_INTAKE,
    STATUS_RENDERING: PHASE_REVIEW,
    STATUS_AI_REVIEWING: PHASE_REVIEW,
    STATUS_AWAITING_HUMAN: PHASE_REVIEW,
    STATUS_AUTO_CLEARED: PHASE_REVIEW,
    STATUS_REVIEW_FAILED: PHASE_REVIEW,
    STATUS_AWAITING_APPROVAL: PHASE_APPROVAL,
    STATUS_APPROVAL_BLOCKED: PHASE_APPROVAL,
    STATUS_APPROVED: PHASE_APPROVAL,
    STATUS_SENDING: PHASE_SENT,
    STATUS_SENT_AWAITING_COUNTERPARTY: PHASE_SENT,
    STATUS_SEND_FAILED: PHASE_SENT,
    STATUS_SIGNATURE_DECLINED: PHASE_SENT,
    STATUS_SIGNATURE_VOIDED: PHASE_APPROVAL,
    STATUS_COUNTER_RECEIVED: PHASE_NEGOTIATION,
    STATUS_RE_REVIEWING: PHASE_NEGOTIATION,
    STATUS_FULLY_SIGNED: PHASE_EXECUTED,
}

_FAILURE_STATUS_BY_PHASE: Dict[str, str] = {
    PHASE_INTAKE: STATUS_INTAKE_FAILED,
    PHASE_REVIEW: STATUS_REVIEW_FAILED,
    PHASE_SENT: STATUS_SEND_FAILED,
}
