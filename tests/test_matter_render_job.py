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
