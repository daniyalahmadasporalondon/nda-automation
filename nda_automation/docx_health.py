from __future__ import annotations

from collections import Counter
import posixpath
import re
import xml.etree.ElementTree as ET
from io import BytesIO
from typing import Dict, List, Set, Tuple
from zipfile import BadZipFile, ZipFile

from .docx_xml import UnsafeDocxXmlError, parse_docx_xml
from .docx_text import DocxExtractionError, validate_docx_archive, validate_docx_bytes_before_open
from .inline_diff import diff_text_operations, tokenize_inline_diff
from .redline_actions import (
    REDLINE_DELETE_PARAGRAPH,
    REDLINE_INSERT_AFTER_PARAGRAPH,
    REDLINE_REPLACE_PARAGRAPH,
)
from .redline_edit_contract import confident_text_match, is_freeform_manual_replace_edit
from .redline_xml import redline_replace_uses_whole_text_markup

# Tracked redlines only add text (insertions as w:t, deletions retained as
# w:delText), so the exported visible text is always >= the source text. An
# export that covers far less than the source has dropped/empty content.
EXPORT_CONTENT_COVERAGE_RATIO = 0.5

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
SETTINGS_RELATIONSHIP_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings"
OFFICE_DOCUMENT_RELATIONSHIP_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
STYLE_RELATIONSHIP_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"
DOCUMENT_CONTENT_TYPE = f"{DOCX_MIME}.main+xml"
RELATIONSHIPS_CONTENT_TYPE = "application/vnd.openxmlformats-package.relationships+xml"
SETTINGS_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"
STYLES_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"


def validate_docx_open_health(docx_bytes: bytes, require_styles: bool = False) -> List[str]:
    errors: List[str] = []
    required_parts = {
        "[Content_Types].xml",
        "_rels/.rels",
        "word/document.xml",
        "word/_rels/document.xml.rels",
        "word/settings.xml",
    }
    if require_styles:
        required_parts.add("word/styles.xml")

    try:
        validate_docx_bytes_before_open(docx_bytes)
        with ZipFile(BytesIO(docx_bytes)) as archive:
            try:
                validate_docx_archive(archive)
            except DocxExtractionError as exc:
                errors.append(str(exc))
                return errors

            corrupt_part = archive.testzip()
            if corrupt_part:
                errors.append(f"ZIP integrity check failed at {corrupt_part}.")
            archive_names = archive.namelist()
            duplicate_names = sorted(name for name, count in Counter(archive_names).items() if count > 1)
            if duplicate_names:
                errors.append(f"DOCX package contains duplicate entries: {', '.join(duplicate_names)}.")
            names = set(archive_names)
            missing_parts = sorted(required_parts - names)
            if missing_parts:
                errors.append(f"Missing DOCX parts: {', '.join(missing_parts)}.")
                return errors

            try:
                defaults, overrides = _docx_content_types(archive)
            except (KeyError, ET.ParseError, UnsafeDocxXmlError) as exc:
                errors.append(f"Content types are unreadable: {exc}.")
                return errors

            if defaults.get("rels") != RELATIONSHIPS_CONTENT_TYPE:
                errors.append("Missing or invalid .rels content type default.")
            if defaults.get("xml") != "application/xml":
                errors.append("Missing or invalid .xml content type default.")
            if overrides.get("/word/document.xml") != DOCUMENT_CONTENT_TYPE:
                errors.append("Missing or invalid document.xml content type override.")
            if overrides.get("/word/settings.xml") != SETTINGS_CONTENT_TYPE:
                errors.append("Missing or invalid settings.xml content type override.")
            if "word/styles.xml" in names and overrides.get("/word/styles.xml") != STYLES_CONTENT_TYPE:
                errors.append("Missing or invalid styles.xml content type override.")

            try:
                package_relationships = _relationship_targets(archive, "_rels/.rels")
                document_relationships = _relationship_targets(archive, "word/_rels/document.xml.rels")
            except (KeyError, ET.ParseError, UnsafeDocxXmlError) as exc:
                errors.append(f"Relationships are unreadable: {exc}.")
                return errors

            office_document_targets = [
                _resolve_relationship_target("_rels/.rels", relationship["Target"])
                for relationship in package_relationships
                if relationship.get("Type") == OFFICE_DOCUMENT_RELATIONSHIP_TYPE and "Target" in relationship
            ]
            if office_document_targets != ["word/document.xml"]:
                errors.append("Package relationships do not resolve to word/document.xml.")

            document_targets_by_type = {
                relationship["Type"]: _resolve_relationship_target("word/_rels/document.xml.rels", relationship["Target"])
                for relationship in document_relationships
                if relationship.get("TargetMode") != "External" and "Target" in relationship and "Type" in relationship
            }
            for relationship_type, target in document_targets_by_type.items():
                if target not in names:
                    errors.append(f"Relationship target is missing: {relationship_type} -> {target}.")
            if document_targets_by_type.get(SETTINGS_RELATIONSHIP_TYPE) != "word/settings.xml":
                errors.append("Document relationships do not resolve settings.xml.")
            if require_styles and document_targets_by_type.get(STYLE_RELATIONSHIP_TYPE) != "word/styles.xml":
                errors.append("Document relationships do not resolve styles.xml.")
            if not require_styles and STYLE_RELATIONSHIP_TYPE in document_targets_by_type and document_targets_by_type[STYLE_RELATIONSHIP_TYPE] != "word/styles.xml":
                errors.append("Document styles relationship does not resolve styles.xml.")

            try:
                settings_root = parse_docx_xml(archive.read("word/settings.xml"), part_name="word/settings.xml")
            except (KeyError, ET.ParseError, UnsafeDocxXmlError) as exc:
                errors.append(f"settings.xml is unreadable: {exc}.")
                return errors
            if settings_root.find(_w_tag("trackRevisions")) is None:
                errors.append("settings.xml does not enable Track Changes.")

            try:
                document_root = parse_docx_xml(archive.read("word/document.xml"), part_name="word/document.xml")
            except (KeyError, ET.ParseError, UnsafeDocxXmlError) as exc:
                errors.append(f"document.xml is unreadable: {exc}.")
                return errors
            body = document_root.find(_w_tag("body"))
            if body is None:
                errors.append("document.xml is missing w:body.")
            elif body.find(_w_tag("sectPr")) is None:
                errors.append("document.xml is missing section properties.")
            if document_root.findall(f".//{_w_tag('pPr')}/{_w_tag('rPr')}/{_w_tag('ins')}"):
                errors.append("document.xml contains insertion revision markup inside paragraph properties.")
            if document_root.findall(f".//{_w_tag('pPr')}/{_w_tag('rPr')}/{_w_tag('del')}"):
                errors.append("document.xml contains deletion revision markup inside paragraph properties.")
    except DocxExtractionError as exc:
        errors.append(str(exc))
    except BadZipFile:
        errors.append("Export is not a readable DOCX zip package.")
    return errors


def verify_export_content_coverage(
    docx_bytes: bytes,
    source_text: str,
    *,
    expected_redline_edits: object = None,
    clean_fills: object = None,
    source_docx: bytes | None = None,
) -> List[str]:
    """Content gate the structural health check misses: an empty body or a
    redline that drops, reorders, duplicates, or misplaces source content.
    Returns error strings (counts only, never source text, to avoid leaking NDA content).

    ``clean_fills`` are inbound-NDA clean-mode fills baked into the base document
    text (e.g. ``____`` -> ``Acme Ltd``). Their accepted-view substitution is applied
    to the expected source paragraphs so a filled export is not flagged as diverging.
    Tracked-mode fills need no handling here: they arrive as synthesized
    replace-paragraph entries in ``expected_redline_edits``."""
    source_normalized = _normalize_export_text(source_text)
    if not source_normalized:
        return []
    export_paragraphs = _export_revision_paragraphs(docx_bytes)
    export_normalized = _normalize_export_text(" ".join(record["all"] for record in export_paragraphs))
    if not export_normalized:
        return ["Exported document body contains no text."]
    if len(export_normalized) < len(source_normalized) * EXPORT_CONTENT_COVERAGE_RATIO:
        return [
            f"Exported text covers only {len(export_normalized)} of {len(source_normalized)} "
            "source characters; the redline may have dropped source content."
        ]
    source_paragraphs = _apply_clean_fills_to_source_paragraphs(
        _source_paragraphs_from_text(source_text), clean_fills
    )
    if source_docx:
        structural_errors = _verify_structural_counts(docx_bytes, source_docx)
        if structural_errors:
            return structural_errors
    if source_paragraphs:
        expected_accepted_paragraphs, expected_errors = _expected_accepted_source_paragraphs(
            source_paragraphs,
            expected_redline_edits,
        )
        if expected_errors:
            return expected_errors
        accepted_paragraphs = [record["accepted"] for record in export_paragraphs if record["accepted"]]
        if accepted_paragraphs != expected_accepted_paragraphs:
            return [
                "Exported accepted-change paragraph sequence does not match the expected source/redline "
                f"sequence ({len(accepted_paragraphs)} paragraph(s); expected {len(expected_accepted_paragraphs)}). "
                "The redline may have misplaced, duplicated, or dropped source content."
            ]
    return []


def verify_pdf_reconstruction_redline_coverage(
    docx_bytes: bytes,
    expected_redline_edits: object,
) -> List[str]:
    """Coverage gate for the PDF-reconstruction reviewed export.

    The strong DOCX sequence gate (``verify_export_content_coverage``) cannot run on
    a PDF-source export: the reviewed Word doc is REBUILT by a layout engine
    (pdf2docx) from a different paragraph/whitespace model than the PDF text
    extractor, so an exact accepted-paragraph-sequence comparison false-positives on
    every normal reconstruction. That gate is therefore switched off for PDF -- which
    historically left the PDF path with NO post-render coverage check, so a reviewer
    redline that anchored but never landed in the output bytes shipped SILENTLY
    (silent data loss on an outbound legal document).

    This gate restores a fail-loud guarantee adapted to the reconstruction by keying
    on the one signal a DROPPED redline cannot fake: the TRACKED-CHANGE MARKUP for its
    change, scoped to the redline's OWN target paragraph. A landed redline emits real
    ``w:ins``/``w:del`` revision runs carrying the changed tokens INSIDE the paragraph
    it anchored to; a dropped redline leaves that paragraph's original text intact and
    untracked. So for every reviewer redline we resolve the paragraph it targets (by
    confident text match of its original/anchor text against each paragraph's pre-change
    text) and verify a matching tracked change exists WITHIN THAT PARAGRAPH:

    - replace: the inserted tokens of the original->replacement diff must appear inside
      that paragraph's ``w:ins`` markup AND the removed tokens inside its ``w:del``. We
      require only the CHANGED tokens (the tokenizer emits just the delta, not the whole
      paragraph), not a whole-paragraph similarity score -- so a small edit to a long
      clause is caught when dropped, even though the surviving original is >90%
      token-identical to the intended replacement.
    - char-level freeform manual replace: the char-diff builder fragments words into
      single-character ``w:ins``/``w:del`` runs (``written``->``oral`` emits del
      ``w``+``itten`` / ins ``o``+``al``), so token-keyed matching cannot find the
      whole word. For these the target paragraph carrying genuine tracked markup whose
      ACCEPTED text confidently matches the replacement (and whose pre-change text
      matched the original) is sufficient; a drop leaves the paragraph plain (no
      markup), which fails.
    - insert: the inserted text's tokens must appear inside a wholly-inserted paragraph.
    - delete: the removed text must be retained as a tracked deletion (``w:del`` /
      ``w:delText``) in its target paragraph.

    Scoping to the redline's own paragraph (rather than a document-global token pool) is
    what stops a sibling clause's landed change from satisfying a DIFFERENT redline: two
    clauses that received the IDENTICAL edit are checked independently against their own
    paragraphs. But per-paragraph scoping alone is NOT enough when two redlines have
    IDENTICAL (or >=0.9 similar) original text -- a single landed twin's markup matches
    BOTH redlines' target-paragraph criteria, so one landing could "cover" any number of
    dropped twins. Coverage is therefore a ONE-TO-ONE bipartite matching: each marked-up
    paragraph that carries a change can be CONSUMED by at most ONE redline. N identical
    redlines need N DISTINCT paragraphs carrying that change; if only M < N do, N-M are
    uncovered -> caught. Returns error strings (counts only, never the NDA text; empty ==
    covered). Fails toward safety: a redline that cannot be assigned a distinct covering
    paragraph is a drop.
    """
    redlines = [redline for redline in expected_redline_edits if isinstance(redline, dict)] \
        if isinstance(expected_redline_edits, list) else []
    if not redlines:
        return []

    paragraphs = _export_revision_change_paragraphs(docx_bytes)
    has_any_markup = any(record["inserted_tokens"] or record["deleted_tokens"] for record in paragraphs)

    # Split redlines into those with nothing to verify (no-op edits -- always covered,
    # consume no paragraph) and those whose change must be assigned a DISTINCT covering
    # paragraph. ``_covering_paragraph_indices`` returns None for the no-op case, which is
    # dropped from the matching so it neither steals a paragraph nor counts as missing.
    candidate_indices: List[Set[int]] = [
        indices
        for indices in (_covering_paragraph_indices(redline, paragraphs) for redline in redlines)
        if indices is not None
    ]

    # A paragraph, once consumed by one redline, cannot satisfy another -- so identical
    # redlines compete for distinct paragraphs. The matching counts how many can each be
    # given a distinct covering paragraph.
    matched = _max_bipartite_matching(candidate_indices)
    missing = len(candidate_indices) - matched

    if missing:
        return [
            f"Exported reconstruction is missing {missing} of {len(redlines)} reviewer "
            "redline(s); no tracked change carries the edit, so it may have been "
            "silently dropped from the PDF export."
        ]
    if redlines and not has_any_markup:
        return ["Exported reconstruction body contains no tracked-change markup; all redlines were dropped."]
    return []


def _covering_paragraph_indices(
    redline: Dict[str, object], paragraphs: List[Dict[str, object]]
) -> Set[int] | None:
    """Indices of the export paragraphs whose OWN tracked markup covers ``redline``'s
    change, or ``None`` when the redline has nothing to verify.

    Returns the FULL set of paragraphs that carry this redline's change (not just the
    first), because coverage is decided by a one-to-one matching: identical redlines
    share the same candidate paragraphs and must each be assigned a DISTINCT one. A
    redline whose change carries no tokens to verify (a no-op edit -- e.g. a diff with no
    changed tokens) returns ``None``: the caller drops it from the matching entirely so
    it neither consumes a paragraph nor counts as a drop. Coverage is checked
    per-paragraph, never against a document-global token pool, so a sibling clause's
    identical landed edit can only satisfy this redline by being CONSUMED for it."""
    action = str(redline.get("action") or "")
    original_text = str(redline.get("original_text") or "")

    if action == REDLINE_DELETE_PARAGRAPH:
        removed = _change_tokens(original_text)
        if not removed:
            return None
        # A delete must survive as a tracked deletion in a paragraph whose pre-change
        # text is the deleted clause. A reconstruction that simply omits the paragraph
        # (no w:del) dropped the redline.
        return {
            index
            for index, record in _candidate_target_paragraphs(redline, paragraphs, match_original=True)
            if _tokens_present(removed, record["deleted_tokens"])
        }

    if action == REDLINE_INSERT_AFTER_PARAGRAPH:
        added = _change_tokens(_redline_new_text(redline, action))
        if not added:
            return None
        # An insert-after redline adds a whole new block: its tokens land in a
        # wholly-inserted paragraph's w:ins markup.
        return {
            index
            for index, record in enumerate(paragraphs)
            if _tokens_present(added, record["inserted_tokens"])
        }

    replacement_text = _redline_new_text(redline, action)

    if is_freeform_manual_replace_edit(redline):
        # The char-level builder fragments words into single-character ins/del runs, so
        # token-keyed delta matching cannot find the whole changed word. A genuinely
        # landed char edit still produces tracked markup IN ITS TARGET PARAGRAPH whose
        # accepted text confidently matches the replacement; a drop leaves the paragraph
        # plain. Match the target paragraph by its pre-change text (== original), then
        # require both tracked markup and an accepted view equal to the replacement.
        return {
            index
            for index, record in _candidate_target_paragraphs(redline, paragraphs, match_original=True)
            if (record["inserted_tokens"] or record["deleted_tokens"])
            and confident_text_match(record["accepted"], replacement_text)
        }

    # Replace (and any other content-carrying action): verify the CHANGED tokens of the
    # original->replacement diff appear in THIS redline's target paragraph's markup. We
    # do NOT look for the whole replacement_text in w:ins (the tokenizer emits only the
    # delta) and do NOT fall back to whole-paragraph similarity (which let a dropped
    # small edit slip past round 1). The redline is passed through so the delta honors
    # the run-model whole-text branch as well as the newline/overflow ones.
    added_delta, removed_delta = _redline_change_deltas(redline, original_text, replacement_text)
    if not added_delta and not removed_delta:
        # The diff found nothing to change (e.g. a no-op edit): nothing to verify.
        return None
    covering: Set[int] = set()
    for index, record in _candidate_target_paragraphs(redline, paragraphs, match_original=True):
        added_present = (not added_delta) or _tokens_present(added_delta, record["inserted_tokens"])
        removed_present = (not removed_delta) or _tokens_present(removed_delta, record["deleted_tokens"])
        # A landed replace produces the inserted delta in w:ins AND the removed delta in
        # w:del WITHIN this paragraph; a dropped replace has neither (the original
        # paragraph survives untracked).
        if added_present and removed_present:
            covering.add(index)
    return covering


def _max_bipartite_matching(candidate_indices: List[Set[int]]) -> int:
    """Maximum one-to-one assignment of redlines to DISTINCT covering paragraphs.

    Each redline (left vertex) may be assigned exactly one paragraph (right vertex) from
    its candidate set, and each paragraph may be assigned to at most one redline. The
    count of redlines that CAN be assigned a distinct paragraph is the maximum matching;
    any unmatched redline has no covering paragraph left for it and is treated as a drop.

    This is what stops one landed paragraph from "covering" several identical dropped
    redlines: N identical redlines share one candidate set, so the matching can satisfy
    only as many of them as there are distinct paragraphs carrying that change.

    Standard augmenting-path (Kuhn's) algorithm: for each redline, try to find an
    augmenting path that frees up a paragraph for it. O(V*E), bounded by the small
    redline/paragraph counts of a single NDA."""
    assigned_to: Dict[int, int] = {}  # paragraph index -> redline index it is assigned to

    def _try_assign(redline_index: int, visited: Set[int]) -> bool:
        for paragraph_index in candidate_indices[redline_index]:
            if paragraph_index in visited:
                continue
            visited.add(paragraph_index)
            holder = assigned_to.get(paragraph_index)
            if holder is None or _try_assign(holder, visited):
                assigned_to[paragraph_index] = redline_index
                return True
        return False

    matched = 0
    for redline_index in range(len(candidate_indices)):
        if _try_assign(redline_index, set()):
            matched += 1
    return matched


def _candidate_target_paragraphs(
    redline: Dict[str, object],
    paragraphs: List[Dict[str, object]],
    *,
    match_original: bool,
) -> List[Tuple[int, Dict[str, object]]]:
    """``(index, record)`` pairs for the export paragraphs this redline could have
    anchored to: those whose PRE-CHANGE (rejected) text confidently matches the
    redline's original/anchor text. The index identifies the paragraph for the
    one-to-one matching (so a consumed paragraph cannot satisfy a second redline).

    The pre-change view of a landed tracked change reproduces the source clause exactly
    (its deletions are kept verbatim, its insertions removed), so it equals the
    redline's ``original_text`` even on a noisy reconstruction (``confident_text_match``
    tolerates whitespace/run-split drift at its anchor ratio). Matching on it -- rather
    than scanning every paragraph -- scopes the coverage check to this redline's own
    clause: a different clause that received the IDENTICAL edit does not match this
    redline's original text, so its landed markup can never satisfy this redline. When a
    redline carries anchor text but NO paragraph matches it, return no candidates (the
    clause's original text is absent from the export) -- a strict drop, NOT a fall-back
    to a global scan, which would re-open the cross-clause masking this fix closes. The
    fall-back to every paragraph applies only to anchorless redlines."""
    anchor_texts = [
        text
        for text in (
            str(redline.get("original_text") or ""),
            str(redline.get("anchor_text") or ""),
        )
        if text.strip()
    ]
    if match_original and anchor_texts:
        return [
            (index, record)
            for index, record in enumerate(paragraphs)
            if any(confident_text_match(record["rejected"], anchor) for anchor in anchor_texts)
        ]
    return list(enumerate(paragraphs))


def _redline_new_text(redline: Dict[str, object], action: str) -> str:
    if action == REDLINE_INSERT_AFTER_PARAGRAPH:
        return str(redline.get("insert_text") or redline.get("replacement_text") or "")
    return str(redline.get("replacement_text") or redline.get("insert_text") or "")


def _redline_change_deltas(
    redline: Dict[str, object], original_text: str, replacement_text: str
) -> Tuple[List[str], List[str]]:
    """The changed tokens of a replace, split by side: (inserted_delta, removed_delta).

    Diffs original->replacement with the SAME tokenizer the export uses to emit
    tracked runs (``diff_text_operations``), so the deltas line up byte-for-byte with
    the tokens that landed inside ``w:ins``/``w:del``. Whitespace-only tokens are
    dropped (the export normalizes whitespace away). When the replacement is wholly new
    or the original wholly removed, one side is empty -- the caller verifies only the
    non-empty side(s).

    The builder does NOT always emit fine-grained per-changed-token runs: it emits the
    WHOLE original inside one ``w:del`` and the WHOLE replacement inside one ``w:ins``
    whenever ANY of three branches fire -- (a) either side carries an internal newline
    (``_tracked_replace_paragraph``'s multiline guard), (b) the inline diff overflows its
    matrix limit and falls back to a single whole delete + whole insert pair, or (c) the
    redline carries a non-empty ``replacement_runs`` run model, routing
    ``_source_tracked_primary_redline_paragraph`` into
    ``_source_tracked_replace_paragraph_runs`` (-> ``_tracked_replace_paragraph_runs``),
    which always emits whole-del/whole-ins. ``_export_revision_change_paragraphs`` then
    tokenizes that whole-text markup into per-WORD tokens -- INCLUDING the tokens shared
    between original and replacement, which the fine-grained diff would exclude.

    For case (c) the sanitiser (``export_service._clean_replacement_runs``) guarantees the
    runs' joined text equals ``replacement_text``, so the whole-ins markup carries exactly
    ``replacement``'s tokens -- the same WHOLE-text per-word expectation as (a)/(b).

    To stay byte-for-byte consistent with what actually lands, we ask the builder's own
    predicate (``redline_replace_uses_whole_text_markup``, the single source of truth over
    ALL three branches) which path it takes and, when it uses whole-text markup, expect
    the WHOLE-text per-word token sequence of each side rather than the
    shared-token-excluding fine-grained diff. Using the builder's predicate (instead of
    re-deriving the condition) keeps the gate and the builder in lockstep: a
    genuinely-LANDED runs / multiline / large-clause replace reconciles and PASSES, while
    a DROPPED one (target paragraph left plain, no ``w:ins``/``w:del``) still carries none
    of these tokens and is CAUGHT."""
    original = str(original_text or "")
    replacement = str(replacement_text or "")
    if redline_replace_uses_whole_text_markup(redline, original, replacement):
        # Whole-del/whole-ins branch: the markup carries every token of each side, so
        # the expected delta is each side tokenized in full (matches the export markup
        # the builder emits, shared tokens and all).
        return (
            _significant_tokens(tokenize_inline_diff(replacement)),
            _significant_tokens(tokenize_inline_diff(original)),
        )
    operations = diff_text_operations(original, replacement)
    inserted: List[str] = []
    removed: List[str] = []
    for kind, token in operations:
        if kind == "insert":
            inserted.extend(tokenize_inline_diff(token))
        elif kind == "delete":
            removed.extend(tokenize_inline_diff(token))
    return _significant_tokens(inserted), _significant_tokens(removed)


def _change_tokens(text: str) -> List[str]:
    """The significant (non-whitespace) tokens of ``text`` in the export's tokenizer
    space -- used for insert/delete whole-block redlines that carry no diff."""
    return _significant_tokens(tokenize_inline_diff(str(text or "")))


def _significant_tokens(tokens: List[str]) -> List[str]:
    return [token for token in (t.strip() for t in tokens) if token]


def _tokens_present(needle_tokens: List[str], haystack_tokens: List[str]) -> bool:
    """Whether ``needle_tokens`` occur as a contiguous run inside ``haystack_tokens``.

    Both sides are the export tokenizer's significant tokens (whitespace-normalized),
    so this keys on the CHANGED tokens within the tracked-change markup rather than a
    whole-paragraph similarity score or a global document substring. A contiguous
    subsequence match tolerates the markup carrying additional surrounding context
    (e.g. an adjacent same-run boundary) while still requiring every changed token to
    be present in order -- a dropped change contributes none of them, so it cannot
    match. A single-token delta (the common small edit) reduces to membership."""
    if not needle_tokens:
        return True
    if len(needle_tokens) > len(haystack_tokens):
        return False
    first = needle_tokens[0]
    span = len(needle_tokens)
    for start in range(0, len(haystack_tokens) - span + 1):
        if haystack_tokens[start] != first:
            continue
        if haystack_tokens[start : start + span] == needle_tokens:
            return True
    return False


def _export_revision_change_paragraphs(docx_bytes: bytes) -> List[Dict[str, object]]:
    """Per-paragraph tracked-change view of the export, one record per body ``<w:p>``.

    Each record carries the paragraph's own revision markup -- ``inserted_tokens`` (its
    ``w:ins`` tokens) and ``deleted_tokens`` (its ``w:del`` tokens) -- plus its
    ``accepted`` (post-accept) and ``rejected`` (pre-change) views. Scoping each
    redline's coverage to its OWN paragraph (resolved by matching ``rejected`` to the
    redline's original text) is what stops a sibling clause's identical landed edit from
    satisfying a different redline -- the document-global pooling bug. ``w:p`` elements
    do not nest, so gathering each paragraph's own ``w:ins``/``w:del`` descendants
    partitions the markup without overlap."""
    try:
        validate_docx_bytes_before_open(docx_bytes)
        with ZipFile(BytesIO(docx_bytes)) as archive:
            validate_docx_archive(archive)
            document_root = parse_docx_xml(archive.read("word/document.xml"), part_name="word/document.xml")
    except (BadZipFile, DocxExtractionError, KeyError, ET.ParseError, UnsafeDocxXmlError):
        return []

    records: List[Dict[str, object]] = []
    for paragraph in document_root.iter(_w_tag("p")):
        inserted_text_parts = [_revision_markup_text(ins) for ins in paragraph.iter(_w_tag("ins"))]
        deleted_text_parts = [_revision_markup_text(delete) for delete in paragraph.iter(_w_tag("del"))]
        records.append(
            {
                "accepted": _normalize_export_text(_paragraph_revision_text(paragraph, accepted=True)),
                "rejected": _normalize_export_text(_paragraph_revision_text(paragraph, accepted=False)),
                "inserted_tokens": _significant_tokens(
                    tokenize_inline_diff(_normalize_export_text(" ".join(inserted_text_parts)))
                ),
                "deleted_tokens": _significant_tokens(
                    tokenize_inline_diff(_normalize_export_text(" ".join(deleted_text_parts)))
                ),
            }
        )
    return records


def _revision_markup_text(node: ET.Element) -> str:
    """The visible text carried by a revision element: ``w:t`` for insertions,
    ``w:delText`` for deletions. ``w:br``/``w:cr`` become spaces so a hard break inside
    the change does not fuse two tokens."""
    parts: List[str] = []
    for descendant in node.iter():
        if descendant.tag in (_w_tag("t"), _w_tag("delText")):
            parts.append(descendant.text or "")
        elif descendant.tag in (_w_tag("br"), _w_tag("cr"), _w_tag("tab")):
            parts.append(" ")
    return "".join(parts)


def _verify_structural_counts(docx_bytes: bytes, source_docx: bytes) -> List[str]:
    export_counts = _docx_structural_counts(docx_bytes)
    source_counts = _docx_structural_counts(source_docx)
    if export_counts is None or source_counts is None:
        return []
    mismatches = {
        key: {"source": source_counts.get(key, 0), "export": export_counts.get(key, 0)}
        for key in sorted(source_counts)
        if source_counts.get(key, 0) != export_counts.get(key, 0)
    }
    if not mismatches:
        return []
    details = ", ".join(
        f"{key} source={counts['source']} export={counts['export']}"
        for key, counts in mismatches.items()
    )
    return [
        "Exported structural counts do not match the source document "
        f"({details}); the redline may have dropped non-text document structure."
    ]


def _docx_structural_counts(docx_bytes: bytes) -> Dict[str, int] | None:
    try:
        validate_docx_bytes_before_open(docx_bytes)
        with ZipFile(BytesIO(docx_bytes)) as archive:
            validate_docx_archive(archive)
            names = set(archive.namelist())
            xml_parts = [
                name
                for name in names
                if name == "word/document.xml"
                or re.fullmatch(r"word/(header|footer)\d+\.xml", name)
            ]
            counts = {
                "tables": 0,
                "drawings": 0,
                "pictures": 0,
                "hyperlinks": 0,
                "footnote_refs": 0,
                "endnote_refs": 0,
                "header_parts": sum(1 for name in names if re.fullmatch(r"word/header\d+\.xml", name)),
                "footer_parts": sum(1 for name in names if re.fullmatch(r"word/footer\d+\.xml", name)),
            }
            for part_name in xml_parts:
                root = parse_docx_xml(archive.read(part_name), part_name=part_name)
                counts["tables"] += sum(1 for _ in root.iter(_w_tag("tbl")))
                counts["drawings"] += sum(1 for _ in root.iter(_w_tag("drawing")))
                counts["pictures"] += sum(1 for _ in root.iter(_w_tag("pict")))
                counts["hyperlinks"] += sum(1 for _ in root.iter(_w_tag("hyperlink")))
                counts["footnote_refs"] += sum(1 for _ in root.iter(_w_tag("footnoteReference")))
                counts["endnote_refs"] += sum(1 for _ in root.iter(_w_tag("endnoteReference")))
            return counts
    except (BadZipFile, DocxExtractionError, KeyError, ET.ParseError, UnsafeDocxXmlError):
        return None


def _export_revision_paragraphs(docx_bytes: bytes) -> List[Dict[str, str]]:
    try:
        validate_docx_bytes_before_open(docx_bytes)
        with ZipFile(BytesIO(docx_bytes)) as archive:
            validate_docx_archive(archive)
            document_root = parse_docx_xml(archive.read("word/document.xml"), part_name="word/document.xml")
    except (BadZipFile, DocxExtractionError, KeyError, ET.ParseError, UnsafeDocxXmlError):
        return []

    return [
        {
            "accepted": _normalize_export_text(_paragraph_revision_text(paragraph, accepted=True)),
            "all": _normalize_export_text(_paragraph_all_revision_text(paragraph)),
            "rejected": _normalize_export_text(_paragraph_revision_text(paragraph, accepted=False)),
        }
        for paragraph in document_root.iter(_w_tag("p"))
    ]


def _paragraph_revision_text(node: ET.Element, *, accepted: bool) -> str:
    tag = node.tag.rsplit("}", 1)[-1]
    if tag == "del":
        return "" if accepted else "".join(_paragraph_revision_text(child, accepted=False) for child in list(node))
    if tag == "ins":
        return "".join(_paragraph_revision_text(child, accepted=True) for child in list(node)) if accepted else ""
    if tag in {"t", "delText"}:
        return node.text or ""
    if tag == "br":
        return "\n"
    return "".join(_paragraph_revision_text(child, accepted=accepted) for child in list(node))


def _paragraph_all_revision_text(paragraph: ET.Element) -> str:
    return "".join(
        node.text or ""
        for node in paragraph.iter()
        if node.tag in (_w_tag("t"), _w_tag("delText"))
    )


def _source_paragraphs_from_text(source_text: str) -> List[str]:
    return [
        normalized
        for paragraph in re.split(r"\n\s*\n+", str(source_text or ""))
        if (normalized := _normalize_export_text(paragraph))
    ]


def _apply_clean_fills_to_source_paragraphs(source_paragraphs: List[str], clean_fills: object) -> List[str]:
    """Apply clean-mode fills (``find`` -> ``value``) to the expected source blocks.

    Clean fills are baked into the exported base document but are not redline edits,
    so the expected accepted-paragraph sequence must reflect them or every filled
    paragraph reads as a divergence. A fill targets one block by its review
    ``paragraph_id`` (``p<N>`` == the 1-based block ordinal, the same key the redline
    expected-sequence uses); the value is normalized to match the export's normalized
    accepted text.
    """
    if not isinstance(clean_fills, list) or not clean_fills:
        return source_paragraphs
    filled = list(source_paragraphs)
    for fill in clean_fills:
        if not isinstance(fill, dict):
            continue
        index = _fill_source_index(fill.get("paragraph_id"))
        find = fill.get("find")
        value = fill.get("value")
        if index is None or not isinstance(find, str) or not find or not isinstance(value, str):
            continue
        if 1 <= index <= len(filled):
            filled[index - 1] = _normalize_export_text(filled[index - 1].replace(find, value))
    return filled


def _fill_source_index(paragraph_id: object) -> int | None:
    match = re.match(r"^p(\d+)$", str(paragraph_id or "").strip())
    return int(match.group(1)) if match else None


def _expected_accepted_source_paragraphs(
    source_paragraphs: List[str],
    expected_redline_edits: object,
) -> Tuple[List[str], List[str]]:
    expected = list(source_paragraphs)
    errors: List[str] = []
    expected_insertions_by_source_index: Dict[int, List[str]] = {}
    # Each source paragraph may be destructively rewritten (replace/delete) by AT MOST
    # one redline. Category A coalesces every same-paragraph span of a clause into a
    # single replace_paragraph before export, so a SECOND destructive edit on the same
    # paragraph means coalescing did not happen: silently keeping only one (the prior
    # behavior overwrote expected[index] in place) would build the gate's expected
    # sequence from the surviving edit alone and could PASS an export that dropped the
    # other -- defeating the fail-closed guarantee. We fail closed instead. Distinct
    # blocks of one physical paragraph carry distinct paragraph_index values (the unique
    # block key), so legitimate per-block edits never collide here.
    destructive_index_owner: Dict[int, str] = {}
    if not isinstance(expected_redline_edits, list):
        return expected, []

    for redline in expected_redline_edits:
        if not isinstance(redline, dict):
            continue
        action = str(redline.get("action") or "")
        source_index = _expected_redline_source_index(redline)
        if source_index is None:
            continue
        if source_index < 1 or source_index > len(source_paragraphs):
            errors.append(f"Redline {_redline_label(redline)} targets missing source paragraph {source_index}.")
            continue

        if action in (REDLINE_REPLACE_PARAGRAPH, REDLINE_DELETE_PARAGRAPH):
            previous_owner = destructive_index_owner.get(source_index)
            if previous_owner is not None:
                errors.append(
                    f"Redlines {previous_owner} and {_redline_label(redline)} both rewrite source "
                    f"paragraph {source_index}; the clause's edits were not coalesced, so the export "
                    "may have silently dropped one of them."
                )
                continue
            destructive_index_owner[source_index] = _redline_label(redline)

        if action == REDLINE_REPLACE_PARAGRAPH:
            expected[source_index - 1] = _normalize_export_text(redline.get("replacement_text"))
        elif action == REDLINE_DELETE_PARAGRAPH:
            expected[source_index - 1] = ""
        elif action == REDLINE_INSERT_AFTER_PARAGRAPH:
            expected_insertions_by_source_index.setdefault(source_index, []).extend(
                _redline_text_blocks(redline.get("insert_text") or redline.get("replacement_text") or "")
            )

    if errors:
        return [], errors

    accepted: List[str] = []
    for source_index, paragraph in enumerate(expected, start=1):
        if paragraph:
            accepted.append(paragraph)
        accepted.extend(expected_insertions_by_source_index.get(source_index, []))
    return accepted, []


def _expected_redline_source_index(redline: Dict[str, object]) -> int | None:
    # The expected sequence is built over the blank-line-split source blocks
    # (_source_paragraphs_from_text), whose 1-based ordinal is the review
    # paragraph_index. Prefer paragraph_index over source_index: source_index is
    # provenance and is shared by the parts of one extracted block that split on an
    # internal blank line, so keying on it would map two redlines to one block and
    # spuriously fail the content-coverage check. paragraph_index is unique per
    # block. source_index remains the fallback for redlines that carry no index.
    for key in ("paragraph_index", "source_index"):
        value = redline.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


def _redline_text_blocks(value: object) -> List[str]:
    blocks = [
        normalized
        for block in str(value or "").split("\n\n")
        if (normalized := _normalize_export_text(block))
    ]
    return blocks


def _redline_label(redline: Dict[str, object]) -> str:
    for key in ("id", "clause_id", "paragraph_id"):
        value = str(redline.get(key) or "").strip()
        if value:
            return value
    return "unknown"


def _normalize_export_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _docx_content_types(archive: ZipFile) -> Tuple[Dict[str, str], Dict[str, str]]:
    content_types_root = parse_docx_xml(archive.read("[Content_Types].xml"), part_name="[Content_Types].xml")
    defaults = {
        default.attrib["Extension"]: default.attrib["ContentType"]
        for default in content_types_root.findall(_content_type_tag("Default"))
        if "Extension" in default.attrib and "ContentType" in default.attrib
    }
    overrides = {
        override.attrib["PartName"]: override.attrib["ContentType"]
        for override in content_types_root.findall(_content_type_tag("Override"))
        if "PartName" in override.attrib and "ContentType" in override.attrib
    }
    return defaults, overrides


def _relationship_targets(archive: ZipFile, relationship_part: str) -> List[Dict[str, str]]:
    relationships_root = parse_docx_xml(archive.read(relationship_part), part_name=relationship_part)
    return [
        dict(relationship.attrib)
        for relationship in relationships_root.findall(_rel_tag("Relationship"))
    ]


def _resolve_relationship_target(relationship_part: str, target: str) -> str:
    if target.startswith("/"):
        return target.removeprefix("/")
    if relationship_part == "_rels/.rels":
        base_dir = ""
    else:
        rels_dir = posixpath.dirname(relationship_part)
        base_dir = posixpath.dirname(rels_dir)
    return posixpath.normpath(posixpath.join(base_dir, target))


def _w_tag(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


def _rel_tag(tag: str) -> str:
    return f"{{{REL_NS}}}{tag}"


def _content_type_tag(tag: str) -> str:
    return f"{{{CONTENT_TYPES_NS}}}{tag}"
