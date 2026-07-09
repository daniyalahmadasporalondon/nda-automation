"""Admin backfill: heal PDF matters whose STORED extraction is glyph-garbled.

WHY: PDF pages rendered one glyph per positioned text operation (signature-block
overlays, some DOCX->PDF converters) used to extract as stacked one-character
paragraphs ("C" / "E" / "O") and scrambled space-joined fragments
("M o r w a n d L i m i t e d o") — see ``pdf_text._chunks_are_glyph_fragmented``
for the extractor-side fix. Matters imported BEFORE that fix still carry the
garbled text in their stored ``extracted_text``. The original document bytes are
retained on disk (``matter_store.UPLOADS_DIR``), so those matters can be healed
by re-running the SAME extraction seam ingest uses (``ingestion_service.
extract_document``) over the original bytes and swapping the stored text.

CONTRACT (each deliberate):

* DETECTION NEVER MUTATES. ``matter_garble_assessment`` is read-only; mutation
  happens only in an execute run, only after re-extraction produced coherent,
  different text, and only through ``matter_store.update_matter_extracted_text``
  with an optimistic-concurrency guard on the exact garbled text assessed.
* PDF-SOURCE ONLY. DOCX imports never went through the broken geometry grouping,
  so a DOCX matter is never a candidate no matter what its text looks like.
* STALENESS, NOT REPAIR. ``review_result`` / redlines / reviewer decisions are
  never touched; the existing staleness contract
  (``routes/matters._matter_review_text_changed`` comparing the matter's
  ``extracted_text`` to the review's ``extracted_text`` snapshot) flags the
  stored review as ``matter_text_changed`` by itself. NO AI CALLS anywhere in
  this module and no review is ever enqueued (review-storm history).
* RESOURCE-GUARDED. Serial (no thread fan-out), capped by ``limit`` per
  invocation; re-extraction reuses the ingest path's existing caps
  (``pdf_text.MAX_PDF_PAGES`` / byte / character guards) and skips the PyMuPDF
  visual profile (``include_visual_profile=False`` — the same cheap variant the
  Gmail poll uses; paragraphs are byte-identical and the profile is recomputed
  on demand). No temp copies of documents and no backup blobs of old blocks —
  the retained original bytes ARE the recovery source.

DETECTION FINGERPRINT — tuned against the pre-fix output of the parent branch's
glyph-fragmented signature fixture (tests/test_pdf_text.py,
``make_pdf_glyph_fragmented_signature_page`` extracted with the demotion
disabled), which is exactly:

    'M o r w a n d L i m i t e d o'                 <- "exploded" paragraph
    'S i n e d : _ _ _ _ _ _ _ _ _ _ _ _ _ B r ...' <- "exploded" paragraph
    'C'                                              <- shard paragraph  \\
    'E'                                              <- shard paragraph   > run of 3
    'O'                                              <- shard paragraph  /
    'L u u e r a d c G n'                            <- "exploded" paragraph
    'A u h o r i s e i g n a o r y N a e ...'        <- "exploded" paragraph
    'D a t e'                                        <- 4 lone tokens (below run min)

Two signals, each individually too weak (a numbered list can produce a short run
of standalone one-char clause-number paragraphs; a real letterhead can space out
ONE heading like "C O N F I D E N T I A L"), so the verdict requires either a
corroborated pair or an overwhelming count of one:

* shard run   — consecutive stored paragraphs of <= GARBLE_SHARD_MAX_CHARS chars
  (the stacked 'C'/'E'/'O' shape; the fixture's run is 3).
* exploded    — a paragraph containing >= GARBLE_EXPLODED_TOKEN_RUN_MIN
  consecutive single-character whitespace-separated tokens (the space-joined
  per-glyph shape; mirrors ``pdf_text._GLYPH_FRAGMENT_RUN_MIN``).

garbled = exploded_count >= 2  OR  (exploded_count >= 1 AND shard_run >= 3)
          OR shard_run >= 6.
"""

from __future__ import annotations

import logging
from typing import Any

from . import matter_store, telemetry
from .review_result_contract import extracted_text_from_paragraphs

LOGGER = logging.getLogger(__name__)

# A stored paragraph this short is a potential glyph shard ('C', 'E', 'O').
GARBLE_SHARD_MAX_CHARS = 2
# Consecutive shard paragraphs needed to corroborate an exploded paragraph
# (the fixture's 'C'/'E'/'O' run is exactly 3).
GARBLE_SHARD_RUN_CORROBORATING = 3
# Consecutive shard paragraphs that are conclusive on their own. Deliberately
# high: a legitimate numbered list can yield a few standalone one/two-char
# clause-number paragraphs, but never six in a row.
GARBLE_SHARD_RUN_ALONE = 6
# Consecutive single-character TOKENS inside one paragraph marking it "exploded"
# ('M o r w a n d ...'). Mirrors pdf_text._GLYPH_FRAGMENT_RUN_MIN: per-glyph
# writers emit long lone-glyph runs, real prose never does, and a single spaced
# letterhead heading ("C O N F I D E N T I A L") is why ONE exploded paragraph
# alone is not conclusive.
GARBLE_EXPLODED_TOKEN_RUN_MIN = 6
# Exploded paragraphs that are conclusive without shard corroboration: two
# independently spaced-out headings are conceivable only as garble.
GARBLE_EXPLODED_ALONE_MIN = 2

# Per-invocation processing cap (dry-run assessment is cheap; the cap bounds the
# CPU-bound pypdf re-extraction of an execute run so a huge store cannot peg the
# single-process server — re-run to resume, exactly like the PDF->DOCX backfill).
GARBLE_BACKFILL_DEFAULT_LIMIT = 50
GARBLE_BACKFILL_MAX_LIMIT = 200


def stored_paragraph_blocks(extracted_text: object) -> list[str]:
    """The stored paragraph texts, recovered from the canonical serialization.

    ``matter["extracted_text"]`` is written by ``extracted_text_from_paragraphs``
    (a "\\n\\n" join of the extracted paragraph texts), so splitting on the same
    separator recovers the stored blocks exactly.
    """
    return [block.strip() for block in str(extracted_text or "").split("\n\n") if block.strip()]


def _paragraph_is_exploded(block: str) -> bool:
    """True when ``block`` carries a run of >= GARBLE_EXPLODED_TOKEN_RUN_MIN
    consecutive single-character tokens — the space-joined per-glyph shape."""
    run = 0
    for token in block.split():
        if len(token) == 1:
            run += 1
            if run >= GARBLE_EXPLODED_TOKEN_RUN_MIN:
                return True
        else:
            run = 0
    return False


def garble_fingerprint(blocks: list[str]) -> dict[str, Any]:
    """Read-only garble fingerprint over stored paragraph blocks (never mutates)."""
    shard_count = 0
    longest_shard_run = 0
    run = 0
    exploded_count = 0
    for block in blocks:
        if len(block) <= GARBLE_SHARD_MAX_CHARS:
            shard_count += 1
            run += 1
            longest_shard_run = max(longest_shard_run, run)
        else:
            run = 0
            if _paragraph_is_exploded(block):
                exploded_count += 1
    garbled = (
        exploded_count >= GARBLE_EXPLODED_ALONE_MIN
        or (exploded_count >= 1 and longest_shard_run >= GARBLE_SHARD_RUN_CORROBORATING)
        or longest_shard_run >= GARBLE_SHARD_RUN_ALONE
    )
    return {
        "paragraphs": len(blocks),
        "shard_count": shard_count,
        "longest_shard_run": longest_shard_run,
        "exploded_count": exploded_count,
        "garbled": garbled,
    }


def _matter_has_pdf_filename(matter: dict[str, Any]) -> bool:
    """PDF-source check keyed on the filename, the SAME signal ``extract_document``
    routes on — so a candidate here is guaranteed to take the PDF extraction path
    when its bytes are re-extracted. (DOCX imports never went through the broken
    geometry grouping and are never candidates.)"""
    for key in ("stored_filename", "source_filename"):
        if str(matter.get(key) or "").lower().endswith(".pdf"):
            return True
    return False


def matter_garble_assessment(matter: dict[str, Any]) -> dict[str, Any]:
    """READ-ONLY per-matter garble assessment. Detection alone never mutates.

    ``candidate`` is True only for a PDF-source matter whose stored text carries
    the garble fingerprint; ``skip_reason`` explains every non-candidate.
    """
    blocks = stored_paragraph_blocks(matter.get("extracted_text"))
    fingerprint = garble_fingerprint(blocks)
    entry: dict[str, Any] = {
        "id": str(matter.get("id") or ""),
        "document": str(matter.get("source_filename") or matter.get("stored_filename") or ""),
        "owner_user_id": str(matter.get("owner_user_id") or ""),
        "fingerprint": fingerprint,
        "candidate": False,
        "skip_reason": "",
    }
    # Report-only visibility: a retro-converted matter re-keys the (garbled)
    # pypdf paragraphs into working_docx_paragraphs. Healing extracted_text does
    # not rewrite those; the next on-demand review self-corrects (its alignment
    # guard rejects paragraphs that no longer match the healed text and retries
    # text-only), so we surface the flag rather than touching artifact machinery.
    working = matter.get("working_docx_paragraphs")
    if isinstance(working, list) and working:
        working_blocks = stored_paragraph_blocks(extracted_text_from_paragraphs(
            [p for p in working if isinstance(p, dict) and "text" in p]
        ))
        entry["working_docx_paragraphs_garbled"] = bool(
            garble_fingerprint(working_blocks)["garbled"]
        )
    if not str(matter.get("id") or "").strip():
        entry["skip_reason"] = "missing_matter_id"
        return entry
    if not _matter_has_pdf_filename(matter):
        entry["skip_reason"] = "not_pdf_source"
        return entry
    if not blocks:
        entry["skip_reason"] = "no_extracted_text"
        return entry
    if not fingerprint["garbled"]:
        entry["skip_reason"] = "not_garbled"
        return entry
    entry["candidate"] = True
    return entry


def _heal_matter(matter: dict[str, Any], entry: dict[str, Any]) -> None:
    """Re-extract ONE matter through the fixed extractor and persist the healed
    text. Mutates ``entry`` (action/error fields) only; every failure path is
    caught by the caller's per-matter guard so one matter never aborts the run.
    """
    # Local import: keeps this module import-light (mirrors routes/admin.py's
    # local ingestion_service imports) and avoids a module-load cycle.
    from .ingestion_service import extract_document  # noqa: PLC0415

    matter_id = str(matter.get("id") or "")
    old_text = str(matter.get("extracted_text") or "")

    document_bytes = matter_store.get_source_document_bytes(matter)
    if not document_bytes:
        # FAIL-SOFT: the original upload is gone (pruned/wiped). Report + skip.
        entry["action"] = "skipped_missing_bytes"
        return

    filename = str(matter.get("stored_filename") or matter.get("source_filename") or "")
    # The SAME extraction seam ingest uses. include_visual_profile=False is the
    # documented cheap variant (Gmail-poll path): paragraphs are byte-identical
    # and the visual profile is recomputed on demand for the source preview.
    # extract_document -> extract_pdf_document enforces the existing per-doc caps
    # (MAX_PDF_PAGES, byte + extracted-character ceilings), so no extra cap is
    # layered here. Deterministic pypdf work only — never an AI call.
    _document_type, paragraphs, _quality = extract_document(
        filename, document_bytes, include_visual_profile=False
    )
    new_text = extracted_text_from_paragraphs(paragraphs)

    if new_text == old_text:
        entry["action"] = "unchanged"
        return
    if garble_fingerprint(stored_paragraph_blocks(new_text))["garbled"]:
        # The fixed extractor did not produce coherent text for this document
        # (a different degradation class). Swapping garble for garble is churn
        # with no benefit — report it for a human instead. NO write.
        entry["action"] = "still_garbled"
        return

    updated = matter_store.update_matter_extracted_text(
        matter_id,
        new_text,
        # Optimistic-concurrency guard: only replace the exact garbled text this
        # run assessed; anything else raced us and must not be clobbered.
        expected_extracted_text=old_text,
        owner_user_id="",
    )
    if updated is None:
        entry["action"] = "write_conflict"
        return
    entry["action"] = "healed"
    entry["new_paragraphs"] = len(paragraphs)
    telemetry.increment("garble_backfill_matters_healed")


def run_garble_backfill(*, dry_run: bool = True, limit: int = GARBLE_BACKFILL_DEFAULT_LIMIT) -> dict[str, Any]:
    """Scan the whole store for garble-fingerprinted PDF matters; heal up to
    ``limit`` of them when ``dry_run`` is False. SERIAL — no thread fan-out.

    Dry-run is detection-only: it reads matter records (no document bytes, no
    re-extraction, NO writes of any kind) and reports what an execute run would
    process. Execute re-reads each selected matter fresh and re-asserts the
    fingerprint immediately before healing, so a matter healed by a concurrent
    run (or edited meanwhile) is skipped, and processes matters one at a time,
    collecting per-matter failures into the report instead of aborting.
    """
    limit = max(1, min(int(limit), GARBLE_BACKFILL_MAX_LIMIT))
    matters = matter_store.list_matters("")
    entries: list[dict[str, Any]] = []
    garbled_matched = 0
    for matter in matters:
        assessment = matter_garble_assessment(matter)
        if not assessment["candidate"]:
            continue
        garbled_matched += 1
        if len(entries) >= limit:
            continue
        assessment["action"] = "would_reextract"
        entries.append(assessment)

    report: dict[str, Any] = {
        "dry_run": bool(dry_run),
        "scanned": len(matters),
        "garbled_matched": garbled_matched,
        "selected": len(entries),
        "limit": limit,
        "healed": 0,
        "unchanged": 0,
        "still_garbled": 0,
        "skipped_missing_bytes": 0,
        "write_conflicts": 0,
        "no_longer_garbled": 0,
        "failed": 0,
        "matters": entries,
        "errors": [],
    }
    if dry_run:
        return report

    for entry in entries:
        matter_id = entry["id"]
        try:
            # Re-read fresh + re-assert the fingerprint right before mutating —
            # detection alone never mutates, and a matter that stopped being
            # garbled (concurrent heal/edit) is skipped, never rewritten.
            fresh = matter_store.get_matter(matter_id, owner_user_id="")
            if not isinstance(fresh, dict) or not matter_garble_assessment(fresh)["candidate"]:
                entry["action"] = "no_longer_garbled"
                report["no_longer_garbled"] += 1
                continue
            _heal_matter(fresh, entry)
        except Exception as error:  # noqa: BLE001 - atomic per matter: collect, continue.
            LOGGER.warning(
                "Garble backfill: healing matter %s failed; continuing", matter_id, exc_info=True
            )
            entry["action"] = "failed"
            entry["error"] = f"{type(error).__name__}: {error}"
            report["failed"] += 1
            report["errors"].append({"id": matter_id, "error": entry["error"]})
            continue
        action = str(entry.get("action") or "")
        if action == "healed":
            report["healed"] += 1
        elif action == "unchanged":
            report["unchanged"] += 1
        elif action == "still_garbled":
            report["still_garbled"] += 1
        elif action == "skipped_missing_bytes":
            report["skipped_missing_bytes"] += 1
        elif action == "write_conflict":
            report["write_conflicts"] += 1
    return report
