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

from .inline_diff import diff_text_char_operations, diff_text_operations
from .docx_xml import _clone_element, _escape_attr, _escape_xml, _w_tag, _word_paragraph_from_xml


def _source_tracked_replace_paragraph(
    source_paragraph: ET.Element,
    original: str,
    replacement: str,
    first_revision_id: int,
) -> Tuple[ET.Element, int]:
    tracked_paragraph_xml, next_revision_id = _tracked_replace_paragraph(original, replacement, first_revision_id)
    return _merge_source_paragraph_properties(source_paragraph, _word_paragraph_from_xml(tracked_paragraph_xml)), next_revision_id

def _source_tracked_replace_paragraph_char(
    source_paragraph: ET.Element,
    original: str,
    replacement: str,
    first_revision_id: int,
) -> Tuple[ET.Element, int]:
    """Char-level mirror of :func:`_source_tracked_replace_paragraph` for free-form
    manual edits. Builds the tracked <w:p> via :func:`_tracked_replace_paragraph_char`
    (single-character diff, no inter-token spacing) and merges the source paragraph's
    pPr/run formatting in the same way, so character-level redlines still inherit the
    source paragraph's appearance."""
    tracked_paragraph_xml, next_revision_id = _tracked_replace_paragraph_char(original, replacement, first_revision_id)
    return _merge_source_paragraph_properties(source_paragraph, _word_paragraph_from_xml(tracked_paragraph_xml)), next_revision_id

def _source_tracked_replace_paragraph_runs(
    source_paragraph: ET.Element,
    original: str,
    replacement_runs: List[dict],
    first_revision_id: int,
) -> Tuple[ET.Element, int]:
    """Whole-paragraph tracked replace that re-emits the inserted text as FORMATTED
    runs carried by ``replacement_runs`` (an array of ``{text, bold?, italic?, font?,
    size?}``), rather than as plain ``<w:t>``.

    Used when a text-edited paragraph's run model is attached to the replace redline so
    the CLEAN export keeps the paragraph's bold/italic/font/size. The whole original is
    tracked-deleted and the new formatted runs are tracked-inserted -- a minimal char
    diff is unnecessary because the tracked view is flattened away on accept; only the
    final clean runs matter. Mirrors :func:`_source_tracked_replace_paragraph`'s merge
    of the source paragraph's pPr/run formatting onto the result."""
    tracked_paragraph_xml, next_revision_id = _tracked_replace_paragraph_runs(
        original, replacement_runs, first_revision_id
    )
    return (
        _merge_source_paragraph_properties(source_paragraph, _word_paragraph_from_xml(tracked_paragraph_xml)),
        next_revision_id,
    )

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
        elif prop == "size":
            _set_paragraph_run_default_size(new_properties, op.get("to"))

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
        and op.get("property") in ("bold", "italic", "font", "size", "underline", "strike", "color", "highlight")
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


def _run_offset_text(run: ET.Element) -> str:
    """The run's text in the frontend's offset space: a mirror of
    ``docx_text._run_text`` -- ``<w:t>`` text plus "\\t" for ``<w:tab>`` and "\\n" for
    ``<w:br>``/``<w:cr>``. The run-format offsets index into this space, so the
    splitter must measure runs the same way."""
    parts: List[str] = []
    for node in run.iter():
        if node.tag in {_w_tag("t"), _w_tag("delText")} and node.text:
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
        elif prop == "size":
            _set_run_font_size(new_rpr, op.get("to"))
        elif prop == "underline":
            _set_run_underline(new_rpr, bool(op.get("to")))
        elif prop == "strike":
            _set_run_toggle(new_rpr, "strike", bool(op.get("to")))
        elif prop == "color":
            _set_run_color(new_rpr, str(op.get("to") or ""))
        elif prop == "highlight":
            _set_run_highlight(new_rpr, str(op.get("to") or ""))

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
            _insert_run_property_child(run_properties, ET.Element(_w_tag(tag)))
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
        _insert_run_property_child(run_properties, rfonts)
    for attr in ("ascii", "hAnsi", "cs"):
        rfonts.set(_w_tag(attr), font)


def _size_half_points(size: object) -> int | None:
    """Word stores run sizes in HALF-points (12pt -> 24). Returns None for a
    falsy/invalid size so the caller emits nothing (clearing is not tracked)."""
    try:
        points = float(size)
    except (TypeError, ValueError):
        return None
    if points <= 0:
        return None
    return int(round(points * 2))


# CT_RPr children that must FOLLOW <w:sz>/<w:szCs> in the schema sequence; a fresh
# size element is inserted before the first of these so the rPr stays valid even
# when the source run already carries later-ordered props (lang, u, highlight, ...).
_RPR_TAGS_AFTER_SIZE = (
    "szCs", "highlight", "u", "effect", "bdr", "shd", "fitText", "vertAlign",
    "rtl", "cs", "em", "lang", "eastAsianLayout", "specVanish", "oMath", "rPrChange",
)


def _set_size_on_rpr(run_properties: ET.Element, half_points: int) -> None:
    """Set <w:sz>/<w:szCs> (val in half-points), updating in place when present and
    otherwise inserting them in CT_RPr order: sz before highlight/u/lang/..., szCs
    immediately after sz."""
    after = {_w_tag(tag) for tag in _RPR_TAGS_AFTER_SIZE}
    sz = run_properties.find(_w_tag("sz"))
    if sz is None:
        index = next(
            (i for i, child in enumerate(list(run_properties)) if child.tag in after),
            len(run_properties),
        )
        sz = ET.Element(_w_tag("sz"))
        run_properties.insert(index, sz)
    sz.set(_w_tag("val"), str(half_points))
    szcs = run_properties.find(_w_tag("szCs"))
    if szcs is None:
        szcs = ET.Element(_w_tag("szCs"))
        run_properties.insert(list(run_properties).index(sz) + 1, szcs)
    szcs.set(_w_tag("val"), str(half_points))


def _insert_run_property_child(run_properties: ET.Element, child: ET.Element) -> None:
    target_index = _RPR_CHILD_ORDER_INDEX.get(child.tag.split("}", 1)[-1], len(_RPR_CHILD_ORDER))
    for index, existing in enumerate(list(run_properties)):
        existing_index = _RPR_CHILD_ORDER_INDEX.get(existing.tag.split("}", 1)[-1], len(_RPR_CHILD_ORDER))
        if existing_index > target_index:
            run_properties.insert(index, child)
            return
    run_properties.append(child)


def _set_run_font_size(run_properties: ET.Element, size: object) -> None:
    """Set the run's point size as `<w:sz>`/`<w:szCs>` (half-points)."""
    half_points = _size_half_points(size)
    if half_points is None:
        return
    _set_size_on_rpr(run_properties, half_points)


def _set_run_underline(run_properties: ET.Element, enabled: bool) -> None:
    existing = run_properties.find(_w_tag("u"))
    if enabled:
        if existing is None:
            existing = ET.Element(_w_tag("u"))
            _insert_run_property_child(run_properties, existing)
        existing.set(_w_tag("val"), "single")
        return
    for node in list(run_properties.findall(_w_tag("u"))):
        run_properties.remove(node)


def _set_run_color(run_properties: ET.Element, color: str) -> None:
    value = str(color or "").strip().lstrip("#").upper()
    if len(value) != 6 or any(character not in "0123456789ABCDEF" for character in value):
        return
    color_element = run_properties.find(_w_tag("color"))
    if color_element is None:
        color_element = ET.Element(_w_tag("color"))
        _insert_run_property_child(run_properties, color_element)
    color_element.set(_w_tag("val"), value)


def _set_run_highlight(run_properties: ET.Element, highlight: str) -> None:
    value = str(highlight or "").strip().lower()
    if not value:
        return
    highlight_element = run_properties.find(_w_tag("highlight"))
    if highlight_element is None:
        highlight_element = ET.Element(_w_tag("highlight"))
        _insert_run_property_child(run_properties, highlight_element)
    highlight_element.set(_w_tag("val"), value)


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


def _set_paragraph_run_default_size(properties: ET.Element, size: object) -> None:
    """Paragraph-mark run default size: `<w:pPr><w:rPr><w:sz>/<w:szCs></w:rPr>`
    (val in half-points). Mirrors _set_paragraph_run_default_font's rPr placement."""
    half_points = _size_half_points(size)
    if half_points is None:
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
    _set_size_on_rpr(run_properties, half_points)


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
    # Carry the source paragraph's run formatting onto retained/deleted tracked
    # runs by mapping the emitted text back through the original run spans. This
    # avoids the old first-run flattening where a mixed-format paragraph made
    # every touched run inherit whichever rPr happened to appear first.
    _apply_source_run_properties(merged, source_paragraph)
    return merged

def _apply_source_run_properties(paragraph: ET.Element, source_paragraph: ET.Element) -> None:
    source_spans = _source_run_property_spans(source_paragraph)
    if not source_spans:
        return
    source_offset = 0

    def visit(parent: ET.Element, *, source_backed: bool) -> None:
        nonlocal source_offset
        children = list(parent)
        for child in children:
            if child.tag == _w_tag("ins"):
                visit(child, source_backed=False)
                continue
            if child.tag == _w_tag("del"):
                visit(child, source_backed=True)
                continue
            if child.tag == _w_tag("r"):
                run_text = _run_offset_text(child)
                if not run_text:
                    continue
                if not source_backed:
                    if child.find(_w_tag("rPr")) is None:
                        run_properties = _source_run_properties_at_offset(source_spans, source_offset)
                        if run_properties is not None:
                            child.insert(0, _clone_element(run_properties))
                    continue
                if child.find(_w_tag("rPr")) is not None or _run_has_flow_content(child):
                    source_offset += len(run_text)
                    continue
                replacements = _split_run_by_source_properties(child, source_spans, source_offset)
                source_offset += len(run_text)
                if replacements:
                    position = list(parent).index(child)
                    parent.remove(child)
                    for offset, replacement in enumerate(replacements):
                        parent.insert(position + offset, replacement)
                continue

            visit(child, source_backed=source_backed)

    visit(paragraph, source_backed=True)


def _source_run_property_spans(source_paragraph: ET.Element) -> list[tuple[int, int, ET.Element | None]]:
    spans: list[tuple[int, int, ET.Element | None]] = []
    offset = 0
    for run in source_paragraph.iter(_w_tag("r")):
        text = _run_offset_text(run)
        if not text:
            continue
        start = offset
        offset += len(text)
        spans.append((start, offset, run.find(_w_tag("rPr"))))
    return spans


def _source_run_properties_at_offset(
    source_spans: list[tuple[int, int, ET.Element | None]],
    offset: int,
) -> ET.Element | None:
    if not source_spans:
        return None
    for start, end, run_properties in source_spans:
        if start <= offset < end:
            return run_properties
    return source_spans[-1][2]


def _split_run_by_source_properties(
    run: ET.Element,
    source_spans: list[tuple[int, int, ET.Element | None]],
    source_offset: int,
) -> list[ET.Element]:
    text_node = _single_run_text_node(run)
    if text_node is None or not text_node.text:
        return []
    run_text = text_node.text
    text_tag = text_node.tag
    replacements: list[ET.Element] = []
    local_offset = 0
    while local_offset < len(run_text):
        absolute_offset = source_offset + local_offset
        span_end = source_offset + len(run_text)
        run_properties = _source_run_properties_at_offset(source_spans, absolute_offset)
        for start, end, candidate_properties in source_spans:
            if start <= absolute_offset < end:
                span_end = min(source_offset + len(run_text), end)
                run_properties = candidate_properties
                break
        if span_end <= absolute_offset:
            span_end = absolute_offset + 1
        next_local_offset = span_end - source_offset
        segment_text = run_text[local_offset:next_local_offset]
        segment = ET.Element(_w_tag("r"))
        if run_properties is not None:
            segment.append(_clone_element(run_properties))
        segment_text_node = ET.SubElement(segment, text_tag)
        if text_node.get("{http://www.w3.org/XML/1998/namespace}space") == "preserve":
            segment_text_node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        segment_text_node.text = segment_text
        replacements.append(segment)
        local_offset = next_local_offset
    return replacements


def _single_run_text_node(run: ET.Element) -> ET.Element | None:
    text_nodes = [node for node in list(run) if node.tag in {_w_tag("t"), _w_tag("delText")}]
    return text_nodes[0] if len(text_nodes) == 1 else None

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

def _tracked_replace_paragraph_char(original: str, replacement: str, first_revision_id: int) -> Tuple[str, int]:
    """Char-level tracked replace for free-form manual edits.

    Mirrors :func:`_tracked_replace_paragraph` (same multi-line guard, same
    per-tracked-run revision-id increment) BUT iterates the single-character diff
    and batches consecutive same-type ops by PLAIN CONCATENATION -- no
    ``_needs_inline_space`` prefix. The char tokens already carry their own
    whitespace; the word-spacing heuristic would put a spurious space between
    adjacent letters (e.g. inside "colour"), so it must not run here. This is the
    backend counterpart of the frontend ``renderVerbatimDiffOperations`` path."""
    if "\n" in str(original) or "\n" in str(replacement):
        return (
            f"<w:p>{_tracked_delete(str(original), first_revision_id)}{_tracked_insert(str(replacement), first_revision_id + 1)}</w:p>",
            first_revision_id + 2,
        )

    runs: List[str] = []
    revision_id = first_revision_id
    current_type = ""
    current_parts: List[str] = []

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

    for operation_type, token in diff_text_char_operations(original, replacement):
        if operation_type != current_type:
            flush_current()
            current_type = operation_type
        # Verbatim batching: the char token carries its own whitespace, so it is
        # appended with no inter-token prefix (unlike the token-level path).
        current_parts.append(token)

    flush_current()
    return f"<w:p>{''.join(runs)}</w:p>", revision_id

def _tracked_replace_paragraph_runs(
    original: str,
    replacement_runs: List[dict],
    first_revision_id: int,
) -> Tuple[str, int]:
    """Build a whole-paragraph tracked replace: a single ``<w:del>`` of the whole
    ``original`` followed by a single ``<w:ins>`` carrying the new FORMATTED runs.

    The replacement runs each become a ``<w:r>`` with an ``<w:rPr>`` built from the
    run-model toggles/font/size via the shared ET helpers (``_set_run_toggle`` /
    ``_set_run_font`` / ``_set_run_font_size``), so the inserted text keeps its
    formatting once the tracked view is flattened on accept. Two revision ids are
    consumed (one for the delete, one for the insert)."""
    delete_xml = _tracked_delete(str(original), first_revision_id)
    insert_xml = _tracked_insert_formatted_runs(replacement_runs, first_revision_id + 1)
    return f"<w:p>{delete_xml}{insert_xml}</w:p>", first_revision_id + 2


def _tracked_insert_formatted_runs(replacement_runs: List[dict], revision_id: int) -> str:
    runs = "".join(_formatted_run(run) for run in replacement_runs if isinstance(run, dict))
    return f"<w:ins {_revision_attrs(revision_id)}>{runs}</w:ins>"


def _formatted_run(run_model: dict) -> str:
    """A single ``<w:r>`` carrying ``run_model['text']`` with the run-model's
    formatting applied as an ``<w:rPr>``.

    The text may contain newlines: each is emitted as a ``<w:br/>`` between ``<w:t>``
    segments (matching :func:`_run`). The rPr is assembled by the same ET helpers the
    tracked-format path uses (``_set_run_font``/``_set_run_toggle``/``_set_run_font_size``)
    so toggle/font/size ordering stays schema-valid, then serialised back to a
    ``w:``-prefixed string for the string-based tracked-insert path."""
    run_properties = ET.Element(_w_tag("rPr"))
    font = str(run_model.get("font") or "")
    if font:
        _set_run_font(run_properties, font)
    if run_model.get("bold"):
        _set_run_toggle(run_properties, "b", True)
    if run_model.get("italic"):
        _set_run_toggle(run_properties, "i", True)
    if run_model.get("underline"):
        _set_run_underline(run_properties, True)
    if run_model.get("strike"):
        _set_run_toggle(run_properties, "strike", True)
    color = str(run_model.get("color") or "")
    if color:
        _set_run_color(run_properties, color)
    highlight = str(run_model.get("highlight") or "")
    if highlight:
        _set_run_highlight(run_properties, highlight)
    size = run_model.get("size")
    if size:
        _set_run_font_size(run_properties, size)

    rpr_xml = _run_properties_xml(run_properties)

    text = str(run_model.get("text") or "")
    parts: List[str] = []
    for index, line in enumerate(text.split("\n")):
        if index:
            parts.append("<w:br/>")
        parts.append(f'<w:t xml:space="preserve">{_escape_xml(line)}</w:t>')
    return f"<w:r>{rpr_xml}{''.join(parts)}</w:r>"


# CT_RPr child order (subset we emit). The ET helpers insert in this canonical
# sequence, and the string serializer re-sorts to the same order so the emitted
# rPr stays schema-valid regardless of operation order.
_RPR_CHILD_ORDER = (
    "rFonts",
    "b",
    "bCs",
    "i",
    "iCs",
    "strike",
    "dstrike",
    "color",
    "sz",
    "szCs",
    "highlight",
    "u",
    "rPrChange",
)
_RPR_CHILD_ORDER_INDEX = {tag: index for index, tag in enumerate(_RPR_CHILD_ORDER)}


def _run_properties_xml(run_properties: ET.Element) -> str:
    """Serialise a ``<w:rPr>`` element (built with the ET helpers, in Clark notation)
    to a ``w:``-prefixed XML string suitable for embedding in the string-based
    tracked-paragraph builders. Returns ``""`` for an empty rPr so a run with no
    formatting carries no properties element.

    ``ET.tostring`` would emit ``ns0:`` prefixes / redundant ``xmlns`` declarations, so
    we serialise by hand: each child becomes ``<w:tag .../>`` with its ``w:``-named
    attributes (the only attribute these helpers set is ``w:val``). Children are sorted
    into CT_RPr schema order first."""
    children = list(run_properties)
    if not children:
        return ""
    children.sort(
        key=lambda child: _RPR_CHILD_ORDER_INDEX.get(child.tag.split("}", 1)[-1], len(_RPR_CHILD_ORDER))
    )
    parts: List[str] = []
    for child in children:
        local_name = child.tag.split("}", 1)[-1]
        attrs = "".join(
            f' w:{key.split("}", 1)[-1]}="{_escape_attr(value)}"'
            for key, value in child.attrib.items()
        )
        parts.append(f"<w:{local_name}{attrs}/>")
    return f"<w:rPr>{''.join(parts)}</w:rPr>"


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
