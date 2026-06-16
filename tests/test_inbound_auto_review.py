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


def test_schedule_increments_scheduled_counter_and_logs(caplog):
    """A successful schedule increments inbound_ai_review_scheduled and logs it.

    Reuses the existing inbound-review counters (REFINEMENT G): the new
    inbound_ai_review_scheduled is the intake signal that pairs with the existing
    completed/failed/queue_full/schedule_failed outcome counters.
    """
    import logging

    from nda_automation import telemetry

    telemetry.reset()
    repository = InMemoryMatterRepository()
    ai_engine = _stub_ai_first_engine()
    matter = create_matter_from_document(
        filename="inbound.docx",
        document_bytes=_docx(NDA_PARAGRAPHS),
        source_type="gmail_inbound",
        repository=repository,
        defer_ai_review=True,
    )

    before = telemetry.snapshot()["counters"].get("inbound_ai_review_scheduled", 0)
    with caplog.at_level(logging.INFO, logger="nda_automation.ingestion_service"):
        # owner left empty to match the matter created above (no owner tag), so the
        # background body's owner-scoped re-fetch resolves and the review runs.
        scheduled = schedule_inbound_ai_review(
            matter,
            repository=repository,
            runner=_synchronous_runner,
            review_engine_func=ai_engine,
        )
    assert scheduled is True

    counters = telemetry.snapshot()["counters"]
    assert counters.get("inbound_ai_review_scheduled", 0) == before + 1
    # The scheduling log fired for the matter, and the body's "Running" log fired too.
    assert any(
        "Scheduling inbound AI review for matter" in r.message and matter["id"] in r.message
        for r in caplog.records
    )
    assert any("Running inbound AI review for matter" in r.message for r in caplog.records)


def test_schedule_skips_and_does_not_count_scheduled_for_duplicate():
    """A gmail-duplicate / already-reviewed matter is rejected BEFORE the counter.

    The scheduled counter must reflect real demand, so an early-return guard
    (duplicate, no id, already ai_first) must NOT bump it.
    """
    from nda_automation import telemetry

    telemetry.reset()
    repository = InMemoryMatterRepository()
    ai_engine = _stub_ai_first_engine()

    # A gmail duplicate sentinel is rejected by the guard before any counting.
    scheduled = schedule_inbound_ai_review(
        {"id": "m1", "_existing_gmail_duplicate": True},
        repository=repository,
        runner=_synchronous_runner,
        review_engine_func=ai_engine,
    )
    assert scheduled is False
    assert telemetry.snapshot()["counters"].get("inbound_ai_review_scheduled", 0) == 0


def test_already_reviewed_matter_increments_skip_counter_and_logs(caplog):
    """When the review body finds the matter already ai_first, it logs the skip and
    increments inbound_ai_review_skipped_already_reviewed (the existing counter)."""
    import logging

    from nda_automation import ingestion_service, telemetry
    from nda_automation.matter_repository import InMemoryMatterRepository as _Repo

    telemetry.reset()
    repository = _Repo()
    ai_engine = _stub_ai_first_engine()
    matter = create_matter_from_document(
        filename="inbound.docx",
        document_bytes=_docx(NDA_PARAGRAPHS),
        source_type="gmail_inbound",
        repository=repository,
        defer_ai_review=True,
    )
    # Review it once so it is now ai_first.
    schedule_inbound_ai_review(
        matter, repository=repository, runner=_synchronous_runner, review_engine_func=ai_engine
    )
    before = telemetry.snapshot()["counters"].get("inbound_ai_review_skipped_already_reviewed", 0)

    # Now drive the review body DIRECTLY on the already-reviewed matter (models a
    # duplicate enqueue that slipped past the schedule-time guard): it must skip.
    # owner left empty to match the matter created above (no owner tag).
    with caplog.at_level(logging.INFO, logger="nda_automation.ingestion_service"):
        ingestion_service._perform_inbound_ai_review(
            matter["id"],
            repository=repository,
            owner_user_id="",
            review_engine_func=ai_engine,
        )

    counters = telemetry.snapshot()["counters"]
    assert counters.get("inbound_ai_review_skipped_already_reviewed", 0) == before + 1
    assert any(
        "already ai_first reviewed" in r.message and matter["id"] in r.message
        for r in caplog.records
    )
    # The engine was NOT called again for the skip.
    assert len(ai_engine.calls) == 1  # type: ignore[attr-defined]


def _failing_engine():
    """A review engine that ALWAYS raises -- a poison pill (no deterministic
    fallback). Records each call so a test can count how many times it was tried."""

    calls: list[str] = []

    def _engine(text, *, paragraphs=None, **_kwargs):
        calls.append(text)
        raise RuntimeError("permanent review failure")  # caught by the body's except

    _engine.calls = calls  # type: ignore[attr-defined]
    return _engine


def test_review_failure_increments_per_matter_failure_count():
    from nda_automation import ingestion_service

    repository = InMemoryMatterRepository()
    failing = _failing_engine()
    matter = create_matter_from_document(
        filename="inbound.docx",
        document_bytes=_docx(NDA_PARAGRAPHS),
        source_type="gmail_inbound",
        repository=repository,
        defer_ai_review=True,
    )
    # Drive the body directly with a failing engine: the failure is recorded on the
    # matter (poison-pill guard), the matter is NOT stamped ai_first.
    ingestion_service._perform_inbound_ai_review(
        matter["id"], repository=repository, owner_user_id="", review_engine_func=failing,
    )
    after = repository.get_matter(matter["id"])
    assert after["inbound_review_failures"] == 1
    assert "inbound_review_failed_at" in after
    # Still deterministic-only (the review never succeeded).
    assert after["review_result"]["active_review_engine"]["executed_engine"] == "deterministic"


def test_recovery_sweep_gives_up_on_poison_pill_after_cap(monkeypatch):
    """A matter that has failed review >= the cap is NOT re-enqueued by the sweep.

    This is the verifier-storm fix: without it, recover_unreviewed_inbound_matters
    re-enqueues a permanently-failing review every poll, forever.
    """
    from nda_automation import ingestion_service, telemetry

    monkeypatch.delenv(ingestion_service.INBOUND_AI_REVIEW_ENABLED_ENV, raising=False)
    monkeypatch.setenv(ingestion_service.INBOUND_REVIEW_MAX_FAILURES_ENV, "3")
    telemetry.reset()

    # Isolate the worker pool so enqueue() does not actually run reviews.
    pool = ingestion_service._InboundReviewWorkerPool()
    enqueued: list[str] = []
    pool.configure(lambda matter_id, owner: enqueued.append(matter_id))
    monkeypatch.setattr(ingestion_service, "_INBOUND_REVIEW_POOL", pool)

    repository = InMemoryMatterRepository()
    matter = create_matter_from_document(
        filename="inbound.docx",
        document_bytes=_docx(NDA_PARAGRAPHS),
        source_type="gmail_inbound",
        repository=repository,
        defer_ai_review=True,
    )
    matter_id = matter["id"]

    # Below the cap (2 failures): the sweep STILL re-enqueues it (transient retries).
    repository.update_matter_fields(matter_id, {"inbound_review_failures": 2})
    n = ingestion_service.recover_unreviewed_inbound_matters(repository=repository)
    assert n == 1 and enqueued == [matter_id]

    # At the cap (3 failures): the sweep GIVES UP -- not re-enqueued -- and bumps the
    # give-up counter.
    enqueued.clear()
    repository.update_matter_fields(matter_id, {"inbound_review_failures": 3})
    n2 = ingestion_service.recover_unreviewed_inbound_matters(repository=repository)
    assert n2 == 0
    assert enqueued == []  # poison pill stopped looping
    assert telemetry.snapshot()["counters"].get("inbound_ai_review_gave_up", 0) == 1


def test_transient_failures_retry_up_to_cap_then_succeed():
    """A matter that fails twice (transient) then succeeds is still reviewed -- the
    cap only stops a matter that keeps failing, never a recoverable one."""
    from nda_automation import ingestion_service

    repository = InMemoryMatterRepository()
    matter = create_matter_from_document(
        filename="inbound.docx",
        document_bytes=_docx(NDA_PARAGRAPHS),
        source_type="gmail_inbound",
        repository=repository,
        defer_ai_review=True,
    )
    matter_id = matter["id"]
    failing = _failing_engine()
    succeeding = _stub_ai_first_engine()

    # Two transient failures (below cap=3): recorded, matter still deterministic.
    ingestion_service._perform_inbound_ai_review(
        matter_id, repository=repository, owner_user_id="", review_engine_func=failing,
    )
    ingestion_service._perform_inbound_ai_review(
        matter_id, repository=repository, owner_user_id="", review_engine_func=failing,
    )
    assert repository.get_matter(matter_id)["inbound_review_failures"] == 2
    assert ingestion_service.inbound_review_max_failures() == 3  # default; still under cap

    # The matter is still UNDER the cap, so the sweep would retry it -- and a now-
    # healthy engine reviews it successfully.
    ingestion_service._perform_inbound_ai_review(
        matter_id, repository=repository, owner_user_id="", review_engine_func=succeeding,
    )
    reviewed = repository.get_matter(matter_id)
    assert reviewed["review_result"]["active_review_engine"]["executed_engine"] == "ai_first"


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


# --------------------------------------------------------------------------- #
# Hardening: a fresh, isolated worker pool per test so we never touch the live
# module pool, the disk repository, or leak threads between tests.
# --------------------------------------------------------------------------- #
def _fresh_pool(monkeypatch):
    """Swap the module pool for a fresh one for the duration of one test."""
    pool = ingestion_service._InboundReviewWorkerPool()
    monkeypatch.setattr(ingestion_service, "_INBOUND_REVIEW_POOL", pool)
    return pool


# --------------------------------------------------------------------------- #
# Issue 1 (OOM edge): a burst of N imports creates a BOUNDED pool of threads,
# NOT N daemon threads.
# --------------------------------------------------------------------------- #
def test_burst_of_100_imports_creates_bounded_threads_not_one_per_matter(monkeypatch):
    monkeypatch.delenv(ingestion_service.INBOUND_AI_REVIEW_ENABLED_ENV, raising=False)
    monkeypatch.setenv(ingestion_service.INBOUND_REVIEW_CONCURRENCY_ENV, "1")
    pool = _fresh_pool(monkeypatch)

    live_threads: set[int] = set()
    processed: list[str] = []
    lock = threading.Lock()
    gate = threading.Event()

    def _handler(matter_id, owner_user_id):
        # Block briefly so all 100 jobs are in flight while we count the threads
        # that are actually draining them.
        with lock:
            live_threads.add(threading.get_ident())
        gate.wait(timeout=5)
        with lock:
            processed.append(matter_id)

    pool.configure(_handler)

    # Enqueue a 100-matter burst through the PRODUCTION scheduling path (no runner
    # injected), exactly as a catch-up poll of MAX_GMAIL_IMPORT_LIMIT would.
    for index in range(100):
        scheduled = schedule_inbound_ai_review(
            {"id": f"matter_{index}", "extracted_text": "x"}
        )
        assert scheduled is True

    # The number of worker threads is the pool size (1), NEVER ~100.
    time.sleep(0.1)  # let the single worker pick up the first job
    assert len(live_threads) <= ingestion_service.inbound_review_concurrency()
    assert len(live_threads) == 1  # default concurrency

    gate.set()
    pool._join_for_tests(timeout=5)
    assert len(processed) == 100  # every matter still got processed, serially


def test_burst_respects_concurrency_env_for_pool_size(monkeypatch):
    monkeypatch.delenv(ingestion_service.INBOUND_AI_REVIEW_ENABLED_ENV, raising=False)
    monkeypatch.setenv(ingestion_service.INBOUND_REVIEW_CONCURRENCY_ENV, "3")
    pool = _fresh_pool(monkeypatch)

    live_threads: set[int] = set()
    lock = threading.Lock()
    gate = threading.Event()

    def _handler(matter_id, owner_user_id):
        with lock:
            live_threads.add(threading.get_ident())
        gate.wait(timeout=5)

    pool.configure(_handler)
    for index in range(50):
        schedule_inbound_ai_review({"id": f"m_{index}", "extracted_text": "x"})

    time.sleep(0.1)
    # Bounded by the configured pool size (3), still far below the 50-matter burst.
    assert len(live_threads) <= 3
    gate.set()
    pool._join_for_tests(timeout=5)


def test_enqueue_dedups_same_matter_and_bounds_the_queue(monkeypatch):
    monkeypatch.delenv(ingestion_service.INBOUND_AI_REVIEW_ENABLED_ENV, raising=False)
    monkeypatch.setenv(ingestion_service.INBOUND_REVIEW_CONCURRENCY_ENV, "1")
    pool = _fresh_pool(monkeypatch)

    processed: list[str] = []
    lock = threading.Lock()
    release = threading.Event()

    def _handler(matter_id, owner_user_id):
        release.wait(timeout=5)
        with lock:
            processed.append(matter_id)

    pool.configure(_handler)
    # Enqueue the SAME matter many times before the worker drains it: dedup keeps
    # it pending exactly once.
    for _ in range(20):
        assert schedule_inbound_ai_review({"id": "same", "extracted_text": "x"}) is True
    # The first job is already taken by the worker (pending count is 0 or 1).
    assert pool._pending_count() <= 1

    release.set()
    pool._join_for_tests(timeout=5)
    assert processed.count("same") <= 1


# --------------------------------------------------------------------------- #
# Issue 2 (silent-skip): the recovery sweep re-enqueues an un-AI-reviewed matter
# and it gets reviewed, while an already-ai_first matter is NOT re-reviewed.
# --------------------------------------------------------------------------- #
def test_recovery_sweep_reenqueues_unreviewed_but_not_already_reviewed(monkeypatch):
    monkeypatch.delenv(ingestion_service.INBOUND_AI_REVIEW_ENABLED_ENV, raising=False)
    monkeypatch.setenv(ingestion_service.INBOUND_REVIEW_CONCURRENCY_ENV, "1")
    repository = InMemoryMatterRepository()
    ai_engine = _stub_ai_first_engine()
    pool = _fresh_pool(monkeypatch)

    # Route the pool's per-job handler at the in-memory repo + stub engine.
    def _handler(matter_id, owner_user_id):
        ingestion_service._perform_inbound_ai_review(
            matter_id,
            repository=repository,
            owner_user_id=owner_user_id,
            review_engine_func=ai_engine,
            use_semaphore=False,
        )

    pool.configure(_handler)

    # One inbound matter left deterministic-only (never AI-reviewed)...
    unreviewed = create_matter_from_document(
        filename="unreviewed.docx", document_bytes=_docx(NDA_PARAGRAPHS),
        source_type="gmail_inbound", repository=repository, defer_ai_review=True,
    )
    # ...and one already AI-reviewed (the sweep must NOT touch it).
    already = create_matter_from_document(
        filename="already.docx", document_bytes=_docx(NDA_PARAGRAPHS),
        source_type="gmail_inbound", repository=repository, defer_ai_review=True,
    )
    schedule_inbound_ai_review(
        already, repository=repository, runner=_synchronous_runner, review_engine_func=ai_engine
    )
    assert repository.get_matter(already["id"])["review_result"][
        "active_review_engine"]["executed_engine"] == "ai_first"
    calls_before = len(ai_engine.calls)  # type: ignore[attr-defined]

    enqueued = ingestion_service.recover_unreviewed_inbound_matters(repository=repository)
    assert enqueued == 1  # only the unreviewed matter was re-enqueued
    pool._join_for_tests(timeout=5)

    # The unreviewed matter is now AI-reviewed; the already-reviewed one ran once.
    assert repository.get_matter(unreviewed["id"])["review_result"][
        "active_review_engine"]["executed_engine"] == "ai_first"
    assert len(ai_engine.calls) == calls_before + 1  # type: ignore[attr-defined]


def test_recovery_sweep_skips_outbound_and_is_bounded(monkeypatch):
    monkeypatch.delenv(ingestion_service.INBOUND_AI_REVIEW_ENABLED_ENV, raising=False)
    repository = InMemoryMatterRepository()
    pool = _fresh_pool(monkeypatch)
    enqueued_ids: list[str] = []
    pool.configure(lambda matter_id, owner: enqueued_ids.append(matter_id))

    # An OUTBOUND (generated) matter must never be swept into inbound auto-review.
    create_matter_from_document(
        filename="generated.docx", document_bytes=_docx(NDA_PARAGRAPHS),
        source_type="generated", repository=repository, defer_ai_review=True,
    )
    # Many inbound matters; the sweep is bounded per call.
    for index in range(10):
        create_matter_from_document(
            filename=f"in_{index}.docx", document_bytes=_docx(NDA_PARAGRAPHS),
            source_type="gmail_inbound", repository=repository, defer_ai_review=True,
        )

    enqueued = ingestion_service.recover_unreviewed_inbound_matters(
        repository=repository, limit=3
    )
    assert enqueued == 3  # capped at the per-sweep limit
    pool._join_for_tests(timeout=5)
    # The generated/outbound matter was never enqueued.
    assert all("generated" not in i for i in enqueued_ids)


# --------------------------------------------------------------------------- #
# Issue 3 (kill-switch): NDA_INBOUND_AI_REVIEW_ENABLED=false -> zero AI review.
# --------------------------------------------------------------------------- #
def test_kill_switch_off_skips_scheduling_and_sweep(monkeypatch):
    repository = InMemoryMatterRepository()
    pool = _fresh_pool(monkeypatch)
    ran: list[str] = []
    pool.configure(lambda matter_id, owner: ran.append(matter_id))

    matter = create_matter_from_document(
        filename="a.docx", document_bytes=_docx(NDA_PARAGRAPHS),
        source_type="gmail_inbound", repository=repository, defer_ai_review=True,
    )

    monkeypatch.setenv(ingestion_service.INBOUND_AI_REVIEW_ENABLED_ENV, "false")
    assert ingestion_service.inbound_ai_review_enabled() is False
    # Scheduling is a no-op (production path).
    assert schedule_inbound_ai_review(matter) is False
    # The explicit-runner path is ALSO gated by the kill-switch.
    assert schedule_inbound_ai_review(
        matter, repository=repository, runner=_synchronous_runner,
        review_engine_func=_stub_ai_first_engine(),
    ) is False
    # The recovery sweep is a no-op too.
    assert ingestion_service.recover_unreviewed_inbound_matters(repository=repository) == 0

    pool._join_for_tests(timeout=1)
    assert ran == []  # nothing was ever reviewed
    # The matter keeps its deterministic first-pass, reviewable on-demand.
    assert repository.get_matter(matter["id"])["review_result"][
        "active_review_engine"]["executed_engine"] == "deterministic"


def test_kill_switch_default_enabled(monkeypatch):
    monkeypatch.delenv(ingestion_service.INBOUND_AI_REVIEW_ENABLED_ENV, raising=False)
    assert ingestion_service.inbound_ai_review_enabled() is True
    for value in ("false", "0", "no", "off", "FALSE", "Off"):
        monkeypatch.setenv(ingestion_service.INBOUND_AI_REVIEW_ENABLED_ENV, value)
        assert ingestion_service.inbound_ai_review_enabled() is False
    for value in ("true", "1", "yes", "on", "anything"):
        monkeypatch.setenv(ingestion_service.INBOUND_AI_REVIEW_ENABLED_ENV, value)
        assert ingestion_service.inbound_ai_review_enabled() is True
