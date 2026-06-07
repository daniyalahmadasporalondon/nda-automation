from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import threading
from typing import Any

from . import matter_store

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for local dev portability.
    fcntl = None

_SETTINGS_LOCK = threading.RLock()
MAX_GMAIL_SYNC_HISTORY = 5
MAX_SETTINGS_AUDIT_HISTORY = 25
MAX_GMAIL_SEARCH_TERMS = 60
MAX_GMAIL_SEARCH_TERM_LENGTH = 80
LEGACY_GMAIL_INBOUND_SEARCH_TERMS = [
    "NDA",
    "MNDA",
    "mutual NDA",
    "non-disclosure",
    "non disclosure",
    "non-disclosure agreement",
    "non disclosure agreement",
    "mutual non-disclosure",
    "mutual non disclosure",
    "confidentiality agreement",
    "mutual confidentiality agreement",
    "confidentiality",
    "confidential",
    "confidential disclosure agreement",
    "CDA",
    "confidentiality deed",
    "non-disclosure deed",
    "confidentiality undertaking",
    "letter of confidentiality",
    "data processing agreement",
    "DPA",
]
DEFAULT_GMAIL_INBOUND_SEARCH_TERMS = [
    "NDA",
    "MNDA",
    "mutual NDA",
    "non-disclosure",
    "non disclosure",
    "non-disclosure agreement",
    "non disclosure agreement",
    "mutual non-disclosure",
    "mutual non disclosure",
    "mutual non-disclosure agreement",
    "mutual non disclosure agreement",
    "mutual NDA agreement",
    "mutual MNDA",
    "confidentiality agreement",
    "mutual confidentiality agreement",
    "confidentiality",
    "confidential",
    "confidential disclosure agreement",
    "mutual confidential disclosure agreement",
    "CDA",
    "MCDA",
    "confidentiality deed",
    "non-disclosure deed",
    "mutual confidentiality deed",
    "mutual non-disclosure deed",
    "confidentiality undertaking",
    "non-disclosure undertaking",
    "letter of confidentiality",
    "confidentiality letter",
    "confidentiality terms",
    "confidentiality obligations",
    "confidential information",
    "confidential materials",
    "confidentiality provisions",
    "confidentiality clause",
    "confidentiality clauses",
    "secrecy agreement",
    "proprietary information agreement",
    "restricted disclosure",
    "do not disclose",
    "not disclose",
    "data processing agreement",
    "DPA",
]
DEFAULT_GMAIL_SETTINGS = {
    "inbound_enabled": True,
    "inbound_search_terms": DEFAULT_GMAIL_INBOUND_SEARCH_TERMS,
    "outbound_enabled": True,
    "sync_frequency": "10_minutes",
    "last_sync_at": "",
    "last_sync_imported_count": 0,
    "last_sync_skipped_count": 0,
    "sync_history": [],
}
DEFAULT_DRIVE_SETTINGS = {
    "enabled": False,
    "folder_id": "",
    "folder_name": "",
}
MAX_DRIVE_FOLDER_ID_LENGTH = 256
MAX_DRIVE_FOLDER_NAME_LENGTH = 200
# Google Drive ids are URL-safe base64-ish tokens; restrict to that alphabet so a
# stored folder id can only ever be a plain id (never a path, URL or traversal).
_DRIVE_FOLDER_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
DEFAULT_AI_SETTINGS = {
    "enabled": None,
    "provider": "",
    "model": "",
}
DEFAULT_REVIEW_RUNTIME_SETTINGS = {
    "active_review_engine": None,
}
SUPPORTED_ACTIVE_REVIEW_ENGINES = {"deterministic", "ai_first"}
AI_API_KEY_FILENAME = "ai_api_key.json"
GMAIL_TRIAGE_API_KEY_FILENAME = "gmail_triage_api_key.json"
MAX_AI_API_KEY_LENGTH = 2000
GMAIL_SYNC_FREQUENCIES = {
    "always_on": 60,
    "10_minutes": 10 * 60,
    "30_minutes": 30 * 60,
    "1_hour": 60 * 60,
    "2_hours": 2 * 60 * 60,
}


class AppSettingsError(RuntimeError):
    pass


def gmail_settings() -> dict[str, Any]:
    settings = _load_settings()
    gmail = settings.get("gmail")
    if not isinstance(gmail, dict):
        gmail = {}
    return gmail_settings_from_payload(gmail)


def drive_settings() -> dict[str, Any]:
    settings = _load_settings()
    drive = settings.get("drive")
    if not isinstance(drive, dict):
        drive = {}
    return drive_settings_from_payload(drive)


def ai_settings() -> dict[str, Any]:
    settings = _load_settings()
    ai_review = settings.get("ai_review")
    if not isinstance(ai_review, dict):
        ai_review = {}
    return ai_settings_from_payload(ai_review)


def review_runtime_settings() -> dict[str, Any]:
    settings = _load_settings()
    review_runtime = settings.get("review_runtime")
    if not isinstance(review_runtime, dict):
        review_runtime = {}
    return review_runtime_settings_from_payload(review_runtime)


def settings_audit_history() -> list[dict[str, Any]]:
    settings = _load_settings()
    return settings_audit_history_from_payload(settings.get("settings_audit"))


def update_ai_settings(updates: dict[str, Any]) -> dict[str, Any]:
    cleaned = {
        key: value
        for key, value in updates.items()
        if _valid_ai_setting(key, value)
    }
    if not cleaned:
        return ai_settings()

    with _locked_settings():
        settings = _load_settings_unlocked()
        ai_review = settings.get("ai_review")
        if not isinstance(ai_review, dict):
            ai_review = {}
        settings["ai_review"] = {**ai_settings_from_payload(ai_review), **cleaned}
        _save_settings_unlocked(settings)
        return ai_settings_from_payload(settings["ai_review"])


def update_review_runtime_settings(updates: dict[str, Any]) -> dict[str, Any]:
    cleaned = {
        key: _clean_review_runtime_setting(key, value)
        for key, value in updates.items()
        if _valid_review_runtime_setting(key, value)
    }
    if not cleaned:
        return review_runtime_settings()

    with _locked_settings():
        settings = _load_settings_unlocked()
        review_runtime = settings.get("review_runtime")
        if not isinstance(review_runtime, dict):
            review_runtime = {}
        settings["review_runtime"] = {**review_runtime_settings_from_payload(review_runtime), **cleaned}
        _save_settings_unlocked(settings)
        return review_runtime_settings_from_payload(settings["review_runtime"])


def record_settings_audit_event(event: dict[str, Any]) -> list[dict[str, Any]]:
    cleaned_event = settings_audit_event_from_payload(event)
    with _locked_settings():
        settings = _load_settings_unlocked()
        settings["settings_audit"] = _prepend_settings_audit_event(settings.get("settings_audit"), cleaned_event)
        _save_settings_unlocked(settings)
        return settings_audit_history_from_payload(settings["settings_audit"])


def stored_ai_api_key() -> str:
    with _locked_settings():
        return _stored_ai_api_key_unlocked()


def stored_gmail_triage_api_key() -> str:
    with _locked_settings():
        return _stored_secret_key_unlocked(_gmail_triage_api_key_path(), "Gmail triage API key")


def save_ai_api_key(api_key: str) -> None:
    cleaned_key = str(api_key or "").strip()
    if not cleaned_key:
        raise AppSettingsError("AI API key is required.")
    if len(cleaned_key) > MAX_AI_API_KEY_LENGTH:
        raise AppSettingsError("AI API key is too long.")

    with _locked_settings():
        _save_ai_api_key_unlocked(cleaned_key)


def save_gmail_triage_api_key(api_key: str) -> None:
    cleaned_key = str(api_key or "").strip()
    if not cleaned_key:
        raise AppSettingsError("Gmail triage API key is required.")
    if len(cleaned_key) > MAX_AI_API_KEY_LENGTH:
        raise AppSettingsError("Gmail triage API key is too long.")

    with _locked_settings():
        _save_secret_key_unlocked(_gmail_triage_api_key_path(), cleaned_key, "Gmail triage API key")


def clear_ai_api_key() -> None:
    with _locked_settings():
        try:
            _ai_api_key_path().unlink()
        except FileNotFoundError:
            pass


def clear_gmail_triage_api_key() -> None:
    with _locked_settings():
        try:
            _gmail_triage_api_key_path().unlink()
        except FileNotFoundError:
            pass


def update_gmail_settings(updates: dict[str, Any]) -> dict[str, Any]:
    cleaned = {
        key: value
        for key, value in updates.items()
        if _valid_gmail_setting(key, value)
    }
    if not cleaned:
        return gmail_settings()

    with _locked_settings():
        settings = _load_settings_unlocked()
        gmail = settings.get("gmail")
        if not isinstance(gmail, dict):
            gmail = {}
        settings["gmail"] = {**gmail_settings_from_payload(gmail), **cleaned}
        _save_settings_unlocked(settings)
        return gmail_settings_from_payload(settings["gmail"])


def update_drive_settings(updates: dict[str, Any]) -> dict[str, Any]:
    cleaned = {
        key: _clean_drive_setting(key, value)
        for key, value in updates.items()
        if _valid_drive_setting(key, value)
    }
    if not cleaned:
        return drive_settings()

    with _locked_settings():
        settings = _load_settings_unlocked()
        drive = settings.get("drive")
        if not isinstance(drive, dict):
            drive = {}
        settings["drive"] = {**drive_settings_from_payload(drive), **cleaned}
        _save_settings_unlocked(settings)
        return drive_settings_from_payload(settings["drive"])


def gmail_role_enabled(role: str) -> bool:
    key = f"{role}_enabled"
    return gmail_settings().get(key, True)


def drive_enabled() -> bool:
    return bool(drive_settings().get("enabled", DEFAULT_DRIVE_SETTINGS["enabled"]))


def gmail_inbound_search_terms() -> list[str]:
    return gmail_settings()["inbound_search_terms"]


def gmail_sync_interval_seconds(frequency: object | None = None) -> int:
    frequency_key = frequency if isinstance(frequency, str) else gmail_settings()["sync_frequency"]
    return GMAIL_SYNC_FREQUENCIES.get(frequency_key, GMAIL_SYNC_FREQUENCIES[DEFAULT_GMAIL_SETTINGS["sync_frequency"]])


def record_gmail_sync(
    result: dict[str, Any],
    *,
    synced_at: str,
    started_at: str = "",
    finished_at: str = "",
) -> dict[str, Any]:
    imported = result.get("imported") if isinstance(result.get("imported"), list) else []
    skipped = result.get("skipped") if isinstance(result.get("skipped"), list) else []
    sync_run = _sync_history_entry(
        result,
        started_at=started_at or synced_at,
        finished_at=finished_at or synced_at,
        status="success",
    )
    with _locked_settings():
        settings = _load_settings_unlocked()
        gmail = settings.get("gmail")
        if not isinstance(gmail, dict):
            gmail = {}
        current_gmail = gmail_settings_from_payload(gmail)
        settings["gmail"] = {
            **current_gmail,
            "last_sync_at": synced_at,
            "last_sync_imported_count": len(imported),
            "last_sync_skipped_count": len(skipped),
            "sync_history": _prepend_sync_history(current_gmail.get("sync_history"), sync_run),
        }
        _save_settings_unlocked(settings)
        return gmail_settings_from_payload(settings["gmail"])


def record_gmail_sync_error(
    error: str,
    *,
    started_at: str,
    finished_at: str,
    query: str = "",
) -> dict[str, Any]:
    sync_run = _sync_history_entry(
        {"imported": [], "skipped": [], "query": query},
        started_at=started_at,
        finished_at=finished_at,
        status="error",
        error=error,
    )
    with _locked_settings():
        settings = _load_settings_unlocked()
        gmail = settings.get("gmail")
        if not isinstance(gmail, dict):
            gmail = {}
        current_gmail = gmail_settings_from_payload(gmail)
        settings["gmail"] = {
            **current_gmail,
            "last_sync_at": finished_at,
            "last_sync_imported_count": 0,
            "last_sync_skipped_count": 0,
            "sync_history": _prepend_sync_history(current_gmail.get("sync_history"), sync_run),
        }
        _save_settings_unlocked(settings)
        return gmail_settings_from_payload(settings["gmail"])


def gmail_settings_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw_frequency = payload.get("sync_frequency", payload.get("sync_cadence", DEFAULT_GMAIL_SETTINGS["sync_frequency"]))
    sync_frequency = str(raw_frequency or DEFAULT_GMAIL_SETTINGS["sync_frequency"])
    if sync_frequency not in GMAIL_SYNC_FREQUENCIES:
        sync_frequency = DEFAULT_GMAIL_SETTINGS["sync_frequency"]
    inbound_search_terms = gmail_search_terms_from_payload(payload.get("inbound_search_terms"))
    if _is_legacy_default_gmail_search_terms(inbound_search_terms):
        inbound_search_terms = list(DEFAULT_GMAIL_INBOUND_SEARCH_TERMS)
    return {
        "inbound_enabled": bool(payload.get("inbound_enabled", DEFAULT_GMAIL_SETTINGS["inbound_enabled"])),
        "inbound_search_terms": inbound_search_terms,
        "outbound_enabled": bool(payload.get("outbound_enabled", DEFAULT_GMAIL_SETTINGS["outbound_enabled"])),
        "sync_frequency": sync_frequency,
        "last_sync_at": str(payload.get("last_sync_at") or DEFAULT_GMAIL_SETTINGS["last_sync_at"]),
        "last_sync_imported_count": _nonnegative_int(
            payload.get("last_sync_imported_count"),
            DEFAULT_GMAIL_SETTINGS["last_sync_imported_count"],
        ),
        "last_sync_skipped_count": _nonnegative_int(
            payload.get("last_sync_skipped_count"),
            DEFAULT_GMAIL_SETTINGS["last_sync_skipped_count"],
        ),
        "sync_history": _sync_history_from_payload(payload.get("sync_history")),
    }


def drive_settings_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": bool(payload.get("enabled", DEFAULT_DRIVE_SETTINGS["enabled"])),
        "folder_id": _clean_drive_folder_id(payload.get("folder_id")),
        "folder_name": _clean_drive_folder_name(payload.get("folder_name")),
    }


def ai_settings_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    enabled = payload.get("enabled", DEFAULT_AI_SETTINGS["enabled"])
    if not isinstance(enabled, bool):
        enabled = None
    provider = str(payload.get("provider") or DEFAULT_AI_SETTINGS["provider"]).strip().lower()
    if provider not in {"", "openrouter"}:
        provider = ""
    model = str(payload.get("model") or DEFAULT_AI_SETTINGS["model"]).strip()
    if len(model) > 200:
        model = ""
    return {"enabled": enabled, "provider": provider, "model": model}


def review_runtime_settings_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    active_review_engine = _stored_runtime_value(
        payload.get("active_review_engine", DEFAULT_REVIEW_RUNTIME_SETTINGS["active_review_engine"]),
        SUPPORTED_ACTIVE_REVIEW_ENGINES,
    )
    return {
        "active_review_engine": active_review_engine,
    }


def settings_audit_event_from_payload(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        payload = {}
    return {
        "recorded_at": str(payload.get("recorded_at") or ""),
        "actor": str(payload.get("actor") or "admin")[:80],
        "action": str(payload.get("action") or "settings_update")[:80],
        "changes": _settings_audit_changes_from_payload(payload.get("changes")),
    }


def settings_audit_history_from_payload(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    history: list[dict[str, Any]] = []
    for item in value:
        event = settings_audit_event_from_payload(item)
        if not event["recorded_at"] and not event["changes"]:
            continue
        history.append(event)
        if len(history) >= MAX_SETTINGS_AUDIT_HISTORY:
            break
    return history


def _valid_ai_setting(key: str, value: Any) -> bool:
    if key == "enabled":
        return isinstance(value, bool)
    if key == "provider":
        return isinstance(value, str) and value.strip().lower() in {"", "openrouter"}
    if key == "model":
        return isinstance(value, str) and len(value.strip()) <= 200
    return False


def _valid_review_runtime_setting(key: str, value: Any) -> bool:
    if key == "active_review_engine":
        return value is None or _normalized_runtime_value(value) in SUPPORTED_ACTIVE_REVIEW_ENGINES
    return False


def _clean_review_runtime_setting(key: str, value: Any) -> str | None:
    if value is None:
        return None
    normalized = _normalized_runtime_value(value)
    if key == "active_review_engine" and normalized in SUPPORTED_ACTIVE_REVIEW_ENGINES:
        return normalized
    return None


def _stored_runtime_value(value: Any, supported: set[str]) -> str | None:
    normalized = _normalized_runtime_value(value)
    return normalized if normalized in supported else None


def _normalized_runtime_value(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _valid_gmail_setting(key: str, value: Any) -> bool:
    if key in ("inbound_enabled", "outbound_enabled"):
        return isinstance(value, bool)
    if key == "inbound_search_terms":
        return bool(gmail_search_terms_from_payload(value, fallback=[]))
    if key == "sync_frequency":
        return isinstance(value, str) and value in GMAIL_SYNC_FREQUENCIES
    return False


def _valid_drive_setting(key: str, value: Any) -> bool:
    if key == "enabled":
        return isinstance(value, bool)
    if key in ("folder_id", "folder_name"):
        return isinstance(value, str)
    return False


def _clean_drive_setting(key: str, value: Any) -> Any:
    if key == "enabled":
        return bool(value)
    if key == "folder_id":
        return _clean_drive_folder_id(value)
    if key == "folder_name":
        return _clean_drive_folder_name(value)
    return value


def _clean_drive_folder_id(value: object) -> str:
    """Normalise a stored Drive folder id.

    The folder id is stored verbatim and only ever passed to the Drive API
    ``parents`` field; reject anything that is not a plain id token (no path
    traversal, whitespace, slashes or URLs) so it can never be interpolated into
    a filesystem path or another request.
    """
    folder_id = str(value or "").strip()
    if not folder_id:
        return ""
    if len(folder_id) > MAX_DRIVE_FOLDER_ID_LENGTH:
        raise AppSettingsError("Drive folder id is too long.")
    if not _DRIVE_FOLDER_ID_PATTERN.fullmatch(folder_id):
        raise AppSettingsError(
            "Drive folder id must be the plain Drive folder id (letters, digits, '-' and '_' only)."
        )
    return folder_id


def _clean_drive_folder_name(value: object) -> str:
    name = " ".join(str(value or "").split())
    return name[:MAX_DRIVE_FOLDER_NAME_LENGTH]


def gmail_search_terms_from_payload(value: object, *, fallback: list[str] | None = None) -> list[str]:
    fallback_terms = DEFAULT_GMAIL_INBOUND_SEARCH_TERMS if fallback is None else fallback
    raw_terms: list[object]
    if value is None:
        raw_terms = list(fallback_terms)
    elif isinstance(value, str):
        raw_terms = value.replace("\n", ",").split(",")
    elif isinstance(value, list):
        raw_terms = value
    else:
        raw_terms = list(fallback_terms)

    terms: list[str] = []
    seen: set[str] = set()
    for raw_term in raw_terms:
        term = _clean_gmail_search_term(raw_term)
        if not term:
            continue
        key = term.casefold()
        if key in seen:
            continue
        terms.append(term)
        seen.add(key)
        if len(terms) >= MAX_GMAIL_SEARCH_TERMS:
            break
    if not terms and fallback_terms:
        return gmail_search_terms_from_payload(list(fallback_terms), fallback=[])
    return terms


def _clean_gmail_search_term(value: object) -> str:
    term = " ".join(str(value or "").split())
    term = term.strip("\"'()")
    if not term or len(term) > MAX_GMAIL_SEARCH_TERM_LENGTH:
        return ""
    if any(character in term for character in "\r\n\t"):
        return ""
    return term


def _is_legacy_default_gmail_search_terms(terms: list[str]) -> bool:
    if len(terms) != len(LEGACY_GMAIL_INBOUND_SEARCH_TERMS):
        return False
    return [term.casefold() for term in terms] == [
        term.casefold() for term in LEGACY_GMAIL_INBOUND_SEARCH_TERMS
    ]


def _nonnegative_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(0, parsed)


def _sync_history_entry(
    result: dict[str, Any],
    *,
    started_at: str,
    finished_at: str,
    status: str,
    error: str = "",
) -> dict[str, Any]:
    imported = result.get("imported") if isinstance(result.get("imported"), list) else []
    skipped = result.get("skipped") if isinstance(result.get("skipped"), list) else []
    duplicate_count = sum(1 for item in skipped if isinstance(item, dict) and item.get("reason") == "duplicate_attachment")
    deduplicated_count = _nonnegative_int(result.get("deduplicated_count"), 0)
    review_failed_count = sum(1 for item in skipped if isinstance(item, dict) and item.get("reason") == "review_failed")
    return {
        "started_at": str(started_at or ""),
        "finished_at": str(finished_at or ""),
        "query": str(result.get("query") or ""),
        "imported_count": len(imported),
        "skipped_count": len(skipped),
        "duplicate_count": duplicate_count,
        "deduplicated_count": deduplicated_count,
        "review_failed_count": review_failed_count,
        "status": "error" if status == "error" else "success",
        "error": str(error or "")[:500],
    }


def _prepend_sync_history(history: object, sync_run: dict[str, Any]) -> list[dict[str, Any]]:
    return [sync_run, *_sync_history_from_payload(history)][:MAX_GMAIL_SYNC_HISTORY]


def _sync_history_from_payload(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    history: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        history.append({
            "started_at": str(item.get("started_at") or ""),
            "finished_at": str(item.get("finished_at") or ""),
            "query": str(item.get("query") or ""),
            "imported_count": _nonnegative_int(item.get("imported_count"), 0),
            "skipped_count": _nonnegative_int(item.get("skipped_count"), 0),
            "duplicate_count": _nonnegative_int(item.get("duplicate_count"), 0),
            "deduplicated_count": _nonnegative_int(item.get("deduplicated_count"), 0),
            "review_failed_count": _nonnegative_int(item.get("review_failed_count"), 0),
            "status": "error" if item.get("status") == "error" else "success",
            "error": str(item.get("error") or "")[:500],
        })
        if len(history) >= MAX_GMAIL_SYNC_HISTORY:
            break
    return history


def _settings_audit_changes_from_payload(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    changes: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        setting = str(item.get("setting") or "")[:120]
        if not setting:
            continue
        changes.append({
            "setting": setting,
            "before": _safe_audit_value(item.get("before")),
            "after": _safe_audit_value(item.get("after")),
        })
        if len(changes) >= 20:
            break
    return changes


def _prepend_settings_audit_event(history: object, event: dict[str, Any]) -> list[dict[str, Any]]:
    if not event.get("changes"):
        return settings_audit_history_from_payload(history)
    return [event, *settings_audit_history_from_payload(history)][:MAX_SETTINGS_AUDIT_HISTORY]


def _safe_audit_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    text = str(value)
    if len(text) > 200:
        return f"{text[:197]}..."
    return text


def _load_settings() -> dict[str, Any]:
    with _locked_settings():
        return _load_settings_unlocked()


@contextmanager
def _locked_settings():
    with _SETTINGS_LOCK:
        matter_store.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with (matter_store.DATA_DIR / "app_settings.lock").open("a+", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _settings_path():
    return matter_store.DATA_DIR / "app_settings.json"


def _ai_api_key_path():
    return matter_store.DATA_DIR / AI_API_KEY_FILENAME


def _gmail_triage_api_key_path():
    return matter_store.DATA_DIR / GMAIL_TRIAGE_API_KEY_FILENAME


def _load_settings_unlocked() -> dict[str, Any]:
    settings_path = _settings_path()
    if not settings_path.is_file():
        return {}
    try:
        with settings_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise AppSettingsError("App settings could not be read.") from exc
    except json.JSONDecodeError as exc:
        raise AppSettingsError("App settings are not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise AppSettingsError("App settings must contain a JSON object.")
    return payload


def _stored_ai_api_key_unlocked() -> str:
    return _stored_secret_key_unlocked(_ai_api_key_path(), "AI API key")


def _stored_secret_key_unlocked(api_key_path: Path, label: str) -> str:
    if not api_key_path.is_file():
        return ""
    try:
        with api_key_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise AppSettingsError(f"{label} could not be read.") from exc
    except json.JSONDecodeError as exc:
        raise AppSettingsError(f"{label} storage is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise AppSettingsError(f"{label} storage must contain a JSON object.")
    return str(payload.get("api_key") or "").strip()


def _save_ai_api_key_unlocked(api_key: str) -> None:
    _save_secret_key_unlocked(_ai_api_key_path(), api_key, "AI API key")


def _save_secret_key_unlocked(api_key_path: Path, api_key: str, label: str) -> None:
    matter_store.DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary_path = api_key_path.with_name(f".{api_key_path.name}.tmp")
    try:
        fd = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"api_key": api_key}, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, api_key_path)
        try:
            os.chmod(api_key_path, 0o600)
        except OSError:
            pass
        _fsync_directory(api_key_path.parent)
    except OSError as exc:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise AppSettingsError(f"{label} could not be saved.") from exc


def _save_settings_unlocked(settings: dict[str, Any]) -> None:
    settings_path = _settings_path()
    matter_store.DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary_path = settings_path.with_name(f".{settings_path.name}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, settings_path)
        _fsync_directory(settings_path.parent)
    except OSError as exc:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise AppSettingsError("App settings could not be saved.") from exc


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = getattr(os, "O_RDONLY", 0)
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        directory_fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
