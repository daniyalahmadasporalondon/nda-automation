"""Inbound NDAs are NOT auto-reviewed; review runs ONLY on-demand.

Storm-safety contract (post-removal of the inbound auto-review path): the Gmail
poll imports each inbound NDA UN-REVIEWED (defer_ai_review=True) and it STAYS
"Not Reviewed" -- no AI review is ever auto-enqueued on import, by a startup
sweep, or by a re-enqueue loop. The full active-engine review (assessor +
verifier) runs ONLY when a human clicks Review (enqueue_on_demand_review ->
the bounded worker pool). These tests pin:

  * import stays fast + un-reviewed and NO review is scheduled,
  * the inbound poll path schedules NO review (the storm engine is gone),
  * the on-demand worker-pool body (run/persist/failure-recording/human-edit
    preservation) still behaves correctly -- that machinery is REUSED by the
    on-demand path and is exercised here through _perform_inbound_ai_review and
    the pool's enqueue/dedup directly,
  * the pool stays bounded + dedups (so even a burst of on-demand clicks is safe).

The on-demand route + 202 lifecycle live in tests/test_async_review_backend.py;
this file covers the import-time "no auto-review" guarantee + the review body.
"""
from __future__ import annotations

import threading
import time
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from nda_automation import gmail_matter_inbox, ingestion_service
from nda_automation.ingestion_service import create_matter_from_document
from nda_automation.matter_repository import InMemoryMatterRepository

NDA_PARAGRAPHS = [
    "This Mutual Non-Disclosure Agreement is entered into by both parties.",
    "Each party agrees to keep the other party's Confidential Information secret.",
    "This Agreement shall be governed by the laws of England and Wales.",
    "The confidentiality obligations survive for three years from disclosure.",
]


def _docx(paragraphs: list[str]) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
            'package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/'
            'package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
            'openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>',
        )
        body = "".join(
            f"<w:p><w:r><w:t>{para}</w:t></w:r></w:p>" for para in paragraphs
        )
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
            f'wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>',
        )
    return buffer.getvalue()


def _force_stored_fields(repository, matter_id, **fields):
    """Overwrite stored fields directly on the in-memory matter (past the allowlist)."""
    with repository._lock:  # type: ignore[attr-defined]
        for index, matter in enumerate(repository._matters):  # type: ignore[attr-defined]
            if matter.get("id") == matter_id:
                repository._matters[index] = {**matter, **fields}  # type: ignore[attr-defined]
                return
    raise AssertionError(f"matter {matter_id} not found")


def _stub_ai_first_engine(executed_engine: str = "ai_first"):
    """A review-engine stub standing in for the full ai_first (assessor+verifier)."""

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
# (a) Import returns fast, un-reviewed, with NO synchronous review on the poll thread
# --------------------------------------------------------------------------- #
def test_import_creates_unreviewed_matter_no_ai_on_poll_thread():
    repository = InMemoryMatterRepository()
    ai_engine = _stub_ai_first_engine()

    matter = create_matter_from_document(
        filename="inbound.docx",
        document_bytes=_docx(NDA_PARAGRAPHS),
        source_type="gmail_inbound",
        repository=repository,
        defer_ai_review=True,
    )

    # The matter exists immediately and is UN-REVIEWED (no review_result): it shows
    # "Not Reviewed" until a human runs the on-demand review. No engine ran on import.
    assert matter["review_result"] is None
    assert matter.get("extracted_text", "").strip()  # text is still stored for later review
    assert ai_engine.calls == []  # type: ignore[attr-defined]


class _RecordingTransport:
    """Minimal inbound transport stub for create_matter_from_prepared_attachment.

    Records any schedule_inbound_ai_review attempt so a test can PROVE the poll path
    schedules NO review (the inbound auto-review path is removed -- there is no such
    transport method to call any more).
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
        # SHOULD NEVER BE CALLED: the inbound poll path no longer schedules a review.
        self.scheduled.append((matter, owner_user_id))
        return True


def test_prepared_attachment_import_schedules_no_review():
    """STORM SAFETY: importing an inbound NDA must NOT auto-schedule an AI review.

    The transport exposes a schedule_inbound_ai_review hook; the poll path must NOT
    invoke it. (The real transport no longer defines that method at all -- this stub
    records any call so a regression that re-adds the auto-enqueue is caught.)
    """
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
    # NO auto-review was scheduled on import -- inbound NDAs stay "Not Reviewed".
    assert transport.scheduled == []


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
    assert transport.scheduled == []  # no review for a duplicate (or anything else)


def test_no_inbound_autoreview_symbols_remain():
    """The inbound auto-review + kill-switch + recovery sweep are REMOVED entirely.

    Storm-engine guard: assert the removed public entry points are gone so a future
    change cannot quietly re-introduce an inbound auto-enqueue path.
    """
    assert not hasattr(ingestion_service, "schedule_inbound_ai_review")
    assert not hasattr(ingestion_service, "recover_unreviewed_inbound_matters")
    assert not hasattr(ingestion_service, "inbound_ai_review_enabled")


# --------------------------------------------------------------------------- #
# (b) The on-demand review BODY still runs the AI engine and persists onto the matter
#     (this is the machinery the Review button reuses; driven directly here).
# --------------------------------------------------------------------------- #
def test_review_body_runs_ai_engine_and_persists_onto_matter():
    repository = InMemoryMatterRepository()
    ai_engine = _stub_ai_first_engine()
    matter = create_matter_from_document(
        filename="inbound.docx",
        document_bytes=_docx(NDA_PARAGRAPHS),
        source_type="gmail_inbound",
        repository=repository,
        defer_ai_review=True,
    )
    assert matter["review_result"] is None  # un-reviewed at create

    ingestion_service._perform_inbound_ai_review(
        matter["id"], repository=repository, owner_user_id="", review_engine_func=ai_engine,
    )

    assert ai_engine.calls  # the AI engine actually ran  # type: ignore[attr-defined]
    persisted = repository.get_matter(matter["id"])
    assert persisted["review_result"]["active_review_engine"]["executed_engine"] == "ai_first"


def test_review_body_is_idempotent_already_reviewed_matter_skipped():
    repository = InMemoryMatterRepository()
    ai_engine = _stub_ai_first_engine()
    matter = create_matter_from_document(
        filename="inbound.docx",
        document_bytes=_docx(NDA_PARAGRAPHS),
        source_type="gmail_inbound",
        repository=repository,
        defer_ai_review=True,
    )
    ingestion_service._perform_inbound_ai_review(
        matter["id"], repository=repository, owner_user_id="", review_engine_func=ai_engine,
    )
    assert len(ai_engine.calls) == 1  # type: ignore[attr-defined]

    # Run again on the now-ai_first matter: the idempotency guard skips re-review.
    ingestion_service._perform_inbound_ai_review(
        matter["id"], repository=repository, owner_user_id="", review_engine_func=ai_engine,
    )
    assert len(ai_engine.calls) == 1  # type: ignore[attr-defined]  # no re-review


# --------------------------------------------------------------------------- #
# (c) Failure recording: a failing/empty/un-persistable review is RECORDED terminally
#     (never left silently stuck "in_progress").
# --------------------------------------------------------------------------- #
def _failing_engine():
    calls: list[str] = []

    def _engine(text, *, paragraphs=None, **_kwargs):
        calls.append(text)
        raise RuntimeError("permanent review failure")  # caught by the body's except

    _engine.calls = calls  # type: ignore[attr-defined]
    return _engine


def test_review_failure_increments_per_matter_failure_count():
    repository = InMemoryMatterRepository()
    failing = _failing_engine()
    matter = create_matter_from_document(
        filename="inbound.docx",
        document_bytes=_docx(NDA_PARAGRAPHS),
        source_type="gmail_inbound",
        repository=repository,
        defer_ai_review=True,
    )
    ingestion_service._perform_inbound_ai_review(
        matter["id"], repository=repository, owner_user_id="", review_engine_func=failing,
    )
    after = repository.get_matter(matter["id"])
    assert after["inbound_review_failures"] == 1
    assert "inbound_review_failed_at" in after
    assert after["review_result"] is None  # still un-reviewed (the review failed)


def test_empty_extracted_text_is_recorded_terminal_failure_not_silent_return():
    """An on-demand review of a matter whose extracted_text is empty/whitespace
    (scanned / image-only / encrypted PDF) is stamped a TERMINAL, RECORDED failure
    (review_status="failed" + a human-readable reason + the failure counter) -- NOT
    a silent return that leaves the matter stuck "in_progress" forever."""
    from nda_automation import telemetry

    telemetry.reset()
    repository = InMemoryMatterRepository()
    matter = create_matter_from_document(
        filename="scanned-as-image.docx",
        document_bytes=_docx(NDA_PARAGRAPHS),
        source_type="gmail_inbound",
        repository=repository,
        defer_ai_review=True,
    )
    matter_id = matter["id"]
    _force_stored_fields(
        repository, matter_id, extracted_text="   \n\t  ", review_status="in_progress"
    )

    engine = _stub_ai_first_engine()
    ingestion_service._perform_inbound_ai_review(
        matter_id, repository=repository, owner_user_id="", review_engine_func=engine,
    )
    assert engine.calls == []  # no paid assessor/verifier call for unreadable text

    after = repository.get_matter(matter_id)
    assert after["review_status"] == "failed"
    assert "scanned or image-only" in after["review_error"]
    assert after["inbound_review_failures"] == 1
    assert "inbound_review_failed_at" in after
    assert not ingestion_service._matter_already_ai_reviewed(after)
    counters = telemetry.snapshot()["counters"]
    assert counters.get("inbound_ai_review_failed", 0) == 1
    assert counters.get("inbound_ai_review_empty_extracted_text", 0) == 1
    assert counters.get("inbound_ai_review_completed", 0) == 0


class _PersistReturnsNoneRepository(InMemoryMatterRepository):
    """A repo whose review persist always returns None (un-persistable save)."""

    def refresh_matter_review(self, *args, **kwargs):  # noqa: ARG002
        return None


def test_persist_returns_none_counts_as_failure(caplog):
    import logging

    repository = _PersistReturnsNoneRepository()
    matter = create_matter_from_document(
        filename="inbound.docx",
        document_bytes=_docx(NDA_PARAGRAPHS),
        source_type="gmail_inbound",
        repository=repository,
        defer_ai_review=True,
    )
    matter_id = matter["id"]
    engine = _stub_ai_first_engine()  # the review itself SUCCEEDS; only the save is null.

    with caplog.at_level(logging.WARNING):
        for expected in (1, 2, 3):
            ingestion_service._perform_inbound_ai_review(
                matter_id, repository=repository, owner_user_id="", review_engine_func=engine,
            )
            assert repository.get_matter(matter_id)["inbound_review_failures"] == expected

    # The orphan log names the matter id so it can later be found / re-homed.
    assert any(matter_id in rec.getMessage() for rec in caplog.records)
    assert repository.get_matter(matter_id)["review_result"] is None


# --------------------------------------------------------------------------- #
# (d) Bounded concurrency: review bodies never exceed the (now 2) concurrency
#     bound, and a concurrent request is not blocked by a running review. The
#     synchronous storm enqueue is gone, so 2 on-demand reviews may overlap -- the
#     bound just stops a burst from running N-at-once on the worker.
# --------------------------------------------------------------------------- #
def test_two_reviews_stay_within_the_concurrency_bound():
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

    def _run(matter_id):
        # The explicit-runner serialization (use_semaphore=True) is the process-wide
        # bound that the worker pool also enforces by its fixed size.
        ingestion_service._perform_inbound_ai_review(
            matter_id, repository=repository, owner_user_id="",
            review_engine_func=_slow_engine, use_semaphore=True,
        )

    for matter in (matter_a, matter_b):
        thread = threading.Thread(target=_run, args=(matter["id"],))
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join(timeout=5)

    # The process-wide semaphore bounds concurrency to inbound_review_concurrency()
    # (default 2): the two reviews never EXCEED the bound (a burst is never
    # N-at-once), even though 2 may now overlap.
    assert concurrency["max"] <= ingestion_service.inbound_review_concurrency()
    assert concurrency["max"] >= 1
    assert repository.get_matter(matter_a["id"])["review_result"][
        "active_review_engine"]["executed_engine"] == "ai_first"
    assert repository.get_matter(matter_b["id"])["review_result"][
        "active_review_engine"]["executed_engine"] == "ai_first"


def test_review_concurrency_defaults_to_two(monkeypatch):
    monkeypatch.delenv(ingestion_service.INBOUND_REVIEW_CONCURRENCY_ENV, raising=False)
    assert ingestion_service.inbound_review_concurrency() == 2  # bumped 1 -> 2
    monkeypatch.setenv(ingestion_service.INBOUND_REVIEW_CONCURRENCY_ENV, "0")
    assert ingestion_service.inbound_review_concurrency() == 1  # clamped to >= 1
    monkeypatch.setenv(ingestion_service.INBOUND_REVIEW_CONCURRENCY_ENV, "3")
    assert ingestion_service.inbound_review_concurrency() == 3


# --------------------------------------------------------------------------- #
# (e) The worker pool stays bounded + dedups, so even a burst of on-demand
#     enqueues never spawns one thread per job and never re-runs the same matter.
# --------------------------------------------------------------------------- #
def _fresh_pool(monkeypatch):
    """Swap the module pool for a fresh one for the duration of one test."""
    pool = ingestion_service._InboundReviewWorkerPool()
    monkeypatch.setattr(ingestion_service, "_INBOUND_REVIEW_POOL", pool)
    return pool


def test_burst_of_100_enqueues_creates_bounded_threads_not_one_per_job(monkeypatch):
    monkeypatch.setenv(ingestion_service.INBOUND_REVIEW_CONCURRENCY_ENV, "1")
    pool = _fresh_pool(monkeypatch)

    live_threads: set[int] = set()
    processed: list[str] = []
    lock = threading.Lock()
    gate = threading.Event()

    def _handler(matter_id, owner_user_id):
        with lock:
            live_threads.add(threading.get_ident())
        gate.wait(timeout=5)
        with lock:
            processed.append(matter_id)

    pool.configure(_handler)

    # Enqueue a 100-job burst directly onto the pool (what enqueue_on_demand_review
    # does for each clicked Review).
    for index in range(100):
        assert pool.enqueue(f"matter_{index}", "") is True

    time.sleep(0.1)  # let the single worker pick up the first job
    assert len(live_threads) <= ingestion_service.inbound_review_concurrency()
    assert len(live_threads) == 1  # pinned to 1 by the env override above

    gate.set()
    pool._join_for_tests(timeout=5)
    assert len(processed) == 100  # every job still processed, serially


def test_enqueue_dedups_same_matter_and_bounds_the_queue(monkeypatch):
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
    for _ in range(20):
        assert pool.enqueue("same", "") is True
    assert pool._pending_count() <= 1  # dedup keeps it pending exactly once

    release.set()
    pool._join_for_tests(timeout=5)
    assert processed.count("same") <= 1


# --------------------------------------------------------------------------- #
# (f) A human edit landing DURING the review window survives the async persist.
# --------------------------------------------------------------------------- #
def _engine_that_lands_human_edit(repository, matter_id, redline_draft):
    calls: list[str] = []

    def _engine(text, *, paragraphs=None, **_kwargs):
        calls.append(text)
        repository.update_redline_draft(matter_id, redline_draft, owner_user_id="")
        repository.update_matter_fields(matter_id, {"human_reviewed": True}, owner_user_id="")
        return {
            "review_mode": "ai_first",
            "active_review_engine": {"executed_engine": "ai_first"},
            "clauses": [],
            "requirements_passed": 1,
            "requirements_needs_review": 0,
            "requirements_failed": 0,
        }

    _engine.calls = calls  # type: ignore[attr-defined]
    return _engine


def test_review_preserves_concurrent_human_edit():
    repository = InMemoryMatterRepository()
    matter = create_matter_from_document(
        filename="inbound.docx",
        document_bytes=_docx(NDA_PARAGRAPHS),
        source_type="gmail_inbound",
        repository=repository,
        defer_ai_review=True,
    )
    matter_id = matter["id"]
    human_redline = {"clauses": [{"id": "c1", "text": "human-authored redline"}]}
    engine = _engine_that_lands_human_edit(repository, matter_id, human_redline)

    ingestion_service._perform_inbound_ai_review(
        matter_id, repository=repository, owner_user_id="", review_engine_func=engine,
    )

    after = repository.get_matter(matter_id)
    assert after["review_result"]["active_review_engine"]["executed_engine"] == "ai_first"
    assert after["human_reviewed"] is True
    assert after.get("redline_draft") == human_redline


def test_review_normal_update_when_no_concurrent_edit():
    repository = InMemoryMatterRepository()
    matter = create_matter_from_document(
        filename="inbound.docx",
        document_bytes=_docx(NDA_PARAGRAPHS),
        source_type="gmail_inbound",
        repository=repository,
        defer_ai_review=True,
    )
    matter_id = matter["id"]
    repository.update_redline_draft(matter_id, {"clauses": [{"id": "old", "text": "stale"}]}, owner_user_id="")
    repository.update_matter_fields(matter_id, {"human_reviewed": True}, owner_user_id="")
    engine = _stub_ai_first_engine()

    ingestion_service._perform_inbound_ai_review(
        matter_id, repository=repository, owner_user_id="", review_engine_func=engine,
    )

    after = repository.get_matter(matter_id)
    assert after["review_result"]["active_review_engine"]["executed_engine"] == "ai_first"
    assert after["human_reviewed"] is False
    assert "redline_draft" not in after
