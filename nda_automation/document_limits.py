from __future__ import annotations

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
DOCUMENT_TOO_LARGE_MESSAGE = "The document is larger than the 10 MB upload limit."

MAX_REVIEW_TEXT_CHARS = 500_000
REVIEW_TEXT_TOO_LARGE_MESSAGE = (
    "The NDA text exceeds the review size limit. Trim the text or upload the document instead."
)


class DocumentSizeError(ValueError):
    pass


class ReviewTextTooLargeError(ValueError):
    pass


def ensure_document_size(document_bytes: bytes) -> None:
    if len(document_bytes) > MAX_DOCUMENT_BYTES:
        raise DocumentSizeError(DOCUMENT_TOO_LARGE_MESSAGE)


def ensure_review_text_size(text: str) -> None:
    if len(text) > MAX_REVIEW_TEXT_CHARS:
        raise ReviewTextTooLargeError(REVIEW_TEXT_TOO_LARGE_MESSAGE)
