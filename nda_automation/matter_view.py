from __future__ import annotations

from copy import deepcopy
from typing import Any, TypedDict

from . import artifact_registry, governing_law_view
from .concept_classifier import classify_document_concepts
from .contract_structure import build_contract_structure
from .gmail_integration import matter_reply_recipient, recipient_email
from .pdf_export_service import public_matter_document_downloads
from .reference_resolver import resolve_document_references
from .review_document import split_document_paragraphs
from .review_state import (
    aggregate_review_state,
    result_requires_human_review,
    review_state_from_result,
    review_was_ai_executed,
)
from .source_fidelity import source_fidelity_payload
from .workflow import workflow_state


class PublicMatter(TypedDict, total=False):
    ai_review_ran: bool
    approved_at: str
    approver: str
    artifacts: list[dict[str, Any]]
    attachment_filename: str
    board_column: str
    can_send_redline: bool
    counterparty: str
    counterparty_confidence: float
    counterparty_needs_confirmation: bool
    counterparty_source: str
    counterparty_verified: bool
    created_at: str
    current_artifact_id: str
    document_downloads: dict[str, Any]
    document_title: str
    gmail_account: str
    gmail_attachment_selector: str
    gmail_attachment_selector_confidence: str
    gmail_attachment_selector_model: str
    gmail_attachment_selector_reason: str
    governing_law: str
    has_ai_review: bool
    has_redline_draft: bool
    human_reviewed: bool
    id: str
    issue_count: int
    last_outbound_account: str
    last_outbound_at: str
    last_outbound_filename: str
    last_outbound_message_id: str
    last_outbound_subject: str
    last_outbound_thread_id: str
    last_outbound_to: str
    matter_timeline: list[dict[str, Any]]
    message_snippet: str
    next_action: str
    recipient_email: str
    received_at: str
    recipient_redirected_from_reply_to: bool
    recipient_warning: str
    requirements_failed: int
    requirements_needs_review: int
    requirements_passed: int
    reply_to: str
    review_state: dict[str, Any]
    reviewer_decisions: dict[str, Any]
    sender: str
    send_block_reason: str
    source_filename: str
    source_type: str
    status: str
    subject: str
    term_label: str
    term_years: float
    triage_status: str
    updated_at: str
    workflow_state: dict[str, Any]


PUBLIC_MATTER_FIELDS = {
    "approved_at",
    "approver",
    "attachment_filename",
    "board_column",
    "created_at",
    "docusign",
    "document_title",
    "drive",
    "executed",
    "executed_at",
    "gmail_account",
    "gmail_attachment_selector",
    "gmail_attachment_selector_confidence",
    "gmail_attachment_selector_model",
    "gmail_attachment_selector_reason",
    "human_reviewed",
    "id",
    "issue_count",
    "last_outbound_account",
    "last_outbound_at",
    "last_outbound_filename",
    "last_outbound_message_id",
    "last_outbound_subject",
    "last_outbound_thread_id",
    "last_outbound_to",
    "matter_timeline",
    "message_snippet",
    "needs_triage",
    "next_action",
    "pdf_annotations",
    "received_at",
    "requirements_failed",
    "requirements_needs_review",
    "requirements_passed",
    "reply_to",
    "review_state",
    "reviewer_decisions",
    "sender",
    "send_block_reason",
    "source_filename",
    "source_type",
    "status",
    "subject",
    "triage_confidence",
    "triage_reason",
    "triage_status",
    "updated_at",
}


def public_matter(matter: dict[str, Any], *, detail: bool = True) -> PublicMatter:
    recipient = matter_reply_recipient(matter)
    send_block_reason = ""
    if recipient and _same_email_address(recipient, str(matter.get("gmail_account") or "")):
        send_block_reason = (
            "Matter appears to be an outbound or self-sent Gmail message; refusing to send a redline "
            f"back to {recipient}."
        )
    elif matter_needs_human_review(matter) and not _matter_review_block_resolved(matter):
        send_block_reason = "Matter needs human review before a redline can be sent."
    elif not recipient:
        send_block_reason = "Matter does not have a valid reply recipient email address."
    public = {
        key: value
        for key, value in matter.items()
        if key in PUBLIC_MATTER_FIELDS
    }
    public.update({
        "recipient_email": recipient,
        "can_send_redline": bool(recipient and not send_block_reason),
        "has_redline_draft": isinstance(matter.get("redline_draft"), dict),
        "human_reviewed": bool(matter.get("human_reviewed")),
    })
    recipient_warning = _recipient_redirect_warning(matter, recipient)
    if recipient_warning:
        public["recipient_redirected_from_reply_to"] = True
        public["recipient_warning"] = recipient_warning
    review_state = matter_review_state(matter)
    if review_state:
        public["review_state"] = review_state
    # The canonical workflow state (phase/status/next_action/human_gate/
    # needs_attention) -- one derived source the UI and automation read instead of
    # guessing from the overlapping status/board/triage fields. Its
    # next_action SUPERSEDES the legacy free-text top-level next_action.
    workflow = workflow_state(matter)
    public["workflow_state"] = workflow
    public["next_action"] = workflow["next_action"]["label"]
    # A derived, best-available counterparty name so the dashboard can group/find
    # matters by who they're with. For a generated NDA this is the exact manifest
    # company name; for an inbound matter it's the cleaned email subject; otherwise
    # "Unknown Counterparty". One source of truth: artifact_registry.derive_counterparty
    # (the same name drive_integration files under), so the UI and Drive never drift.
    public["counterparty"] = artifact_registry.derive_counterparty(matter)
    # Surface the AI-extracted-counterparty provenance the human-confirmation UI
    # needs alongside the display name: the raw confidence/verified/source from the
    # stored extraction dict, plus a single derived needs_confirmation flag. The
    # display name above still comes from derive_counterparty (a verified extraction
    # OR a cleaned subject fallback), so the UI shows a usable name even while the
    # extraction is unconfirmed.
    public.update(_counterparty_confirmation_fields(matter))
    # Matter facts the Overview roster reads alongside the counterparty + received
    # date: the governing law (a Playbook approved-option id, "" when unknown -- the
    # same value the corpus/dashboard derive) and the detected term. Both are
    # best-effort derivations over the stored review; an absent/unclear value
    # degrades to "" / None rather than guessing, so the UI shows "not specified".
    public.update(_matter_facts_fields(matter))
    # Whether ANY review (deterministic OR AI) has produced a stored result -- the
    # legacy "No review yet" probe. Kept verbatim (callers/tests depend on it); do
    # NOT repurpose. It is intentionally broader than ``ai_review_ran``.
    public["has_ai_review"] = _matter_has_any_review(matter)
    # Whether an AI review has ACTUALLY run (executed_engine == "ai_first"). This is
    # the single boolean every user-facing VERDICT surface gates on: a
    # deterministic-only matter has has_ai_review=True but ai_review_ran=False, so
    # the UI shows "Review not run / Pending" instead of deterministic verdicts.
    # Triage metadata (counterparty/dedup/issue_count routing) is unaffected.
    public["ai_review_ran"] = _matter_ai_review_ran(matter)
    public["document_downloads"] = public_matter_document_downloads(matter)
    # The artifact registry view: the tracked documents on the matter plus the
    # current_artifact_id pointer ("the version that matters now"). A compact
    # projection -- provenance the UI needs, never the storage internals
    # (content_hash/stored_filename stay server-side).
    artifacts_view = matter_artifacts_view(matter)
    if artifacts_view:
        public["artifacts"] = artifacts_view
        public["current_artifact_id"] = str(matter.get(artifact_registry.CURRENT_ARTIFACT_FIELD) or "")
    if send_block_reason:
        public["send_block_reason"] = send_block_reason
    return public


def matter_artifacts_view(matter: dict[str, Any]) -> list[dict[str, Any]]:
    """A compact, UI-facing projection of the matter's tracked artifacts.

    Exposes provenance (id/source/actor/role/version/name/based_on/created_at)
    plus an ``is_current`` flag, in registration order. Storage internals
    (content_hash, stored_filename) are deliberately omitted from the public
    shape; callers that need bytes go through the registry by artifact id.
    """
    current_id = str(matter.get(artifact_registry.CURRENT_ARTIFACT_FIELD) or "")
    view: list[dict[str, Any]] = []
    for artifact in artifact_registry.matter_artifacts(matter):
        view.append({
            "id": artifact.id,
            "source": artifact.source,
            "actor": artifact.actor,
            "role": artifact.role,
            "version": artifact.version,
            "name": artifact.name,
            "ext": artifact.ext,
            "based_on_artifact_id": artifact.based_on_artifact_id,
            "created_at": artifact.created_at,
            "is_current": bool(current_id) and artifact.id == current_id,
        })
    return view


COUNTERPARTY_CONFIRMATION_THRESHOLD = 0.75


def _counterparty_confirmation_fields(matter: dict[str, Any]) -> dict[str, Any]:
    """Project the stored AI-extracted-counterparty dict into the public shape.

    Reads DEFENSIVELY from ``matter["intake_metadata"]["counterparty"]`` (the shared
    storage contract): the matter may lack the key entirely, ``intake_metadata`` may
    be absent or not a dict, and the value may not be a dict. Any of those degrade
    to ``needs_confirmation = True`` (fail-open: an absent/unparseable extraction is
    treated as unconfirmed, never a crash).

    ``needs_confirmation`` is True when the stored dict is missing, ``verified`` is
    falsey, OR ``confidence`` < 0.75 -- so the UI prompts a human to confirm exactly
    when we should not silently trust the extraction.
    """
    record = _stored_counterparty_record(matter)
    if record is None:
        return {
            "counterparty_confidence": 0.0,
            "counterparty_verified": False,
            "counterparty_source": "",
            "counterparty_needs_confirmation": True,
        }
    verified = bool(record.get("verified"))
    confidence = _safe_confidence(record.get("confidence"))
    source = str(record.get("source") or "")
    needs_confirmation = not verified or confidence < COUNTERPARTY_CONFIRMATION_THRESHOLD
    return {
        "counterparty_confidence": confidence,
        "counterparty_verified": verified,
        "counterparty_source": source,
        "counterparty_needs_confirmation": needs_confirmation,
    }


def _stored_counterparty_record(matter: dict[str, Any]) -> dict[str, Any] | None:
    intake = matter.get("intake_metadata")
    if not isinstance(intake, dict):
        return None
    record = intake.get("counterparty")
    if not isinstance(record, dict):
        return None
    return record


def _safe_confidence(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _matter_facts_fields(matter: dict[str, Any]) -> dict[str, Any]:
    """Project the matter's governing-law + term facts into the public shape.

    Both are best-effort derivations over the stored review and fail open: any
    derivation failure degrades to ``""`` / ``None`` rather than crashing the
    projection, so a sparse/odd review never breaks the matter view.

    * ``governing_law`` -- the Playbook approved-option id (e.g. ``difc``), reusing
      ``governing_law_view.derive_governing_law`` (the same single source the corpus
      and Drive index read); ``""`` when no approved law is detectable.
    * ``term_years`` -- the clean ``term_years`` scalar the checker persists on the
      ``term_and_survival`` clause result, or ``None`` when unknown.
    * ``term_label`` -- a human display string for ``term_years`` (e.g. ``"1 year"``
      / ``"3 years"``), or ``""`` when the term is unknown.
    """
    try:
        governing_law = governing_law_view.derive_governing_law(matter)
    except Exception:  # noqa: BLE001 -- an odd review never breaks the matter view.
        governing_law = ""
    term_years = _matter_term_years(matter)
    return {
        "governing_law": governing_law,
        "term_years": term_years,
        "term_label": _term_label(term_years),
    }


def _matter_term_years(matter: dict[str, Any]) -> float | None:
    """Best-effort term in years from the stored ``term_and_survival`` clause.

    Reads the clean ``term_years`` scalar the checker persists (the same field the
    corpus ``term_years`` facet reads); absent/odd/non-positive -> ``None`` so the
    term degrades to "unknown" rather than guessing. Never raises.
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


def _term_label(term_years: float | None) -> str:
    """A human display string for a term in years (e.g. ``"1 year"`` / ``"3 years"``).

    Renders a whole number without a trailing ``.0`` and pluralizes the unit; a
    fractional term keeps one decimal. ``None`` -> ``""`` (term unknown).
    """
    if term_years is None or term_years <= 0:
        return ""
    if float(term_years).is_integer():
        whole = int(term_years)
        return f"{whole} year" if whole == 1 else f"{whole} years"
    rendered = f"{term_years:.1f}".rstrip("0").rstrip(".")
    return f"{rendered} years"


def _matter_has_any_review(matter: dict[str, Any]) -> bool:
    """True when ANY review (deterministic or AI) has produced a stored result.

    The Overview empty state ("No review yet") reads this single boolean. We treat
    a non-empty ``review_result`` OR a stored ``ai_first_review_result`` as "a
    review has run". This is deliberately broader than "an AI review has run"
    (routes.matters._matter_has_ai_review) -- the empty state is about whether the
    roster has any verdicts to show, not which engine produced them.
    """
    review_result = matter.get("review_result")
    if isinstance(review_result, dict) and review_result:
        return True
    ai_first = matter.get("ai_first_review_result")
    return isinstance(ai_first, dict) and bool(ai_first)


def _matter_ai_review_ran(matter: dict[str, Any]) -> bool:
    """True when an AI (ai_first) review has actually run for this matter.

    Reliable signal: ``review_result.active_review_engine.executed_engine ==
    "ai_first"`` (a completed AI review overwrites ``review_result``). A stored
    ``ai_first_review_result`` is also accepted as positive provenance. This is the
    single boolean every user-facing VERDICT surface gates on -- a deterministic-only
    matter returns False so the UI shows "Review not run / Pending" instead of the
    deterministic verdict (the last "deterministic ghost"). Triage metadata is
    unaffected. Delegates to the shared ``review_state.review_was_ai_executed``.
    """
    if review_was_ai_executed(matter.get("review_result")):
        return True
    ai_first = matter.get("ai_first_review_result")
    return isinstance(ai_first, dict) and bool(ai_first)


def _matter_review_block_resolved(matter: dict[str, Any]) -> bool:
    """Has a human resolved the review/fail block so the redline can be sent?

    Mirrors matter_lifecycle._matter_review_block_resolved so the UI projection's
    can_send_redline / send_block_reason stay aligned with the actual send gate:
    a needs-review OR unresolved-fail (check) matter is sendable once a human
    engages it -- ``human_reviewed`` set, or a recorded approval (a stronger
    sign-off). Kept local to avoid importing matter_lifecycle (cycle).
    """
    if matter.get("human_reviewed"):
        return True
    if str(matter.get("status") or "").strip().lower() == "approved":
        return True
    return bool(matter.get("approved_at"))


def matter_needs_human_review(matter: dict[str, Any]) -> bool:
    review_result = matter.get("review_result")
    if isinstance(review_result, dict):
        return result_requires_human_review(review_result)
    review_state = matter.get("review_state")
    if isinstance(review_state, dict):
        # Mirror result_requires_human_review for the stored-state fallback: an
        # unresolved fail (check) state must gate the send like needs-review does,
        # consuming the already-computed blocks_auto_send/requires_redline flags
        # (and the CHECK state itself) rather than only the review-state flags.
        if (
            bool(review_state.get("requires_human_review"))
            or bool(review_state.get("blocks_send"))
            or bool(review_state.get("blocks_auto_send"))
            or bool(review_state.get("requires_redline"))
        ):
            return True
        if str(review_state.get("state") or "") in {"review", "check"}:
            return True
    try:
        needs_review = int(matter.get("requirements_needs_review") or 0) > 0
    except (TypeError, ValueError):
        return True
    try:
        failed = int(matter.get("requirements_failed") or 0) > 0
    except (TypeError, ValueError):
        return True
    return needs_review or failed


def matter_review_state(matter: dict[str, Any]) -> dict[str, Any]:
    review_result = matter.get("review_result")
    if isinstance(review_result, dict):
        return review_state_from_result(review_result)
    existing = matter.get("review_state")
    if isinstance(existing, dict) and existing.get("state"):
        return existing
    if any(key in matter for key in ["requirements_passed", "requirements_needs_review", "requirements_failed"]):
        return aggregate_review_state(
            [],
            pass_count=_safe_count(matter.get("requirements_passed")),
            review_count=_safe_count(matter.get("requirements_needs_review")),
            check_count=_safe_count(matter.get("requirements_failed")),
        )
    return {}


def review_matter(matter: dict[str, Any]) -> dict[str, Any]:
    extracted_text = str(matter.get("extracted_text") or "")
    review_payload = {
        "matter": public_matter(matter),
        "extracted_text": extracted_text,
    }
    review_result = matter.get("review_result")
    if isinstance(review_result, dict):
        structured_review_result = review_result_with_structure(review_result, extracted_text)
        review_payload["review_result"] = structured_review_result
        source_fidelity = structured_review_result.get("source_fidelity")
        review_payload["source_fidelity"] = (
            source_fidelity
            if isinstance(source_fidelity, dict)
            else source_fidelity_payload(
                structured_review_result,
                source=structured_review_result.get("source") if isinstance(structured_review_result.get("source"), dict) else None,
            )
        )
    ai_first_review_metadata = matter.get("ai_first_review_metadata")
    if isinstance(ai_first_review_metadata, dict):
        review_payload["ai_first_review_metadata"] = ai_first_review_metadata
    ai_first_review_result = matter.get("ai_first_review_result")
    if isinstance(ai_first_review_result, dict):
        review_payload["ai_first_review_result"] = review_result_with_structure(ai_first_review_result, extracted_text)
    redline_draft = matter.get("redline_draft")
    if isinstance(redline_draft, dict):
        review_payload["redline_draft"] = redline_draft
    return review_payload


def _safe_count(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def review_result_with_structure(review_result: dict[str, Any], extracted_text: str = "") -> dict[str, Any]:
    if (
        isinstance(review_result.get("contract_structure"), dict)
        and isinstance(review_result.get("reference_resolver"), dict)
        and isinstance(review_result.get("concept_classifier"), dict)
    ):
        return review_result

    enriched = deepcopy(review_result)
    paragraphs = enriched.get("paragraphs")
    if not isinstance(paragraphs, list):
        paragraphs = split_document_paragraphs(extracted_text)
        if paragraphs:
            enriched["paragraphs"] = paragraphs
    if not isinstance(enriched.get("contract_structure"), dict):
        enriched["contract_structure"] = build_contract_structure(paragraphs if isinstance(paragraphs, list) else [])
    if not isinstance(enriched.get("reference_resolver"), dict):
        enriched["reference_resolver"] = resolve_document_references(
            paragraphs if isinstance(paragraphs, list) else [],
            enriched["contract_structure"],
        )
    if not isinstance(enriched.get("concept_classifier"), dict):
        enriched["concept_classifier"] = classify_document_concepts(
            paragraphs if isinstance(paragraphs, list) else [],
            enriched["contract_structure"],
        )
    return enriched


def public_matters(matters: list[dict[str, Any]]) -> list[PublicMatter]:
    return [public_matter(matter, detail=False) for matter in matters]


def _same_email_address(left: str, right: str) -> bool:
    return bool(left and right and left.strip().casefold() == right.strip().casefold())


def _recipient_redirect_warning(matter: dict[str, Any], recipient: str) -> str:
    """Warn when the resolved recipient came from an untrusted ``Reply-To``.

    The inbound ``Reply-To`` header is attacker-controlled. When it points at a
    different address than the verified ``From`` sender, honouring it silently
    would let a spoofed ``Reply-To`` redirect the outbound document. We still
    surface the matter so a human can decide, but we flag the divergence so the
    operator confirms the destination deliberately rather than by default.

    We fail toward warning: if the ``From`` sender is absent or unparseable we
    cannot confirm the Reply-To matches a verified participant, so we treat that
    as a divergence and warn. Otherwise an attacker could suppress the warning by
    pairing a spoofed Reply-To with a malformed From header.
    """
    if not recipient:
        return ""
    reply_to_recipient = recipient_email(matter.get("reply_to"))
    if not reply_to_recipient or not _same_email_address(recipient, reply_to_recipient):
        return ""
    sender_recipient = recipient_email(matter.get("sender"))
    if sender_recipient and _same_email_address(reply_to_recipient, sender_recipient):
        return ""
    sender_label = sender_recipient or "an unverified sender"
    return (
        f"Reply-To ({reply_to_recipient}) differs from the sender ({sender_label}). "
        "Confirm the recipient before sending."
    )
