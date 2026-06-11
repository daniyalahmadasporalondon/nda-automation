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
                "table": {"table_index": 1, "row_index": 1, "cell_index": 2},
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
    assert payload["capabilities"]["run_colors"] is True
    assert payload["summary"] == {
        "paragraph_count": 4,
        "block_count": 2,
        "table_count": 1,
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
    assert table["rows"][0]["cells"][1]["blocks"][0]["style"]["style_name"] == "Table Text"
    assert table["rows"][1]["cells"][0]["paragraph_ids"] == ["p4"]


def test_source_fidelity_marks_pdf_as_source_preview_limited():
    review_result = {
        "source": {"type": "pdf"},
        "paragraphs": [
            {"id": "p1", "index": 1, "text": "PDF page one.", "source_part": "pdf", "page_number": 1},
            {"id": "p2", "index": 2, "text": "PDF page two.", "source_part": "pdf", "page_number": 2},
        ],
    }

    payload = source_fidelity_payload(review_result, source=review_result["source"])

    assert payload["source_type"] == "pdf"
    assert payload["capabilities"]["pdf_page_references"] is True
    assert payload["summary"]["pdf_page_reference_count"] == 2
    assert {limitation["code"] for limitation in payload["limitations"]} == {
        "semantic_review_is_paragraph_based",
        "pdf_visual_fidelity_requires_source_preview",
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
