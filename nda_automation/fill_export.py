"""Inbound-NDA fill support for the DOCX export pipeline.

A *fill* replaces a blank in a reviewed document with a concrete value, in one
of two modes:

* ``mode="clean"`` — the value is substituted as REAL text, with NO tracked-change
  markup. This is the inbound analogue of the generator filling ``[SLOT] -> value``:
  the blank is simply gone and the value reads as part of the base document.
* ``mode="tracked"`` — the value appears as a Word tracked *insertion*, exactly
  like a reviewer's redline suggestion, so a counterparty can see and accept it.

Ordering contract: clean fills are baked into the BASE document first (so they
become part of the source text the redlines anchor against), then tracked
fills / manual redlines are applied on top as revisions. This module owns the
"what to fill"; ``docx_export`` owns the tracked-change XML and the source
paragraph model, and we reuse both rather than duplicating them.

Increment 1 targets DOCX-source exports. Clean fills on a non-DOCX source
(PDF / generated review report) are best-effort no-ops here (the caller never
threads a source ``document_root`` for those paths), so they never crash —
they simply do not apply. See ``apply_clean_fills_to_source_document`` /
``synthesize_tracked_fill_redlines`` for the two halves.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Dict, List

from .redline_actions import REDLINE_REPLACE_PARAGRAPH
from .docx_xml import _w_tag

# The fill record shape the frontend sends alongside redline edits / comments:
#   {"paragraph_id": "p9", "find": "____", "value": "...", "mode": "clean"|"tracked"}
# A "field" key may also be present for the UI; it is ignored server-side.
FILL_MODE_CLEAN = "clean"
FILL_MODE_TRACKED = "tracked"
_FILL_MODES = {FILL_MODE_CLEAN, FILL_MODE_TRACKED}

# A defensive cap so a malformed / hostile payload cannot make us iterate forever.
MAX_FILLS = 500


def clean_fills(fills: object) -> List[dict]:
    """Validate + sanitise the raw ``fills`` payload, dropping malformed records.

    Each kept record is ``{"paragraph_id": str, "find": str, "value": str,
    "mode": "clean"|"tracked"}``. A record is dropped when it is not a dict, has
    a non-string ``paragraph_id``/``find``/``value``, an empty ``paragraph_id`` or
    ``find`` (nothing to anchor / nothing to replace), or a ``mode`` outside the
    allowed set. ``value`` may be empty (a fill that clears a blank). Any extra
    keys (e.g. the UI's ``field``) are ignored.
    """

    if not isinstance(fills, list):
        return []

    cleaned: List[dict] = []
    for fill in fills[:MAX_FILLS]:
        if not isinstance(fill, dict):
            continue
        paragraph_id = fill.get("paragraph_id")
        find = fill.get("find")
        value = fill.get("value")
        mode = fill.get("mode")
        if not isinstance(paragraph_id, str) or not isinstance(find, str):
            continue
        if not isinstance(value, str):
            continue
        if not isinstance(mode, str) or mode not in _FILL_MODES:
            continue
        paragraph_id = paragraph_id.strip()
        if not paragraph_id or not find:
            continue
        cleaned.append({
            "paragraph_id": paragraph_id,
            "find": find,
            "value": value,
            "mode": mode,
        })
    return cleaned


def split_fills_by_mode(fills: List[dict]) -> tuple[List[dict], List[dict]]:
    """Partition cleaned fills into ``(clean_fills, tracked_fills)``."""

    clean_mode = [fill for fill in fills if fill.get("mode") == FILL_MODE_CLEAN]
    tracked_mode = [fill for fill in fills if fill.get("mode") == FILL_MODE_TRACKED]
    return clean_mode, tracked_mode


def apply_clean_fills_to_source_document(
    document_root: ET.Element,
    clean_mode_fills: List[dict],
    review_result: dict,
) -> int:
    """Bake clean fills into the SOURCE document as plain text (no tracked markup).

    Resolves each fill's paragraph using the SAME mapping the redline path uses
    (``paragraph_id`` -> review paragraph -> ``source_index``, falling back to a
    direct numeric ``paragraph_id``), then replaces ``find`` -> ``value`` across
    that physical ``<w:p>``'s ``<w:t>`` runs as ordinary text. No ``<w:ins>`` /
    ``<w:del>`` is ever produced, so an accepted-or-rejected reader sees only the
    filled value.

    The matched paragraph's text in ``review_result`` (the review paragraph's
    ``text`` and the document's ``extracted_text``) is updated the same way, so
    the post-fill text is what the downstream redline anchoring AND the export
    content-coverage gate measure against — otherwise the gate would compare the
    filled export body against the stale pre-fill expectation and reject it.

    Returns the number of fills actually applied (a fill whose paragraph or
    ``find`` text is not found is skipped, not an error).
    """

    if not clean_mode_fills:
        return 0

    source_paragraphs = _indexed_source_paragraphs(document_root)
    source_by_index = {paragraph.source_index: paragraph for paragraph in source_paragraphs}
    review_paragraphs = review_result.get("paragraphs")
    review_by_id = _review_paragraphs_by_id(review_paragraphs)

    applied = 0
    for fill in clean_mode_fills:
        source_index = _resolve_source_index(fill["paragraph_id"], review_by_id)
        if source_index is None:
            continue
        source_paragraph = source_by_index.get(source_index)
        if source_paragraph is None:
            continue
        if _replace_in_paragraph_runs(source_paragraph.paragraph, fill["find"], fill["value"]):
            applied += 1
            _apply_clean_fill_to_review_paragraph(review_by_id, fill)

    if applied:
        _rebuild_extracted_text(review_result)
    return applied


def synthesize_tracked_fill_redlines(
    tracked_mode_fills: List[dict],
    review_result: dict,
) -> List[dict]:
    """Turn tracked fills into ``replace_paragraph`` redline edits.

    A tracked fill must render as a Word tracked change, so the simplest correct
    representation is a replace-paragraph redline whose ``original_text`` is the
    paragraph's pre-fill text and whose ``replacement_text`` is the filled text:
    that flows through the existing tracked path (``_tracked_replace_paragraph``)
    and produces ``<w:ins>``/``<w:del>`` exactly like any other suggested edit.

    The synthesized redlines carry the paragraph's ``paragraph_id`` /
    ``paragraph_index`` / ``source_index`` so the redline anchoring resolves them
    to the right source ``<w:p>``. They are returned (not appended) so the caller
    controls precedence — these are merged ahead of the server redlines, like
    manual viewer edits.

    A fill whose paragraph is unknown, or whose ``find`` is absent from the
    paragraph text (nothing to replace), is skipped.
    """

    review_by_id = _review_paragraphs_by_id(review_result.get("paragraphs"))
    synthesized: List[dict] = []
    for fill in tracked_mode_fills:
        review_paragraph = review_by_id.get(fill["paragraph_id"])
        if not isinstance(review_paragraph, dict):
            continue
        original_text = str(review_paragraph.get("text") or "")
        if fill["find"] not in original_text:
            continue
        replacement_text = original_text.replace(fill["find"], fill["value"])
        if replacement_text == original_text:
            continue
        redline = {
            "id": f"fill-{fill['paragraph_id']}-{len(synthesized) + 1}",
            "clause_id": "inbound_fill",
            "status": "proposed",
            "action": REDLINE_REPLACE_PARAGRAPH,
            "action_label": "Fill blank",
            "paragraph_id": fill["paragraph_id"],
            "original_text": original_text,
            "replacement_text": replacement_text,
        }
        _copy_review_paragraph_indexes(review_paragraph, redline)
        synthesized.append(redline)
    return synthesized


def merge_fill_redlines(review_result: dict, fill_redlines: List[dict]) -> None:
    """Prepend synthesized tracked-fill redlines, replacing any server redline on
    the same paragraph (a fill on a paragraph supersedes a competing suggestion).

    Mirrors ``export_service.apply_manual_export_redlines`` so a tracked fill and
    a manual viewer edit follow the same precedence rule against server redlines.
    """

    if not fill_redlines:
        return
    fill_paragraph_ids = {str(redline.get("paragraph_id")) for redline in fill_redlines}
    existing = review_result.get("redline_edits", [])
    if not isinstance(existing, list):
        existing = []
    review_result["redline_edits"] = fill_redlines + [
        redline
        for redline in existing
        if not (isinstance(redline, dict) and str(redline.get("paragraph_id")) in fill_paragraph_ids)
    ]


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _indexed_source_paragraphs(document_root: ET.Element):
    # Deferred import: docx_export imports redline_xml which is heavy, and this
    # keeps fill_export importable without pulling the whole export stack at module
    # load. The function is the authoritative <w:p> ordinal model the redline path
    # also uses, so clean fills and redlines agree on source_index.
    from .docx_export import _indexed_source_paragraphs as indexed  # noqa: PLC0415

    return indexed(document_root)


def _review_paragraphs_by_id(review_paragraphs: object) -> Dict[str, dict]:
    if not isinstance(review_paragraphs, list):
        return {}
    return {
        str(paragraph.get("id")): paragraph
        for paragraph in review_paragraphs
        if isinstance(paragraph, dict) and paragraph.get("id")
    }


def _resolve_source_index(paragraph_id: str, review_by_id: Dict[str, dict]) -> int | None:
    """The physical source ``<w:p>`` ordinal a fill targets.

    Prefers the review paragraph's recorded ``source_index`` (the provenance the
    extractor stamped), then falls back to treating a bare numeric ``paragraph_id``
    as a source ordinal. A ``source_part`` paragraph (header/footer/footnote) is
    not addressable in the main document body, so it resolves to ``None`` and the
    fill is skipped rather than mis-anchored.
    """

    review_paragraph = review_by_id.get(paragraph_id)
    if isinstance(review_paragraph, dict):
        if str(review_paragraph.get("source_part") or "").strip():
            return None
        for key in ("source_index", "index"):
            value = review_paragraph.get(key)
            try:
                source_index = int(value)
            except (TypeError, ValueError):
                continue
            if source_index > 0:
                return source_index
        return None

    # No review paragraph: a bare numeric id is interpreted as a source ordinal.
    try:
        source_index = int(paragraph_id)
    except (TypeError, ValueError):
        return None
    return source_index if source_index > 0 else None


def _replace_in_paragraph_runs(paragraph: ET.Element, find: str, value: str) -> bool:
    """Replace ``find`` -> ``value`` across a paragraph's ``<w:t>`` runs as plain
    text. Returns True iff a replacement was made.

    Two passes so a blank that spans run boundaries (Word often splits a single
    logical placeholder across several runs) is still caught:

    1. Per-run: replace any occurrence wholly contained in one ``<w:t>``.
    2. Whole-paragraph: if the joined run text still contains ``find``, collapse
       the paragraph's text into its first ``<w:t>`` with the replacement applied
       and clear the rest — the generator's ``_set_paragraph_text`` strategy,
       which is acceptable because a filled blank is plain body text.

    No tracked-change element is created on either pass.
    """

    text_nodes = [node for node in paragraph.iter(_w_tag("t")) if node is not None]
    replaced = False
    for node in text_nodes:
        if node.text and find in node.text:
            node.text = node.text.replace(find, value)
            replaced = True
    if replaced:
        return True

    if not text_nodes:
        return False
    joined = "".join(node.text or "" for node in text_nodes)
    if find not in joined:
        return False
    text_nodes[0].text = joined.replace(find, value)
    for node in text_nodes[1:]:
        node.text = ""
    return True


def _apply_clean_fill_to_review_paragraph(review_by_id: Dict[str, dict], fill: dict) -> None:
    review_paragraph = review_by_id.get(fill["paragraph_id"])
    if not isinstance(review_paragraph, dict):
        return
    text = str(review_paragraph.get("text") or "")
    if fill["find"] in text:
        review_paragraph["text"] = text.replace(fill["find"], fill["value"])


def _rebuild_extracted_text(review_result: dict) -> None:
    """Re-derive ``extracted_text`` from the (now filled) review paragraph texts so
    the export content-coverage gate validates against the post-clean-fill source.

    The extractor joins paragraph texts with a blank line, so we mirror that
    exactly. Only rebuilt when there are review paragraphs to derive from; a
    direct-upload path without a paragraph list keeps its existing extracted text.
    """

    paragraphs = review_result.get("paragraphs")
    if not isinstance(paragraphs, list) or not paragraphs:
        return
    texts = [str(paragraph.get("text") or "") for paragraph in paragraphs if isinstance(paragraph, dict)]
    review_result["extracted_text"] = "\n\n".join(texts)


def _copy_review_paragraph_indexes(review_paragraph: dict, redline: dict) -> None:
    """Carry the review paragraph's ordinals onto a synthesized redline so the
    redline anchoring resolves it to the same source ``<w:p>`` a clean fill would."""

    for key in ("paragraph_index", "source_index"):
        try:
            redline[key] = int(review_paragraph.get(key))
        except (TypeError, ValueError):
            continue
    # 'index' is the review-paragraph ordinal the anchoring uses as paragraph_index
    # when no explicit paragraph_index is present.
    if "paragraph_index" not in redline:
        try:
            redline["paragraph_index"] = int(review_paragraph.get("index"))
        except (TypeError, ValueError):
            pass
    source_part = str(review_paragraph.get("source_part") or "").strip()
    if source_part:
        redline["source_part"] = source_part
