from __future__ import annotations

import json
from datetime import datetime, timezone

from .. import ai_review, app_settings, matter_store, telemetry
from ..deployment import _deployment_status_for_host


def handle_deployment_status(handler, *, send_body: bool = True) -> None:
    handler._send_json(
        {"deployment": _deployment_status_for_host(str(handler.server.server_address[0]))},
        send_body=send_body,
    )


def handle_telemetry(handler, *, send_body: bool = True) -> None:
    handler._send_json({"telemetry": telemetry.snapshot()}, send_body=send_body)


def handle_ai_settings(handler, *, send_body: bool = True) -> None:
    handler._send_json({"ai_review": ai_review.ai_review_status()}, send_body=send_body)


def handle_ai_settings_update(handler) -> None:
    payload = handler._read_json_payload()
    if payload is None:
        return
    if "enabled" not in payload:
        handler._send_json({"error": "Provide an AI setting to update."}, status=400)
        return
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        handler._send_json({"error": "AI enabled setting must be true or false."}, status=400)
        return
    app_settings.update_ai_settings({"enabled": enabled})
    handler._send_json({"ai_review": ai_review.ai_review_status()})


def handle_matter_backup(handler, *, send_body: bool = True) -> None:
    telemetry.increment("matter_backup_requests")
    try:
        backup = matter_store.export_matters_backup()
    except matter_store.MatterStoreError as error:
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
