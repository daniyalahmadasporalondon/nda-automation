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
* EXECUTED/APPROVED MATTERS ARE NEVER MUTATED. An approved or executed matter's
  stored record stays byte-identical at all times (``matter_is_executed_or_
  approved`` — the product's own executed contract + approve-transition triad);
  a garble-detected one is LISTED in the report as ``excluded_executed`` so the
  owner knows it exists, but execute never re-extracts or writes it.
* STALENESS, NOT REPAIR. ``review_result`` / redlines / reviewer decisions are
  never touched; the existing staleness contract
  (``routes/matters._matter_review_text_changed`` comparing the matter's
  ``extracted_text`` to the review's ``extracted_text`` snapshot) flags the
  stored review as ``matter_text_changed`` by itself. NO AI CALLS anywhere in
  this module and no review is ever enqueued (review-storm history).
* RESOURCE-GUARDED. Serial (no fan-out), capped by ``limit`` per invocation;
  the EXECUTE run happens on ONE background daemon thread
  (``start_garble_backfill_async``, mirroring the PDF->DOCX backfill) so its
  minutes of GIL-heavy pypdf CPU never sit on the web worker's request thread,
  while the detection-only dry-run (record reads, no byte reads) stays a cheap
  synchronous response; re-extraction reuses the ingest path's existing caps
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

import contextlib
import logging
import threading
from datetime import datetime, timezone
from typing import Any

from . import matter_store, telemetry, workflow
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


def matter_is_executed_or_approved(matter: dict[str, Any]) -> bool:
    """The exclusion predicate: an approved OR executed matter is never mutated.

    Product-owner rule: a matter whose NDA is already approved/executed keeps its
    stored record byte-identical at all times — even a garble-detected one is
    only REPORTED (``excluded_executed``), never re-extracted or written.

    Reuses the product's own classifications rather than inventing one:

    * EXECUTED — ``workflow.is_matter_executed`` (workflow.py), the shared
      executed contract (``executed`` / ``executed_at`` / the executed phase
      marker) that ``lifecycle_signed.mark_matter_executed`` stamps
      (``executed=True`` / ``status="fully_signed"`` / ``executed_at``) and the
      board / DocuSign-completion sync read. A BARE ``status == "fully_signed"``
      (legacy/partial stamp) also counts: the product elsewhere treats that
      status alone as signed (``drive_integration``'s signed filter and
      ``corpus_index._SIGNED_TRUE_STATUSES`` both key on
      ``workflow.STATUS_FULLY_SIGNED``).
    * APPROVED — the canonical approve transition's own triad:
      ``matter_store.record_matter_approval`` stamps ``status="approved"`` +
      ``approver`` + ``approved_at``; ``status == "approved"`` / ``approved_at``
      are exactly the approval signals ``docusign_workflow.
      matter_cleared_for_signature`` and ``matter_store.
      _matter_was_cleared_for_signature`` key on. Any one of the three excludes
      (inclusive: a partial/legacy stamp still counts as approved).

    Deliberately NOT excluded: ``human_reviewed`` alone (the board's
    "mark reviewed" — a human sign-off on the review, not an approval) and a
    clean auto-review — neither means an approved/executed document exists, and
    both are exactly the matters whose garbled text still needs healing before
    any approval bakes it into a reviewed artifact.
    """
    if not isinstance(matter, dict):
        return False
    if workflow.is_matter_executed(matter):
        return True
    status = str(matter.get("status") or "").strip().lower()
    if status in ("approved", workflow.STATUS_FULLY_SIGNED):
        return True
    return bool(matter.get("approved_at") or matter.get("approver"))


def working_docx_paragraphs_garbled(matter: dict[str, Any]) -> bool | None:
    """Garble verdict over the matter's persisted ``working_docx_paragraphs``.

    ``None`` when the matter has no working paragraphs (never retro-converted,
    or a native DOCX); otherwise the same read-only fingerprint the stored-text
    detection uses. This is BOTH the report flag (``working_docx_paragraphs_
    garbled``) and the trigger/verdict for the post-heal working-DOCX rebuild.
    """
    working = matter.get("working_docx_paragraphs")
    if not isinstance(working, list) or not working:
        return None
    working_blocks = stored_paragraph_blocks(extracted_text_from_paragraphs(
        [p for p in working if isinstance(p, dict) and "text" in p]
    ))
    return bool(garble_fingerprint(working_blocks)["garbled"])


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
    # A retro-converted matter re-keyed the (garbled) pypdf paragraphs into
    # working_docx_paragraphs. Healing extracted_text alone does not rewrite
    # those — and the retro conversion is idempotent (working DOCX present →
    # no-op) so nothing else ever would: reviews would permanently degrade to
    # text-only anchoring (alignment of the garbled working text against the
    # healed source raises ParagraphAlignmentError forever). The flag is
    # therefore BOTH surfaced here (read-only) and, in an execute run, the
    # trigger for the post-heal working-DOCX rebuild (_rebuild_working_docx).
    working_garbled = working_docx_paragraphs_garbled(matter)
    if working_garbled is not None:
        entry["working_docx_paragraphs_garbled"] = working_garbled
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


def _shard_rejoin_context(shard_rejoin: bool):
    """Force the DEFAULT-OFF PDF shard-fragment reflow ON for the wrapped
    re-extraction when ``shard_rejoin`` is True; otherwise a no-op that leaves the
    extractor's own env-flag default in force (byte-identical re-extraction).

    Local import keeps this module import-light and avoids a load cycle."""
    if not shard_rejoin:
        return contextlib.nullcontext()
    from . import pdf_text  # noqa: PLC0415

    return pdf_text.shard_rejoin_forced(True)


def _reextract_paragraphs(
    filename: str, document_bytes: bytes, *, shard_rejoin: bool
) -> list[dict[str, Any]]:
    """Re-extract paragraphs through the SAME seam ingest uses, optionally forcing
    the shard-fragment reflow on for this pass only."""
    # Local import: keeps this module import-light (mirrors routes/admin.py's
    # local ingestion_service imports) and avoids a module-load cycle.
    from .ingestion_service import extract_document  # noqa: PLC0415

    with _shard_rejoin_context(shard_rejoin):
        # include_visual_profile=False is the documented cheap variant (Gmail-poll
        # path): paragraphs are byte-identical and the visual profile is recomputed
        # on demand. extract_document -> extract_pdf_document enforces the existing
        # per-doc caps (MAX_PDF_PAGES, byte + extracted-character ceilings), so no
        # extra cap is layered here. Deterministic pypdf work only — never an AI call.
        _document_type, paragraphs, _quality = extract_document(
            filename, document_bytes, include_visual_profile=False
        )
    return paragraphs


def _reflow_healed_text_is_readable(new_text: str) -> bool:
    """The shard-reflow READABILITY BACKSTOP, applied to a whole re-extracted
    document's text before it may be reported as a would-heal or PERSISTED.

    Two conditions, both required (bias to NOT persist — any doubt returns False):

    * NOT GARBLED by the fingerprint (the shard/exploded shape is gone), AND
    * NO IMPLAUSIBLE MEGAWORD — no spaceless alphanumeric run longer than
      ``pdf_text._SHARD_MAX_WORD_CHARS``. The per-page adoption gate already
      rejects a fused reflow, but a dropped inter-word space is invisible to the
      garble fingerprint, so this is an independent second line of defence at the
      persist boundary: a heal that welded two words together is refused, not
      written. Empty text is never readable.

    This is the concrete gate (b) the persist path is doubly-gated on."""
    from . import pdf_text  # noqa: PLC0415 - local, mirrors the other lazy imports.

    blocks = stored_paragraph_blocks(new_text)
    if not blocks:
        return False
    if garble_fingerprint(blocks)["garbled"]:
        return False
    return not pdf_text.text_has_implausible_megaword(new_text)


def _measure_matter(
    matter: dict[str, Any], entry: dict[str, Any], *, shard_rejoin: bool
) -> None:
    """MEASURE-ONLY re-extraction: report what an execute run WOULD do, writing
    NOTHING. Reads the original bytes and re-extracts (optionally with the
    shard-fragment reflow forced on), then records the resulting fingerprint and a
    would-heal verdict. Never calls ``update_matter_extracted_text`` and never
    rebuilds the working DOCX — this exists purely so an operator can size the
    shard-rejoin win on the real corpus BEFORE any mutation."""
    old_text = str(matter.get("extracted_text") or "")
    document_bytes = matter_store.get_source_document_bytes(matter)
    if not document_bytes:
        entry["action"] = "skipped_missing_bytes"
        return
    filename = str(matter.get("stored_filename") or matter.get("source_filename") or "")
    paragraphs = _reextract_paragraphs(filename, document_bytes, shard_rejoin=shard_rejoin)
    new_text = extracted_text_from_paragraphs(paragraphs)
    after = garble_fingerprint(stored_paragraph_blocks(new_text))
    entry["fingerprint_after"] = after
    entry["new_paragraphs"] = len(paragraphs)
    if new_text == old_text:
        entry["action"] = "measure_unchanged"
    elif _reflow_healed_text_is_readable(new_text):
        # would_heal is gated on the SAME readability backstop the persist path
        # enforces (not garbled AND no fused megaword), so the measurement is an
        # HONEST size of what a persisting run would actually write — not the old
        # over-estimate that counted a fused/garbled re-extraction as a heal.
        entry["action"] = "would_heal"
    else:
        entry["action"] = "measure_still_garbled"


def _heal_matter(
    matter: dict[str, Any], entry: dict[str, Any], *, shard_rejoin: bool = False
) -> None:
    """Re-extract ONE matter through the fixed extractor and PERSIST the healed
    text. Mutates ``entry`` (action/error fields) only; every failure path is
    caught by the caller's per-matter guard so one matter never aborts the run.

    DEFAULT reflow-free (``shard_rejoin=False``): the per-glyph heal re-extracts
    with the shard-fragment reflow OFF and gates on the garble fingerprint alone —
    byte-identical to the proven path.

    OPT-IN persist path (``shard_rejoin=True``, reachable ONLY via
    ``run_garble_backfill(persist_shard_rejoin=True)`` — see the structural guards
    there): re-extracts WITH the reflow on and gates the write on the stronger
    READABILITY BACKSTOP (``_reflow_healed_text_is_readable`` — not garbled AND no
    fused megaword), so a reflowed re-extraction is persisted only when it is
    genuinely reviewable. Bias to NOT persist: any doubt reports and skips.
    """
    matter_id = str(matter.get("id") or "")
    old_text = str(matter.get("extracted_text") or "")

    document_bytes = matter_store.get_source_document_bytes(matter)
    if not document_bytes:
        # FAIL-SOFT: the original upload is gone (pruned/wiped). Report + skip.
        entry["action"] = "skipped_missing_bytes"
        return

    filename = str(matter.get("stored_filename") or matter.get("source_filename") or "")
    paragraphs = _reextract_paragraphs(filename, document_bytes, shard_rejoin=shard_rejoin)
    new_text = extracted_text_from_paragraphs(paragraphs)

    if new_text == old_text:
        entry["action"] = "unchanged"
        return
    if shard_rejoin:
        # PERSIST gate (b): the reflowed re-extraction must clear the readability
        # backstop (not garbled AND no fused megaword) before it may be written.
        if not _reflow_healed_text_is_readable(new_text):
            entry["action"] = "reflow_unreadable"
            return
    elif garble_fingerprint(stored_paragraph_blocks(new_text))["garbled"]:
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
        # TOCTOU closer: the run loop's exclusion pre-check runs BEFORE the
        # seconds-long re-extraction above, and an approval/execution landing in
        # that window does NOT touch extracted_text (so the expected-text guard
        # alone would let the write through). Re-evaluating the exclusion on the
        # fresh record INSIDE the store lock — the same lock every approval /
        # executed writer serializes on — makes "approved/executed is never
        # written" airtight.
        reject_when=_reject_executed_or_approved,
        owner_user_id="",
    )
    if isinstance(updated, matter_store.RejectedTextUpdate):
        # Approved/executed landed between the pre-check and the write: vetoed
        # in-lock, nothing written. Reported distinctly from the scan-time
        # exclusion so the operator can see the race happened.
        entry["action"] = "excluded_executed_late"
        return
    if updated is None:
        entry["action"] = "write_conflict"
        return
    entry["action"] = "healed"
    entry["new_paragraphs"] = len(paragraphs)
    telemetry.increment("garble_backfill_matters_healed")
    # TEXT healed. If this matter's PERSISTED working-DOCX representation is
    # still garbled (the retro conversion re-keyed the old garbled pypdf
    # paragraphs and is idempotent — it would never rebuild them), force-rebuild
    # it from the same original bytes, reusing the healed paragraphs extracted
    # above. FAIL-SOFT by construction: the healed text write above is already
    # committed and is NEVER rolled back by a rebuild failure.
    _rebuild_working_docx(
        updated if isinstance(updated, dict) else matter,
        entry,
        document_bytes=document_bytes,
        healed_paragraphs=paragraphs,
    )


def _rebuild_working_docx(
    matter: dict[str, Any],
    entry: dict[str, Any],
    *,
    document_bytes: bytes,
    healed_paragraphs: list[dict[str, Any]],
) -> None:
    """Rebuild a healed matter's GARBLED working-DOCX representation from the
    original bytes (execute runs only — reached exclusively via ``_heal_matter``,
    so never on a dry-run and never on an excluded matter).

    Delegates to ``ingestion_service.rebuild_pdf_working_docx`` — the SAME
    reconstruction + persistence a fresh import uses (same pdf2docx engine with
    its RLIMIT/semaphore/page-cap guards, same storage fields + artifact
    provenance) minus only the idempotency no-op and the stored-review-paragraph
    preference. Passes the executed/approved predicate as the write-free
    pre-persist veto (a rebuild is a mutation; an approval landing during the
    reconstruction window must still win). Reports per matter:

    * ``docx_rebuild: "rebuilt"`` — the persisted working paragraphs are now
      coherent (verdict re-read from the store, not trusted from a return code);
    * ``docx_rebuild: "failed"`` — reconstruction failed / was vetoed / rolled
      back; the healed TEXT is kept and ``working_docx_paragraphs_garbled``
      stays True so the report still points at the matter.

    NEVER raises (a rebuild failure must not turn the heal into ``failed``) and
    makes NO AI calls — pdf2docx is deterministic local work.
    """
    if working_docx_paragraphs_garbled(matter) is not True:
        return  # No working DOCX, or a healthy one: rebuild not needed.
    matter_id = str(matter.get("id") or "")
    entry["working_docx_paragraphs_garbled"] = True
    # Local import mirrors _heal_matter's: keeps module import light, no cycle.
    from .ingestion_service import rebuild_pdf_working_docx  # noqa: PLC0415
    from .matter_repository import DiskMatterRepository  # noqa: PLC0415

    try:
        rebuild_pdf_working_docx(
            matter,
            repository=DiskMatterRepository(),
            owner_user_id="",
            document_bytes=document_bytes,
            extracted_paragraphs=healed_paragraphs,
            reject_when=_reject_executed_or_approved,
        )
        # Verdict from the STORE, not the return value: success is "the persisted
        # working paragraphs are no longer garbled" (the shared path is fail-open
        # and restores the old garbled paragraphs on a mid-way rollback).
        fresh = matter_store.get_matter(matter_id, owner_user_id="")
        rebuilt = (
            isinstance(fresh, dict) and working_docx_paragraphs_garbled(fresh) is False
        )
    except Exception:  # noqa: BLE001 - fail-soft: the heal must survive any rebuild error.
        LOGGER.warning(
            "Garble backfill: working-DOCX rebuild raised for matter %s; healed text kept",
            matter_id,
            exc_info=True,
        )
        rebuilt = False
    if rebuilt:
        entry["docx_rebuild"] = "rebuilt"
        entry["working_docx_paragraphs_garbled"] = False
        telemetry.increment("garble_backfill_working_docx_rebuilt")
    else:
        entry["docx_rebuild"] = "failed"
        telemetry.increment("garble_backfill_working_docx_rebuild_failed")


def _reject_executed_or_approved(matter: dict[str, Any]) -> str | None:
    """``reject_when`` adapter for the store writer's in-lock veto seam (and the
    rebuild path's write-free pre-persist veto)."""
    return "executed_or_approved" if matter_is_executed_or_approved(matter) else None


def run_garble_backfill(
    *,
    dry_run: bool = True,
    limit: int = GARBLE_BACKFILL_DEFAULT_LIMIT,
    status_run_id: str = "",
    shard_rejoin: bool = False,
    measure_only: bool = False,
    persist_shard_rejoin: bool = False,
) -> dict[str, Any]:
    """Scan the whole store for garble-fingerprinted PDF matters; heal up to
    ``limit`` of them when ``dry_run`` is False. SERIAL — no thread fan-out.

    Dry-run is detection-only: it reads matter records (no document bytes, no
    re-extraction, NO writes of any kind) and reports what an execute run would
    process. Execute re-reads each selected matter fresh and re-asserts the
    fingerprint immediately before healing, so a matter healed by a concurrent
    run (or edited meanwhile) is skipped, and processes matters one at a time,
    collecting per-matter failures into the report instead of aborting.

    ``shard_rejoin`` forces the DEFAULT-OFF PDF shard-fragment reflow on for every
    re-extraction in this run. ``measure_only`` (implies no mutation) re-extracts
    each selected candidate and reports a would-heal verdict WITHOUT writing — the
    hook that sizes the shard-rejoin win on the real corpus before any mutation.
    ``measure_only`` re-reads document bytes and runs pypdf, so — like the execute
    path — it must be driven off the request thread (via
    ``start_garble_backfill_async``); the plain synchronous dry-run
    (``measure_only=False``) stays record-reads-only and cheap.

    ``persist_shard_rejoin`` is the SEPARATE, explicit opt-in that lets a
    reflow-on re-extraction actually PERSIST (heal-for-real). It is distinct from
    the measurement flag by design and DOUBLY gated: (a) the caller must set it AND
    ``shard_rejoin`` (measurement flag alone can never write a reflow), and (b) each
    matter's reflowed re-extraction must clear the readability backstop in
    ``_heal_matter`` before it is written. Default False keeps the reflow
    measurement-only; a persisting run with ``shard_rejoin`` but WITHOUT this opt-in
    is still structurally refused below. ``measure_only``, ``shard_rejoin`` and the
    default execute path all stay unchanged: every extra flag defaults False.

    ``status_run_id`` (set only by ``start_garble_backfill_async``) publishes a
    per-matter progress snapshot for the GET status route; a synchronous dry-run
    never publishes, so it can't clobber the last execute run's status.
    """
    # STRUCTURAL WRITE GUARDS (not a route convention): the shard-fragment reflow is
    # measurement-only BY DEFAULT and may PERSIST only under the explicit, separate
    # ``persist_shard_rejoin`` opt-in — never the measurement flag alone.
    #
    #  * ``shard_rejoin`` on a PERSISTING pass (``measure_only`` False) without the
    #    persist opt-in is refused: measurement is the only sanctioned reflow write
    #    surface unless a caller deliberately opts in. (dry_run+measure_only is the
    #    sanctioned MEASURE combo.)
    #  * The persist opt-in itself must be paired with ``shard_rejoin`` (persisting a
    #    reflow makes no sense with the reflow off) and is mutually exclusive with
    #    ``measure_only`` (you cannot both measure and persist) and with ``dry_run``
    #    (persisting IS a mutation). Any inconsistent combination raises BEFORE the
    #    scan, so nothing is ever half-written.
    if shard_rejoin and not measure_only and not persist_shard_rejoin:
        raise ValueError(
            "shard_rejoin re-extraction is measurement-only (measure_only=True) "
            "unless persist_shard_rejoin=True is explicitly set; refusing to run a "
            "persisting backfill with shard_rejoin enabled."
        )
    if persist_shard_rejoin:
        if not shard_rejoin:
            raise ValueError("persist_shard_rejoin requires shard_rejoin=True.")
        if measure_only:
            raise ValueError("persist_shard_rejoin cannot combine with measure_only.")
        if dry_run:
            raise ValueError("persist_shard_rejoin is a persisting run; dry_run must be False.")
    limit = max(1, min(int(limit), GARBLE_BACKFILL_MAX_LIMIT))
    matters = matter_store.list_matters("")
    entries: list[dict[str, Any]] = []
    selected = 0
    excluded_executed = 0
    garbled_matched = 0
    for matter in matters:
        assessment = matter_garble_assessment(matter)
        if not assessment["candidate"]:
            continue
        garbled_matched += 1
        # EXECUTED/APPROVED EXCLUSION: still LISTED (the owner must know a signed/
        # approved matter carries garbled text) but never selected for healing —
        # its stored record stays byte-identical. Listed entries are capped at
        # ``limit`` like the heal selection so a huge store can't bloat the report.
        if matter_is_executed_or_approved(matter):
            if excluded_executed < limit:
                assessment["action"] = "excluded_executed"
                entries.append(assessment)
            excluded_executed += 1
            continue
        if selected >= limit:
            continue
        assessment["action"] = "would_reextract"
        entries.append(assessment)
        selected += 1

    report: dict[str, Any] = {
        "dry_run": bool(dry_run),
        "measure_only": bool(measure_only),
        "shard_rejoin": bool(shard_rejoin),
        "persist_shard_rejoin": bool(persist_shard_rejoin),
        "scanned": len(matters),
        "garbled_matched": garbled_matched,
        "selected": selected,
        "excluded_executed": excluded_executed,
        "limit": limit,
        "healed": 0,
        "unchanged": 0,
        "still_garbled": 0,
        "reflow_unreadable": 0,
        "skipped_missing_bytes": 0,
        "write_conflicts": 0,
        "no_longer_garbled": 0,
        "excluded_executed_late": 0,
        "docx_rebuilt": 0,
        "docx_rebuild_failed": 0,
        "failed": 0,
        # MEASURE-ONLY tallies (no mutation): what an execute run with the current
        # settings WOULD do, sized against the real corpus.
        "would_heal": 0,
        "measure_still_garbled": 0,
        "measure_unchanged": 0,
        "matters": entries,
        "errors": [],
    }
    # A plain synchronous dry-run is record-reads-only: return before touching any
    # document bytes. A measure-only pass DELIBERATELY re-extracts (still writing
    # nothing) and so continues into the loop below.
    if dry_run and not measure_only:
        return report

    processed = 0
    for entry in entries:
        if entry.get("action") == "excluded_executed":
            # Never re-extracted, never written — report-only presence.
            continue
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
            # Re-check the exclusion on the FRESH record: a matter approved or
            # executed after selection must not be written either.
            if matter_is_executed_or_approved(fresh):
                entry["action"] = "excluded_executed"
                report["excluded_executed"] += 1
                continue
            if measure_only:
                # NON-MUTATING: re-extract + report a would-heal verdict, no write.
                _measure_matter(fresh, entry, shard_rejoin=shard_rejoin)
            else:
                # PERSISTING path. Reflow-free by default; the reflow is threaded in
                # ONLY under the explicit persist opt-in (guarded above), and even
                # then _heal_matter gates the write on the readability backstop.
                _heal_matter(fresh, entry, shard_rejoin=persist_shard_rejoin)
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
        rebuild = str(entry.get("docx_rebuild") or "")
        if rebuild == "rebuilt":
            report["docx_rebuilt"] += 1
        elif rebuild == "failed":
            report["docx_rebuild_failed"] += 1
        if action == "healed":
            report["healed"] += 1
        elif action == "unchanged":
            report["unchanged"] += 1
        elif action == "still_garbled":
            report["still_garbled"] += 1
        elif action == "reflow_unreadable":
            report["reflow_unreadable"] += 1
        elif action == "skipped_missing_bytes":
            report["skipped_missing_bytes"] += 1
        elif action == "write_conflict":
            report["write_conflicts"] += 1
        elif action == "excluded_executed_late":
            report["excluded_executed_late"] += 1
        elif action == "would_heal":
            report["would_heal"] += 1
        elif action == "measure_still_garbled":
            report["measure_still_garbled"] += 1
        elif action == "measure_unchanged":
            report["measure_unchanged"] += 1
        processed += 1
        if status_run_id:
            _publish_status({
                "state": "running",
                "run_id": status_run_id,
                "processed": processed,
                "selected": selected,
                "healed": report["healed"],
                "failed": report["failed"],
            })
    return report


# --------------------------------------------------------------------------- #
# Background execute runner + status snapshot.
#
# Mirrors ingestion_service's PDF->DOCX backfill pattern exactly (run lock +
# daemon thread + best-effort status dict behind its own lock): the execute run
# is minutes of GIL-heavy pypdf CPU, so it must NEVER run on the single web
# worker's request thread (sync-CPU incident history). At most one run at a
# time; the HTTP trigger returns immediately and the GET status route serves
# the latest snapshot (final snapshot carries the full report).
# --------------------------------------------------------------------------- #
_RUN_LOCK = threading.Lock()
_RUNNING = False
_STATUS_LOCK = threading.Lock()
_LAST_STATUS: dict[str, Any] = {}


def _publish_status(status: dict[str, Any]) -> None:
    """Best-effort snapshot of the latest run for the GET status route."""
    try:
        with _STATUS_LOCK:
            _LAST_STATUS.clear()
            _LAST_STATUS.update(status)
    except Exception:  # pragma: no cover - status snapshot is best-effort
        pass


def garble_backfill_status() -> dict[str, Any]:
    """Snapshot of the most recent / in-flight execute run (cheap, no re-scan)."""
    with _STATUS_LOCK:
        return dict(_LAST_STATUS)


def start_garble_backfill_async(
    *,
    limit: int = GARBLE_BACKFILL_DEFAULT_LIMIT,
    shard_rejoin: bool = False,
    measure_only: bool = False,
    persist_shard_rejoin: bool = False,
) -> dict[str, Any]:
    """Start a background daemon-thread run; return immediately.

    Defaults to an EXECUTE run (mutating). ``measure_only=True`` runs the
    NON-MUTATING shard-rejoin measurement instead (re-extract + would-heal tally,
    no writes) — kept on the background thread because it re-reads bytes and runs
    pypdf, the same GIL-heavy CPU the execute path must keep off the request
    thread. ``shard_rejoin`` forces the shard-fragment reflow on for the run.

    ``persist_shard_rejoin=True`` (paired with ``shard_rejoin=True``) is the
    explicit opt-in that lets a reflow-on re-extraction actually PERSIST, gated per
    matter by the readability backstop in ``_heal_matter``; it stays a background
    run for the same GIL-heavy-CPU reason. No HTTP route exposes this opt-in yet —
    it is deliberately code-level only until the reflow is proven on the real
    corpus — so an operator drives a real heal through this seam, not the endpoint.
    ``run_garble_backfill`` structurally refuses every inconsistent combination.

    Returns ``{"started": bool, "run_id": str, "already_running": bool}``. At
    most one run at a time (it is serial by design): a second trigger while one
    is in flight reports the in-flight run instead of starting another.
    """
    global _RUNNING
    with _RUN_LOCK:
        if _RUNNING:
            with _STATUS_LOCK:
                run_id = str(_LAST_STATUS.get("run_id") or "")
            return {"started": False, "run_id": run_id, "already_running": True}
        _RUNNING = True
    if persist_shard_rejoin:
        prefix = "garble-persist"
    elif measure_only:
        prefix = "garble-measure"
    else:
        prefix = "garble-backfill"
    run_id = datetime.now(timezone.utc).strftime(f"{prefix}-%Y%m%dT%H%M%SZ")
    _publish_status({"state": "running", "run_id": run_id, "processed": 0})

    def _run() -> None:
        global _RUNNING
        try:
            report = run_garble_backfill(
                dry_run=measure_only,
                limit=limit,
                status_run_id=run_id,
                shard_rejoin=shard_rejoin,
                measure_only=measure_only,
                persist_shard_rejoin=persist_shard_rejoin,
            )
            _publish_status({
                "state": "done",
                "run_id": run_id,
                "processed": report["selected"],
                "selected": report["selected"],
                "healed": report["healed"],
                "would_heal": report["would_heal"],
                "failed": report["failed"],
                "report": report,
            })
        except Exception:  # pragma: no cover - run_garble_backfill is already fail-open per matter
            LOGGER.warning("Garble backfill thread crashed", exc_info=True)
            _publish_status({"state": "error", "run_id": run_id})
        finally:
            with _RUN_LOCK:
                _RUNNING = False

    thread = threading.Thread(target=_run, name=prefix, daemon=True)
    thread.start()
    return {"started": True, "run_id": run_id, "already_running": False}
