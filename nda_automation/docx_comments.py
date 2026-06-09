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
from typing import Dict, List, NamedTuple

from .docx_xml import (
    _clone_element,
    _normalize_paragraph_text,
    _parse_docx_xml_with_namespaces,
    _w_tag,
    _xml_bytes,
)

# Word 2010 wordml (carries the per-paragraph w14:paraId that ties a comment to
# its commentsExtended thread entry).
W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
# Word 2012 wordml (commentsExtended: thread parent + resolved/done state).
W15_NS = "http://schemas.microsoft.com/office/word/2012/wordml"
ET.register_namespace("w14", W14_NS)
ET.register_namespace("w15", W15_NS)

COMMENTS_EXTENDED_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtended+xml"
)
COMMENTS_EXTENDED_RELATIONSHIP_TYPE = (
    "http://schemas.microsoft.com/office/2011/relationships/commentsExtended"
)


def _w14_tag(tag: str) -> str:
    return f"{{{W14_NS}}}{tag}"


def _w15_tag(tag: str) -> str:
    return f"{{{W15_NS}}}{tag}"


class AppendedComments(NamedTuple):
    """Result of folding a batch of comment objects into ``word/comments.xml``.

    ``assigned`` is each input comment dict copied with two build-local keys added:
      * ``_word_comment_id`` -- the ``w:id`` written into ``word/comments.xml`` and
        used by ``_apply_comment_anchor`` to place the in-document range/reference.
      * ``_word_para_id`` -- the 8-hex-char ``w14:paraId`` stamped on the comment's
        first ``<w:p>``; the join key into ``word/commentsExtended.xml``.
    ``comments_xml`` is the serialized ``word/comments.xml`` bytes (empty when there
    were no comments). Build ``word/commentsExtended.xml`` from ``assigned`` via
    :func:`_comments_extended_xml_for_assigned`.
    """

    assigned: List[dict]
    comments_xml: bytes


def _comments_xml_with_appended_comments(
    existing_comments_xml: bytes | None,
    comments: List[dict],
) -> AppendedComments:
    if not comments:
        return AppendedComments([], b"")
    comments_root, namespaces = _comments_root(existing_comments_xml)
    next_id = _next_comment_id(comments_root)
    used_para_ids = _existing_para_ids(comments_root)
    assigned: List[dict] = []
    for comment in comments:
        comment_id = str(next_id)
        next_id += 1
        para_id = _allocate_para_id(used_para_ids)
        comments_root.append(_word_comment(comment_id, para_id, comment))
        assigned.append({**comment, "_word_comment_id": comment_id, "_word_para_id": para_id})
    return AppendedComments(assigned, _xml_bytes(comments_root, namespace_declarations=namespaces))

def _comments_root(existing_comments_xml: bytes | None) -> tuple[ET.Element, Dict[str, str]]:
    if existing_comments_xml:
        return _parse_docx_xml_with_namespaces(existing_comments_xml, part_name="word/comments.xml")
    return ET.Element(_w_tag("comments")), {}

def _comments_extended_xml_for_assigned(
    existing_comments_extended_xml: bytes | None,
    assigned_comments: List[dict],
) -> bytes:
    """Build (or extend) ``word/commentsExtended.xml`` for a batch of comments that
    have already been folded into ``word/comments.xml`` (i.e. each carries the
    ``_word_para_id`` / ``parent_id`` / ``resolved`` it needs).

    One ``<w15:commentEx>`` per comment, keyed by its ``w14:paraId``:
      * replies (non-empty ``parent_id``) carry ``w15:paraIdParent`` pointing at the
        ROOT comment's paraId;
      * ``w15:done="1"`` for every comment whose thread ROOT is ``resolved`` -- the
        root AND all of its replies -- else ``"0"``.

    Returns ``b""`` when there is nothing to write. When ``existing_comments_extended_xml``
    is given (the source-archive merge), new entries are appended to it; otherwise a
    fresh part is created.
    """
    if not assigned_comments:
        return existing_comments_extended_xml or b""
    root, namespaces = _comments_extended_root(existing_comments_extended_xml)

    # parent_id carries the ROOT comment's APPLICATION id (e.g. "c1"), so the
    # paraIdParent join is keyed by application id, not the w:id we stamped.
    para_id_by_app_id = {
        str(comment.get("id") or ""): str(comment.get("_word_para_id") or "")
        for comment in assigned_comments
        if str(comment.get("id") or "") and comment.get("_word_para_id")
    }
    resolved_root_ids = {
        str(comment.get("id") or "")
        for comment in assigned_comments
        if not str(comment.get("parent_id") or "").strip() and bool(comment.get("resolved"))
    }

    for comment in assigned_comments:
        para_id = str(comment.get("_word_para_id") or "")
        if not para_id:
            continue
        parent_app_id = str(comment.get("parent_id") or "").strip()
        attributes = {_w15_tag("paraId"): para_id}
        if parent_app_id:
            parent_para_id = para_id_by_app_id.get(parent_app_id)
            if parent_para_id:
                attributes[_w15_tag("paraIdParent")] = parent_para_id
            thread_root_id = parent_app_id
        else:
            thread_root_id = str(comment.get("id") or "")
        attributes[_w15_tag("done")] = "1" if thread_root_id in resolved_root_ids else "0"
        root.append(ET.Element(_w15_tag("commentEx"), attributes))
    return _xml_bytes(root, namespace_declarations={**namespaces, "w15": W15_NS})

def _comments_extended_root(
    existing_comments_extended_xml: bytes | None,
) -> tuple[ET.Element, Dict[str, str]]:
    if existing_comments_extended_xml:
        return _parse_docx_xml_with_namespaces(
            existing_comments_extended_xml, part_name="word/commentsExtended.xml"
        )
    return ET.Element(_w15_tag("commentsEx")), {}

def _next_comment_id(comments_root: ET.Element) -> int:
    comment_ids = []
    for comment in comments_root.findall(_w_tag("comment")):
        try:
            comment_ids.append(int(comment.attrib.get(_w_tag("id"), "")))
        except ValueError:
            continue
    return max(comment_ids, default=-1) + 1

def _existing_para_ids(comments_root: ET.Element) -> set[str]:
    """paraIds already present on comment paragraphs in a source comments part, so a
    freshly allocated paraId never collides with one Word already wrote."""
    used: set[str] = set()
    for paragraph in comments_root.iter(_w_tag("p")):
        para_id = paragraph.attrib.get(_w14_tag("paraId"))
        if para_id:
            used.add(para_id.upper())
    return used

def _allocate_para_id(used_para_ids: set[str]) -> str:
    """A fresh 8-uppercase-hex w14:paraId, unique within this build pass. Seeded from
    a counter so a single build is deterministic; skips any value already taken."""
    while True:
        _allocate_para_id._counter += 1  # type: ignore[attr-defined]
        candidate = f"{_allocate_para_id._counter & 0xFFFFFFFF:08X}"  # type: ignore[attr-defined]
        if candidate not in used_para_ids:
            used_para_ids.add(candidate)
            return candidate

# Seed high enough to look like a real Word paraId and stay clear of 00000000.
_allocate_para_id._counter = 0x10000000  # type: ignore[attr-defined]

def _word_comment(comment_id: str, para_id: str, comment: dict) -> ET.Element:
    created_at = str(comment.get("created_at") or "").strip()
    if not created_at:
        created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    element = ET.Element(_w_tag("comment"), {
        _w_tag("id"): comment_id,
        _w_tag("author"): str(comment.get("author") or "Reviewer")[:255],
        _w_tag("date"): created_at,
        _w_tag("initials"): _comment_initials(str(comment.get("author") or "Reviewer")),
    })
    paragraph_texts = _comment_paragraph_texts(comment) or [""]
    for paragraph_text in paragraph_texts:
        paragraph = ET.SubElement(element, _w_tag("p"))
        # Word ties a comment to its commentsExtended thread entry through the
        # paraId on the comment's LAST paragraph; stamp every paragraph so the
        # join key is unambiguous regardless of how many lines the comment has.
        paragraph.set(_w14_tag("paraId"), para_id)
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
