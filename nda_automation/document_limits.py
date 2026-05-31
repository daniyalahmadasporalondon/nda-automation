from __future__ import annotations

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
DOCUMENT_TOO_LARGE_MESSAGE = "The Word document is larger than the 10 MB upload limit."


class DocumentSizeError(ValueError):
    pass


def ensure_document_size(document_bytes: bytes) -> None:
    if len(document_bytes) > MAX_DOCUMENT_BYTES:
        raise DocumentSizeError(DOCUMENT_TOO_LARGE_MESSAGE)
