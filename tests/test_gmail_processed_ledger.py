"""Tests for the durable per-message processed ledger + its inbox-loop wiring.

Two layers:

* Unit tests for ``gmail_processed_ledger`` itself: round-trip persistence, the
  load-once / mark-many / write-once batch session, most-recent-first eviction at
  the cap, atomic write, and corrupt-file tolerance.
* Integration tests against the REAL ``gmail_matter_inbox.import_inbound_matters``
  loop, driven by a ledger-aware fake transport that ALSO spies on the gmail_intake
  classifier + gmail_triage attachment-selector AI calls, so the central guarantees
  are asserted end-to-end:
    - an already-processed message is skipped BEFORE any AI call;
    - a NEW message is processed and marked;
    - a TRANSIENT failure does NOT mark the message (it retries next poll);
    - the ledger file is written at most ONCE per poll (not per message);
    - the ledger coexists with the drain cursor (a normal drain still makes forward
      progress with the ledger active).
"""

from __future__ import annotations

import json

import pytest

from nda_automation import (
    gmail_integration,
    gmail_matter_inbox,
    gmail_processed_ledger as ledger,
)

from tests.test_gmail_transport import _Executable
from tests.test_inbound_flow_e2e import _FullInboundTransport


@pytest.fixture
def import_limit_20(monkeypatch):
    """Pin the catch-up import limit to 20 through the real module constant.

    A local copy of the e2e fixture (rather than a cross-module import that trips
    ruff's redefinition check) -- the integration tests below drive the real inbox
    loop, whose per-poll NEW-work budget is this limit.
    """
    monkeypatch.setenv(gmail_integration.NDA_GMAIL_IMPORT_LIMIT_ENV, "20")
    monkeypatch.setattr(
        gmail_integration,
        "MAX_GMAIL_IMPORT_LIMIT",
        gmail_integration._gmail_import_limit_from_env(),
    )
    assert gmail_integration.MAX_GMAIL_IMPORT_LIMIT == 20
    from nda_automation import ingestion_service

    monkeypatch.delenv(ingestion_service.INBOUND_AI_REVIEW_ENABLED_ENV, raising=False)
    return 20


# --------------------------------------------------------------------------- #
# Unit: the ledger module
# --------------------------------------------------------------------------- #
@pytest.fixture
def ledger_data_dir(tmp_path, monkeypatch):
    """Point the ledger's DATA_DIR root at an isolated tmp dir for the test.

    The ledger roots at ``matter_store.DATA_DIR``; repoint that module attribute so
    a unit test never touches the shared session tmp dir other tests use.
    """
    from nda_automation import matter_store

    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    return tmp_path


def test_mark_load_and_is_processed_round_trip(ledger_data_dir):
    owner = "owner_1"
    assert ledger.load_processed_message_ids(owner) == set()
    assert ledger.is_message_processed("msg_a", owner) is False

    ledger.mark_message_processed("msg_a", owner)
    ledger.mark_message_processed("msg_b", owner)

    assert ledger.load_processed_message_ids(owner) == {"msg_a", "msg_b"}
    assert ledger.is_message_processed("msg_a", owner) is True
    assert ledger.is_message_processed("msg_b", owner) is True
    assert ledger.is_message_processed("msg_c", owner) is False


def test_ledger_persists_under_sanitized_owner_path(ledger_data_dir):
    # The ledger file lands at DATA_DIR/gmail/<sanitized-owner>/gmail-processed-messages.json
    ledger.mark_message_processed("msg_a", "owner_1")
    expected = ledger_data_dir / "gmail" / "owner_1" / "gmail-processed-messages.json"
    assert expected.is_file()
    payload = json.loads(expected.read_text())
    assert payload == {"message_ids": ["msg_a"]}


def test_ledger_is_per_owner_isolated(ledger_data_dir):
    ledger.mark_message_processed("msg_a", "owner_1")
    ledger.mark_message_processed("msg_b", "owner_2")
    assert ledger.load_processed_message_ids("owner_1") == {"msg_a"}
    assert ledger.load_processed_message_ids("owner_2") == {"msg_b"}


def test_mark_is_idempotent_no_rewrite(ledger_data_dir, monkeypatch):
    owner = "owner_1"
    ledger.mark_message_processed("msg_a", owner)

    # A second mark of the same id must not rewrite the file.
    calls: list[int] = []
    real_write = ledger._write_ids
    monkeypatch.setattr(ledger, "_write_ids", lambda *a, **k: (calls.append(1), real_write(*a, **k))[1])
    ledger.mark_message_processed("msg_a", owner)
    assert calls == []  # no write for an already-present id


def test_session_load_once_mark_many_write_once(ledger_data_dir, monkeypatch):
    owner = "owner_1"

    read_calls: list[int] = []
    write_calls: list[int] = []
    real_read = ledger._read_ids
    real_write = ledger._write_ids
    monkeypatch.setattr(ledger, "_read_ids", lambda o: (read_calls.append(1), real_read(o))[1])
    monkeypatch.setattr(ledger, "_write_ids", lambda o, ids: (write_calls.append(1), real_write(o, ids))[1])

    session = ledger.ProcessedLedgerSession(owner)
    assert len(read_calls) == 1  # the file is read exactly ONCE on construction

    for i in range(50):
        session.mark(f"msg_{i}")
    # Marking is in-memory only: NO writes yet.
    assert write_calls == []
    assert session.dirty is True

    wrote = session.flush()
    assert wrote is True
    assert len(write_calls) == 1  # written exactly ONCE
    # ... and a second flush with nothing new is a no-op.
    assert session.flush() is False
    assert len(write_calls) == 1
    # The read count never grew during the mark loop.
    assert len(read_calls) == 1

    # The 50 marks are durable.
    assert ledger.load_processed_message_ids(owner) == {f"msg_{i}" for i in range(50)}


def test_session_flush_noop_when_nothing_marked(ledger_data_dir, monkeypatch):
    owner = "owner_1"
    write_calls: list[int] = []
    monkeypatch.setattr(ledger, "_write_ids", lambda *a, **k: write_calls.append(1))
    session = ledger.ProcessedLedgerSession(owner)
    assert session.flush() is False
    assert write_calls == []  # a poll that marks nothing performs ZERO writes


def test_cap_evicts_oldest_keeps_most_recent(ledger_data_dir, monkeypatch):
    # Shrink the cap so the test is fast, then prove most-recent-first eviction.
    monkeypatch.setattr(ledger, "MAX_LEDGER_ENTRIES", 5)
    owner = "owner_1"
    session = ledger.ProcessedLedgerSession(owner)
    for i in range(8):  # msg_0 (oldest) .. msg_7 (newest)
        session.mark(f"msg_{i}")
    session.flush()

    kept = ledger.load_processed_message_ids(owner)
    # Only the 5 most-recent survive; the 3 oldest were evicted.
    assert kept == {"msg_3", "msg_4", "msg_5", "msg_6", "msg_7"}
    assert "msg_0" not in kept and "msg_2" not in kept


def test_write_is_atomic_no_partial_file(ledger_data_dir):
    owner = "owner_1"
    ledger.mark_message_processed("msg_a", owner)
    path = ledger_data_dir / "gmail" / "owner_1" / "gmail-processed-messages.json"
    # No leftover .tmp file after a successful write (os.replace consumed it).
    assert path.is_file()
    assert not path.with_name(path.name + ".tmp").exists()


def test_corrupt_ledger_file_degrades_to_empty(ledger_data_dir):
    owner = "owner_1"
    path = ledger_data_dir / "gmail" / "owner_1" / "gmail-processed-messages.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not valid json ")
    # A corrupt file is treated as empty, never raises.
    assert ledger.load_processed_message_ids(owner) == set()
    # ... and a subsequent mark recovers cleanly.
    ledger.mark_message_processed("msg_a", owner)
    assert ledger.load_processed_message_ids(owner) == {"msg_a"}


def test_mark_messages_processed_batch_one_shot(ledger_data_dir, monkeypatch):
    owner = "owner_1"
    write_calls: list[int] = []
    real_write = ledger._write_ids
    monkeypatch.setattr(ledger, "_write_ids", lambda o, ids: (write_calls.append(1), real_write(o, ids))[1])
    added = ledger.mark_messages_processed(["a", "b", "c"], owner)
    assert added == 3
    assert len(write_calls) == 1  # batched into a single write
    assert ledger.load_processed_message_ids(owner) == {"a", "b", "c"}


# --------------------------------------------------------------------------- #
# A spying, ledger-aware fake transport built on the real e2e transport.
# --------------------------------------------------------------------------- #
class _InMemoryLedgerSession:
    """An isolated in-memory stand-in for ProcessedLedgerSession.

    Same load-once / mark-many / write-once contract, but backed by a shared dict on
    the transport so it survives across polls (modelling the durable file) WITHOUT
    touching disk. Counts ``flush`` writes so a test can assert at-most-once-per-poll.
    """

    def __init__(self, store: set[str], write_log: list[int]) -> None:
        self._store = store
        self._write_log = write_log
        self._new: set[str] = set()

    def is_processed(self, message_id: str) -> bool:
        return str(message_id or "") in self._store

    def mark(self, message_id: str) -> None:
        mid = str(message_id or "")
        if mid and mid not in self._store:
            self._new.add(mid)

    def flush(self) -> bool:
        if not self._new:
            return False
        self._store |= self._new
        self._new.clear()
        self._write_log.append(1)  # one durable write performed this poll
        return True


class _LedgerSpyTransport(_FullInboundTransport):
    """Full inbound transport + an isolated in-memory ledger + AI-call spies.

    ``select_nda_attachments`` (gmail_triage selector) and
    ``classify_intake_attachment`` (gmail_intake classifier) are made "configured"
    and recorded, so a test can assert they are NOT invoked for a message the ledger
    skips. The ledger session is in-memory + per-transport, so polls are isolated and
    flush-writes are counted.
    """

    def __init__(self, inbox_size: int, *, import_limit: int) -> None:
        super().__init__(inbox_size, import_limit=import_limit)
        self._ledger_store: set[str] = set()
        self.ledger_write_log: list[int] = []
        # Spy tallies for the two AI calls the ledger skip must short-circuit.
        self.selector_calls: list[str] = []
        self.intake_calls: list[str] = []

    # -- ledger seam (in-memory, isolated, write-counted) ---------------- #
    def processed_ledger_session(self, owner_user_id: str = "") -> _InMemoryLedgerSession:
        return _InMemoryLedgerSession(self._ledger_store, self.ledger_write_log)

    def ledger_ids(self) -> set[str]:
        return set(self._ledger_store)

    # -- gmail_triage attachment-selector AI call (spied) ---------------- #
    def selector_configured(self) -> bool:
        return True

    def select_nda_attachments(self, *, message_metadata, candidates):
        # Record the call, then select every candidate (so the import proceeds).
        self.selector_calls.append(str(message_metadata.get("gmail_message_id") or ""))
        return {
            "status": "selected",
            "selected_attachment_ids": [str(c.get("attachment_id") or "") for c in candidates],
            "model": "spy-selector",
            "confidence": "100",
            "reason": "spy",
        }

    # -- gmail_intake classifier AI call (spied) ------------------------- #
    def intake_classifier_configured(self) -> bool:
        return True

    def gmail_intake_playbook(self) -> str:
        return "spy-playbook"

    def intake_classifier_model(self) -> str:
        return "spy-intake"

    def classify_intake_attachment(self, metadata, candidate, playbook):
        self.intake_calls.append(str(candidate.get("attachment_id") or ""))
        return {"status": "ok", "verdict": "NDA", "confidence": 0.99, "model": "spy-intake"}


class _StickyNonNdaTransport(_LedgerSpyTransport):
    """A transport whose every message has ONE attachment that is conclusively
    NON-NDA: the deterministic validation has no content basis and the selector does
    not select it, so the import path terminally skips it as ``non_nda_attachment``
    and imports NOTHING. This models the P1-1 sticky-message bug: without the ledger
    marking a fully-evaluated zero-import message, it re-runs gmail_triage +
    gmail_intake on EVERY poll and burns an import_limit slot forever.
    """

    def attachment_nda_validation(self, filename, paragraphs, *, message_metadata=None):
        # Conclusively NOT an NDA: no acceptance, no content basis, zero score. With
        # the selector configured-but-not-selecting, classify_attachment_lane -> skip.
        return {"accepted": False, "has_content_basis": False, "score": 0, "reason": "not an nda"}

    def select_nda_attachments(self, *, message_metadata, candidates):
        # Record the (gmail_triage) selector call, then select NOTHING -> the
        # candidate is demoted; with no content basis it terminally skips.
        self.selector_calls.append(str(message_metadata.get("gmail_message_id") or ""))
        return {
            "status": "selected",
            "selected_attachment_ids": [],
            "model": "spy-selector",
            "confidence": "0",
            "reason": "no nda",
        }

    def classify_intake_attachment(self, metadata, candidate, playbook):
        # The gmail_intake classifier ALSO judges it non-NDA, so the reconciled lane
        # is a terminal skip (a NOT_NDA intake over a non-confident deterministic
        # lane yields lane=skip in resolve_intake_lane).
        self.intake_calls.append(str(candidate.get("attachment_id") or ""))
        return {"status": "ok", "verdict": "NOT_NDA", "confidence": 0.99, "model": "spy-intake"}


class _TransientAttachmentFailureTransport(_LedgerSpyTransport):
    """The attachment download raises a transient GmailIntegrationError the first
    time, then succeeds -- modelling a flaky attachment fetch. The message must NOT
    be marked on the failing poll (its outcome is not stable), so it retries.
    """

    def __init__(self, inbox_size: int, *, import_limit: int) -> None:
        super().__init__(inbox_size, import_limit=import_limit)
        self._failed_once = False

    def attachment_bytes(self, service, message_id: str, attachment):
        if not self._failed_once:
            self._failed_once = True
            raise self.GmailIntegrationError("transient attachment download failure")
        return super().attachment_bytes(service, message_id, attachment)


def test_transient_attachment_failure_does_not_mark(import_limit_20):
    # A transient per-attachment download failure leaves the message UNMARKED
    # (stable_outcome is False), so it retries next poll and then imports.
    transport = _TransientAttachmentFailureTransport(inbox_size=1, import_limit=import_limit_20)

    r1 = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1",
    )
    assert r1["imported"] == []
    assert any(s.get("reason") == "attachment_unavailable" for s in r1["skipped"])
    # NOT marked -> no durable write, retries next poll.
    assert transport.ledger_ids() == set()
    assert transport.ledger_write_log == []

    r2 = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1",
    )
    assert len(r2["imported"]) == 1  # retried successfully
    assert transport.ledger_ids() == {"msg_000"}  # NOW marked (stable success)


def test_sticky_non_nda_message_marked_and_skipped_before_ai_next_poll(import_limit_20):
    # THE P1-1 GUARD. A message whose only attachment is conclusively non-NDA imports
    # nothing, but is FULLY EVALUATED -- it must be marked processed so the next poll
    # skips it BEFORE re-running the selector + intake AI calls.
    transport = _StickyNonNdaTransport(inbox_size=1, import_limit=import_limit_20)

    # POLL 1: evaluated, terminally skipped as non-NDA, NOTHING imported -- but the
    # selector (gmail_triage) AI call ran, and the message is now MARKED processed.
    result1 = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1",
    )
    assert result1["imported"] == []
    assert any(s.get("reason") == "non_nda_attachment" for s in result1["skipped"])
    assert transport.selector_calls == ["msg_000"]   # selector ran on poll 1
    assert transport.ledger_ids() == {"msg_000"}      # MARKED despite zero imports
    assert len(transport.ledger_write_log) == 1

    transport.selector_calls.clear()
    transport.intake_calls.clear()

    # POLL 2: the sticky message re-surfaces but is now skipped as processed_message
    # BEFORE the fetch + BEFORE the selector/intake AI calls. No more per-poll storm.
    result2 = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1",
    )
    assert any(s.get("reason") == "processed_message" for s in result2["skipped"])
    assert transport.selector_calls == []   # gmail_triage NOT re-run -- the fix
    assert transport.intake_calls == []     # gmail_intake NOT re-run -- the fix
    assert len(transport.ledger_write_log) == 1  # poll 2 marked nothing new -> no write


# --------------------------------------------------------------------------- #
# Integration: skip BEFORE any AI call, mark on success, write-once.
# --------------------------------------------------------------------------- #
def test_already_processed_message_skipped_before_any_ai_call(import_limit_20):
    transport = _LedgerSpyTransport(inbox_size=1, import_limit=import_limit_20)

    # POLL 1: the single new message is fetched, classified (both AI calls fire),
    # imported, and then MARKED processed.
    result1 = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1",
    )
    assert len(result1["imported"]) == 1
    assert transport.selector_calls == ["msg_000"]  # selector ran on poll 1
    assert transport.intake_calls == ["att_0"]      # intake classifier ran on poll 1
    assert transport.ledger_ids() == {"msg_000"}    # marked after the terminal import
    assert len(transport.ledger_write_log) == 1     # ledger written exactly once

    # Reset the spies; the message re-surfaces on poll 2 (no readonly exclusion).
    transport.selector_calls.clear()
    transport.intake_calls.clear()

    # POLL 2: the message is now in the ledger -> skipped BEFORE the fetch + BEFORE
    # both AI calls. THE CENTRAL GUARANTEE.
    result2 = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1",
    )
    processed_skips = [s for s in result2["skipped"] if s.get("reason") == "processed_message"]
    assert len(processed_skips) == 1
    assert result2["imported"] == []
    # NOT ONE AI call was made for the skipped message (the whole point: AI cost saved).
    assert transport.selector_calls == []
    assert transport.intake_calls == []
    # Poll 2 marked nothing new -> ZERO ledger writes this poll (write-once, no-op).
    assert len(transport.ledger_write_log) == 1  # unchanged from poll 1


def test_processed_skip_happens_before_messages_get_fetch(import_limit_20):
    # Prove the skip is BEFORE messages().get by counting get() calls. A skipped
    # message must never be fetched.
    transport = _LedgerSpyTransport(inbox_size=1, import_limit=import_limit_20)
    messages_api = transport.service.users_api.messages_api

    get_calls: list[str] = []
    original_get = messages_api.get

    def _counting_get(*, userId, id, format):
        get_calls.append(id)
        return original_get(userId=userId, id=id, format=format)

    messages_api.get = _counting_get  # type: ignore[assignment]

    gmail_matter_inbox.import_inbound_matters(transport=transport, limit=999, owner_user_id="owner_1")
    assert get_calls == ["msg_000"]  # fetched once on poll 1

    gmail_matter_inbox.import_inbound_matters(transport=transport, limit=999, owner_user_id="owner_1")
    # Poll 2: the ledger skip fired BEFORE get(), so no second fetch.
    assert get_calls == ["msg_000"]


def test_new_message_is_processed_and_marked(import_limit_20):
    transport = _LedgerSpyTransport(inbox_size=3, import_limit=import_limit_20)
    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1",
    )
    # All three new messages import and are marked processed in one poll, one write.
    assert len(result["imported"]) == 3
    assert transport.ledger_ids() == {"msg_000", "msg_001", "msg_002"}
    assert len(transport.ledger_write_log) == 1


def test_ledger_written_at_most_once_per_poll(import_limit_20):
    # A 20-message poll marks 20 ids but performs exactly ONE durable write.
    transport = _LedgerSpyTransport(inbox_size=20, import_limit=import_limit_20)
    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1",
    )
    assert len(result["imported"]) == 20
    assert len(transport.ledger_ids()) == 20
    assert len(transport.ledger_write_log) == 1  # NOT 20


# --------------------------------------------------------------------------- #
# Integration: a TRANSIENT failure must NOT mark the message.
# --------------------------------------------------------------------------- #
class _TransientGetFailureTransport(_LedgerSpyTransport):
    """A transport whose messages().get raises a transient (non-rate-limit) error
    the first time msg_000 is fetched, then succeeds. Models a flaky Gmail get():
    the message must NOT be marked processed on the failure, so it retries.
    """

    def __init__(self, inbox_size: int, *, import_limit: int) -> None:
        super().__init__(inbox_size, import_limit=import_limit)
        self._failed_once = False
        outer = self

        messages_api = self.service.users_api.messages_api
        original_get = messages_api.get

        def _flaky_get(*, userId, id, format):
            if id == "msg_000" and not outer._failed_once:
                outer._failed_once = True
                raise RuntimeError("transient gmail get failure")
            return original_get(userId=userId, id=id, format=format)

        messages_api.get = _flaky_get  # type: ignore[assignment]

    # The import path probes these on a get() error; a transient (non-429,
    # no-retry-after) error must fall through to a skip, NOT a mark.
    def gmail_retry_after_epoch(self, error: Exception) -> float:
        return 0.0

    def is_rate_limit_error(self, error: Exception) -> bool:
        return False

    def raise_gmail_api_error(self, error: Exception, fallback_message: str) -> None:
        # Not reached for this error (retry_after is 0), but keep it faithful.
        raise gmail_integration.GmailIntegrationError(fallback_message)


def test_transient_failure_does_not_mark_and_retries_next_poll(import_limit_20):
    transport = _TransientGetFailureTransport(inbox_size=1, import_limit=import_limit_20)

    # POLL 1: the get() raises a transient error -> message is recorded as a
    # transient skip (message_unavailable) and crucially is NOT marked processed.
    result1 = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1",
    )
    assert result1["imported"] == []
    assert any(s.get("reason") == "message_unavailable" for s in result1["skipped"])
    # NOT marked -> the ledger is empty and NO write happened (so it WILL retry).
    assert transport.ledger_ids() == set()
    assert transport.ledger_write_log == []

    # POLL 2: because it was never marked, the message is NOT skipped as processed;
    # it is fetched again, imported, and only NOW marked. The transient failure
    # correctly retried.
    result2 = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1",
    )
    assert len(result2["imported"]) == 1
    assert not any(s.get("reason") == "processed_message" for s in result2["skipped"])
    assert transport.ledger_ids() == {"msg_000"}
    assert len(transport.ledger_write_log) == 1


# --------------------------------------------------------------------------- #
# Integration: coexistence with the drain cursor.
# --------------------------------------------------------------------------- #
class _CursorAwareLedgerTransport(_LedgerSpyTransport):
    """Ledger-aware AND cursor-aware: each message carries an internalDate and the
    inbox honours a ``before:`` bound, so the real two-pass (head + drain) cursor
    logic runs WHILE the ledger is active. Proves the ledger skip does not stall the
    cursor's forward progress.
    """

    _BASE_SECONDS = 1_700_000_000

    def __init__(self, inbox_size: int, *, import_limit: int) -> None:
        super().__init__(inbox_size, import_limit=import_limit)
        self._drain_cursor = 0
        self.cursor_resets = 0
        # Rebuild the paged service with date-bearing, before:-honouring messages.
        ids = [f"msg_{i:03d}" for i in range(inbox_size)]
        self._internal_ms = {mid: (self._BASE_SECONDS - i) * 1000 for i, mid in enumerate(ids)}
        self.service.users_api.messages_api = _DatedPagedMessages(ids, self._internal_ms)

    # -- drain cursor (persistent in-memory across polls) ---------------- #
    def inbound_drain_cursor(self, owner_user_id: str = "") -> int:
        return self._drain_cursor

    def advance_inbound_drain_cursor(self, owner_user_id: str, internal_date_ms: int) -> int:
        candidate = int(internal_date_ms)
        if candidate <= 0:
            return self._drain_cursor
        if self._drain_cursor <= 0 or candidate < self._drain_cursor:
            self._drain_cursor = candidate
        return self._drain_cursor

    def reset_inbound_drain_cursor(self, owner_user_id: str = "") -> None:
        self._drain_cursor = 0
        self.cursor_resets += 1

    def message_internal_date_ms(self, message: dict) -> int:
        try:
            return max(0, int(str(message.get("internalDate") or "0")))
        except (TypeError, ValueError):
            return 0

    def inbound_query_before(self, base_query: str, cursor_internal_date_ms: int) -> str:
        if cursor_internal_date_ms <= 0:
            return base_query
        before_seconds = (cursor_internal_date_ms + 999) // 1000
        if before_seconds <= 0:
            return base_query
        return f"{base_query} before:{before_seconds}"


class _DatedPagedMessages:
    """Paged inbox whose list() honours ``before:<seconds>`` and whose get() returns
    each message's internalDate -- the minimum needed to exercise the drain cursor.
    """

    def __init__(self, message_ids: list[str], internal_ms: dict[str, int]) -> None:
        self.message_ids = message_ids
        self.internal_ms = internal_ms
        self.max_results_seen: list[int] = []

    def _before_seconds(self, q: str) -> int | None:
        for term in q.split():
            if term.startswith("before:"):
                try:
                    return int(term.split(":", 1)[1])
                except ValueError:
                    return None
        return None

    def list(self, *, userId: str, q: str, maxResults: int, pageToken: str = ""):
        self.max_results_seen.append(maxResults)
        before_seconds = self._before_seconds(q)
        if before_seconds is None:
            eligible = list(self.message_ids)
        else:
            eligible = [m for m in self.message_ids if self.internal_ms[m] < before_seconds * 1000]
        start = int(pageToken or "0")
        page = eligible[start:start + maxResults]
        next_start = start + len(page)
        next_token = str(next_start) if next_start < len(eligible) else ""
        return _Executable({"messages": [{"id": m} for m in page], "nextPageToken": next_token})

    def get(self, *, userId: str, id: str, format: str):
        return _Executable({"id": id, "payload": {}, "internalDate": str(self.internal_ms.get(id, 0))})


def test_ledger_coexists_with_cursor_drain_forward_progress(import_limit_20):
    # A 50-message backlog drains 20-at-a-time across polls. The ledger is active the
    # whole time; it must NOT stall the cursor's forward progress nor hide new mail.
    transport = _CursorAwareLedgerTransport(inbox_size=50, import_limit=import_limit_20)

    # POLL 1: head pass imports the newest 20 (msg_000..msg_019).
    r1 = gmail_matter_inbox.import_inbound_matters(transport=transport, limit=999, owner_user_id="owner_1")
    imported1 = {m["id"] for m in r1["imported"]}
    assert len(imported1) == 20
    matters1 = {
        m["gmail_message_id"]
        for m in transport.repository.list_matters(owner_user_id="owner_1")
    }
    assert matters1 == {f"msg_{i:03d}" for i in range(20)}
    assert transport.ledger_ids() == {f"msg_{i:03d}" for i in range(20)}

    # POLL 2: forward progress -- the NEXT 20 (msg_020..msg_039) import. The first 20
    # re-surface but are skipped (ledger 'processed_message' and/or dedup
    # 'already_imported'); either way they do not block reaching the new batch.
    r2 = gmail_matter_inbox.import_inbound_matters(transport=transport, limit=999, owner_user_id="owner_1")
    matters2 = {
        m["gmail_message_id"]
        for m in transport.repository.list_matters(owner_user_id="owner_1")
    }
    assert matters2 == {f"msg_{i:03d}" for i in range(40)}  # 20 new ones added -> progress
    assert len(r2["imported"]) == 20

    # POLL 3: drains the final 10 (msg_040..msg_049). The backlog is fully drained
    # with NO silent tail-drop, ledger active throughout.
    r3 = gmail_matter_inbox.import_inbound_matters(transport=transport, limit=999, owner_user_id="owner_1")
    matters3 = {
        m["gmail_message_id"]
        for m in transport.repository.list_matters(owner_user_id="owner_1")
    }
    assert matters3 == {f"msg_{i:03d}" for i in range(50)}  # every message drained
    assert len(r3["imported"]) == 10
    # The whole 50-message backlog is now in the ledger.
    assert transport.ledger_ids() == {f"msg_{i:03d}" for i in range(50)}
