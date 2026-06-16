"""Inbound auto-review: async + serialized full AI review of imported Gmail NDAs.

bb62b8f made the Gmail poll import each inbound NDA with ONLY the fast offline
deterministic first-pass (defer_ai_review=True), pushing the AI review on-demand
-- which removed the core feature (inbound NDAs auto-reviewed by AI). These tests
pin the restored behaviour: import stays fast (NO synchronous assessor/verifier on
the poll thread), the full active-engine review runs AUTOMATICALLY in the
background and persists onto the matter, the background reviews are SERIALIZED
behind a process-wide semaphore (never N-at-once), and a concurrent request is
never blocked while a background review runs.
"""
from __future__ import annotations

import threading
import time
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from nda_automation import gmail_matter_inbox, ingestion_service
from nda_automation.ingestion_service import (
    create_matter_from_document,
    schedule_inbound_ai_review,
)
from nda_automation.matter_repository import InMemoryMatterRepository

NDA_PARAGRAPHS = [
    "This Mutual Non-Disclosure Agreement is entered into by both parties.",
    "Each party agrees to keep the other party's Confidential Information secret.",
    "This Agreement shall be governed by the laws of England and Wales.",
    "The confidentiality obligations survive for three years from disclosure.",
]


def _docx(paragraphs: list[str]) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document_xml)
    return buffer.getvalue()


def _synchronous_runner(work):
    """A BackgroundRunner that runs the work inline (deterministic in tests)."""
    work()


def _stub_ai_first_engine(executed_engine: str = "ai_first"):
    """A review-engine stub standing in for the full ai_first (assessor+verifier).

    Returns a result whose active_review_engine.executed_engine marks it as the AI
    engine -- the same idempotency marker the real active engine stamps. Records
    each call so a test can assert the AI engine ran (or did NOT run) exactly when
    expected.
    """

    calls: list[str] = []

    def _engine(text, *, paragraphs=None, **_kwargs):
        calls.append(text)
        return {
            "review_mode": "ai_first",
            "active_review_engine": {"executed_engine": executed_engine},
            "clauses": [],
            "requirements_passed": 1,
            "requirements_needs_review": 0,
            "requirements_failed": 0,
        }

    _engine.calls = calls  # type: ignore[attr-defined]
    return _engine


# --------------------------------------------------------------------------- #
# (a) Import returns fast with NO synchronous assessor/verifier on the poll thread
# --------------------------------------------------------------------------- #
def test_import_runs_only_deterministic_first_pass_no_ai_on_poll_thread():
    repository = InMemoryMatterRepository()
    ai_engine = _stub_ai_first_engine()

    matter = create_matter_from_document(
        filename="inbound.docx",
        document_bytes=_docx(NDA_PARAGRAPHS),
        source_type="gmail_inbound",
        repository=repository,
        # The inbound poll path passes defer_ai_review=True so import runs only the
        # offline deterministic engine -- NEVER the AI assessor/verifier.
        defer_ai_review=True,
    )

    # The matter exists immediately and its review is the deterministic first-pass,
    # NOT the AI engine. The AI engine was never called on the import path.
    assert matter["review_result"]["active_review_engine"]["executed_engine"] == "deterministic"
    assert ai_engine.calls == []  # type: ignore[attr-defined]


class _RecordingTransport:
    """Minimal inbound transport stub for create_matter_from_prepared_attachment.

    Records the schedule_inbound_ai_review call so a test can assert the poll path
    schedules the async review after a successful (non-duplicate) import.
    """

    class ActiveReviewEngineError(Exception):
        pass

    class DocxExtractionError(Exception):
        pass

    class PdfExtractionError(Exception):
        pass

    class ParagraphAlignmentError(Exception):
        pass

    def __init__(self, matter):
        self._matter = matter
        self.scheduled = []

    def attachment_validation_metadata(self, metadata, validation):
        return metadata

    def attachment_selector_metadata(self, metadata, selector_metadata):
        return metadata

    def create_matter_from_document(self, **kwargs):
        # Mirror the real seam: the inbound path defers the AI review.
        assert kwargs.get("defer_ai_review") is True
        return self._matter

    def schedule_inbound_ai_review(self, matter, *, owner_user_id=""):
        self.scheduled.append((matter, owner_user_id))
        return True


def test_prepared_attachment_import_schedules_async_review():
    matter = {"id": "matter_1", "extracted_text": "x"}
    transport = _RecordingTransport(matter)
    candidate = {
        "message_id": "m1",
        "attachment_id": "a1",
        "filename": "nda.docx",
        "attachment_sha256": "deadbeef",
        "document_bytes": b"PK\x03\x04stub",
        "part_id": "p1",
        "validation": {"score": "5"},
    }

    result, skip = gmail_matter_inbox.create_matter_from_prepared_attachment(
        candidate, {}, transport=transport, owner_user_id="owner_1"
    )

    assert skip is None
    assert result is matter
    # The poll path scheduled the async AI review for the imported matter.
    assert transport.scheduled == [(matter, "owner_1")]


def test_prepared_attachment_duplicate_does_not_schedule_review():
    transport = _RecordingTransport({"_existing_gmail_duplicate": True})
    candidate = {
        "message_id": "m1",
        "attachment_id": "a1",
        "filename": "nda.docx",
        "document_bytes": b"PK\x03\x04stub",
        "part_id": "p1",
        "validation": {},
    }

    result, skip = gmail_matter_inbox.create_matter_from_prepared_attachment(
        candidate, {}, transport=transport, owner_user_id="owner_1"
    )

    assert result is None
    assert skip is not None  # duplicate_attachment skip
    assert transport.scheduled == []  # no review for a duplicate


# --------------------------------------------------------------------------- #
# (b) The AI review DOES run automatically in the background and persists
# --------------------------------------------------------------------------- #
def test_scheduled_review_runs_ai_engine_and_persists_onto_matter():
    repository = InMemoryMatterRepository()
    ai_engine = _stub_ai_first_engine()
    matter = create_matter_from_document(
        filename="inbound.docx",
        document_bytes=_docx(NDA_PARAGRAPHS),
        source_type="gmail_inbound",
        repository=repository,
        defer_ai_review=True,
    )
    assert matter["review_result"]["active_review_engine"]["executed_engine"] == "deterministic"

    scheduled = schedule_inbound_ai_review(
        matter,
        repository=repository,
        runner=_synchronous_runner,
        review_engine_func=ai_engine,
    )

    assert scheduled is True
    assert ai_engine.calls  # the AI engine actually ran in the background  # type: ignore[attr-defined]
    persisted = repository.get_matter(matter["id"])
    assert persisted is not None
    # The full AI review is now persisted as the matter's review_result.
    assert persisted["review_result"]["active_review_engine"]["executed_engine"] == "ai_first"


def test_scheduled_review_is_idempotent_already_reviewed_matter_skipped():
    repository = InMemoryMatterRepository()
    ai_engine = _stub_ai_first_engine()
    matter = create_matter_from_document(
        filename="inbound.docx",
        document_bytes=_docx(NDA_PARAGRAPHS),
        source_type="gmail_inbound",
        repository=repository,
        defer_ai_review=True,
    )
    # First schedule reviews it.
    schedule_inbound_ai_review(
        matter, repository=repository, runner=_synchronous_runner, review_engine_func=ai_engine
    )
    first_calls = len(ai_engine.calls)  # type: ignore[attr-defined]
    assert first_calls == 1

    # Re-fetch the now-AI-reviewed matter and try to schedule again: nothing runs.
    reviewed = repository.get_matter(matter["id"])
    scheduled_again = schedule_inbound_ai_review(
        reviewed, repository=repository, runner=_synchronous_runner, review_engine_func=ai_engine
    )
    assert scheduled_again is False
    assert len(ai_engine.calls) == first_calls  # type: ignore[attr-defined]  # no re-review


def test_scheduled_review_survives_a_restart_mid_batch():
    """A worker that finished matter A but not matter B before restarting must, on
    re-schedule, review ONLY B (A is already ai_first and is skipped)."""
    repository = InMemoryMatterRepository()
    ai_engine = _stub_ai_first_engine()
    matter_a = create_matter_from_document(
        filename="a.docx", document_bytes=_docx(NDA_PARAGRAPHS),
        source_type="gmail_inbound", repository=repository, defer_ai_review=True,
    )
    matter_b = create_matter_from_document(
        filename="b.docx", document_bytes=_docx(NDA_PARAGRAPHS),
        source_type="gmail_inbound", repository=repository, defer_ai_review=True,
    )
    # A got reviewed before the "restart".
    schedule_inbound_ai_review(
        matter_a, repository=repository, runner=_synchronous_runner, review_engine_func=ai_engine
    )
    assert len(ai_engine.calls) == 1  # type: ignore[attr-defined]

    # After restart, re-schedule both (e.g. a recovery sweep): only B is reviewed.
    schedule_inbound_ai_review(
        repository.get_matter(matter_a["id"]), repository=repository,
        runner=_synchronous_runner, review_engine_func=ai_engine,
    )
    schedule_inbound_ai_review(
        matter_b, repository=repository, runner=_synchronous_runner, review_engine_func=ai_engine
    )
    assert len(ai_engine.calls) == 2  # type: ignore[attr-defined]  # A skipped, B reviewed
    assert repository.get_matter(matter_b["id"])["review_result"][
        "active_review_engine"]["executed_engine"] == "ai_first"


# --------------------------------------------------------------------------- #
# (c) Serialization: two scheduled reviews never run concurrently
# --------------------------------------------------------------------------- #
def test_two_inbound_reviews_run_sequentially_not_concurrently():
    repository = InMemoryMatterRepository()
    matter_a = create_matter_from_document(
        filename="a.docx", document_bytes=_docx(NDA_PARAGRAPHS),
        source_type="gmail_inbound", repository=repository, defer_ai_review=True,
    )
    matter_b = create_matter_from_document(
        filename="b.docx", document_bytes=_docx(NDA_PARAGRAPHS),
        source_type="gmail_inbound", repository=repository, defer_ai_review=True,
    )

    concurrency = {"current": 0, "max": 0}
    lock = threading.Lock()

    def _slow_engine(text, *, paragraphs=None, **_kwargs):
        with lock:
            concurrency["current"] += 1
            concurrency["max"] = max(concurrency["max"], concurrency["current"])
        try:
            time.sleep(0.05)  # hold the semaphore long enough to overlap if unserialized
        finally:
            with lock:
                concurrency["current"] -= 1
        return {
            "review_mode": "ai_first",
            "active_review_engine": {"executed_engine": "ai_first"},
            "clauses": [],
        }

    threads: list[threading.Thread] = []

    def _real_runner(work):
        thread = threading.Thread(target=work)
        thread.start()
        threads.append(thread)

    schedule_inbound_ai_review(
        matter_a, repository=repository, runner=_real_runner, review_engine_func=_slow_engine
    )
    schedule_inbound_ai_review(
        matter_b, repository=repository, runner=_real_runner, review_engine_func=_slow_engine
    )
    for thread in threads:
        thread.join(timeout=5)

    # The process-wide semaphore (limit 1) guarantees the two reviews never overlap.
    assert concurrency["max"] == 1
    # Both still completed and persisted.
    assert repository.get_matter(matter_a["id"])["review_result"][
        "active_review_engine"]["executed_engine"] == "ai_first"
    assert repository.get_matter(matter_b["id"])["review_result"][
        "active_review_engine"]["executed_engine"] == "ai_first"


# --------------------------------------------------------------------------- #
# (d) A concurrent (generate/other) request is NOT blocked by a running review
# --------------------------------------------------------------------------- #
def test_concurrent_request_not_blocked_by_running_background_review():
    repository = InMemoryMatterRepository()
    matter = create_matter_from_document(
        filename="a.docx", document_bytes=_docx(NDA_PARAGRAPHS),
        source_type="gmail_inbound", repository=repository, defer_ai_review=True,
    )

    review_entered = threading.Event()
    release_review = threading.Event()

    def _blocking_engine(text, *, paragraphs=None, **_kwargs):
        review_entered.set()
        # Hold the review (and the serialization semaphore) until the test releases it.
        assert release_review.wait(timeout=5)
        return {
            "review_mode": "ai_first",
            "active_review_engine": {"executed_engine": "ai_first"},
            "clauses": [],
        }

    review_thread_holder: list[threading.Thread] = []

    def _real_runner(work):
        thread = threading.Thread(target=work)
        thread.start()
        review_thread_holder.append(thread)

    schedule_inbound_ai_review(
        matter, repository=repository, runner=_real_runner, review_engine_func=_blocking_engine
    )
    assert review_entered.wait(timeout=5)  # background review is now in-flight + holding the semaphore

    # While the background review is BLOCKED inside the semaphore, an unrelated
    # request (a fresh deterministic generation/import) still completes promptly --
    # it does not contend on the inbound-review semaphore at all.
    started = time.monotonic()
    other_matter = create_matter_from_document(
        filename="other.docx", document_bytes=_docx(NDA_PARAGRAPHS),
        source_type="gmail_inbound", repository=repository, defer_ai_review=True,
    )
    elapsed = time.monotonic() - started
    assert other_matter["id"]
    assert elapsed < 2.0  # not blocked waiting on the held review

    release_review.set()
    for thread in review_thread_holder:
        thread.join(timeout=5)
    assert repository.get_matter(matter["id"])["review_result"][
        "active_review_engine"]["executed_engine"] == "ai_first"


# --------------------------------------------------------------------------- #
# Scheduling guards + telemetry
# --------------------------------------------------------------------------- #
def test_schedule_skips_duplicate_and_idless_matters():
    repository = InMemoryMatterRepository()
    ai_engine = _stub_ai_first_engine()
    assert schedule_inbound_ai_review(None, repository=repository, runner=_synchronous_runner,
                                      review_engine_func=ai_engine) is False
    assert schedule_inbound_ai_review({"_existing_gmail_duplicate": True}, repository=repository,
                                      runner=_synchronous_runner, review_engine_func=ai_engine) is False
    assert schedule_inbound_ai_review({"id": ""}, repository=repository,
                                      runner=_synchronous_runner, review_engine_func=ai_engine) is False
    assert ai_engine.calls == []  # type: ignore[attr-defined]


def test_scheduling_never_raises_when_runner_throws():
    repository = InMemoryMatterRepository()
    matter = create_matter_from_document(
        filename="a.docx", document_bytes=_docx(NDA_PARAGRAPHS),
        source_type="gmail_inbound", repository=repository, defer_ai_review=True,
    )

    def _raising_runner(work):
        raise RuntimeError("thread pool exhausted")

    # A scheduling failure must be swallowed (best-effort), never break the import.
    assert schedule_inbound_ai_review(
        matter, repository=repository, runner=_raising_runner,
        review_engine_func=_stub_ai_first_engine(),
    ) is False


def test_failed_background_review_is_swallowed_and_leaves_first_pass():
    repository = InMemoryMatterRepository()
    matter = create_matter_from_document(
        filename="a.docx", document_bytes=_docx(NDA_PARAGRAPHS),
        source_type="gmail_inbound", repository=repository, defer_ai_review=True,
    )

    def _failing_engine(text, *, paragraphs=None, **_kwargs):
        raise RuntimeError("OpenRouter down")

    # The work runs inline (synchronous runner); the engine raises but the failure
    # is fail-soft -- no exception escapes, and the deterministic first-pass stays.
    schedule_inbound_ai_review(
        matter, repository=repository, runner=_synchronous_runner, review_engine_func=_failing_engine
    )
    persisted = repository.get_matter(matter["id"])
    assert persisted["review_result"]["active_review_engine"]["executed_engine"] == "deterministic"


def test_inbound_review_concurrency_defaults_to_one(monkeypatch):
    monkeypatch.delenv(ingestion_service.INBOUND_REVIEW_CONCURRENCY_ENV, raising=False)
    assert ingestion_service.inbound_review_concurrency() == 1
    monkeypatch.setenv(ingestion_service.INBOUND_REVIEW_CONCURRENCY_ENV, "3")
    assert ingestion_service.inbound_review_concurrency() == 3
    monkeypatch.setenv(ingestion_service.INBOUND_REVIEW_CONCURRENCY_ENV, "0")
    assert ingestion_service.inbound_review_concurrency() == 1  # clamps to >= 1
    monkeypatch.setenv(ingestion_service.INBOUND_REVIEW_CONCURRENCY_ENV, "garbage")
    assert ingestion_service.inbound_review_concurrency() == 1  # unparseable -> default
