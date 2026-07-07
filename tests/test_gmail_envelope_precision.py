"""Tests for the Gmail inbound envelope-precision batch (fix/gmail-envelope-precision).

Four features are pinned here:

TIER 0 -- SENDER EXCLUDES. The structural fetch envelope matches EVERY docx/pdf
  attachment email, importing e-sign platform notifications and calendar invites
  as phantom matters. The authoritative suppression is the CODE-level sender
  check (visible in skipped[] + the ledger, catches forwards); the query-level
  ``-from:`` clauses are a redundant fetch-quota optimization over the same
  entries. Admin-editable (gmail settings ``inbound_excluded_senders``), env
  kill switch NDA_GMAIL_ENVELOPE_EXCLUDES (off = the exact pre-exclude query +
  the DocuSign-only code check).

EXECUTED-NDA CAPTURE. A platform notification whose attachment filename or
  subject/body/snippet carries an EXPLICIT NDA signal is let through intake
  (clamped to the TRIAGE lane, provenance triage_reason=esign_notification_nda)
  instead of terminally dropped -- a counterparty-initiated envelope's completion
  email is often the only copy of an executed NDA. Env: NDA_GMAIL_ESIGN_NDA_CAPTURE.

TIER 1 -- AI PRE-GATE. Deterministic-skip candidates no longer pay for the Flash
  intake call, and the Pro selector only runs when >=1 candidate reaches the
  triage band -- with MANDATORY fail-open exemptions (explicit-NDA escape hatch;
  extraction-blind text under ~200 chars: image-only DOCX, partial-scan PDF,
  foreign-language NDA). Env: NDA_GMAIL_AI_PREGATE. The pre-gate only activates
  on transports exposing the escape-hatch seam (older fakes = legacy behaviour).

TIER 3 -- FIRST-SYNC BACKFILL CAP. A newly connected account's first sync scans
  min(window, NDA_GMAIL_FIRST_SYNC_CAP_DAYS) days and widens per successful poll;
  existing users are exempt; progress is surfaced; the drain cursor is RE-ARMED
  at the old window boundary on a widen step so the widened band is reachable
  past an arbitrarily long processed prefix (ledger pre-skips no longer count
  toward the max_scan cap).
"""

from __future__ import annotations

from typing import Any

import pytest

from nda_automation import (
    app_settings,
    gmail_integration,
    gmail_matter_inbox,
    user_store,
)
from nda_automation.gmail_transport import GmailTransport

from tests.test_gmail_processed_ledger import (
    _CursorAwareLedgerTransport,
    _LedgerSpyTransport,
)
from tests.test_gmail_transport import _Executable


@pytest.fixture
def settings_data_dir(tmp_path, monkeypatch):
    """Root the settings + user stores at an isolated tmp dir."""
    from nda_automation import matter_store

    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    monkeypatch.setenv("NDA_USERS_PATH", str(tmp_path / "users.json"))
    return tmp_path


@pytest.fixture
def import_limit_20(monkeypatch):
    monkeypatch.setenv(gmail_integration.NDA_GMAIL_IMPORT_LIMIT_ENV, "20")
    monkeypatch.setattr(
        gmail_integration,
        "MAX_GMAIL_IMPORT_LIMIT",
        gmail_integration._gmail_import_limit_from_env(),
    )
    return 20


# =========================================================================== #
# TIER 0 -- query construction
# =========================================================================== #

EXPECTED_DEFAULT_QUERY = (
    "in:inbox has:attachment (filename:docx OR filename:pdf) -from:me newer_than:90d"
    " -from:docusign.net -from:docusign.com -from:adobesign.com -from:documents.adobe.com"
    " -from:echosign.com -from:hellosign.com -from:pandadoc.com"
    " -from:calendar-notification@google.com"
)


def test_default_query_is_the_exact_extended_envelope(settings_data_dir):
    assert gmail_integration._default_inbound_query() == EXPECTED_DEFAULT_QUERY
    assert gmail_integration.DEFAULT_INBOUND_QUERY == EXPECTED_DEFAULT_QUERY
    assert gmail_integration.DEFAULT_INBOUND_QUERY_WITH_AI_SELECTOR == EXPECTED_DEFAULT_QUERY


def test_kill_switch_off_restores_byte_identical_legacy_query(settings_data_dir, monkeypatch):
    monkeypatch.setenv(gmail_integration.NDA_GMAIL_ENVELOPE_EXCLUDES_ENV, "0")
    query = gmail_integration._default_inbound_query()
    assert query == gmail_integration.GMAIL_INBOUND_BASE_QUERY
    assert query == (
        "in:inbox has:attachment (filename:docx OR filename:pdf) -from:me newer_than:90d"
    )
    assert "-from:docusign" not in query


@pytest.mark.parametrize("raw", ["false", "no", "off", "0"])
def test_kill_switch_accepts_all_off_spellings(settings_data_dir, monkeypatch, raw):
    monkeypatch.setenv(gmail_integration.NDA_GMAIL_ENVELOPE_EXCLUDES_ENV, raw)
    assert gmail_integration.gmail_envelope_excludes_enabled() is False
    assert gmail_integration._default_inbound_query() == gmail_integration.GMAIL_INBOUND_BASE_QUERY


def test_admin_customized_excludes_are_respected_and_validated(settings_data_dir):
    app_settings.update_gmail_settings(
        {"inbound_excluded_senders": ["docusign.net", "Foo.Example", "junk entry with spaces"]}
    )
    # The invalid entry is dropped by the normalizer; the valid ones are kept
    # (lowercased) and drive the query.
    stored = app_settings.gmail_inbound_excluded_senders()
    assert stored == ["docusign.net", "foo.example"]
    query = gmail_integration._default_inbound_query()
    assert query.endswith("-from:docusign.net -from:foo.example")
    assert "junk" not in query


def test_admin_cleared_excludes_yield_bare_query_but_keep_docusign_code_floor(settings_data_dir):
    app_settings.update_gmail_settings({"inbound_excluded_senders": []})
    assert app_settings.gmail_inbound_excluded_senders() == []
    # Query falls back to the bare envelope (nothing to exclude)...
    assert gmail_integration._default_inbound_query() == gmail_integration.GMAIL_INBOUND_BASE_QUERY
    # ...but the code-level check keeps the hard DocuSign floor.
    message = _message_with_sender("dse@docusign.net")
    assert gmail_integration._excluded_notification_sender(message) == "docusign.net"
    # And the settings-level platforms are no longer code-matched.
    assert gmail_integration._excluded_notification_sender(
        _message_with_sender("noreply@hellosign.com")
    ) == ""


def test_absent_stored_key_defaults_without_rewriting_settings(settings_data_dir):
    # A stored blob that PREDATES the key (simulated by an unrelated write) must
    # read back the defaults purely at read time -- no migration write.
    app_settings.update_gmail_settings({"inbound_window_days": 30})
    repo = app_settings._repository()
    section = repo.read_section("gmail", lambda payload: dict(payload))
    # Simulate the pre-feature blob: drop the key entirely.
    section.pop("inbound_excluded_senders", None)
    assert app_settings.gmail_inbound_excluded_senders(section) == list(
        app_settings.DEFAULT_GMAIL_INBOUND_EXCLUDED_SENDERS
    )


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, list(app_settings.DEFAULT_GMAIL_INBOUND_EXCLUDED_SENDERS)),  # absent => default
        ([], []),                                        # explicit clear preserved
        ({"bad": "shape"}, list(app_settings.DEFAULT_GMAIL_INBOUND_EXCLUDED_SENDERS)),
        (["DocuSign.NET", "docusign.net"], ["docusign.net"]),  # lowercased + deduped
        (["nodot"], []),                                  # dot-less domain rejected
        (['evil" OR from:me', "ok.example"], ["ok.example"]),  # query-unsafe rejected
        (["a@b@c.example"], []),                          # double-@ rejected
        ("one.example, two.example\nthree.example", ["one.example", "two.example", "three.example"]),
    ],
)
def test_excluded_senders_normalizer(raw, expected):
    assert app_settings.gmail_excluded_senders_from_payload(raw) == expected


def test_drain_before_bound_composes_with_extended_query():
    transport = GmailTransport()
    bounded = transport.inbound_query_before(EXPECTED_DEFAULT_QUERY, 1_700_000_000_500)
    assert bounded == f"{EXPECTED_DEFAULT_QUERY} before:1700000001"


# =========================================================================== #
# TIER 0 -- code-level sender matcher
# =========================================================================== #

def _message_with_sender(address: str) -> dict[str, Any]:
    return {"payload": {"headers": [{"name": "From", "value": f"Notify <{address}>"}]}}


@pytest.mark.parametrize(
    "address, expected_entry",
    [
        ("dse@docusign.net", "docusign.net"),
        ("dse_na3@eumail.docusign.net", "docusign.net"),
        ("noreply@docusign.com", "docusign.com"),
        ("echosign@echosign.com", "echosign.com"),
        ("adobesign@adobesign.com", "adobesign.com"),
        ("no-reply@documents.adobe.com", "documents.adobe.com"),
        ("noreply@mail.hellosign.com", "hellosign.com"),
        ("docs@pandadoc.com", "pandadoc.com"),
        ("calendar-notification@google.com", "calendar-notification@google.com"),
        # A full-address entry must NOT match siblings at the same domain.
        ("someone-else@google.com", ""),
        # Lookalike/substring domains never match (suffix must be a dot boundary).
        ("x@notdocusign.net", ""),
        ("x@docusign.net.evil.example", ""),
        ("jane@acme.com", ""),
    ],
)
def test_excluded_notification_sender_matching(settings_data_dir, address, expected_entry):
    assert gmail_integration._excluded_notification_sender(_message_with_sender(address)) == expected_entry


def test_excluded_sender_fails_open_on_malformed_from(settings_data_dir):
    assert gmail_integration._excluded_notification_sender({"payload": {"headers": []}}) == ""
    assert gmail_integration._excluded_notification_sender(
        {"payload": {"headers": [{"name": "From", "value": "a@docusign.net, b@docusign.net"}]}}
    ) == ""


def test_kill_switch_off_shrinks_code_check_to_docusign_floor(settings_data_dir, monkeypatch):
    monkeypatch.setenv(gmail_integration.NDA_GMAIL_ENVELOPE_EXCLUDES_ENV, "false")
    assert gmail_integration._excluded_notification_sender(
        _message_with_sender("dse@docusign.net")
    ) == "docusign.net"
    assert gmail_integration._excluded_notification_sender(
        _message_with_sender("docs@pandadoc.com")
    ) == ""


# =========================================================================== #
# Scan-loop transports (shared by the exclude / capture / pre-gate tests)
# =========================================================================== #

class _PrecisionTransport(_LedgerSpyTransport):
    """Ledger-spy transport + the REAL sender-exclude, capture, and escape-hatch
    seams (which ACTIVATE the pre-gate), with scriptable per-filename extraction,
    validation and attachment lists.
    """

    def __init__(self, *, import_limit: int = 20, inbox_size: int = 1) -> None:
        super().__init__(inbox_size=inbox_size, import_limit=import_limit)
        self.scripted_paragraphs: dict[str, list[str]] = {}
        self.scripted_validation: dict[str, dict[str, Any]] = {}
        self.attachment_filenames: list[str] = ["inbound_nda_sample.pdf"]

    # -- real production seams ------------------------------------------- #
    def excluded_notification_sender(self, message: dict[str, Any]) -> str:
        return gmail_integration._excluded_notification_sender(message)

    def esign_nda_capture_hit(self, message, attachments) -> bool:
        return gmail_integration._esign_notification_nda_hit(message, attachments)

    def attachment_explicit_nda_signal(self, metadata, candidate) -> bool:
        return gmail_integration._attachment_explicit_nda_hit(
            metadata, str(candidate.get("filename") or "")
        )

    def excluded_message_capture_probe(self, service, message_id, attachments) -> bool:
        # Mirrors gmail_integration._excluded_message_content_probe but routes
        # download/extraction/validation through this transport's own scripted
        # seams (the production function has its own unit test below).
        if not gmail_integration.gmail_esign_nda_capture_enabled():
            return False
        for attachment in attachments:
            filename = str(attachment.get("filename") or "")
            document_bytes = self.attachment_bytes(service, message_id, attachment)
            _document_type, paragraphs = self.extract_document_paragraphs(filename, document_bytes)
            validation = self.attachment_nda_validation(filename, paragraphs)
            if bool(validation.get("has_content_basis")) or int(validation.get("score") or 0) >= 40:
                return True
            if int(validation.get("detection_hits") or 0) == 0:
                total = sum(len(" ".join(str(p.get("text") or "").split())) for p in paragraphs)
                if total >= gmail_matter_inbox.MIN_PREGATE_EXTRACTED_TEXT_CHARS:
                    return True
        return False

    # -- scripting --------------------------------------------------------- #
    def set_senders(self, senders: dict[str, str]) -> None:
        self.service.users_api.messages_api._senders = dict(senders)

    def reviewable_attachments(self, payload):
        return [
            {"attachment_id": f"att_{i}", "part_id": str(i), "filename": filename}
            for i, filename in enumerate(self.attachment_filenames)
        ]

    def extract_document_paragraphs(self, filename: str, document_bytes: bytes):
        if filename in self.scripted_paragraphs:
            return "pdf", [
                {"id": f"p{i}", "text": text}
                for i, text in enumerate(self.scripted_paragraphs[filename], start=1)
            ]
        return super().extract_document_paragraphs(filename, document_bytes)

    def attachment_nda_validation(self, filename, paragraphs, *, message_metadata=None):
        if filename in self.scripted_validation:
            return dict(self.scripted_validation[filename])
        return super().attachment_nda_validation(
            filename, paragraphs, message_metadata=message_metadata
        )


# A text-extractable SPANISH NDA (~260 chars, over the blind threshold): the
# English vocabulary matches NOTHING here (zero detection hits), so the scorer
# is language-blind and the AI overlay must stay available (F1).
SPANISH_NDA_TEXT = (
    "Acuerdo de Confidencialidad entre las partes. La parte receptora se obliga a "
    "mantener en estricta reserva toda la información revelada por la parte "
    "divulgante y a no utilizarla para fines distintos de la evaluación de la "
    "relación comercial propuesta entre ambas organizaciones."
)

# A long, clearly non-NDA text body (> the 200-char blind threshold) so the
# deterministic skip is TRUSTED (not extraction-blind).
LONG_COLLATERAL_TEXT = (
    "Project proposal overview covering pricing, milestones, programme management "
    "expectations and the statement of work for the upcoming engagement. This "
    "questionnaire summarises the commercial rollout plan, invoice schedule and "
    "purchase order process for the vendor onboarding track across both regions."
)
# detection_hits > 0: the scorer ENGAGED the vocabulary (collateral hits), so the
# skip is TRUSTED (not language-blind) and the pre-gate applies.
SKIP_VALIDATION = {
    "accepted": False,
    "has_content_basis": False,
    "score": 0,
    "reason": "collateral:proposal, collateral:invoice",
    "detection_hits": 3,
}
TRIAGE_VALIDATION = {
    "accepted": False,
    "has_content_basis": True,
    "score": 55,
    "reason": "uncertain nda",
    "detection_hits": 2,
}


def _neutral_metadata(message_id: str = "msg_x") -> dict[str, str]:
    return {
        "gmail_account": "legal@aspora.com",
        "gmail_message_id": message_id,
        "subject": "Fwd: a document",
        "sender": "ops@example.com",
    }


# =========================================================================== #
# TIER 0 / capture -- scan-loop integration
# =========================================================================== #

def test_platform_notification_without_nda_signal_is_terminally_dropped(settings_data_dir, import_limit_20):
    transport = _PrecisionTransport(import_limit=import_limit_20)
    transport.set_senders({"msg_000": "PandaDoc <docs@pandadoc.com>"})
    transport.attachment_filenames = ["Invoice.pdf"]
    # Genuine platform junk: substantial English collateral content, so BOTH the
    # explicit-token capture and the deterministic content probe say drop.
    transport.scripted_paragraphs["Invoice.pdf"] = [LONG_COLLATERAL_TEXT]
    transport.scripted_validation["Invoice.pdf"] = dict(SKIP_VALIDATION)

    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1"
    )

    assert result["imported"] == []
    reasons = {(s.get("reason"), s.get("detail")) for s in result["skipped"]}
    assert ("excluded_sender_notification", "pandadoc.com") in reasons
    # Terminal: ledger-marked, and neither paid AI call ever ran.
    assert transport.ledger_ids() == {"msg_000"}
    assert transport.selector_calls == []
    assert transport.intake_calls == []


def test_docusign_notification_keeps_the_live_fix_reason_label(settings_data_dir, import_limit_20):
    transport = _PrecisionTransport(import_limit=import_limit_20)
    transport.set_senders({"msg_000": "DocuSign <dse@docusign.net>"})
    transport.attachment_filenames = ["Invoice.pdf"]
    transport.scripted_paragraphs["Invoice.pdf"] = [LONG_COLLATERAL_TEXT]
    transport.scripted_validation["Invoice.pdf"] = dict(SKIP_VALIDATION)

    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1"
    )

    assert result["imported"] == []
    assert {s.get("reason") for s in result["skipped"]} == {"docusign_notification"}
    assert transport.selector_calls == []
    assert transport.intake_calls == []


def test_docusign_completion_with_nda_filename_is_captured_as_triage(settings_data_dir, import_limit_20):
    transport = _PrecisionTransport(import_limit=import_limit_20)
    transport.set_senders({"msg_000": "DocuSign <dse@docusign.net>"})
    transport.attachment_filenames = ["Mutual NDA - signed.pdf"]

    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1"
    )

    assert [s for s in result["skipped"] if s.get("reason") == "docusign_notification"] == []
    assert len(result["imported"]) == 1
    imported = result["imported"][0]
    # Clamped to the triage lane with the uniform provenance reason, and the
    # matched platform entry rides the matter for later filtering.
    assert imported["needs_triage"] == "true"
    assert imported["triage_reason"] == gmail_matter_inbox.ESIGN_NDA_CAPTURE_TRIAGE_REASON
    assert imported["gmail_esign_notification"] == "docusign.net"


def test_new_platform_domain_completion_is_captured_too(settings_data_dir, import_limit_20):
    transport = _PrecisionTransport(import_limit=import_limit_20)
    transport.set_senders({"msg_000": "PandaDoc <docs@pandadoc.com>"})
    transport.attachment_filenames = ["Confidentiality Agreement (executed).pdf"]

    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1"
    )

    assert len(result["imported"]) == 1
    imported = result["imported"][0]
    assert imported["needs_triage"] == "true"
    assert imported["triage_reason"] == "esign_notification_nda"
    assert imported["gmail_esign_notification"] == "pandadoc.com"


def test_capture_flag_off_drops_all_platform_mail(settings_data_dir, import_limit_20, monkeypatch):
    monkeypatch.setenv(gmail_integration.NDA_GMAIL_ESIGN_NDA_CAPTURE_ENV, "0")
    transport = _PrecisionTransport(import_limit=import_limit_20)
    transport.set_senders({"msg_000": "DocuSign <dse@docusign.net>"})
    transport.attachment_filenames = ["Mutual NDA - signed.pdf"]

    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1"
    )

    assert result["imported"] == []
    assert {s.get("reason") for s in result["skipped"]} == {"docusign_notification"}


def test_agreement_shaped_envelope_without_nda_token_is_captured_by_content_probe(
    settings_data_dir, import_limit_20
):
    # F2: Adobe Sign "Signature requested on 'Acme - Mutual Agreement'" -- no
    # English NDA token anywhere, but the attachment CONTENT reaches the triage
    # band (the fixture is a real NDA), so the deterministic content probe routes
    # it to the capture path instead of the terminal drop. The base
    # (pre-exclude) behaviour imported these; the excludes must not lose them.
    transport = _PrecisionTransport(import_limit=import_limit_20)
    transport.set_senders({"msg_000": "Adobe Sign <echosign@echosign.com>"})
    transport.attachment_filenames = ["Acme - Mutual Agreement.pdf"]

    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1"
    )

    assert len(result["imported"]) == 1
    imported = result["imported"][0]
    assert imported["needs_triage"] == "true"
    assert imported["triage_reason"] == "esign_notification_nda"
    assert imported["gmail_esign_notification"] == "echosign.com"


def test_content_probe_captures_language_blind_platform_attachment(settings_data_dir, import_limit_20):
    # F1(a) parity inside the F2 probe: a platform envelope whose attachment is a
    # text-extractable NON-ENGLISH document (zero vocabulary hits, substantial
    # text) is captured for triage rather than dropped.
    transport = _PrecisionTransport(import_limit=import_limit_20)
    transport.set_senders({"msg_000": "DocuSign <dse@docusign.net>"})
    transport.attachment_filenames = ["Acuerdo.pdf"]
    transport.scripted_paragraphs["Acuerdo.pdf"] = [SPANISH_NDA_TEXT]

    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1"
    )

    assert len(result["imported"]) == 1
    assert result["imported"][0]["triage_reason"] == "esign_notification_nda"


def test_content_probe_is_capped_per_poll_and_defers_overflow(settings_data_dir, monkeypatch):
    # The content probe (download + extraction) is capped at import_limit per
    # poll; overflow excluded messages are DEFERRED unmarked (no terminal drop)
    # and retried next poll -- a backlog of platform mail can never out-extract
    # the import budget nor be silently dropped unprobed.
    monkeypatch.setenv(gmail_integration.NDA_GMAIL_IMPORT_LIMIT_ENV, "1")
    monkeypatch.setattr(
        gmail_integration,
        "MAX_GMAIL_IMPORT_LIMIT",
        gmail_integration._gmail_import_limit_from_env(),
    )
    transport = _PrecisionTransport(import_limit=1, inbox_size=2)
    transport.set_senders({
        "msg_000": "PandaDoc <docs@pandadoc.com>",
        "msg_001": "PandaDoc <docs@pandadoc.com>",
    })
    transport.attachment_filenames = ["Invoice.pdf"]
    transport.scripted_paragraphs["Invoice.pdf"] = [LONG_COLLATERAL_TEXT]
    transport.scripted_validation["Invoice.pdf"] = dict(SKIP_VALIDATION)

    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1"
    )

    reasons = [str(s.get("reason") or "") for s in result["skipped"]]
    assert reasons.count("excluded_sender_notification") == 1  # probed + dropped
    assert reasons.count("excluded_probe_deferred") == 1       # over-cap, deferred
    # Only the probed message is terminally ledger-marked; the deferred one
    # stays unmarked so it retries next poll.
    assert len(transport.ledger_ids()) == 1

    # Next poll probes the deferred message and drops it too.
    result2 = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1"
    )
    reasons2 = [str(s.get("reason") or "") for s in result2["skipped"]]
    assert reasons2.count("excluded_sender_notification") == 1
    assert len(transport.ledger_ids()) == 2


def test_production_content_probe_triage_band_and_language_blind(settings_data_dir, monkeypatch):
    # Unit test of gmail_integration._excluded_message_content_probe with the
    # download seam patched: agreement-shaped content (real NDA fixture) -> True;
    # junk collateral -> False; language-blind substantial text -> True.
    from tests.test_inbound_flow_e2e import _fixture_pdf_bytes

    monkeypatch.setattr(
        gmail_integration, "_attachment_bytes", lambda service, message_id, attachment: _fixture_pdf_bytes()
    )
    assert gmail_integration._excluded_message_content_probe(
        None, "m1", [{"filename": "Acme - Mutual Agreement.pdf", "attachment_id": "a1"}]
    ) is True

    junk_paragraphs = [{"id": "p1", "text": LONG_COLLATERAL_TEXT}]
    monkeypatch.setattr(
        gmail_integration,
        "extract_document",
        lambda filename, document_bytes, include_visual_profile=True: ("pdf", junk_paragraphs, None),
    )
    assert gmail_integration._excluded_message_content_probe(
        None, "m2", [{"filename": "Invoice.pdf", "attachment_id": "a1"}]
    ) is False

    spanish_paragraphs = [{"id": "p1", "text": SPANISH_NDA_TEXT}]
    monkeypatch.setattr(
        gmail_integration,
        "extract_document",
        lambda filename, document_bytes, include_visual_profile=True: ("pdf", spanish_paragraphs, None),
    )
    assert gmail_integration._excluded_message_content_probe(
        None, "m3", [{"filename": "Acuerdo.pdf", "attachment_id": "a1"}]
    ) is True

    # Flag off => unconditional False (the caller falls back to the drop).
    monkeypatch.setenv(gmail_integration.NDA_GMAIL_ESIGN_NDA_CAPTURE_ENV, "0")
    assert gmail_integration._excluded_message_content_probe(
        None, "m4", [{"filename": "Acuerdo.pdf", "attachment_id": "a1"}]
    ) is False


# =========================================================================== #
# TIER 1 -- AI pre-gate
# =========================================================================== #

def _run_attachments(transport, attachments, metadata):
    return gmail_matter_inbox.import_inbound_attachments(
        None,
        str(metadata.get("gmail_message_id") or "msg_x"),
        attachments,
        metadata,
        transport=transport,
    )


def test_pregate_suppresses_both_ai_calls_for_trusted_deterministic_skip(settings_data_dir):
    transport = _PrecisionTransport()
    transport.scripted_paragraphs["Collateral.pdf"] = [LONG_COLLATERAL_TEXT]
    transport.scripted_validation["Collateral.pdf"] = dict(SKIP_VALIDATION)

    result = _run_attachments(
        transport,
        [{"attachment_id": "att_1", "part_id": "1", "filename": "Collateral.pdf"}],
        _neutral_metadata(),
    )

    assert result["imported"] == []
    assert [s["reason"] for s in result["skipped"]] == ["non_nda_attachment"]
    # NO Flash intake call, NO Pro selector call (asserted via the spies).
    assert transport.intake_calls == []
    assert transport.selector_calls == []
    # The suppressed call is not tallied as an AI call.
    assert result["ai_intake"]["ai_calls"] == 0
    # AUDITABILITY (F3): the suppression is countable in the tallies and the
    # skip record itself is marked, so "AI never consulted" is distinguishable
    # from "AI agreed".
    assert result["ai_intake"]["ai_skipped_pregate"] == 1
    assert result["skipped"][0]["pregate"] == "suppressed"
    # The skip is still terminal-stable (ledger-markable), never a poison retry.
    assert result["stable_outcome"] is True


def test_pregate_escape_hatch_body_mention_with_neutral_filename(settings_data_dir):
    # "Attached is our NDA" in the BODY + "document (3).pdf" filename: the
    # metadata escape hatch (subject/body/snippet, via the SAME
    # _metadata_has_explicit_nda_signal) must keep the AI overlay available.
    transport = _PrecisionTransport()
    transport.scripted_paragraphs["document (3).pdf"] = [LONG_COLLATERAL_TEXT]
    transport.scripted_validation["document (3).pdf"] = dict(SKIP_VALIDATION)
    metadata = {
        **_neutral_metadata(),
        "gmail_detection_sources": "body",
        "gmail_detection_terms": "NDA",
    }

    result = _run_attachments(
        transport,
        [{"attachment_id": "att_1", "part_id": "1", "filename": "document (3).pdf"}],
        metadata,
    )

    # Both AI stages ran (the spy intake says NDA -> clamped to triage by the
    # det-skip floor in resolve_intake_lane -> imported flagged, never dropped).
    assert transport.selector_calls != []
    assert transport.intake_calls != []
    assert len(result["imported"]) == 1


def test_pregate_escape_hatch_strong_nda_filename(settings_data_dir):
    transport = _PrecisionTransport()
    transport.scripted_paragraphs["Mutual NDA.pdf"] = [LONG_COLLATERAL_TEXT]
    transport.scripted_validation["Mutual NDA.pdf"] = dict(SKIP_VALIDATION)

    result = _run_attachments(
        transport,
        [{"attachment_id": "att_1", "part_id": "1", "filename": "Mutual NDA.pdf"}],
        _neutral_metadata(),
    )

    assert transport.intake_calls != []
    assert len(result["imported"]) == 1


def test_pregate_extraction_blind_image_only_document_keeps_flash_call(settings_data_dir):
    # Image-only document (the image-only DOCX / partial-scan PDF class):
    # extraction succeeds with EMPTY text and a neutral filename. A blind
    # det-skip means "couldn't judge" -- the Flash call must still happen. (The
    # blind check reads the extracted paragraphs, so it is extension-agnostic;
    # a .pdf filename keeps the fake transport's fixture-byte ingestion valid.)
    transport = _PrecisionTransport()
    transport.scripted_paragraphs["scan.pdf"] = []
    transport.scripted_validation["scan.pdf"] = dict(SKIP_VALIDATION)

    result = _run_attachments(
        transport,
        [{"attachment_id": "att_1", "part_id": "1", "filename": "scan.pdf"}],
        _neutral_metadata(),
    )

    assert transport.intake_calls != []
    # The AI overlay (spy selector promotes + spy intake says NDA) rescues the
    # blind candidate: imported, NEVER a terminal skip.
    assert len(result["imported"]) == 1
    assert result["skipped"] == []


def test_pregate_extraction_blind_short_foreign_language_text(settings_data_dir):
    # A foreign-language NDA scores ~0 on the English regexes and its short
    # extraction sits under the blind threshold -- the AI must still be consulted.
    transport = _PrecisionTransport()
    transport.scripted_paragraphs["vertrag.pdf"] = ["Geheimhaltungsvereinbarung zwischen den Parteien."]
    transport.scripted_validation["vertrag.pdf"] = dict(SKIP_VALIDATION)

    result = _run_attachments(
        transport,
        [{"attachment_id": "att_1", "part_id": "1", "filename": "vertrag.pdf"}],
        _neutral_metadata(),
    )

    assert transport.intake_calls != []
    assert len(result["imported"]) == 1


def test_pregate_language_blind_spanish_nda_keeps_ai(settings_data_dir):
    # F1(a): a TEXT-EXTRACTABLE Spanish NDA (~277 chars, over the blind
    # threshold) scores zero on the English vocabulary through the REAL scorer
    # (detection_hits == 0) -- the pre-gate must treat that as blind-equivalent
    # and keep both AI calls, so the multilingual Flash classifier rescues it
    # exactly as it did pre-build.
    transport = _PrecisionTransport()
    transport.scripted_paragraphs["Acuerdo de Confidencialidad.pdf"] = [SPANISH_NDA_TEXT]
    # NO scripted validation: the REAL deterministic scorer runs and reports
    # detection_hits == 0 over substantial text.

    result = _run_attachments(
        transport,
        [{"attachment_id": "att_1", "part_id": "1", "filename": "Acuerdo de Confidencialidad.pdf"}],
        _neutral_metadata(),
    )

    assert transport.intake_calls != []
    assert transport.selector_calls != []
    assert len(result["imported"]) == 1
    assert result["skipped"] == []


def test_pregate_selector_runs_when_any_candidate_reaches_triage_band(settings_data_dir):
    # Two attachments: a trusted det-skip and a triage-band candidate. The Pro
    # selector must run (once) and see BOTH candidates -- above-skip behaviour
    # is byte-identical to before.
    transport = _PrecisionTransport()
    transport.scripted_paragraphs["Collateral.pdf"] = [LONG_COLLATERAL_TEXT]
    transport.scripted_validation["Collateral.pdf"] = dict(SKIP_VALIDATION)
    transport.scripted_paragraphs["Maybe NDA.pdf"] = [LONG_COLLATERAL_TEXT]
    transport.scripted_validation["Maybe NDA.pdf"] = dict(TRIAGE_VALIDATION)

    seen_candidates: list[int] = []
    original = transport.select_nda_attachments

    def _spy(**kwargs):
        seen_candidates.append(len(kwargs.get("candidates") or []))
        return original(**kwargs)

    transport.select_nda_attachments = _spy

    _run_attachments(
        transport,
        [
            {"attachment_id": "att_1", "part_id": "1", "filename": "Collateral.pdf"},
            {"attachment_id": "att_2", "part_id": "2", "filename": "Maybe NDA.pdf"},
        ],
        _neutral_metadata(),
    )

    assert seen_candidates == [2]


def test_pregate_env_off_restores_always_call_behaviour(settings_data_dir, monkeypatch):
    monkeypatch.setenv(gmail_matter_inbox.NDA_GMAIL_AI_PREGATE_ENV, "0")
    transport = _PrecisionTransport()
    transport.scripted_paragraphs["Collateral.pdf"] = [LONG_COLLATERAL_TEXT]
    transport.scripted_validation["Collateral.pdf"] = dict(SKIP_VALIDATION)

    _run_attachments(
        transport,
        [{"attachment_id": "att_1", "part_id": "1", "filename": "Collateral.pdf"}],
        _neutral_metadata(),
    )

    assert transport.intake_calls != []
    assert transport.selector_calls != []


def test_pregate_inactive_without_escape_hatch_seam(settings_data_dir):
    # A transport that cannot answer the escape-hatch question must not pre-gate:
    # legacy behaviour (both AI calls) is preserved for older transports.
    class _NoSeamTransport(_PrecisionTransport):
        attachment_explicit_nda_signal = None  # seam absent (not callable)

    transport = _NoSeamTransport()
    transport.scripted_paragraphs["Collateral.pdf"] = [LONG_COLLATERAL_TEXT]
    transport.scripted_validation["Collateral.pdf"] = dict(SKIP_VALIDATION)

    _run_attachments(
        transport,
        [{"attachment_id": "att_1", "part_id": "1", "filename": "Collateral.pdf"}],
        _neutral_metadata(),
    )

    assert transport.intake_calls != []
    assert transport.selector_calls != []


# =========================================================================== #
# TIER 3 -- first-sync backfill cap
# =========================================================================== #

def test_first_sync_cap_env_parsing(monkeypatch):
    monkeypatch.delenv(gmail_integration.NDA_GMAIL_FIRST_SYNC_CAP_DAYS_ENV, raising=False)
    assert gmail_integration._first_sync_cap_days() == 14
    for raw, expected in (("7", 7), ("30", 30), ("0", 0)):
        monkeypatch.setenv(gmail_integration.NDA_GMAIL_FIRST_SYNC_CAP_DAYS_ENV, raw)
        assert gmail_integration._first_sync_cap_days() == expected, raw
    for raw in ("", "  ", "abc", "-3", "2.5"):
        monkeypatch.setenv(gmail_integration.NDA_GMAIL_FIRST_SYNC_CAP_DAYS_ENV, raw)
        assert gmail_integration._first_sync_cap_days() == 14, raw


def _make_user(sub: str = "backfill-subject") -> str:
    return user_store.upsert_google_user({"sub": sub, "email": "u@example.com"})["id"]


def test_backfill_state_new_user_capped_then_widens(settings_data_dir):
    user_id = _make_user()

    state = gmail_integration._inbound_backfill_state(user_id)
    assert state == {
        "effective_window_days": 14,
        "completed_through_days": 0,
        "target_days": 90,
    }

    user_store.record_gmail_backfill_progress(user_id, 14)
    assert gmail_integration._inbound_backfill_state(user_id) == {
        "effective_window_days": 28,
        "completed_through_days": 14,
        "target_days": 90,
    }

    user_store.record_gmail_backfill_progress(user_id, 84)
    assert gmail_integration._inbound_backfill_state(user_id) == {
        "effective_window_days": 90,
        "completed_through_days": 84,
        "target_days": 90,
    }

    user_store.record_gmail_backfill_progress(user_id, 90)
    assert gmail_integration._inbound_backfill_state(user_id) is None  # complete


def test_backfill_cursor_is_monotonic_up(settings_data_dir):
    user_id = _make_user()
    user_store.record_gmail_backfill_progress(user_id, 28)
    user_store.record_gmail_backfill_progress(user_id, 14)  # stale write ignored
    assert user_store.gmail_sync_status(user_id)["backfill_completed_through_days"] == 28


def test_backfill_exempts_existing_connected_user(settings_data_dir):
    user_id = _make_user()
    # Prior sync evidence (a recorded sync run) predating the cap => exempt.
    user_store.record_user_gmail_sync(
        user_id,
        {"imported": [], "skipped": [], "query": "q"},
        synced_at="2026-06-01T00:00:00+00:00",
    )
    assert gmail_integration._inbound_backfill_state(user_id) is None


def test_backfill_disabled_when_cap_env_zero(settings_data_dir, monkeypatch):
    monkeypatch.setenv(gmail_integration.NDA_GMAIL_FIRST_SYNC_CAP_DAYS_ENV, "0")
    user_id = _make_user()
    assert gmail_integration._inbound_backfill_state(user_id) is None


def test_status_payload_carries_backfill_progress(settings_data_dir):
    user_id = _make_user()
    user_store.record_gmail_backfill_progress(user_id, 28)
    payload = gmail_integration._gmail_sync_status_payload(user_id, app_settings.gmail_settings())
    assert payload["backfill"] == {
        "active": True,
        "completed_through_days": 28,
        "target_days": 90,
        "label": "backfilling: 28 of 90 days",
    }
    # Complete => inactive, no label.
    user_store.record_gmail_backfill_progress(user_id, 90)
    payload = gmail_integration._gmail_sync_status_payload(user_id, app_settings.gmail_settings())
    assert payload["backfill"]["active"] is False
    assert "label" not in payload["backfill"]


def test_status_exposes_exclude_lists_and_flag(settings_data_dir):
    status = gmail_integration.gmail_status()
    assert status["inbound_excluded_senders"] == list(
        app_settings.DEFAULT_GMAIL_INBOUND_EXCLUDED_SENDERS
    )
    assert status["inbound_excluded_senders_default"] == list(
        app_settings.DEFAULT_GMAIL_INBOUND_EXCLUDED_SENDERS
    )
    assert status["envelope_excludes_enabled"] is True


# --------------------------------------------------------------------------- #
# TIER 3 integration: the capped/widening poll against a dated, window-honouring
# inbox, including the cursor RE-ARM past a processed prefix.
# --------------------------------------------------------------------------- #

_DAY_MS = 86_400_000


class _WindowedDatedMessages:
    """Paged inbox whose list() honours BOTH ``newer_than:{N}d`` and
    ``before:<seconds>`` against a fixed now -- the minimum needed to exercise
    the backfill window + drain-cursor interplay."""

    def __init__(self, message_ids: list[str], internal_ms: dict[str, int], now_ms: int) -> None:
        self.message_ids = message_ids
        self.internal_ms = internal_ms
        self.now_ms = now_ms
        self.queries_seen: list[str] = []

    def _bounds(self, q: str) -> tuple[int | None, int | None]:
        newer_ms = before_ms = None
        for term in q.split():
            if term.startswith("newer_than:") and term.endswith("d"):
                try:
                    newer_ms = self.now_ms - int(term[len("newer_than:"):-1]) * _DAY_MS
                except ValueError:
                    pass
            elif term.startswith("before:"):
                try:
                    before_ms = int(term.split(":", 1)[1]) * 1000
                except ValueError:
                    pass
        return newer_ms, before_ms

    def list(self, *, userId: str, q: str, maxResults: int, pageToken: str = ""):
        self.queries_seen.append(q)
        newer_ms, before_ms = self._bounds(q)
        eligible = [
            m for m in self.message_ids
            if (newer_ms is None or self.internal_ms[m] >= newer_ms)
            and (before_ms is None or self.internal_ms[m] < before_ms)
        ]
        start = int(pageToken or "0")
        page = eligible[start:start + maxResults]
        next_start = start + len(page)
        next_token = str(next_start) if next_start < len(eligible) else ""
        return _Executable({"messages": [{"id": m} for m in page], "nextPageToken": next_token})

    def get(self, *, userId: str, id: str, format: str):
        return _Executable({"id": id, "payload": {}, "internalDate": str(self.internal_ms.get(id, 0))})


class _BackfillTransport(_CursorAwareLedgerTransport):
    """Cursor-aware transport + the backfill seams, over a day-spread inbox.

    Message ``msg_i`` is ``ages_days[i]`` days old. The backfill state machine
    mirrors the production one (cap 14, target 90) but lives in-memory so the
    integration test drives the REAL inbox loop without the user store.
    """

    CAP = 14
    WINDOW = 90

    def __init__(self, ages_days: list[float], *, import_limit: int) -> None:
        super().__init__(inbox_size=len(ages_days), import_limit=import_limit)
        self.now_ms = 1_700_000_000_000
        ids = [f"msg_{i:03d}" for i in range(len(ages_days))]
        self._internal_ms = {
            mid: self.now_ms - int(age * _DAY_MS) - 1 for mid, age in zip(ids, ages_days)
        }
        self.messages_api = _WindowedDatedMessages(ids, self._internal_ms, self.now_ms)
        self.service.users_api.messages_api = self.messages_api
        self.backfill_days = 0
        self.backfill_records: list[int] = []

    def default_inbound_query(self) -> str:
        return self.inbound_query_for_window(self.WINDOW)

    def inbound_query_for_window(self, window_days: int) -> str:
        return f"in:inbox has:attachment newer_than:{int(window_days)}d"

    def inbound_backfill_state(self, owner_user_id: str = ""):
        completed = self.backfill_days
        if completed <= 0:
            return {
                "effective_window_days": min(self.CAP, self.WINDOW),
                "completed_through_days": 0,
                "target_days": self.WINDOW,
            }
        if completed >= self.WINDOW:
            return None
        return {
            "effective_window_days": min(completed + self.CAP, self.WINDOW),
            "completed_through_days": completed,
            "target_days": self.WINDOW,
        }

    def record_inbound_backfill_progress(self, owner_user_id: str, days: int) -> None:
        self.backfill_records.append(int(days))
        self.backfill_days = max(self.backfill_days, int(days))


def test_first_poll_uses_capped_window_and_records_progress(import_limit_20):
    # 30 messages spread one per day (ages 0..29): only the 14 inside the capped
    # window are visible to the first poll.
    transport = _BackfillTransport([float(i) for i in range(30)], import_limit=import_limit_20)

    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1"
    )

    assert result["query"] == "in:inbox has:attachment newer_than:14d"
    assert len(result["imported"]) == 14
    assert transport.backfill_records == [14]
    assert result["backfill"]["completed_through_days"] == 14
    assert result["backfill"]["target_days"] == 90
    assert result["backfill"]["label"] == "backfilling: 14 of 90 days"


def test_subsequent_polls_widen_to_the_full_window(import_limit_20):
    # Ages 0..89 (one per day). Successive polls widen 14 -> 28 -> ... -> 90 and
    # each poll imports (at most) the next 14-day band.
    transport = _BackfillTransport([float(i) for i in range(90)], import_limit=import_limit_20)

    imported_total = 0
    for expected_window in (14, 28, 42, 56, 70, 84, 90):
        result = gmail_matter_inbox.import_inbound_matters(
            transport=transport, limit=999, owner_user_id="owner_1"
        )
        assert f"newer_than:{expected_window}d" in result["query"]
        imported_total += len(result["imported"])
        assert transport.backfill_days == expected_window
    assert imported_total == 90

    # Backfill complete: the next poll runs the full window with no backfill key.
    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=999, owner_user_id="owner_1"
    )
    assert result["query"] == "in:inbox has:attachment newer_than:90d"
    assert "backfill" not in result


def test_widened_band_is_reached_past_a_processed_prefix_beyond_max_scan(monkeypatch):
    # THE COORDINATOR'S REGRESSION: a processed prefix LONGER than max_scan sits
    # between the inbox head and the widened band. Pre-fix, ledger pre-skips
    # counted toward max_scan and the unarmed cursor was never re-armed, so the
    # bounded pass exhausted itself inside the prefix every poll and the widened
    # band was silently unreachable. Now: (a) the widen step ARMS the cursor at
    # the old window boundary so the drain pass jumps straight below the prefix,
    # and (b) ledger pre-skips don't count toward max_scan.
    monkeypatch.setenv(gmail_integration.NDA_GMAIL_IMPORT_LIMIT_ENV, "5")
    monkeypatch.setattr(
        gmail_integration,
        "MAX_GMAIL_IMPORT_LIMIT",
        gmail_integration._gmail_import_limit_from_env(),
    )
    # 120 messages inside the first 14d band (all already processed on prior
    # polls -- longer than max_scan = max(5*5, 5+100) = 105), plus 10 older
    # messages in the 14-28d widened band.
    ages = [i * (13.0 / 119.0) for i in range(120)] + [15.0 + i for i in range(10)]
    transport = _BackfillTransport(ages, import_limit=5)
    transport.backfill_days = 14  # the 0-14d band already drained
    for i in range(120):
        transport._ledger_store.add(f"msg_{i:03d}")

    old_band = {f"msg_{i:03d}" for i in range(120, 130)}
    imported: set[str] = set()
    for _poll in range(4):  # 10 old messages / 5 per poll, with slack
        result = gmail_matter_inbox.import_inbound_matters(
            transport=transport, limit=999, owner_user_id="owner_1"
        )
        imported |= {m["gmail_message_id"] for m in result["imported"]}
        if old_band <= imported:
            break

    assert old_band <= imported, f"widened band unreachable; got {sorted(imported)}"


def test_backfill_skipped_for_explicit_caller_query(import_limit_20):
    transport = _BackfillTransport([0.0], import_limit=import_limit_20)
    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport,
        limit=999,
        query="in:inbox custom",
        owner_user_id="owner_1",
    )
    assert result["query"] == "in:inbox custom"
    assert "backfill" not in result
    assert transport.backfill_records == []
