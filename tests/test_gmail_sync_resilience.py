"""Gmail-poll CPU-resilience: single-pass extraction + cooperative GIL yields.

The prod incident: during the poll's heavy windows the single worker's GIL is
monopolized by CPU-bound document extraction, and bursts of static-asset
requests time out at the Render proxy (502) -- bricking page loads. Two of the
three fixes live in the poll path and are covered here:

* **Single-pass extraction (prong 2).** The poll used to extract every
  attachment TWICE: once at prepare/classification
  (``prepare_inbound_attachment``) and again inside matter creation
  (``create_matter_from_document``). Now the prepare stage runs the full
  ``extract_document`` ONCE -- with ``include_visual_profile=False``, exactly
  what the create stage's ``defer_pdf_conversion=True`` ingest computes -- and
  threads the FULL ``(document_type, paragraphs, extraction_quality)`` triple
  through the prepared-attachment seam into create. Carrying the full triple
  (not just paragraphs) matters: for DOCX the quality slot is
  ``detect_docx_tracked_changes``, which feeds the tracked-changes gate;
  dropping it would silently disable that gate.

* **Cooperative yields (prong 3).** ``_sync_cpu_yield`` briefly sleeps after
  each heavy per-attachment extraction and each heavy per-message import so
  request threads get scheduled during a long sync. Env-tunable
  (``NDA_GMAIL_SYNC_YIELD_MS``, default 50, clamped to 0..1000; <=0 disables).
  It fires ONLY on the heavy path -- never per scanned stub, so a 400-stub
  scan of already-imported mail adds no dead sleep.
"""
from __future__ import annotations

import importlib.util
import unittest
from io import BytesIO
from typing import Any
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from nda_automation import gmail_matter_inbox, ingestion_service
from nda_automation.docx_text import detect_docx_tracked_changes

from tests.test_inbound_flow_e2e import (
    EXPECTED_PDF_PARAGRAPHS,
    _FullInboundTransport,
)

PYPDF_AVAILABLE = importlib.util.find_spec("pypdf") is not None
requires_pypdf = pytest.mark.skipif(not PYPDF_AVAILABLE, reason="pypdf is not installed")

MESSAGE_METADATA = {"gmail_message_id": "msg_000"}
PDF_ATTACHMENT = {"attachment_id": "att_0", "part_id": "0", "filename": "inbound_nda_sample.pdf"}


def make_tracked_changes_docx() -> bytes:
    """A DOCX with unresolved tracked changes (insertion + deletion)."""
    document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:r><w:t xml:space="preserve">The term is </w:t></w:r>
      <w:ins w:id="1" w:author="Counterparty"><w:r><w:t>three (3)</w:t></w:r></w:ins>
      <w:del w:id="2" w:author="Counterparty"><w:r><w:delText>five (5)</w:delText></w:r></w:del>
      <w:r><w:t xml:space="preserve"> years.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>"""
    with BytesIO() as output:
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document_xml)
        return output.getvalue()


class _SinglePassTransport(_FullInboundTransport):
    """The e2e full-distance transport + the new full-extraction seam.

    ``extract_document`` mirrors production (``gmail_transport`` delegates to
    ``ingestion_service.extract_document``); calls and flags are recorded so a
    test can assert the prepare stage extracts once, without the visual
    profile. ``create_matter_from_document`` records its kwargs so a test can
    see exactly what was (or was not) threaded through.
    """

    def __init__(self, inbox_size: int = 1, *, import_limit: int = 20) -> None:
        super().__init__(inbox_size, import_limit=import_limit)
        self.extract_calls: list[dict[str, Any]] = []
        self.create_kwargs: list[dict[str, Any]] = []

    def extract_document(
        self,
        filename: str,
        document_bytes: bytes,
        *,
        include_visual_profile: bool = True,
    ):
        self.extract_calls.append(
            {"filename": filename, "include_visual_profile": include_visual_profile}
        )
        return ingestion_service.extract_document(
            filename, document_bytes, include_visual_profile=include_visual_profile
        )

    def create_matter_from_document(self, **kwargs):
        self.create_kwargs.append(dict(kwargs))
        return super().create_matter_from_document(**kwargs)


class _TwoPassTransport(_FullInboundTransport):
    """The pre-fix shape: no ``extract_document`` seam (older/fake transports)."""

    def __init__(self, inbox_size: int = 1, *, import_limit: int = 20) -> None:
        super().__init__(inbox_size, import_limit=import_limit)
        self.create_kwargs: list[dict[str, Any]] = []

    def create_matter_from_document(self, **kwargs):
        self.create_kwargs.append(dict(kwargs))
        return super().create_matter_from_document(**kwargs)


def _import_one(transport) -> tuple[dict[str, Any], dict[str, Any]]:
    """Drive prepare -> create for one attachment; return (candidate, matter)."""
    candidate, skip = gmail_matter_inbox.prepare_inbound_attachment(
        transport.service,
        "msg_000",
        dict(PDF_ATTACHMENT),
        dict(MESSAGE_METADATA),
        transport=transport,
        owner_user_id="owner_1",
    )
    assert skip is None and candidate is not None
    matter, skip = gmail_matter_inbox.create_matter_from_prepared_attachment(
        candidate,
        dict(MESSAGE_METADATA),
        transport=transport,
        owner_user_id="owner_1",
    )
    assert skip is None and matter is not None
    return candidate, matter


# --------------------------------------------------------------------------- #
# Prong 2: extraction runs exactly ONCE per attachment on the poll path
# --------------------------------------------------------------------------- #
@requires_pypdf
def test_poll_import_extracts_each_attachment_exactly_once():
    transport = _SinglePassTransport()
    # Count EVERY extraction, wherever it originates: the transport seam calls
    # the patched ingestion_service.extract_document, and so would the
    # create-stage internal re-extraction if the reuse failed.
    with patch.object(
        ingestion_service, "extract_document", wraps=ingestion_service.extract_document
    ) as extraction:
        candidate, matter = _import_one(transport)

    assert extraction.call_count == 1
    # The single pass ran through the transport seam with the visual profile
    # skipped -- the exact extraction the create stage's defer_pdf_conversion
    # ingest would have computed (paragraphs are byte-identical either way).
    assert transport.extract_calls == [
        {"filename": "inbound_nda_sample.pdf", "include_visual_profile": False}
    ]
    # ... and the full triple was threaded into create.
    (create_kwargs,) = transport.create_kwargs
    document_type, paragraphs, _quality = create_kwargs["precomputed_extraction"]
    assert document_type == "pdf"
    assert [p["text"] for p in paragraphs] == EXPECTED_PDF_PARAGRAPHS
    assert candidate["extraction_result"][1] is paragraphs
    assert EXPECTED_PDF_PARAGRAPHS[0] in matter.get("extracted_text", "")


@requires_pypdf
def test_single_pass_import_content_identical_to_two_pass():
    single = _SinglePassTransport()
    two_pass = _TwoPassTransport()

    with patch.object(
        ingestion_service, "extract_document", wraps=ingestion_service.extract_document
    ) as extraction:
        _candidate, single_matter = _import_one(single)
        single_calls = extraction.call_count
        _candidate, two_pass_matter = _import_one(two_pass)
        two_pass_calls = extraction.call_count - single_calls

    # The fix removed exactly one full extraction per attachment...
    assert single_calls == 1
    assert two_pass_calls == 2
    # ... and changed NOTHING about the imported matter's content.
    assert single_matter["extracted_text"] == two_pass_matter["extracted_text"]
    assert single_matter.get("review_result") == two_pass_matter.get("review_result")
    assert single_matter.get("source_type") == two_pass_matter.get("source_type")


def test_docx_tracked_changes_quality_threads_through_intact():
    # The regression the full-triple threading prevents: for DOCX the quality
    # slot is detect_docx_tracked_changes -- reusing paragraphs while dropping
    # quality would silently disable the tracked-changes gate downstream.
    docx_bytes = make_tracked_changes_docx()
    transport = _SinglePassTransport()
    transport._pdf_bytes = docx_bytes  # attachment_bytes serves these bytes

    attachment = {"attachment_id": "att_0", "part_id": "0", "filename": "redlined.docx"}
    candidate, skip = gmail_matter_inbox.prepare_inbound_attachment(
        transport.service,
        "msg_000",
        attachment,
        dict(MESSAGE_METADATA),
        transport=transport,
        owner_user_id="owner_1",
    )
    assert skip is None and candidate is not None

    document_type, paragraphs, quality = candidate["extraction_result"]
    assert document_type == "docx"
    # The threaded quality is byte-for-byte what the create stage would have
    # recomputed itself (the tracked-changes gate's input is unchanged).
    assert quality == detect_docx_tracked_changes(docx_bytes)
    assert quality["has_tracked_changes"] is True
    assert quality["tracked_insertions"] == 1
    assert quality["tracked_deletions"] == 1
    # The in-force baseline (not the all-accepted fabrication) is what imports.
    assert [p["text"] for p in paragraphs] == ["The term is five (5) years."]

    matter, skip = gmail_matter_inbox.create_matter_from_prepared_attachment(
        candidate,
        dict(MESSAGE_METADATA),
        transport=transport,
        owner_user_id="owner_1",
    )
    assert skip is None and matter is not None
    (create_kwargs,) = transport.create_kwargs
    assert create_kwargs["precomputed_extraction"][2] == detect_docx_tracked_changes(docx_bytes)
    assert matter["extracted_text"] == "The term is five (5) years."


@requires_pypdf
def test_transport_without_extract_seam_degrades_to_two_pass():
    # Older/fake transports without the extract_document seam keep the exact
    # pre-fix behavior: no threading, create extracts for itself.
    transport = _TwoPassTransport()
    candidate, skip = gmail_matter_inbox.prepare_inbound_attachment(
        transport.service,
        "msg_000",
        dict(PDF_ATTACHMENT),
        dict(MESSAGE_METADATA),
        transport=transport,
        owner_user_id="owner_1",
    )
    assert skip is None and candidate is not None
    assert candidate["extraction_result"] is None

    matter, skip = gmail_matter_inbox.create_matter_from_prepared_attachment(
        candidate,
        dict(MESSAGE_METADATA),
        transport=transport,
        owner_user_id="owner_1",
    )
    assert skip is None and matter is not None
    (create_kwargs,) = transport.create_kwargs
    assert "precomputed_extraction" not in create_kwargs


# --------------------------------------------------------------------------- #
# Prong 3: cooperative GIL yields on the heavy path
# --------------------------------------------------------------------------- #
class SyncCpuYieldUnitTests(unittest.TestCase):
    def _recorded_sleeps(self, env_value: str | None) -> list[float]:
        sleeps: list[float] = []
        env_patch = (
            patch.dict("os.environ", {gmail_matter_inbox.GMAIL_SYNC_YIELD_MS_ENV: env_value})
            if env_value is not None
            else patch.dict("os.environ")
        )
        with env_patch:
            if env_value is None:
                # conftest pins the yield OFF suite-wide; the default-path test
                # must see an unset env.
                import os

                os.environ.pop(gmail_matter_inbox.GMAIL_SYNC_YIELD_MS_ENV, None)
            with patch.object(gmail_matter_inbox.time, "sleep", sleeps.append):
                gmail_matter_inbox._sync_cpu_yield()
        return sleeps

    def test_default_yield_is_50ms(self):
        self.assertEqual(self._recorded_sleeps(None), [0.05])

    def test_env_tunes_the_yield(self):
        self.assertEqual(self._recorded_sleeps("125"), [0.125])

    def test_zero_disables_the_yield(self):
        self.assertEqual(self._recorded_sleeps("0"), [])

    def test_negative_disables_the_yield(self):
        self.assertEqual(self._recorded_sleeps("-5"), [])

    def test_garbage_falls_back_to_default(self):
        self.assertEqual(self._recorded_sleeps("not-a-number"), [0.05])

    def test_yield_is_clamped_to_one_second(self):
        self.assertEqual(self._recorded_sleeps("999999"), [1.0])


@requires_pypdf
def test_poll_yields_between_heavy_messages_and_attachments(monkeypatch):
    # Re-enable the yield (conftest pins it off suite-wide for speed) and run a
    # REAL 2-message poll; the yield must fire once per extracted attachment
    # plus once per heavy message -- and change nothing about the outcome.
    monkeypatch.setenv(gmail_matter_inbox.GMAIL_SYNC_YIELD_MS_ENV, "50")
    sleeps: list[float] = []
    monkeypatch.setattr(gmail_matter_inbox.time, "sleep", sleeps.append)

    transport = _SinglePassTransport(inbox_size=2, import_limit=20)
    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=20, owner_user_id="owner_1"
    )

    assert len(result["imported"]) == 2
    # 2 messages x (1 attachment yield + 1 per-message yield) = 4 yields of 50ms.
    assert sleeps.count(0.05) == 4


@requires_pypdf
def test_poll_yield_disabled_is_a_no_op(monkeypatch):
    monkeypatch.setenv(gmail_matter_inbox.GMAIL_SYNC_YIELD_MS_ENV, "0")
    sleeps: list[float] = []
    monkeypatch.setattr(gmail_matter_inbox.time, "sleep", sleeps.append)

    transport = _SinglePassTransport(inbox_size=2, import_limit=20)
    result = gmail_matter_inbox.import_inbound_matters(
        transport=transport, limit=20, owner_user_id="owner_1"
    )

    assert len(result["imported"]) == 2
    assert sleeps == []
