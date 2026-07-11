from __future__ import annotations

from nda_automation import matter_view
from nda_automation.source_fidelity import source_fidelity_payload


def test_source_fidelity_groups_table_cells_and_preserves_color_runs():
    review_result = {
        "source": {"type": "docx"},
        "paragraphs": [
            {
                "id": "p1",
                "index": 1,
                "text": "Intro red text.",
                "source_kind": "paragraph",
                "runs": [
                    {"text": "Intro ", "bold": False, "italic": False, "underline": False},
                    {"text": "red", "bold": False, "italic": False, "underline": False, "color": "#ff0000"},
                    {"text": " text.", "bold": False, "italic": False, "underline": False},
                ],
            },
            {
                "id": "p2",
                "index": 2,
                "text": "Party",
                "source_kind": "table_cell",
                "table": {"table_index": 1, "row_index": 1, "cell_index": 1},
            },
            {
                "id": "p3",
                "index": 3,
                "text": "Signature",
                "source_kind": "table_cell",
                "table": {
                    "table_index": 1,
                    "row_index": 1,
                    "cell_index": 2,
                    "cell_style": {
                        "background_color": "#d9ead3",
                        "width": {"value": 2400, "type": "dxa"},
                    },
                },
                "style_name": "Table Text",
            },
            {
                "id": "p4",
                "index": 4,
                "text": "Aspora",
                "source_kind": "table_cell",
                "table": {"table_index": 1, "row_index": 2, "cell_index": 1},
            },
        ],
    }

    payload = source_fidelity_payload(review_result, source=review_result["source"])

    assert payload["version"] == 1
    assert payload["analysis_model"] == "paragraphs"
    assert payload["render_model"] == "source_blocks"
    assert payload["capabilities"]["structured_tables"] is True
    assert payload["capabilities"]["table_cell_styles"] is True
    assert payload["capabilities"]["table_cell_backgrounds"] is True
    assert payload["capabilities"]["run_colors"] is True
    assert payload["summary"] == {
        "paragraph_count": 4,
        "block_count": 2,
        "table_count": 1,
        "styled_table_cell_count": 1,
        "table_cell_background_count": 1,
        "styled_run_count": 1,
        "color_run_count": 1,
        "pdf_page_reference_count": 0,
    }
    paragraph = payload["blocks"][0]
    assert paragraph["type"] == "paragraph"
    assert paragraph["runs"][1] == {"text": "red", "color": "#ff0000", "bold": False, "italic": False, "underline": False}

    table = payload["blocks"][1]
    assert table["type"] == "table"
    assert table["table_index"] == 1
    assert table["rows"][0]["cells"][0]["paragraph_ids"] == ["p2"]
    assert table["rows"][0]["cells"][1]["paragraph_ids"] == ["p3"]
    assert table["rows"][0]["cells"][1]["style"] == {
        "background_color": "#d9ead3",
        "width": {"value": 2400, "type": "dxa"},
    }
    assert table["rows"][0]["cells"][1]["blocks"][0]["style"]["style_name"] == "Table Text"
    assert table["rows"][1]["cells"][0]["paragraph_ids"] == ["p4"]


def test_pdf_geometry_maps_into_block_style_font_size_and_relative_indent():
    # D3 STRUCTURAL CARRY: a PDF paragraph's pdf_geometry (font_size/left_x) must
    # surface in block.style under the renderer's key names (fontSize / indent_left),
    # with left_x converted to a page-relative indent (0 at the text margin, matching
    # the DOCX field). Body text at the margin gets NO indent; the nested sub-clause
    # keeps a positive one; the heading keeps its larger font size.
    review_result = {
        "source": {"type": "pdf"},
        "paragraphs": [
            {
                "id": "p1", "index": 1, "text": "1. CONFIDENTIALITY", "source_part": "pdf",
                "page_number": 1,
                "pdf_geometry": {"font_size": 14.0, "left_x": 72.0, "body_font": 11.0,
                                 "heading_font_ratio": 14.0 / 11.0},
            },
            {
                "id": "p2", "index": 2, "text": "The Receiving Party shall hold it in confidence.",
                "source_part": "pdf", "page_number": 1,
                "pdf_geometry": {"font_size": 11.0, "left_x": 72.0, "body_font": 11.0},
            },
            {
                "id": "p3", "index": 3, "text": "(a) a nested sub-clause set further in.",
                "source_part": "pdf", "page_number": 1,
                "pdf_geometry": {"font_size": 11.0, "left_x": 108.0, "body_font": 11.0},
            },
        ],
    }

    payload = source_fidelity_payload(review_result, source=review_result["source"])
    blocks = {block["paragraph_id"]: block for block in payload["blocks"]}

    # Heading: larger font size carried; sits at the margin so no indent.
    assert blocks["p1"]["style"]["fontSize"] == 14
    assert "indent_left" not in blocks["p1"].get("style", {})
    # Body text at the margin: font size carried, indent normalized to 0 (dropped).
    assert blocks["p2"]["style"]["fontSize"] == 11
    assert "indent_left" not in blocks["p2"].get("style", {})
    # Nested sub-clause: 108 - 72 = 36pt relative indent surfaced.
    assert blocks["p3"]["style"]["indent_left"] == 36


def test_pdf_paragraph_without_geometry_carries_no_derived_typography():
    # A flat-fallback PDF paragraph (no pdf_geometry) surfaces no DERIVED typography
    # (fontSize / indent_left) — we only expose font size and indent we can prove.
    review_result = {
        "source": {"type": "pdf"},
        "paragraphs": [
            {"id": "p1", "index": 1, "text": "flat text.", "source_part": "pdf", "page_number": 1},
        ],
    }
    payload = source_fidelity_payload(review_result, source=review_result["source"])
    style = payload["blocks"][0].get("style", {})
    assert "fontSize" not in style
    assert "indent_left" not in style


def test_source_fidelity_marks_pdf_as_source_preview_limited():
    review_result = {
        "source": {
            "type": "pdf",
            "extraction_quality": {
                "visual_profile": {
                    "status": "ready",
                    "requires_source_preview": True,
                    "visual_features": ["colored_text", "drawings_or_borders"],
                    "non_black_text_span_count": 3,
                    "drawing_count": 2,
                }
            },
        },
        "paragraphs": [
            {"id": "p1", "index": 1, "text": "PDF page one.", "source_part": "pdf", "page_number": 1},
            {"id": "p2", "index": 2, "text": "PDF page two.", "source_part": "pdf", "page_number": 2},
        ],
    }

    payload = source_fidelity_payload(review_result, source=review_result["source"])

    assert payload["source_type"] == "pdf"
    assert payload["preferred_render_mode"] == "source_pdf_preview"
    assert payload["capabilities"]["pdf_page_references"] is True
    assert payload["capabilities"]["faithful_source_preview"] is True
    assert payload["capabilities"]["pdf_visual_profile"] is True
    assert payload["capabilities"]["pdf_visual_elements"] is True
    assert payload["summary"]["pdf_page_reference_count"] == 2
    assert payload["pdf_fidelity"]["analysis_mode"] == "extracted_text_only"
    assert payload["pdf_fidelity"]["layout_mode"] == "original_pdf_page_preview"
    assert payload["pdf_fidelity"]["word_conversion"] == "unsupported_for_fidelity"
    assert payload["pdf_fidelity"]["redlined_docx"] == "reconstructed_not_fidelity_preserving"
    assert "best-effort reconstructed Word" in payload["pdf_fidelity"]["message"]
    assert payload["pdf_fidelity"]["visual_profile"]["visual_features"] == [
        "colored_text",
        "drawings_or_borders",
    ]
    assert {limitation["code"] for limitation in payload["limitations"]} == {
        "pdf_text_extraction_not_layout",
        "pdf_visual_elements_detected",
        "semantic_review_is_paragraph_based",
        "pdf_visual_fidelity_requires_source_preview",
        "pdf_word_conversion_unsupported_for_fidelity",
    }


def test_source_fidelity_marks_missing_pdf_visual_profile_as_preview_required():
    review_result = {
        "source": {
            "type": "pdf",
            "extraction_quality": {
                "visual_profile": {
                    "status": "unavailable",
                    "reason": "pymupdf_not_installed",
                    "requires_source_preview": True,
                }
            },
        },
        "paragraphs": [
            {"id": "p1", "index": 1, "text": "PDF text.", "source_part": "pdf", "page_number": 1},
        ],
    }

    payload = source_fidelity_payload(review_result, source=review_result["source"])

    assert payload["source_type"] == "pdf"
    assert payload["preferred_render_mode"] == "source_pdf_preview"
    assert payload["capabilities"]["faithful_source_preview"] is True
    assert payload["capabilities"]["pdf_visual_profile"] is True
    assert payload["capabilities"]["pdf_visual_elements"] is True
    assert payload["pdf_fidelity"]["requires_source_preview"] is True
    assert payload["pdf_fidelity"]["visual_profile"]["reason"] == "pymupdf_not_installed"
    assert {limitation["code"] for limitation in payload["limitations"]} == {
        "pdf_text_extraction_not_layout",
        "pdf_visual_elements_detected",
        "pdf_visual_fidelity_requires_source_preview",
        "pdf_visual_profile_unavailable",
        "pdf_word_conversion_unsupported_for_fidelity",
        "semantic_review_is_paragraph_based",
    }


def test_review_matter_exposes_additive_source_fidelity_contract():
    matter = {
        "id": "matter-1",
        "source_filename": "NDA.docx",
        "extracted_text": "Party\n\nSignature",
        "review_result": {
            "source": {"type": "docx", "filename": "NDA.docx"},
            "clauses": [],
            "redline_edits": [],
            "paragraphs": [
                {
                    "id": "p1",
                    "index": 1,
                    "text": "Party",
                    "start": 0,
                    "end": 5,
                    "source_kind": "table_cell",
                    "table": {"table_index": 1, "row_index": 1, "cell_index": 1},
                },
                {
                    "id": "p2",
                    "index": 2,
                    "text": "Signature",
                    "start": 7,
                    "end": 16,
                    "source_kind": "table_cell",
                    "table": {"table_index": 1, "row_index": 1, "cell_index": 2},
                },
            ],
        },
    }

    payload = matter_view.review_matter(matter)

    assert payload["review_result"]["paragraphs"][0]["table"]["cell_index"] == 1
    assert payload["source_fidelity"]["capabilities"]["structured_tables"] is True
    assert payload["source_fidelity"]["blocks"][0]["type"] == "table"


def test_source_fidelity_exposes_paragraph_alignment_and_font_in_style():
    # The extractor-captured paragraph alignment ("both"->justify) and base font
    # are additive STYLE facts. Once they survive the review_document allowlist
    # they must reach the source-fidelity block's ``style`` so the reconstruction
    # renderer can emit inline text-align / font-family. A paragraph that carries
    # neither exposes no such keys, so the renderer falls back to the source
    # default (left) and the app font.
    review_result = {
        "source": {"type": "docx"},
        "paragraphs": [
            {
                "id": "p1",
                "index": 1,
                "text": "Confidentiality Agreement",
                "source_kind": "paragraph",
                "alignment": "center",
                "font": "Times New Roman",
            },
            {
                "id": "p2",
                "index": 2,
                "text": "Justified body clause text.",
                "source_kind": "paragraph",
                "alignment": "justify",
            },
            {
                "id": "p3",
                "index": 3,
                "text": "Unstyled paragraph.",
                "source_kind": "paragraph",
            },
        ],
    }

    payload = source_fidelity_payload(review_result, source=review_result["source"])
    blocks = payload["blocks"]

    assert blocks[0]["style"]["alignment"] == "center"
    assert blocks[0]["style"]["font"] == "Times New Roman"
    assert blocks[1]["style"]["alignment"] == "justify"
    assert "font" not in blocks[1].get("style", {})
    # A paragraph with no alignment/font exposes no such style keys.
    assert "alignment" not in blocks[2].get("style", {})
    assert "font" not in blocks[2].get("style", {})
    # Style facts never mutate the paragraph text the redline targets.
    assert [block["text"] for block in blocks] == [
        "Confidentiality Agreement",
        "Justified body clause text.",
        "Unstyled paragraph.",
    ]


def _cell_texts(cell):
    return [block["text"] for block in cell["blocks"] if block.get("type") == "paragraph"]


def _nested_tables(cell):
    return [block for block in cell["blocks"] if block.get("type") == "table"]


def test_source_fidelity_preserves_horizontal_grid_span_as_colspan():
    # A header cell spanning both columns (col_span from w:gridSpan) must reach the
    # rendered cell so the renderer can emit a colspan; the ordinary cells do not.
    review_result = {
        "source": {"type": "docx"},
        "paragraphs": [
            {"id": "h", "index": 1, "text": "Header", "source_kind": "table_cell",
             "table": {"table_index": 1, "row_index": 1, "cell_index": 1, "col_span": 2}},
            {"id": "l", "index": 2, "text": "Left", "source_kind": "table_cell",
             "table": {"table_index": 1, "row_index": 2, "cell_index": 1}},
            {"id": "r", "index": 3, "text": "Right", "source_kind": "table_cell",
             "table": {"table_index": 1, "row_index": 2, "cell_index": 2}},
        ],
    }

    payload = source_fidelity_payload(review_result, source=review_result["source"])
    table = payload["blocks"][0]

    header_cell = table["rows"][0]["cells"][0]
    assert header_cell["col_span"] == 2
    assert _cell_texts(header_cell) == ["Header"]
    assert len(table["rows"][1]["cells"]) == 2
    assert "col_span" not in table["rows"][1]["cells"][0]


def test_source_fidelity_preserves_vertical_merge_and_folds_continuation():
    # The restart cell carries row_span; the continuation cell (v_merge=continue)
    # is folded away so column 2 of row 2 stays in column 2, not shifted left.
    review_result = {
        "source": {"type": "docx"},
        "paragraphs": [
            {"id": "m", "index": 1, "text": "Merged down", "source_kind": "table_cell",
             "table": {"table_index": 1, "row_index": 1, "cell_index": 1, "row_span": 2}},
            {"id": "a", "index": 2, "text": "Row1 col2", "source_kind": "table_cell",
             "table": {"table_index": 1, "row_index": 1, "cell_index": 2}},
            {"id": "c", "index": 3, "text": "hidden", "source_kind": "table_cell",
             "table": {"table_index": 1, "row_index": 2, "cell_index": 1, "v_merge": "continue"}},
            {"id": "b", "index": 4, "text": "Row2 col2", "source_kind": "table_cell",
             "table": {"table_index": 1, "row_index": 2, "cell_index": 2}},
        ],
    }

    payload = source_fidelity_payload(review_result, source=review_result["source"])
    table = payload["blocks"][0]

    restart_cell = table["rows"][0]["cells"][0]
    assert restart_cell["row_span"] == 2
    assert _cell_texts(restart_cell) == ["Merged down"]
    # Row 2 has ONLY the real column-2 cell; the folded continuation is gone.
    assert [cell["cell_index"] for cell in table["rows"][1]["cells"]] == [2]
    assert _cell_texts(table["rows"][1]["cells"][0]) == ["Row2 col2"]


def test_source_fidelity_nests_child_table_inside_parent_cell():
    # A nested table (its cells carry a parent back-pointer) is emitted INSIDE the
    # parent cell's blocks in document order, not as a flat sibling table.
    review_result = {
        "source": {"type": "docx"},
        "paragraphs": [
            {"id": "o", "index": 1, "text": "Outer cell before", "source_kind": "table_cell",
             "table": {"table_index": 1, "row_index": 1, "cell_index": 1}},
            {"id": "ia", "index": 2, "text": "Inner A", "source_kind": "table_cell",
             "table": {"table_index": 2, "row_index": 1, "cell_index": 1,
                       "parent": {"table_index": 1, "row_index": 1, "cell_index": 1}}},
            {"id": "ib", "index": 3, "text": "Inner B", "source_kind": "table_cell",
             "table": {"table_index": 2, "row_index": 1, "cell_index": 2,
                       "parent": {"table_index": 1, "row_index": 1, "cell_index": 1}}},
        ],
    }

    payload = source_fidelity_payload(review_result, source=review_result["source"])
    blocks = payload["blocks"]

    # Exactly one TOP-LEVEL table; the inner table is not a sibling.
    assert [block["type"] for block in blocks] == ["table"]
    outer_cell = blocks[0]["rows"][0]["cells"][0]
    assert _cell_texts(outer_cell) == ["Outer cell before"]
    nested = _nested_tables(outer_cell)
    assert len(nested) == 1
    assert nested[0]["table_index"] == 2
    inner_texts = [
        _cell_texts(cell)[0]
        for cell in nested[0]["rows"][0]["cells"]
    ]
    assert inner_texts == ["Inner A", "Inner B"]
    # Summary counts the nested table too.
    assert payload["summary"]["table_count"] == 2


def test_source_fidelity_plain_table_carries_no_span_keys():
    # A plain 2-cell table is unchanged: cells expose no col_span/row_span keys.
    review_result = {
        "source": {"type": "docx"},
        "paragraphs": [
            {"id": "x", "index": 1, "text": "One", "source_kind": "table_cell",
             "table": {"table_index": 1, "row_index": 1, "cell_index": 1}},
            {"id": "y", "index": 2, "text": "Two", "source_kind": "table_cell",
             "table": {"table_index": 1, "row_index": 1, "cell_index": 2}},
        ],
    }

    payload = source_fidelity_payload(review_result, source=review_result["source"])
    cells = payload["blocks"][0]["rows"][0]["cells"]

    assert [cell["cell_index"] for cell in cells] == [1, 2]
    for cell in cells:
        assert "col_span" not in cell
        assert "row_span" not in cell
    assert payload["summary"]["table_count"] == 1
