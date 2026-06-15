"""TEST DOUBLE for DocuSign — automated unit tests ONLY. Never the product path.

This module is imported only by the test suite. The running app's factory
(:func:`docusign_integration.get_client`) NEVER returns this class; it always
returns the real :class:`docusign_integration.HttpDocuSignClient`. The double
exists so the workflow + route tests can exercise the full send -> sign ->
completed -> signed-artifact flow deterministically without a live DocuSign
account.

It implements the same :class:`docusign_integration.DocuSignClient` interface
and adds an :meth:`advance` control to step a simulated envelope through the real
status ladder (``sent -> delivered -> completed``), minting a small valid
placeholder executed PDF on completion so the downstream signed-artifact capture
has real bytes to store.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .docusign_integration import (
    DEFAULT_EMAIL_SUBJECT,
    DEFAULT_SIGNING_ORDER,
    SIGNING_ORDERS,
    STATUS_COMPLETED,
    STATUS_CREATED,
    STATUS_DELIVERED,
    STATUS_SENT,
    STATUS_VOIDED,
    TERMINAL_STATUSES,
    DocuSignEnvelopeNotFoundError,
    DocuSignError,
    Signer,
    normalize_signers,
)

_HAPPY_PATH = (STATUS_CREATED, STATUS_SENT, STATUS_DELIVERED, STATUS_COMPLETED)


@dataclass
class _Envelope:
    envelope_id: str
    status: str
    filename: str
    email_subject: str
    signers: list[dict[str, Any]]
    document_bytes: bytes
    history: list[dict[str, str]] = field(default_factory=list)
    void_reason: str = ""


class FakeDocuSignClient:
    """In-memory DocuSign simulator — TEST ONLY (see module docstring)."""

    name = "fake"

    def __init__(self, *, auto_complete: bool = False) -> None:
        self._auto_complete = auto_complete
        self._lock = threading.RLock()
        self._envelopes: dict[str, _Envelope] = {}
        self._counter = 0

    def create_envelope(
        self,
        document_bytes: bytes,
        filename: str,
        signers: list[Signer],
        *,
        signing_order: str = DEFAULT_SIGNING_ORDER,
        email_subject: str = DEFAULT_EMAIL_SUBJECT,
    ) -> dict[str, Any]:
        normalized = normalize_signers(signers, signing_order=signing_order)
        if not isinstance(document_bytes, (bytes, bytearray)) or not document_bytes:
            raise DocuSignError("No document bytes to send for signature.")
        with self._lock:
            self._counter += 1
            envelope_id = self._mint_id(self._counter)
            now = _now_iso()
            envelope = _Envelope(
                envelope_id=envelope_id,
                status=STATUS_SENT,
                filename=str(filename or "document.pdf"),
                email_subject=str(email_subject or DEFAULT_EMAIL_SUBJECT),
                signers=[signer.to_dict() for signer in normalized],
                document_bytes=bytes(document_bytes),
            )
            envelope.history.append({"status": STATUS_CREATED, "at": now})
            envelope.history.append({"status": STATUS_SENT, "at": now})
            self._envelopes[envelope_id] = envelope
            if self._auto_complete:
                self._walk_to_completed(envelope)
            return {"envelope_id": envelope_id, "status": envelope.status}

    def get_envelope_status(self, envelope_id: str) -> str:
        with self._lock:
            return self._require(envelope_id).status

    def download_completed(self, envelope_id: str) -> bytes:
        with self._lock:
            envelope = self._require(envelope_id)
            if envelope.status != STATUS_COMPLETED:
                raise DocuSignError(f"Envelope {envelope_id} is {envelope.status}, not completed.")
            return _placeholder_executed_pdf(envelope)

    def void_envelope(self, envelope_id: str, reason: str) -> dict[str, Any]:
        with self._lock:
            envelope = self._require(envelope_id)
            if envelope.status == STATUS_COMPLETED:
                raise DocuSignError("A completed envelope cannot be voided.")
            envelope.status = STATUS_VOIDED
            envelope.void_reason = str(reason or "")
            envelope.history.append({"status": STATUS_VOIDED, "at": _now_iso()})
            return {"envelope_id": envelope_id, "status": envelope.status}

    # --- simulation controls (tests) ---------------------------------------
    def advance(self, envelope_id: str) -> str:
        with self._lock:
            envelope = self._require(envelope_id)
            if envelope.status in TERMINAL_STATUSES:
                return envelope.status
            try:
                index = _HAPPY_PATH.index(envelope.status)
            except ValueError:
                index = 0
            envelope.status = _HAPPY_PATH[min(index + 1, len(_HAPPY_PATH) - 1)]
            envelope.history.append({"status": envelope.status, "at": _now_iso()})
            return envelope.status

    def complete(self, envelope_id: str) -> str:
        with self._lock:
            envelope = self._require(envelope_id)
            if envelope.status not in TERMINAL_STATUSES:
                self._walk_to_completed(envelope)
            return envelope.status

    def envelope_history(self, envelope_id: str) -> list[dict[str, str]]:
        with self._lock:
            return list(self._require(envelope_id).history)

    # --- internals ---------------------------------------------------------
    def _walk_to_completed(self, envelope: _Envelope) -> None:
        while envelope.status != STATUS_COMPLETED:
            index = _HAPPY_PATH.index(envelope.status)
            envelope.status = _HAPPY_PATH[index + 1]
            envelope.history.append({"status": envelope.status, "at": _now_iso()})

    def _require(self, envelope_id: str) -> _Envelope:
        envelope = self._envelopes.get(str(envelope_id or ""))
        if envelope is None:
            raise DocuSignEnvelopeNotFoundError(f"Envelope {envelope_id!r} not found.")
        return envelope

    @staticmethod
    def _mint_id(counter: int) -> str:
        digest = hashlib.sha1(f"fake-envelope-{counter}".encode("utf-8")).hexdigest()
        return f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"


def _placeholder_executed_pdf(envelope: _Envelope) -> bytes:
    lines = [
        "EXECUTED NDA (test double)",
        f"Envelope: {envelope.envelope_id}",
        f"Document: {envelope.filename}",
    ]
    for signer in envelope.signers:
        lines.append(f"Signed by: {signer.get('name', '')} <{signer.get('email', '')}>")
    return _minimal_pdf(lines)


def _minimal_pdf(text_lines: list[str]) -> bytes:
    safe_lines = [_pdf_escape(line) for line in text_lines] or [_pdf_escape("EXECUTED")]
    commands = ["BT", "/F1 14 Tf", "72 720 Td", "16 TL"]
    for index, line in enumerate(safe_lines):
        if index:
            commands.append("T*")
        commands.append(f"({line}) Tj")
    commands.append("ET")
    content_stream = "\n".join(commands).encode("latin-1", "replace")
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content_stream)).encode("latin-1") + b" >>\nstream\n"
        + content_stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += f"{index} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"
    xref_offset = len(pdf)
    pdf += f"xref\n0 {len(objects) + 1}\n".encode("latin-1")
    pdf += b"0000000000 65535 f \n"
    for offset in offsets:
        pdf += f"{offset:010d} 00000 n \n".encode("latin-1")
    pdf += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n"
    ).encode("latin-1")
    return bytes(pdf)


def _pdf_escape(value: str) -> str:
    return str(value or "").replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["FakeDocuSignClient", "SIGNING_ORDERS"]
