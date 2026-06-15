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
    # message + non-empty nextPageToken) must still terminate. Here the import
    # cap (max_import_limit == 25) bounds it; the hard page cap (import_limit+25)
    # is the further backstop should that path ever change.
    pages = [{"messages": [{}], "nextPageToken": "always-more"}]
    transport = _ScriptedInboxTransport(pages)

    gmail_matter_inbox.import_inbound_matters(
        transport=transport,
        limit=999,
        owner_user_id="owner_1",
    )

    messages_api = transport.service.users_api.messages_api
    # One stub per page, import_limit capped at 25 => stops after 25 calls,
    # never spinning unbounded.
    assert messages_api.list_calls == 25
