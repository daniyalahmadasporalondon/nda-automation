from __future__ import annotations

from typing import Any
from unittest.mock import patch

from nda_automation import gmail_integration, gmail_matter_inbox, gmail_matter_outbox, gmail_transport


class _Executable:
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload

    def execute(self) -> dict[str, Any]:
        return self.payload


class _EmptyMessages:
    query = ""
    max_results = 0

    def list(self, *, userId: str, q: str, maxResults: int):
        self.query = q
        self.max_results = maxResults
        return _Executable({"messages": []})


class _EmptyUsers:
    def __init__(self) -> None:
        self.messages_api = _EmptyMessages()

    def messages(self) -> _EmptyMessages:
        return self.messages_api


class _EmptyGmailService:
    def __init__(self) -> None:
        self.users_api = _EmptyUsers()

    def users(self) -> _EmptyUsers:
        return self.users_api


class _PublicOnlyInboxTransport:
    class GmailIntegrationError(Exception):
        pass

    def __init__(self) -> None:
        self.service = _EmptyGmailService()

    def gmail_role_enabled(self, role: str) -> bool:
        return role == "inbound"

    def clean_user_token_segment(self, value: object) -> str:
        return str(value or "").strip()

    def gmail_service_for_owner(self, role: str, owner_user_id: str = "") -> _EmptyGmailService:
        assert role == "inbound"
        assert owner_user_id == "owner_1"
        return self.service

    def gmail_profile_for_role(
        self,
        role: str,
        *,
        service: Any | None = None,
        owner_user_id: str = "",
    ) -> dict[str, str]:
        assert role == "inbound"
        assert service is self.service
        assert owner_user_id == "owner_1"
        return {"emailAddress": "legal@aspora.com"}

    def default_inbound_query(self) -> str:
        return "in:inbox has:attachment"

    def max_import_limit(self) -> int:
        return 25

    def selector_configured(self) -> bool:
        return False


class _ScriptedMessages:
    """Returns a scripted sequence of list() pages, then repeats the last one.

    Tracks how many list() calls were made so tests can assert the paginated
    fetch actually terminates instead of looping unbounded.
    """

    def __init__(self, pages: list[dict[str, Any]]):
        self.pages = pages
        self.list_calls = 0

    def list(self, *, userId: str, q: str, maxResults: int, pageToken: str = ""):
        index = min(self.list_calls, len(self.pages) - 1)
        self.list_calls += 1
        return _Executable(self.pages[index])


class _ScriptedUsers:
    def __init__(self, messages_api: _ScriptedMessages) -> None:
        self.messages_api = messages_api

    def messages(self) -> _ScriptedMessages:
        return self.messages_api


class _ScriptedGmailService:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.users_api = _ScriptedUsers(_ScriptedMessages(pages))

    def users(self) -> _ScriptedUsers:
        return self.users_api


class _ScriptedInboxTransport(_PublicOnlyInboxTransport):
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        super().__init__()
        self.service = _ScriptedGmailService(pages)


class _PublicOnlyOutboxTransport:
    class GmailIntegrationError(Exception):
        pass

    class RecipientConfirmationError(GmailIntegrationError):
        pass

    def __init__(self) -> None:
        self.service = object()

    def gmail_role_enabled(self, role: str) -> bool:
        return role == "outbound"

    def clean_user_token_segment(self, value: object) -> str:
        return str(value or "").strip()

    def gmail_service_for_owner(self, role: str, owner_user_id: str = "") -> object:
        assert role == "outbound"
        assert owner_user_id == "owner_1"
        return self.service

    def gmail_profile_for_role(
        self,
        role: str,
        *,
        service: Any | None = None,
        owner_user_id: str = "",
    ) -> dict[str, str]:
        assert role == "outbound"
        assert service is self.service
        assert owner_user_id == "owner_1"
        return {"emailAddress": "legal@aspora.com"}


def test_inbound_workflow_accepts_public_only_transport():
    transport = _PublicOnlyInboxTransport()

    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport,
        limit=999,
        owner_user_id=" owner_1 ",
    )

    assert result == {
        "account": "legal@aspora.com",
        "imported": [],
        "query": "in:inbox has:attachment",
        "skipped": [],
        # AI intake telemetry is always present; all zero when the classifier is
        # unconfigured (this public-only transport) and no call is attempted.
        "ai_intake": {"ai_calls": 0, "ai_errors": 0, "ai_timeouts": 0, "ai_skipped_cap": 0},
        # The drain never tripped a Gmail rate-limit, so the poll completed cleanly.
        "rate_limited": False,
        # No message hit the transient-retry cap, so nothing was quarantined.
        "quarantined": 0,
    }
    assert transport.service.users_api.messages_api.max_results == 25


def test_outbound_workflow_accepts_public_only_transport():
    transport = _PublicOnlyOutboxTransport()

    recipient, service, outbound_account = gmail_matter_outbox.outbound_send_context(
        {"id": "matter_1", "sender": "Counterparty <counterparty@example.com>"},
        transport=transport,
        confirmed_recipient="counterparty@example.com",
        owner_user_id=" owner_1 ",
    )

    assert recipient == "counterparty@example.com"
    assert service is transport.service
    assert outbound_account == "legal@aspora.com"


def test_default_transport_preserves_legacy_patch_points():
    transport = gmail_transport.default_transport()
    with (
        patch.object(gmail_integration, "_gmail_service_for_owner", return_value=object()) as service_for_owner,
        patch.object(gmail_integration, "_gmail_profile_for_role", return_value={"emailAddress": "legal@aspora.com"}),
        patch.object(gmail_integration, "_gmail_retry_after_epoch", return_value=123.0),
        patch.object(gmail_integration, "_attachment_bytes", return_value=b"nda"),
    ):
        service = transport.gmail_service_for_owner("outbound", "owner_1")

        assert transport.gmail_profile_for_role("outbound", service=service, owner_user_id="owner_1") == {
            "emailAddress": "legal@aspora.com"
        }
        assert transport.gmail_retry_after_epoch(Exception("rate limited")) == 123.0
        assert transport.attachment_bytes(service, "msg_1", {"attachment_id": "att_1"}) == b"nda"

    service_for_owner.assert_called_once_with("outbound", "owner_1")


def test_inbound_pagination_terminates_on_zero_progress_page():
    # Reproduces the production hang: Gmail returns a NON-empty nextPageToken on
    # a page that yielded ZERO messages. Without a zero-progress break, this
    # loops forever (a reviewer saw ~5001 calls). The loop must terminate.
    pages = [{"messages": [], "nextPageToken": "endless"}]
    transport = _ScriptedInboxTransport(pages)

    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport,
        limit=999,
        owner_user_id="owner_1",
    )

    messages_api = transport.service.users_api.messages_api
    # A single zero-progress page is enough to stop; far below the hard cap.
    assert messages_api.list_calls == 1
    assert result["imported"] == []
    assert result["skipped"] == []


def test_inbound_pagination_accumulates_across_multiple_pages():
    # Normal multi-page case: stubs carry no usable id so the per-message fetch
    # is skipped, letting us assert pagination accumulation/termination directly.
    pages = [
        {"messages": [{}, {}], "nextPageToken": "page2"},
        {"messages": [{}, {}], "nextPageToken": "page3"},
        {"messages": [{}], "nextPageToken": ""},
    ]
    transport = _ScriptedInboxTransport(pages)

    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport,
        limit=999,
        owner_user_id="owner_1",
    )

    messages_api = transport.service.users_api.messages_api
    # All three pages were fetched (token ran out on the third), no extra calls.
    assert messages_api.list_calls == 3
    assert result["imported"] == []
    assert result["skipped"] == []


def test_inbound_pagination_stays_bounded_with_endless_token():
    # Defense in depth: a transport that NEVER yields an empty token (always one
    # message + non-empty nextPageToken) must still terminate. The idless stubs
    # never count toward import_limit (no new work), so the hard SCAN cap is the
    # backstop that stops the probe instead of spinning unbounded.
    pages = [{"messages": [{}], "nextPageToken": "always-more"}]
    transport = _ScriptedInboxTransport(pages)

    gmail_matter_inbox.import_inbound_matters(
        transport=transport,
        limit=999,
        owner_user_id="owner_1",
    )

    messages_api = transport.service.users_api.messages_api
    # One idless stub per page, import_limit capped at 25 by max_import_limit().
    # The scan cap (max(25*5, 25+100) == 125) bounds the probe; one list() per
    # stub means it stops after 125 calls, never spinning unbounded.
    assert messages_api.list_calls == 125


# --- Gentle Gmail catch-up knob (NDA_GMAIL_IMPORT_LIMIT) -------------------------


def test_gmail_import_limit_env_default_and_overrides(monkeypatch):
    # Unset -> the modest default that keeps a (re)connect catch-up from
    # overwhelming the single 2 GB worker.
    monkeypatch.delenv(gmail_integration.NDA_GMAIL_IMPORT_LIMIT_ENV, raising=False)
    assert gmail_integration._gmail_import_limit_from_env() == 20

    # A valid override within the clamp is honoured verbatim (operator trades burst
    # for drain speed).
    monkeypatch.setenv(gmail_integration.NDA_GMAIL_IMPORT_LIMIT_ENV, "5")
    assert gmail_integration._gmail_import_limit_from_env() == 5
    monkeypatch.setenv(gmail_integration.NDA_GMAIL_IMPORT_LIMIT_ENV, " 30 ")
    assert gmail_integration._gmail_import_limit_from_env() == 30

    # An override above the clamp is capped so the per-poll messages.get() burst
    # cannot push a single user past Gmail's ~6,000 quota-units/minute and trip
    # rate-limits. The clamp is the upper bound, not the default.
    monkeypatch.setenv(gmail_integration.NDA_GMAIL_IMPORT_LIMIT_ENV, "50")
    assert gmail_integration._gmail_import_limit_from_env() == gmail_integration._MAX_GMAIL_IMPORT_LIMIT_CLAMP
    assert gmail_integration._gmail_import_limit_from_env() == 40
    monkeypatch.setenv(gmail_integration.NDA_GMAIL_IMPORT_LIMIT_ENV, "1000")
    assert gmail_integration._gmail_import_limit_from_env() == 40

    # Garbage and non-positive values are meaningless (0 would import nothing and
    # wedge the catch-up) -> fall back to the default rather than break the poll.
    for bad in ("", "abc", "0", "-3", "12.5"):
        monkeypatch.setenv(gmail_integration.NDA_GMAIL_IMPORT_LIMIT_ENV, bad)
        assert gmail_integration._gmail_import_limit_from_env() == 20


class _CatchUpMessages:
    """A fake inbox of `inbox_size` messages, each with one reviewable attachment.

    Records the per-page `maxResults` the import loop requests so the test can
    assert the per-poll fetch is bounded by the catch-up limit, and serves
    `get()` so the per-message path can run as far as the dedup short-circuit.
    """

    def __init__(self, inbox_size: int):
        self.message_ids = [f"msg_{i:03d}" for i in range(inbox_size)]
        self.max_results_seen: list[int] = []

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
        return _Executable({"id": id, "payload": {}})


class _CatchUpUsers:
    def __init__(self, messages_api: _CatchUpMessages) -> None:
        self.messages_api = messages_api

    def messages(self) -> _CatchUpMessages:
        return self.messages_api


class _CatchUpService:
    def __init__(self, inbox_size: int) -> None:
        self.users_api = _CatchUpUsers(_CatchUpMessages(inbox_size))

    def users(self) -> _CatchUpUsers:
        return self.users_api


class _CatchUpInboxTransport(_PublicOnlyInboxTransport):
    """Drives the per-message path to the dedup short-circuit with a persistent
    (in-memory) already-imported index, modelling the real disk dedup index that
    survives across polls. Honours `gmail_integration.MAX_GMAIL_IMPORT_LIMIT` so
    the env knob is exercised end-to-end through the real module constant.
    """

    def __init__(self, inbox_size: int) -> None:
        super().__init__()
        self.service = _CatchUpService(inbox_size)
        # The PERSISTENT dedup index: message ids whose attachment is already
        # imported. Skipped BEFORE any download/extract on subsequent polls.
        self.already_imported: set[str] = set()
        self.fetched_message_ids: list[str] = []

    def max_import_limit(self) -> int:
        return int(gmail_integration.MAX_GMAIL_IMPORT_LIMIT)

    def is_self_or_outbound_message(self, message, account_email) -> bool:
        return False

    def reviewable_attachments(self, payload):
        # One reviewable attachment per message; its identity key is the message id.
        return [{"attachment_id": "att_0", "part_id": "0"}]

    def gmail_attachment_already_imported(self, message_id, attachment_id, **_kwargs) -> bool:
        return message_id in self.already_imported

    def message_nda_detection(self, message, attachments):
        # This is the FIRST call on the heavy path, reached only for a dedup-miss
        # (new) message. Record it as imported-this-poll, then mark it in the
        # persistent index so the NEXT poll skips it at the cheap dedup gate.
        message_id = str(message.get("id") or "")
        self.fetched_message_ids.append(message_id)
        self.already_imported.add(message_id)
        return {"matched": True}  # skip the attachment content-scan fallback

    def message_metadata(self, message, account_email, *, detection=None):
        return {}

    def message_body_text(self, payload):
        return ""


def test_catch_up_drains_a_bounded_batch_per_poll(monkeypatch):
    # Re-connecting Gmail surfaces a 100-email 90-day backlog. With the gentle
    # catch-up limit at 20, a SINGLE poll must hand only 20 NEW messages to the
    # heavy import path (bounding poll-thread Pro-selector/Flash-intake/PyMuPDF
    # work); a FOLLOW-UP poll must skip those 20 at the cheap persistent-dedup gate
    # and make real forward progress on the next batch -- the inbound query applies
    # no already-imported exclusion, so without paging-past this would stall.
    monkeypatch.setenv(gmail_integration.NDA_GMAIL_IMPORT_LIMIT_ENV, "20")
    monkeypatch.setattr(
        gmail_integration,
        "MAX_GMAIL_IMPORT_LIMIT",
        gmail_integration._gmail_import_limit_from_env(),
    )
    assert gmail_integration.MAX_GMAIL_IMPORT_LIMIT == 20

    # Stub the heavy per-attachment import (selector + intake + matter creation):
    # this test exercises the catch-up batching/paging contract, not matter build.
    monkeypatch.setattr(
        gmail_matter_inbox,
        "import_inbound_attachments",
        lambda *a, **k: {"imported": [], "skipped": [], "ai_intake": {}},
    )

    transport = _CatchUpInboxTransport(inbox_size=100)
    messages_api = transport.service.users_api.messages_api

    # Poll 1: exactly the first 20 NEW messages reach the heavy path (never the
    # other 80), bounding poll-thread work to the catch-up limit.
    gmail_matter_inbox.import_inbound_matters(
        transport=transport,
        limit=999,  # caller asks for the max; the catch-up limit is the real bound
        owner_user_id="owner_1",
    )
    assert transport.fetched_message_ids == [f"msg_{i:03d}" for i in range(20)]
    # No single page was ever asked for more than the catch-up limit.
    assert max(messages_api.max_results_seen) <= 20

    # Poll 2: the inbox query re-surfaces all 100 messages, but the first 20 are now
    # in the persistent dedup index -> skipped BEFORE any download/extract, and the
    # scan pages PAST them to the next batch (msg_020..msg_039) -- real per-cycle
    # forward progress, not a re-fetch of the same newest 20.
    transport.fetched_message_ids.clear()
    result2 = gmail_matter_inbox.import_inbound_matters(
        transport=transport,
        limit=999,
        owner_user_id="owner_1",
    )
    assert transport.fetched_message_ids == [f"msg_{i:03d}" for i in range(20, 40)]
    # The 20 already-imported messages that re-surfaced ahead of the new batch are
    # reported as cheaply skipped (no re-download), proving the dedup-gated paging.
    already = [s for s in result2["skipped"] if s.get("reason") == "already_imported"]
    assert len(already) == 20


# --- Drain cursor: an arbitrary backlog (> max_scan) fully drains, no tail-drop ---


class _CursorCatchUpMessages:
    """A fake inbox of `inbox_size` messages, newest-first, each carrying a Gmail
    server-assigned ``internalDate`` (ms) and honouring a ``before:<seconds>`` term
    so the date-bounded drain query skips the already-drained newest prefix.

    msg_000 is the NEWEST (largest internalDate); index N is older by N seconds. This
    mirrors Gmail: ``list`` returns newest-first and ``before:S`` returns only
    messages strictly OLDER than S seconds.
    """

    # An arbitrary fixed epoch so the dates are realistic but deterministic.
    _BASE_SECONDS = 1_700_000_000

    def __init__(self, inbox_size: int):
        self.message_ids = [f"msg_{i:03d}" for i in range(inbox_size)]
        # Newest first: msg_000 gets the largest date, each later index 1s older.
        self.internal_ms = {
            mid: (self._BASE_SECONDS - i) * 1000 for i, mid in enumerate(self.message_ids)
        }
        self.list_calls = 0

    def _before_seconds(self, q: str) -> int | None:
        for term in q.split():
            if term.startswith("before:"):
                try:
                    return int(term.split(":", 1)[1])
                except ValueError:
                    return None
        return None

    def list(self, *, userId: str, q: str, maxResults: int, pageToken: str = ""):
        self.list_calls += 1
        before_seconds = self._before_seconds(q)
        # Apply the date bound, preserving newest-first order.
        if before_seconds is None:
            eligible = list(self.message_ids)
        else:
            eligible = [
                mid for mid in self.message_ids if self.internal_ms[mid] < before_seconds * 1000
            ]
        start = int(pageToken or "0")
        page = eligible[start:start + maxResults]
        next_start = start + len(page)
        next_token = str(next_start) if next_start < len(eligible) else ""
        return _Executable({
            "messages": [{"id": mid} for mid in page],
            "nextPageToken": next_token,
        })

    def get(self, *, userId: str, id: str, format: str):
        return _Executable({"id": id, "payload": {}, "internalDate": str(self.internal_ms.get(id, 0))})


class _CursorCatchUpUsers:
    def __init__(self, messages_api: _CursorCatchUpMessages) -> None:
        self.messages_api = messages_api

    def messages(self) -> _CursorCatchUpMessages:
        return self.messages_api


class _CursorCatchUpService:
    def __init__(self, inbox_size: int) -> None:
        self.users_api = _CursorCatchUpUsers(_CursorCatchUpMessages(inbox_size))

    def users(self) -> _CursorCatchUpUsers:
        return self.users_api


class _CursorCatchUpInboxTransport(_CatchUpInboxTransport):
    """A cursor-AWARE catch-up transport: persistent in-memory dedup index AND a
    persistent drain cursor (internalDate low-water-mark), modelling the real
    readonly-scope production transport. This is the path that must drain an
    arbitrary backlog larger than max_scan without a silent tail-drop.
    """

    def __init__(self, inbox_size: int) -> None:
        super().__init__(inbox_size)
        self.service = _CursorCatchUpService(inbox_size)
        self._drain_cursor = 0
        self.cursor_resets = 0

    # The heavy-path entry point records the fetched id and marks it imported.
    def message_nda_detection(self, message, attachments):
        message_id = str(message.get("id") or "")
        self.fetched_message_ids.append(message_id)
        self.already_imported.add(message_id)
        return {"matched": True}

    # --- drain cursor (persistent across polls) ------------------------------
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

    def is_rate_limit_error(self, error: Exception) -> bool:
        return False


def test_drain_cursor_drains_backlog_larger_than_max_scan_no_silent_drop(monkeypatch):
    # THE REGRESSION GUARD. Re-connecting Gmail surfaces a backlog FAR larger than a
    # single poll's max_scan cap. With the old fixed-cap paged scan, once the
    # already-imported prefix grew past max_scan (= max(20*5, 20+100) = 120), every
    # poll exhausted its whole scan budget inside already-imported messages, found
    # ZERO new work, and the catch-up STALLED FOREVER -- silently dropping the
    # (backlog - 120) tail. The persistent drain cursor must instead let the FULL
    # backlog drain across polls with no tail-drop.
    monkeypatch.setenv(gmail_integration.NDA_GMAIL_IMPORT_LIMIT_ENV, "20")
    monkeypatch.setattr(
        gmail_integration,
        "MAX_GMAIL_IMPORT_LIMIT",
        gmail_integration._gmail_import_limit_from_env(),
    )
    assert gmail_integration.MAX_GMAIL_IMPORT_LIMIT == 20

    monkeypatch.setattr(
        gmail_matter_inbox,
        "import_inbound_attachments",
        lambda *a, **k: {"imported": [], "skipped": [], "ai_intake": {}},
    )

    backlog = 300  # > max_scan (120): the exact size the old scan silently dropped at.
    max_scan = max(20 * 5, 20 + 100)
    assert backlog > max_scan
    transport = _CursorCatchUpInboxTransport(inbox_size=backlog)
    all_ids = list(transport.service.users_api.messages_api.message_ids)

    # Poll until drained or stalled. A hard cap on polls (well above backlog/limit)
    # turns a regression (a stall) into a FAILED assertion rather than a hang.
    drained: list[str] = []
    max_polls = backlog  # pathological ceiling; a healthy drain finishes far sooner.
    for _poll in range(max_polls):
        transport.fetched_message_ids.clear()
        gmail_matter_inbox.import_inbound_matters(
            transport=transport,
            limit=999,
            owner_user_id="owner_1",
        )
        new_this_poll = list(transport.fetched_message_ids)
        drained.extend(new_this_poll)
        if len(transport.already_imported) >= backlog:
            break
        # Forward progress is mandatory: a poll that imports nothing while the
        # backlog is non-empty is the stall this fix exists to prevent.
        assert new_this_poll, (
            f"catch-up STALLED at {len(transport.already_imported)}/{backlog} imported "
            f"-- {backlog - len(transport.already_imported)} silently dropped"
        )

    # The FULL backlog drained: every message imported exactly once, none dropped.
    assert len(transport.already_imported) == backlog
    assert sorted(drained) == sorted(all_ids)
    assert len(drained) == len(set(drained)) == backlog
    # The drain reached completion in a sane number of polls (~backlog/limit), not by
    # accident of the pathological ceiling -- proving steady forward progress.
    assert _poll + 1 <= (backlog // 20) + 5

    # One more poll after the backlog is empty: it imports nothing (no double-import),
    # the date-bounded drain pass reaches the end of the backlog, and the cursor is
    # reset so future polls run head-only instead of perpetually re-paging below a
    # stale frontier.
    transport.fetched_message_ids.clear()
    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport,
        limit=999,
        owner_user_id="owner_1",
    )
    assert transport.fetched_message_ids == []
    assert result["rate_limited"] is False
    assert transport.cursor_resets >= 1
    assert transport.inbound_drain_cursor("owner_1") == 0


class _RateLimitError(Exception):
    pass


class _RateLimitAfterNMessages(_CursorCatchUpInboxTransport):
    """A cursor-aware transport whose message get() raises a Gmail rate-limit (429)
    after `fail_after` successful fetches, to exercise the graceful 429 early-exit:
    the poll must KEEP what it imported this cycle and flag rate_limited, never abort
    the whole drain and lose the new messages already found."""

    def __init__(self, inbox_size: int, fail_after: int) -> None:
        super().__init__(inbox_size)
        self._fail_after = fail_after
        self._get_calls = 0

    def is_rate_limit_error(self, error: Exception) -> bool:
        return isinstance(error, _RateLimitError)

    # Route the heavy-path entry through a get() that trips a 429 after N fetches.
    def message_nda_detection(self, message, attachments):
        return super().message_nda_detection(message, attachments)


def test_rate_limit_midscan_keeps_imported_and_does_not_abort_poll(monkeypatch):
    # A 429 during the per-message probe must NOT re-raise and discard the messages
    # already imported this cycle; it pages-gracefully: keep what we have, flag
    # rate_limited, and resume next poll from the persisted cursor.
    monkeypatch.setenv(gmail_integration.NDA_GMAIL_IMPORT_LIMIT_ENV, "20")
    monkeypatch.setattr(
        gmail_integration,
        "MAX_GMAIL_IMPORT_LIMIT",
        gmail_integration._gmail_import_limit_from_env(),
    )
    monkeypatch.setattr(
        gmail_matter_inbox,
        "import_inbound_attachments",
        lambda *a, **k: {"imported": [], "skipped": [], "ai_intake": {}},
    )

    transport = _RateLimitAfterNMessages(inbox_size=100, fail_after=5)
    # Make the 5th get() raise a rate-limit.
    real_get = transport.service.users_api.messages_api.get
    state = {"n": 0}

    def flaky_get(*, userId, id, format):
        state["n"] += 1
        if state["n"] > 5:
            raise _RateLimitError("rateLimitExceeded")
        return real_get(userId=userId, id=id, format=format)

    transport.service.users_api.messages_api.get = flaky_get

    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport,
        limit=999,
        owner_user_id="owner_1",
    )

    # The poll did NOT raise; it kept the 5 it managed to import and flagged the 429.
    assert result["rate_limited"] is True
    assert len(transport.already_imported) == 5
    assert transport.fetched_message_ids == [f"msg_{i:03d}" for i in range(5)]


# --------------------------------------------------------------------------- #
# DocuSign envelope-notification skip: the deterministic domain-only matcher.
# --------------------------------------------------------------------------- #
def _message_with_from(raw_from: str) -> dict[str, Any]:
    return {"payload": {"headers": [{"name": "From", "value": raw_from}]}}


def test_is_docusign_notification_matches_docusign_sender_domains():
    # The full DocuSign sender family the recon enumerated -- all DOMAIN matches.
    for raw_from in (
        "DocuSign <dse@docusign.net>",
        "dse@docusign.net",
        "DocuSign EU <dse_demo@docusign.net>",
        "dse_na3@docusign.net",
        "dse_na1@docusign.net",
        "dse_eu1@docusign.net",
        "DocuSign <dse@eumail.docusign.net>",
        "no-reply@mail.docusign.net",
        # Case-insensitive on the domain.
        "DSE@DocuSign.NET",
    ):
        assert gmail_integration._is_docusign_notification(_message_with_from(raw_from)) is True, raw_from


def test_is_docusign_notification_domain_only_real_nda_mentioning_docusign_passes():
    # A genuine counterparty NDA from a real sender whose subject/body/filename all
    # mention DocuSign must NOT be skipped -- the match is DOMAIN-ONLY.
    message = {
        "payload": {
            "headers": [
                {"name": "From", "value": "Jane Counsel <jane@acme.com>"},
                {"name": "Subject", "value": "NDA -- sent via DocuSign"},
            ],
            "parts": [{"filename": "docusign_nda.pdf"}],
        },
        "snippet": "Please countersign the attached NDA in DocuSign.",
    }
    assert gmail_integration._is_docusign_notification(message) is False


def test_is_docusign_notification_rejects_lookalike_and_substring_domains():
    # Defend against domains that merely CONTAIN the brand string but are not the
    # docusign.net family -- these are real (potentially hostile) third parties.
    for raw_from in (
        "evil <attacker@docusign.net.evil.com>",  # docusign.net is a label, not the suffix
        "phish <dse@notdocusign.net>",  # ends with "docusign.net" textually but wrong domain
        "<dse@docusign.com>",  # .com, not .net
        "<dse@mydocusign.net>",  # mydocusign.net != *.docusign.net
    ):
        assert gmail_integration._is_docusign_notification(_message_with_from(raw_from)) is False, raw_from


def test_is_docusign_notification_fails_open_on_malformed_from():
    # No From header, unparseable address, or MULTIPLE addresses -> fail open
    # (return False) so a real NDA is never wrongly skipped.
    assert gmail_integration._is_docusign_notification({"payload": {"headers": []}}) is False
    assert gmail_integration._is_docusign_notification(_message_with_from("")) is False
    assert gmail_integration._is_docusign_notification(_message_with_from("not an email")) is False
    assert (
        gmail_integration._is_docusign_notification(
            _message_with_from("dse@docusign.net, jane@acme.com")
        )
        is False
    )


def test_transport_is_docusign_notification_delegates_to_legacy():
    transport = gmail_transport.default_transport()
    assert transport.is_docusign_notification(_message_with_from("dse@docusign.net")) is True
    assert transport.is_docusign_notification(_message_with_from("jane@acme.com")) is False
