from nda_automation import redline_edit_contract


def test_backend_normalizer_rejects_manual_insert_after_paragraph():
    normalized = redline_edit_contract.normalize_redline_edit(
        {
            "id": "manual-insert",
            "clause_id": redline_edit_contract.MANUAL_VIEWER_EDIT_CLAUSE_ID,
            "action": redline_edit_contract.REDLINE_INSERT_AFTER_PARAGRAPH,
            "paragraph_id": "p1",
            "insert_text": "New clause.",
        },
    )

    assert normalized is None


def test_backend_normalizer_preserves_server_insert_after_paragraph():
    normalized = redline_edit_contract.normalize_redline_edit(
        {
            "id": "server-insert",
            "clause_id": "term_and_survival",
            "action": redline_edit_contract.REDLINE_INSERT_AFTER_PARAGRAPH,
            "paragraph_id": "p1",
            "replacement_text": "Survival clause.",
            "paragraph_index": "2",
            "source_index": "3",
        },
        require_content=True,
    )

    assert normalized is not None
    assert normalized["action"] == redline_edit_contract.REDLINE_INSERT_AFTER_PARAGRAPH
    assert normalized["clause_id"] == "term_and_survival"
    assert normalized["replacement_text"] == "Survival clause."
    assert normalized["paragraph_index"] == 2
    assert normalized["source_index"] == 3
    assert redline_edit_contract.redline_inserted_text(normalized) == "Survival clause."


def test_export_contract_trims_and_requires_manual_replace_fields():
    cleaned = redline_edit_contract.clean_export_redline_contract(
        {
            "action": redline_edit_contract.REDLINE_REPLACE_PARAGRAPH,
            "clause_id": redline_edit_contract.MANUAL_VIEWER_EDIT_CLAUSE_ID,
            "paragraph_id": " p1 ",
            "original_text": "  Old.  ",
            "replacement_text": "  New.  ",
        },
        redline_edit_contract.MANUAL_REDLINE_ACTIONS,
    )

    assert cleaned == {
        "action": redline_edit_contract.REDLINE_REPLACE_PARAGRAPH,
        "paragraph_id": "p1",
        "original_text": "Old.",
        "replacement_text": "New.",
        "anchor_text": "",
        "insert_text": "",
    }


def test_source_anchor_helpers_dedupe_by_normalized_text():
    edit = {
        "action": redline_edit_contract.REDLINE_REPLACE_PARAGRAPH,
        "paragraph_id": "p1",
        "original_text": "Old paragraph.",
        "anchor_text": " Old   paragraph. ",
        "source_index": 5,
    }

    assert redline_edit_contract.redline_source_index(edit) == 5
    assert redline_edit_contract.redline_review_paragraph_key(edit) == ("paragraph_id", "p1")
    assert redline_edit_contract.redline_anchor_texts(edit, {"p1": {"text": "Old paragraph."}}) == [
        "Old paragraph."
    ]
