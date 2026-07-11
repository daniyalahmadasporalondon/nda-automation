"""End-to-end tests for the on-demand review reading-order carry.

These exercise ``ingestion_service._attach_reading_order_signal`` -- the seam that
re-derives the extractor's reading-order confidence signal at review time and rides it
onto the review result so a degraded PDF surfaces a LOUD banner instead of a silent
verdict. They run against the REAL fixture corpus + the REAL extractor, so they prove
the whole path (bytes -> pdf_text reading-order signal -> review result) and the
critical asymmetry: a clean single-column NDA carries NOTHING.
"""
from __future__ import annotations

import logging
import os
from types import SimpleNamespace

from nda_automation import ingestion_service
from nda_automation.review_result_contract import READING_ORDER_RESULT_FIELD

_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "pdf_reading_order")


def _fixture_bytes(name: str) -> bytes:
    with open(os.path.join(_FIXTURE_DIR, f"{name}.pdf"), "rb") as handle:
        return handle.read()


class _FakeRepo:
    """Minimal repository exposing only what the carry needs: the source bytes."""

    def __init__(self, document_bytes: bytes | None):
        self._document_bytes = document_bytes

    def get_source_document_bytes(self, matter):  # noqa: ANN001 - test double
        return self._document_bytes


def _fake_telemetry():
    counts: dict[str, int] = {}

    def increment(name, amount: int = 1):
        counts[name] = counts.get(name, 0) + amount

    return SimpleNamespace(increment=increment, counts=counts)


def _run(name: str, *, filename: str | None = None, document_bytes=..., matter_extra=None):
    review_result: dict = {"clauses": []}
    matter = {"source_filename": filename if filename is not None else f"{name}.pdf"}
    if matter_extra:
        matter.update(matter_extra)
    bytes_ = _fixture_bytes(name) if document_bytes is ... else document_bytes
    telemetry = _fake_telemetry()
    ingestion_service._attach_reading_order_signal(
        review_result,
        matter,
        repository=_FakeRepo(bytes_),
        matter_id="matter_test",
        owner_user_id="owner_1",
        telemetry=telemetry,
    )
    return review_result, telemetry


def test_two_column_pdf_surfaces_degraded_signal_and_warning():
    result, telemetry = _run("pos_two_column_clean")
    block = result[READING_ORDER_RESULT_FIELD]
    assert block["degraded"] is True
    assert block["columns_detected"] == 2
    assert [w["type"] for w in result["review_warnings"]] == ["pdf_reading_order_uncertain"]
    assert telemetry.counts.get("pdf_reading_order_degraded") == 1


def test_letter_spaced_garble_surfaces_fragmented_warning():
    result, telemetry = _run("garble_letter_spaced_tracking")
    assert result[READING_ORDER_RESULT_FIELD]["garbled"] is True
    assert [w["type"] for w in result["review_warnings"]] == ["pdf_fragmented_text"]
    assert telemetry.counts.get("pdf_reading_order_degraded") == 1


def test_clean_single_column_pdf_surfaces_nothing_to_the_reviewer():
    # THE anti-warning-fatigue guarantee: a normal single-column NDA (wide margins +
    # centered title) records the provenance field but adds NO warning and NO telemetry,
    # so the frontend banner stays hidden.
    result, telemetry = _run("neg_single_col_wide_margins_centered_title")
    assert result[READING_ORDER_RESULT_FIELD]["degraded"] is False
    assert "review_warnings" not in result
    assert telemetry.counts.get("pdf_reading_order_degraded") is None


def test_two_cell_party_table_is_not_a_false_positive():
    # A single-column NDA with a two-cell party/address table must NOT split -> no banner.
    result, _ = _run("neg_two_cell_party_table")
    assert result[READING_ORDER_RESULT_FIELD]["degraded"] is False
    assert "review_warnings" not in result


def test_degraded_extraction_emits_structured_backfill_log(caplog):
    with caplog.at_level(logging.WARNING, logger="nda_automation.ingestion_service"):
        _run("pos_two_column_clean")
    line = next(r.getMessage() for r in caplog.records if "reading-order degraded" in r.getMessage())
    # Structured + greppable so a future backfill can find every affected matter.
    assert "matter=matter_test" in line
    assert "owner=owner_1" in line
    assert "columns=2" in line
    assert "reasons=column_reconstructed" in line


def test_non_pdf_matter_is_skipped_entirely():
    # A native DOCX matter has no reading-order concept: no re-parse, no field, no warning.
    result, telemetry = _run(
        "pos_two_column_clean", filename="contract.docx", document_bytes=b"ignored"
    )
    assert READING_ORDER_RESULT_FIELD not in result
    assert "review_warnings" not in result
    assert telemetry.counts == {}


def test_missing_source_bytes_is_fail_open():
    result, telemetry = _run("pos_two_column_clean", document_bytes=None)
    assert READING_ORDER_RESULT_FIELD not in result
    assert telemetry.counts == {}


def test_unreadable_bytes_never_raise_and_leave_review_untouched():
    result, telemetry = _run("pos_two_column_clean", document_bytes=b"%PDF-1.4 not real")
    # Fail-open: a broken extraction must not add a field, a warning, or raise.
    assert READING_ORDER_RESULT_FIELD not in result
    assert "review_warnings" not in result
    assert telemetry.counts == {}
