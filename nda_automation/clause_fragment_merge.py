"""Merge continuation fragments ONLY for the AI reviewer's view of a DOCX.

A DOCX clause authored across several ``<w:p>`` paragraphs reaches the reviewer as
separate fragments (``p14``, ``p15`` ...). The model then grades half-sentences and
some limbs get no verdict at all. This module reconstructs whole clauses for the
packet the model reads, and exposes the fragment groups so a verdict on the merged
clause can be mapped back onto EVERY constituent fragment id (so the middle limbs
are highlighted, not orphaned).

THE ASYMMETRY (governs every threshold here)
    * A false NON-merge (leaving two fragments apart) == the status quo, always safe.
    * A false MERGE (joining two genuinely distinct clauses) is CATASTROPHIC: a
      required clause can vanish from the reconstructed text, a missing-clause FAIL
      goes invisible, a bad NDA passes.
So every rule below is a VETO: a merge happens only when NO veto fires. When any
signal is unsure, the fragments stay apart.

PACKET-ONLY. Stored paragraphs, paragraph identity, the review result's
``paragraphs``, rendering, and the outbound redline are all built from the ORIGINAL,
unmerged fragments and stay byte-unchanged. This module never mutates its inputs; it
returns a NEW merged-view list plus a member -> group-of-member-ids map.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from .contract_structure import _looks_like_short_heading, _looks_like_uppercase_heading
from .heading_detection import parse_leading_number
from .review_document import Paragraph

# A merged group is capped so a pathological run of vetoes-that-do-not-fire can never
# swallow an unbounded stretch of the document into one packet paragraph. Real clauses
# split across <w:p> are a handful of limbs; the trailing-punctuation veto already stops
# a group at the first completed sentence, so this cap is a belt-and-braces backstop.
MAX_MERGE_FRAGMENTS = 8

# Metadata keys copied from the group HEAD onto the merged model-view record. Only the
# provenance/section-lookup keys the packet builder reads are carried; heading/numbering
# metadata is deliberately NOT carried (a merged clause body is not a heading).
_HEAD_METADATA_KEYS = ("source_index", "source_part", "source_kind")

# source_kind values a paragraph may NOT participate in a merge under.
_NON_BODY_SOURCE_KINDS = {"table_cell", "supplemental"}

# Trailing chars stripped before we look for a sentence-terminal mark, so ``end."`` and
# ``end.)`` still read as terminal.
_TRAILING_WRAPPERS = "\"'“”‘’)]}"
_TERMINAL_PUNCTUATION = {".", "!", "?", ";", ":"}
_TRAILING_CONJUNCTIONS = {"and", "or"}


def merge_continuation_fragments(
    paragraphs: Sequence[Paragraph],
) -> tuple[list[Paragraph], dict[str, list[str]]]:
    """Build the merged model-view of ``paragraphs`` plus the fragment-group map.

    Returns ``(model_paragraphs, groups)`` where:

    * ``model_paragraphs`` is a NEW list. Each genuine continuation run collapses to a
      single record carrying the HEAD fragment's id/index/provenance and the joined
      text of every fragment in the run; every other paragraph passes through
      unchanged (same object identity is not guaranteed, but its fields are copied
      verbatim).
    * ``groups`` maps EACH original fragment id to the ordered list of fragment ids in
      its group (a length-1 list for an unmerged paragraph). Callers expand a verdict
      that cites the head id onto every id in the group.

    Deterministic and pure: the same input always yields the same output, so the
    packet builder and the result builder re-derive an identical merge.
    """
    originals = [paragraph for paragraph in paragraphs if isinstance(paragraph, Mapping)]
    runs: list[list[Paragraph]] = []
    for paragraph in originals:
        if (
            runs
            and len(runs[-1]) < MAX_MERGE_FRAGMENTS
            and _can_continue(runs[-1], paragraph)
        ):
            runs[-1].append(paragraph)
        else:
            runs.append([paragraph])

    model_paragraphs: list[Paragraph] = []
    groups: dict[str, list[str]] = {}
    for run in runs:
        member_ids = [str(member.get("id") or "") for member in run]
        for member_id in member_ids:
            if member_id:
                groups[member_id] = list(member_ids)
        if len(run) == 1:
            model_paragraphs.append(run[0])
        else:
            model_paragraphs.append(_merge_run(run))
    return model_paragraphs, groups


def _can_continue(run: list[Paragraph], nxt: Paragraph) -> bool:
    """Whether ``nxt`` continues the clause built so far in ``run`` (no veto fires)."""
    head = run[0]
    tail = run[-1]
    head_text = _text(head)
    tail_text = _text(tail)
    nxt_text = _text(nxt)
    if not tail_text or not nxt_text:
        return False

    # --- Structural vetoes: a table cell, supplemental part (headers/footers/notes),
    # or the document Title never merges, on either side. ---
    for paragraph in (head, tail, nxt):
        if _is_non_body(paragraph) or _is_document_title(paragraph):
            return False

    # --- The group HEAD must be a clause body, not a heading. Merging a heading into
    # its following body is explicitly forbidden ("Governing Law" || "This Agreement
    # shall be governed ..."). Text heading-shapes catch numbered and un-numbered
    # headings alike; a structural heading style/outline level is an independent veto.
    if _looks_like_heading_text(head_text) or _has_heading_metadata(head):
        return False

    # --- The tail must be an INCOMPLETE sentence: if it already ends in terminal
    # punctuation the clause is complete and the next paragraph starts something new. ---
    if _ends_sentence(tail_text):
        return False

    # --- nxt must not itself begin a new unit. ---
    #  (a) its own numbering/heading metadata (an auto-numbered next clause, a bullet
    #      list item, a styled heading);
    if _has_heading_metadata(nxt):
        return False
    #  (b) a literal leading clause/list marker in the run text ("(a)", "1.", "(i)");
    if parse_leading_number(nxt_text) is not None:
        return False
    #  (c) a capitalised heading-like short line ("Position/Title", "Authorised
    #      Signatory").
    if _looks_like_heading_text(nxt_text):
        return False

    # --- Conjunction discipline. A tail ending in a trailing "and"/"or" is the one
    # shape that is genuinely ambiguous: it can be a mid-sentence enumeration
    # ("... 4 and 5) or" -> "unauthorised disclosure ...") OR the close of one recital
    # limb before the next ("... to the other Party; and" -> "The Disclosing Party
    # ..."). Merge ONLY when nxt continues the sentence in lower case; a capitalised
    # nxt after a trailing conjunction is a new independent clause -> do not merge. ---
    if _ends_with_conjunction(tail_text) and not _starts_lowercase(nxt_text):
        return False

    return True


def _merge_run(run: list[Paragraph]) -> Paragraph:
    """Collapse a continuation run into one model-view record under the head's id.

    Whitespace is normalised to single spaces between fragments because the AI grounds
    the model's returned quotes with the same whitespace-collapsing normalisation, so
    the join separator is immaterial to grounding; a single space reads as continuous
    prose for a mid-sentence <w:p> split.
    """
    head = run[0]
    tail = run[-1]
    merged: Paragraph = {
        "id": str(head.get("id") or ""),
        "index": head.get("index"),
        "text": " ".join(_text(member) for member in run),
    }
    if isinstance(head.get("start"), int):
        merged["start"] = head["start"]
    if isinstance(tail.get("end"), int):
        merged["end"] = tail["end"]
    for key in _HEAD_METADATA_KEYS:
        if key in head:
            merged[key] = head[key]
    # Provenance for debugging/consumers that want to know a record was reconstructed.
    merged["merged_fragment_ids"] = [str(member.get("id") or "") for member in run]
    return merged


def expand_group_members(
    paragraph_ids: Sequence[str],
    groups: Mapping[str, Sequence[str]] | None,
) -> list[str]:
    """Expand cited fragment ids onto their full merge groups, in document order.

    For the first cited member of a group the WHOLE group is emitted (in document
    order); later cited members of the same group are already present. Ids with no
    group entry pass through unchanged. Order is otherwise preserved so a clause's
    matched paragraphs stay in the order they were cited/appear.
    """
    if not groups:
        return [str(paragraph_id) for paragraph_id in paragraph_ids]
    seen: set[str] = set()
    expanded: list[str] = []
    for paragraph_id in paragraph_ids:
        key = str(paragraph_id)
        group = groups.get(key) or [key]
        for member_id in group:
            member_key = str(member_id)
            if member_key and member_key not in seen:
                seen.add(member_key)
                expanded.append(member_key)
    return expanded


def _text(paragraph: Paragraph) -> str:
    return str(paragraph.get("text") or "").strip()


def _is_non_body(paragraph: Paragraph) -> bool:
    if "table" in paragraph:
        return True
    return str(paragraph.get("source_kind") or "").strip() in _NON_BODY_SOURCE_KINDS


def _is_document_title(paragraph: Paragraph) -> bool:
    for key in ("style_id", "style_name"):
        if str(paragraph.get(key) or "").strip().casefold() == "title":
            return True
    return False


def _has_heading_metadata(paragraph: Paragraph) -> bool:
    if paragraph.get("numbering"):
        return True
    if paragraph.get("heading_level") is not None:
        return True
    if paragraph.get("outline_level") is not None:
        return True
    for key in ("style_id", "style_name"):
        if str(paragraph.get(key) or "").strip().casefold().startswith("heading"):
            return True
    return False


def _looks_like_heading_text(text: str) -> bool:
    return _looks_like_short_heading(text) or _looks_like_uppercase_heading(text)


def _ends_sentence(text: str) -> bool:
    stripped = text.rstrip()
    while stripped and stripped[-1] in _TRAILING_WRAPPERS:
        stripped = stripped[:-1].rstrip()
    return bool(stripped) and stripped[-1] in _TERMINAL_PUNCTUATION


def _ends_with_conjunction(text: str) -> bool:
    tokens = re.findall(r"[A-Za-z]+", text)
    return bool(tokens) and tokens[-1].casefold() in _TRAILING_CONJUNCTIONS


def _starts_lowercase(text: str) -> bool:
    for char in text:
        if char.isalpha():
            return char.islower()
    return False
