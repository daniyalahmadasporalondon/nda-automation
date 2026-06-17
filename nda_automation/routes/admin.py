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
from .common import request_owner_user_id, require_admin


def handle_deployment_status(handler, *, send_body: bool = True) -> None:
    handler._send_json(
        {"deployment": _deployment_status_for_host(str(handler.server.server_address[0]))},
        send_body=send_body,
    )


def handle_telemetry(handler, *, send_body: bool = True) -> None:
    # Snapshot once so the health summary derives from the same counters the
    # caller sees (avoids a double snapshot / read race), then surface the
    # derived health block additively alongside the unchanged telemetry block.
    snapshot = telemetry.snapshot()
    handler._send_json(
        {
            "telemetry": snapshot,
            "health": telemetry.health_summary(snapshot.get("counters", {})),
        },
        send_body=send_body,
    )


def handle_ai_settings(handler, *, send_body: bool = True) -> None:
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


def handle_ai_settings_update(handler) -> None:
    if not require_admin(handler):
        return
    payload = handler._read_json_payload()
    if payload is None:
        return
    ai_updates = {}
    runtime_updates = {}
    runtime_noops = set()
    runtime_status = active_review_engine_status()
    if "enabled" in payload:
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            handler._send_json({"error": "AI enabled setting must be true or false."}, status=400)
            return
        ai_updates["enabled"] = enabled
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
        "operational_warnings": _operational_warnings(),
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
