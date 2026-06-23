"""Single source of truth for "which AI model does role X use?".

Every AI-calling feature in the app has a *role* (reviewer, verifier, structure
validation, ...). Historically each feature read its own ``NDA_*_MODEL`` env var
with its own built-in default, and three features (dashboard assistant, search
intent, matter summary) silently *rode* the reviewer's model. This module unifies
all of that behind a single resolver so an admin can pick a model per role from
the in-app settings UI without touching env vars.

Precedence (highest wins):

    persisted (``ai_models`` settings section) -> env var -> built-in default

The built-in defaults and env-var names are *imported lazily from the owning
modules* (see :func:`_role_registry`) so there is exactly one definition of, e.g.,
the reviewer default -- never a divergent copy. Lazy import is deliberate: the
owning modules import THIS module to call :func:`resolve_model`, so a top-level
import here would be circular.

The reviewer role is special: it must keep honouring the LEGACY ``ai_review``
"model" setting that the existing reviewer admin picker already writes, so that
unifying the resolver does not break the shipped reviewer UI. See
:func:`_legacy_reviewer_model`.
"""

from __future__ import annotations

from functools import lru_cache
import os
from typing import Callable, Dict, NamedTuple, Optional

from . import app_settings


# The eleven AI roles. Order here is the canonical UI order.
ROLES = (
    "reviewer",
    "verifier",
    "structure",
    "semantic_lint",
    "generation",
    "gmail_triage",
    "gmail_intake",
    "pdf_ocr",
    "dashboard_assistant",
    "search_intent",
    "matter_summary",
)


class RoleSpec(NamedTuple):
    """How to resolve one role's env/default layer.

    ``env_var``     -- the environment variable that overrides the built-in default.
    ``default``     -- the built-in default model id (the effective model with no
                       persisted setting and no env var set).
    ``legacy``      -- optional callable returning a legacy persisted model id that
                       sits BETWEEN the persisted ``ai_models`` layer and the env
                       layer. Only the reviewer uses this (the pre-existing
                       ``ai_review`` "model" setting).
    """

    env_var: str
    default: str
    legacy: Optional[Callable[[], str]] = None


def _legacy_reviewer_model() -> str:
    """The reviewer model persisted by the EXISTING reviewer admin picker.

    The legacy reviewer picker writes the ``ai_review`` settings section's
    ``model`` field (and pins ``provider=openrouter``). The status reader only
    treats that stored model as effective when its provider matches, so mirror
    that here: an ``ai_review`` model is only honoured when its provider is
    openrouter. Returns "" when there is no usable legacy model.
    """

    try:
        stored = app_settings.ai_settings()
    except Exception:  # noqa: BLE001 -- a store read error must not break resolution
        return ""
    provider = str(stored.get("provider") or "").strip().lower()
    if provider and provider != "openrouter":
        return ""
    return str(stored.get("model") or "").strip()


@lru_cache(maxsize=1)
def _role_registry() -> Dict[str, RoleSpec]:
    """Build the role -> (env var, default, legacy) map.

    Imports the owning modules lazily (inside the function) to avoid the import
    cycle described in the module docstring. Cached: the constants are import-time
    immutable, so this only runs once per process.
    """

    from . import (
        ai_review,
        ai_verifier,
        structure_validation,
        playbook_semantic_lint,
        nda_generation_ai,
        gmail_attachment_selector,
        gmail_intake_classifier,
        pdf_ocr,
    )

    reviewer_default = ai_review.DEFAULT_OPENROUTER_MODEL

    return {
        "reviewer": RoleSpec(
            env_var=ai_review.AI_REVIEW_ENV_MODEL,
            default=reviewer_default,
            legacy=_legacy_reviewer_model,
        ),
        "verifier": RoleSpec(
            env_var=ai_verifier.VERIFIER_ENV_MODEL,
            default=ai_verifier.DEFAULT_VERIFIER_MODEL,
        ),
        "structure": RoleSpec(
            env_var=structure_validation.STRUCTURE_VALIDATION_MODEL_ENV,
            default=structure_validation.STRUCTURE_VALIDATION_MODEL,
        ),
        "semantic_lint": RoleSpec(
            env_var=playbook_semantic_lint.SEMANTIC_LINT_MODEL_ENV,
            default=playbook_semantic_lint.SEMANTIC_LINT_MODEL,
        ),
        "generation": RoleSpec(
            env_var=nda_generation_ai.GENERATION_MODEL_ENV,
            default=nda_generation_ai.DEFAULT_GENERATION_MODEL,
        ),
        "gmail_triage": RoleSpec(
            env_var=gmail_attachment_selector.GMAIL_TRIAGE_MODEL_ENV,
            default=gmail_attachment_selector.DEFAULT_GMAIL_TRIAGE_MODEL,
        ),
        "gmail_intake": RoleSpec(
            env_var=gmail_intake_classifier.GMAIL_INTAKE_MODEL_ENV,
            default=gmail_intake_classifier.DEFAULT_GMAIL_INTAKE_MODEL,
        ),
        "pdf_ocr": RoleSpec(
            env_var=pdf_ocr.NDA_PDF_OCR_MODEL_ENV,
            default=pdf_ocr.DEFAULT_OCR_MODEL,
        ),
        # Decoupled from the reviewer (LOCKED design decision): each gets its own
        # env knob whose default EQUALS the reviewer's effective default, so
        # behaviour is unchanged until an admin overrides them.
        "dashboard_assistant": RoleSpec(
            env_var="NDA_DASHBOARD_ASSISTANT_MODEL",
            default=reviewer_default,
        ),
        "search_intent": RoleSpec(
            env_var="NDA_SEARCH_INTENT_MODEL",
            default=reviewer_default,
        ),
        "matter_summary": RoleSpec(
            env_var="NDA_MATTER_SUMMARY_MODEL",
            default=reviewer_default,
        ),
    }


def role_spec(role: str) -> RoleSpec:
    registry = _role_registry()
    try:
        return registry[role]
    except KeyError:
        raise KeyError(
            f"Unknown AI model role {role!r}; known roles: {', '.join(ROLES)}"
        ) from None


def _persisted_model(role: str) -> str:
    """The admin-picked model for ``role`` from the ``ai_models`` section, or ""."""

    try:
        models = app_settings.model_settings().get("models", {})
    except Exception:  # noqa: BLE001 -- a store read error must fall through, never crash
        return ""
    if not isinstance(models, dict):
        return ""
    return str(models.get(role) or "").strip()


def resolve_model(role: str) -> str:
    """Resolve the effective model id for ``role``.

    Precedence: persisted (``ai_models.<role>``) -> legacy (reviewer only) ->
    env var -> built-in default. Always returns a non-empty model id.
    """

    spec = role_spec(role)

    persisted = _persisted_model(role)
    if persisted:
        return persisted

    if spec.legacy is not None:
        legacy = spec.legacy()
        if legacy:
            return legacy

    env_model = os.environ.get(spec.env_var, "").strip()
    if env_model:
        return env_model

    return spec.default


class ResolvedModel(NamedTuple):
    role: str
    model: str
    source: str  # "persisted" | "env" | "default"  (legacy reports as "persisted")
    env_var: str
    default: str


def resolve_model_detail(role: str) -> ResolvedModel:
    """Like :func:`resolve_model` but also reports WHERE the value came from.

    Used by the admin GET to render per-role source badges. The legacy reviewer
    setting is reported as ``persisted`` (it IS a persisted admin choice, just in
    the older section), so the UI shows "admin override" not "env"/"default".
    """

    spec = role_spec(role)

    persisted = _persisted_model(role)
    if persisted:
        return ResolvedModel(role, persisted, "persisted", spec.env_var, spec.default)

    if spec.legacy is not None:
        legacy = spec.legacy()
        if legacy:
            return ResolvedModel(role, legacy, "persisted", spec.env_var, spec.default)

    env_model = os.environ.get(spec.env_var, "").strip()
    if env_model:
        return ResolvedModel(role, env_model, "env", spec.env_var, spec.default)

    return ResolvedModel(role, spec.default, "default", spec.env_var, spec.default)


# --- Recommended (known-good) model allowlist, per role -----------------------
#
# Drives the FE dropdown. Free-text "custom" is still allowed at save time as long
# as it passes ``ai_review.validate_model_slug`` -- this list is advisory, NOT an
# enforced whitelist. Kept deliberately short and curated (proven good for the job).

RECOMMENDED_MODELS_BY_ROLE: Dict[str, tuple] = {
    "reviewer": (
        "anthropic/claude-opus-4.8-fast",
        "z-ai/glm-5.2",
        "deepseek/deepseek-v4-pro",
    ),
    "verifier": (
        "deepseek/deepseek-v4-pro",
        "google/gemini-2.5-flash",
        "z-ai/glm-5.2",
    ),
    "structure": (
        "deepseek/deepseek-v4-flash",
        "google/gemini-2.5-flash",
    ),
    "semantic_lint": (
        "anthropic/claude-opus-4.8",
        "anthropic/claude-opus-4.8-fast",
        "deepseek/deepseek-v4-pro",
    ),
    "generation": (
        "deepseek/deepseek-v4-flash",
        "google/gemini-2.5-flash",
        "anthropic/claude-opus-4.8-fast",
    ),
    "gmail_triage": (
        "deepseek/deepseek-v4-pro",
        "anthropic/claude-opus-4.8-fast",
        "google/gemini-2.5-flash",
    ),
    "gmail_intake": (
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
        "google/gemini-2.5-flash",
    ),
    "pdf_ocr": (
        "google/gemini-2.5-flash",
        "google/gemini-2.5-pro",
    ),
    "dashboard_assistant": (
        "anthropic/claude-opus-4.8-fast",
        "deepseek/deepseek-v4-flash",
        "google/gemini-2.5-flash",
    ),
    "search_intent": (
        "anthropic/claude-opus-4.8-fast",
        "deepseek/deepseek-v4-flash",
        "google/gemini-2.5-flash",
    ),
    "matter_summary": (
        "anthropic/claude-opus-4.8-fast",
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
    ),
}


def recommended_models(role: str) -> list:
    return list(RECOMMENDED_MODELS_BY_ROLE.get(role, ()))


def role_model_overview() -> list:
    """The per-role payload the admin GET returns: effective / source / recommended.

    Shape per entry:
        {
          "role": "reviewer",
          "model": "anthropic/claude-opus-4.8-fast",   # effective
          "source": "persisted" | "env" | "default",
          "env_var": "NDA_AI_MODEL",
          "default": "anthropic/claude-opus-4.8-fast",
          "recommended": ["...", "..."],
        }
    """

    overview = []
    for role in ROLES:
        detail = resolve_model_detail(role)
        overview.append(
            {
                "role": role,
                "model": detail.model,
                "source": detail.source,
                "env_var": detail.env_var,
                "default": detail.default,
                "recommended": recommended_models(role),
            }
        )
    return overview


def _reset_caches_for_tests() -> None:
    """Drop the cached registry (env/const are import-time constant; cache is per-process)."""

    _role_registry.cache_clear()
