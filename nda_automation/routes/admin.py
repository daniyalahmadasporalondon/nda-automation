from __future__ import annotations

import json
from datetime import datetime, timezone

from .. import ai_review, ai_verifier, app_settings, telemetry
from ..deployment import _deployment_status_for_host
from ..matter_repository import DiskMatterRepository, MatterRepositoryError
from ..review_engine import (
    REVIEW_ENGINE_AI_FIRST,
    active_review_engine_status,
)
from ..http_auth import _admin_user_ids
from .common import request_actor, request_owner_user_id, request_user_email, require_admin


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
    handler._send_json(_admin_list_payload(), send_body=send_body)


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
        handler._send_json(_admin_list_payload(), status=200)
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
    handler._send_json(_admin_list_payload(), status=200)


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
    handler._send_json(_admin_list_payload(), status=200)


def _env_root_admin_ids() -> set[str]:
    """The immutable NDA_ADMIN_USERS env set (verbatim entries, case-sensitive)."""
    return _admin_user_ids()


def _admin_list_payload() -> dict[str, object]:
    return {
        "env_root_admins": sorted(_env_root_admin_ids()),
        "persisted_admins": app_settings.admin_settings()["admins"],
    }


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


def handle_matter_backup(handler, *, send_body: bool = True) -> None:
    # The backup dumps full extracted NDA text and matter metadata, so it is an
    # admin-only endpoint on top of the per-owner scoping applied below.
    if not require_admin(handler, send_body=send_body):
        return
    telemetry.increment("matter_backup_requests")
    try:
        backup = DiskMatterRepository().export_matters_backup(owner_user_id=request_owner_user_id(handler))
    except MatterRepositoryError as error:
        handler._send_json({"error": str(error)}, status=500, send_body=send_body)
        return
    data = json.dumps(backup, indent=2).encode("utf-8") + b"\n"
    exported_at = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    handler._send_download(
        data,
        f"nda-matters-backup-{exported_at}.json",
        "application/json",
        headers={"X-Backup-Contains": "matter-json"},
        send_body=send_body,
    )
