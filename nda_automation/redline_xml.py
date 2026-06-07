"""WordprocessingML tracked-change (revision) XML construction.

The "how" of redlines: revision attributes, tracked deletion/insertion XML,
diff-driven inline spacing, and run/paragraph property handling for tracked
paragraphs. docx_export owns the "what/which" -- mapping review items to source
paragraphs and deciding replace/insert/delete -- and calls into this module to
emit the markup. Depends only on docx_xml + inline_diff + the stdlib; never on
docx_export, so there is no import cycle.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import List, Tuple

from .inline_diff import diff_text_operations
from .docx_xml import _clone_element, _escape_xml, _w_tag, _word_paragraph_from_xml


def _source_tracked_replace_paragraph(
    source_paragraph: ET.Element,
    original: str,
    replacement: str,
    first_revision_id: int,
) -> Tuple[ET.Element, int]:
    tracked_paragraph_xml, next_revision_id = _tracked_replace_paragraph(original, replacement, first_revision_id)
    return _merge_source_paragraph_properties(source_paragraph, _word_paragraph_from_xml(tracked_paragraph_xml)), next_revision_id

def _source_tracked_delete_paragraph(source_paragraph: ET.Element, text: str, revision_id: int) -> ET.Element:
    return _merge_source_paragraph_properties(
        source_paragraph,
        _word_paragraph_from_xml(_tracked_delete_paragraph(text, revision_id)),
    )

def _source_tracked_insert_paragraphs(text: str, first_revision_id: int) -> List[ET.Element]:
    return [
        _word_paragraph_from_xml(paragraph_xml)
        for paragraph_xml in _tracked_insert_paragraphs(text, first_revision_id)
    ]

def _source_verbatim_paragraph(source_paragraph: ET.Element, text: str) -> ET.Element:
    """A plain (untracked) <w:p> carrying ``text`` and inheriting the source
    paragraph's formatting. Used when a split-block physical paragraph is re-emitted
    one paragraph per block: blocks with no redline must survive verbatim."""
    return _merge_source_paragraph_properties(
        source_paragraph,
        _word_paragraph_from_xml(f"<w:p>{_run(text)}</w:p>"),
    )

def _merge_source_paragraph_properties(source_paragraph: ET.Element, tracked_paragraph: ET.Element) -> ET.Element:
    merged = ET.Element(source_paragraph.tag, source_paragraph.attrib)
    source_properties = source_paragraph.find(_w_tag("pPr"))
    tracked_properties = tracked_paragraph.find(_w_tag("pPr"))
    merged_properties = _clone_element(source_properties) if source_properties is not None else None
    if merged_properties is not None:
        _strip_paragraph_property_revisions(merged_properties)

    if tracked_properties is not None:
        tracked_run_properties = tracked_properties.find(_w_tag("rPr"))
        if tracked_run_properties is not None:
            if merged_properties is None:
                merged_properties = ET.Element(_w_tag("pPr"))
            merged_run_properties = merged_properties.find(_w_tag("rPr"))
            if merged_run_properties is None:
                merged_properties.append(_clone_element(tracked_run_properties))
            else:
                for child in list(tracked_run_properties):
                    merged_run_properties.append(_clone_element(child))

    if merged_properties is not None:
        merged.append(merged_properties)
    for child in list(tracked_paragraph):
        if child.tag != _w_tag("pPr"):
            merged.append(_clone_element(child))
    # Carry the source paragraph's run formatting (bold/italic/underline/color/
    # fonts) onto the tracked runs so a replace/delete redline does not silently
    # strip the character formatting of the text it touches.
    _apply_source_run_properties(merged, _dominant_run_properties(source_paragraph))
    return merged

def _dominant_run_properties(source_paragraph: ET.Element) -> ET.Element | None:
    for run in source_paragraph.iter(_w_tag("r")):
        run_properties = run.find(_w_tag("rPr"))
        if run_properties is not None:
            return run_properties
    return None

def _apply_source_run_properties(paragraph: ET.Element, run_properties: ET.Element | None) -> None:
    if run_properties is None:
        return
    for run in paragraph.iter(_w_tag("r")):
        if run.find(_w_tag("rPr")) is None:
            run.insert(0, _clone_element(run_properties))

def _strip_paragraph_property_revisions(root: ET.Element) -> None:
    paragraph_properties = []
    if root.tag == _w_tag("pPr"):
        paragraph_properties.append(root)
    paragraph_properties.extend(root.findall(f".//{_w_tag('pPr')}"))
    for properties in paragraph_properties:
        for revision_tag in (_w_tag("pPrChange"),):
            for revision in list(properties.findall(revision_tag)):
                properties.remove(revision)
        run_properties = properties.find(_w_tag("rPr"))
        if run_properties is None:
            continue
        for revision_tag in (_w_tag("ins"), _w_tag("del"), _w_tag("rPrChange")):
            for revision in list(run_properties.findall(revision_tag)):
                run_properties.remove(revision)

def _run(text: str, bold: bool = False) -> str:
    run_props = "<w:rPr><w:b/></w:rPr>" if bold else ""
    parts = []
    for index, line in enumerate(str(text).split("\n")):
        if index:
            parts.append("<w:br/>")
        parts.append(f'<w:t xml:space="preserve">{_escape_xml(line)}</w:t>')
    return f"<w:r>{run_props}{''.join(parts)}</w:r>"

def _tracked_delete_paragraph(text: str, revision_id: int) -> str:
    revision_attrs = _revision_attrs(revision_id)
    return f"<w:p>{_tracked_delete_with_attrs(text, revision_attrs)}</w:p>"

def _tracked_replace_paragraph(original: str, replacement: str, first_revision_id: int) -> Tuple[str, int]:
    if "\n" in str(original) or "\n" in str(replacement):
        return (
            f"<w:p>{_tracked_delete(str(original), first_revision_id)}{_tracked_insert(str(replacement), first_revision_id + 1)}</w:p>",
            first_revision_id + 2,
        )

    runs: List[str] = []
    revision_id = first_revision_id
    current_type = ""
    current_parts: List[str] = []
    previous_original_token = ""
    previous_accepted_token = ""

    def flush_current() -> None:
        nonlocal revision_id, current_type, current_parts
        if not current_parts:
            return
        text = "".join(current_parts)
        if current_type == "delete":
            runs.append(_tracked_delete(text, revision_id))
            revision_id += 1
        elif current_type == "insert":
            runs.append(_tracked_insert(text, revision_id))
            revision_id += 1
        else:
            runs.append(_run(text))
        current_parts = []

    for operation_type, token in diff_text_operations(original, replacement):
        if operation_type != current_type:
            flush_current()
            current_type = operation_type
        if operation_type == "delete":
            prefix = " " if _needs_inline_space(previous_original_token, token) else ""
            previous_original_token = token
        elif operation_type == "insert":
            prefix = " " if _needs_inline_space(previous_accepted_token, token) else ""
            previous_accepted_token = token
        else:
            prefix = (
                " "
                if _needs_inline_space(previous_original_token, token)
                or _needs_inline_space(previous_accepted_token, token)
                else ""
            )
            previous_original_token = token
            previous_accepted_token = token
        current_parts.append(f"{prefix}{token}")

    flush_current()
    return f"<w:p>{''.join(runs)}</w:p>", revision_id

def _tracked_insert_paragraphs(text: str, first_revision_id: int) -> List[str]:
    blocks = [block for block in str(text).split("\n\n") if block.strip()]
    if not blocks:
        blocks = [str(text)]
    paragraphs: List[str] = []
    for index, block in enumerate(blocks):
        revision_attrs = _revision_attrs(first_revision_id + index)
        paragraphs.append(
            f"<w:p>{_tracked_insert_with_attrs(block, revision_attrs)}</w:p>"
        )
    return paragraphs

def _tracked_delete(text: str, revision_id: int) -> str:
    return _tracked_delete_with_attrs(text, _revision_attrs(revision_id))

def _tracked_insert(text: str, revision_id: int) -> str:
    return _tracked_insert_with_attrs(text, _revision_attrs(revision_id))

def _tracked_delete_with_attrs(text: str, revision_attrs: str) -> str:
    return f'<w:del {revision_attrs}>{_deleted_run(text)}</w:del>'

def _tracked_insert_with_attrs(text: str, revision_attrs: str) -> str:
    return f'<w:ins {revision_attrs}>{_run(text)}</w:ins>'

def _deleted_run(text: str) -> str:
    parts = []
    for index, line in enumerate(str(text).split("\n")):
        if index:
            parts.append("<w:br/>")
        parts.append(f'<w:delText xml:space="preserve">{_escape_xml(line)}</w:delText>')
    return f"<w:r>{''.join(parts)}</w:r>"

# Quote characters hug the content they wrap: a closing quote follows its word
# with no leading space, and an opening quote precedes its word with no trailing
# space. The tokenizer already carries each token's original leading whitespace
# (an opening quote arrives as ' "', a closing quote as '"'), so suppressing the
# heuristic space here just stops a spurious one from being ADDED -- it never
# removes a real space. Covers straight (" ') and curly (“ ” ‘ ’) quotes.
_QUOTE_CHARS = "\"'“”‘’"
_CLOSING_PUNCT_RE = re.compile(rf"^[,.;:!?%)\]{_QUOTE_CHARS}]$")
_OPENING_BEFORE_RE = re.compile(rf"^[(\[{_QUOTE_CHARS}]$")


def _needs_inline_space(previous_token: str, token: str) -> bool:
    if not previous_token:
        return False
    if re.match(r"^\s", token) or re.search(r"\s$", previous_token):
        return False
    token_core = re.sub(r"^\s+", "", token)
    previous_core = re.sub(r"^\s+", "", previous_token)
    # A closing quote/bracket/punctuation hugs the preceding token (no space before it).
    if _CLOSING_PUNCT_RE.match(token_core):
        return False
    # An opening quote/bracket hugs the following token (no space after it).
    if _OPENING_BEFORE_RE.match(previous_core):
        return False
    if re.match(r"^[$£€#@]$", previous_core) and re.match(r"^\d", token_core):
        return False
    return True

def _revision_attrs(revision_id: int) -> str:
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return f'w:id="{revision_id}" w:author="nda-automation" w:date="{timestamp}"'
