from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

from .. import ai_review, ai_verifier, app_settings, matter_store, model_resolver, telemetry
from ..deployment import _deployment_status_for_host, storage_durability_warning
from ..gmail_matter_inbox import ESIGN_NDA_CAPTURE_TRIAGE_REASON
from ..matter_repository import DiskMatterRepository, MatterRepositoryError
from ..review_engine import (
    REVIEW_ENGINE_AI_FIRST,
    REVIEW_ENGINE_DETERMINISTIC,
    active_review_engine_status,
)
from ..http_auth import _admin_user_ids
from .common import (
    request_actor,
    request_owner_user_id,
    request_user_email,
    request_user_provider,
    require_admin,
)

logger = logging.getLogger(__name__)


def handle_deployment_status(handler, *, send_body: bool = True) -> None:
    if not require_admin(handler, send_body=send_body):
        return
    handler._send_json(
        {"deployment": _deployment_status_for_host(str(handler.server.server_address[0]))},
        send_body=send_body,
    )


def handle_telemetry(handler, *, send_body: bool = True) -> None:
    if not require_admin(handler, send_body=send_body):
        return
    # Snapshot once so the health summary derives from the same counters the
    # caller sees (avoids a double snapshot / read race), then surface the
    # derived health block additively alongside the unchanged telemetry block.
    snapshot = telemetry.snapshot()
    counters = snapshot.get("counters", {})
    handler._send_json(
        {
            "telemetry": snapshot,
            "health": telemetry.health_summary(counters),
            # USD AI-spend rollup (per-feature), surfaced beside health. Admin-gated
            # by require_admin above -- spend/usage is never exposed to non-admins.
            "ai_cost": telemetry.ai_cost_summary(counters),
        },
        send_body=send_body,
    )


def handle_ai_availability(handler, *, send_body: bool = True) -> None:
    """GET /api/ai/availability -- a NON-admin, non-sensitive AI on/off read.

    Any authenticated user may USE the AI review (the review-refresh route is not
    admin-gated), but the full ``/api/ai/settings`` read is admin-only because it
    exposes provider/model/key-source config. The frontend still needs to know,
    for every user, whether AI is globally ready so it can render the correct
    "is AI usable?" signal instead of treating the admin 403 as "AI off".

    This endpoint returns ONLY the three derived booleans/strings the dashboard
    health badge needs and NOTHING sensitive: no API key (value OR source), no
    provider, no model, no settings/audit detail. ``active_engine`` is the
    selected engine name ("ai_first"/"deterministic"), which is not a secret.
    """
    status = ai_review.ai_review_status()
    engine = active_review_engine_status()
    handler._send_json(
        {
            "ai_enabled": bool(status.get("enabled")),
            "ai_configured": bool(status.get("api_key_configured")),
            "active_engine": str(engine.get("active_engine") or REVIEW_ENGINE_AI_FIRST),
        },
        send_body=send_body,
    )


def handle_ai_settings(handler, *, send_body: bool = True) -> None:
    if not require_admin(handler, send_body=send_body):
        return
    handler._send_json(
        {
            "ai_review": ai_review.ai_review_status(),
            "ai_verifier": ai_verifier.verifier_status(),
            "active_review_engine": active_review_engine_status(),
            # Per-role model picker payload: for every AI role, its effective model,
            # the source (persisted|env|default), the env var, the built-in default,
            # and the recommended-model allowlist that drives the FE dropdown.
            "ai_models": model_resolver.role_model_overview(),
            "operational_warnings": _operational_warnings(),
            "settings_audit": app_settings.settings_audit_history(),
        },
        send_body=send_body,
    )


def handle_personalisation_settings(handler, *, send_body: bool = True) -> None:
    if not require_admin(handler, send_body=send_body):
        return
    handler._send_json(
        {
            "personalisation": app_settings.personalisation_settings(),
            "defaults": app_settings.DEFAULT_PERSONALISATION_SETTINGS,
        },
        send_body=send_body,
    )


def handle_personalisation_settings_update(handler) -> None:
    if not require_admin(handler):
        return
    payload = handler._read_json_payload()
    if payload is None:
        return
    unsupported = sorted(set(payload) - {"sign_off", "signature", "signature_block"})
    if unsupported:
        handler._send_json({"error": f"Unsupported personalisation setting: {unsupported[0]}."}, status=400)
        return
    if not any(key in payload for key in ("sign_off", "signature", "signature_block")):
        handler._send_json({"error": "Provide a sign_off, signature, or signature_block setting to update."}, status=400)
        return
    if any(not isinstance(payload.get(key), str) for key in ("sign_off", "signature", "signature_block") if key in payload):
        handler._send_json({"error": "Personalisation settings must be text values."}, status=400)
        return

    previous = app_settings.personalisation_settings()
    personalisation = app_settings.update_personalisation_settings({
        key: payload[key]
        for key in ("sign_off", "signature", "signature_block")
        if key in payload
    })
    _record_personalisation_audit_if_changed(previous, personalisation)
    handler._send_json({
        "personalisation": personalisation,
        "defaults": app_settings.DEFAULT_PERSONALISATION_SETTINGS,
        "settings_audit": app_settings.settings_audit_history(),
    })


def handle_my_personalisation_settings(handler, *, send_body: bool = True) -> None:
    """GET /api/me/personalisation-settings -- the CALLER'S OWN personalisation.

    NON-admin accessible: any authenticated user reads their own effective
    signature/sign-off so they can customise it. Strictly scoped to the caller's
    own owner-user-id -- the response only ever reflects THIS user's override (or
    the inherited default when they have none), never another tenant's value.
    """
    owner_user_id = request_owner_user_id(handler)
    own = app_settings.user_personalisation_settings(owner_user_id)
    resolved = app_settings.resolved_personalisation_settings(owner_user_id)
    handler._send_json(
        {
            # The values that WILL be used on this user's outbound email.
            "personalisation": resolved,
            # True when the user has saved their own override; False => inheriting
            # the global/built-in default (so the UI can show "using default").
            "is_custom": own is not None,
            # The deployment/global default the user inherits when not customised.
            "global_default": app_settings.personalisation_settings(),
            "defaults": app_settings.DEFAULT_PERSONALISATION_SETTINGS,
        },
        send_body=send_body,
    )


def handle_my_personalisation_settings_update(handler) -> None:
    """POST /api/me/personalisation-settings -- save the CALLER'S OWN override.

    NON-admin accessible but strictly per-owner: the write only ever touches the
    caller's own slot (scoped by request_owner_user_id), so a user can never read
    or write another tenant's personalisation.
    """
    owner_user_id = request_owner_user_id(handler)
    if not owner_user_id:
        # An anonymous/unauthenticated caller has no private slot to write to.
        handler._send_json({"error": "A signed-in user is required to save personalisation."}, status=403)
        return
    payload = handler._read_json_payload()
    if payload is None:
        return
    unsupported = sorted(set(payload) - {"sign_off", "signature", "signature_block"})
    if unsupported:
        handler._send_json({"error": f"Unsupported personalisation setting: {unsupported[0]}."}, status=400)
        return
    if not any(key in payload for key in ("sign_off", "signature", "signature_block")):
        handler._send_json({"error": "Provide a sign_off, signature, or signature_block setting to update."}, status=400)
        return
    if any(not isinstance(payload.get(key), str) for key in ("sign_off", "signature", "signature_block") if key in payload):
        handler._send_json({"error": "Personalisation settings must be text values."}, status=400)
        return

    personalisation = app_settings.update_user_personalisation_settings(
        owner_user_id,
        {
            key: payload[key]
            for key in ("sign_off", "signature", "signature_block")
            if key in payload
        },
    )
    handler._send_json(
        {
            "personalisation": personalisation,
            "is_custom": True,
            "global_default": app_settings.personalisation_settings(),
            "defaults": app_settings.DEFAULT_PERSONALISATION_SETTINGS,
        }
    )


def handle_ai_settings_update(handler) -> None:
    if not require_admin(handler):
        return
    payload = handler._read_json_payload()
    if payload is None:
        return
    ai_updates = {}
    runtime_updates = {}
    runtime_noops = set()
    extra_response_warnings = []
    runtime_status = active_review_engine_status()
    if "enabled" in payload:
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            handler._send_json({"error": "AI enabled setting must be true or false."}, status=400)
            return
        # Enable-requires-key: never persist an "on-but-broken" state. Turning AI on
        # without a configured key produces silent review failures later with no
        # obvious cause, so reject the toggle until a key is present.
        if enabled and not ai_review.ai_review_status().get("api_key_configured"):
            handler._send_json(
                {"error": "Add a working OpenRouter API key before turning AI on."},
                status=409,
            )
            return
        ai_updates["enabled"] = enabled
    if "model" in payload:
        model = payload.get("model")
        if not isinstance(model, str) or not model.strip():
            handler._send_json({"error": "AI model must be a non-empty model id."}, status=400)
            return
        model = model.strip()
        if len(model) > 200:
            handler._send_json({"error": "AI model id is too long."}, status=400)
            return
        # Validate the slug against the PUBLIC OpenRouter catalog (no key needed) so a
        # mistyped model cannot be persisted and silently no-op / 400 at review time.
        catalog_status, catalog_message = ai_review.validate_model_slug(model)
        if catalog_status == "not_found":
            handler._send_json({"error": catalog_message}, status=400)
            return
        if catalog_status == "unverified":
            # Catalog unreachable: don't hard-block on a transient outage. Persist with
            # an explicit unverified-model warning rather than a false success.
            extra_response_warnings.append(
                {"code": "ai_model_unverified", "message": catalog_message}
            )
        ai_updates["model"] = model
        # OpenRouter is the sole provider; pin it so the saved model is the effective
        # model (the status reader only surfaces a stored model when its provider matches).
        ai_updates.setdefault("provider", "openrouter")
    if "active_review_engine" in payload:
        active_review_engine = _runtime_setting_value(payload.get("active_review_engine"))
        if active_review_engine != REVIEW_ENGINE_AI_FIRST:
            handler._send_json({"error": "Active review engine must be ai_first."}, status=400)
            return
        pinned_engine = runtime_status.get("environment_active_engine")
        if pinned_engine:
            if active_review_engine != pinned_engine:
                telemetry.increment("review_runtime_update_blocked_environment")
                handler._send_json({"error": "Active review engine is pinned by the backend environment."}, status=409)
                return
            runtime_noops.add("active_review_engine")
        else:
            runtime_updates["active_review_engine"] = active_review_engine
    if not ai_updates and not runtime_updates and not runtime_noops:
        handler._send_json({"error": "Provide an AI or runtime review setting to update."}, status=400)
        return
    previous_ai_settings = app_settings.ai_settings()
    previous_runtime_settings = app_settings.review_runtime_settings()
    if ai_updates:
        telemetry.increment("ai_settings_updates")
        app_settings.update_ai_settings(ai_updates)
    if runtime_updates:
        telemetry.increment("review_runtime_settings_updates")
        app_settings.update_review_runtime_settings(runtime_updates)
    _record_settings_audit_if_changed(
        "admin_settings_update",
        previous_ai_settings=previous_ai_settings,
        previous_runtime_settings=previous_runtime_settings,
    )
    handler._send_json({
        "ai_review": ai_review.ai_review_status(),
        "ai_verifier": ai_verifier.verifier_status(),
        "active_review_engine": active_review_engine_status(),
        "operational_warnings": _operational_warnings() + extra_response_warnings,
        "settings_audit": app_settings.settings_audit_history(),
    })


def handle_ai_models_update(handler) -> None:
    """POST /api/ai/models -- set (or clear) the model for one or more AI roles.

    Request body: ``{"models": {"<role>": "<model id>", ...}}``. Each role:
      - must be a known role (model_resolver.ROLES) else 400;
      - a non-empty model id is validated against the live OpenRouter catalog via
        ``ai_review.validate_model_slug``: ``not_found`` -> 400 (reject the whole
        request, persist nothing); ``unverified`` (catalog down) -> persist + WARN
        so a transient outage can't lock saves;
      - an empty string / null CLEARS the override (falls back to env/default).

    Admin-gated (require_admin). On success persists via update_model_settings and
    records a per-role settings-audit event stamped with the real actor.
    """

    if not require_admin(handler):
        return
    payload = handler._read_json_payload()
    if payload is None:
        return

    raw_models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(raw_models, dict) or not raw_models:
        handler._send_json(
            {"error": "Provide a non-empty 'models' object of {role: model id}."},
            status=400,
        )
        return

    known_roles = set(model_resolver.ROLES)
    updates: dict[str, object] = {}
    extra_response_warnings: list[dict[str, str]] = []

    for role, value in raw_models.items():
        if not isinstance(role, str) or role.strip() not in known_roles:
            handler._send_json(
                {"error": f"Unknown AI model role '{role}'."},
                status=400,
            )
            return
        role = role.strip()

        # Null / blank => clear the override (fall back to env/default).
        if value is None or (isinstance(value, str) and not value.strip()):
            updates[role] = None
            continue

        if not isinstance(value, str):
            handler._send_json(
                {"error": f"Model id for role '{role}' must be a string."},
                status=400,
            )
            return
        model = value.strip()
        if len(model) > app_settings.MAX_MODEL_ID_LENGTH:
            handler._send_json(
                {"error": f"Model id for role '{role}' is too long."},
                status=400,
            )
            return

        # Validate against the PUBLIC OpenRouter catalog (no key needed). A mistyped
        # slug must never persist + silently no-op at call time.
        catalog_status, catalog_message = ai_review.validate_model_slug(model)
        if catalog_status == "not_found":
            handler._send_json(
                {"error": f"{role}: {catalog_message}"},
                status=400,
            )
            return
        if catalog_status == "unverified":
            # Catalog unreachable: don't hard-block on a transient outage. Persist
            # with an explicit unverified-model warning rather than a false success.
            extra_response_warnings.append(
                {"code": "ai_model_unverified", "message": f"{role}: {catalog_message}"}
            )
        updates[role] = model

    previous_models = app_settings.model_settings().get("models", {})
    telemetry.increment("ai_models_updates")
    app_settings.update_model_settings(updates)
    current_models = app_settings.model_settings().get("models", {})
    _record_model_settings_audit_if_changed(handler, previous_models, current_models)

    handler._send_json({
        "ai_models": model_resolver.role_model_overview(),
        "operational_warnings": _operational_warnings() + extra_response_warnings,
        "settings_audit": app_settings.settings_audit_history(),
    })


def _record_model_settings_audit_if_changed(
    handler,
    previous_models: dict,
    current_models: dict,
) -> None:
    """Append a settings-audit event when any per-role model changed.

    Stamped with the REAL actor (request_actor) so "who changed which role to which
    model" is recoverable -- unlike the generic ai_review audit which stamps "admin".
    """

    changes = []
    for role in sorted(set(previous_models) | set(current_models)):
        before = previous_models.get(role)
        after = current_models.get(role)
        if before != after:
            changes.append({"setting": f"ai_models.{role}", "before": before, "after": after})
    if not changes:
        return
    telemetry.increment("settings_audit_events")
    app_settings.record_settings_audit_event({
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "actor": request_actor(handler),
        "action": "ai_models_update",
        "changes": changes,
    })


def handle_ai_api_key_update(handler) -> None:
    if not require_admin(handler):
        return
    payload = handler._read_json_payload()
    if payload is None:
        return
    api_key = payload.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        handler._send_json({"error": "Provide an AI API key to save."}, status=400)
        return
    provider = ai_review.provider_for_api_key(api_key)
    telemetry.increment("ai_api_key_save_requests")

    # Validate the key against OpenRouter BEFORE persisting. A wrong/expired/
    # zero-quota key must NEVER save cleanly and flip AI to "on" — that leaves a
    # false "all good" state where every later review fails at call time. We only
    # persist (and only enable) on a confirmed 200. A rejected key (401/403) is a
    # hard 400; a transient blip (network/timeout/5xx) is a 503 and ALSO does not
    # persist, so an unverified key can never turn AI on. Either way the previous
    # good key/state is left untouched.
    validation = ai_review.validate_api_key(api_key)
    if validation.status == "rejected":
        telemetry.increment("ai_api_key_save_rejected")
        handler._send_json({"error": validation.message}, status=400)
        return
    if not validation.is_valid:
        telemetry.increment("ai_api_key_save_unverified")
        handler._send_json({"error": validation.message}, status=503)
        return

    previous_ai_settings = app_settings.ai_settings()
    previous_runtime_settings = app_settings.review_runtime_settings()
    app_settings.save_ai_api_key(api_key)
    app_settings.update_ai_settings({
        "provider": provider,
        "model": ai_review.default_model_for_provider(provider),
    })
    if payload.get("enabled", True) is not False:
        app_settings.update_ai_settings({"enabled": True})
    _record_settings_audit_if_changed(
        "ai_api_key_saved",
        previous_ai_settings=previous_ai_settings,
        previous_runtime_settings=previous_runtime_settings,
        extra_changes=[{"setting": "ai_review.api_key", "before": "", "after": "saved"}],
    )
    handler._send_json({
        "ai_review": ai_review.ai_review_status(),
        "ai_verifier": ai_verifier.verifier_status(),
        "active_review_engine": active_review_engine_status(),
        "operational_warnings": _operational_warnings(),
        "settings_audit": app_settings.settings_audit_history(),
    })


def handle_ai_api_key_clear(handler) -> None:
    if not require_admin(handler):
        return
    telemetry.increment("ai_api_key_clear_requests")
    previous_ai_settings = app_settings.ai_settings()
    previous_runtime_settings = app_settings.review_runtime_settings()
    had_key = bool(app_settings.stored_ai_api_key())
    app_settings.clear_ai_api_key()
    _record_settings_audit_if_changed(
        "ai_api_key_cleared",
        previous_ai_settings=previous_ai_settings,
        previous_runtime_settings=previous_runtime_settings,
        extra_changes=[{"setting": "ai_review.api_key", "before": "saved", "after": ""}] if had_key else [],
    )
    handler._send_json({
        "ai_review": ai_review.ai_review_status(),
        "ai_verifier": ai_verifier.verifier_status(),
        "active_review_engine": active_review_engine_status(),
        "operational_warnings": _operational_warnings(),
        "settings_audit": app_settings.settings_audit_history(),
    })


def _runtime_setting_value(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _record_settings_audit_if_changed(
    action: str,
    *,
    previous_ai_settings: dict,
    previous_runtime_settings: dict,
    extra_changes: list[dict] | None = None,
) -> None:
    changes = list(extra_changes or [])
    current_ai_settings = app_settings.ai_settings()
    current_runtime_settings = app_settings.review_runtime_settings()
    for key in ("enabled", "provider", "model"):
        before = previous_ai_settings.get(key)
        after = current_ai_settings.get(key)
        if before != after:
            changes.append({"setting": f"ai_review.{key}", "before": before, "after": after})
    for key in ("active_review_engine",):
        before = previous_runtime_settings.get(key)
        after = current_runtime_settings.get(key)
        if before != after:
            changes.append({"setting": f"review_runtime.{key}", "before": before, "after": after})
    if not changes:
        return
    telemetry.increment("settings_audit_events")
    app_settings.record_settings_audit_event({
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "actor": "admin",
        "action": action,
        "changes": changes,
    })


def _record_personalisation_audit_if_changed(previous: dict, current: dict) -> None:
    changes = []
    for key in ("sign_off", "signature", "signature_block"):
        before = previous.get(key)
        after = current.get(key)
        if before != after:
            changes.append({"setting": f"personalisation.{key}", "before": before, "after": after})
    if not changes:
        return
    telemetry.increment("settings_audit_events")
    app_settings.record_settings_audit_event({
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "actor": "admin",
        "action": "personalisation_settings_update",
        "changes": changes,
    })


def handle_admin_list(handler, *, send_body: bool = True) -> None:
    """GET /api/admin/admins -- the immutable env roots + the persisted grant list."""
    if not require_admin(handler, send_body=send_body):
        return
    handler._send_json(_admin_list_payload(handler), send_body=send_body)


def handle_admin_add(handler) -> None:
    """POST /api/admin/admins/add {email} -- idempotently grant an admin email."""
    if not require_admin(handler):
        return
    payload = handler._read_json_payload()
    if payload is None:
        return
    raw_email = payload.get("email")
    if not isinstance(raw_email, str) or not raw_email.strip():
        handler._send_json({"error": "An email address is required."}, status=400)
        return
    if len(raw_email.strip()) > app_settings.MAX_ADMIN_EMAIL_LENGTH:
        handler._send_json({"error": "Email address is too long."}, status=400)
        return
    email = app_settings.normalize_admin_email(raw_email)
    if not email:
        handler._send_json({"error": "Enter a valid email address (local@domain)."}, status=400)
        return
    if email in _env_root_admin_ids():
        handler._send_json(
            {"error": "That address is a bootstrap admin set in the environment and is managed there."},
            status=409,
        )
        return

    current = app_settings.admin_settings()["admins"]
    if any(entry.get("email") == email for entry in current):
        # Idempotent: the address is already a persisted admin, so return the
        # current list unchanged (no duplicate, no audit churn).
        handler._send_json(_admin_list_payload(handler), status=200)
        return

    actor = request_user_email(handler) or request_actor(handler)
    updated = [
        *current,
        {
            "email": email,
            "added_at": datetime.now(timezone.utc).isoformat(),
            "added_by": actor,
        },
    ]
    app_settings.update_admin_settings({"admins": updated})
    _record_admin_audit("admin_added", actor=actor, email=email)
    handler._send_json(_admin_list_payload(handler), status=200)


def handle_admin_remove(handler) -> None:
    """DELETE /api/admin/admins {email} -- revoke a persisted admin grant.

    Env roots are immutable (409). Removing the LAST admin (no env roots and no
    other persisted admin would remain) is refused (409) so the app can never be
    locked out. An unknown email is a 404.
    """
    if not require_admin(handler):
        return
    payload = handler._read_json_payload()
    if payload is None:
        return
    raw_email = payload.get("email")
    if not isinstance(raw_email, str) or not raw_email.strip():
        handler._send_json({"error": "An email address is required."}, status=400)
        return
    email = app_settings.normalize_admin_email(raw_email)
    if not email:
        handler._send_json({"error": "Enter a valid email address (local@domain)."}, status=400)
        return
    if email in _env_root_admin_ids():
        handler._send_json(
            {"error": "Bootstrap admins set in the environment cannot be removed here."},
            status=409,
        )
        return

    current = app_settings.admin_settings()["admins"]
    if not any(entry.get("email") == email for entry in current):
        handler._send_json({"error": "That admin is not in the persisted list."}, status=404)
        return

    remaining = [entry for entry in current if entry.get("email") != email]
    # Lockout guard: refuse if removing this entry would leave ZERO admins total
    # (no env roots AND no persisted admins left).
    if not remaining and not _env_root_admin_ids():
        handler._send_json(
            {"error": "Cannot remove the last administrator. Add another admin first."},
            status=409,
        )
        return

    actor = request_user_email(handler) or request_actor(handler)
    app_settings.update_admin_settings({"admins": remaining})
    _record_admin_audit("admin_removed", actor=actor, email=email)
    handler._send_json(_admin_list_payload(handler), status=200)


def _env_root_admin_ids() -> set[str]:
    """The immutable NDA_ADMIN_USERS env set (verbatim entries, case-sensitive)."""
    return _admin_user_ids()


def _admin_list_payload(handler=None) -> dict[str, object]:
    return {
        "env_root_admins": _env_root_admin_view(handler),
        "persisted_admins": app_settings.admin_settings()["admins"],
    }


def _env_root_admin_view(handler=None) -> list[dict[str, object]]:
    """Enrich each immutable NDA_ADMIN_USERS entry for human-readable display.

    DISPLAY-ONLY: the authorization model is untouched -- admin matching still
    happens verbatim by ``google:<sub>`` / email in ``request_is_admin``. This
    only annotates the SAME entries so the Admin Access surface can show a name
    or email instead of an opaque ``google:101508195488490085718``.

    For each entry we emit ``{id, kind, email, display, is_self}``:
      * ``id``   -- the raw verbatim env entry (still the secondary/tooltip text).
      * ``kind`` -- "email" when the entry itself is an email; "google" for a
        ``google:<sub>`` opaque id; "opaque" otherwise (e.g. a basic-auth name).
      * ``email`` -- a known address: the entry itself when email-shaped, OR the
        current session's OAuth-verified email when THIS root is the caller.
      * ``display`` -- the friendly primary label the frontend prefers.
      * ``is_self`` -- True when this root matches the current session identity
        (the frontend tags it "(you)").

    The frontend stays backward compatible: it also accepts a plain string entry
    (an older cached payload), so this enrichment is additive.
    """
    self_user_id = ""
    self_email = ""
    self_email_normalized = ""
    self_provider = ""
    self_name = ""
    if handler is not None:
        self_user_id = request_owner_user_id(handler)
        self_email = request_user_email(handler)
        self_provider = (request_user_provider(handler) or "").strip().lower()
        self_email_normalized = app_settings.normalize_admin_email(self_email)
        current_user = getattr(handler, "current_user", None)
        if isinstance(current_user, dict):
            self_name = str(current_user.get("name") or "").strip()

    view: list[dict[str, object]] = []
    for entry in sorted(_env_root_admin_ids()):
        entry_email = app_settings.normalize_admin_email(entry)
        is_google = entry.startswith("google:")
        # Does this env root resolve to the current session? Either a verbatim id
        # match (covers google:<sub> and basic-auth names) OR, for a Google
        # session only, a normalized-email match -- the SAME two paths the auth
        # predicate uses. The email path is provider-gated so a basic-auth name
        # that merely equals an admin email is never tagged "(you)".
        is_self = bool(
            (self_user_id and entry == self_user_id)
            or (
                self_provider == "google"
                and entry_email
                and self_email_normalized
                and entry_email == self_email_normalized
            )
        )
        # When THIS root is the caller, the friendliest known email is the
        # session's verified address (normalized to match how admin emails are
        # stored/compared); fall back to the raw verified email if it somehow
        # fails normalization.
        self_known_email = (self_email_normalized or self_email) if is_self else ""
        if entry_email:
            kind = "email"
            email = entry_email
        elif is_google:
            kind = "google"
            email = self_known_email
        else:
            kind = "opaque"
            email = self_known_email

        # Choose the friendliest primary label we can justify.
        if email:
            display = email
        elif is_google:
            # A bare google:<sub> with no known email: a stable, friendlier form
            # than the raw 21-digit subject -- the full id stays in `id`.
            sub = entry[len("google:") :]
            tail = sub[-6:] if len(sub) >= 6 else sub
            display = f"Google account ···{tail}" if tail else "Google account"
        else:
            display = entry

        view.append(
            {
                "id": entry,
                "kind": kind,
                "email": email,
                "display": display,
                "name": self_name if is_self else "",
                "is_self": is_self,
            }
        )
    return view


def _record_admin_audit(action: str, *, actor: str, email: str) -> None:
    telemetry.increment("settings_audit_events")
    app_settings.record_settings_audit_event({
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "actor": actor or "admin",
        "action": action,
        "changes": [{"setting": "admins.email", "before": "", "after": email}],
    })


def _operational_warnings() -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    # Non-durable-storage warning FIRST: it is the most consequential (every
    # publish/entity save silently reverts on the next redeploy), so it leads the
    # operator's attention. Fires only on POSITIVE non-durability evidence (a proven
    # wipe or an ephemeral NDA_DATA_DIR path), never on a healthy fresh deploy.
    durability_warning = storage_durability_warning()
    if durability_warning is not None:
        warnings.append(durability_warning)
    ai_status = ai_review.ai_review_status()
    verifier_status = ai_verifier.verifier_status()
    runtime_status = active_review_engine_status()
    if runtime_status.get("active_engine") == REVIEW_ENGINE_AI_FIRST and not ai_status.get("api_key_configured"):
        warnings.append({
            "code": "ai_first_without_key",
            "message": "AI-first is active but no AI API key is configured.",
        })
    if runtime_status.get("environment_active_engine"):
        warnings.append({
            "code": "active_engine_environment_pinned",
            "message": "Active review engine is pinned by the backend environment.",
        })
    if verifier_status.get("enabled") and verifier_status.get("active_kind") == "noop":
        warnings.append({
            "code": "ai_verifier_inactive_no_key",
            "message": "AI verifier is enabled but no OpenRouter key is configured, so it is inactive (a no-op that changes no verdicts). Configure an OpenRouter key for DeepSeek verification.",
        })
    stored_key_migration = ai_status.get("stored_key_migration")
    if isinstance(stored_key_migration, dict) and stored_key_migration.get("message"):
        warnings.append({
            "code": str(stored_key_migration.get("code") or "stored_key_migration"),
            "message": str(stored_key_migration["message"]),
        })
    return warnings


def handle_pdf_docx_backfill_start(handler) -> None:
    """POST: start the one-time PDF->working-DOCX backfill on a background thread.

    CONVERT ONLY -- this NEVER triggers an AI review (see ingestion_service.
    run_pdf_docx_backfill, which calls only the guarded PDF->DOCX converter). Admin-gated
    + CSRF-protected (do_POST enforces CSRF before dispatch). Returns 202 immediately; the
    serial conversion loop runs off the request thread so the request never blocks.
    """
    if not require_admin(handler):
        return
    from .. import ingestion_service  # noqa: PLC0415 - keep the import local/light.

    # Optional {"limit": N} to bound a single run. An absent body (Content-Length 0)
    # yields {}; a malformed body sends its own 400 (payload is None) and we stop.
    payload = handler._read_json_payload()
    if payload is None:
        return
    limit = None
    raw_limit = payload.get("limit")
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            handler._send_json({"error": "limit must be a non-negative integer."}, status=400)
            return
        if limit < 0:
            handler._send_json({"error": "limit must be a non-negative integer."}, status=400)
            return
    telemetry.increment("pdf_docx_backfill_requests")
    result = ingestion_service.start_pdf_docx_backfill_async(limit=limit)
    handler._send_json(
        {
            "started": bool(result.get("started")),
            "already_running": bool(result.get("already_running")),
            "run_id": result.get("run_id", ""),
            "status": ingestion_service.pdf_docx_backfill_status(),
        },
        status=202,
    )


def handle_pdf_docx_backfill_status(handler, *, send_body: bool = True) -> None:
    """GET: the latest / in-flight PDF->DOCX backfill tally (cheap; no re-scan)."""
    if not require_admin(handler, send_body=send_body):
        return
    from .. import ingestion_service  # noqa: PLC0415 - keep the import local/light.

    handler._send_json(
        {"status": ingestion_service.pdf_docx_backfill_status()},
        send_body=send_body,
    )


def handle_matters_garble_backfill(handler) -> None:
    """POST /api/admin/matters/garble-backfill — heal glyph-garbled PDF extractions.

    Matters imported before the per-glyph extraction fix
    (``pdf_text._chunks_are_glyph_fragmented``) carry garbled stored text (stacked
    one-char paragraphs + space-joined glyph fragments). This re-extracts those
    documents' RETAINED original bytes through the fixed extractor and swaps the
    stored ``extracted_text`` — nothing else. Admin-gated; CSRF/auth/host/rate-limit
    are enforced centrally by server.do_POST before dispatch (registered in
    _POST_EXACT_ROUTES like every sibling write).

    Body: ``{"dry_run": default true, "confirm": must be exactly true for an
    execute run, "limit": per-invocation cap (default 50, max 200)}``.

    * DRY-RUN (default) is detection-only: a per-matter report (matter id, doc,
      fingerprint hit counts, would_reextract) with NO writes, NO byte reads and
      no re-extraction.
    * EXECUTE (``dry_run: false``) additionally requires ``"confirm": true``
      (missing/mistyped confirm is a 400 and nothing runs). Serial, capped,
      atomic per matter; missing source bytes are reported + skipped; the
      existing review-staleness contract flags healed matters' stored reviews as
      ``matter_text_changed`` — reviews/redlines/decisions are never touched and
      NO review is enqueued (no AI calls anywhere on this path).
    """
    if not require_admin(handler):
        return
    from .. import garble_backfill  # noqa: PLC0415 - keep the import local/light.

    payload = handler._read_json_payload()
    if payload is None:
        return

    dry_run = payload.get("dry_run", True)
    if not isinstance(dry_run, bool):
        handler._send_json({"error": "dry_run must be true or false."}, status=400)
        return

    raw_limit = payload.get("limit", garble_backfill.GARBLE_BACKFILL_DEFAULT_LIMIT)
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        limit = 0
    if isinstance(raw_limit, bool) or limit < 1 or limit > garble_backfill.GARBLE_BACKFILL_MAX_LIMIT:
        handler._send_json(
            {"error": f"limit must be an integer between 1 and {garble_backfill.GARBLE_BACKFILL_MAX_LIMIT}."},
            status=400,
        )
        return

    if not dry_run and payload.get("confirm") is not True:
        # Execute demands an EXPLICIT boolean confirm on top of dry_run:false —
        # a missing/truthy-string confirm never mutates.
        handler._send_json(
            {"error": 'Executing the garble backfill requires "confirm": true alongside "dry_run": false.'},
            status=400,
        )
        return

    telemetry.increment("garble_backfill_requests")
    try:
        report = garble_backfill.run_garble_backfill(dry_run=dry_run, limit=limit)
    except matter_store.MatterStoreError as error:
        logger.warning("Garble backfill failed: %s", error)
        handler._send_json({"error": matter_store.friendly_matter_store_message(error)}, status=500)
        return
    handler._send_json(report)


def handle_matter_backup(handler, *, send_body: bool = True) -> None:
    # The backup dumps full extracted NDA text and matter metadata, so it is an
    # admin-only endpoint on top of the per-owner scoping applied below.
    if not require_admin(handler, send_body=send_body):
        return
    telemetry.increment("matter_backup_requests")
    # Admin-only ``?owner=<owner_user_id>`` override: the operator can back up a
    # SPECIFIC user's matters (e.g. before a bulk archive of that user's
    # auto-imported Gmail noise), or EVERYTHING with the ``__all__`` sentinel.
    # The gate above already established admin, so this never widens access;
    # absent the param the backup stays scoped to the caller exactly as before.
    backup_owner = request_owner_user_id(handler)
    try:
        query = parse_qs(urlparse(str(getattr(handler, "path", "") or "")).query)
        owner_override = str((query.get("owner") or [""])[0] or "").strip()
    except (ValueError, TypeError):
        owner_override = ""
    export_all = owner_override == "__all__"
    if owner_override and not export_all:
        backup_owner = owner_override
    try:
        repository = DiskMatterRepository()
        if export_all:
            # Disaster-recovery dump: EVERY matter regardless of owner,
            # ownerless (owner_user_id == "") included.
            backup = repository.export_all_matters_backup()
        else:
            backup = repository.export_matters_backup(owner_user_id=backup_owner)
    except MatterRepositoryError as error:
        handler._send_json({"error": str(error)}, status=500, send_body=send_body)
        return
    # Self-describing scope marker so a dump on disk can always be told apart
    # from a single-owner export (matter_count is already in the payload).
    backup["scope"] = "all-owners" if export_all else "single-owner"
    backup["owner"] = None if export_all else (backup_owner or None)
    data = json.dumps(backup, indent=2).encode("utf-8") + b"\n"
    exported_at = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    handler._send_download(
        data,
        f"nda-matters-backup-{exported_at}.json",
        "application/json",
        headers={"X-Backup-Contains": "matter-json"},
        send_body=send_body,
    )


# --------------------------------------------------------------------------- #
# Admin bulk archive: auto-imported Gmail noise
# --------------------------------------------------------------------------- #
# Hard ceiling on a single run's batch (the request ``limit`` may lower it).
BULK_ARCHIVE_MAX_LIMIT = 1000
BULK_ARCHIVE_DEFAULT_LIMIT = 200
# How many excluded matters are echoed back (id + reason only) per response.
BULK_ARCHIVE_EXCLUDED_SAMPLE_CAP = 25
BULK_ARCHIVE_AUDIT_FILENAME = "bulk-archive-audit.log"


def _parse_iso_datetime(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp; naive values are taken as UTC. None on failure."""
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _pristine_deterministic_review(review_result: object) -> bool:
    """True only when the stored review is provably no-more-than-import-time.

    Two (and only two) shapes qualify:

    * NO review at all — the deferred-import default (``create_matter_from_document``
      with ``defer_ai_review=True`` persists ``review_result: None``), i.e. the
      "Not Reviewed Yet" state every Gmail poll import is born in; or
    * a review whose ``active_review_engine`` metadata (written by
      ``review_engine._with_active_engine_metadata``) says the DETERMINISTIC
      engine was both selected and executed, with no ai_first trace anywhere
      (the on-demand human "Review" path pins ``force_engine=ai_first``, so a
      deterministic result can only be an import-time first pass).

    Anything else — ai_first metadata, an ``ai_first_review`` block, missing or
    unrecognized engine metadata — fails CLOSED (not pristine).
    """
    if review_result is None or review_result == {}:
        return True
    if not isinstance(review_result, dict):
        return False
    if review_result.get("ai_first_review") is not None:
        return False
    engine_metadata = review_result.get("active_review_engine")
    if not isinstance(engine_metadata, dict):
        return False
    for key in ("selected_engine", "executed_engine", "engine"):
        if str(engine_metadata.get(key) or "") != REVIEW_ENGINE_DETERMINISTIC:
            return False
    if REVIEW_ENGINE_AI_FIRST in str(engine_metadata.get("ai_first_status") or ""):
        return False
    return True


def _only_system_intake_artifacts(matter: dict[str, Any]) -> bool:
    """True when the matter's artifact registry is no more than the intake backfill.

    VERIFIED SHAPE (probe of the real deferred gmail import path at this SHA):
    ``matter_lifecycle.complete_intake._register_original_artifact`` runs on EVERY
    fresh import and registers exactly ONE ``role == "original"`` artifact with
    ``current_artifact_id`` pointing at it — so "artifacts present" alone is NOT
    human engagement. Human/AI work always ADDS artifacts (redline, reviewed,
    sent, signed, counter, ...) or re-points the current pointer. Allowed shapes:

    * no artifacts and no current pointer (a fail-soft intake whose backfill
      errored), or
    * exactly one ``original`` artifact, with the current pointer absent or
      pointing at it.

    Anything else fails CLOSED.
    """
    artifacts = matter.get("artifacts")
    current_id = str(matter.get("current_artifact_id") or "").strip()
    if not artifacts:
        return not current_id
    if not isinstance(artifacts, list) or len(artifacts) != 1:
        return False
    artifact = artifacts[0]
    if not isinstance(artifact, dict):
        return False
    if str(artifact.get("role") or "") != "original":
        return False
    return current_id in ("", str(artifact.get("id") or ""))


def _only_system_timeline_events(matter: dict[str, Any]) -> bool:
    """True when every timeline event was stamped by the system (or none exist).

    The intake hooks append system-actor events on every import; any event with
    a different (or missing) actor is evidence of human-adjacent workflow
    activity (approval, send, manual mark-executed, ...) and fails CLOSED.
    """
    timeline = matter.get("matter_timeline")
    if not timeline:
        return True
    if not isinstance(timeline, list):
        return False
    for event in timeline:
        if not isinstance(event, dict) or str(event.get("actor") or "") != "system":
            return False
    return True


def _bulk_archive_exclusion_reason(
    matter: dict[str, Any],
    *,
    owner_user_id: str,
    created_after: datetime,
    created_before: datetime,
) -> str | None:
    """Why ``matter`` must NOT be bulk-archived, or None when it is selectable.

    FAIL-CLOSED: every rule treats a missing/unknown/odd-shaped field as
    disqualifying. Only a matter that positively proves it is an untouched
    auto-imported Gmail card (inside the requested window, owned by exactly the
    requested user) comes back None.

    KNOWN BLIND SPOT (verified, deliberate): a human touch that was fully
    REVERTED — a card dragged off gmail_demo and back, or mark-reviewed toggled
    on then off — leaves NO durable field trace at this SHA (those writers set
    only board_column/status/human_reviewed + updated_at, and ``updated_at``
    cannot be used as the signal because SYSTEM writers bump it on every
    pristine import: the intake artifact backfill + two intake timeline appends
    at create, the corpus build's lazy ``content_fingerprint`` write, Drive
    auto-intake's background ``drive`` write, and the PDF->DOCX backfill).
    Detecting reverted touches needs those writers to leave a trace first.
    """
    if not str(matter.get("id") or "").strip():
        return "missing_matter_id"
    if str(matter.get("source_type") or "") != "gmail_inbound":
        return "not_gmail_inbound"
    if not str(matter.get("gmail_message_id") or "").strip():
        return "missing_gmail_message_id"
    matter_owner = matter_store._clean_owner_user_id(matter.get("owner_user_id"))
    if not matter_owner or matter_owner != owner_user_id:
        return "owner_mismatch"
    created_at = _parse_iso_datetime(matter.get("created_at"))
    if created_at is None:
        return "created_at_invalid"
    if created_at < created_after or created_at > created_before:
        return "outside_window"
    if str(matter.get("status") or "") != "active":
        return "status_not_active"
    if str(matter.get("board_column") or "") != "gmail_demo":
        return "board_column_moved"
    if matter.get("human_reviewed"):
        return "human_reviewed"
    if matter.get("reviewer_decisions"):
        return "reviewer_decisions_present"
    if (
        matter.get("approved_at")
        or matter.get("approver")
        or matter.get("approval")
    ):
        return "approval_present"
    if not _only_system_intake_artifacts(matter):
        return "artifacts_present"
    if not _only_system_timeline_events(matter):
        return "non_system_timeline_event"
    if matter.get("signed_artifact_id"):
        return "signed_artifact_present"
    if matter.get("redline_draft") or matter.get("redline_edits"):
        return "redline_edits_present"
    if matter.get("pdf_annotations"):
        return "pdf_annotations_present"
    if (
        matter.get("sent_at")
        or matter.get("last_outbound_at")
        or matter.get("last_outbound_message_id")
    ):
        return "outbound_send_present"
    if (
        matter.get("signed_at")
        or matter.get("executed")
        or matter.get("executed_at")
        or matter.get("awaiting_signature")
        or matter.get("signature_declined")
        or matter.get("signature_voided")
    ):
        return "signature_activity_present"
    if matter.get("docusign"):
        return "docusign_present"
    # E-SIGN CAPTURED NDA: a matter the inbound scan captured off an e-signature
    # platform notification because it carried an explicit NDA signal
    # (gmail_esign_notification provenance / the esign capture triage reason).
    # These are often the ONLY copy of an EXECUTED NDA that ever reached the
    # mailbox — the capture feature exists precisely to keep them — so a
    # never-touched one must NOT be swept as gmail noise even though it looks
    # pristine on every other axis.
    if str(matter.get("gmail_esign_notification") or "").strip() or (
        str(matter.get("triage_reason") or "") == ESIGN_NDA_CAPTURE_TRIAGE_REASON
    ):
        return "esign_captured_nda"
    intake_metadata = matter.get("intake_metadata")
    if isinstance(intake_metadata, dict):
        counterparty = intake_metadata.get("counterparty")
        if isinstance(counterparty, dict) and str(counterparty.get("source") or "") == "human":
            return "counterparty_human_override"
    elif intake_metadata is not None:
        return "intake_metadata_unrecognized"
    review_status = str(matter.get("review_status") or "").strip()
    if review_status and review_status != "idle":
        return "review_status_present"
    if not _pristine_deterministic_review(matter.get("review_result")):
        return "review_engine_not_deterministic"
    return None


def _bulk_archive_matter_summary(matter: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(matter.get("id") or ""),
        "created_at": str(matter.get("created_at") or ""),
        "document_title": str(matter.get("document_title") or ""),
        "gmail_message_id": str(matter.get("gmail_message_id") or ""),
        "sender": str(matter.get("sender") or ""),
        "subject": str(matter.get("subject") or ""),
    }


def _bulk_archive_selection_hash(owner_user_id: str, matter_ids: list[str]) -> str:
    """Deterministic sha256 over the (owner, sorted matter ids) selection."""
    canonical = json.dumps(
        {"owner_user_id": owner_user_id, "matter_ids": sorted(matter_ids)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _bulk_archive_selection(
    *,
    owner_user_id: str,
    created_after: datetime,
    created_before: datetime,
    limit: int,
) -> tuple[list[dict[str, Any]], int, list[dict[str, str]], str]:
    """The current (selected, excluded_count, excluded_samples, selection_hash).

    Selection order is ``matter_store._bulk_archive_sort_key`` (oldest first, id
    tiebreak) so the same store state always yields the same capped selection —
    that determinism is what the confirm-hash handshake relies on.
    """
    matters = matter_store.list_matters(owner_user_id)
    matters.sort(key=matter_store._bulk_archive_sort_key)
    selected: list[dict[str, Any]] = []
    excluded_samples: list[dict[str, str]] = []
    excluded_count = 0
    for matter in matters:
        reason = _bulk_archive_exclusion_reason(
            matter,
            owner_user_id=owner_user_id,
            created_after=created_after,
            created_before=created_before,
        )
        if reason is None:
            if len(selected) < limit:
                selected.append(matter)
            continue
        excluded_count += 1
        if len(excluded_samples) < BULK_ARCHIVE_EXCLUDED_SAMPLE_CAP:
            excluded_samples.append({"id": str(matter.get("id") or ""), "reason": reason})
    selection_hash = _bulk_archive_selection_hash(
        owner_user_id, [str(matter.get("id") or "") for matter in selected]
    )
    return selected, excluded_count, excluded_samples, selection_hash


def _append_bulk_archive_audit_line(entry: dict[str, Any]) -> bool:
    """Append one JSON audit line for a bulk-archive run (best-effort, never raises).

    Lives beside the archived records (``DATA_DIR/pruned-matters/``). Carries
    matter ids and run metadata ONLY — no subjects, no filenames, no NDA content.
    """
    try:
        audit_dir = matter_store.DATA_DIR / matter_store.PRUNED_ARCHIVE_DIRNAME
        audit_dir.mkdir(parents=True, exist_ok=True)
        with (audit_dir / BULK_ARCHIVE_AUDIT_FILENAME).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
        return True
    except OSError:
        logger.warning("Bulk-archive audit line could not be written", exc_info=True)
        return False


def _gmail_inbound_import_active() -> bool:
    """Whether ANY Gmail inbound import path (scheduled OR manual) can run.

    The bulk-archive execute mode must be exclusive with every writer of the
    per-owner processed ledger. There are TWO: the scheduler's inbound step and
    the manual ``POST /api/gmail/import`` route. Manual import while the
    scheduler is paused is a DESIGNED workflow ("sync now" with scheduled
    polling off — the sync_enabled master gate and the NDA_GMAIL_SYNC_ENABLED
    env kill switch were introduced as scheduler-only gates and are deliberately
    not consulted by the manual route), so scheduler-only pauses do NOT make the
    ledger safe. The one toggle both paths obey is ``inbound_enabled``:
    ``gmail_matter_inbox.import_inbound_matters`` — the engine behind both the
    scheduled step and the manual route — refuses outright when it is false.

    Therefore: inbound import is "active" unless ``inbound_enabled`` is false.
    FAIL-CLOSED for the execute gate: if the settings read errors we report
    ACTIVE, so execute refuses rather than racing the ledger.
    """
    try:
        settings = app_settings.gmail_settings()
        return bool(settings.get("inbound_enabled", True))
    except Exception:  # noqa: BLE001 - unknown inbound state must read as ACTIVE (refuse).
        return True


def handle_matters_bulk_archive(handler) -> None:
    """POST /api/admin/matters/bulk-archive — remove auto-imported Gmail noise.

    Admin-gated; CSRF/auth/host/rate-limit are enforced centrally by server.do_POST
    before dispatch (registered in _POST_EXACT_ROUTES like every sibling write).

    Body: {"owner_user_id": required explicit owner (NEVER inferred from the
    session), "created_after"/"created_before": required ISO window, "dry_run":
    default true, "confirm": sha256 selection hash (execute only), "limit": batch
    cap}. Execute requires dry_run:false AND confirm equal to the sha256 hash of
    the CURRENT selection (recomputed server-side); a stale hash gets 409 with
    the fresh hash so the operator re-reviews before re-confirming.

    Deletion is delegated to matter_store.bulk_archive_gmail_matters with the
    CONFIRMED id set threaded through: only ``predicate ∩ confirmed`` is deleted
    (a matter that newly qualifies after the confirm-hash check was never
    operator-reviewed and is skipped), the predicate is re-evaluated under the
    store lock, and every record + source document is archived to
    pruned-matters/ BEFORE deleting (archive failure keeps everything). After
    the batch the deleted gmail message ids are marked in the per-owner
    processed ledger — the MANDATORY re-import guard: deletion destroys the
    store-based sha256 dedupe, so without the ledger mark the next poll would
    re-import every message.

    LEDGER RACE (why execute REFUSES until inbound Gmail import is disabled):
    an inbound import's ProcessedLedgerSession loads the whole per-owner ledger
    file at its start and its flush() REWRITES the whole file at the end — a
    mark written here mid-import would be silently clobbered by that flush, and
    the read-back verification below runs BEFORE that flush so
    ``ledger_marked: true`` could not detect it. There are TWO such writers:
    the scheduled poll AND the manual ``POST /api/gmail/import`` route, and the
    manual route is a designed workflow gated ONLY by ``inbound_enabled``
    (scheduler pauses — sync_enabled / the env kill switch — do not block it).
    Mitigation: execute (never dry-run) requires ``inbound_enabled`` to be
    FALSE, the one toggle that stops both writers, and returns 409 otherwise;
    the success response carries ``polling_paused_verified: true`` as the
    record of that check. The operator must disable inbound Gmail import in
    Admin before executing and may re-enable it after.
    """
    if not require_admin(handler):
        return
    payload = handler._read_json_payload()
    if payload is None:
        return

    owner_user_id = payload.get("owner_user_id")
    if not isinstance(owner_user_id, str) or not owner_user_id.strip():
        handler._send_json({"error": "owner_user_id is required (explicit, never inferred)."}, status=400)
        return
    owner_user_id = matter_store._clean_owner_user_id(owner_user_id)
    if not owner_user_id:
        handler._send_json({"error": "owner_user_id is required (explicit, never inferred)."}, status=400)
        return

    created_after = _parse_iso_datetime(payload.get("created_after"))
    created_before = _parse_iso_datetime(payload.get("created_before"))
    if created_after is None or created_before is None:
        handler._send_json(
            {"error": "created_after and created_before are required ISO-8601 timestamps."},
            status=400,
        )
        return
    if created_after >= created_before:
        handler._send_json({"error": "created_after must be earlier than created_before."}, status=400)
        return

    dry_run = payload.get("dry_run", True)
    if not isinstance(dry_run, bool):
        handler._send_json({"error": "dry_run must be true or false."}, status=400)
        return

    raw_limit = payload.get("limit", BULK_ARCHIVE_DEFAULT_LIMIT)
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        limit = 0
    if isinstance(raw_limit, bool) or limit < 1 or limit > BULK_ARCHIVE_MAX_LIMIT:
        handler._send_json(
            {"error": f"limit must be an integer between 1 and {BULK_ARCHIVE_MAX_LIMIT}."},
            status=400,
        )
        return

    def predicate(matter: dict[str, Any]) -> bool:
        return _bulk_archive_exclusion_reason(
            matter,
            owner_user_id=owner_user_id,
            created_after=created_after,
            created_before=created_before,
        ) is None

    try:
        selected, excluded_count, excluded_samples, selection_hash = _bulk_archive_selection(
            owner_user_id=owner_user_id,
            created_after=created_after,
            created_before=created_before,
            limit=limit,
        )
    except matter_store.MatterStoreError as error:
        logger.warning("Bulk-archive selection failed: %s", error)
        handler._send_json({"error": matter_store.friendly_matter_store_message(error)}, status=500)
        return

    if dry_run:
        handler._send_json({
            "dry_run": True,
            "selected_count": len(selected),
            "excluded_count": excluded_count,
            "selection_hash": selection_hash,
            "matters": [_bulk_archive_matter_summary(matter) for matter in selected],
            "excluded_samples": excluded_samples,
            "archived": 0,
            "ledger_marked": False,
        })
        return

    confirm = str(payload.get("confirm") or "").strip()
    if not confirm or confirm != selection_hash:
        # Stale/absent confirmation: nothing is deleted; the fresh hash of the
        # CURRENT selection is returned so the operator can re-review + re-confirm.
        handler._send_json({
            "error": "confirm does not match the current selection. Re-review and retry with the returned selection_hash.",
            "selection_hash": selection_hash,
            "selected_count": len(selected),
        }, status=409)
        return

    # LEDGER-RACE GUARD (see docstring): a concurrent inbound import's
    # whole-file ledger flush — from the scheduled poll OR the manual
    # POST /api/gmail/import route — would clobber the re-import marks written
    # below, undetectably. Only inbound_enabled=false stops BOTH writers
    # (scheduler pauses don't block the manual route, by design). Refuse to
    # execute until then; dry-run stays always available.
    if _gmail_inbound_import_active():
        handler._send_json({
            "error": (
                "Disable inbound Gmail import (inbound_enabled) before executing "
                "bulk-archive; both the scheduled poll and a manual Gmail sync "
                "rewrite the processed-message ledger and could clobber the "
                "re-import guard. Pausing the scheduler alone is not enough — "
                "the manual sync route stays open. Disable inbound in Admin, "
                "execute, then re-enable."
            ),
            "polling_paused_verified": False,
            "selection_hash": selection_hash,
            "selected_count": len(selected),
        }, status=409)
        return

    confirmed_matter_ids = frozenset(str(matter.get("id") or "") for matter in selected)
    try:
        report = matter_store.bulk_archive_gmail_matters(
            owner_user_id,
            predicate,
            limit=limit,
            confirmed_matter_ids=confirmed_matter_ids,
        )
    except matter_store.MatterStoreError as error:
        logger.warning("Bulk archive failed: %s", error)
        handler._send_json({"error": matter_store.friendly_matter_store_message(error)}, status=500)
        return
    if report.get("archive_failed"):
        handler._send_json(
            {"error": "Archiving to pruned-matters/ failed; nothing was deleted."},
            status=500,
        )
        return

    deleted_matters = list(report.get("deleted_matters") or [])
    deleted_ids = [str(matter.get("id") or "") for matter in deleted_matters]
    deleted_message_ids = sorted({
        str(matter.get("gmail_message_id") or "").strip()
        for matter in deleted_matters
        if str(matter.get("gmail_message_id") or "").strip()
    })

    # MANDATORY re-import guard: mark every deleted message id as processed in
    # the per-owner Gmail ledger in ONE atomic write, then VERIFY by reading the
    # ledger back (mark_messages_processed's write is best-effort internally, so
    # the read-back is what proves the guard actually landed on disk).
    ledger_marked = True
    if deleted_message_ids:
        from .. import gmail_processed_ledger  # noqa: PLC0415 - keep the import local/light.

        gmail_processed_ledger.mark_messages_processed(deleted_message_ids, owner_user_id)
        persisted = gmail_processed_ledger.load_processed_message_ids(owner_user_id)
        ledger_marked = all(message_id in persisted for message_id in deleted_message_ids)
        if not ledger_marked:
            logger.warning(
                "Bulk archive: processed-ledger mark did not persist for owner; "
                "deleted messages may re-import on the next poll."
            )

    if deleted_ids:
        telemetry.increment("bulk_archive_matters_removed", len(deleted_ids))
    audit_written = _append_bulk_archive_audit_line({
        "ts": datetime.now(timezone.utc).isoformat(),
        "admin_user": request_actor(handler),
        "owner": owner_user_id,
        "window": {
            "created_after": str(payload.get("created_after") or ""),
            "created_before": str(payload.get("created_before") or ""),
        },
        "selection_hash": selection_hash,
        "deleted_ids": deleted_ids,
        "ledger_marked": ledger_marked,
    })

    handler._send_json({
        "dry_run": False,
        "selected_count": len(selected),
        "excluded_count": excluded_count,
        "selection_hash": selection_hash,
        "matters": [_bulk_archive_matter_summary(matter) for matter in deleted_matters],
        "excluded_samples": excluded_samples,
        "archived": len(deleted_ids),
        "ledger_marked": ledger_marked,
        # Record of the ledger-race guard: execute only proceeds after verifying
        # inbound Gmail import is disabled — the one toggle that stops BOTH
        # ledger writers, the scheduled poll and the manual sync route.
        "polling_paused_verified": True,
        "audit_written": audit_written,
    })
