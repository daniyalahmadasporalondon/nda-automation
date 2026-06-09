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

def _apply_tracked_paragraph_format(
    source_p: ET.Element,
    paragraph_ops: List[dict],
    rev_id: int,
) -> Tuple[ET.Element, int]:
    """Emit a tracked *paragraph-format* change: a `<w:pPrChange>` recording the
    from-state, with the new alignment/font applied to a fresh `<w:pPr>`. The text
    runs are left untouched (no `<w:ins>`/`<w:del>`), since a format redline changes
    only the paragraph's appearance.

    Clones ``source_p``; takes its current `<w:pPr>` (or an empty one) as the
    original. Applies each ``scope:"paragraph"`` op -- alignment sets `<w:jc>`
    (justify->Word's ``both``), font sets the run-default
    `<w:rPr><w:rFonts ascii/hAnsi/cs="..."/></w:rPr>` -- preserving every other
    existing pPr child. Appends `<w:pPrChange>` whose nested original `<w:pPr>` has
    its own revisions stripped first, so no stale `<w:pPrChange>` is ever nested.
    Run-scope ops are ignored here (later inline milestone). Returns the rebuilt
    `<w:p>` and the next revision id."""
    rebuilt = ET.Element(source_p.tag, dict(source_p.attrib))

    source_properties = source_p.find(_w_tag("pPr"))
    original_properties = (
        _clone_element(source_properties)
        if source_properties is not None
        else ET.Element(_w_tag("pPr"))
    )

    new_properties = _clone_element(original_properties)
    # Never carry a prior tracked-format change into the new (current) pPr.
    for stale in list(new_properties.findall(_w_tag("pPrChange"))):
        new_properties.remove(stale)

    for op in paragraph_ops:
        if not isinstance(op, dict) or op.get("scope") != "paragraph":
            continue
        prop = op.get("property")
        if prop == "alignment":
            _set_paragraph_alignment(new_properties, str(op.get("to") or ""))
        elif prop == "font":
            _set_paragraph_run_default_font(new_properties, str(op.get("to") or ""))

    # The from-state record: a clean clone of the ORIGINAL pPr (its own revisions
    # stripped) wrapped in the pPrChange so Word can roll the formatting back.
    change_original = _clone_element(original_properties)
    _strip_paragraph_property_revisions(change_original)
    paragraph_property_change = ET.SubElement(new_properties, _w_tag("pPrChange"))
    _set_revision_attrs(paragraph_property_change, rev_id)
    paragraph_property_change.append(change_original)

    rebuilt.append(new_properties)
    for child in list(source_p):
        if child.tag != _w_tag("pPr"):
            rebuilt.append(_clone_element(child))
    return rebuilt, rev_id + 1


def _apply_tracked_run_format(
    source_p: ET.Element,
    run_ops: List[dict],
    rev_id: int,
) -> Tuple[ET.Element, int]:
    """Emit tracked *inline run-format* changes: per character-range `<w:rPrChange>`
    records on the runs they cover, with the new bold/italic/font applied to a fresh
    `<w:rPr>`. The paragraph text is left byte-identical -- no `<w:ins>`/`<w:del>`.

    Walks the paragraph's text runs (`<w:r>` carrying `<w:t>`), accumulating a running
    character offset over the concatenated `<w:t>` text (the same offset space the
    frontend's ``start``/``end`` index). Every op boundary (``start`` and ``end``)
    becomes a cut point, and each run is split at the cut points falling strictly
    inside it so each resulting run lies entirely inside-or-outside every op range; the
    text slices are byte-identical to the source and each split inherits its run's
    original `<w:rPr>`.

    For each run segment covered by one or more ops, clones the run's ORIGINAL
    `<w:rPr>` (``orig_rpr``), builds a new `<w:rPr>` = orig + each op's ``to`` change
    (``bold`` toggles `<w:b/>`, ``italic`` toggles `<w:i/>`, ``font`` sets
    `<w:rFonts ascii/hAnsi/cs>`), and appends -- as the LAST child of the new rPr --
    `<w:rPrChange>` whose nested original `<w:rPr>` is a clone of ``orig_rpr`` with any
    existing `<w:rPrChange>` stripped (so no stale revision nests, and the from-state
    is the run's ACTUAL formatting rather than the op's ``from``). Each distinct
    covered segment consumes one revision id. Runs no op covers, and non-run children,
    pass through verbatim. Returns the rebuilt `<w:p>` and the next revision id."""
    # Shallow-copy each op so the local clip bookkeeping below never leaks back into
    # the caller's redline ``format_ops``.
    normalized_ops = [
        dict(op)
        for op in run_ops
        if isinstance(op, dict)
        and op.get("scope") == "run"
        and op.get("property") in ("bold", "italic", "font")
        and isinstance(op.get("start"), int)
        and isinstance(op.get("end"), int)
        and int(op["start"]) < int(op["end"])
    ]

    rebuilt = ET.Element(source_p.tag, dict(source_p.attrib))
    if not normalized_ops:
        for child in list(source_p):
            rebuilt.append(_clone_element(child))
        return rebuilt, rev_id

    # The offset space MUST be byte-identical to the frontend's: the paragraph text
    # the FE indexes into (docx_text._paragraph_text/_run_text) renders <w:tab> as
    # "\t" and <w:br>/<w:cr> as "\n", and tiles all runs of the logical block. So the
    # running offset advances by each run's FULL tab/break-aware length, not the
    # <w:t>-only concatenation -- otherwise ops land on shifted characters.
    raw_text = "".join(
        _run_offset_text(child)
        for child in source_p
        if child.tag == _w_tag("r")
    )
    total_offset_length = len(raw_text)
    # The frontend indexes into the STRIPPED paragraph text (docx_text._paragraph_text
    # strips the joined run text and _trim_run_edges trims the edge runs), but the runs
    # walked here are the RAW source runs, which may carry leading/trailing whitespace.
    # Shift every op by the leading-whitespace count so the cut points land on the same
    # characters the frontend selected -- without rebuilding the paragraph (which would
    # destroy <w:tab>/<w:br> run structure that the tab/break offset handling relies on).
    leading_ws = len(raw_text) - len(raw_text.lstrip())

    cut_points = set()
    for op in normalized_ops:
        # Belt-and-braces: clip every op to [0, total_offset_length] and drop any
        # whose range is then empty -- a mis-sized op fails safe (no change) rather
        # than mis-placing a <w:rPrChange> past the end of the run text.
        op_start = max(0, int(op["start"]) + leading_ws)
        op_end = min(total_offset_length, int(op["end"]) + leading_ws)
        if op_start >= op_end:
            continue
        op["_clipped_start"] = op_start
        op["_clipped_end"] = op_end
        cut_points.add(op_start)
        cut_points.add(op_end)

    applicable_ops = [op for op in normalized_ops if "_clipped_start" in op]
    if not applicable_ops:
        for child in list(source_p):
            rebuilt.append(_clone_element(child))
        return rebuilt, rev_id

    offset = 0
    for child in list(source_p):
        if child.tag != _w_tag("r"):
            # pPr and other non-run children pass through untouched and do NOT
            # advance the character offset (only runs carry block text).
            rebuilt.append(_clone_element(child))
            continue

        run_offset_text = _run_offset_text(child)
        run_start = offset
        run_end = offset + len(run_offset_text)
        offset = run_end

        # A run carrying flow content (<w:tab>/<w:br>/<w:cr>) cannot be re-emitted as
        # a single <w:t> segment, so it is NEVER split: pass it through verbatim while
        # still advancing the offset by its full tab/break-aware length so the running
        # offset stays aligned with the FE text space. Only runs whose content is pure
        # <w:t> text are split and re-styled.
        if not run_offset_text or _run_has_flow_content(child):
            rebuilt.append(_clone_element(child))
            continue

        # run_offset_text == the run's concatenated <w:t> text here (no flow content),
        # so a [run_start, run_end) slice indexes the <w:t> bytes directly.
        run_text = run_offset_text
        interior_cuts = sorted(c for c in cut_points if run_start < c < run_end)
        boundaries = [run_start, *interior_cuts, run_end]
        for segment_start, segment_end in zip(boundaries, boundaries[1:]):
            segment_text = run_text[segment_start - run_start : segment_end - run_start]
            covering = [
                op
                for op in applicable_ops
                if op["_clipped_start"] <= segment_start and segment_end <= op["_clipped_end"]
            ]
            if not covering:
                rebuilt.append(_run_segment(child, segment_text, None))
                continue
            new_rpr, rev_id = _run_format_changed_rpr(child, covering, rev_id)
            rebuilt.append(_run_segment(child, segment_text, new_rpr))

    return rebuilt, rev_id


def _run_concatenated_text(run: ET.Element) -> str:
    return "".join(node.text or "" for node in run.findall(_w_tag("t")))


def _run_offset_text(run: ET.Element) -> str:
    """The run's text in the frontend's offset space: a mirror of
    ``docx_text._run_text`` -- ``<w:t>`` text plus "\\t" for ``<w:tab>`` and "\\n" for
    ``<w:br>``/``<w:cr>``. The run-format offsets index into this space, so the
    splitter must measure runs the same way."""
    parts: List[str] = []
    for node in run.iter():
        if node.tag == _w_tag("t") and node.text:
            parts.append(node.text)
        elif node.tag == _w_tag("tab"):
            parts.append("\t")
        elif node.tag in {_w_tag("br"), _w_tag("cr")}:
            parts.append("\n")
    return "".join(parts)


def _run_has_flow_content(run: ET.Element) -> bool:
    """Whether the run carries non-``<w:t>`` flow content (``<w:tab>``/``<w:br>``/
    ``<w:cr>``). Such a run cannot be re-emitted as a single ``<w:t>`` segment, so the
    splitter passes it through verbatim (advancing the offset by its full length)."""
    for node in run.iter():
        if node.tag in {_w_tag("tab"), _w_tag("br"), _w_tag("cr")}:
            return True
    return False


def _run_segment(source_run: ET.Element, text: str, new_rpr: ET.Element | None) -> ET.Element:
    """A single-`<w:t>` `<w:r>` carrying ``text`` and ``new_rpr`` (the segment's rPr).

    When ``new_rpr`` is None the segment is uncovered and keeps a clone of the source
    run's original `<w:rPr>` verbatim; otherwise it carries the already-built new rPr
    (with its `<w:rPrChange>`). The text slice is emitted exactly, preserving spaces."""
    segment = ET.Element(_w_tag("r"))
    if new_rpr is not None:
        segment.append(new_rpr)
    else:
        original_rpr = source_run.find(_w_tag("rPr"))
        if original_rpr is not None:
            segment.append(_clone_element(original_rpr))
    text_element = ET.SubElement(segment, _w_tag("t"))
    text_element.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    text_element.text = text
    return segment


def _run_format_changed_rpr(
    source_run: ET.Element,
    covering_ops: List[dict],
    rev_id: int,
) -> Tuple[ET.Element, int]:
    """Build the new `<w:rPr>` for a covered segment: the run's original rPr plus each
    covering op's ``to`` change, with a trailing `<w:rPrChange>` recording the
    from-state. Returns the new rPr and the next revision id (one id consumed)."""
    source_rpr = source_run.find(_w_tag("rPr"))
    original_rpr = _clone_element(source_rpr) if source_rpr is not None else ET.Element(_w_tag("rPr"))

    new_rpr = _clone_element(original_rpr)
    # Never carry a prior tracked run-format change into the new (current) rPr.
    for stale in list(new_rpr.findall(_w_tag("rPrChange"))):
        new_rpr.remove(stale)

    for op in covering_ops:
        prop = op.get("property")
        if prop == "bold":
            _set_run_toggle(new_rpr, "b", bool(op.get("to")))
        elif prop == "italic":
            _set_run_toggle(new_rpr, "i", bool(op.get("to")))
        elif prop == "font":
            _set_run_font(new_rpr, str(op.get("to") or ""))

    # The from-state record: a clean clone of the ORIGINAL rPr (its own rPrChange
    # stripped) wrapped in <w:rPrChange> so Word can roll the formatting back.
    change_original = _clone_element(original_rpr)
    for stale in list(change_original.findall(_w_tag("rPrChange"))):
        change_original.remove(stale)
    run_property_change = ET.SubElement(new_rpr, _w_tag("rPrChange"))
    _set_revision_attrs(run_property_change, rev_id)
    run_property_change.append(change_original)
    return new_rpr, rev_id + 1


def _set_run_toggle(run_properties: ET.Element, tag: str, enabled: bool) -> None:
    """Ensure a toggle child (`<w:b/>`/`<w:i/>`) is ON when ``enabled`` and absent
    otherwise. Toggle order in CT_RPr places b/i early, before rFonts-less props; a
    fresh toggle is inserted at the front, ahead of any trailing `<w:rPrChange>`.

    On enable: if the element is ABSENT insert a fresh (val-less = on) one; if it is
    PRESENT but explicitly off (``w:val`` of false/0/off/none) strip that falsy val so
    the toggle becomes ON. Without this an explicit-off `<w:b w:val="false"/>` source
    run would stay un-bold and emit a phantom no-op revision."""
    existing = run_properties.find(_w_tag(tag))
    if enabled:
        if existing is None:
            run_properties.insert(0, ET.Element(_w_tag(tag)))
        else:
            existing.attrib.pop(_w_tag("val"), None)
            existing.attrib.pop("val", None)
    else:
        for node in list(run_properties.findall(_w_tag(tag))):
            run_properties.remove(node)


def _set_run_font(run_properties: ET.Element, font: str) -> None:
    if not font:
        return
    rfonts = run_properties.find(_w_tag("rFonts"))
    if rfonts is None:
        rfonts = ET.Element(_w_tag("rFonts"))
        # rFonts leads CT_RPr; place it before any existing children.
        run_properties.insert(0, rfonts)
    for attr in ("ascii", "hAnsi", "cs"):
        rfonts.set(_w_tag(attr), font)


def _set_paragraph_alignment(properties: ET.Element, alignment: str) -> None:
    word_value = "both" if alignment == "justify" else alignment
    if not word_value:
        return
    jc = properties.find(_w_tag("jc"))
    if jc is None:
        jc = ET.Element(_w_tag("jc"))
        # CT_PPr order: jc is part of EG_PPrBase, so it must precede rPr, sectPr
        # and pPrChange. Insert before the first of those if present, else append.
        properties.insert(_paragraph_property_insert_index(properties), jc)
    jc.set(_w_tag("val"), word_value)


def _paragraph_property_insert_index(properties: ET.Element) -> int:
    trailing = {_w_tag("rPr"), _w_tag("sectPr"), _w_tag("pPrChange")}
    for index, child in enumerate(list(properties)):
        if child.tag in trailing:
            return index
    return len(properties)


def _set_paragraph_run_default_font(properties: ET.Element, font: str) -> None:
    if not font:
        return
    run_properties = properties.find(_w_tag("rPr"))
    if run_properties is None:
        run_properties = ET.Element(_w_tag("rPr"))
        # rPr precedes sectPr and pPrChange in CT_PPr; place it accordingly.
        trailing = {_w_tag("sectPr"), _w_tag("pPrChange")}
        insert_index = next(
            (index for index, child in enumerate(list(properties)) if child.tag in trailing),
            len(properties),
        )
        properties.insert(insert_index, run_properties)
    rfonts = run_properties.find(_w_tag("rFonts"))
    if rfonts is None:
        rfonts = ET.Element(_w_tag("rFonts"))
        run_properties.insert(0, rfonts)
    for attr in ("ascii", "hAnsi", "cs"):
        rfonts.set(_w_tag(attr), font)


def _set_revision_attrs(element: ET.Element, revision_id: int) -> None:
    for token in _revision_attrs(revision_id).split(" "):
        name, _, value = token.partition("=")
        local_name = name.split(":", 1)[1] if ":" in name else name
        element.set(_w_tag(local_name), value.strip('"'))


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
