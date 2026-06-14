"""AI structure-validation overlay for the deterministic contract parse.

The deterministic parser :func:`nda_automation.contract_structure.build_contract_structure`
reads Word numbering / heading metadata to discover sections. On "regime 3"
documents the author MISUSES styles: definition sentences, recital bodies,
signature-block fields ("COMPANY NAME", "IN WITNESS WHEREOF"), connective words
("AND"), party-name lines, street addresses, dates and table-cell values all
inherit heading/numbering metadata and become FALSE "sections". Because those
sections are ``source``-backed, the existing source-backed guard trusts them.
Only semantic judgment catches them.

This module adds an ADDITIVE, semantic post-pass. It takes the deterministic
structure plus the document paragraphs, asks an AI validator to classify each
candidate section ``genuine`` vs ``false_positive``, and DEMOTES the false
positives:

* the section is removed from ``reference_index.alias_to_section_id`` so it can
  no longer be a cross-reference / jump target, and
* the section is flagged (``validation == "false_positive"``) and dropped from a
  derived ``navigable_sections`` view.

It NEVER deletes paragraphs, NEVER touches genuine sections, and NEVER blocks
ingestion. The deterministic parse remains the source of truth; the AI only
demotes. Any failure (missing key, network error, timeout, unparseable output)
returns the deterministic structure UNCHANGED.

The real validator reuses the existing OpenRouter infrastructure from
``ai_review`` (same ``OPENROUTER_API_KEY``, same endpoint, same TLS context and
model sanitiser). The model for THIS pass is :data:`STRUCTURE_VALIDATION_MODEL`
(DeepSeek V4 Flash). Tests inject a stub through the ``validator`` parameter and
never touch the network or an API key.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from copy import deepcopy
from typing import Any, Callable, Dict, List, Mapping, Sequence

from .ai_review import (
    DEFAULT_AI_TIMEOUT_SECONDS,
    OPENROUTER_CHAT_COMPLETIONS_ENDPOINT,
    _configured_api_key,
    _env_int,
    _openrouter_response_text,
    _sanitize_model_name,
    _trusted_https_context,
)
from .openrouter_usage import record_openrouter_usage

LOGGER = logging.getLogger(__name__)

STRUCTURE_VALIDATION_VERSION = 1

#: Model used for this structure-validation pass. DeepSeek V4 Flash was validated
#: by a spike to flag regime-3 style-misuse false positives the deterministic
#: source-guard cannot catch. Overridable via ``NDA_STRUCTURE_VALIDATION_MODEL``.
STRUCTURE_VALIDATION_MODEL = "deepseek/deepseek-v4-flash"

#: Env var that points the pass at a different OpenRouter model.
STRUCTURE_VALIDATION_MODEL_ENV = "NDA_STRUCTURE_VALIDATION_MODEL"

#: Number of leading characters of the section's first body paragraph sent to the
#: validator as a context snippet. Keeps the packet small; the heading itself is
#: the primary signal.
SNIPPET_CHAR_LIMIT = 240

#: Verdict literals.
VERDICT_GENUINE = "genuine"
VERDICT_FALSE_POSITIVE = "false_positive"

#: A ``validator`` maps a list of section-candidate dicts to a list of verdict
#: dicts ``{"id", "verdict", "reason"}``. Returning ``None`` (or anything
#: unparseable) is treated as "no usable opinion" and leaves the structure
#: unchanged.
StructureValidator = Callable[[List[Dict[str, Any]]], "Sequence[Mapping[str, Any]] | None"]


SYSTEM_PROMPT = (
    "You are a contract-structure auditor. A deterministic parser read a Word "
    "document's numbering and heading styles and produced a list of CANDIDATE "
    "sections. On some documents the author misuses Word styles, so non-structural "
    "text inherited heading/numbering formatting and was wrongly promoted to a "
    "section. Your only job is to separate GENUINE structural sections from those "
    "FALSE POSITIVES.\n"
    "\n"
    "Mark verdict \"false_positive\" ONLY when the candidate is genuinely "
    "NON-structural text that merely inherited heading/numbering style, such as:\n"
    "- signature-block fields or phrases: \"COMPANY NAME\", \"IN WITNESS WHEREOF\", "
    "\"NAME:\", \"TITLE:\", \"DATE:\", \"SIGNATURE\", \"BY:\";\n"
    "- bare connective words: \"AND\", \"OR\", \"BY AND BETWEEN\";\n"
    "- party-name lines (a company or person name standing alone);\n"
    "- street addresses, postal addresses, or bare dates;\n"
    "- table-cell values (numbers, money amounts, single words);\n"
    "- a definition sentence or recital/body sentence that was promoted to a "
    "TOP-LEVEL heading (e.g. a full \"X means ...\" sentence sitting at level 1).\n"
    "\n"
    "Mark verdict \"genuine\" for EVERYTHING that is real document structure, "
    "including:\n"
    "- all real numbered or titled clauses, articles and sections;\n"
    "- schedules, annexes, annexures, appendices and exhibits;\n"
    "- recital HEADINGS (e.g. \"RECITALS\", \"BACKGROUND\", \"WHEREAS\" headers);\n"
    "- real sub-clauses and enumerations: (a)/(b)/(c), (i)/(ii)/(iii), 1.1/1.2, etc.\n"
    "Do NOT flag a real sub-clause just because it is short or lower-level: real "
    "sub-structure must be KEPT as genuine. When unsure, prefer \"genuine\".\n"
    "\n"
    "Return ONLY a JSON array. Each element is an object with exactly these keys: "
    "\"id\" (the candidate id you were given), \"verdict\" (\"genuine\" or "
    "\"false_positive\"), and \"reason\" (a short justification). Return one element "
    "for every candidate. Output no prose outside the JSON array. "
    "Return ONLY the raw JSON array -- no markdown fences, no commentary."
)


#: Matches the first balanced-looking ``[ ... ]`` block across newlines. DeepSeek
#: V4 Flash often wraps the verdict array in a ```json fence and/or a prose
#: preamble ("Here is the analysis:"); this extracts the array body so a strict
#: ``json.loads`` of the WHOLE content does not throw away an otherwise-correct
#: response. Greedy on purpose: the last ``]`` closes the outermost array.
_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


class StructureValidationError(RuntimeError):
    pass


def validate_structure(
    structure: Dict[str, Any],
    paragraphs: Sequence[Mapping[str, Any]] | None,
    *,
    validator: StructureValidator | None = None,
) -> Dict[str, Any]:
    """Demote AI-identified false-positive sections in ``structure``.

    ``structure`` is the dict returned by ``build_contract_structure``.
    ``paragraphs`` are the same document paragraphs that built it (used to derive
    each section's heading + first-body-paragraph snippet for the validator).

    The deterministic structure is treated as the source of truth: a copy is
    returned with false positives demoted (removed from
    ``reference_index.alias_to_section_id``, flagged ``validation ==
    "false_positive"``, and excluded from a derived ``navigable_sections`` list).
    Genuine sections, their aliases, and ALL paragraphs are left untouched.

    On ANY failure -- no API key, validator raises, times out, or returns
    unusable output -- the deterministic structure is returned UNCHANGED (a
    warning is logged). Validation is additive and must never block ingestion.
    """
    if not isinstance(structure, dict):
        return structure
    sections = structure.get("sections")
    if not isinstance(sections, list) or not sections:
        return structure

    candidates = _build_candidates(sections, paragraphs or [])
    if not candidates:
        return structure

    active_validator = validator if validator is not None else _default_validator()
    if active_validator is None:
        LOGGER.warning(
            "Structure validation skipped: no validator and no OpenRouter API key configured."
        )
        return structure

    try:
        raw_verdicts = active_validator(candidates)
    except Exception as error:  # noqa: BLE001 - additive pass must never break ingestion
        LOGGER.warning("Structure validation failed; using deterministic structure: %s", error)
        return structure

    false_positive_ids = _false_positive_ids(raw_verdicts, valid_ids={c["id"] for c in candidates})
    if false_positive_ids is None:
        LOGGER.warning(
            "Structure validation returned unparseable output; using deterministic structure."
        )
        return structure
    if not false_positive_ids:
        # Nothing to demote, but still surface that the pass ran cleanly.
        return _annotate(deepcopy(structure), demoted_ids=set())

    return _demote_false_positives(deepcopy(structure), false_positive_ids)


def should_validate_structure(
    structure: Mapping[str, Any] | None,
    paragraphs: Sequence[Mapping[str, Any]] | None,
) -> bool:
    """Gate the optional post-pass to DOCX-sourced parses with sections to check.

    The style-misuse "regime 3" failure mode only exists for DOCX, whose
    extractor stamps paragraphs with rich layout metadata (``source_kind``,
    ``numbering``, ``heading_level``, ``structure_number``, ``style_id``) and
    produces ``source``-backed sections. PDF / plain-text parses come from
    ``split_document_paragraphs`` and carry none of that, so there is nothing
    style-derived to second-guess -- skip them for now. We also skip when there
    are no real (non-preamble) sections to validate.
    """
    if not isinstance(structure, Mapping):
        return False
    sections = structure.get("sections")
    if not isinstance(sections, list):
        return False
    has_real_section = any(
        isinstance(section, Mapping) and str(section.get("kind") or "") != "preamble"
        for section in sections
    )
    if not has_real_section:
        return False

    # Any source-backed section means the deterministic parse trusted DOCX
    # layout metadata -- exactly the input this pass is designed to audit.
    if any(isinstance(section, Mapping) and isinstance(section.get("source"), dict) for section in sections):
        return True

    metadata_keys = ("source_kind", "numbering", "heading_level", "structure_number", "style_id")
    for paragraph in paragraphs or []:
        if isinstance(paragraph, Mapping) and any(paragraph.get(key) for key in metadata_keys):
            return True
    return False


def _build_candidates(
    sections: Sequence[Mapping[str, Any]],
    paragraphs: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Shape each candidate section into the minimal record sent to the validator.

    The preamble pseudo-section is never a navigation target and is not a
    style-misuse risk, so it is excluded from validation.
    """
    text_by_paragraph_id = {
        str(paragraph.get("id") or ""): str(paragraph.get("text") or "")
        for paragraph in paragraphs
        if isinstance(paragraph, Mapping)
    }
    candidates: List[Dict[str, Any]] = []
    for section in sections:
        if not isinstance(section, Mapping):
            continue
        section_id = str(section.get("id") or "")
        if not section_id:
            continue
        if str(section.get("kind") or "") == "preamble":
            continue
        candidates.append({
            "id": section_id,
            "kind": str(section.get("kind") or ""),
            "label": str(section.get("label") or ""),
            "number": section.get("number") if isinstance(section.get("number"), str) else None,
            "level": int(section.get("level", 0)) if isinstance(section.get("level"), int) else 0,
            "heading": str(section.get("heading") or section.get("heading_text") or ""),
            "snippet": _section_snippet(section, text_by_paragraph_id),
        })
    return candidates


def _section_snippet(
    section: Mapping[str, Any],
    text_by_paragraph_id: Mapping[str, str],
) -> str:
    paragraph_ids = section.get("paragraph_ids")
    if not isinstance(paragraph_ids, list):
        return ""
    for paragraph_id in paragraph_ids:
        text = text_by_paragraph_id.get(str(paragraph_id) or "")
        if text and text.strip():
            collapsed = " ".join(text.split())
            if len(collapsed) <= SNIPPET_CHAR_LIMIT:
                return collapsed
            return collapsed[: SNIPPET_CHAR_LIMIT - 3].rstrip() + "..."
    return ""


def _false_positive_ids(
    raw_verdicts: object,
    *,
    valid_ids: set[str],
) -> set[str] | None:
    """Extract the demoted ids from a validator response.

    Returns ``None`` when the response cannot be parsed into a usable verdict
    list (the caller then leaves the structure unchanged). Returns a (possibly
    empty) set of section ids to demote otherwise. Verdicts referencing unknown
    ids are ignored; only an explicit ``false_positive`` for a KNOWN candidate
    demotes it -- an omitted or unrecognised verdict defaults to keeping the
    section genuine (conservative).
    """
    verdicts = _coerce_verdict_list(raw_verdicts)
    if verdicts is None:
        return None
    demoted: set[str] = set()
    for verdict in verdicts:
        if not isinstance(verdict, Mapping):
            continue
        section_id = str(verdict.get("id") or "")
        if section_id not in valid_ids:
            continue
        decision = str(verdict.get("verdict") or "").strip().lower()
        if decision == VERDICT_FALSE_POSITIVE:
            demoted.add(section_id)
    return demoted


def _coerce_verdict_list(raw_verdicts: object) -> List[Mapping[str, Any]] | None:
    if raw_verdicts is None:
        return None
    if isinstance(raw_verdicts, list):
        return raw_verdicts
    # Accept a JSON-object envelope ``{"verdicts": [...]}`` or a raw JSON string,
    # since model output formats vary.
    if isinstance(raw_verdicts, Mapping):
        for key in ("verdicts", "sections", "results"):
            value = raw_verdicts.get(key)
            if isinstance(value, list):
                return value
        return None
    if isinstance(raw_verdicts, str):
        try:
            parsed = json.loads(raw_verdicts)
        except (json.JSONDecodeError, ValueError):
            return None
        return _coerce_verdict_list(parsed)
    return None


def _parse_model_verdicts(response_text: str) -> object | None:
    """Leniently extract the verdict array from raw model content.

    DeepSeek V4 Flash returns the CORRECT verdict JSON array but often WRAPPED:
    in a ```json fence and/or behind a prose preamble ("Here is the analysis:").
    A strict ``json.loads`` of the whole content throws an otherwise-good response
    away, which silently demotes 0 sections and makes the pass inert.

    Strategy (cheapest-correct first):
      1. Try to parse the whole content as-is (clean responses, the happy path).
      2. Strip ```json / ``` markdown fences and retry.
      3. Locate the first balanced ``[ ... ]`` block (preamble + trailing prose
         tolerated) and parse THAT.

    Returns the parsed object on success, or ``None`` when no JSON array can be
    recovered -- the caller then raises and the existing graceful fallback
    returns the deterministic structure UNCHANGED. There is no retry: a single
    parse failure is terminal, so a malformed response never spins another
    expensive model call.
    """
    text = (response_text or "").strip()
    if not text:
        return None

    # 1. Happy path: the whole content is valid JSON.
    parsed = _try_json_loads(text)
    if parsed is not None:
        return parsed

    # 2. Strip a surrounding markdown code fence, then retry.
    unfenced = _strip_code_fence(text)
    if unfenced != text:
        parsed = _try_json_loads(unfenced)
        if parsed is not None:
            return parsed

    # 3. Extract the first balanced [ ... ] block from anywhere in the content
    #    (preamble prose and trailing commentary tolerated).
    match = _JSON_ARRAY_RE.search(unfenced)
    if match is not None:
        parsed = _try_json_loads(match.group(0))
        if parsed is not None:
            return parsed

    return None


def _try_json_loads(text: str) -> object | None:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _strip_code_fence(text: str) -> str:
    """Remove a wrapping ```json ... ``` (or bare ``` ... ```) markdown fence."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    # Drop the opening fence line (``` or ```json) and any trailing fence.
    body = stripped[3:]
    newline = body.find("\n")
    if newline != -1:
        # The text after the first ``` up to the newline is the language tag.
        body = body[newline + 1:]
    closing = body.rfind("```")
    if closing != -1:
        body = body[:closing]
    return body.strip()


def _demote_false_positives(structure: Dict[str, Any], demoted_ids: set[str]) -> Dict[str, Any]:
    sections = structure.get("sections")
    if isinstance(sections, list):
        for section in sections:
            if isinstance(section, dict) and str(section.get("id") or "") in demoted_ids:
                section["validation"] = VERDICT_FALSE_POSITIVE

    reference_index = structure.get("reference_index")
    if isinstance(reference_index, dict):
        alias_to_section_id = reference_index.get("alias_to_section_id")
        if isinstance(alias_to_section_id, dict):
            reference_index["alias_to_section_id"] = {
                key: value
                for key, value in alias_to_section_id.items()
                if str(value) not in demoted_ids
            }
        # Mark the demoted ids inside the resolver-facing section records too, so a
        # consumer reading from reference_index sees the same verdict.
        sections_by_id = reference_index.get("sections_by_id")
        if isinstance(sections_by_id, dict):
            for section_id, record in sections_by_id.items():
                if isinstance(record, dict) and str(section_id) in demoted_ids:
                    record["validation"] = VERDICT_FALSE_POSITIVE

    return _annotate(structure, demoted_ids=demoted_ids)


def _annotate(structure: Dict[str, Any], *, demoted_ids: set[str]) -> Dict[str, Any]:
    """Attach the validation summary + a derived navigable-sections view."""
    sections = structure.get("sections")
    navigable_sections: List[str] = []
    if isinstance(sections, list):
        navigable_sections = [
            str(section.get("id") or "")
            for section in sections
            if isinstance(section, Mapping)
            and str(section.get("id") or "")
            and str(section.get("validation") or "") != VERDICT_FALSE_POSITIVE
        ]
    structure["structure_validation"] = {
        "version": STRUCTURE_VALIDATION_VERSION,
        "model": _configured_model(),
        "demoted_section_ids": sorted(demoted_ids),
        "demoted_count": len(demoted_ids),
        "navigable_sections": navigable_sections,
    }
    return structure


def _default_validator() -> StructureValidator | None:
    """The production validator, or ``None`` when no OpenRouter key is configured."""
    api_key = _configured_api_key("openrouter")
    if not api_key:
        return None
    return OpenRouterStructureValidator(
        api_key=api_key,
        model=_configured_model(),
        timeout_seconds=_env_int("NDA_AI_TIMEOUT_SECONDS", DEFAULT_AI_TIMEOUT_SECONDS),
    )


def _configured_model() -> str:
    env_model = os.environ.get(STRUCTURE_VALIDATION_MODEL_ENV, "").strip()
    return _sanitize_model_name(env_model or STRUCTURE_VALIDATION_MODEL)


class OpenRouterStructureValidator:
    """Calls DeepSeek V4 Flash over the shared OpenRouter infrastructure.

    Reuses the same endpoint, TLS context, model sanitiser, usage recorder and
    response-text extractor as the review AI (``ai_review``) -- no new HTTP
    client. The model defaults to :data:`STRUCTURE_VALIDATION_MODEL`.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = STRUCTURE_VALIDATION_MODEL,
        timeout_seconds: int = DEFAULT_AI_TIMEOUT_SECONDS,
    ) -> None:
        cleaned_key = str(api_key or "").strip()
        if not cleaned_key:
            raise StructureValidationError("OpenRouter API key is not configured.")
        self.api_key = cleaned_key
        self.model = _sanitize_model_name(model or STRUCTURE_VALIDATION_MODEL)
        self.timeout_seconds = max(1, int(timeout_seconds or DEFAULT_AI_TIMEOUT_SECONDS))

    def __call__(self, candidates: List[Dict[str, Any]]) -> Sequence[Mapping[str, Any]] | None:
        request = urllib.request.Request(
            OPENROUTER_CHAT_COMPLETIONS_ENDPOINT,
            data=json.dumps(self._request_body(candidates)).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "nda-automation/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds, context=_trusted_https_context()
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            message = error.read().decode("utf-8", errors="replace")[:500]
            raise StructureValidationError(
                f"OpenRouter API returned HTTP {error.code}: {message}"
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise StructureValidationError(f"OpenRouter API request failed: {error}") from error

        record_openrouter_usage(payload, feature="structure_validation", model=self.model)
        response_text = _openrouter_response_text(payload)
        if not response_text:
            raise StructureValidationError("OpenRouter API returned no message content.")
        parsed = _parse_model_verdicts(response_text)
        if parsed is None:
            raise StructureValidationError("OpenRouter API returned non-JSON text.")
        return parsed

    def _request_body(self, candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
        user_payload = {
            "task": "classify_candidate_sections",
            "candidates": candidates,
        }
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(user_payload, ensure_ascii=False, indent=2),
                },
            ],
            "temperature": 0,
        }
