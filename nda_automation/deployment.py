from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

from . import app_settings, export_service, matter_store, process_memory, user_store
from .http_auth import (
    AUTH_ALLOWED_HOSTS_ENV,
    AUTH_NOT_CONFIGURED_MESSAGE,
    _auth_method_configured,
    _auth_required_for_host,
    _basic_auth_configured,
    _env_flag_enabled,
    _google_oauth_configured,
    _is_loopback_host,
)
from .rate_limit import _rate_limit_per_window

DURABLE_DATA_DIR_REQUIRED_MESSAGE = "Public deployments must set NDA_DATA_DIR to a durable storage path."
EPHEMERAL_DATA_DIR_MESSAGE = "NDA_DATA_DIR points at ephemeral storage; use a persistent disk or external store."
EPHEMERAL_EXPORTS_DIR_MESSAGE = "NDA_EXPORTS_DIR points at ephemeral storage; use a persistent disk or disable saved export URLs."
EPHEMERAL_USERS_PATH_MESSAGE = "NDA_USERS_PATH points at ephemeral storage; use persistent storage for users and sessions."
GOOGLE_OAUTH_REDIRECT_URI_ENV = "NDA_GOOGLE_OAUTH_REDIRECT_URI"
GMAIL_OAUTH_REDIRECT_URI_ENV = "NDA_GMAIL_OAUTH_REDIRECT_URI"
GMAIL_LEGACY_TOKEN_PATH_ENVS = ("NDA_GMAIL_INBOUND_TOKEN_PATH", "NDA_GMAIL_OUTBOUND_TOKEN_PATH")
AI_REVIEW_ENABLED_ENV = "NDA_AI_REVIEW_ENABLED"
AI_PROVIDER_ENV = "NDA_AI_PROVIDER"
AI_MODEL_ENV = "NDA_AI_MODEL"
ACTIVE_REVIEW_ENGINE_ENV = "NDA_ACTIVE_REVIEW_ENGINE"
OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
GMAIL_TRIAGE_MODEL_ENV = "NDA_GMAIL_TRIAGE_MODEL"
GMAIL_INTAKE_MODEL_ENV = "NDA_GMAIL_INTAKE_MODEL"
DEFAULT_GMAIL_INTAKE_MODEL = "deepseek/deepseek-v4-flash"
INBOUND_AI_REVIEW_ENABLED_ENV = "NDA_INBOUND_AI_REVIEW_ENABLED"

# Worker memory is flagged red only when it crosses this fraction of the
# container limit -- close enough that an OOM is plausibly imminent. Mirrors the
# data_dir_persistence asymmetry: an UNKNOWN limit (macOS/local/bare host) stays
# advisory ok=True, never a false red.
MEMORY_HEADROOM_WARN_FRACTION = 0.85

# The Render persistent disk at NDA_DATA_DIR is small (1GB) and holds matters,
# users/sessions, exports, AND the gentle-catch-up 90-day attachment backlog, so a
# fill-up is a real risk. Flagged red only when KNOWN usage crosses this fraction;
# an unreadable path (local/no NDA_DATA_DIR) stays advisory ok=True.
DISK_HEADROOM_WARN_FRACTION = 0.85

# Boot-sentinel filename written under NDA_DATA_DIR to prove durability across
# restarts.  An UNMOUNTED /var/data (a fresh container dir) is not on a denylisted
# ephemeral root, so the path-string checks above cannot catch it -- only a value
# that survives a restart can.  See `record_data_dir_boot`.
DATA_DIR_SENTINEL_FILENAME = ".nda_boot_sentinel.json"
NON_PERSISTENT_DATA_DIR_WARNING = (
    "NDA_DATA_DIR boot sentinel from a prior deploy did NOT survive this restart; "
    "storage may NOT be persistent (an unmounted disk wipes matters/users/sessions "
    "on every restart). Verify the persistent disk is actually mounted at NDA_DATA_DIR."
)

# Per-deploy / per-instance identity Render injects at runtime.  RENDER_GIT_COMMIT
# changes on every code deploy; RENDER_INSTANCE_ID changes on every (re)start of the
# running instance.  Together they let us tell a GENUINE FIRST BOOT (no sentinel,
# nothing to compare) apart from a real WIPE (a sentinel from a DIFFERENT prior
# deploy/instance that should have survived but vanished).  Absent on local/dev.
RENDER_DEPLOY_ID_ENVS = ("RENDER_GIT_COMMIT", "RENDER_SERVICE_ID")
RENDER_INSTANCE_ID_ENVS = ("RENDER_INSTANCE_ID",)

# Persistence verdicts (distinct from the path-string `data_dir_ephemeral` flag).
DATA_DIR_PERSISTED = "persisted"  # POSITIVE proof: a prior boot's sentinel survived a restart
DATA_DIR_NOT_PERSISTED = "not_persisted"  # POSITIVE proof of a wipe: a prior-deploy sentinel vanished
DATA_DIR_PERSISTENCE_UNKNOWN = "unknown"  # first boot / sentinel I/O failed / cannot yet prove either way

# Module-level verdict recorded once at boot by `record_data_dir_boot` and read by
# the deployment-status endpoint.  Defaults to "unknown" until a boot is recorded
# (e.g. local/test imports that never call the boot hook).
_data_dir_persistence_state: str = DATA_DIR_PERSISTENCE_UNKNOWN
_data_dir_boot_count: int = 0


def record_data_dir_boot(data_dir: Path) -> str:
    """Write/read the boot sentinel under ``data_dir`` to prove durability.

    Called once at startup.  Reads any sentinel left by a PRIOR boot, then writes a
    fresh sentinel stamped with this boot's deploy/instance identity.  The verdict is
    deliberately ASYMMETRIC -- the loud ``not_persisted`` (ok:False) alarm is reserved
    for POSITIVE evidence of a wipe; a genuine first boot or any ambiguous case stays
    advisory ``unknown`` (ok:True) so a healthy fresh deploy is never flagged red:

    * A surviving sentinel whose recorded boot is from a DIFFERENT deploy/instance
      than this one -> the dir carried data across a real restart -> ``persisted``
      (positive proof).
    * A surviving sentinel that records a PRIOR boot but this boot is a different
      deploy/instance AND the sentinel's own boot identity is unknowable -> we still
      treat survival itself as proof -> ``persisted``.
    * No surviving sentinel: indistinguishable between a genuine first boot and a wipe
      from a single-instance service that Render does not auto-restart, so we DEFAULT
      TO ``unknown`` (advisory) rather than a false red.  A wipe is only asserted
      ``not_persisted`` when we have a recorded prior boot to compare against and it
      vanished (see ``_prior_boot_marker`` / the loud path below).

    Returns one of ``DATA_DIR_PERSISTED`` / ``DATA_DIR_NOT_PERSISTED`` /
    ``DATA_DIR_PERSISTENCE_UNKNOWN`` and records it module-globally for the
    deployment-status endpoint.  NEVER raises -- a storage hiccup here must not
    crash a healthy deploy; on any I/O error the verdict is ``unknown``.
    """
    global _data_dir_persistence_state, _data_dir_boot_count
    sentinel_path = data_dir / DATA_DIR_SENTINEL_FILENAME
    prior = _read_data_dir_sentinel(sentinel_path)
    now = time.time()
    this_deploy_id = _current_deploy_id()
    this_instance_id = _current_instance_id()

    prior_boot_count = 0
    first_seen: float = now
    prior_deploy_id: str | None = None
    prior_instance_id: str | None = None
    if isinstance(prior, dict):
        raw_count = prior.get("boot_count")
        if isinstance(raw_count, int) and raw_count >= 0:
            prior_boot_count = raw_count
        raw_first_seen = prior.get("first_seen")
        if isinstance(raw_first_seen, (int, float)):
            first_seen = raw_first_seen
        raw_deploy = prior.get("deploy_id")
        if isinstance(raw_deploy, str) and raw_deploy:
            prior_deploy_id = raw_deploy
        raw_instance = prior.get("instance_id")
        if isinstance(raw_instance, str) and raw_instance:
            prior_instance_id = raw_instance

    boot_count = prior_boot_count + 1
    _write_data_dir_sentinel(
        sentinel_path,
        {
            "boot_count": boot_count,
            "first_seen": first_seen,
            "last_seen": now,
            "deploy_id": this_deploy_id or "",
            "instance_id": this_instance_id or "",
        },
    )
    _data_dir_boot_count = boot_count

    _data_dir_persistence_state = _classify_persistence(
        prior=prior,
        prior_boot_count=prior_boot_count,
        prior_deploy_id=prior_deploy_id,
        prior_instance_id=prior_instance_id,
        this_deploy_id=this_deploy_id,
        this_instance_id=this_instance_id,
    )
    return _data_dir_persistence_state


def _classify_persistence(
    *,
    prior: dict[str, object] | None,
    prior_boot_count: int,
    prior_deploy_id: str | None,
    prior_instance_id: str | None,
    this_deploy_id: str | None,
    this_instance_id: str | None,
) -> str:
    """Map the prior sentinel + this boot's identity to a persistence verdict.

    Asymmetric on purpose: ``not_persisted`` (loud) only on POSITIVE wipe evidence;
    everything ambiguous -- above all a genuine first boot -- stays ``unknown``."""
    if prior is None:
        # An EXISTING sentinel could not be read/parsed -- never claim non-persistence.
        return DATA_DIR_PERSISTENCE_UNKNOWN

    survived = bool(prior) and prior_boot_count >= 1
    if survived:
        # A prior boot's sentinel is physically present after this restart: the data
        # dir carried bytes across a restart, which is exactly durability.
        return DATA_DIR_PERSISTED

    # No surviving prior sentinel (empty dir / boot_count 0).  This is the crux of the
    # false-positive: a genuine FIRST BOOT on a healthy durable disk looks identical to
    # a wipe.  We only assert a wipe when we can PROVE a restart happened on this dir
    # and the sentinel still vanished.  We cannot prove that from an empty dir alone on
    # a single-instance Render service (no auto-restart), so default to advisory
    # ``unknown`` -- strictly better than a false red.
    if _prior_boot_marker_indicates_wipe(
        prior_deploy_id=prior_deploy_id,
        prior_instance_id=prior_instance_id,
        this_deploy_id=this_deploy_id,
        this_instance_id=this_instance_id,
    ):
        return DATA_DIR_NOT_PERSISTED
    return DATA_DIR_PERSISTENCE_UNKNOWN


def _prior_boot_marker_indicates_wipe(
    *,
    prior_deploy_id: str | None,
    prior_instance_id: str | None,
    this_deploy_id: str | None,
    this_instance_id: str | None,
) -> bool:
    """True only with POSITIVE evidence a prior boot existed yet its sentinel vanished.

    Reached when the surviving sentinel records NO retained boot (boot_count 0) but
    still carries a deploy/instance identity from a DIFFERENT prior boot -- i.e. the
    file is present but its durable boot history was wiped under it.  In practice an
    empty dir carries no such marker, so this stays ``False`` and we report ``unknown``
    rather than a false red; it exists so a genuine cross-deploy wipe is still caught."""
    if prior_deploy_id and this_deploy_id and prior_deploy_id != this_deploy_id:
        return True
    if prior_instance_id and this_instance_id and prior_instance_id != this_instance_id:
        return True
    return False


def _current_deploy_id() -> str | None:
    return _first_env_value(RENDER_DEPLOY_ID_ENVS)


def _current_instance_id() -> str | None:
    return _first_env_value(RENDER_INSTANCE_ID_ENVS)


def _first_env_value(env_names: tuple[str, ...]) -> str | None:
    for name in env_names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def data_dir_persistence_state() -> str:
    return _data_dir_persistence_state


def data_dir_boot_count() -> int:
    """How many times this NDA_DATA_DIR has booted (per the boot sentinel).

    Recorded by ``record_data_dir_boot`` and read by the deployment-status
    endpoint so a RESTART SPIKE -- the classic OOM crash-loop signature -- is
    visible in prod. ``0`` until a boot is recorded (local/test imports that never
    call the boot hook), which the status check reports as "unknown", never red.
    """
    return _data_dir_boot_count


def _reset_data_dir_persistence_state_for_tests() -> None:
    global _data_dir_persistence_state, _data_dir_boot_count
    _data_dir_persistence_state = DATA_DIR_PERSISTENCE_UNKNOWN
    _data_dir_boot_count = 0


def _read_data_dir_sentinel(sentinel_path: Path) -> dict[str, object] | None:
    """Return the prior sentinel dict, ``{}`` for the empty-at-boot signal (no
    sentinel present -- including a not-yet-created data dir), or ``None`` only when
    an EXISTING sentinel could not be read/parsed (treated as unknown, never as a
    false non-persistence claim)."""
    try:
        if not sentinel_path.exists():
            # No prior sentinel survives: this is the empty-at-boot signal a genuine
            # FIRST BOOT and a wipe both produce.  Return {} (not None); the caller
            # treats it as advisory ``unknown`` -- NOT a loud non-persistence claim --
            # since first-boot and wipe are indistinguishable from an empty dir alone.
            return {}
        with sentinel_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return None


def _write_data_dir_sentinel(sentinel_path: Path, payload: dict[str, object]) -> None:
    try:
        sentinel_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = sentinel_path.with_name(f"{sentinel_path.name}.tmp")
        with temporary_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(sentinel_path)
    except OSError:
        # Best-effort: a write failure leaves the prior verdict intact and never
        # propagates.  Detection degrades to "unknown", it does not crash boot.
        pass


def _validate_public_auth(host: str) -> None:
    if not _auth_required_for_host(host):
        return
    if not _auth_method_configured():
        raise RuntimeError(AUTH_NOT_CONFIGURED_MESSAGE)


def _validate_public_storage(host: str) -> None:
    if _is_loopback_host(host) or _env_flag_enabled("NDA_ALLOW_EPHEMERAL_DATA"):
        return
    if not os.environ.get("NDA_DATA_DIR"):
        raise RuntimeError(DURABLE_DATA_DIR_REQUIRED_MESSAGE)
    if _is_ephemeral_storage_path(matter_store.DATA_DIR):
        raise RuntimeError(EPHEMERAL_DATA_DIR_MESSAGE)
    if export_service.EXPORTS_DIR is not None and _is_ephemeral_storage_path(export_service.EXPORTS_DIR):
        raise RuntimeError(EPHEMERAL_EXPORTS_DIR_MESSAGE)
    if os.environ.get("NDA_USERS_PATH") and _is_ephemeral_storage_path(user_store.users_path()):
        raise RuntimeError(EPHEMERAL_USERS_PATH_MESSAGE)


def _deployment_status_for_host(host: str) -> dict[str, object]:
    auth_required = _auth_required_for_host(host)
    basic_auth_configured = _basic_auth_configured()
    google_oauth_configured = _google_oauth_configured()
    auth_configured = basic_auth_configured or google_oauth_configured
    data_dir_configured = bool(os.environ.get("NDA_DATA_DIR"))
    data_dir_ephemeral = _is_ephemeral_storage_path(matter_store.DATA_DIR)
    exports_dir = export_service.EXPORTS_DIR
    exports_dir_ephemeral = exports_dir is not None and _is_ephemeral_storage_path(exports_dir)
    users_path_configured = bool(os.environ.get("NDA_USERS_PATH"))
    users_path_ephemeral = users_path_configured and _is_ephemeral_storage_path(user_store.users_path())
    rate_limit_per_minute = _rate_limit_per_window()
    data_dir_persisted = data_dir_persistence_state()
    data_dir_check = _deployment_data_dir_check(host, data_dir_configured, data_dir_ephemeral)
    data_dir_persistence_check = _deployment_data_dir_persistence_check(host, data_dir_persisted)
    users_path_check = _deployment_users_path_check(host, users_path_configured, users_path_ephemeral)
    public_host = not _is_loopback_host(host)
    allowed_hosts_configured = bool(os.environ.get(AUTH_ALLOWED_HOSTS_ENV, "").strip())
    google_redirect_uri = os.environ.get(GOOGLE_OAUTH_REDIRECT_URI_ENV, "").strip()
    gmail_redirect_uri = os.environ.get(GMAIL_OAUTH_REDIRECT_URI_ENV, "").strip()
    legacy_gmail_token_paths_configured = any(os.environ.get(env_name, "").strip() for env_name in GMAIL_LEGACY_TOKEN_PATH_ENVS)
    ai_env = _deployment_ai_env_status()
    gmail_triage_env = _deployment_gmail_triage_env_status(public_host)
    gmail_intake_env = _deployment_gmail_intake_env_status()
    memory = process_memory.memory_usage()
    memory_check = _deployment_memory_headroom_check(memory)
    disk = _data_dir_disk_usage()
    disk_check = _deployment_disk_headroom_check(disk)
    inbound_ai_review_check = _deployment_inbound_ai_review_check()
    boot_count = data_dir_boot_count()
    boot_count_check = _deployment_boot_count_check(boot_count)
    inbound_review_queue_depth = _inbound_review_queue_depth()
    checks = [
        {
            "id": "auth",
            "ok": (not auth_required) or auth_configured,
            "message": _deployment_auth_message(auth_required, auth_configured),
        },
        {
            "id": "google_identity",
            "ok": (not public_host) or google_oauth_configured,
            "message": _deployment_google_identity_message(public_host, google_oauth_configured),
        },
        {
            "id": "allowed_hosts",
            "ok": (not public_host) or allowed_hosts_configured,
            "message": _deployment_allowed_hosts_message(public_host, allowed_hosts_configured),
        },
        {
            "id": "oauth_redirects",
            "ok": _deployment_oauth_redirects_ok(public_host, google_oauth_configured, google_redirect_uri, gmail_redirect_uri),
            "message": _deployment_oauth_redirects_message(public_host, google_oauth_configured, google_redirect_uri, gmail_redirect_uri),
        },
        {
            "id": "data_dir",
            "ok": data_dir_check["ok"],
            "message": data_dir_check["message"],
        },
        {
            # Mount-liveness proof (boot sentinel): distinct from `data_dir` which
            # only path-string-denylists ephemeral roots.  Advisory -- it reports
            # "unknown" on first boot / failed I/O and only WARNS (never fails the
            # gate) so a healthy first deploy is not blocked.
            "id": "data_dir_persistence",
            "ok": data_dir_persistence_check["ok"],
            "persisted": data_dir_persistence_check["persisted"],
            "message": data_dir_persistence_check["message"],
        },
        {
            "id": "users_path",
            "ok": users_path_check["ok"],
            "message": users_path_check["message"],
        },
        {
            "id": "exports_dir",
            "ok": not exports_dir_ephemeral,
            "message": "Saved export storage is durable or disabled." if not exports_dir_ephemeral else "Saved export storage points at ephemeral storage.",
        },
        {
            "id": "gmail_token_mode",
            "ok": (not public_host) or not legacy_gmail_token_paths_configured,
            "message": (
                "Per-user Gmail OAuth tokens are used; legacy shared Gmail token paths are unset."
                if not legacy_gmail_token_paths_configured
                else "Legacy shared Gmail token path env vars are set; unset them for per-user hosted Gmail."
            ),
        },
        {
            "id": "ai_review_env",
            "ok": ai_env["ok"],
            "message": ai_env["message"],
        },
        {
            "id": "gmail_triage_ai",
            "ok": gmail_triage_env["ok"],
            "message": gmail_triage_env["message"],
        },
        {
            # Informational only (never fails the gate): the intake classifier reuses
            # OPENROUTER_API_KEY and falls open if NDA_GMAIL_INTAKE_MODEL is unset.
            # This check reports only what it can verify WITHOUT a live API call --
            # key presence and the resolved model slug. It deliberately does NOT
            # assert the classifier is actually reachable / the model slug valid
            # (a bad slug, rate-limit, or OpenRouter outage is observed at sync time
            # via the per-sync ai_intake tallies, not here).
            "id": "gmail_intake_ai",
            "ok": gmail_intake_env["ok"],
            "configured": gmail_intake_env["configured"],
            "message": gmail_intake_env["message"],
        },
        {
            "id": "rate_limit",
            "ok": rate_limit_per_minute > 0,
            "message": "Expensive endpoint rate limiting is enabled." if rate_limit_per_minute > 0 else "Expensive endpoint rate limiting is disabled.",
        },
        {
            # Live RSS vs the container memory ceiling -- the OOM-firefight "measure
            # don't guess" probe. Advisory like data_dir_persistence: ok stays True
            # when the container limit is UNKNOWN (macOS/local/bare host) so a healthy
            # box with no cgroup cap is never flagged red; only crosses False when
            # used_fraction is KNOWN and >= MEMORY_HEADROOM_WARN_FRACTION.
            "id": "memory_headroom",
            "ok": memory_check["ok"],
            "message": memory_check["message"],
        },
        {
            # Free space on the (small, 1GB) NDA_DATA_DIR persistent disk -- the
            # gentle catch-up imports a 90-day attachment backlog onto it. Same
            # asymmetry as memory_headroom: ok stays True when the path is unreadable
            # (local/no NDA_DATA_DIR), only crosses False on KNOWN usage >= warn.
            "id": "disk_headroom",
            "ok": disk_check["ok"],
            "message": disk_check["message"],
        },
        {
            # Echo the inbound-auto-review kill-switch so an operator can SEE whether
            # the OOM-mitigation toggle is currently disabling background review.
            # Informational: never fails the gate (either state is valid).
            "id": "inbound_ai_review_env",
            "ok": inbound_ai_review_check["ok"],
            "enabled": inbound_ai_review_check["enabled"],
            "message": inbound_ai_review_check["message"],
        },
        {
            # Surface the NDA_DATA_DIR boot count so a RESTART SPIKE (the OOM
            # crash-loop signature) is visible. Informational: "unknown" before a
            # boot is recorded, never red.
            "id": "data_dir_boot_count",
            "ok": boot_count_check["ok"],
            "boot_count": boot_count_check["boot_count"],
            "message": boot_count_check["message"],
        },
    ]
    return {
        "host": host,
        "public_host": public_host,
        "auth_required": auth_required,
        "auth_configured": auth_configured,
        "basic_auth_configured": basic_auth_configured,
        "google_oauth_configured": google_oauth_configured,
        "allowed_hosts_configured": allowed_hosts_configured,
        "google_oauth_redirect_uri_configured": bool(google_redirect_uri),
        "gmail_oauth_redirect_uri_configured": bool(gmail_redirect_uri),
        "data_dir_configured": data_dir_configured,
        "data_dir_ephemeral": data_dir_ephemeral,
        "data_dir_persisted": data_dir_persisted,
        "users_path_configured": users_path_configured,
        "users_path_ephemeral": users_path_ephemeral,
        "exports_dir_configured": exports_dir is not None,
        "exports_dir_ephemeral": exports_dir_ephemeral,
        "legacy_gmail_token_paths_configured": legacy_gmail_token_paths_configured,
        "ai_review_env_configured": ai_env["configured"],
        "gmail_triage_ai_configured": gmail_triage_env["configured"],
        "gmail_intake_ai_configured": gmail_intake_env["configured"],
        "rate_limit_per_minute": rate_limit_per_minute,
        "memory": memory,
        "disk": disk,
        "inbound_ai_review_enabled": inbound_ai_review_check["enabled"],
        "inbound_review_queue_depth": inbound_review_queue_depth,
        "data_dir_boot_count": boot_count,
        "health_check_path": "/healthz",
        "status": "ok" if all(bool(check["ok"]) for check in checks) else "needs_attention",
        "checks": checks,
    }


def _deployment_auth_message(auth_required: bool, auth_configured: bool) -> str:
    if _google_oauth_configured():
        return "Google OAuth login is configured."
    if _basic_auth_configured():
        return "HTTP Basic auth is configured."
    if auth_required:
        return "No login method is configured."
    return "Authentication is not required for this host."


def _deployment_data_dir_check(host: str, data_dir_configured: bool, data_dir_ephemeral: bool) -> dict[str, object]:
    if data_dir_configured and not data_dir_ephemeral:
        return {"ok": True, "message": "Matter data uses configured durable storage."}
    if _is_loopback_host(host):
        return {"ok": True, "message": "Local deployment may use local matter data storage."}
    if _env_flag_enabled("NDA_ALLOW_EPHEMERAL_DATA"):
        return {"ok": True, "message": "Ephemeral matter data is explicitly allowed."}
    return {"ok": False, "message": "Matter data is not on configured durable storage."}


def _deployment_data_dir_persistence_check(host: str, data_dir_persisted: str) -> dict[str, object]:
    # `ok` only goes False when we have POSITIVE evidence of non-persistence (a
    # prior-boot sentinel that did not survive).  First boot / unknown stays ok so
    # a healthy fresh deploy is never flagged red.
    if data_dir_persisted == DATA_DIR_PERSISTED:
        return {"ok": True, "persisted": True, "message": "NDA_DATA_DIR boot sentinel survived a restart; storage is durable."}
    if data_dir_persisted == DATA_DIR_NOT_PERSISTED:
        if _is_loopback_host(host):
            return {"ok": True, "persisted": False, "message": "Local deployment data dir may reset between runs."}
        return {"ok": False, "persisted": False, "message": NON_PERSISTENT_DATA_DIR_WARNING}
    return {
        "ok": True,
        "persisted": None,
        "message": "NDA_DATA_DIR persistence not yet proven (first boot or sentinel unreadable); confirmed once a restart preserves it.",
    }


def _deployment_memory_headroom_check(memory: dict[str, object]) -> dict[str, object]:
    """Verdict on worker RSS vs the container limit.

    ASYMMETRIC like the data_dir_persistence check: ``ok`` only goes False on
    POSITIVE evidence of pressure -- a KNOWN ``used_fraction`` at/above the warn
    fraction. An unknown container limit (no cgroup cap, e.g. macOS/local) or an
    unreadable RSS stays advisory ``ok: True`` so a healthy box is never flagged
    red. Never raises; tolerates a missing/odd ``memory`` mapping.
    """
    used_fraction = memory.get("used_fraction") if isinstance(memory, dict) else None
    limit_bytes = memory.get("limit_bytes") if isinstance(memory, dict) else None
    if not isinstance(used_fraction, (int, float)) or not isinstance(limit_bytes, int):
        return {
            "ok": True,
            "message": "Worker memory headroom is unknown (no container limit or RSS readable here).",
        }
    percent = used_fraction * 100
    if used_fraction >= MEMORY_HEADROOM_WARN_FRACTION:
        return {
            "ok": False,
            "message": f"Worker memory is at {percent:.0f}% of the container limit; OOM headroom is low.",
        }
    return {
        "ok": True,
        "message": f"Worker memory is at {percent:.0f}% of the container limit.",
    }


def _data_dir_disk_usage() -> dict[str, object]:
    """Disk usage for the NDA_DATA_DIR persistent disk, fail-safe to all-None.

    Probes the filesystem backing ``matter_store.DATA_DIR`` (where matters, users,
    sessions, exports, and the catch-up attachment backlog all land) via stdlib
    ``shutil.disk_usage``.

    Deliberately scoped to AVOID a false red: it only reports numbers when
    ``NDA_DATA_DIR`` is explicitly configured AND that exact directory already
    EXISTS. We never walk up to a parent mount -- doing so would read the HOST root
    filesystem (a developer's near-full laptop, or a test patching DATA_DIR to an
    unmounted ``/var/data``) and wrongly flag a healthy deploy red. When the data
    dir is unset/absent (local dev, first boot before the dir is created) every
    field is ``None`` so the caller degrades to "unknown". NEVER raises.
    """
    total: int | None = None
    used: int | None = None
    free: int | None = None
    used_fraction: float | None = None
    try:
        if os.environ.get("NDA_DATA_DIR", "").strip():
            target = Path(matter_store.DATA_DIR).expanduser()
            if target.is_dir():
                usage = shutil.disk_usage(target)
                total, used, free = int(usage.total), int(usage.used), int(usage.free)
                if total > 0:
                    used_fraction = used / total
    except (OSError, ValueError, TypeError):
        # Unreadable path / odd DATA_DIR -> degrade to unknown, never crash.
        total = used = free = None
        used_fraction = None
    return {
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "used_fraction": used_fraction,
    }


def _deployment_disk_headroom_check(disk: dict[str, object]) -> dict[str, object]:
    """Verdict on NDA_DATA_DIR disk usage.

    ASYMMETRIC like memory_headroom: ``ok`` only goes False on KNOWN usage at/above
    the warn fraction; an unreadable disk (no path / local dev) stays advisory
    ``ok: True`` so a healthy box is never flagged red. Never raises.
    """
    used_fraction = disk.get("used_fraction") if isinstance(disk, dict) else None
    if not isinstance(used_fraction, (int, float)):
        return {
            "ok": True,
            "message": "Persistent-disk headroom is unknown (NDA_DATA_DIR path not readable here).",
        }
    percent = used_fraction * 100
    if used_fraction >= DISK_HEADROOM_WARN_FRACTION:
        return {
            "ok": False,
            "message": f"Persistent disk is {percent:.0f}% full; free space is low for the attachment backlog.",
        }
    return {
        "ok": True,
        "message": f"Persistent disk is {percent:.0f}% full.",
    }


def _deployment_inbound_ai_review_check() -> dict[str, object]:
    """Echo the inbound auto-review kill-switch (informational, never fails gate)."""
    enabled = _inbound_ai_review_enabled()
    if enabled:
        return {
            "ok": True,
            "enabled": True,
            "message": f"Inbound auto-review is ENABLED ({INBOUND_AI_REVIEW_ENABLED_ENV} is not disabling it).",
        }
    return {
        "ok": True,
        "enabled": False,
        "message": (
            f"Inbound auto-review is DISABLED via {INBOUND_AI_REVIEW_ENABLED_ENV}; "
            "imported NDAs keep their deterministic first-pass and stay reviewable on-demand."
        ),
    }


def _deployment_boot_count_check(boot_count: int) -> dict[str, object]:
    """Surface the data-dir boot count so a restart spike (OOM loop) is visible."""
    if boot_count <= 0:
        return {
            "ok": True,
            "boot_count": boot_count,
            "message": "NDA_DATA_DIR boot count is not yet recorded (first boot or local).",
        }
    return {
        "ok": True,
        "boot_count": boot_count,
        "message": f"NDA_DATA_DIR has booted {boot_count} time(s); a sudden spike signals a restart/OOM loop.",
    }


def _inbound_ai_review_enabled() -> bool:
    """Read the inbound auto-review kill-switch without importing it at module load.

    Lazily pulls ``ingestion_service`` to avoid an import cycle and to stay
    fail-safe: any import/attribute error degrades to ``True`` (the default-enabled
    state), never crashing the status endpoint.
    """
    try:
        from . import ingestion_service

        return bool(ingestion_service.inbound_ai_review_enabled())
    except Exception:  # pragma: no cover - defensive, status must never crash
        return True


def _inbound_review_queue_depth() -> int | None:
    """Current inbound-review pool queue depth (pending jobs), or ``None`` on error.

    Reads the public ``queue_depth()`` on the process-wide worker pool so a
    SATURATING queue is observable. Lazily imported + fail-safe -- the status
    endpoint must never crash because telemetry is unavailable.
    """
    try:
        from . import ingestion_service

        return int(ingestion_service._INBOUND_REVIEW_POOL.queue_depth())
    except Exception:  # pragma: no cover - defensive
        return None


def _deployment_users_path_check(host: str, users_path_configured: bool, users_path_ephemeral: bool) -> dict[str, object]:
    if users_path_configured and users_path_ephemeral:
        return {"ok": False, "message": "User/session storage points at ephemeral storage."}
    if users_path_configured:
        return {"ok": True, "message": "User/session storage uses a configured path."}
    if _is_loopback_host(host):
        return {"ok": True, "message": "Local users default to local matter data storage."}
    return {"ok": True, "message": "User/session storage defaults to NDA_DATA_DIR/users.json."}


def _deployment_google_identity_message(public_host: bool, google_oauth_configured: bool) -> str:
    if google_oauth_configured:
        return "Google OAuth login is configured for per-user accounts."
    if public_host:
        return "Set Google OAuth client ID and secret for per-user login and Gmail."
    return "Google OAuth login is optional for local development."


def _deployment_allowed_hosts_message(public_host: bool, allowed_hosts_configured: bool) -> str:
    if not public_host:
        return "Host allowlist is optional for loopback development."
    if allowed_hosts_configured:
        return "Request host allowlist is configured."
    return f"Set {AUTH_ALLOWED_HOSTS_ENV} to the deployed Render hostname."


def _deployment_oauth_redirects_ok(
    public_host: bool,
    google_oauth_configured: bool,
    google_redirect_uri: str,
    gmail_redirect_uri: str,
) -> bool:
    if not public_host or not google_oauth_configured:
        return True
    return _https_redirect_uri(google_redirect_uri) and _https_redirect_uri(gmail_redirect_uri)


def _deployment_oauth_redirects_message(
    public_host: bool,
    google_oauth_configured: bool,
    google_redirect_uri: str,
    gmail_redirect_uri: str,
) -> str:
    if not public_host:
        return "Fixed OAuth redirect URIs are optional for loopback development."
    if not google_oauth_configured:
        return "OAuth redirect URIs are checked after Google OAuth is configured."
    missing = []
    if not google_redirect_uri:
        missing.append(GOOGLE_OAUTH_REDIRECT_URI_ENV)
    if not gmail_redirect_uri:
        missing.append(GMAIL_OAUTH_REDIRECT_URI_ENV)
    if missing:
        return f"Set fixed HTTPS OAuth redirect URI env vars: {', '.join(missing)}."
    if not _https_redirect_uri(google_redirect_uri) or not _https_redirect_uri(gmail_redirect_uri):
        return "OAuth redirect URIs must be absolute HTTPS URLs for public Render deployments."
    return "Google login and Gmail OAuth redirect URIs are configured."


def _deployment_ai_env_status() -> dict[str, object]:
    enabled = _env_flag_enabled(AI_REVIEW_ENABLED_ENV) or os.environ.get(ACTIVE_REVIEW_ENGINE_ENV, "").strip() == "ai_first"
    provider = os.environ.get(AI_PROVIDER_ENV, "").strip().lower()
    model = os.environ.get(AI_MODEL_ENV, "").strip()
    configured = bool(provider and model and _ai_provider_key_configured(provider))
    if not enabled and not provider and not model:
        return {"ok": True, "configured": False, "message": "AI review can be configured from Admin or environment."}
    if configured:
        return {"ok": True, "configured": True, "message": "AI review provider, model, and server-side API key are configured."}
    return {
        "ok": False,
        "configured": False,
        "message": "Set NDA_AI_PROVIDER, NDA_AI_MODEL, and the matching server-side API key before enabling hosted AI review.",
    }


def _deployment_gmail_triage_env_status(public_host: bool) -> dict[str, object]:
    key_configured = bool(
        os.environ.get(OPENROUTER_API_KEY_ENV, "").strip()
        or _stored_key_configured(app_settings.stored_ai_api_key)
    )
    model_configured = bool(os.environ.get(GMAIL_TRIAGE_MODEL_ENV, "").strip())
    configured = key_configured and model_configured
    if configured:
        return {"ok": True, "configured": True, "message": "Gmail OpenRouter triage key and model are configured."}
    if not public_host:
        return {"ok": True, "configured": False, "message": "Gmail OpenRouter triage can be configured later for local development."}
    return {
        "ok": False,
        "configured": False,
        "message": "Set OPENROUTER_API_KEY and NDA_GMAIL_TRIAGE_MODEL for AI-assisted Gmail attachment selection.",
    }


def _deployment_gmail_intake_env_status() -> dict[str, object]:
    # The intake classifier reuses OPENROUTER_API_KEY (same precedence as triage) and
    # defaults the model to deepseek/deepseek-v4-flash, so it is non-blocking and
    # fails open if the env knob is unset.
    key_configured = bool(
        os.environ.get(OPENROUTER_API_KEY_ENV, "").strip()
        or _stored_key_configured(app_settings.stored_ai_api_key)
    )
    model = os.environ.get(GMAIL_INTAKE_MODEL_ENV, "").strip() or DEFAULT_GMAIL_INTAKE_MODEL
    if key_configured:
        return {
            "ok": True,
            "configured": True,
            "message": f"Gmail NDA-intake classifier uses OpenRouter model {model}.",
        }
    return {
        "ok": True,
        "configured": False,
        "message": (
            "Gmail NDA-intake classifier is optional; it reuses OPENROUTER_API_KEY and "
            f"defaults NDA_GMAIL_INTAKE_MODEL to {DEFAULT_GMAIL_INTAKE_MODEL}."
        ),
    }


def _ai_provider_key_configured(provider: str) -> bool:
    if provider == "openrouter":
        return bool(os.environ.get(OPENROUTER_API_KEY_ENV, "").strip() or _stored_key_configured(app_settings.stored_ai_api_key))
    return False


def _stored_key_configured(loader) -> bool:
    try:
        return bool(loader())
    except (app_settings.AppSettingsError, OSError):
        return False


def _https_redirect_uri(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


def _is_ephemeral_storage_path(path: Path) -> bool:
    try:
        resolved_path = path.expanduser().resolve(strict=False)
    except OSError:
        resolved_path = path.expanduser().absolute()
    ephemeral_roots = {
        Path("/tmp"),
        Path("/private/tmp"),
        Path("/var/tmp"),
        Path(tempfile.gettempdir()).expanduser().resolve(strict=False),
    }
    for root in ephemeral_roots:
        try:
            resolved_root = root.resolve(strict=False)
        except OSError:
            resolved_root = root.absolute()
        if resolved_path == resolved_root or resolved_root in resolved_path.parents:
            return True
    return False
