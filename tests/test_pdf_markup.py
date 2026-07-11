"""Tests for the user-placed PDF markup feature.

Two layers are exercised:

* ``bake_user_annotations`` against a small fixture PDF built with fitz — real
  baking, opened back with fitz to assert annotation counts / content / clamping.
* The CRUD + marked-up-pdf routes driven with a fake handler and an in-memory
  matter repository — persistence, id/author/created_at stamping, the cap,
  owner-scope, and the DOCX-only 400.
"""

from __future__ import annotations

import unittest

from nda_automation import annotated_pdf_export, pdf_markup, telemetry
from nda_automation.matter_repository import InMemoryMatterRepository
from nda_automation.routes import pdf_markup as pdf_markup_routes
from nda_automation.routes.common import parse_matter_id


def _fitz():
    try:
        import fitz
    except ImportError:
        return None
    return fitz


def _sample_pdf_bytes(pages: int = 1):
    fitz = _fitz()
    if fitz is None:
        raise unittest.SkipTest("PyMuPDF is not installed")
    document = fitz.open()
    for index in range(pages):
        page = document.new_page()
        page.insert_text((72, 72), f"This is page {index + 1} of the agreement.")
    data = document.write()
    document.close()
    return data


# --- bake ------------------------------------------------------------------


class BakeUserAnnotationsTests(unittest.TestCase):
    def setUp(self):
        if _fitz() is None:
            self.skipTest("PyMuPDF is not installed")

    def test_bake_places_each_annotation_type_with_scaled_coords(self):
        fitz = _fitz()
        source = _sample_pdf_bytes()
        annotations = [
            {
                "id": "a1",
                "page": 1,
                "type": "comment",
                "rect": {"x": 0.1, "y": 0.1, "w": 0.0, "h": 0.0},
                "text": "Please clarify this clause.",
            },
            {
                "id": "a2",
                "page": 1,
                "type": "highlight",
                "rect": {"x": 0.1, "y": 0.2, "w": 0.4, "h": 0.03},
                "color": "#ffcc00",
            },
            {
                "id": "a3",
                "page": 1,
                "type": "strikethrough",
                "rect": {"x": 0.1, "y": 0.3, "w": 0.4, "h": 0.03},
            },
        ]

        baked = pdf_markup.bake_user_annotations(source, annotations)

        document = fitz.open(stream=baked, filetype="pdf")
        try:
            page = document[0]
            annots = list(page.annots() or [])
            self.assertEqual(len(annots), 3)
            types = {a.type[1] for a in annots}
            self.assertEqual(types, {"Text", "Highlight", "StrikeOut"})
            comment = next(a for a in annots if a.type[1] == "Text")
            self.assertEqual(comment.info.get("content"), "Please clarify this clause.")
        finally:
            document.close()

    def test_normalized_coords_map_to_top_left_scaled_points(self):
        # Confirms the shared contract: normalized (top-left origin) -> PDF points
        # via x*W / y*H, asserted on a LIVE document (reading .rect on a reopened
        # PyMuPDF annot is unstable in this build, so we verify on the live doc).
        fitz = _fitz()
        document = fitz.open(stream=_sample_pdf_bytes(), filetype="pdf")
        try:
            page = document[0]
            width, height = page.rect.width, page.rect.height
            x, y, w, h = 0.1, 0.2, 0.4, 0.03
            box = fitz.Rect(x * width, y * height, (x + w) * width, (y + h) * height)
            annot = page.add_highlight_annot(box)
            annot.update()
            # PyMuPDF pads a highlight's bounding rect a few points for its
            # appearance stream, so allow a small tolerance around the contract.
            self.assertAlmostEqual(annot.rect.x0, x * width, delta=8.0)
            self.assertAlmostEqual(annot.rect.y0, y * height, delta=8.0)
            self.assertAlmostEqual(annot.rect.x1, (x + w) * width, delta=8.0)
            # The box is firmly in the top-left quadrant — origin agreement holds.
            self.assertLess(annot.rect.x0, width / 2)
            self.assertLess(annot.rect.y0, height / 2)
        finally:
            document.close()

    def test_bake_skips_out_of_range_page_without_crashing(self):
        fitz = _fitz()
        source = _sample_pdf_bytes(pages=1)
        annotations = [
            {"id": "ok", "page": 1, "type": "highlight", "rect": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.05}},
            {"id": "off", "page": 9, "type": "highlight", "rect": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.05}},
        ]
        baked = pdf_markup.bake_user_annotations(source, annotations)
        document = fitz.open(stream=baked, filetype="pdf")
        try:
            self.assertEqual(len(list(document[0].annots() or [])), 1)
        finally:
            document.close()

    def test_bake_skips_malformed_and_unknown(self):
        fitz = _fitz()
        source = _sample_pdf_bytes()
        annotations = [
            {"id": "bad-rect", "page": 1, "type": "highlight", "rect": {"x": "nope", "y": 0.1, "w": 0.2, "h": 0.05}},
            {"id": "no-rect", "page": 1, "type": "highlight"},
            {"id": "bad-type", "page": 1, "type": "scribble", "rect": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.05}},
            "not-a-dict",
            {"id": "good", "page": 1, "type": "highlight", "rect": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.05}},
        ]
        baked = pdf_markup.bake_user_annotations(source, annotations)
        document = fitz.open(stream=baked, filetype="pdf")
        try:
            self.assertEqual(len(list(document[0].annots() or [])), 1)
        finally:
            document.close()

    def test_bake_clamps_out_of_range_coords_into_page(self):
        fitz = _fitz()
        source = _sample_pdf_bytes()
        annotations = [
            {
                "id": "huge",
                "page": 1,
                "type": "highlight",
                # x/w deliberately out of [0,1]; should clamp to a box inside the page.
                "rect": {"x": -0.5, "y": 0.5, "w": 5.0, "h": 0.05},
            }
        ]
        # Out-of-range coords are clamped (not rejected), so the highlight is
        # still produced and bakes into a valid in-page box.
        baked = pdf_markup.bake_user_annotations(source, annotations)
        document = fitz.open(stream=baked, filetype="pdf")
        try:
            self.assertEqual(len(list(document[0].annots() or [])), 1)
        finally:
            document.close()

    def test_bake_clips_rects_that_extend_past_page_edge(self):
        fitz = _fitz()
        source = _sample_pdf_bytes()
        annotations = [
            {
                "id": "edge",
                "page": 1,
                "type": "highlight",
                "rect": {"x": 0.92, "y": 0.94, "w": 0.25, "h": 0.2},
            }
        ]

        normalized = pdf_markup.normalize_rect(annotations[0]["rect"])
        self.assertIsNotNone(normalized)
        self.assertLessEqual(normalized["x"] + normalized["w"], 1.0)
        self.assertLessEqual(normalized["y"] + normalized["h"], 1.0)
        baked = pdf_markup.bake_user_annotations(source, annotations)
        document = fitz.open(stream=baked, filetype="pdf")
        try:
            page = document[0]
            annotation = next(iter(page.annots() or []), None)
            self.assertIsNotNone(annotation)
        finally:
            document.close()

    def test_bake_empty_list_returns_valid_pdf(self):
        fitz = _fitz()
        source = _sample_pdf_bytes()
        baked = pdf_markup.bake_user_annotations(source, [])
        document = fitz.open(stream=baked, filetype="pdf")
        try:
            self.assertEqual(len(list(document[0].annots() or [])), 0)
        finally:
            document.close()


# --- validation ------------------------------------------------------------


class NormalizeAnnotationInputTests(unittest.TestCase):
    def _valid(self, **overrides):
        payload = {
            "page": 1,
            "type": "highlight",
            "rect": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.04},
        }
        payload.update(overrides)
        return payload

    def test_accepts_valid_payload(self):
        out = pdf_markup.normalize_annotation_input(self._valid(text="note", color="#ABC"))
        self.assertEqual(out["type"], "highlight")
        self.assertEqual(out["page"], 1)
        self.assertEqual(out["color"], "#abc")
        self.assertEqual(out["text"], "note")

    def test_rejects_bad_type(self):
        with self.assertRaises(pdf_markup.PdfMarkupError):
            pdf_markup.normalize_annotation_input(self._valid(type="scribble"))

    def test_rejects_non_positive_page(self):
        with self.assertRaises(pdf_markup.PdfMarkupError):
            pdf_markup.normalize_annotation_input(self._valid(page=0))
        with self.assertRaises(pdf_markup.PdfMarkupError):
            pdf_markup.normalize_annotation_input(self._valid(page="2"))

    def test_rejects_non_numeric_rect(self):
        with self.assertRaises(pdf_markup.PdfMarkupError):
            pdf_markup.normalize_annotation_input(self._valid(rect={"x": "a", "y": 0.1, "w": 0.1, "h": 0.1}))

    def test_clamps_rect_values(self):
        out = pdf_markup.normalize_annotation_input(
            self._valid(rect={"x": -1.0, "y": 2.0, "w": 0.5, "h": 0.5})
        )
        self.assertEqual(out["rect"]["x"], 0.0)
        self.assertEqual(out["rect"]["y"], 1.0)
        self.assertEqual(out["rect"]["h"], 0.0)

    def test_clips_rect_width_and_height_to_remaining_page(self):
        out = pdf_markup.normalize_annotation_input(
            self._valid(rect={"x": 0.9, "y": 0.95, "w": 0.5, "h": 0.25})
        )
        self.assertAlmostEqual(out["rect"]["w"], 0.1)
        self.assertAlmostEqual(out["rect"]["h"], 0.05)

    def test_text_is_bounded(self):
        out = pdf_markup.normalize_annotation_input(self._valid(text="x" * 5000))
        self.assertEqual(len(out["text"]), pdf_markup.MAX_ANNOTATION_TEXT_CHARS)

    def test_bad_color_dropped(self):
        out = pdf_markup.normalize_annotation_input(self._valid(color="red"))
        self.assertNotIn("color", out)


# --- routes ----------------------------------------------------------------


class _FakeHandler:
    def __init__(self, repository, *, payload=None, owner_user_id="owner-1"):
        self.matter_repository = repository
        self.current_user_id = owner_user_id
        self.current_user = {"id": owner_user_id, "email": owner_user_id}
        self._payload = payload
        self.status = 200
        self.json = None
        self.download = None
        self.download_headers = {}

    def _read_json_payload(self):
        return self._payload

    def _send_json(self, payload, status=200, headers=None, *, send_body=True):
        self.status = status
        self.json = payload

    def _send_download(self, data, filename, content_type, headers=None, *, send_body=True):
        self.status = 200
        self.download = {"data": data, "filename": filename, "content_type": content_type}
        self.download_headers = headers or {}


def _seed_pdf_matter(repo, *, owner_user_id="owner-1"):
    fitz = _fitz()
    if fitz is None:
        raise unittest.SkipTest("PyMuPDF is not installed")
    return repo.create_matter(
        source_filename="Mutual NDA.pdf",
        document_bytes=_sample_pdf_bytes(),
        extracted_text="This Agreement is mutual.",
        review_result={"clauses": []},
        triage={"triage_status": "review", "headline": "Mutual NDA"},
        source_type="manual_upload",
        board_column="in_review",
        owner_user_id=owner_user_id,
    )


def _seed_docx_matter(repo, *, owner_user_id="owner-1"):
    return repo.create_matter(
        source_filename="Mutual NDA.docx",
        document_bytes=b"PK\x03\x04 docx",
        extracted_text="This Agreement is mutual.",
        review_result={"clauses": []},
        triage={"triage_status": "review", "headline": "Mutual NDA"},
        source_type="manual_upload",
        board_column="in_review",
        owner_user_id=owner_user_id,
    )


class PdfMarkupRouteTests(unittest.TestCase):
    def setUp(self):
        if _fitz() is None:
            self.skipTest("PyMuPDF is not installed")
        self.repo = InMemoryMatterRepository()
        self.matter = _seed_pdf_matter(self.repo)
        self.matter_id = self.matter["id"]

    def _create(self, **payload_overrides):
        payload = {"page": 1, "type": "comment", "rect": {"x": 0.1, "y": 0.1, "w": 0.0, "h": 0.0}, "text": "hi"}
        payload.update(payload_overrides)
        handler = _FakeHandler(self.repo, payload=payload)
        pdf_markup_routes.handle_pdf_annotation_create(
            handler, f"/api/matters/{self.matter_id}/pdf-annotations"
        )
        return handler

    def test_create_persists_and_returns_server_fields(self):
        handler = self._create()
        self.assertEqual(handler.status, 201, handler.json)
        annotation = handler.json["annotation"]
        self.assertTrue(annotation["id"].startswith("annot_"))
        self.assertEqual(annotation["author"], "owner-1")
        self.assertTrue(annotation["created_at"])
        # Persisted on the matter.
        stored = self.repo.get_matter(self.matter_id, owner_user_id="owner-1")
        self.assertEqual(len(stored["pdf_annotations"]), 1)
        self.assertEqual(stored["pdf_annotations"][0]["id"], annotation["id"])

    def test_list_returns_stored_annotations(self):
        self._create(text="first")
        self._create(text="second")
        handler = _FakeHandler(self.repo)
        pdf_markup_routes.handle_pdf_annotations_list(
            handler, f"/api/matters/{self.matter_id}/pdf-annotations"
        )
        self.assertEqual(handler.status, 200)
        self.assertEqual(len(handler.json["annotations"]), 2)

    def test_create_rejects_invalid_payload(self):
        handler = self._create(type="scribble")
        self.assertEqual(handler.status, 400)

    def test_delete_removes_annotation(self):
        created = self._create()
        annotation_id = created.json["annotation"]["id"]
        handler = _FakeHandler(self.repo)
        pdf_markup_routes.handle_pdf_annotation_delete(
            handler, f"/api/matters/{self.matter_id}/pdf-annotations/{annotation_id}"
        )
        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.json, {"ok": True})
        stored = self.repo.get_matter(self.matter_id, owner_user_id="owner-1")
        self.assertEqual(stored["pdf_annotations"], [])

    def test_delete_missing_annotation_is_404(self):
        handler = _FakeHandler(self.repo)
        pdf_markup_routes.handle_pdf_annotation_delete(
            handler, f"/api/matters/{self.matter_id}/pdf-annotations/annot_missing"
        )
        self.assertEqual(handler.status, 404)

    def test_cap_enforced(self):
        # Pre-load the matter at the cap, then a create must be rejected.
        full = [
            {"id": f"annot_{i}", "page": 1, "type": "comment", "rect": {"x": 0.1, "y": 0.1, "w": 0.0, "h": 0.0}}
            for i in range(pdf_markup.MAX_ANNOTATIONS_PER_MATTER)
        ]
        self.repo.update_matter_fields(self.matter_id, {"pdf_annotations": full}, owner_user_id="owner-1")
        handler = self._create()
        self.assertEqual(handler.status, 409)

    def test_owner_scope_blocks_other_owner(self):
        self._create()
        other = _FakeHandler(self.repo, owner_user_id="intruder")
        pdf_markup_routes.handle_pdf_annotations_list(
            other, f"/api/matters/{self.matter_id}/pdf-annotations"
        )
        self.assertEqual(other.status, 404)

        other_delete = _FakeHandler(self.repo, owner_user_id="intruder")
        pdf_markup_routes.handle_pdf_annotation_delete(
            other_delete, f"/api/matters/{self.matter_id}/pdf-annotations/annot_x"
        )
        self.assertEqual(other_delete.status, 404)

    def test_marked_up_pdf_returns_pdf_for_pdf_matter(self):
        fitz = _fitz()
        self._create(type="highlight", rect={"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.04})
        self._create(type="comment", rect={"x": 0.2, "y": 0.3, "w": 0.0, "h": 0.0})
        handler = _FakeHandler(self.repo)
        pdf_markup_routes.handle_marked_up_pdf(
            handler, f"/api/matters/{self.matter_id}/marked-up-pdf"
        )
        self.assertEqual(handler.status, 200)
        self.assertIsNotNone(handler.download)
        self.assertEqual(handler.download["content_type"], "application/pdf")
        self.assertTrue(handler.download["filename"].endswith("-marked-up.pdf"))
        self.assertEqual(
            handler.download_headers["X-Export-Verified"],
            pdf_markup.MARKED_UP_PDF_VERIFICATION_HEADER,
        )
        self.assertEqual(handler.download_headers["X-PDF-Annotation-Count"], "2")
        document = fitz.open(stream=handler.download["data"], filetype="pdf")
        try:
            self.assertGreaterEqual(len(list(document[0].annots() or [])), 2)
        finally:
            document.close()

    def test_marked_up_pdf_400_for_docx_matter(self):
        docx_matter = _seed_docx_matter(self.repo)
        handler = _FakeHandler(self.repo)
        pdf_markup_routes.handle_marked_up_pdf(
            handler, f"/api/matters/{docx_matter['id']}/marked-up-pdf"
        )
        self.assertEqual(handler.status, 400)

    def test_telemetry_counters_increment(self):
        before = telemetry.snapshot()["counters"]
        created = self._create()
        annotation_id = created.json["annotation"]["id"]
        delete_handler = _FakeHandler(self.repo)
        pdf_markup_routes.handle_pdf_annotation_delete(
            delete_handler, f"/api/matters/{self.matter_id}/pdf-annotations/{annotation_id}"
        )
        export_handler = _FakeHandler(self.repo)
        pdf_markup_routes.handle_marked_up_pdf(
            export_handler, f"/api/matters/{self.matter_id}/marked-up-pdf"
        )
        after = telemetry.snapshot()["counters"]
        self.assertEqual(
            after.get("pdf_annotation_added", 0), before.get("pdf_annotation_added", 0) + 1
        )
        self.assertEqual(
            after.get("pdf_annotation_deleted", 0), before.get("pdf_annotation_deleted", 0) + 1
        )
        self.assertEqual(
            after.get("marked_up_pdf_export_requests", 0),
            before.get("marked_up_pdf_export_requests", 0) + 1,
        )


class AnnotatedPdfRecoveryRouteTests(unittest.TestCase):
    """The recovery route wired for ``PdfSourceRedlineUnavailableError``.

    The error payload (redline_export_service) points users at
    ``/api/matters/{matter_id}/annotated-pdf`` after the DOCX redline path fails
    closed. These tests pin that the route resolves (no 404 dead-link), is
    owner-gated, returns the review-redline-on-PDF export, and -- crucially --
    still returns a usable PDF when the highlights cannot be produced.
    """

    def setUp(self):
        if _fitz() is None:
            self.skipTest("PyMuPDF is not installed")
        self.repo = InMemoryMatterRepository()

    def _seed_reviewed_pdf(self, *, owner_user_id="owner-1"):
        fitz = _fitz()
        document = fitz.open()
        page = document.new_page()
        page.insert_text(
            (72, 72),
            "This Agreement shall be governed by the laws of Abu Dhabi.",
        )
        pdf_bytes = document.write()
        document.close()
        return self.repo.create_matter(
            source_filename="Recovery NDA.pdf",
            document_bytes=pdf_bytes,
            extracted_text="This Agreement shall be governed by the laws of Abu Dhabi.",
            review_result={
                "clauses": [
                    {
                        "id": "governing_law",
                        "name": "Governing Law",
                        "decision": "fail",
                        "reason": "Abu Dhabi is outside the approved governing-law list.",
                        "structured_evidence": [
                            {
                                "paragraph_id": "p1",
                                "text": "This Agreement shall be governed by the laws of Abu Dhabi.",
                                "matched_text": "laws of Abu Dhabi",
                            }
                        ],
                    }
                ],
            },
            triage={"triage_status": "review", "headline": "Recovery NDA"},
            source_type="manual_upload",
            board_column="in_review",
            owner_user_id=owner_user_id,
        )

    def test_recovery_payload_endpoint_string_matches_route(self):
        # The exact string the failure payload advertises must resolve to this
        # handler -- prove there is no template/route mismatch (the 404 bug).
        from nda_automation.redline_export_service import PdfSourceRedlineUnavailableError

        error = PdfSourceRedlineUnavailableError.for_unplaceable_anchors(
            3, source_filename="Recovery NDA.pdf"
        )
        endpoint_template = error.payload["recovery"]["endpoint"]
        self.assertEqual(endpoint_template, "/api/matters/{matter_id}/annotated-pdf")

        matter = self._seed_reviewed_pdf()
        resolved = endpoint_template.format(matter_id=matter["id"])
        self.assertEqual(parse_matter_id(resolved, suffix="/annotated-pdf"), matter["id"])

    def test_returns_annotated_pdf_for_reviewed_pdf_matter(self):
        fitz = _fitz()
        # The review here is intentionally not playbook-fresh; force the
        # staleness gate open so we exercise the real annotation path.
        original = annotated_pdf_export.review_result_staleness
        annotated_pdf_export.review_result_staleness = lambda *_args, **_kwargs: {
            "stale": False,
            "stale_reasons": [],
        }
        try:
            matter = self._seed_reviewed_pdf()
            handler = _FakeHandler(self.repo)
            pdf_markup_routes.handle_annotated_pdf(
                handler, f"/api/matters/{matter['id']}/annotated-pdf"
            )
        finally:
            annotated_pdf_export.review_result_staleness = original

        self.assertEqual(handler.status, 200, handler.json)
        self.assertIsNotNone(handler.download)
        self.assertEqual(handler.download["content_type"], "application/pdf")
        self.assertTrue(handler.download["filename"].endswith("-annotated-review.pdf"))
        self.assertTrue(handler.download["data"].startswith(b"%PDF"))
        self.assertEqual(handler.download_headers["X-PDF-Annotation-Fallback"], "none")
        self.assertGreaterEqual(int(handler.download_headers["X-PDF-Annotation-Count"]), 1)
        annotated = fitz.open(stream=handler.download["data"], filetype="pdf")
        try:
            self.assertGreaterEqual(len(list(annotated[0].annots() or [])), 1)
        finally:
            annotated.close()

    def test_failed_annotation_fails_loud_and_never_serves_source_as_success(self):
        # A naively-seeded review is stale (no playbook runtime), so the builder
        # raises. The route must FAIL LOUD (terminal 422) and MUST NOT hand back
        # the unmodified source PDF under a 200 "annotated" download -- that would
        # be silent-wrong-bytes (the caller downloads what reads as the redlined
        # NDA but carries no redlines).
        matter = self._seed_pdf_matter_with_source()
        handler = _FakeHandler(self.repo)
        pdf_markup_routes.handle_annotated_pdf(
            handler, f"/api/matters/{matter['id']}/annotated-pdf"
        )
        # No success-typed download of any bytes.
        self.assertIsNone(handler.download)
        self.assertEqual(handler.status, 422, handler.json)
        # Machine-checkable terminal error the caller must handle.
        self.assertEqual(handler.json["error_code"], "annotated_pdf_unavailable")
        self.assertEqual(
            handler.json["annotated_pdf_fallback"], "source-original-withheld"
        )
        # The source PDF was deliberately withheld -- the error body carries no
        # PDF bytes that could be mistaken for the redlined output.
        source_bytes = self.repo.get_source_document_bytes(matter)
        self.assertNotIn(source_bytes, handler.json.values())

    def _seed_pdf_matter_with_source(self, *, owner_user_id="owner-1"):
        return _seed_pdf_matter(self.repo, owner_user_id=owner_user_id)

    def test_owner_scope_blocks_other_owner(self):
        matter = self._seed_reviewed_pdf()
        other = _FakeHandler(self.repo, owner_user_id="intruder")
        pdf_markup_routes.handle_annotated_pdf(
            other, f"/api/matters/{matter['id']}/annotated-pdf"
        )
        self.assertEqual(other.status, 404)
        self.assertIsNone(other.download)

    def test_400_for_docx_matter(self):
        docx_matter = _seed_docx_matter(self.repo)
        handler = _FakeHandler(self.repo)
        pdf_markup_routes.handle_annotated_pdf(
            handler, f"/api/matters/{docx_matter['id']}/annotated-pdf"
        )
        self.assertEqual(handler.status, 400)
        self.assertIsNone(handler.download)


if __name__ == "__main__":
    unittest.main()
