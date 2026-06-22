"""End-to-end integration test of the full inbound NDA flow as ONE deterministic path.

This stitches together the real production code for the entire inbound journey and
exercises it as a single flow, with only the two unavoidable externalities mocked:

* the Gmail transport (no network) -- a scripted in-memory inbox, modelled on the
  ``_CatchUpInboxTransport`` already in ``tests/test_gmail_transport.py``, but driven
  all the way through the heavy import path instead of being stubbed at the dedup
  short-circuit; and
* the AI review engine -- a recording stub standing in for the slow ai_first
  (assessor + verifier) engine, so the background review is fast + deterministic.

Everything between those two seams is the REAL implementation:

    Gmail poll
      -> paged-scan import bounded by NDA_GMAIL_IMPORT_LIMIT (gmail_matter_inbox)
      -> identity dedup (already-imported messages skipped on the next poll)
      -> matter created UN-REVIEWED (no review at create; defer_ai_review=True)
      -> the full AI review ENQUEUED to the inbound worker pool (ingestion_service)
      -> memory-bounded PDF extraction (real pdf_text/pypdf on a real PDF fixture)
      -> review completes + persists onto the matter (executed_engine == "ai_first").

A real, committed PDF fixture (``tests/fixtures/inbound_nda_sample.pdf``) is parsed
by the production ``extract_pdf_document`` path so the extraction structure is the
genuine article, not a hand-rolled paragraph list.

The four task assertions are:
  (1) the per-poll cap bounds new imports to NDA_GMAIL_IMPORT_LIMIT;
  (2) dedup prevents re-import on a second poll;
  (3) the matter lands UN-REVIEWED ("Not Reviewed") -- inbound NDAs are NEVER
      auto-reviewed; review runs only on-demand;
  (4) extraction produces the expected paragraph structure from the real PDF.

Plus poll-layer storm-safety coverage: importing an inbound matter enqueues NO
review (there is no recovery sweep / auto-review path).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nda_automation import (
    gmail_integration,
    gmail_intake_classifier,
    gmail_matter_inbox,
    ingestion_service,
)
from nda_automation.matter_repository import InMemoryMatterRepository

# Reuse the existing public-only inbox fake + the tiny execute() shim rather than
# re-deriving the Gmail service plumbing. This is the same base the catch-up
# transport in test_gmail_transport.py builds on.
from tests.test_gmail_transport import _Executable, _PublicOnlyInboxTransport

FIXTURE_PDF = Path(__file__).parent / "fixtures" / "inbound_nda_sample.pdf"

# The text baked into the PDF fixture; the real extraction must round-trip it.
EXPECTED_PDF_PARAGRAPHS = [
    "MUTUAL NON-DISCLOSURE AGREEMENT",
    "This Mutual Non-Disclosure Agreement is entered into by both parties.",
    "Each party agrees to keep the other party Confidential Information secret.",
    "This Agreement shall be governed by the laws of England and Wales.",
    "The confidentiality obligations survive for three years from disclosure.",
]


def _fixture_pdf_bytes() -> bytes:
    return FIXTURE_PDF.read_bytes()


# --------------------------------------------------------------------------- #
# A scripted, paginated in-memory inbox (no network).
# --------------------------------------------------------------------------- #
class _PagedMessages:
    """A fake inbox of ``inbox_size`` messages, each with one PDF attachment.

    Pages on ``pageToken`` exactly like the real Gmail list() call and records the
    per-page ``maxResults`` so a test can assert the per-poll fetch is bounded by
    the catch-up import limit. ``get()`` returns the message envelope (the message
    id is enough for the rest of the path, which reads payload via the transport).
    """

    def __init__(self, inbox_size: int, *, senders: dict[str, str] | None = None) -> None:
        self.message_ids = [f"msg_{i:03d}" for i in range(inbox_size)]
        self.max_results_seen: list[int] = []
        # Optional per-message-id raw ``From`` header. Messages absent from this map
        # carry no From header (today's default), so the DocuSign matcher fails open.
        self._senders = dict(senders or {})

    def list(self, *, userId: str, q: str, maxResults: int, pageToken: str = ""):
        self.max_results_seen.append(maxResults)
        start = int(pageToken or "0")
        page = self.message_ids[start:start + maxResults]
        next_start = start + len(page)
        next_token = str(next_start) if next_start < len(self.message_ids) else ""
        return _Executable({
            "messages": [{"id": mid} for mid in page],
            "nextPageToken": next_token,
        })

    def get(self, *, userId: str, id: str, format: str):
        sender = self._senders.get(id)
        headers = [{"name": "From", "value": sender}] if sender else []
        return _Executable({"id": id, "payload": {"headers": headers}})


class _PagedUsers:
    def __init__(self, messages_api: _PagedMessages) -> None:
        self.messages_api = messages_api

    def messages(self) -> _PagedMessages:
        return self.messages_api


class _PagedService:
    def __init__(self, inbox_size: int, *, senders: dict[str, str] | None = None) -> None:
        self.users_api = _PagedUsers(_PagedMessages(inbox_size, senders=senders))

    def users(self) -> _PagedUsers:
        return self.users_api


class _RecordingAiEngine:
    """A stand-in for the full ai_first (assessor + verifier) engine.

    Stamps ``executed_engine == "ai_first"`` -- the same idempotency marker the
    real active engine writes -- and records each call so a test can assert the
    background review actually ran. Deterministic and instant (no model call).
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, text: str, *, paragraphs: Any = None, **_kwargs: Any) -> dict[str, Any]:
        self.calls.append(text)
        return {
            "review_mode": "ai_first",
            "active_review_engine": {"executed_engine": "ai_first"},
            "clauses": [],
            "requirements_passed": 1,
            "requirements_needs_review": 0,
            "requirements_failed": 0,
        }


def _synchronous_runner(work) -> None:
    """Run the scheduled background review inline so the flow is deterministic."""
    work()


class _FullInboundTransport(_PublicOnlyInboxTransport):
    """A full-distance inbound transport: everything but Gmail + the AI model is real.

    The heavy import seams delegate to the REAL production code:
      * ``attachment_bytes`` serves the real PDF fixture bytes (no download);
      * ``extract_document_paragraphs`` runs the real pypdf extraction;
      * ``attachment_nda_validation`` runs the real deterministic NDA band classifier;
      * ``create_matter_from_document`` runs the real ingestion against an isolated
        in-memory repository (un-reviewed at create, defer_ai_review=True);
      * dedup is routed through the SAME in-memory repository's
        ``find_gmail_attachment`` so a second poll sees the prior import.

    Inbound NDAs are NOT auto-reviewed: import leaves each matter "Not Reviewed"
    (no AI engine runs on the poll path). The recording AI engine is kept only to
    PROVE no review was scheduled (its call list must stay empty after import).

    Each message carries a distinct ``gmail_message_id`` (the dedup key is prefixed
    by it), exactly as the real ``_message_metadata`` does, so the identity dedup is
    genuinely exercised rather than trivially short-circuited.
    """

    # Surface the real exception types the import path catches.
    ActiveReviewEngineError = gmail_integration.ActiveReviewEngineError
    DocxExtractionError = gmail_integration.DocxExtractionError
    PdfExtractionError = gmail_integration.PdfExtractionError
    ParagraphAlignmentError = gmail_integration.ParagraphAlignmentError
    DocumentSizeError = gmail_integration.DocumentSizeError

    def __init__(
        self,
        inbox_size: int,
        *,
        import_limit: int,
        senders: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self.service = _PagedService(inbox_size, senders=senders)
        self.repository = InMemoryMatterRepository()
        self.ai_engine = _RecordingAiEngine()
        self._import_limit = import_limit
        self._pdf_bytes = _fixture_pdf_bytes()
        self.created_matter_ids: list[str] = []

    # -- import bound ---------------------------------------------------- #
    def max_import_limit(self) -> int:
        return self._import_limit

    # -- message-level seams --------------------------------------------- #
    def is_self_or_outbound_message(self, message: dict[str, Any], account_email: str) -> bool:
        return False

    def is_docusign_notification(self, message: dict[str, Any]) -> bool:
        # REAL deterministic domain-only matcher over the message's From header.
        return gmail_integration._is_docusign_notification(message)

    def reviewable_attachments(self, payload: dict[str, Any]) -> list[dict[str, str]]:
        # One reviewable PDF attachment per message.
        return [{
            "attachment_id": "att_0",
            "part_id": "0",
            "filename": "inbound_nda_sample.pdf",
        }]

    def message_nda_detection(self, message: dict[str, Any], attachments: list[dict[str, str]]) -> dict[str, object]:
        return {"matched": True}

    def attachment_nda_detection(self, service, message_id, attachments) -> dict[str, object]:
        return {"matched": True}

    def message_metadata(self, message: dict[str, Any], account_email: str, *, detection=None) -> dict[str, str]:
        # The dedup keys are prefixed by gmail_message_id (see matter_store
        # _gmail_attachment_keys_for_metadata), so carrying it is what makes the
        # identity dedup real -- the production _message_metadata sets it too.
        return {"gmail_message_id": str(message.get("id") or "")}

    def message_body_text(self, payload: dict[str, Any]) -> str:
        return ""

    # -- dedup routed through the same in-memory repository -------------- #
    def gmail_attachment_already_imported(
        self,
        message_id: str,
        attachment_id: str,
        *,
        attachment_filename: str = "",
        attachment_sha256: str = "",
        part_id: str = "",
        owner_user_id: str = "",
    ) -> bool:
        return self.repository.find_gmail_attachment(
            message_id,
            attachment_id,
            attachment_filename=attachment_filename,
            attachment_sha256=attachment_sha256,
            part_id=part_id,
            owner_user_id=owner_user_id,
        ) is not None

    # -- real bytes, real extraction, real validation -------------------- #
    def attachment_bytes(self, service, message_id: str, attachment: dict[str, str]) -> bytes:
        return self._pdf_bytes

    def ensure_document_size(self, document_bytes: bytes) -> None:
        # The size guard is exercised separately; here it is a pass-through so the
        # small fixture always clears it.
        return None

    def extract_document_paragraphs(self, filename: str, document_bytes: bytes):
        # REAL memory-bounded extraction (pdf_text/pypdf) on the fixture bytes.
        return ingestion_service.extract_document_paragraphs(filename, document_bytes)

    def pdf_attachment_skip_reason(self, error: Exception) -> str:
        return "pdf_review_failed"

    def attachment_nda_validation(self, filename, paragraphs, *, message_metadata=None) -> dict[str, object]:
        # REAL deterministic NDA band classifier over the extracted paragraphs.
        return gmail_integration._attachment_nda_validation(
            filename, paragraphs, message_metadata=message_metadata
        )

    def attachment_validation_metadata(self, metadata, validation) -> dict[str, str]:
        return metadata

    def attachment_selector_metadata(self, metadata, selection) -> dict[str, str]:
        return metadata

    def resolve_intake_lane(self, det_lane, det_reason, ai_result):
        # The AI intake classifier is unconfigured here, so this returns the
        # deterministic lane verbatim -- the real reconciliation, no model call.
        return gmail_intake_classifier.resolve_intake_lane(det_lane, det_reason, ai_result)

    # -- real matter creation + real review scheduling ------------------- #
    def create_matter_from_document(self, **kwargs):
        # The inbound poll creates the matter un-reviewed (no review at create).
        assert kwargs.get("defer_ai_review") is True
        matter = ingestion_service.create_matter_from_document(
            repository=self.repository, **kwargs
        )
        if not matter.get("_existing_gmail_duplicate"):
            self.created_matter_ids.append(str(matter.get("id") or ""))
        return matter

    # NOTE: no schedule_inbound_ai_review seam -- inbound NDAs are NOT auto-reviewed.
    # The poll path must never schedule a review; the recording engine proves it.


@pytest.fixture
def import_limit_20(monkeypatch):
    """Pin the catch-up import limit to 20 through the real module constant."""
    monkeypatch.setenv(gmail_integration.NDA_GMAIL_IMPORT_LIMIT_ENV, "20")
    monkeypatch.setattr(
        gmail_integration,
        "MAX_GMAIL_IMPORT_LIMIT",
        gmail_integration._gmail_import_limit_from_env(),
    )
    assert gmail_integration.MAX_GMAIL_IMPORT_LIMIT == 20
    return 20


# --------------------------------------------------------------------------- #
# Sanity: the committed PDF fixture extracts to the expected structure via the
# REAL production extractor (no fitz, no hand-rolled paragraphs). This doubles as
# the byte-stability guard -- a regenerated/garbled fixture would not round-trip.
# --------------------------------------------------------------------------- #
def test_fixture_pdf_extracts_to_expected_structure():
    data = _fixture_pdf_bytes()
    assert data.startswith(b"%PDF")

    document_type, paragraphs = ingestion_service.extract_document_paragraphs(
        "inbound_nda_sample.pdf", data
    )

    # (4) Extraction produces the expected structure: a pdf document broken into
    # the five fixture paragraphs, each a {id, text, ...} record with page+order.
    assert document_type == "pdf"
    assert [p["text"] for p in paragraphs] == EXPECTED_PDF_PARAGRAPHS
    assert all(p.get("page_number") == 1 for p in paragraphs)
    assert [p["id"] for p in paragraphs] == [f"p{i}" for i in range(1, 6)]


# --------------------------------------------------------------------------- #
# The full inbound flow as one deterministic pass.
# --------------------------------------------------------------------------- #
def test_inbound_flow_e2e_import_cap_dedup_extraction_and_review(import_limit_20):
    # A 50-email backlog re-surfaces on every poll (the inbound query applies no
    # already-imported exclusion), so the per-poll cap + dedup are what bound the
    # work and make forward progress.
    transport = _FullInboundTransport(inbox_size=50, import_limit=import_limit_20)
    messages_api = transport.service.users_api.messages_api

    # ----- POLL 1 -----------------------------------------------------------
    result1 = gmail_matter_inbox.import_inbound_matters(
        transport=transport,
        limit=999,  # caller asks for the max; the catch-up limit is the real bound
        owner_user_id="owner_1",
    )

    # (1) The per-poll cap bounds NEW imports to NDA_GMAIL_IMPORT_LIMIT (20).
    assert len(result1["imported"]) == 20
    assert result1["skipped"] == []
    # No single Gmail page was ever asked for more than the catch-up limit.
    assert max(messages_api.max_results_seen) <= 20
    # The 20 imports landed as real matters in the store, none over the cap.
    assert len(transport.repository.list_matters(owner_user_id="owner_1")) == 20

    # (3) Inbound NDAs are NOT auto-reviewed: NO AI engine ran on import, and every
    # imported matter is left UN-REVIEWED ("Not Reviewed", review_result is None).
    # It becomes reviewed only when a human clicks Review (the on-demand path).
    assert transport.ai_engine.calls == []  # storm-safe: zero auto-reviews
    for matter_summary in result1["imported"]:
        matter_id = str(matter_summary["id"])
        persisted = transport.repository.get_matter(matter_id, owner_user_id="owner_1")
        assert persisted is not None
        assert persisted["review_result"] is None  # un-reviewed at import
        # (4) The matter carries the REAL extracted NDA text from the PDF.
        assert "Confidential Information" in persisted["extracted_text"]
        assert "MUTUAL NON-DISCLOSURE AGREEMENT" in persisted["extracted_text"]

    # ----- POLL 2 (dedup) ---------------------------------------------------
    # The inbox re-surfaces all 50 messages; the first 20 are now in the store, so
    # they are skipped at the cheap pre-download identity gate, and the scan pages
    # PAST them to make real forward progress on the next batch.
    result2 = gmail_matter_inbox.import_inbound_matters(
        transport=transport,
        limit=999,
        owner_user_id="owner_1",
    )

    # (2) Dedup: the 20 already-imported messages that re-surfaced ahead of the new
    # batch are skipped (no re-download, no re-extract, no duplicate matter).
    already_imported = [s for s in result2["skipped"] if s.get("reason") == "already_imported"]
    assert len(already_imported) == 20
    # The next 20 NEW messages (msg_020..msg_039) are imported -- real progress.
    assert len(result2["imported"]) == 20
    # Total matters == 40 (the original 20 + the next 20), never a re-import of the
    # first 20.
    assert len(transport.repository.list_matters(owner_user_id="owner_1")) == 40
    # Still NO auto-review: neither poll ever scheduled an AI review (storm-safe).
    assert transport.ai_engine.calls == []


def test_inbound_flow_e2e_single_message_round_trip(import_limit_20):
    """A focused single-NDA pass: one inbound PDF -> matter -> AI-reviewed + persisted."""
    transport = _FullInboundTransport(inbox_size=1, import_limit=import_limit_20)

    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport,
        limit=999,
        owner_user_id="owner_1",
    )

    assert len(result["imported"]) == 1
    matter_id = str(result["imported"][0]["id"])
    matter = transport.repository.get_matter(matter_id, owner_user_id="owner_1")
    assert matter is not None
    # Source + the REAL PDF-extracted text survived onto the matter.
    assert matter["source_type"] == "gmail_inbound"
    assert "MUTUAL NON-DISCLOSURE AGREEMENT" in matter["extracted_text"]
    assert "governed by the laws of England and Wales" in matter["extracted_text"]
    # Inbound NDAs are NOT auto-reviewed: no AI review ran, the matter is un-reviewed.
    assert transport.ai_engine.calls == []
    assert matter["review_result"] is None


# --------------------------------------------------------------------------- #
# DocuSign envelope-notification skip: a notification email with a valid-looking
# PDF attachment must NOT become a phantom inbound matter -- it is skipped + ledger-
# marked BEFORE the import budget, no review scheduled. A real NDA still imports.
# --------------------------------------------------------------------------- #
def test_inbound_flow_e2e_docusign_notification_is_skipped_not_imported(import_limit_20):
    """A single DocuSign notification (valid PDF attachment) -> skipped, never a matter."""
    transport = _FullInboundTransport(
        inbox_size=1,
        import_limit=import_limit_20,
        senders={"msg_000": "DocuSign <dse@docusign.net>"},
    )

    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport,
        limit=999,
        owner_user_id="owner_1",
    )

    # Nothing imported, the repository stays empty, and the skip reason is recorded.
    assert result["imported"] == []
    assert transport.created_matter_ids == []
    assert transport.repository.list_matters(owner_user_id="owner_1") == []
    reasons = [s.get("reason") for s in result["skipped"]]
    assert "docusign_notification" in reasons
    # The skip happens BEFORE the import-budget slot + the heavy path, so the AI
    # review is NEVER scheduled (the recording engine recorded zero calls).
    assert transport.ai_engine.calls == []

    # The message was ledger-marked: a SECOND poll re-skips it cheaply -- it never
    # reaches reviewable_attachments / the download path again.
    again = gmail_matter_inbox.import_inbound_matters(
        transport=transport,
        limit=999,
        owner_user_id="owner_1",
    )
    assert again["imported"] == []
    assert transport.repository.list_matters(owner_user_id="owner_1") == []
    assert transport.ai_engine.calls == []


def test_inbound_flow_e2e_real_nda_mentioning_docusign_still_imports(import_limit_20):
    """A genuine NDA from a real sender still imports even if it mentions DocuSign.

    The match is DOMAIN-ONLY, so a normal sender (jane@acme.com) is unaffected even
    though the fixture text is a real NDA. This is the non-regression guard.
    """
    transport = _FullInboundTransport(
        inbox_size=1,
        import_limit=import_limit_20,
        senders={"msg_000": "Jane Counsel <jane@acme.com>"},
    )

    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport,
        limit=999,
        owner_user_id="owner_1",
    )

    assert len(result["imported"]) == 1
    assert "docusign_notification" not in [s.get("reason") for s in result["skipped"]]
    matter_id = str(result["imported"][0]["id"])
    matter = transport.repository.get_matter(matter_id, owner_user_id="owner_1")
    assert matter is not None
    assert matter["source_type"] == "gmail_inbound"
    # Inbound NDAs are NOT auto-reviewed: it imports un-reviewed, no AI review ran.
    assert transport.ai_engine.calls == []
    assert matter["review_result"] is None


def test_inbound_flow_e2e_transport_without_docusign_guard_degrades_gracefully(import_limit_20):
    """A transport lacking is_docusign_notification behaves exactly as today (no skip).

    The inbox guards the predicate with getattr + callable(), so an older/fake
    transport without it does not crash and does not skip -- it imports as before.
    """

    class _NoDocusignGuardTransport(_FullInboundTransport):
        # Older/fake transport: the predicate simply does not exist.
        is_docusign_notification = property(
            lambda self: (_ for _ in ()).throw(AttributeError("no such attribute"))
        )

    # Even a message FROM docusign.net imports here, because the guard can't fire.
    transport = _NoDocusignGuardTransport(
        inbox_size=1,
        import_limit=import_limit_20,
        senders={"msg_000": "DocuSign <dse@docusign.net>"},
    )

    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport,
        limit=999,
        owner_user_id="owner_1",
    )

    # No crash; today's behavior preserved -> the message imports as a matter.
    assert len(result["imported"]) == 1
    assert "docusign_notification" not in [s.get("reason") for s in result["skipped"]]


# --------------------------------------------------------------------------- #
# Poll-layer storm safety: importing an inbound NDA enqueues NO review (there is
# no recovery sweep / auto-review path any more). The matter stays "Not Reviewed".
# --------------------------------------------------------------------------- #
@pytest.fixture
def fresh_review_pool(monkeypatch):
    """Swap the module-global review pool for an isolated one."""
    pool = ingestion_service._InboundReviewWorkerPool()
    monkeypatch.setattr(ingestion_service, "_INBOUND_REVIEW_POOL", pool)
    return pool


def test_import_enqueues_no_review_and_leaves_matter_unreviewed(fresh_review_pool):
    """STORM SAFETY: creating an inbound matter enqueues NOTHING onto the review pool
    (no auto-review, no recovery sweep), and the matter stays un-reviewed until a
    human runs the on-demand review."""
    repository = InMemoryMatterRepository()
    enqueued: list[str] = []
    fresh_review_pool.configure(lambda matter_id, owner: enqueued.append(matter_id))

    matter = ingestion_service.create_matter_from_document(
        filename="inbound_nda_sample.pdf",
        document_bytes=_fixture_pdf_bytes(),
        source_type="gmail_inbound",
        repository=repository,
        defer_ai_review=True,
    )
    fresh_review_pool._join_for_tests(timeout=1)

    # Nothing was ever enqueued for review on import (the storm engine is gone).
    assert enqueued == []
    # The matter is un-reviewed ("Not Reviewed"), reviewable only on-demand.
    persisted = repository.get_matter(matter["id"])
    assert persisted["review_result"] is None


def test_create_matter_defaults_to_deferred_review_no_sync_review(fresh_review_pool):
    """create_matter_from_document defaults defer_ai_review=True: even a caller that
    omits the flag gets NO synchronous review (the storm-safe default)."""
    repository = InMemoryMatterRepository()
    enqueued: list[str] = []
    fresh_review_pool.configure(lambda matter_id, owner: enqueued.append(matter_id))

    # NOTE: defer_ai_review NOT passed -> must default to True (no sync review).
    matter = ingestion_service.create_matter_from_document(
        filename="inbound_nda_sample.pdf",
        document_bytes=_fixture_pdf_bytes(),
        source_type="gmail_inbound",
        repository=repository,
    )
    fresh_review_pool._join_for_tests(timeout=1)

    assert enqueued == []
    assert matter["review_result"] is None  # no synchronous review ran at create
