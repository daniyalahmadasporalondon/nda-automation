from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Any
from zipfile import ZipFile
import xml.etree.ElementTree as ET

from nda_automation.redline_actions import (
    REDLINE_DELETE_PARAGRAPH,
    REDLINE_INSERT_AFTER_PARAGRAPH,
    REDLINE_REPLACE_PARAGRAPH,
)

W_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


@dataclass(frozen=True)
class RedlineContractInspection:
    errors: list[str]
    summary: dict[str, int]
    revision_states: list[tuple[str, str]]


def inspect_docx_redline_contract(docx_bytes: bytes, redline_edits: list[dict[str, Any]]) -> RedlineContractInspection:
    errors: list[str] = []
    settings_root, document_root = _docx_roots(docx_bytes)

    if settings_root.find(".//w:trackRevisions", W_NS) is None:
        errors.append("settings.xml does not enable Track Changes.")

    paragraphs = document_root.findall(".//w:p", W_NS)
    revision_states = [
        (
            revision_text_for_state(paragraph, accepted=False),
            revision_text_for_state(paragraph, accepted=True),
        )
        for paragraph in paragraphs
    ]
    summary = {
        "expected_replace": 0,
        "expected_insert": 0,
        "expected_delete": 0,
        "actual_inline_deletions": len(document_root.findall(".//w:del", W_NS)),
        "actual_inline_insertions": len(document_root.findall(".//w:ins", W_NS)),
        "invalid_paragraph_property_deletions": len(paragraph_property_revisions(document_root, "del")),
        "invalid_paragraph_property_insertions": len(paragraph_property_revisions(document_root, "ins")),
    }
    if paragraph_property_revisions(document_root, "del"):
        errors.append("document contains w:del inside paragraph properties, which Word repairs on open.")
    if paragraph_property_revisions(document_root, "ins"):
        errors.append("document contains w:ins inside paragraph properties, which Word repairs on open.")

    for edit in redline_edits:
        action = edit.get("action")
        edit_id = str(edit.get("id") or edit.get("clause_id") or action)
        if action == REDLINE_REPLACE_PARAGRAPH:
            summary["expected_replace"] += 1
            original = str(edit.get("original_text") or "")
            replacement = str(edit.get("replacement_text") or "")
            matches = [
                paragraph
                for paragraph in paragraphs
                if revision_text_for_state(paragraph, accepted=False) == original
                and revision_text_for_state(paragraph, accepted=True) == replacement
            ]
            if not matches:
                errors.append(f"{edit_id}: replace did not preserve original/accepted paragraph states.")
                continue
            if not matches[0].findall(".//w:del", W_NS):
                errors.append(f"{edit_id}: replace is missing inline w:del.")
            if not matches[0].findall(".//w:ins", W_NS):
                errors.append(f"{edit_id}: replace is missing inline w:ins.")
        elif action == REDLINE_INSERT_AFTER_PARAGRAPH:
            summary["expected_insert"] += 1
            insert_text = str(edit.get("insert_text") or edit.get("replacement_text") or "")
            matches = [
                paragraph
                for paragraph in paragraphs
                if revision_text_for_state(paragraph, accepted=False) == ""
                and revision_text_for_state(paragraph, accepted=True) == insert_text
            ]
            if not matches:
                errors.append(f"{edit_id}: insert did not preserve empty-original/accepted paragraph states.")
                continue
            if not matches[0].findall(".//w:ins", W_NS):
                errors.append(f"{edit_id}: insert is missing inserted text in w:ins.")
        elif action == REDLINE_DELETE_PARAGRAPH:
            summary["expected_delete"] += 1
            original = str(edit.get("original_text") or "")
            matches = [
                paragraph
                for paragraph in paragraphs
                if revision_text_for_state(paragraph, accepted=False) == original
                and revision_text_for_state(paragraph, accepted=True) == ""
            ]
            if not matches:
                errors.append(f"{edit_id}: delete did not preserve original/empty-accepted paragraph states.")
                continue
            if not matches[0].findall(".//w:delText", W_NS):
                errors.append(f"{edit_id}: delete is missing deleted text in w:delText.")

    return RedlineContractInspection(errors=errors, summary=summary, revision_states=revision_states)


def assert_docx_redline_contract(testcase: Any, docx_bytes: bytes, redline_edits: list[dict[str, Any]]) -> RedlineContractInspection:
    inspection = inspect_docx_redline_contract(docx_bytes, redline_edits)
    testcase.assertEqual(inspection.errors, [], f"Redline contract failed: {inspection.summary}")
    return inspection


def tracked_deleted_text(document_root: ET.Element) -> list[str]:
    return [
        "".join(node.text or "" for node in deletion.findall(".//w:delText", W_NS))
        for deletion in document_root.findall(".//w:del", W_NS)
    ]


def tracked_inserted_text(document_root: ET.Element) -> list[str]:
    return [
        "".join(node.text or "" for node in insertion.findall(".//w:t", W_NS))
        for insertion in document_root.findall(".//w:ins", W_NS)
    ]


def paragraph_property_revisions(document_root: ET.Element, kind: str) -> list[ET.Element]:
    return document_root.findall(f".//w:pPr/w:rPr/w:{kind}", W_NS)


def paragraph_mark_revisions(document_root: ET.Element, kind: str) -> list[ET.Element]:
    return paragraph_property_revisions(document_root, kind)


def revision_text_for_state(node: ET.Element, accepted: bool) -> str:
    tag = node.tag.rsplit("}", 1)[-1]
    if tag == "del":
        return "".join(item.text or "" for item in node.findall(".//w:delText", W_NS)) if not accepted else ""
    if tag == "ins":
        return "".join(item.text or "" for item in node.findall(".//w:t", W_NS)) if accepted else ""
    if tag == "t":
        return node.text or ""
    if tag == "br":
        return "\n"
    return "".join(revision_text_for_state(child, accepted) for child in list(node))


def _docx_roots(docx_bytes: bytes) -> tuple[ET.Element, ET.Element]:
    with ZipFile(BytesIO(docx_bytes)) as archive:
        settings_root = ET.fromstring(archive.read("word/settings.xml"))
        document_root = ET.fromstring(archive.read("word/document.xml"))
    return settings_root, document_root
