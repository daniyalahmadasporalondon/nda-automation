"""Tests for the flag-gated Gmail History-API incremental inbound sync.

This subsystem is the MOST storm-prone in the codebase; the #1 historical failure is
SILENTLY DROPPING a real NDA. Every case below names the load-bearing invariant it
pins. The two MERGE-BLOCKERS are:

* CASE 5 -- a transient per-message failure must NOT advance the historyId frontier
  (the catastrophic-loss guard: advancing past an un-handled message makes
  history.list never surface it again).
* CASE 7 -- flag OFF is byte-identical current behaviour: history.list is NEVER
  called and the frontier store is NEVER touched.

The transport fakes extend the ledger/cursor-aware ``_CursorAwareLedgerTransport``
from tests.test_gmail_processed_ledger (real heavy-import path against an in-memory
repository), adding:
  * an in-memory per-owner historyId + poll_count store (the frontier);
  * ``history_sync_enabled`` / ``history_fullsweep_every`` (env-driven, overridable);
  * a ``_ScriptedHistory`` fake driving ``history_list`` with scripted messagesAdded
    pages and an EXPIRY mode that raises a fake 404 HttpError;
  * ``profile_history_id`` (a settable mailbox head).

The unit round-trip tests (case 12) drive the real ``matter_store`` seams directly.
"""

from __future__ import annotations

import importlib
import os

import pytest

from nda_automation import gmail_integration, gmail_matter_inbox

from tests.test_gmail_processed_ledger import (
    _CursorAwareLedgerTransport,
    _DatedPagedMessages,
)
from tests.test_gmail_transport import _Executable


# --------------------------------------------------------------------------- #
# Fakes: a fake 404 HttpError, a scripted history listing, and a history-aware
# inbound transport.
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, status: int) -> None:
        self.status = status


class _FakeHttpError(Exception):
    """Mimics googleapiclient.errors.HttpError enough for the status probes:
    ``error.resp.status`` is what both is_history_expired_error and the get-404
    terminal check read."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.resp = _FakeResp(status)


class _ScriptedHistory:
    """A scripted ``history_list`` source.

    ``pages`` is a list of response dicts shaped like the real Gmail
    ``users.history.list`` payload::

        {"history": [{"messagesAdded": [{"message": {"id": ..., "labelIds": [...]}}]}],
         "historyId": "<head>", "nextPageToken": "<tok or ''>"}

    Records every call (so a test can assert paging) and how many times it was hit.
    When ``expire`` is True, the FIRST call raises a fake 404 HttpError (the
    startHistoryId-aged-out path). ``pages_for`` lets a later poll return a different
    script keyed by the startHistoryId it is called with (so a re-seeded frontier can
    drive a fresh incremental page).
    """

    def __init__(
        self,
        pages: list[dict] | None = None,
        *,
        expire: bool = False,
        pages_by_start: dict[str, list[dict]] | None = None,
    ) -> None:
        self.pages = list(pages or [])
        self.expire = expire
        self.pages_by_start = dict(pages_by_start or {})
        self.calls: list[dict] = []
        self.call_count = 0

    def list_pages(self, start_history_id: str, page_token: str) -> dict:
        self.call_count += 1
        self.calls.append({"start": start_history_id, "page_token": page_token})
        if self.expire:
            raise _FakeHttpError(404)
        script = self.pages_by_start.get(str(start_history_id), self.pages)
        index = int(page_token or "0")
        if index >= len(script):
            return {"history": [], "historyId": start_history_id, "nextPageToken": ""}
        return script[index]


class _Get404Messages(_DatedPagedMessages):
    """Dated paged inbox whose get() raises a fake 404 (or 5xx) for chosen ids."""

    def __init__(self, message_ids, internal_ms, *, deleted_ids=None, error_ids=None) -> None:
        super().__init__(message_ids, internal_ms)
        self._deleted_ids = set(deleted_ids or [])
        self._error_ids = set(error_ids or [])

    def get(self, *, userId: str, id: str, format: str):
        if id in self._deleted_ids:
            raise _FakeHttpError(404)
        if id in self._error_ids:
            raise _FakeHttpError(503)
        return _Executable({"id": id, "payload": {}, "internalDate": str(self.internal_ms.get(id, 0))})


class _FaithfulLedgerSession:
    """An in-memory ProcessedLedgerSession stand-in with the REAL semantics the
    history-advance gate depends on: a ``mark`` is visible to ``is_processed`` IMMEDIATELY
    (same poll), not only after ``flush`` -- the production ProcessedLedgerSession keeps
    marks in ``self._known`` at mark time. Backed by a per-transport durable set +
    attempts dict so state survives across polls. Supports the attempt/quarantine seams
    so a transient failure counts + quarantines exactly like production."""

    def __init__(self, store: set[str], attempts: dict[str, int], write_log: list[int]) -> None:
        self._store = store
        self._attempts = attempts
        self._write_log = write_log
        self._dirty = False

    def is_processed(self, message_id: str) -> bool:
        return str(message_id or "") in self._store

    def mark(self, message_id: str) -> None:
        mid = str(message_id or "")
        if not mid:
            return
        self._attempts.pop(mid, None)
        if mid not in self._store:
            self._store.add(mid)
        self._dirty = True

    def attempt_count(self, message_id: str) -> int:
        return int(self._attempts.get(str(message_id or ""), 0))

    def record_attempt(self, message_id: str) -> int:
        mid = str(message_id or "")
        self._attempts[mid] = int(self._attempts.get(mid, 0)) + 1
        self._dirty = True
        return self._attempts[mid]

    def quarantine(self, message_id: str, *, reason: str = "", attempts: int = 0, filename: str = "") -> None:
        self.mark(message_id)

    def flush(self) -> bool:
        if not self._dirty:
            return False
        self._dirty = False
        self._write_log.append(1)
        return True


class _HistoryInboundTransport(_CursorAwareLedgerTransport):
    """Cursor/ledger-aware inbound transport + the History-API seams.

    The frontier (historyId + poll_count) lives in an in-memory per-owner dict, exactly
    like the drain cursor. ``history_list`` is driven by an attached ``_ScriptedHistory``
    (no service.history() fake needed -- the seam is overridden). The flag + cadence are
    forced ON/here so a test does not depend on process env, but a test may still set the
    real env vars and clear these overrides to exercise the real gates.
    """

    def __init__(
        self,
        inbox_size: int,
        *,
        import_limit: int,
        scripted_history: _ScriptedHistory | None = None,
        profile_head: str = "",
        fullsweep_every: int = 6,
        enabled: bool = True,
        get_messages: _DatedPagedMessages | None = None,
    ) -> None:
        super().__init__(inbox_size, import_limit=import_limit)
        self._history: dict[str, dict] = {}
        self._scripted_history = scripted_history
        self._profile_head = profile_head
        self._fullsweep_every = fullsweep_every
        self._enabled = enabled
        # A faithful in-memory ledger (marks visible immediately, like production) so
        # the history-advance gate's all_handled check is exercised honestly. Replaces
        # the deferred-visibility _InMemoryLedgerSession the base class installs.
        self._attempts_store: dict[str, int] = {}
        # Count profile_history_id calls so a test can prove the re-seed uses the
        # already-fetched profile (no extra getProfile round-trip beyond the poll's).
        self.profile_head_calls = 0
        if get_messages is not None:
            self.service.users_api.messages_api = get_messages

    def processed_ledger_session(self, owner_user_id: str = "") -> _FaithfulLedgerSession:
        return _FaithfulLedgerSession(self._ledger_store, self._attempts_store, self.ledger_write_log)

    # -- flag + cadence -------------------------------------------------- #
    def history_sync_enabled(self, owner_user_id: str = "") -> bool:
        return bool(self._enabled)

    def history_fullsweep_every(self) -> int:
        return int(self._fullsweep_every)

    # -- frontier store (in-memory, per-owner, survives across polls) ----- #
    def inbound_history_id(self, owner_user_id: str = "") -> str:
        entry = self._history.get(owner_user_id) or {}
        return str(entry.get("history_id") or "")

    def set_inbound_history_id(self, owner_user_id: str, history_id: str) -> str:
        value = str(history_id or "").strip()
        entry = dict(self._history.get(owner_user_id) or {})
        if not value:
            entry.pop("history_id", None)
        else:
            entry["history_id"] = value
        if entry:
            self._history[owner_user_id] = entry
        else:
            self._history.pop(owner_user_id, None)
        return value

    def inbound_history_poll_count(self, owner_user_id: str = "") -> int:
        entry = self._history.get(owner_user_id) or {}
        return int(entry.get("poll_count") or 0)

    def bump_inbound_history_poll_count(self, owner_user_id: str = "") -> int:
        entry = dict(self._history.get(owner_user_id) or {})
        new_value = int(entry.get("poll_count") or 0) + 1
        entry["poll_count"] = new_value
        self._history[owner_user_id] = entry
        return new_value

    def reset_inbound_history(self, owner_user_id: str = "") -> None:
        self._history.pop(owner_user_id, None)

    # -- history listing + expiry probe ---------------------------------- #
    def history_list(self, service, *, start_history_id, label_id="INBOX",
                     history_types=("messageAdded",), page_token=""):
        assert self._scripted_history is not None, "test must attach a scripted history"
        return self._scripted_history.list_pages(str(start_history_id), str(page_token))

    def is_history_expired_error(self, error: Exception) -> bool:
        return getattr(getattr(error, "resp", None), "status", None) == 404

    def is_rate_limit_error(self, error: Exception) -> bool:
        # A 429 is signalled by the real retry-after probe over a fake HttpError; here
        # we treat a fake 429-status HttpError as rate-limited and everything else not.
        return getattr(getattr(error, "resp", None), "status", None) == 429

    def gmail_retry_after_epoch(self, error: Exception) -> float:
        # The get() handler consults this to decide whether a non-404 error is a
        # retry-after (429-ish) escalation. A fake 429 returns a truthy epoch; every
        # other status (503/etc.) returns 0.0 so it becomes a transient skip.
        return 1.0 if getattr(getattr(error, "resp", None), "status", None) == 429 else 0.0

    def raise_gmail_api_error(self, error: Exception, fallback_message: str) -> None:
        # Delegate to the real escalator so a generic (non-429/non-404) history.list
        # error surfaces as the production GmailIntegrationError, exactly as the
        # window-scan list() path does.
        gmail_integration._raise_gmail_api_error(error, fallback_message)

    # -- re-seed source (already-fetched profile head) ------------------- #
    def profile_history_id(self, role: str = "inbound", *, service=None, owner_user_id: str = "") -> str:
        self.profile_head_calls += 1
        return str(self._profile_head or "")

    # -- test helpers ---------------------------------------------------- #
    def frontier(self, owner: str = "owner_1") -> str:
        return self.inbound_history_id(owner)

    def poll_count(self, owner: str = "owner_1") -> int:
        return self.inbound_history_poll_count(owner)

    def list_calls(self) -> int:
        return self.service.users_api.messages_api.list_calls if hasattr(
            self.service.users_api.messages_api, "list_calls"
        ) else 0


def _added(mid: str, *, inbox: bool = True) -> dict:
    labels = ["INBOX"] if inbox else ["SENT"]
    return {"message": {"id": mid, "labelIds": labels}}


def _page(added_ids, *, head: str, next_token: str = "", inbox: bool = True) -> dict:
    return {
        "history": [{"messagesAdded": [_added(m, inbox=inbox) for m in added_ids]}],
        "historyId": head,
        "nextPageToken": next_token,
    }


class _CountingListMessages(_DatedPagedMessages):
    """Dated paged inbox that COUNTS messages().list calls so a test can prove the
    incremental path never lists (case 1) and the fallback path does (case 2/3)."""

    def __init__(self, message_ids, internal_ms) -> None:
        super().__init__(message_ids, internal_ms)
        self.list_calls = 0

    def list(self, *, userId: str, q: str, maxResults: int, pageToken: str = ""):
        self.list_calls += 1
        return super().list(userId=userId, q=q, maxResults=maxResults, pageToken=pageToken)


def _dated_messages(inbox_size: int, cls=_CountingListMessages, **kw):
    base = _CursorAwareLedgerTransport._BASE_SECONDS
    ids = [f"msg_{i:03d}" for i in range(inbox_size)]
    internal_ms = {mid: (base - i) * 1000 for i, mid in enumerate(ids)}
    return cls(ids, internal_ms, **kw)


@pytest.fixture
def import_limit_20(monkeypatch):
    monkeypatch.setenv(gmail_integration.NDA_GMAIL_IMPORT_LIMIT_ENV, "20")
    monkeypatch.setattr(
        gmail_integration,
        "MAX_GMAIL_IMPORT_LIMIT",
        gmail_integration._gmail_import_limit_from_env(),
    )
    assert gmail_integration.MAX_GMAIL_IMPORT_LIMIT == 20
    return 20


def _imported_ids(result) -> set[str]:
    # ``imported`` items are matter records: identity is the gmail_message_id (the
    # ``id`` field is the generated matter id), matching the existing e2e tests.
    return {str(m.get("gmail_message_id") or m.get("id") or "") for m in result["imported"]}


def _matter_ids(transport, owner: str = "owner_1") -> set[str]:
    return {
        m["gmail_message_id"] for m in transport.repository.list_matters(owner_user_id=owner)
    }


# --------------------------------------------------------------------------- #
# CASE 1 -- Steady-state incremental imports only new mail; messages().list NEVER
# called; frontier advances to the mailbox head.
# --------------------------------------------------------------------------- #
def test_case1_incremental_imports_only_new_mail_no_list_call(import_limit_20):
    getter = _dated_messages(3)  # 3 messages exist; get() serves any of them
    history = _ScriptedHistory([_page(["msg_000", "msg_001"], head="5000")])
    transport = _HistoryInboundTransport(
        3, import_limit=import_limit_20, scripted_history=history,
        profile_head="4000", get_messages=getter,
    )
    # Seed a frontier so the incremental path is taken this poll.
    transport.set_inbound_history_id("owner_1", "1000")

    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1",
    )

    # Only the two messagesAdded ids imported (the 3rd never appeared in history).
    assert _imported_ids(result) == {"msg_000", "msg_001"}
    assert _matter_ids(transport) == {"msg_000", "msg_001"}
    # THE ASSERTION: the incremental path did NOT touch messages().list at all.
    assert getter.list_calls == 0, "incremental path must not page messages().list"
    assert history.call_count == 1
    # Frontier advanced to the mailbox head at read time; poll_count bumped once.
    assert transport.frontier() == "5000"
    assert transport.poll_count() == 1


# --------------------------------------------------------------------------- #
# CASE 2 -- 404 expiry -> fallback window-scan runs; frontier reset then re-seeded;
# no exception escapes.
# --------------------------------------------------------------------------- #
def test_case2_history_expired_falls_back_and_reseeds(import_limit_20):
    getter = _dated_messages(2)
    history = _ScriptedHistory(expire=True)  # first history.list raises 404
    transport = _HistoryInboundTransport(
        2, import_limit=import_limit_20, scripted_history=history,
        profile_head="9999", get_messages=getter,
    )
    transport.set_inbound_history_id("owner_1", "1000")

    # No exception escapes; the window-scan imports the current inbox.
    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1",
    )
    assert _imported_ids(result) == {"msg_000", "msg_001"}
    # The window-scan DID page messages().list (the fallback path).
    assert getter.list_calls >= 1
    # Frontier was reset by the 404 then re-seeded from the profile head at poll end.
    assert transport.frontier() == "9999"


# --------------------------------------------------------------------------- #
# CASE 3 -- No stored frontier -> fallback window-scan runs + seeds from profile;
# a 2nd poll then goes incremental.
# --------------------------------------------------------------------------- #
def test_case3_no_frontier_seeds_then_second_poll_incremental(import_limit_20):
    getter = _dated_messages(2)
    # Poll 2 (after the seed to "7000") returns one NEW messagesAdded id.
    history = _ScriptedHistory(pages_by_start={"7000": [_page(["msg_000"], head="7100")]})
    transport = _HistoryInboundTransport(
        2, import_limit=import_limit_20, scripted_history=history,
        profile_head="7000", get_messages=getter,
    )
    # No frontier -> first poll is the window-scan seed.
    assert transport.frontier() == ""

    r1 = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1",
    )
    assert _imported_ids(r1) == {"msg_000", "msg_001"}  # window-scan imported both
    assert history.call_count == 0, "no history.list on the seeding poll"
    assert transport.frontier() == "7000"  # seeded from profile head
    list_after_poll1 = getter.list_calls
    assert list_after_poll1 >= 1

    # POLL 2: frontier present -> incremental. Both messages already imported+ledgered,
    # so the incremental candidate is skipped (dedup/processed), no new matter, and the
    # frontier still advances (all handled, clean).
    r2 = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1",
    )
    assert history.call_count == 1, "poll 2 went incremental (history.list called once)"
    assert getter.list_calls == list_after_poll1, "poll 2 did NOT page messages().list"
    assert r2["imported"] == []
    assert transport.frontier() == "7100"


# --------------------------------------------------------------------------- #
# CASE 4 -- get-404 (deleted) is TERMINAL: 'message_deleted' skip, no matter, AND the
# frontier STILL advances. Contrast: get-5xx -> frontier NOT advanced.
# --------------------------------------------------------------------------- #
def test_case4_get_404_deleted_is_terminal_frontier_advances(import_limit_20):
    getter = _dated_messages(3, cls=_Get404Messages, deleted_ids={"msg_001"})
    # History yields two ids; msg_001 is gone (get 404), msg_000 imports fine.
    history = _ScriptedHistory([_page(["msg_000", "msg_001"], head="5000")])
    transport = _HistoryInboundTransport(
        3, import_limit=import_limit_20, scripted_history=history,
        profile_head="4000", get_messages=getter,
    )
    transport.set_inbound_history_id("owner_1", "1000")

    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1",
    )
    assert _imported_ids(result) == {"msg_000"}  # only the live one
    assert _matter_ids(transport) == {"msg_000"}
    assert any(s.get("reason") == "message_deleted" for s in result["skipped"])
    # TERMINAL: the deleted id was ledger-marked, so all_handled is True and the
    # frontier advances past it (else one deleted id pins the frontier forever).
    assert transport.frontier() == "5000"


def test_case4b_get_5xx_transient_does_not_advance_frontier(import_limit_20):
    getter = _dated_messages(3, cls=_Get404Messages, error_ids={"msg_001"})
    history = _ScriptedHistory([_page(["msg_000", "msg_001"], head="5000")])
    transport = _HistoryInboundTransport(
        3, import_limit=import_limit_20, scripted_history=history,
        profile_head="4000", get_messages=getter,
    )
    transport.set_inbound_history_id("owner_1", "1000")

    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1",
    )
    assert _imported_ids(result) == {"msg_000"}
    assert any(s.get("reason") == "message_unavailable" for s in result["skipped"])
    # msg_001 hit a transient 5xx and is UNMARKED -> all_handled False -> frontier
    # left UNCHANGED so the next poll re-lists + retries it.
    assert transport.frontier() == "1000", "transient get-5xx must NOT advance the frontier"


# --------------------------------------------------------------------------- #
# CASE 5 (MERGE-BLOCKER) -- a transient per-message import failure leaves the id
# unmarked, the frontier UNCHANGED, and the next poll re-lists + retries.
# --------------------------------------------------------------------------- #
class _TransientThenOkHistoryTransport(_HistoryInboundTransport):
    """The message's heavy import fails (transient) on the FIRST attempt, then
    succeeds. Models a review_failed / extraction blip whose stable_outcome is False:
    the id must stay unmarked and the frontier must NOT advance past it."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._attempts: dict[str, int] = {}

    def extract_document_paragraphs(self, filename: str, document_bytes: bytes):
        self._attempts[filename] = self._attempts.get(filename, 0) + 1
        if self._attempts[filename] == 1:
            # A transient extraction failure -> import_inbound_attachments records a
            # non-stable outcome (the message is left unmarked to retry).
            raise gmail_integration.PdfExtractionError("transient blip")
        return super().extract_document_paragraphs(filename, document_bytes)


def test_case5_transient_failure_does_not_advance_frontier(import_limit_20):
    getter = _dated_messages(1)
    history = _ScriptedHistory(
        pages_by_start={
            "1000": [_page(["msg_000"], head="5000")],  # poll 1: fails transiently
            "5000": [_page(["msg_000"], head="5000")],  # (unused; frontier stays 1000)
        }
    )
    transport = _TransientThenOkHistoryTransport(
        1, import_limit=import_limit_20, scripted_history=history,
        profile_head="4000", get_messages=getter,
    )
    transport.set_inbound_history_id("owner_1", "1000")

    # POLL 1: the sole candidate fails its extraction (transient) -> unmarked.
    r1 = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1",
    )
    assert r1["imported"] == []
    assert transport.ledger_ids() == set(), "transient failure leaves the id UNMARKED"
    # THE MERGE-BLOCKER ASSERTION: the frontier did NOT advance past the un-handled id.
    assert transport.frontier() == "1000", "must not advance past a transiently-failed message"
    assert transport.poll_count() == 0, "poll_count not bumped on a non-clean poll"

    # POLL 2: same frontier -> re-lists the SAME id -> now succeeds -> marked -> the
    # frontier finally advances.
    r2 = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1",
    )
    assert _imported_ids(r2) == {"msg_000"}, "retried successfully next poll"
    assert transport.ledger_ids() == {"msg_000"}
    assert transport.frontier() == "5000", "frontier advances once the id is handled"


# --------------------------------------------------------------------------- #
# CASE 6 -- Full-sweep (fullsweep_every=3): a current-INBOX message that NEVER appears
# in any messagesAdded page is imported on the full-sweep poll (poll 3).
# --------------------------------------------------------------------------- #
def test_case6_fullsweep_catches_relabeled_mail(import_limit_20):
    # 2 messages live in the inbox. History only ever surfaces msg_000 (msg_001 was
    # re-labeled INTO the inbox, which messagesAdded never fires for). The full-sweep
    # window-scan (poll 3) is the only thing that catches msg_001.
    getter = _dated_messages(2)
    history = _ScriptedHistory(
        pages_by_start={
            "1000": [_page(["msg_000"], head="1000")],  # steady frontier: only msg_000
        }
    )
    transport = _HistoryInboundTransport(
        2, import_limit=import_limit_20, scripted_history=history,
        profile_head="1000", fullsweep_every=3, get_messages=getter,
    )
    transport.set_inbound_history_id("owner_1", "1000")

    # POLL 1 (incremental, poll_count 0->1): imports msg_000 only.
    r1 = gmail_matter_inbox.import_inbound_matters(transport=transport, limit=999, owner_user_id="owner_1")
    assert _imported_ids(r1) == {"msg_000"}
    assert transport.poll_count() == 1
    assert getter.list_calls == 0

    # POLL 2 (incremental, poll_count 1->2): msg_000 already handled; nothing new.
    r2 = gmail_matter_inbox.import_inbound_matters(transport=transport, limit=999, owner_user_id="owner_1")
    assert r2["imported"] == []
    assert transport.poll_count() == 2
    assert getter.list_calls == 0
    assert "msg_001" not in _matter_ids(transport), "incremental never sees the re-labeled msg_001"

    # POLL 3 ((2+1)%3==0 -> FULL-SWEEP): the window-scan finally imports msg_001.
    r3 = gmail_matter_inbox.import_inbound_matters(transport=transport, limit=999, owner_user_id="owner_1")
    assert getter.list_calls >= 1, "full-sweep pages messages().list"
    assert _matter_ids(transport) == {"msg_000", "msg_001"}
    assert "msg_001" in _imported_ids(r3)
    # After a full-sweep the cadence restarts cleanly: poll_count reset to 0 and the
    # frontier re-seeded from the profile head so the NEXT poll is incremental again.
    assert transport.poll_count() == 0, "full-sweep resets the cadence counter"
    assert transport.frontier() == "1000", "full-sweep re-seeds the frontier from profile head"


def test_fullsweep_cadence_repeats_every_n_polls(import_limit_20):
    # With fullsweep_every=3 and everything already handled, prove the sweep recurs on
    # a clean 3-poll cycle: polls 3 and 6 are full-sweeps (list() called), the rest are
    # incremental (list() untouched).
    getter = _dated_messages(1)
    history = _ScriptedHistory(pages_by_start={"1000": [_page(["msg_000"], head="1000")]})
    transport = _HistoryInboundTransport(
        1, import_limit=import_limit_20, scripted_history=history,
        profile_head="1000", fullsweep_every=3, get_messages=getter,
    )
    transport.set_inbound_history_id("owner_1", "1000")

    sweep_polls = []
    for poll in range(1, 7):
        before = getter.list_calls
        gmail_matter_inbox.import_inbound_matters(transport=transport, limit=999, owner_user_id="owner_1")
        if getter.list_calls > before:
            sweep_polls.append(poll)
    assert sweep_polls == [3, 6], f"full-sweep must recur every 3 polls, got {sweep_polls}"


def test_fullsweep_cannot_be_disabled_clamps_to_default(import_limit_20):
    # A <1 cadence must clamp to the default (the gap-closer can't be turned off).
    transport = _HistoryInboundTransport(
        1, import_limit=import_limit_20, scripted_history=_ScriptedHistory(),
        fullsweep_every=0,
    )
    assert gmail_matter_inbox._history_fullsweep_every(transport) == 6


# --------------------------------------------------------------------------- #
# CASE 7 (MERGE-BLOCKER) -- Flag OFF: history_list NEVER called, frontier store NEVER
# touched, result identical to a no-history transport.
# --------------------------------------------------------------------------- #
def test_case7_flag_off_is_noop_history_never_called(import_limit_20):
    getter = _dated_messages(2)
    history = _ScriptedHistory([_page(["msg_000"], head="5000")])
    transport = _HistoryInboundTransport(
        2, import_limit=import_limit_20, scripted_history=history,
        profile_head="4000", get_messages=getter, enabled=False,  # FLAG OFF
    )
    # Even a pre-existing frontier must be ignored while the flag is off.
    transport.set_inbound_history_id("owner_1", "1000")

    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1",
    )
    # Behaves like the plain window-scan: both current messages import.
    assert _imported_ids(result) == {"msg_000", "msg_001"}
    assert getter.list_calls >= 1, "flag-off path uses the window-scan (messages().list)"
    # THE ASSERTIONS: history.list never called; the frontier store is untouched.
    assert history.call_count == 0, "flag OFF must never call history.list"
    assert transport.frontier() == "1000", "flag OFF must not touch the frontier"
    assert transport.poll_count() == 0
    assert transport.profile_head_calls == 0, "flag OFF must not re-seed"


def test_case7_default_env_is_off_via_real_transport():
    # The REAL transport default: with the env unset, history_sync_enabled is False.
    from nda_automation import gmail_transport as gt

    os.environ.pop("NDA_GMAIL_HISTORY_SYNC_ENABLED", None)
    assert gt.GmailTransport().history_sync_enabled("owner_1") is False


class _RealStoreHistoryTransport(_CursorAwareLedgerTransport):
    """Flag-gated inbound transport whose frontier + flag seams delegate to the REAL
    ``matter_store`` (via ``DiskMatterRepository``), so ANY store write while the flag
    is off would create ``gmail_inbound_history.json`` on disk. Used to prove the
    byte-identical-off guarantee at the store layer, not just at the API layer."""

    def __init__(self, inbox_size: int, *, import_limit: int, get_messages=None) -> None:
        super().__init__(inbox_size, import_limit=import_limit)
        if get_messages is not None:
            self.service.users_api.messages_api = get_messages

    def _repo(self):
        from nda_automation.matter_repository import DiskMatterRepository

        return DiskMatterRepository()

    def history_sync_enabled(self, owner_user_id: str = "") -> bool:
        from nda_automation import gmail_transport as gt

        return gt.GmailTransport().history_sync_enabled(owner_user_id)

    def history_fullsweep_every(self) -> int:
        from nda_automation import gmail_transport as gt

        return gt.GmailTransport().history_fullsweep_every()

    def history_list(self, service, **kwargs):  # pragma: no cover - never reached when OFF
        raise AssertionError("history_list must never be called while the flag is OFF")

    def inbound_history_id(self, owner_user_id: str = "") -> str:
        return str(self._repo().gmail_inbound_history_id(owner_user_id=owner_user_id))

    def set_inbound_history_id(self, owner_user_id: str, history_id: str) -> str:
        return str(self._repo().set_gmail_inbound_history_id(owner_user_id, history_id))

    def inbound_history_poll_count(self, owner_user_id: str = "") -> int:
        return int(self._repo().gmail_inbound_history_poll_count(owner_user_id=owner_user_id))

    def bump_inbound_history_poll_count(self, owner_user_id: str = "") -> int:
        return int(self._repo().bump_gmail_inbound_history_poll_count(owner_user_id=owner_user_id))

    def reset_inbound_history(self, owner_user_id: str = "") -> None:
        self._repo().reset_gmail_inbound_history(owner_user_id=owner_user_id)

    def profile_history_id(self, role: str = "inbound", *, service=None, owner_user_id: str = "") -> str:
        raise AssertionError("profile_history_id must never be called while the flag is OFF")


def test_case7_flag_off_never_creates_history_store_file(tmp_path, monkeypatch, import_limit_20):
    # BYTE-IDENTICAL-OFF at the STORE layer: with the flag off, the real store seams
    # must never be exercised, so gmail_inbound_history.json is never created on disk.
    import nda_automation.matter_store as ms

    monkeypatch.setattr(ms, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ms, "GMAIL_INBOUND_HISTORY_PATH", tmp_path / "gmail_inbound_history.json")
    monkeypatch.delenv("NDA_GMAIL_HISTORY_SYNC_ENABLED", raising=False)  # flag OFF

    getter = _dated_messages(2)
    transport = _RealStoreHistoryTransport(2, import_limit=import_limit_20, get_messages=getter)

    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1",
    )
    # Window-scan behaviour, and -- critically -- NO history store file was created.
    assert _imported_ids(result) == {"msg_000", "msg_001"}
    assert getter.list_calls >= 1
    assert not (tmp_path / "gmail_inbound_history.json").exists(), (
        "flag OFF must never create the history store file"
    )
    assert ms.gmail_inbound_history_id("owner_1") == ""


# --------------------------------------------------------------------------- #
# CASE 9 -- import_limit=1, history yields 3 new -> only 1 imported, frontier NOT
# advanced (budget-truncated = not clean); next poll drains the rest.
# --------------------------------------------------------------------------- #
@pytest.fixture
def import_limit_1(monkeypatch):
    monkeypatch.setenv(gmail_integration.NDA_GMAIL_IMPORT_LIMIT_ENV, "1")
    monkeypatch.setattr(
        gmail_integration,
        "MAX_GMAIL_IMPORT_LIMIT",
        gmail_integration._gmail_import_limit_from_env(),
    )
    assert gmail_integration.MAX_GMAIL_IMPORT_LIMIT == 1
    return 1


def test_case9_budget_truncated_does_not_advance_frontier(import_limit_1):
    getter = _dated_messages(3)
    history = _ScriptedHistory(
        pages_by_start={"1000": [_page(["msg_000", "msg_001", "msg_002"], head="5000")]}
    )
    transport = _HistoryInboundTransport(
        3, import_limit=import_limit_1, scripted_history=history,
        profile_head="4000", get_messages=getter,
    )
    transport.set_inbound_history_id("owner_1", "1000")

    r1 = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1",
    )
    # Only ONE new message imported (import_limit=1); the other two are un-handled.
    assert len(r1["imported"]) == 1
    assert r1["new_processed"] == 1
    # Budget-truncated == NOT clean -> the frontier must NOT advance (else the two
    # un-imported ids are lost -- history.list would never surface them again).
    assert transport.frontier() == "1000", "budget-truncated poll must not advance the frontier"
    assert transport.poll_count() == 0


# --------------------------------------------------------------------------- #
# CASE 10 -- 429 during history.list page 2: rate_limited=True, page-1 imports kept,
# frontier unchanged (not reset, not advanced).
# --------------------------------------------------------------------------- #
class _RateLimitOnPage2History(_ScriptedHistory):
    """Returns page 1 normally, then raises a fake 429 on the second list_pages call."""

    def list_pages(self, start_history_id: str, page_token: str) -> dict:
        self.call_count += 1
        self.calls.append({"start": start_history_id, "page_token": page_token})
        if (page_token or "0") != "0":
            raise _FakeHttpError(429)
        # Page 1: one id + a next-page token so the caller pages again.
        return _page(["msg_000"], head="5000", next_token="1")


def test_case10_429_midlist_keeps_page1_frontier_unchanged(import_limit_20):
    getter = _dated_messages(3)
    history = _RateLimitOnPage2History()
    transport = _HistoryInboundTransport(
        3, import_limit=import_limit_20, scripted_history=history,
        profile_head="4000", get_messages=getter,
    )
    transport.set_inbound_history_id("owner_1", "1000")

    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1",
    )
    # Page-1 candidate still imported (we do not discard work on a mid-list 429).
    assert _imported_ids(result) == {"msg_000"}
    assert result["rate_limited"] is True
    # Frontier neither RESET (that's the 404 path) nor ADVANCED (not clean).
    assert transport.frontier() == "1000", "a mid-list 429 leaves the frontier unchanged"
    assert transport.poll_count() == 0
    # A 429 is not an expiry: the frontier store still holds the same id (not cleared).
    assert transport.inbound_history_id("owner_1") == "1000"


# --------------------------------------------------------------------------- #
# CASE 11 -- Active first-sync backfill wins: the window-scan branch runs even with
# the flag ON, until the backfill completes.
# --------------------------------------------------------------------------- #
class _BackfillHistoryTransport(_HistoryInboundTransport):
    """Flag ON + an ACTIVE first-sync backfill window. The backfill must WIN: the
    incremental path is deferred (history.list never called) until it completes."""

    def inbound_backfill_state(self, owner_user_id: str = ""):
        return {"effective_window_days": 7, "target_days": 30, "completed_through_days": 0}

    def inbound_query_for_window(self, window_days: int) -> str:
        return f"in:inbox has:attachment newer_than:{int(window_days)}d"

    def record_inbound_backfill_progress(self, owner_user_id: str, completed_through_days: int) -> None:
        pass


def test_case11_active_backfill_defers_incremental(import_limit_20):
    getter = _dated_messages(2)
    history = _ScriptedHistory([_page(["msg_000"], head="5000")])
    transport = _BackfillHistoryTransport(
        2, import_limit=import_limit_20, scripted_history=history,
        profile_head="4000", get_messages=getter,
    )
    # A frontier exists, but the ACTIVE backfill must win regardless.
    transport.set_inbound_history_id("owner_1", "1000")

    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1",
    )
    # The window-scan (backfill) ran; the incremental history path was NOT taken.
    assert history.call_count == 0, "active backfill must defer the incremental path"
    assert getter.list_calls >= 1
    assert "backfill" in result
    # The frontier is left as-is (backfill wins; no incremental advance this poll).
    assert transport.frontier() == "1000"


# --------------------------------------------------------------------------- #
# CASE 12 -- Store round-trip unit tests (mirror the sync-window store tests):
# set/get/reset round-trip, corrupt -> "", owner isolation.
# --------------------------------------------------------------------------- #
@pytest.fixture
def history_store(tmp_path, monkeypatch):
    """A matter_store reloaded against an isolated DATA_DIR so the history file is
    tmp-scoped."""
    monkeypatch.setenv("NDA_DATA_DIR", str(tmp_path))
    import nda_automation.matter_store as ms

    importlib.reload(ms)
    yield ms
    # Restore the module's DATA_DIR-derived globals for other tests.
    monkeypatch.delenv("NDA_DATA_DIR", raising=False)
    importlib.reload(ms)


def test_case12_store_set_get_reset_round_trip(history_store):
    ms = history_store
    assert ms.gmail_inbound_history_id("o1") == ""
    assert ms.gmail_inbound_history_poll_count("o1") == 0
    assert ms.set_gmail_inbound_history_id("o1", "123456789012345") == "123456789012345"
    assert ms.gmail_inbound_history_id("o1") == "123456789012345"
    assert ms.bump_gmail_inbound_history_poll_count("o1") == 1
    assert ms.bump_gmail_inbound_history_poll_count("o1") == 2
    assert ms.gmail_inbound_history_poll_count("o1") == 2
    # reset clears BOTH fields.
    ms.reset_gmail_inbound_history("o1")
    assert ms.gmail_inbound_history_id("o1") == ""
    assert ms.gmail_inbound_history_poll_count("o1") == 0


def test_case12_store_history_id_is_string_not_int(history_store):
    ms = history_store
    # A uint64-scale id survives as an exact STRING (no int coercion / precision loss).
    big = "18446744073709551615"
    ms.set_gmail_inbound_history_id("o1", big)
    assert ms.gmail_inbound_history_id("o1") == big
    assert isinstance(ms.gmail_inbound_history_id("o1"), str)


def test_case12_store_owner_isolation(history_store):
    ms = history_store
    ms.set_gmail_inbound_history_id("owner_a", "111")
    ms.set_gmail_inbound_history_id("owner_b", "222")
    ms.bump_gmail_inbound_history_poll_count("owner_a")
    assert ms.gmail_inbound_history_id("owner_a") == "111"
    assert ms.gmail_inbound_history_id("owner_b") == "222"
    assert ms.gmail_inbound_history_poll_count("owner_a") == 1
    assert ms.gmail_inbound_history_poll_count("owner_b") == 0
    ms.reset_gmail_inbound_history("owner_a")
    assert ms.gmail_inbound_history_id("owner_a") == ""
    assert ms.gmail_inbound_history_id("owner_b") == "222", "owner_b untouched by owner_a reset"


def test_case12_store_corrupt_file_returns_empty(history_store):
    ms = history_store
    ms.GMAIL_INBOUND_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    ms.GMAIL_INBOUND_HISTORY_PATH.write_text("{ this is not json", encoding="utf-8")
    assert ms.gmail_inbound_history_id("o1") == ""
    assert ms.gmail_inbound_history_poll_count("o1") == 0
    # A non-dict payload is also tolerated.
    ms.GMAIL_INBOUND_HISTORY_PATH.write_text("[1, 2, 3]", encoding="utf-8")
    assert ms.gmail_inbound_history_id("o1") == ""


def test_case12_store_empty_set_clears_hid_keeps_poll_count(history_store):
    ms = history_store
    ms.set_gmail_inbound_history_id("o1", "500")
    ms.bump_gmail_inbound_history_poll_count("o1")
    ms.bump_gmail_inbound_history_poll_count("o1")
    # An empty-string set clears the hid but preserves the poll_count (a 404 re-seed
    # keeps its cadence bookkeeping; only reset_gmail_inbound_history clears both).
    ms.set_gmail_inbound_history_id("o1", "")
    assert ms.gmail_inbound_history_id("o1") == ""
    assert ms.gmail_inbound_history_poll_count("o1") == 2


# --------------------------------------------------------------------------- #
# CASE 8 -- Dedup: the SAME message through an incremental poll then a 404 fallback
# poll -> exactly ONE matter (the ledger + dedup index are the authority, never the
# historyId). (The broader e2e dedup lives in test_inbound_flow_e2e; this pins the
# history-path interaction directly.)
# --------------------------------------------------------------------------- #
class _GenericErrorHistory(_ScriptedHistory):
    """history_list raises a NON-429/NON-404 error (e.g. a 500) on the FIRST call,
    AFTER a hypothetical page would have reported a head -- to prove a raised listing
    error never advances the frontier."""

    def list_pages(self, start_history_id: str, page_token: str) -> dict:
        self.call_count += 1
        raise _FakeHttpError(500)


def test_raised_list_error_does_not_advance_frontier(import_limit_20):
    getter = _dated_messages(1)
    history = _GenericErrorHistory()
    transport = _HistoryInboundTransport(
        1, import_limit=import_limit_20, scripted_history=history,
        profile_head="4000", get_messages=getter,
    )
    transport.set_inbound_history_id("owner_1", "1000")

    # A generic (non-429/non-404) history.list error escalates out of the poll.
    with pytest.raises(gmail_integration.GmailIntegrationError):
        gmail_matter_inbox.import_inbound_matters(
            transport=transport, limit=999, owner_user_id="owner_1",
        )
    # The frontier was NOT advanced (listing never completed) and NOT reset (only a
    # 404 resets); the next poll retries the SAME frontier.
    assert transport.frontier() == "1000"
    assert transport.poll_count() == 0


def test_case8_dedup_incremental_then_fallback_one_matter(import_limit_20):
    getter = _dated_messages(1)
    # Poll 1: incremental imports msg_000. Poll 2: history.list 404s -> fallback
    # window-scan re-surfaces msg_000, which the dedup/ledger must suppress.
    history = _ScriptedHistory(pages_by_start={"1000": [_page(["msg_000"], head="5000")]})
    transport = _HistoryInboundTransport(
        1, import_limit=import_limit_20, scripted_history=history,
        profile_head="6000", get_messages=getter,
    )
    transport.set_inbound_history_id("owner_1", "1000")

    r1 = gmail_matter_inbox.import_inbound_matters(transport=transport, limit=999, owner_user_id="owner_1")
    assert _imported_ids(r1) == {"msg_000"}
    assert transport.frontier() == "5000"

    # Flip history into expiry mode for poll 2 (the frontier is now "5000").
    history.expire = True
    r2 = gmail_matter_inbox.import_inbound_matters(transport=transport, limit=999, owner_user_id="owner_1")
    # The fallback re-surfaced msg_000 but it is already imported -> NO second matter.
    assert r2["imported"] == []
    assert _matter_ids(transport) == {"msg_000"}
    assert len(transport.repository.list_matters(owner_user_id="owner_1")) == 1
    # Frontier was reset by the 404 then re-seeded from the profile head.
    assert transport.frontier() == "6000"
