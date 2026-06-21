"""Regression: the DOCX redline content-coverage gate must RUN on the deferred
(production-default) export path.

Confirmed silent-data-loss defect: both real intake paths create matters with
``defer_ai_review=True`` (manual upload + inbound). The async review then stores
the engine result verbatim, and neither the deterministic checker nor the
active-engine review sets ``review_result["extracted_text"]`` -- only the (now
test-only) EAGER create path's ``attach_document_source`` does. On export,
``_review_result_for_export`` deepcopied that stored result and the DOCX renderer
was handed ``expected_source_text=str(review_result.get("extracted_text") or "")``
==> ``""``. ``verify_export_content_coverage`` short-circuits to ``[]`` on an empty
source text, so the length / accepted-paragraph-SEQUENCE / structural-count checks
ALL skipped: a redline that silently DROPPED / reordered / duplicated source
clauses shipped UNVERIFIED on an outbound legal document.

The existing coverage tests in ``test_docx_export``/``test_pdf_redline_coverage``
call ``verify_export_content_coverage`` directly (or patch
``_review_result_for_export``), so they hand the gate a non-empty source text by
construction and DO NOT exercise the deferred export path that produces the empty
one. These tests drive the FULL ``build_matter_redline`` export through a real
``InMemoryMatterRepository`` matter whose stored ``review_result`` has NO
``extracted_text`` -- the real deferred-then-refreshed shape -- so the gap is
covered end to end.

The fix stamps the matter's authoritative ``extracted_text`` (the SAME
``extracted_text_from_paragraphs`` value the redline is built against) onto the
deepcopied result in ``_review_result_for_export`` so the gate runs.
"""
from __future__ import annotations

import re
import unittest
from io import BytesIO
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from nda_automation import docx_package_renderer, redline_export_service, source_redline_docx
from nda_automation.docx_export import SourceRedlinePackage
from nda_automation.docx_text import extract_docx_paragraphs
from nda_automation.matter_repository import InMemoryMatterRepository
from nda_automation.redline_export_service import (
    DocxOpenHealthError,
    build_matter_redline,
)
from nda_automation.review_engine import (
    REVIEW_ENGINE_DETERMINISTIC,
    review_nda_with_active_engine,
)

from tests.test_docx_export import make_source_docx

SOURCE_PARAGRAPHS = [
    "Intro paragraph.",
    "This Agreement shall be governed by the laws of California.",
    "The confidentiality obligations survive for three years.",
]


def _drop_paragraphs_keep_health(docx_bytes: bytes, *, keep_first: int) -> bytes:
    """Strip all but the first ``keep_first`` body paragraphs while preserving every
    other package part (styles/settings/sectPr/rels) so the result stays HEALTH-valid
    but CONTENT-short. This isolates the content-coverage gate from the open-health
    gate: a bytes truncation that also broke health would prove nothing about
    coverage (health is checked first and short-circuits coverage)."""
    with ZipFile(BytesIO(docx_bytes)) as zin:
        parts = {name: zin.read(name) for name in zin.namelist()}
    doc = parts["word/document.xml"].decode("utf-8")
    body = re.search(r"(<w:body>)(.*)(</w:body>)", doc, re.S)
    assert body is not None, "rendered DOCX has no w:body"
    inner = body.group(2)
    paragraphs = re.findall(r"<w:p\b.*?</w:p>", inner, re.S)
    sect = re.search(r"<w:sectPr\b.*?</w:sectPr>", inner, re.S)
    new_inner = "".join(paragraphs[:keep_first]) + (sect.group(0) if sect else "")
    new_doc = doc[: body.start()] + body.group(1) + new_inner + body.group(3) + doc[body.end() :]
    parts["word/document.xml"] = new_doc.encode("utf-8")
    out = BytesIO()
    with ZipFile(out, "w", ZIP_DEFLATED) as zout:
        for name, data in parts.items():
            zout.writestr(name, data)
    return out.getvalue()


class DeferredRedlineCoverageGateTests(unittest.TestCase):
    def _deferred_matter(self):
        """A matter in the real deferred-then-refreshed shape: stored
        ``review_result`` carries NO ``extracted_text`` (the production default; only
        the eager create path's ``attach_document_source`` sets it), while the matter
        itself carries the authoritative ``extracted_text``."""
        source_docx = make_source_docx(SOURCE_PARAGRAPHS)
        paragraphs = extract_docx_paragraphs(source_docx)
        source_text = "\n\n".join(str(paragraph["text"]) for paragraph in paragraphs)
        review_result = review_nda_with_active_engine(
            source_text,
            paragraphs=paragraphs,
            force_engine=REVIEW_ENGINE_DETERMINISTIC,
        )
        # Guard the premise: the deferred/refresh path never stamps extracted_text.
        self.assertNotIn("extracted_text", review_result)

        repository = InMemoryMatterRepository()
        matter = repository.create_matter(
            source_filename="Mutual NDA.docx",
            document_bytes=source_docx,
            extracted_text=source_text,
            review_result=review_result,
            triage={},
        )
        return repository, matter, source_text

    def _capture_expected_source_text(self):
        """Wrap render_source_redline_package to record the expected_source_text the
        renderer actually receives (the value the gate keys on)."""
        captured: dict[str, str | None] = {}
        real_render = docx_package_renderer.render_source_redline_package

        def spy(source_docx, review_result, **kwargs):
            captured["expected_source_text"] = kwargs.get("expected_source_text")
            return real_render(source_docx, review_result, **kwargs)

        return captured, spy

    def test_complete_redline_on_deferred_path_exports_cleanly(self):
        # No false positive: a legitimately-complete redline on the deferred path
        # exports without tripping the (now-running) coverage gate.
        repository, matter, source_text = self._deferred_matter()
        captured, spy = self._capture_expected_source_text()
        with patch.object(
            redline_export_service.docx_package_renderer,
            "render_source_redline_package",
            side_effect=spy,
        ):
            export = build_matter_redline(matter["id"], {}, persist=False, repository=repository)

        self.assertTrue(export.data)
        # The fix is load-bearing for the gate to have run at all: the renderer was
        # handed the matter's real source text, not "".
        self.assertEqual(captured["expected_source_text"], source_text)
        self.assertTrue(captured["expected_source_text"])

    def test_truncated_redline_on_deferred_path_is_blocked_by_coverage_gate(self):
        # The load-bearing test. A redline that dropped 2 of 3 source paragraphs (kept
        # health-valid) must be BLOCKED by the content-coverage gate -- proving the
        # gate runs on the deferred path. Before the fix expected_source_text was ""
        # and this shipped silently (see the base-commit companion proof).
        repository, matter, source_text = self._deferred_matter()
        captured, spy = self._capture_expected_source_text()

        real_builder = source_redline_docx.build_source_redline_package

        def truncating_builder(source_docx, review_result, **kwargs):
            package = real_builder(source_docx, review_result, **kwargs)
            data = package.data if isinstance(package, SourceRedlinePackage) else package
            truncated = _drop_paragraphs_keep_health(data, keep_first=1)
            return SourceRedlinePackage(data=truncated, anchor_uncertain_redlines=[])

        with patch.object(
            docx_package_renderer,
            "build_source_redline_docx",
            side_effect=truncating_builder,
        ), patch.object(
            redline_export_service.docx_package_renderer,
            "render_source_redline_package",
            side_effect=spy,
        ):
            with self.assertRaises(DocxOpenHealthError) as caught:
                build_matter_redline(matter["id"], {}, persist=False, repository=repository)

        # Blocked by the CONTENT-coverage check specifically (not open-health), and the
        # detail names a coverage shortfall.
        self.assertIn("content-coverage check", caught.exception.args[0])
        self.assertTrue(caught.exception.details)
        self.assertTrue(
            any(
                "source content" in detail or "source characters" in detail
                for detail in caught.exception.details
            ),
            caught.exception.details,
        )
        # The gate only fires because expected_source_text was non-empty (the fix).
        self.assertEqual(captured["expected_source_text"], source_text)
        self.assertTrue(captured["expected_source_text"])


if __name__ == "__main__":
    unittest.main()
