import os
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

from nda_automation import document_rendering, matter_render_job


def test_parse_matter_render_page_path_accepts_encoded_matter_ids():
    assert matter_render_job.parse_matter_render_page_path("/api/matters/matter%201/render-page/2") == (
        "matter 1",
        2,
    )


def test_parse_matter_render_page_path_rejects_non_page_paths():
    assert matter_render_job.parse_matter_render_page_path("/api/matters/matter-1/render-page/0") is None
    assert matter_render_job.parse_matter_render_page_path("/api/matters/matter-1/render-page/not-a-page") is None
    assert matter_render_job.parse_matter_render_page_path("/api/matters/matter-1/source") is None


def test_public_document_render_includes_page_manifest_and_overlay_contract(tmp_path):
    pdf_path = tmp_path / "matter.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\n%%EOF\n")
    page_path = tmp_path / "page-1.png"
    page_path.write_bytes(b"\x89PNG\r\n")
    rendered = document_rendering.RenderedDocument(
        status=document_rendering.READY_STATUS,
        cache_key="render-key",
        source_sha256="source-sha",
        source_kind="pdf",
        cache_dir=tmp_path,
        pdf_path=pdf_path,
        cached=True,
    )
    page_manifest = document_rendering.RenderedPdfPageImageManifest(
        status=document_rendering.READY_STATUS,
        cache_key="page-key",
        cache_dir=tmp_path,
        pdf_path=pdf_path,
        pages=(
            document_rendering.RenderedPdfPageImage(
                page_number=1,
                image_path=page_path,
                width=640,
                height=880,
                dpi=192,
                scale=2.0,
            ),
        ),
        cached=False,
        dpi=192,
        scale=2.0,
    )
    matter = {
        "review_result": {
            "paragraphs": [{"id": "p1", "page_number": 1}],
            "clauses": [{"id": "mutuality", "matched_paragraph_ids": ["p1"]}],
            "redline_edits": [{"id": "r1", "clause_id": "mutuality", "paragraph_id": "p1"}],
        }
    }

    payload = matter_render_job.public_document_render(
        "matter-1",
        rendered,
        matter=matter,
        page_manifest=page_manifest,
    )

    assert payload["pdf_url"] == "/api/matters/matter-1/render-pdf"
    assert payload["page_image_status"] == document_rendering.READY_STATUS
    assert payload["pages"] == payload["page_images"]["pages"]
    assert payload["pages"][0] == {
        "page_number": 1,
        "image_url": "/api/matters/matter-1/render-page/1",
        "width": 640,
        "height": 880,
        "dpi": 192,
        "scale": 2.0,
    }
    assert payload["document_overlay"]["version"] == 1
    assert payload["document_overlay"]["status"] == "partial"
    assert payload["document_overlay"]["precision"] == "page"
    assert payload["document_overlay"]["fallback_mode"] == "text_dom_scroll"
    assert payload["document_overlay"]["anchors"] == [
        {
            "target_type": "evidence",
            "clause_id": "mutuality",
            "paragraph_id": "p1",
            "page_number": 1,
            "boxes": [],
            "confidence": 0.6,
            "confidence_reason": "Page-level match only; no verified text coordinates.",
            "fallback": {"mode": "text_dom_scroll", "selector": '[data-paragraph-id="p1"]'},
        },
        {
            "target_type": "redline",
            "clause_id": "mutuality",
            "paragraph_id": "p1",
            "page_number": 1,
            "boxes": [],
            "confidence": 0.6,
            "confidence_reason": "Page-level match only; no verified text coordinates.",
            "fallback": {"mode": "text_dom_scroll", "selector": '[data-paragraph-id="p1"]'},
            "redline_id": "r1",
        },
    ]


# Deterministic PDF source: passthrough render writes these exact bytes to the
# cache (no soffice; see document_rendering PDF passthrough), so byte-serving GETs
# can be asserted without external render tooling.
_ASYNC_PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


class _FakePageRenderer:
    """Stub PyMuPDF so a full render can be pre-warmed without fitz installed."""

    name = "fake-async-render-pages"

    def is_available(self) -> bool:
        return True

    def render_pdf_to_page_images(self, pdf_path, output_dir, *, dpi):
        image_path = Path(output_dir) / "page-1.png"
        image_path.write_bytes(b"\x89PNG\r\nfake async page\n")
        return [
            document_rendering.RenderedPdfPageImage(
                page_number=1,
                image_path=image_path,
                width=612,
                height=792,
                dpi=dpi,
                scale=2.0,
            )
        ]


class _FakeMatterRepository:
    def __init__(self, matter, source_bytes):
        self._matter = matter
        self._source_bytes = source_bytes

    def get_matter(self, matter_id, owner_user_id=""):
        if matter_id == self._matter.get("id"):
            return dict(self._matter)
        return None

    def get_source_document_bytes(self, matter):
        return self._source_bytes


class AsyncRenderFlagTests(unittest.TestCase):
    """NDA_ASYNC_RENDER: OFF is byte-identical inline; ON serves warm hits and
    sheds cache-miss byte GETs to a background render instead of holding the thread."""

    def setUp(self):
        document_rendering.matter_render_coordinator().reset_for_tests()

    def tearDown(self):
        document_rendering.matter_render_coordinator().reset_for_tests()

    def _repo(self, matter_id="matter-async", owner="alice"):
        matter = {"id": matter_id, "source_filename": "Source NDA.pdf", "owner_user_id": owner}
        return _FakeMatterRepository(matter, _ASYNC_PDF_BYTES), matter

    # --- flag reader -----------------------------------------------------

    def test_flag_reader_defaults_off_and_honors_truthy_values(self):
        with unittest.mock.patch.dict(os.environ, {matter_render_job.ASYNC_RENDER_ENV: ""}):
            self.assertFalse(matter_render_job.async_render_enabled())
        for truthy in ("1", "true", "TRUE", "yes", "on"):
            with unittest.mock.patch.dict(os.environ, {matter_render_job.ASYNC_RENDER_ENV: truthy}):
                self.assertTrue(matter_render_job.async_render_enabled())

    # --- flag OFF: byte-identical inline behavior ------------------------

    def test_flag_off_renders_pdf_inline_on_cold_cache(self):
        # The core invariant: OFF renders INLINE on a cold cache and serves the
        # bytes -- it must NOT shed with 503. Identical to the historical path
        # (render_pdf_file -> render_matter_document).
        repo, matter = self._repo(matter_id="matter-off-pdf")
        with tempfile.TemporaryDirectory() as cache_name:
            cache_dir = Path(cache_name)
            with unittest.mock.patch.dict(os.environ, {matter_render_job.ASYNC_RENDER_ENV: ""}):
                result = matter_render_job.render_pdf_file(
                    matter["id"], owner_user_id="alice", repository=repo, cache_dir=cache_dir
                )
            self.assertEqual(result.content_type, document_rendering.PDF_CONTENT_TYPE)
            self.assertEqual(result.path.read_bytes(), _ASYNC_PDF_BYTES)
            # No background job was scheduled -- the render happened on this thread.
            self.assertIsNone(document_rendering.matter_render_coordinator().in_flight(matter["id"]))

    def test_flag_off_matches_render_matter_document_pdf_path(self):
        # OFF path is literally render_matter_document -> same cached artifact.
        repo, matter = self._repo(matter_id="matter-off-identical")
        with tempfile.TemporaryDirectory() as cache_name:
            cache_dir = Path(cache_name)
            direct = matter_render_job.render_matter_document(
                matter["id"], owner_user_id="alice", include_page_images=False,
                repository=repo, cache_dir=cache_dir,
            )
            with unittest.mock.patch.dict(os.environ, {matter_render_job.ASYNC_RENDER_ENV: ""}):
                served = matter_render_job.render_pdf_file(
                    matter["id"], owner_user_id="alice", repository=repo, cache_dir=cache_dir
                )
            self.assertEqual(served.path, direct.rendered.pdf_path)
            self.assertEqual(served.path.read_bytes(), _ASYNC_PDF_BYTES)

    def test_flag_off_serves_page_image_from_cache(self):
        repo, matter = self._repo(matter_id="matter-off-page")
        with tempfile.TemporaryDirectory() as cache_name:
            cache_dir = Path(cache_name)
            document_rendering.render_source_document_result(
                _ASYNC_PDF_BYTES,
                source_filename="Source NDA.pdf",
                cache_dir=cache_dir,
                owner_user_id="alice",
                page_renderer=_FakePageRenderer(),
                include_page_images=True,
            )
            with unittest.mock.patch.dict(os.environ, {matter_render_job.ASYNC_RENDER_ENV: ""}):
                result = matter_render_job.render_page_image_file(
                    matter["id"], 1, owner_user_id="alice", repository=repo, cache_dir=cache_dir
                )
            self.assertEqual(result.content_type, document_rendering.PAGE_IMAGE_CONTENT_TYPE)
            self.assertTrue(result.path.read_bytes().startswith(b"\x89PNG"))

    # --- flag ON: warm cache is served immediately -----------------------

    def test_flag_on_warm_cache_serves_pdf_without_scheduling(self):
        repo, matter = self._repo(matter_id="matter-on-warm-pdf")
        with tempfile.TemporaryDirectory() as cache_name:
            cache_dir = Path(cache_name)
            document_rendering.render_source_document_to_pdf(
                _ASYNC_PDF_BYTES, source_filename="Source NDA.pdf",
                cache_dir=cache_dir, owner_user_id="alice",
            )
            with unittest.mock.patch.dict(os.environ, {matter_render_job.ASYNC_RENDER_ENV: "1"}):
                result = matter_render_job.render_pdf_file(
                    matter["id"], owner_user_id="alice", repository=repo, cache_dir=cache_dir
                )
            self.assertEqual(result.content_type, document_rendering.PDF_CONTENT_TYPE)
            self.assertEqual(result.path.read_bytes(), _ASYNC_PDF_BYTES)
            # A warm hit never touches the background coordinator.
            self.assertIsNone(document_rendering.matter_render_coordinator().in_flight(matter["id"]))

    def test_flag_on_warm_cache_serves_page_image(self):
        repo, matter = self._repo(matter_id="matter-on-warm-page")
        with tempfile.TemporaryDirectory() as cache_name:
            cache_dir = Path(cache_name)
            document_rendering.render_source_document_result(
                _ASYNC_PDF_BYTES,
                source_filename="Source NDA.pdf",
                cache_dir=cache_dir,
                owner_user_id="alice",
                page_renderer=_FakePageRenderer(),
                include_page_images=True,
            )
            with unittest.mock.patch.dict(os.environ, {matter_render_job.ASYNC_RENDER_ENV: "1"}):
                result = matter_render_job.render_page_image_file(
                    matter["id"], 1, owner_user_id="alice", repository=repo, cache_dir=cache_dir
                )
            self.assertEqual(result.content_type, document_rendering.PAGE_IMAGE_CONTENT_TYPE)
            self.assertTrue(result.path.read_bytes().startswith(b"\x89PNG"))

    # --- flag ON: cold cache sheds 503 + schedules, then serves ----------

    def test_flag_on_cold_cache_sheds_503_then_background_completes_and_serves(self):
        repo, matter = self._repo(matter_id="matter-on-cold")
        with tempfile.TemporaryDirectory() as cache_name:
            cache_dir = Path(cache_name)
            with unittest.mock.patch.dict(os.environ, {matter_render_job.ASYNC_RENDER_ENV: "1"}):
                # Cold cache: the byte GET must NOT render inline -- it sheds 503 +
                # Retry-After with a "rendering" status and schedules a background job.
                with self.assertRaises(matter_render_job.MatterRenderJobError) as ctx:
                    matter_render_job.render_pdf_file(
                        matter["id"], owner_user_id="alice", repository=repo, cache_dir=cache_dir
                    )
                error = ctx.exception
                self.assertEqual(error.status, 503)
                self.assertEqual(error.headers.get("Retry-After"), "5")
                self.assertEqual(
                    error.payload["document_render"]["status"], document_rendering.RENDERING_STATUS
                )

                # The scheduled background render completes; a later GET serves the
                # warm cache hit rather than shedding forever.
                served = None
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    try:
                        served = matter_render_job.render_pdf_file(
                            matter["id"], owner_user_id="alice", repository=repo, cache_dir=cache_dir
                        )
                        break
                    except matter_render_job.MatterRenderJobError as retry_error:
                        self.assertEqual(retry_error.status, 503)
                        time.sleep(0.05)
            self.assertIsNotNone(served, "background render never produced a servable cache hit")
            self.assertEqual(served.path.read_bytes(), _ASYNC_PDF_BYTES)
            # Do not let a coordinator thread outlive the temp cache dir.
            job = document_rendering.matter_render_coordinator().in_flight(matter["id"])
            if job is not None and job.thread is not None:
                job.thread.join(timeout=10)

    # --- flag ON: the byte GET does not block on an in-flight render -----

    def test_flag_on_byte_get_does_not_block_on_in_flight_render(self):
        # Proves the request thread is FREED: with a slow render already in flight
        # for this matter, the byte GET sheds (503) immediately instead of blocking
        # for the full render duration.
        repo, matter = self._repo(matter_id="matter-on-slow")
        coordinator = document_rendering.matter_render_coordinator()
        release = threading.Event()

        def slow_render():
            release.wait(timeout=5)
            return None

        coordinator.ensure_in_flight(matter["id"], slow_render)
        try:
            with tempfile.TemporaryDirectory() as cache_name:
                cache_dir = Path(cache_name)
                with unittest.mock.patch.dict(os.environ, {matter_render_job.ASYNC_RENDER_ENV: "1"}):
                    start = time.monotonic()
                    with self.assertRaises(matter_render_job.MatterRenderJobError) as ctx:
                        matter_render_job.render_pdf_file(
                            matter["id"], owner_user_id="alice", repository=repo, cache_dir=cache_dir
                        )
                    elapsed = time.monotonic() - start
                self.assertEqual(ctx.exception.status, 503)
                # Returned in a small fraction of the 5s slow render -> not blocked.
                self.assertLess(elapsed, 1.0)
        finally:
            release.set()
            job = coordinator.in_flight(matter["id"])
            if job is not None and job.thread is not None:
                job.thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
