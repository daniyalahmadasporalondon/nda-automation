"""Word comment XML construction and in-paragraph anchoring mechanics.

Extracted from docx_export so comment construction and the text-selection /
anchoring algorithm have a real unit seam, instead of being reachable only
through full build_*_docx() integration runs. docx_export stays the orchestrator
(it decides WHICH paragraph gets WHICH comment, using review results and
source-paragraph indexing); this module decides HOW a comment is expressed in
WordprocessingML and HOW its anchor is placed within a paragraph's runs. Depends
only on docx_xml and the stdlib -- never on docx_export -- so there is no cycle.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Dict, List

from .docx_xml import (
    _clone_element,
    _normalize_paragraph_text,
    _parse_docx_xml_with_namespaces,
    _w_tag,
    _xml_bytes,
)


def _comments_xml_with_appended_comments(
    existing_comments_xml: bytes | None,
    comments: List[dict],
) -> tuple[List[dict], bytes]:
    if not comments:
        return [], b""
    comments_root, namespaces = _comments_root(existing_comments_xml)
    next_id = _next_comment_id(comments_root)
    assigned: List[dict] = []
    for comment in comments:
        comment_id = str(next_id)
        next_id += 1
        comments_root.append(_word_comment(comment_id, comment))
        assigned.append({**comment, "_word_comment_id": comment_id})
    return assigned, _xml_bytes(comments_root, namespace_declarations=namespaces)

def _comments_root(existing_comments_xml: bytes | None) -> tuple[ET.Element, Dict[str, str]]:
    if existing_comments_xml:
        return _parse_docx_xml_with_namespaces(existing_comments_xml, part_name="word/comments.xml")
    return ET.Element(_w_tag("comments")), {}

def _next_comment_id(comments_root: ET.Element) -> int:
    comment_ids = []
    for comment in comments_root.findall(_w_tag("comment")):
        try:
            comment_ids.append(int(comment.attrib.get(_w_tag("id"), "")))
        except ValueError:
            continue
    return max(comment_ids, default=-1) + 1

def _word_comment(comment_id: str, comment: dict) -> ET.Element:
    created_at = str(comment.get("created_at") or "").strip()
    if not created_at:
        created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    element = ET.Element(_w_tag("comment"), {
        _w_tag("id"): comment_id,
        _w_tag("author"): str(comment.get("author") or "Reviewer")[:255],
        _w_tag("date"): created_at,
        _w_tag("initials"): _comment_initials(str(comment.get("author") or "Reviewer")),
    })
    for paragraph_text in _comment_paragraph_texts(comment):
        paragraph = ET.SubElement(element, _w_tag("p"))
        paragraph.append(_word_run(paragraph_text))
    return element

def _comment_paragraph_texts(comment: dict) -> List[str]:
    parts = []
    clause_name = str(comment.get("clause_name") or "").strip()
    clause_id = str(comment.get("clause_id") or "").strip()
    if clause_name:
        parts.append(f"{clause_name}:")
    elif clause_id:
        parts.append(f"{clause_id}:")
    parts.append(str(comment.get("text") or "").strip())
    return [part for part in parts if part]

def _comment_initials(author: str) -> str:
    initials = "".join(part[0] for part in re.findall(r"[A-Za-z0-9]+", author)[:3])
    return (initials or "R")[:9]

def _word_run(text: str) -> ET.Element:
    run = ET.Element(_w_tag("r"))
    for index, line in enumerate(str(text).split("\n")):
        if index:
            ET.SubElement(run, _w_tag("br"))
        text_node = ET.SubElement(run, _w_tag("t"), {"{http://www.w3.org/XML/1998/namespace}space": "preserve"})
        text_node.text = line
    return run

def _apply_comment_anchor(paragraph: ET.Element, comment: dict) -> None:
    comment_id = str(comment.get("_word_comment_id") or "")
    if not comment_id:
        return
    if _apply_comment_anchor_to_text_selection(paragraph, comment_id, comment):
        return
    start = ET.Element(_w_tag("commentRangeStart"), {_w_tag("id"): comment_id})
    end = ET.Element(_w_tag("commentRangeEnd"), {_w_tag("id"): comment_id})
    reference_run = _comment_reference_run(comment_id)

    insert_position = 1 if len(paragraph) and paragraph[0].tag == _w_tag("pPr") else 0
    paragraph.insert(insert_position, start)
    paragraph.append(end)
    paragraph.append(reference_run)

def _apply_comment_anchor_to_text_selection(paragraph: ET.Element, comment_id: str, comment: dict) -> bool:
    selected_text = str(comment.get("selected_text") or "")
    selection = _comment_selection_range(paragraph, comment)
    if selection is None:
        return False
    selection_start, selection_end = selection
    if selection_start >= selection_end:
        return False
    paragraph_text = _paragraph_direct_text(paragraph)
    if not paragraph_text or selection_end > len(paragraph_text):
        return False
    if selected_text and _normalize_paragraph_text(paragraph_text[selection_start:selection_end]) != _normalize_paragraph_text(selected_text):
        return False

    children = list(paragraph)
    new_children: list[ET.Element] = []
    position = 0
    started = False
    ended = False
    for child in children:
        run_text = _direct_run_text(child)
        if run_text is None:
            new_children.append(child)
            continue
        run_start = position
        run_end = position + len(run_text)
        position = run_end
        if run_end <= selection_start or run_start >= selection_end:
            new_children.append(child)
            continue

        local_start = max(0, selection_start - run_start)
        local_end = min(len(run_text), selection_end - run_start)
        before = run_text[:local_start]
        selected = run_text[local_start:local_end]
        after = run_text[local_end:]
        if before:
            new_children.append(_clone_run_with_text(child, before))
        if selected and not started:
            new_children.append(ET.Element(_w_tag("commentRangeStart"), {_w_tag("id"): comment_id}))
            started = True
        if selected:
            new_children.append(_clone_run_with_text(child, selected))
        if selected and not ended and run_end >= selection_end:
            new_children.append(ET.Element(_w_tag("commentRangeEnd"), {_w_tag("id"): comment_id}))
            ended = True
        if after:
            new_children.append(_clone_run_with_text(child, after))

    if not started or not ended:
        return False
    new_children.append(_comment_reference_run(comment_id))
    paragraph[:] = new_children
    return True

def _comment_selection_range(paragraph: ET.Element, comment: dict) -> tuple[int, int] | None:
    paragraph_text = _paragraph_direct_text(paragraph)
    if not paragraph_text:
        return None
    try:
        selection_start = int(comment.get("selection_start"))
        selection_end = int(comment.get("selection_end"))
    except (TypeError, ValueError):
        selection_start = -1
        selection_end = -1
    selected_text = str(comment.get("selected_text") or "")
    if (
        0 <= selection_start < selection_end <= len(paragraph_text)
        and (
            not selected_text
            or _normalize_paragraph_text(paragraph_text[selection_start:selection_end]) == _normalize_paragraph_text(selected_text)
        )
    ):
        return selection_start, selection_end
    if selected_text:
        index = paragraph_text.find(selected_text)
        if index >= 0:
            return index, index + len(selected_text)
    return None

def _paragraph_direct_text(paragraph: ET.Element) -> str:
    parts = []
    for child in list(paragraph):
        run_text = _direct_run_text(child)
        if run_text is None:
            continue
        parts.append(run_text)
    return "".join(parts)

def _direct_run_text(run: ET.Element) -> str | None:
    if run.tag != _w_tag("r"):
        return None
    text_nodes = [child for child in list(run) if child.tag == _w_tag("t")]
    other_text_nodes = [
        child
        for child in list(run)
        if child.tag in {_w_tag("tab"), _w_tag("br"), _w_tag("cr"), _w_tag("delText")}
    ]
    if len(text_nodes) != 1 or other_text_nodes:
        return None
    return text_nodes[0].text or ""

def _clone_run_with_text(run: ET.Element, text: str) -> ET.Element:
    cloned = ET.Element(run.tag, run.attrib)
    run_properties = run.find(_w_tag("rPr"))
    if run_properties is not None:
        cloned.append(_clone_element(run_properties))
    text_node = ET.SubElement(cloned, _w_tag("t"), {"{http://www.w3.org/XML/1998/namespace}space": "preserve"})
    text_node.text = text
    return cloned

def _comment_reference_run(comment_id: str) -> ET.Element:
    reference_run = ET.Element(_w_tag("r"))
    reference_props = ET.SubElement(reference_run, _w_tag("rPr"))
    ET.SubElement(reference_props, _w_tag("rStyle"), {_w_tag("val"): "CommentReference"})
    ET.SubElement(reference_run, _w_tag("commentReference"), {_w_tag("id"): comment_id})
    return reference_run
