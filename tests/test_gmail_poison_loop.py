"""Tests for the Gmail poison-message quarantine + poll resilience (fix/gmail-poison-loop).

Four interlocking prod defects are pinned here:

A. INFINITE AI RETRY LOOP: a message whose heavy import path fails the same way on
   every poll (poisoned attachment -> create_matter raises) was retried FOREVER,
   re-running the paid gmail_triage selector + gmail_intake classifier AI calls each
   cycle. Now each expensive-stage transient failure bumps a per-message attempt
   counter in the processed ledger; at the limit (default 5, env
   NDA_GMAIL_TRANSIENT_RETRY_LIMIT) the message is terminally marked with reason
   "quarantined" and never burns another AI call.
A-guard. RETRY BIAS PRESERVED: a genuinely transient failure (fails twice, then
   succeeds) still imports fine and is never quarantined.
A-budget. Only the EXPENSIVE stage counts an attempt: a message never reached
   because the per-poll budget ran out accrues nothing and is not quarantined.
C. RESILIENCE: the ledger flush + drain-cursor persist now run in try/finally, so a
   mid-scan exception no longer forgets the terminal outcomes already reached this
   poll (which re-ran their AI calls next cycle).

The server-side fan-out fairness (B) + JSON summary/error visibility (D) are tested
in tests/test_server.py alongside the existing scheduler tests.
"""

from __future__ import annotations

import json

import pytest

from nda_automation import (
    gmail_integration,
    gmail_matter_inbox,
    gmail_processed_ledger as ledger,
    matter_store,
)

from tests.test_gmail_processed_ledger import (
    _CursorAwareLedgerTransport,
    _LedgerSpyTransport,
)


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


@pytest.fixture
def ledger_data_dir(tmp_path, monkeypatch):
    """Point the ledger's DATA_DIR root at an isolated tmp dir for the test."""
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    return tmp_path


# --------------------------------------------------------------------------- #
# Unit: the env knob.
# --------------------------------------------------------------------------- #
def test_transient_retry_limit_default_and_env_override(monkeypatch):
    monkeypatch.delenv(gmail_matter_inbox.NDA_GMAIL_TRANSIENT_RETRY_LIMIT_ENV, raising=False)
    assert gmail_matter_inbox._transient_retry_limit() == 5
    assert gmail_matter_inbox.DEFAULT_TRANSIENT_RETRY_LIMIT == 5
    # A valid positive override is honoured verbatim.
    for raw, expected in (("1", 1), ("3", 3), ("10", 10)):
        monkeypatch.setenv(gmail_matter_inbox.NDA_GMAIL_TRANSIENT_RETRY_LIMIT_ENV, raw)
        assert gmail_matter_inbox._transient_retry_limit() == expected, raw
    # Fail-open: junk / blank / non-positive fall back to the default (a
    # misconfigured knob must never quarantine on the very first failure).
    for raw in ("", "   ", "abc", "0", "-2", "2.5"):
        monkeypatch.setenv(gmail_matter_inbox.NDA_GMAIL_TRANSIENT_RETRY_LIMIT_ENV, raw)
        assert gmail_matter_inbox._transient_retry_limit() == 5, raw


# --------------------------------------------------------------------------- #
# Unit: attempt counting + quarantine in the ledger itself.
# --------------------------------------------------------------------------- #
def test_ledger_attempt_counts_round_trip_across_sessions(ledger_data_dir):
    owner = "owner_1"
    session = ledger.ProcessedLedgerSession(owner)
    assert session.attempt_count("msg_a") == 0
    assert session.record_attempt("msg_a") == 1
    assert session.record_attempt("msg_a") == 2
    assert session.record_attempt("msg_b") == 1
    session.flush()

    # A fresh session (the next poll) sees the durable counts.
    session2 = ledger.ProcessedLedgerSession(owner)
    assert session2.attempt_count("msg_a") == 2
    assert session2.attempt_count("msg_b") == 1
    assert session2.attempt_count("msg_c") == 0


def test_ledger_mark_drops_the_attempt_counter(ledger_data_dir):
    # A terminal outcome ends retry counting: the attempts blob only ever holds
    # still-retrying ids, so the file cannot grow with stale counters.
    owner = "owner_1"
    session = ledger.ProcessedLedgerSession(owner)
    session.record_attempt("msg_a")
    session.record_attempt("msg_a")
    session.mark("msg_a")
    session.flush()

    session2 = ledger.ProcessedLedgerSession(owner)
    assert session2.is_processed("msg_a") is True
    assert session2.attempt_count("msg_a") == 0
    payload = json.loads(
        (ledger_data_dir / "gmail" / "owner_1" / "gmail-processed-messages.json").read_text()
    )
    assert "attempts" not in payload  # empty sections are omitted (legacy shape)


def test_ledger_quarantine_is_processed_and_durably_distinguished(ledger_data_dir):
    owner = "owner_1"
    session = ledger.ProcessedLedgerSession(owner)
    session.record_attempt("msg_a")
    session.quarantine("msg_a", reason="review_failed", attempts=5)
    session.flush()

    session2 = ledger.ProcessedLedgerSession(owner)
    # Quarantined == processed (skipped before any fetch/AI work on future polls)...
    assert session2.is_processed("msg_a") is True
    # ... AND a DISTINCT keyed record (findable/removable without touching the
    # normal marks), with its retry counter dropped.
    assert session2.quarantined_ids() == {"msg_a"}
    assert session2.attempt_count("msg_a") == 0
    payload = json.loads(
        (ledger_data_dir / "gmail" / "owner_1" / "gmail-processed-messages.json").read_text()
    )
    assert payload["message_ids"] == ["msg_a"]
    record = payload["quarantined"]["msg_a"]
    assert record["attempts"] == 5
    assert record["reason"] == "review_failed"
    assert record["last_at"]  # timestamped for operator triage
    # The module-level inspector sees the same keyed record.
    assert ledger.quarantined_messages(owner)["msg_a"]["reason"] == "review_failed"


def test_ledger_requeue_releases_a_quarantined_message(ledger_data_dir):
    # THE MANUAL RECOVERY PATH: requeue removes the id from marks + quarantine +
    # attempts so the next poll retries it with a clean slate.
    owner = "owner_1"
    session = ledger.ProcessedLedgerSession(owner)
    session.record_attempt("msg_a")
    session.quarantine("msg_a", reason="pdf_text_unreadable_needs_ocr", attempts=2)
    session.mark("msg_other")  # an unrelated normal mark must survive the requeue
    session.flush()

    assert ledger.requeue_quarantined_message("msg_a", owner) is True

    session2 = ledger.ProcessedLedgerSession(owner)
    assert session2.is_processed("msg_a") is False       # falls through the pre-skip
    assert session2.attempt_count("msg_a") == 0          # clean retry slate
    assert session2.quarantined_ids() == set()
    assert session2.is_processed("msg_other") is True    # unrelated mark untouched
    # Requeueing an unknown id is a clean no-op.
    assert ledger.requeue_quarantined_message("msg_a", owner) is False


def test_ledger_legacy_ids_only_file_still_reads(ledger_data_dir):
    # A pre-quarantine ledger file (ids only) loads cleanly: ids preserved, zero
    # attempts, nothing quarantined. (New-code-reads-old-file rollback direction.)
    path = ledger_data_dir / "gmail" / "owner_1" / "gmail-processed-messages.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"message_ids": ["msg_old"]}))
    session = ledger.ProcessedLedgerSession("owner_1")
    assert session.is_processed("msg_old") is True
    assert session.attempt_count("msg_old") == 0
    assert session.quarantined_ids() == set()


def test_ledger_new_file_shape_readable_by_old_code_path(ledger_data_dir):
    # Old-code-reads-new-file rollback direction: the legacy reader
    # (_coerce_id_list over the whole payload) ignores the attempts/quarantined
    # sections and still sees the processed ids.
    owner = "owner_1"
    session = ledger.ProcessedLedgerSession(owner)
    session.mark("msg_a")
    session.record_attempt("msg_b")
    session.quarantine("msg_c", reason="attachment_too_large", attempts=2)
    session.flush()
    payload = json.loads(
        (ledger_data_dir / "gmail" / "owner_1" / "gmail-processed-messages.json").read_text()
    )
    # Exactly what the OLD reader evaluates: the message_ids list.
    assert ledger._coerce_id_list(payload) == ["msg_a", "msg_c"]


def test_ledger_legacy_list_shaped_quarantine_section_tolerated(ledger_data_dir):
    # Defensive: a hand-edited/early list-shaped quarantined section coerces to
    # keyed entries instead of crashing the poll.
    path = ledger_data_dir / "gmail" / "owner_1" / "gmail-processed-messages.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"message_ids": ["msg_q"], "quarantined": ["msg_q"]}))
    session = ledger.ProcessedLedgerSession("owner_1")
    assert session.quarantined_ids() == {"msg_q"}
    assert ledger.quarantined_messages("owner_1")["msg_q"]["attempts"] == 0


def test_ledger_corrupt_file_is_sidelined_not_clobbered_on_flush(ledger_data_dir):
    # CLOBBER GUARD: when the load found an EXISTING but unreadable file, flush
    # sidelines it to *.corrupt (forensics) before writing the fresh ledger,
    # instead of silently overwriting up to 20k prior marks in place.
    path = ledger_data_dir / "gmail" / "owner_1" / "gmail-processed-messages.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json at all")

    session = ledger.ProcessedLedgerSession("owner_1")
    session.mark("msg_new")
    assert session.flush() is True

    corrupt = path.with_name(path.name + ".corrupt")
    assert corrupt.is_file()
    assert corrupt.read_text() == "{ not json at all"
    assert json.loads(path.read_text())["message_ids"] == ["msg_new"]


# --------------------------------------------------------------------------- #
# Integration harness: the real inbox loop + the REAL durable ledger + AI spies.
# --------------------------------------------------------------------------- #
class _RealLedgerTransport(_LedgerSpyTransport):
    """The ledger-aware spy transport, but with the REAL durable file-backed
    ProcessedLedgerSession (rooted at the test's isolated DATA_DIR) so attempt
    counting + quarantine are exercised end-to-end through actual persistence.
    """

    def processed_ledger_session(self, owner_user_id: str = "") -> ledger.ProcessedLedgerSession:
        return ledger.ProcessedLedgerSession(owner_user_id)


class _PoisonedCreateTransport(_RealLedgerTransport):
    """Every create_matter_from_document call fails the same way, forever --
    modelling a poisoned message whose import path deterministically crashes AFTER
    the paid selector/intake AI calls (the exact live prod loop).
    """

    def __init__(self, inbox_size: int, *, import_limit: int) -> None:
        super().__init__(inbox_size, import_limit=import_limit)
        self.create_attempts = 0

    def create_matter_from_document(self, **kwargs):
        self.create_attempts += 1
        raise self.ActiveReviewEngineError("poisoned attachment: review engine crash")


class _FlakyThenHealthyCreateTransport(_RealLedgerTransport):
    """create_matter_from_document fails N times, then succeeds -- a genuine
    transient blip that must keep its retries and NEVER quarantine.
    """

    def __init__(self, inbox_size: int, *, import_limit: int, failures: int) -> None:
        super().__init__(inbox_size, import_limit=import_limit)
        self._failures_left = failures

    def create_matter_from_document(self, **kwargs):
        if self._failures_left > 0:
            self._failures_left -= 1
            raise self.ActiveReviewEngineError("transient review engine blip")
        return super().create_matter_from_document(**kwargs)


def _poll(transport, *, limit=999):
    return gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=limit, owner_user_id="owner_1",
    )


# --------------------------------------------------------------------------- #
# A: the retry cap -- a poisoned message stops burning AI spend after N polls.
# --------------------------------------------------------------------------- #
def test_poisoned_message_is_quarantined_after_retry_limit_and_stops_ai_calls(
    ledger_data_dir, import_limit_20, monkeypatch,
):
    monkeypatch.delenv(gmail_matter_inbox.NDA_GMAIL_TRANSIENT_RETRY_LIMIT_ENV, raising=False)
    limit = gmail_matter_inbox.DEFAULT_TRANSIENT_RETRY_LIMIT  # 5
    transport = _PoisonedCreateTransport(inbox_size=1, import_limit=import_limit_20)

    # Polls 1..N-1: the heavy path (selector + intake AI, then create) runs and
    # fails transiently each time; the message stays UNMARKED (retry bias) and the
    # attempt counter climbs.
    for poll_number in range(1, limit):
        result = _poll(transport)
        assert result["imported"] == []
        assert any(s.get("reason") == "review_failed" for s in result["skipped"])
        assert result["quarantined"] == 0
        assert transport.selector_calls == ["msg_000"] * poll_number  # PAID AI call re-ran
        session = ledger.ProcessedLedgerSession("owner_1")
        assert session.is_processed("msg_000") is False
        assert session.attempt_count("msg_000") == poll_number

    # Poll N: the Nth expensive failure reaches the cap -> the message is
    # terminally QUARANTINED, visibly (skip reason + result counter + ledger).
    result_n = _poll(transport)
    assert result_n["imported"] == []
    quarantine_skips = [s for s in result_n["skipped"] if s.get("reason") == "quarantined"]
    assert len(quarantine_skips) == 1
    assert quarantine_skips[0]["attempts"] == str(limit)
    assert result_n["quarantined"] == 1
    session = ledger.ProcessedLedgerSession("owner_1")
    assert session.is_processed("msg_000") is True
    assert session.quarantined_ids() == {"msg_000"}
    # The durable quarantine record carries the underlying reason for triage.
    record = ledger.quarantined_messages("owner_1")["msg_000"]
    assert record["reason"] == "review_failed"
    assert record["attempts"] == limit
    assert transport.create_attempts == limit

    # Poll N+1: THE WHOLE POINT -- the quarantined message is skipped BEFORE the
    # fetch and BEFORE the selector/intake AI calls. No more paid retries, ever.
    transport.selector_calls.clear()
    transport.intake_calls.clear()
    result_after = _poll(transport)
    assert any(s.get("reason") == "processed_message" for s in result_after["skipped"])
    assert transport.selector_calls == []   # gmail_triage NOT invoked again
    assert transport.intake_calls == []     # gmail_intake NOT invoked again
    assert transport.create_attempts == limit  # the heavy path never re-ran
    assert result_after["quarantined"] == 0    # nothing NEWLY quarantined


def test_quarantine_honours_env_retry_limit(ledger_data_dir, import_limit_20, monkeypatch):
    monkeypatch.setenv(gmail_matter_inbox.NDA_GMAIL_TRANSIENT_RETRY_LIMIT_ENV, "2")
    transport = _PoisonedCreateTransport(inbox_size=1, import_limit=import_limit_20)

    r1 = _poll(transport)
    assert r1["quarantined"] == 0
    r2 = _poll(transport)
    assert r2["quarantined"] == 1
    assert ledger.ProcessedLedgerSession("owner_1").quarantined_ids() == {"msg_000"}
    assert transport.create_attempts == 2


# --------------------------------------------------------------------------- #
# A (stratified): a DETERMINISTIC-PERMANENT skip (same bytes -> same outcome)
# quarantines at the tighter limit, visibly, with reason + filename for a human.
# --------------------------------------------------------------------------- #
class _TooLargeAttachmentTransport(_RealLedgerTransport):
    """Every attachment fails the size gate -- deterministic on the bytes, so a
    retry can never change the outcome (the too_large stratum).
    """

    def ensure_document_size(self, document_bytes: bytes) -> None:
        raise self.DocumentSizeError("attachment exceeds the size limit")


def test_deterministic_permanent_skip_quarantines_early_with_reason_and_filename(
    ledger_data_dir, import_limit_20, monkeypatch,
):
    monkeypatch.delenv(gmail_matter_inbox.NDA_GMAIL_TRANSIENT_RETRY_LIMIT_ENV, raising=False)
    monkeypatch.delenv(gmail_matter_inbox.NDA_GMAIL_PERMANENT_SKIP_RETRY_LIMIT_ENV, raising=False)
    assert gmail_matter_inbox.DEFAULT_PERMANENT_SKIP_RETRY_LIMIT == 2
    transport = _TooLargeAttachmentTransport(inbox_size=1, import_limit=import_limit_20)

    # Poll 1: attempt 1 -- below even the tight limit; still unmarked (one confirm
    # re-run is allowed).
    r1 = _poll(transport)
    assert any(s.get("reason") == "attachment_too_large" for s in r1["skipped"])
    assert r1["quarantined"] == 0
    assert ledger.ProcessedLedgerSession("owner_1").is_processed("msg_000") is False

    # Poll 2: the deterministic stratum's limit (2) -- quarantined NOW, three polls
    # earlier than an environmental failure would be (5). NOT silent: the skip
    # record carries the filename + underlying reason so a human can act (OCR /
    # raise the size limit / requeue), and the ledger record is inspectable.
    r2 = _poll(transport)
    quarantine_skips = [s for s in r2["skipped"] if s.get("reason") == "quarantined"]
    assert len(quarantine_skips) == 1
    assert quarantine_skips[0]["attachment_filename"] == "inbound_nda_sample.pdf"
    assert "attachment_too_large" in quarantine_skips[0]["detail"]
    assert r2["quarantined"] == 1
    record = ledger.quarantined_messages("owner_1")["msg_000"]
    assert record["reason"] == "attachment_too_large"
    assert record["attempts"] == 2


def test_environmental_failure_is_not_caught_by_the_tight_permanent_limit(
    ledger_data_dir, import_limit_20, monkeypatch,
):
    # CONTROL for the stratification: an environmental failure (review_failed --
    # a retry CAN genuinely fix it) must NOT quarantine at the tight limit (2);
    # it keeps the full environmental allowance (5).
    monkeypatch.delenv(gmail_matter_inbox.NDA_GMAIL_TRANSIENT_RETRY_LIMIT_ENV, raising=False)
    monkeypatch.delenv(gmail_matter_inbox.NDA_GMAIL_PERMANENT_SKIP_RETRY_LIMIT_ENV, raising=False)
    transport = _PoisonedCreateTransport(inbox_size=1, import_limit=import_limit_20)

    r1 = _poll(transport)
    r2 = _poll(transport)
    r3 = _poll(transport)
    assert r1["quarantined"] == r2["quarantined"] == r3["quarantined"] == 0
    assert ledger.ProcessedLedgerSession("owner_1").quarantined_ids() == set()
    assert ledger.ProcessedLedgerSession("owner_1").attempt_count("msg_000") == 3


# --------------------------------------------------------------------------- #
# A (reversible): the requeue path releases a quarantined message end-to-end.
# --------------------------------------------------------------------------- #
def test_requeued_quarantined_message_is_retried_and_imports(
    ledger_data_dir, import_limit_20, monkeypatch,
):
    monkeypatch.setenv(gmail_matter_inbox.NDA_GMAIL_TRANSIENT_RETRY_LIMIT_ENV, "2")
    # Fails on polls 1+2 (-> quarantined at the limit), healthy afterwards --
    # modelling a poison whose root cause an operator has since fixed.
    transport = _FlakyThenHealthyCreateTransport(
        inbox_size=1, import_limit=import_limit_20, failures=2,
    )

    _poll(transport)
    r2 = _poll(transport)
    assert r2["quarantined"] == 1
    assert ledger.ProcessedLedgerSession("owner_1").quarantined_ids() == {"msg_000"}

    # Poll 3 (still quarantined): skipped pre-AI, nothing imported.
    transport.selector_calls.clear()
    r3 = _poll(transport)
    assert r3["imported"] == []
    assert transport.selector_calls == []

    # OPERATOR RECOVERY: requeue, then the next poll retries + imports normally.
    assert ledger.requeue_quarantined_message("msg_000", "owner_1") is True
    r4 = _poll(transport)
    assert len(r4["imported"]) == 1
    assert transport.selector_calls == ["msg_000"]  # the retry genuinely re-ran
    session = ledger.ProcessedLedgerSession("owner_1")
    assert session.is_processed("msg_000") is True   # marked via the normal path
    assert session.quarantined_ids() == set()


# --------------------------------------------------------------------------- #
# A-guard: the retry bias is preserved -- transient blips still import.
# --------------------------------------------------------------------------- #
def test_message_that_fails_twice_then_succeeds_imports_and_is_never_quarantined(
    ledger_data_dir, import_limit_20, monkeypatch,
):
    monkeypatch.delenv(gmail_matter_inbox.NDA_GMAIL_TRANSIENT_RETRY_LIMIT_ENV, raising=False)
    transport = _FlakyThenHealthyCreateTransport(
        inbox_size=1, import_limit=import_limit_20, failures=2,
    )

    r1 = _poll(transport)
    r2 = _poll(transport)
    assert r1["imported"] == [] and r2["imported"] == []
    assert r1["quarantined"] == 0 and r2["quarantined"] == 0
    assert ledger.ProcessedLedgerSession("owner_1").attempt_count("msg_000") == 2

    # Third poll: the blip has passed -- the message imports fine (retry bias).
    r3 = _poll(transport)
    assert len(r3["imported"]) == 1
    assert r3["quarantined"] == 0
    session = ledger.ProcessedLedgerSession("owner_1")
    assert session.is_processed("msg_000") is True
    assert session.quarantined_ids() == set()
    # The successful import DROPPED the attempt counter (no stale accounting).
    assert session.attempt_count("msg_000") == 0


# --------------------------------------------------------------------------- #
# A-budget: only the EXPENSIVE stage counts -- budget-starved mail never
# quarantines, and once the poison is quarantined the queued mail imports.
# --------------------------------------------------------------------------- #
def test_budget_starved_messages_accrue_no_attempts_and_import_after_quarantine(
    ledger_data_dir, monkeypatch,
):
    monkeypatch.setenv(gmail_matter_inbox.NDA_GMAIL_TRANSIENT_RETRY_LIMIT_ENV, "2")
    # Poisoned newest message + ONE heavy slot per poll: msg_001 sits behind the
    # poison and is never attempted while the budget is consumed by msg_000.
    transport = _PoisonedCreateTransport(inbox_size=2, import_limit=1)

    _poll(transport, limit=1)   # poll 1: msg_000 fails (attempt 1); msg_001 untouched
    session = ledger.ProcessedLedgerSession("owner_1")
    assert session.attempt_count("msg_000") == 1
    assert session.attempt_count("msg_001") == 0  # never attempted -> no counter

    _poll(transport, limit=1)   # poll 2: msg_000 fails again -> QUARANTINED
    session = ledger.ProcessedLedgerSession("owner_1")
    assert session.quarantined_ids() == {"msg_000"}
    assert session.attempt_count("msg_001") == 0

    # Poll 3: the poison no longer eats the budget -- msg_001 gets the heavy slot.
    # Its create still fails (this transport poisons every create), but the point
    # is proven the other way: it was ATTEMPTED (accrues its own first attempt)
    # only NOW, and was never quarantined while merely queued.
    _poll(transport, limit=1)
    session = ledger.ProcessedLedgerSession("owner_1")
    assert session.attempt_count("msg_001") == 1
    assert session.quarantined_ids() == {"msg_000"}


def test_healthy_message_behind_quarantined_poison_imports(ledger_data_dir, monkeypatch):
    # The end-to-end payoff of A+B at the inbox level: a healthy NDA queued behind a
    # poisoned message imports normally once the poison is quarantined.
    monkeypatch.setenv(gmail_matter_inbox.NDA_GMAIL_TRANSIENT_RETRY_LIMIT_ENV, "2")

    class _PoisonOnlyFirstMessageTransport(_RealLedgerTransport):
        def create_matter_from_document(self, **kwargs):
            metadata = kwargs.get("intake_metadata") or {}
            if str(metadata.get("gmail_message_id") or "") == "msg_000":
                raise self.ActiveReviewEngineError("poisoned attachment")
            return super().create_matter_from_document(**kwargs)

    transport = _PoisonOnlyFirstMessageTransport(inbox_size=2, import_limit=1)

    _poll(transport, limit=1)   # poll 1: poison fails (attempt 1)
    _poll(transport, limit=1)   # poll 2: poison fails -> quarantined
    result3 = _poll(transport, limit=1)  # poll 3: the healthy message imports
    assert len(result3["imported"]) == 1
    matters = {
        m["gmail_message_id"]
        for m in transport.repository.list_matters(owner_user_id="owner_1")
    }
    assert matters == {"msg_001"}
    assert ledger.ProcessedLedgerSession("owner_1").quarantined_ids() == {"msg_000"}


# --------------------------------------------------------------------------- #
# C: ledger flush + drain-cursor persist happen even when the scan raises.
# --------------------------------------------------------------------------- #
class _SecondGetRaisesTransport(_CursorAwareLedgerTransport):
    """msg_000 imports cleanly; msg_001's get() raises an error the transport
    escalates (retry-after present, not a 429) -> raise_gmail_api_error raises
    GmailIntegrationError OUT of the scan. Pre-fix that skipped the ledger flush
    AND the cursor persist; both must now still happen (try/finally).
    """

    class _EscalatedGetError(Exception):
        pass

    def __init__(self, inbox_size: int, *, import_limit: int) -> None:
        super().__init__(inbox_size, import_limit=import_limit)
        outer = self
        messages_api = self.service.users_api.messages_api
        original_get = messages_api.get

        def _poisoned_get(*, userId, id, format):
            if id == "msg_001":
                raise outer._EscalatedGetError("gmail get() exploded")
            return original_get(userId=userId, id=id, format=format)

        messages_api.get = _poisoned_get  # type: ignore[assignment]

    def is_rate_limit_error(self, error: Exception) -> bool:
        return False

    def gmail_retry_after_epoch(self, error: Exception) -> float:
        return 60.0 if isinstance(error, self._EscalatedGetError) else 0.0

    def raise_gmail_api_error(self, error: Exception, fallback_message: str) -> None:
        raise gmail_integration.GmailIntegrationError(fallback_message)


def test_scan_exception_still_flushes_ledger_and_persists_cursor(
    ledger_data_dir, import_limit_20,
):
    transport = _SecondGetRaisesTransport(inbox_size=2, import_limit=import_limit_20)

    with pytest.raises(gmail_integration.GmailIntegrationError):
        _poll(transport)

    # msg_000's terminal outcome reached BEFORE the explosion was NOT forgotten:
    # the in-memory ledger session was flushed durably (try/finally)...
    assert transport.ledger_ids() == {"msg_000"}
    assert len(transport.ledger_write_log) == 1
    # ... and the drain cursor advanced to msg_000's floor, so the next poll
    # resumes below it instead of re-scanning (and re-paying for) the drained head.
    assert transport._drain_cursor == transport._internal_ms["msg_000"]

    # Next poll: msg_000 is skipped BEFORE any fetch/AI work (no re-AI of already-
    # terminal outcomes after a crash -- the C fix's user-visible payoff).
    transport.selector_calls.clear()
    with pytest.raises(gmail_integration.GmailIntegrationError):
        _poll(transport)
    assert transport.selector_calls == []
