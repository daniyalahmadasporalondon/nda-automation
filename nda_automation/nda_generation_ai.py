"""AI-first clause adaptation for NDA generation (Playbook-bounded).

The generation engine (``nda_generation``) fills the template and stitches the
Playbook's authoritative clause wording into it. This module supplies the
optional AI ``ClauseAdapter`` that team-lead's AI-first directive calls for: the
AI *adapts* the Playbook wording to read naturally for the specific deal — it
does NOT decide positions. The split mirrors the review engine:

* The PLAYBOOK clause text is authoritative. The AI may rephrase for flow and
  weave in the deal context, but every substantive element of the position must
  survive.
* A GUARDRAIL checks the adapted text still contains the position's load-bearing
  terms; if the AI drifts (drops a term, pads it, refuses), the engine falls back
  to the deterministic Playbook wording. So AI failure degrades to the proven
  deterministic path, never to an off-position clause.
* The generated doc is still verified by the deterministic self-check
  (``self_check_generated_nda``) and gen-verify's independent drift check.

The provider plumbing reuses the project's OpenRouter client config
(``OPENROUTER_API_KEY`` / ``DEFAULT_OPENROUTER_MODEL``). With no key configured,
``build_clause_adapter`` returns ``None`` and generation runs fully deterministic.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping

from .ai_review import (
    DEFAULT_OPENROUTER_MODEL,
    OPENROUTER_API_KEY_ENV,
    _sanitize_model_name,
)
from .openrouter_usage import record_openrouter_usage
from .prohibited_positions import ANY_PROHIBITED_POSITION

OPENROUTER_CHAT_COMPLETIONS_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
# Per-call ceiling for a single clause adaptation. Generation is synchronous and
# adapts three clauses, so this is the unit that bounds how long a slow model can
# hold the request: a call that exceeds it raises ClauseAdaptationError, which the
# GuardedClauseAdapter degrades to the deterministic Playbook wording (a timeout
# is just another adapter failure, and the Playbook text is always on-position).
# Kept tight (was 30s) so one slow clause can't stall the whole generate request —
# the three calls also run in parallel (``adapt_clauses_in_parallel``), so the
# worst-case AI wall-time is roughly this ceiling, not 3x it.
DEFAULT_ADAPT_TIMEOUT_SECONDS = 12

# Per-clause load-bearing terms. The adapted text MUST still contain these (case-
# insensitive substring) or it is treated as drift and the deterministic Playbook
# wording is used instead. These are the substance the Playbook position turns on
# — not stylistic, so requiring them does not constrain phrasing, only position.
#
# These terms are a THIRD copy of wording that also lives in the Playbook clause
# templates (which the generator now reads live). To stop the copies silently
# diverging — e.g. someone rewords the Playbook ``standard_exclusions_template``
# away from "independently developed" so the guard rejects every adaptation —
# :func:`reconcile_required_terms` asserts at import that each required term is a
# substring of its source clause template.
CLAUSE_REQUIRED_TERMS: dict[str, tuple[str, ...]] = {
    "mutuality": ("each party", "disclosing party", "receiving party"),
    "confidential_information": ("independently developed",),
    "term_and_survival": ("trade secret", "data-protection"),
}

# Which Playbook clause-template field each clause's required terms are drawn
# from — mirrors the field the generator reads live for that clause
# (``_align_mutuality`` / ``_independent_development_sentence`` /
# ``_survival_sentence`` in nda_generation.py).
_REQUIRED_TERM_SOURCE_FIELD: dict[str, str] = {
    "mutuality": "redline_template",
    "confidential_information": "standard_exclusions_template",
    "term_and_survival": "redline_template",
}


def reconcile_required_terms(playbook: Mapping[str, Any]) -> None:
    """Assert each ``CLAUSE_REQUIRED_TERMS`` entry is in its source clause template.

    The required-terms table duplicates substance carried by the Playbook clause
    templates the generator reads live. If a Playbook edit moves the wording out
    from under a required term, the on-position guard would silently reject every
    legitimate adaptation for that clause. This makes that divergence loud instead:
    it raises ``AssertionError`` naming the clause, term, and source field.
    """

    clauses = {str(c.get("id")): c for c in playbook.get("clauses", [])}
    for clause_id, terms in CLAUSE_REQUIRED_TERMS.items():
        field_name = _REQUIRED_TERM_SOURCE_FIELD[clause_id]
        template = str((clauses.get(clause_id) or {}).get(field_name) or "").lower()
        for term in terms:
            assert term.lower() in template, (
                f"CLAUSE_REQUIRED_TERMS[{clause_id!r}] term {term!r} is not a substring of "
                f"the Playbook {clause_id}.{field_name}; the required-terms table has diverged "
                f"from the clause template and would reject legitimate adaptations."
            )


def _reconcile_required_terms_at_import() -> None:
    """Best-effort import-time reconciliation against the canonical Playbook.

    Runs the assertion against the on-disk Playbook so a divergence surfaces the
    moment this module is imported (the test suite imports it). Import is never
    blocked by an *unreadable* Playbook — only by an actual divergence — so a
    minimal environment without the Playbook file still imports cleanly.
    """

    try:
        from .checker import load_playbook  # noqa: PLC0415

        playbook = load_playbook()
    except Exception:  # noqa: BLE001 - missing/unreadable Playbook must not block import
        return
    reconcile_required_terms(playbook)


_reconcile_required_terms_at_import()

# Prohibited language the AI must never smuggle into ANY adapted clause — the
# restrictions the Playbook bans. If adapted text matches, the guard rejects it
# and keeps the Playbook wording. (The deterministic self-check, the pre-save ship
# gate, and gen-verify's meaning-based scan would also catch a leak, but the guard
# stops it from ever reaching the document — defence in depth.)
#
# The pattern set is the SHARED canonical one (prohibited_positions) so the guard,
# the ship gate, and gen-verify's gate never drift apart on what is off-position.
_PROHIBITED_PATTERN = ANY_PROHIBITED_POSITION


class ClauseAdaptationError(RuntimeError):
    """Raised when the AI adapter cannot produce usable adapted text."""


ProviderFn = Callable[[Mapping[str, Any]], str]


def build_clause_adapter(
    *,
    api_key: str | None = None,
    model: str | None = None,
    provider: ProviderFn | None = None,
):
    """Return a guarded AI clause adapter, or ``None`` if AI is not configured.

    ``provider`` is injectable for tests (a callable ``request -> text``). In
    production it defaults to the OpenRouter client when an API key is available;
    with no key, returns ``None`` so generation runs deterministically.
    """

    if provider is not None:
        return GuardedClauseAdapter(_CallableClauseAdapter(provider))

    import os

    resolved_key = (api_key or os.environ.get(OPENROUTER_API_KEY_ENV, "")).strip()
    if not resolved_key:
        return None
    return GuardedClauseAdapter(
        OpenRouterClauseAdapter(api_key=resolved_key, model=model or DEFAULT_OPENROUTER_MODEL)
    )


def adapt_clauses_in_parallel(
    adapter: Any,
    jobs: "list[tuple[str, str, Mapping[str, Any]]]",
    *,
    max_workers: int = 3,
) -> dict[str, str]:
    """Adapt several clauses CONCURRENTLY, returning ``{clause_id: adapted_text}``.

    The generation engine adapts three independent clauses; called serially that is
    three round-trips back-to-back (the dominant cost of an AI-on generate). Each
    clause's ``adapt`` is already self-contained and individually guarded (it falls
    back to its own ``base_text`` on any failure, timeout, or drift), so running
    them in parallel only overlaps the network waits — it changes timing, never the
    per-clause outcome. The result for ``clause_id`` is the adapter's output for
    that clause (already the deterministic base text when the adapter declined it).

    Robustness: a worker that raises despite the guard (it shouldn't — the guard
    swallows adapter errors) degrades to that clause's ``base_text`` rather than
    failing the whole generation. With a single job (or ``max_workers <= 1``) this
    runs inline, so the common test path stays synchronous and deterministic.
    """

    resolved: dict[str, str] = {}
    if not jobs:
        return resolved
    base_by_id = {clause_id: base_text for clause_id, base_text, _ in jobs}

    def _one(job: "tuple[str, str, Mapping[str, Any]]") -> "tuple[str, str]":
        clause_id, base_text, context = job
        try:
            out = adapter.adapt(clause_id, base_text, context)
        except Exception:  # noqa: BLE001 - defensive: the guard already handles this, but never fail the batch
            return clause_id, base_text
        text = (out or "").strip()
        return clause_id, (text or base_text)

    if len(jobs) == 1 or max_workers <= 1:
        for job in jobs:
            clause_id, text = _one(job)
            resolved[clause_id] = text
        return resolved

    import concurrent.futures as _futures  # noqa: PLC0415

    try:
        with _futures.ThreadPoolExecutor(max_workers=min(max_workers, len(jobs))) as executor:
            for clause_id, text in executor.map(_one, jobs):
                resolved[clause_id] = text
    except Exception:  # noqa: BLE001 - if the pool itself fails, fall back to base text for all
        for clause_id, base_text in base_by_id.items():
            resolved.setdefault(clause_id, base_text)
    return resolved


# --------------------------------------------------------------------------- #
# Frozen / golden-fixture adapter (repeatable AI-shaped output for the gate)
# --------------------------------------------------------------------------- #
# The live AI adapter is non-deterministic, so gen-verify cannot gate the exact
# AI-shaped document the product ships. The frozen adapter REPLAYS recorded,
# on-position adapted clause text from a committed JSON fixture, wrapped in the
# SAME GuardedClauseAdapter as the live path -- so the gate exercises the real
# guardrail/fallback machinery against a FIXED set of AI outputs. The result is a
# deterministic, network-free AI-path draft the gate can re-run identically.
#
# Fixture file (package data): nda_automation/fixtures/frozen_clause_adapter.json
#   {"clauses": {"<clause_id>": "<recorded adapted clause text>", ...}}
# A clause absent from the fixture replays as "" -> the guard keeps the Playbook
# wording (the same safe fallback as a live AI miss), so the fixture only needs to
# carry the clauses whose AI-shaped wording the gate wants to pin.
FROZEN_FIXTURE_RESOURCE = ("fixtures", "frozen_clause_adapter.json")


class FrozenClauseAdapter:
    """An inner adapter that replays recorded per-clause text (no network).

    Keyed by ``clause_id`` only: the recorded text is the on-position adaptation
    the gate pins, independent of deal context (the context-specific values --
    counterparty, purpose -- are filled by the template's variable slots, not the
    clause bodies the adapter rewrites, so a single recording per clause is the
    right grain). An unknown clause replays as "" so the guard falls back to the
    authoritative Playbook wording.
    """

    def __init__(self, recordings: Mapping[str, str]) -> None:
        self._recordings = {str(k): str(v) for k, v in dict(recordings).items()}

    def adapt(self, clause_id: str, playbook_text: str, context: Mapping[str, Any]) -> str:
        return self._recordings.get(clause_id, "")


def build_frozen_clause_adapter(
    *,
    recordings: Mapping[str, str] | None = None,
    fixtures_path: Any | None = None,
):
    """Return a GUARDED frozen adapter for a repeatable AI-shaped generation.

    ``recordings`` (clause_id -> adapted text) is injectable for tests; otherwise
    the committed golden fixture is loaded (from ``fixtures_path`` if given, else
    the package resource). The frozen adapter is wrapped in the same
    ``GuardedClauseAdapter`` as the live path so the guardrail still rejects any
    fixture that drifts off position -- the fixture cannot smuggle a bad clause
    past the gate, it would just fall back to the Playbook wording like a live miss.
    """

    if recordings is None:
        recordings = _load_frozen_recordings(fixtures_path)
    return GuardedClauseAdapter(FrozenClauseAdapter(recordings))


def _load_frozen_recordings(fixtures_path: Any | None = None) -> dict[str, str]:
    """Load the recorded clause adaptations from the golden fixture JSON."""

    if fixtures_path is not None:
        from pathlib import Path  # noqa: PLC0415

        raw = json.loads(Path(fixtures_path).read_text(encoding="utf-8"))
    else:
        import importlib.resources as resources  # noqa: PLC0415

        resource = resources.files("nda_automation").joinpath(*FROZEN_FIXTURE_RESOURCE)
        raw = json.loads(resource.read_text(encoding="utf-8"))
    clauses = raw.get("clauses") if isinstance(raw, Mapping) else None
    if not isinstance(clauses, Mapping):
        raise ClauseAdaptationError(
            "Frozen clause-adapter fixture is missing a 'clauses' object."
        )
    return {str(clause_id): str(text) for clause_id, text in clauses.items()}


class GuardedClauseAdapter:
    """Wraps an adapter so AI drift falls back to the Playbook wording.

    The inner adapter does the writing; this guard enforces the AI-first
    guardrail: the adapted text must retain the clause's load-bearing terms and
    stay within a sane length, else the authoritative ``playbook_text`` is kept.
    The guard is what makes "AI drives the writing, the Playbook bounds it" safe.
    """

    def __init__(self, inner: "ClauseAdapterImpl") -> None:
        self._inner = inner

    def adapt(self, clause_id: str, playbook_text: str, context: Mapping[str, Any]) -> str:
        try:
            adapted = self._inner.adapt(clause_id, playbook_text, context)
        except Exception:  # noqa: BLE001 - any adapter failure degrades to the deterministic path
            return playbook_text
        adapted = (adapted or "").strip()
        if not _adapted_text_is_on_position(clause_id, playbook_text, adapted):
            return playbook_text
        return adapted


def _adapted_text_is_on_position(clause_id: str, playbook_text: str, adapted: str) -> bool:
    """True iff the adapted text keeps the position (load-bearing terms + length)."""

    if not adapted:
        return False
    lowered = adapted.lower()
    for term in CLAUSE_REQUIRED_TERMS.get(clause_id, ()):  # noqa: SIM110
        if term.lower() not in lowered:
            return False
    # Reject any smuggled prohibited restriction (non-circ / non-solicit /
    # exclusivity) — unless the Playbook text itself already carries that wording
    # (it never does for these clauses, but compare so we only flag NEW additions).
    if _PROHIBITED_PATTERN.search(adapted) and not _PROHIBITED_PATTERN.search(playbook_text):
        return False
    # Reject runaway padding or truncation: the adapted clause should be in the
    # same ballpark as the Playbook wording (the AI rephrases, not rewrites scope).
    if len(adapted) > max(400, len(playbook_text) * 3):
        return False
    return True


class _CallableClauseAdapter:
    """Adapter backed by a plain ``request -> text`` callable (test seam)."""

    def __init__(self, provider: ProviderFn) -> None:
        self._provider = provider

    def adapt(self, clause_id: str, playbook_text: str, context: Mapping[str, Any]) -> str:
        request = build_adaptation_request(clause_id, playbook_text, context)
        return str(self._provider(request) or "")


class OpenRouterClauseAdapter:
    """Calls OpenRouter to adapt a Playbook clause to the deal, on-position only."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_OPENROUTER_MODEL,
        timeout_seconds: int = DEFAULT_ADAPT_TIMEOUT_SECONDS,
    ) -> None:
        cleaned_key = str(api_key or "").strip()
        if not cleaned_key:
            raise ClauseAdaptationError("OpenRouter API key is not configured.")
        self.api_key = cleaned_key
        self.model = _sanitize_model_name(model or DEFAULT_OPENROUTER_MODEL)
        self.timeout_seconds = max(1, int(timeout_seconds or DEFAULT_ADAPT_TIMEOUT_SECONDS))

    def adapt(self, clause_id: str, playbook_text: str, context: Mapping[str, Any]) -> str:
        body = _openrouter_request_body(
            build_adaptation_request(clause_id, playbook_text, context), model=self.model
        )
        request = urllib.request.Request(
            OPENROUTER_CHAT_COMPLETIONS_ENDPOINT,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "nda-automation/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            message = error.read().decode("utf-8", errors="replace")[:300]
            raise ClauseAdaptationError(f"OpenRouter returned HTTP {error.code}: {message}") from error
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise ClauseAdaptationError(f"OpenRouter request failed: {error}") from error
        record_openrouter_usage(payload, feature="generation", model=self.model)
        return _openrouter_response_text(payload)


def build_adaptation_request(
    clause_id: str, playbook_text: str, context: Mapping[str, Any]
) -> dict[str, Any]:
    """Build the strict, position-preserving adaptation instruction for a clause.

    The instruction is deliberately constraining: rephrase for the deal, preserve
    every substantive element, output only the clause text. The guardrail then
    independently checks the position survived — the prompt asks for compliance,
    the guard enforces it.
    """

    return {
        "clause_id": clause_id,
        "system": (
            "You are a legal drafting assistant adapting an authoritative NDA clause to a "
            "specific deal. You may rephrase for natural flow and weave in the parties and "
            "purpose, but you MUST preserve every substantive element of the clause's position "
            "— do not add, remove, weaken, or strengthen any obligation, carve-out, term, or "
            "exception. Do not introduce any new obligation (especially no non-circumvention, "
            "non-solicit, or exclusivity language). Output ONLY the adapted clause text, no "
            "preamble, no markdown."
        ),
        "playbook_text": playbook_text,
        "deal_context": {
            "counterparty": context.get("counterparty", ""),
            "purpose": context.get("purpose", ""),
            "nda_type": context.get("nda_type", "mutual"),
        },
    }


def _openrouter_request_body(request: Mapping[str, Any], *, model: str) -> dict[str, Any]:
    deal = request.get("deal_context", {})
    user = (
        f"Authoritative clause text:\n{request.get('playbook_text', '')}\n\n"
        f"Deal context: counterparty={deal.get('counterparty')!r}, "
        f"purpose={deal.get('purpose')!r}, nda_type={deal.get('nda_type')!r}.\n\n"
        "Return the adapted clause text only."
    )
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": request.get("system", "")},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
    }


def _openrouter_response_text(payload: Mapping[str, Any]) -> str:
    choices = payload.get("choices") if isinstance(payload, Mapping) else None
    if not choices:
        raise ClauseAdaptationError("OpenRouter response had no choices.")
    message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
    content = str((message or {}).get("content") or "").strip()
    if not content:
        raise ClauseAdaptationError("OpenRouter response had empty content.")
    # Strip any stray code fences / quotes a model might add despite instructions.
    content = re.sub(r"^```[a-zA-Z]*\n?|```$", "", content).strip()
    return content


# Structural type the guard wraps. Both concrete adapters satisfy it.
class ClauseAdapterImpl:  # pragma: no cover - typing aid
    def adapt(self, clause_id: str, playbook_text: str, context: Mapping[str, Any]) -> str: ...
