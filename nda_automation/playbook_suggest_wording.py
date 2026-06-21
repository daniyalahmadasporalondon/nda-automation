"""Propose MINIMAL wording edits so a clause's free-text reflects a list change.

When an admin edits a clause's structured lists in the Playbook editor (e.g. adds a
new category to ``definition_categories`` or a new approved law) the dependent
free-text fields (``preferred_position``, ``check_trigger``, ``redline_template``,
``standard_exclusions_template``) can drift out of sync with the lists. This module
asks the AI to incorporate the list change into the EXISTING wording with a minimal,
targeted edit -- inserting the new category into the existing sentence rather than
rewriting the clause from scratch -- so the admin can PREVIEW and APPROVE the change.

Contract guarantees:

* **Never persists.** This proposes only; saving stays with the publish flow.
* **Never silently alters legal text.** Each proposed ``new`` value is run through the
  SAME validation the publish gate uses (neutralization + length caps + forum_shape +
  redline/template coherence, via :func:`collect_playbook_validation_errors`). A
  suggestion that fails validation sets ``validation_ok=False`` and the reason rides
  in ``warnings`` -- it is NOT returned as if it were safe to apply.
* **Fail-soft.** On any AI error/timeout the response carries empty ``suggestions`` and
  a warning, never a 500 that would lose the admin's in-progress draft.

The AI seam mirrors the assessor: a reviewer callable takes a packet and returns a
``{field: new_text}`` mapping. ``NDA_AI_WORDING_SUGGEST_STUB`` swaps in a deterministic
key-free reviewer for tests (the same opt-in pattern as ``NDA_AI_ASSESSMENT_STUB``).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from copy import deepcopy
from typing import Any, Callable, Mapping

from .ai_review import (
    DEFAULT_AI_TIMEOUT_SECONDS,
    DEFAULT_OPENROUTER_MODEL,
    OPENROUTER_CHAT_COMPLETIONS_ENDPOINT,
    _ai_review_settings,
    _configured_api_key,
    _openrouter_response_text,
    _sanitize_model_name,
    _trusted_https_context,
)
from .openrouter_usage import record_openrouter_usage
from .playbook_authoring import collect_playbook_validation_errors
from .playbook_runtime import read_playbook_from_path
from .checker import PLAYBOOK_PATH

# The free-text fields this endpoint may propose edits for. Every other key in the
# request ``fields`` list is ignored (it is not a dependent free-text field). Keeping
# this a CLOSED allowlist means a caller cannot smuggle the AI into rewriting a
# structured list or an unrelated field through this route.
SUGGESTIBLE_FIELDS = (
    "preferred_position",
    "check_trigger",
    "redline_template",
    "standard_exclusions_template",
)

# Test seam only: when this env var is truthy the deterministic key-free stub
# reviewer is used instead of any real provider. Off by default in production, so the
# real OpenRouter reviewer is always used unless a test explicitly opts in. Mirrors
# ``NDA_AI_ASSESSMENT_STUB`` in ai_assessor.
AI_WORDING_SUGGEST_STUB_ENV = "NDA_AI_WORDING_SUGGEST_STUB"

# A reviewer takes a packet and returns {field: proposed_new_text}. None / a missing
# key means "no change proposed for that field".
WordingSuggestionReviewer = Callable[[dict[str, Any]], Mapping[str, Any] | None]


class WordingSuggestionError(RuntimeError):
    """Raised by a reviewer when the AI call fails; caught and turned into a warning."""


# ---------------------------------------------------------------------------
# Reviewers (the AI seam)
# ---------------------------------------------------------------------------
class OpenRouterWordingSuggestionReviewer:
    """Calls OpenRouter for minimal wording edits, reusing ai_review's key/model."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_OPENROUTER_MODEL,
        timeout_seconds: int = DEFAULT_AI_TIMEOUT_SECONDS,
    ) -> None:
        cleaned_key = str(api_key or "").strip()
        if not cleaned_key:
            raise WordingSuggestionError("OpenRouter API key is not configured.")
        self.api_key = cleaned_key
        self.model = _sanitize_model_name(model or DEFAULT_OPENROUTER_MODEL)
        self.timeout_seconds = max(1, int(timeout_seconds or DEFAULT_AI_TIMEOUT_SECONDS))

    def __call__(self, packet: dict[str, Any]) -> Mapping[str, Any] | None:
        request = urllib.request.Request(
            OPENROUTER_CHAT_COMPLETIONS_ENDPOINT,
            data=json.dumps(
                openrouter_wording_suggestion_request_body(packet, model=self.model)
            ).encode("utf-8"),
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
            raise WordingSuggestionError(
                f"OpenRouter API returned HTTP {error.code}: {message}"
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise WordingSuggestionError(f"OpenRouter API request failed: {error}") from error
        record_openrouter_usage(payload, feature="wording_suggest", model=self.model)
        text = _openrouter_response_text(payload)
        if not text:
            raise WordingSuggestionError("OpenRouter API returned no message content.")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as error:
            raise WordingSuggestionError("OpenRouter API returned non-JSON text.") from error
        suggestions = parsed.get("suggestions") if isinstance(parsed, dict) else None
        return suggestions if isinstance(suggestions, Mapping) else {}


class InMemoryWordingSuggestionReviewer:
    """Deterministic test reviewer: a fixed mapping (or callable), or an error."""

    def __init__(
        self,
        *,
        response: object | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.packets: list[dict[str, Any]] = []

    def __call__(self, packet: dict[str, Any]) -> Mapping[str, Any] | None:
        self.packets.append(deepcopy(packet))
        if self.error is not None:
            raise self.error
        if callable(self.response):
            return self.response(packet)
        return deepcopy(self.response) if isinstance(self.response, Mapping) else self.response


def _stub_wording_suggestion(packet: dict[str, Any]) -> dict[str, Any]:
    """Deterministic key-free reviewer used only under AI_WORDING_SUGGEST_STUB_ENV.

    It performs the same minimal-edit job the prompt asks the model for, but
    mechanically: for each requested field it appends any list item that is present in
    the updated lists but not yet mentioned in the current wording. This lets the
    route be exercised end to end without a live key while still proving the
    "incorporate the list change, preserve the rest" contract.
    """
    current = packet.get("current_wording")
    fields = packet.get("fields")
    lists = packet.get("updated_lists")
    current = current if isinstance(current, Mapping) else {}
    fields = list(fields) if isinstance(fields, (list, tuple)) else []
    lists = lists if isinstance(lists, Mapping) else {}

    # Flatten every authored list value to a pool of category strings.
    pool: list[str] = []
    for value in lists.values():
        if isinstance(value, (list, tuple)):
            pool.extend(str(item).strip() for item in value if str(item).strip())

    out: dict[str, str] = {}
    for field in fields:
        text = str(current.get(field) or "")
        if not text:
            continue
        missing = [item for item in pool if item and item.lower() not in text.lower()]
        if not missing:
            out[field] = text
            continue
        # Minimal edit: weave the missing categories into the EXISTING sentence rather
        # than rewriting it. We splice before a trailing period to keep the sentence
        # intact, preserving the admin's phrasing verbatim everywhere else.
        addition = ", " + ", ".join(missing)
        if text.rstrip().endswith("."):
            head = text.rstrip()[:-1]
            out[field] = f"{head}{addition}."
        else:
            out[field] = f"{text}{addition}"
    return out


def configured_wording_suggestion_reviewer(
    settings: Mapping[str, Any] | None = None,
) -> WordingSuggestionReviewer:
    if os.environ.get(AI_WORDING_SUGGEST_STUB_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return InMemoryWordingSuggestionReviewer(response=_stub_wording_suggestion)
    config = dict(settings or _ai_review_settings())
    provider = str(config.get("provider") or "openrouter").strip().lower()
    timeout_seconds = int(config.get("timeout_seconds") or DEFAULT_AI_TIMEOUT_SECONDS)
    model = str(config.get("model") or "").strip()
    if provider == "openrouter":
        return OpenRouterWordingSuggestionReviewer(
            api_key=_configured_api_key(provider),
            model=model or DEFAULT_OPENROUTER_MODEL,
            timeout_seconds=timeout_seconds,
        )
    raise WordingSuggestionError(f"Unsupported AI provider: {provider}")


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------
def build_wording_suggestion_packet(
    *,
    clause: Mapping[str, Any],
    fields: list[str],
    current_wording: Mapping[str, str],
) -> dict[str, Any]:
    """A compact, neutral packet for the model: the clause id, its updated lists, the
    fields to edit, and each field's CURRENT text."""
    clause_id = str(clause.get("id") or "")
    # Surface only the authored LIST fields as "updated_lists" -- the structured change
    # the wording must reflect. Free-text fields are passed separately as current
    # wording so the model edits them, not re-reads them as lists.
    updated_lists = {
        key: list(value)
        for key, value in clause.items()
        if isinstance(value, (list, tuple)) and key not in SUGGESTIBLE_FIELDS
    }
    return {
        "clause_id": clause_id,
        "clause_name": str(clause.get("name") or ""),
        "fields": list(fields),
        "current_wording": dict(current_wording),
        "updated_lists": updated_lists,
    }


def openrouter_wording_suggestion_request_body(
    packet: Mapping[str, Any], *, model: str
) -> dict[str, Any]:
    system = (
        "You are a legal-operations editor maintaining an NDA review Playbook. An admin "
        "has changed a clause's structured lists and you must update the clause's "
        "free-text wording to reflect that change. Make the MINIMAL, TARGETED edit "
        "needed: incorporate the list change into the EXISTING wording (for example, "
        "insert the new category into the existing sentence). Do NOT rewrite from "
        "scratch. Preserve the admin's existing legal phrasing verbatim everywhere the "
        "list change does not require a change. Do not invent new obligations, change "
        "the legal meaning, or alter unrelated wording. If a field already reflects the "
        "lists, return it unchanged. Respond with a JSON object of the form "
        '{"suggestions": {"<field>": "<new text>"}} containing only the requested '
        "fields."
    )
    user = json.dumps(
        {
            "clause_id": packet.get("clause_id"),
            "clause_name": packet.get("clause_name"),
            "fields_to_update": packet.get("fields"),
            "current_wording": packet.get("current_wording"),
            "updated_lists": packet.get("updated_lists"),
        },
        ensure_ascii=False,
        indent=2,
    )
    return {
        "model": _sanitize_model_name(model or DEFAULT_OPENROUTER_MODEL),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }


# ---------------------------------------------------------------------------
# Validation (reuse the publish gate)
# ---------------------------------------------------------------------------
def _validation_warnings_for_field(
    *,
    clause: Mapping[str, Any],
    field: str,
    new_text: str,
    playbook_path: Any,
) -> list[str]:
    """Run the proposed wording through the SAME validation the publish gate uses.

    The candidate clause (the in-progress clause with this one field set to the
    proposed ``new``) is spliced into the ACTIVE playbook and the whole thing is run
    through :func:`collect_playbook_validation_errors` -- the exact stack the publish
    gate runs (validate_playbook + validate_playbook_rules + lint, which covers
    neutralization, length caps, forum_shape and redline/template coherence). Only
    errors scoped to THIS clause are returned, so unrelated pre-existing playbook
    issues do not mask or fabricate a verdict on the suggestion.
    """
    clause_id = str(clause.get("id") or "")
    try:
        active = read_playbook_from_path(playbook_path)
    except Exception as error:  # noqa: BLE001 - fail-soft: cannot validate => warn, don't crash.
        return [f"could not load the active playbook to validate this suggestion: {error}"]
    if not isinstance(active, dict):
        return ["could not load the active playbook to validate this suggestion."]

    candidate = deepcopy(active)
    clauses = candidate.get("clauses")
    if not isinstance(clauses, list):
        return ["active playbook has no clauses to validate against."]

    candidate_clause = deepcopy(dict(clause))
    candidate_clause[field] = new_text
    replaced = False
    for index, existing in enumerate(clauses):
        if isinstance(existing, Mapping) and str(existing.get("id") or "") == clause_id:
            clauses[index] = candidate_clause
            replaced = True
            break
    if not replaced:
        # A brand-new clause not yet in the active playbook: append it so the rules /
        # lint still see it (and scope errors to its id below).
        clauses.append(candidate_clause)

    try:
        errors = collect_playbook_validation_errors(candidate)
    except Exception as error:  # noqa: BLE001 - fail-soft: validation crash => warn, don't 500.
        return [f"validation of this suggestion could not run: {error}"]

    warnings: list[str] = []
    for record in errors:
        if not isinstance(record, Mapping):
            continue
        record_clause = record.get("clause")
        record_field = record.get("field")
        # Keep only errors attributable to THIS clause. Errors with no clause but the
        # matching field are also surfaced (some rule errors name the field, not the id).
        if record_clause == clause_id or (not record_clause and record_field == field):
            message = str(record.get("message") or "").strip()
            if message:
                warnings.append(message)
    return warnings


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def suggest_clause_wording(
    *,
    clause: Mapping[str, Any],
    fields: list[str],
    playbook_path: Any = PLAYBOOK_PATH,
    reviewer: WordingSuggestionReviewer | None = None,
) -> dict[str, Any]:
    """Propose minimal wording edits for a clause's dependent free-text fields.

    Returns the stable response contract::

        {
          "suggestions": {"<field>": {"old": ..., "new": ..., "changed": bool}, ...},
          "warnings": [...],
          "validation_ok": bool,
        }

    Never persists. Fail-soft: an AI error yields empty ``suggestions`` + a warning.
    """
    requested = [
        field
        for field in (fields or [])
        if isinstance(field, str) and field in SUGGESTIBLE_FIELDS
    ]
    # Current wording for each requested field (what the admin has now). Missing /
    # non-string values become "" so the diff is well-defined.
    current_wording: dict[str, str] = {}
    for field in requested:
        value = clause.get(field)
        current_wording[field] = value if isinstance(value, str) else ""

    warnings: list[str] = []
    if not requested:
        # Nothing relevant to propose for -- not an error, just an empty result.
        return {"suggestions": {}, "warnings": warnings, "validation_ok": True}

    active_reviewer = reviewer or _safe_configured_reviewer(warnings)
    proposed: Mapping[str, Any] = {}
    if active_reviewer is not None:
        packet = build_wording_suggestion_packet(
            clause=clause, fields=requested, current_wording=current_wording
        )
        try:
            result = active_reviewer(packet)
            proposed = result if isinstance(result, Mapping) else {}
        except WordingSuggestionError as error:
            warnings.append(f"AI wording suggestion failed: {error}")
        except Exception as error:  # noqa: BLE001 - fail-soft: never let an AI error 500.
            warnings.append(f"AI wording suggestion failed: {error}")

    suggestions: dict[str, dict[str, Any]] = {}
    validation_ok = True
    for field in requested:
        old_text = current_wording[field]
        raw_new = proposed.get(field) if isinstance(proposed, Mapping) else None
        if not isinstance(raw_new, str):
            # The model proposed nothing for this field -> treat as no change.
            continue
        new_text = raw_new
        changed = new_text != old_text
        if changed:
            field_warnings = _validation_warnings_for_field(
                clause=clause,
                field=field,
                new_text=new_text,
                playbook_path=playbook_path,
            )
            if field_warnings:
                validation_ok = False
                for message in field_warnings:
                    warnings.append(f"{field}: {message}")
        suggestions[field] = {"old": old_text, "new": new_text, "changed": changed}

    return {
        "suggestions": suggestions,
        "warnings": warnings,
        "validation_ok": validation_ok,
    }


def _safe_configured_reviewer(
    warnings: list[str],
) -> WordingSuggestionReviewer | None:
    """Build the configured reviewer, appending a warning instead of raising on error.

    A missing API key (or unsupported provider) must not 500 the endpoint; it degrades
    to empty ``suggestions`` plus an explanatory warning so the admin keeps their draft.
    """
    try:
        return configured_wording_suggestion_reviewer()
    except WordingSuggestionError as error:
        warnings.append(f"AI wording suggestion unavailable: {error}")
        return None
    except Exception as error:  # noqa: BLE001 - fail-soft on any configuration error.
        warnings.append(f"AI wording suggestion unavailable: {error}")
        return None
