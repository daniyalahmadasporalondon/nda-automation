from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from ..checker import PLAYBOOK_PATH, PlaybookTemplateError, validate_playbook

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for local dev portability.
    fcntl = None

_PLAYBOOK_LOCK = threading.RLock()
PLAYBOOK_HISTORY_VERSION = 1
PLAYBOOK_HISTORY_LIMIT = 25
PLAYBOOK_RUNTIME_VERSION = 1

_ACTIVE_RUNTIME_KEYS = [
    "active_version_id",
    "active_hash",
    "published_at",
    "published_by",
    "playbook_name",
    "playbook_version",
    "source",
]

_DRAFT_RUNTIME_KEYS = [
    "draft_id",
    "draft_hash",
    "draft_updated_at",
    "draft_updated_by",
    "draft_base_active_version_id",
    "draft_base_active_hash",
]


@contextmanager
def locked_playbook(playbook_path=PLAYBOOK_PATH):
    with _PLAYBOOK_LOCK:
        playbook_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = playbook_path.with_suffix(f"{playbook_path.suffix}.lock")
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def history_path_for(playbook_path=PLAYBOOK_PATH):
    return playbook_path.with_name(f"{playbook_path.stem}.history.json")


def runtime_path_for(playbook_path=PLAYBOOK_PATH):
    return playbook_path.with_name(f"{playbook_path.stem}.runtime.json")


def draft_path_for(playbook_path=PLAYBOOK_PATH):
    return playbook_path.with_name(f"{playbook_path.stem}.draft.json")


def read_playbook_from_path(playbook_path=PLAYBOOK_PATH) -> dict[str, Any]:
    with playbook_path.open("r", encoding="utf-8") as handle:
        playbook = json.load(handle)
    if not isinstance(playbook, dict):
        raise PlaybookTemplateError("Playbook must be a JSON object.")
    return playbook


def write_json_atomically(value: object, path, *, replace_file=os.replace) -> None:
    data = json.dumps(value, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    temporary_path = path.with_name(f".{path.name}.tmp")
    try:
        with temporary_path.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        replace_file(temporary_path, path)
    except OSError:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


def write_playbook_atomically(playbook: dict, *, playbook_path=PLAYBOOK_PATH, replace_file=os.replace) -> None:
    write_json_atomically(playbook, playbook_path, replace_file=replace_file)


def read_playbook_history(*, playbook_path=PLAYBOOK_PATH) -> list[dict[str, Any]]:
    history_path = history_path_for(playbook_path)
    try:
        with history_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(payload, dict):
        entries = payload.get("entries", [])
    else:
        entries = payload
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def write_playbook_history(entries: list[dict[str, Any]], *, playbook_path=PLAYBOOK_PATH, replace_file=os.replace) -> None:
    history = {
        "version": PLAYBOOK_HISTORY_VERSION,
        "entries": entries[:PLAYBOOK_HISTORY_LIMIT],
    }
    write_json_atomically(history, history_path_for(playbook_path), replace_file=replace_file)


def read_playbook_runtime(*, playbook_path=PLAYBOOK_PATH) -> dict[str, Any] | None:
    try:
        with runtime_path_for(playbook_path).open("r", encoding="utf-8") as handle:
            runtime = json.load(handle)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(runtime, dict):
        return None
    return runtime


def write_playbook_runtime(
    runtime: dict[str, Any],
    *,
    playbook_path=PLAYBOOK_PATH,
    replace_file=os.replace,
) -> None:
    payload = {"version": PLAYBOOK_RUNTIME_VERSION, **runtime}
    write_json_atomically(payload, runtime_path_for(playbook_path), replace_file=replace_file)


def playbook_snapshot_hash(playbook: dict[str, Any]) -> str:
    return "sha256:" + sha256(_stable_json(playbook).encode("utf-8")).hexdigest()


def ensure_active_playbook_runtime(
    *,
    playbook_path=PLAYBOOK_PATH,
    replace_file=os.replace,
    actor: str = "system",
    source: str = "bootstrap",
) -> dict[str, Any]:
    with locked_playbook(playbook_path):
        playbook = read_playbook_from_path(playbook_path)
        validate_playbook(playbook)
        return ensure_active_runtime_for_playbook(
            playbook,
            playbook_path=playbook_path,
            replace_file=replace_file,
            actor=actor,
            source=source,
        )


def ensure_active_runtime_for_playbook(
    playbook: dict[str, Any],
    *,
    playbook_path=PLAYBOOK_PATH,
    replace_file=os.replace,
    actor: str = "system",
    source: str = "bootstrap",
) -> dict[str, Any]:
    runtime = read_playbook_runtime(playbook_path=playbook_path)
    active_hash = playbook_snapshot_hash(playbook)
    if _runtime_matches_active_playbook(runtime, active_hash):
        return runtime or {}

    active_runtime = _active_runtime_from_playbook(playbook, actor=actor, source=source)
    next_runtime = {
        **active_runtime,
        **_draft_runtime_fields(runtime),
    }
    write_playbook_runtime(next_runtime, playbook_path=playbook_path, replace_file=replace_file)
    return {"version": PLAYBOOK_RUNTIME_VERSION, **next_runtime}


def public_playbook_runtime(runtime: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(runtime, dict):
        return {}
    return {key: runtime.get(key) for key in _ACTIVE_RUNTIME_KEYS if key in runtime}


def public_playbook_draft(runtime: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(runtime, dict) or not runtime.get("draft_id"):
        return None
    return {key: runtime.get(key) for key in _DRAFT_RUNTIME_KEYS if key in runtime}


def public_playbook_history(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    public_entries = []
    for entry in entries[:PLAYBOOK_HISTORY_LIMIT]:
        public_entries.append({
            key: entry.get(key)
            for key in [
                "id",
                "recorded_at",
                "actor",
                "action",
                "summary",
                "playbook_name",
                "playbook_version",
                "changed_clause_ids",
                "restored_from_id",
            ]
            if key in entry
        })
    return public_entries


def handle_playbook_get(handler, *, playbook_path=PLAYBOOK_PATH, send_body: bool = True) -> None:
    try:
        with locked_playbook(playbook_path):
            playbook = read_playbook_from_path(playbook_path)
            validate_playbook(playbook)
            runtime = ensure_active_runtime_for_playbook(
                playbook,
                playbook_path=playbook_path,
                source="bootstrap",
            )
            history = read_playbook_history(playbook_path=playbook_path)
    except (OSError, json.JSONDecodeError):
        handler._send_json({"error": "Playbook could not be loaded."}, status=500, send_body=send_body)
        return
    except PlaybookTemplateError as error:
        handler._send_json({"error": str(error)}, status=400, send_body=send_body)
        return
    handler._send_json(
        {
            "playbook": playbook,
            "active": {
                "playbook": playbook,
                "metadata": public_playbook_runtime(runtime),
            },
            "draft": public_playbook_draft(runtime),
            "history": public_playbook_history(history),
        },
        send_body=send_body,
    )


def handle_playbook_save(handler, *, playbook_path=PLAYBOOK_PATH, replace_file=os.replace) -> None:
    payload = handler._read_json_payload()
    if payload is None:
        return

    playbook = payload.get("playbook")
    if not isinstance(playbook, dict):
        handler._send_json({"error": "Playbook payload must include a playbook object."}, status=400)
        return

    try:
        with locked_playbook(playbook_path):
            validate_playbook(playbook)
            previous_playbook = read_playbook_from_path(playbook_path) if playbook_path.exists() else None
            history = read_playbook_history(playbook_path=playbook_path)
            if previous_playbook and not history:
                history.append(_history_entry(
                    previous_playbook,
                    action="baseline",
                    actor="system",
                    summary="Initial playbook snapshot before version history.",
                ))
            write_playbook_atomically(playbook, playbook_path=playbook_path, replace_file=replace_file)
            runtime = ensure_active_runtime_for_playbook(
                playbook,
                playbook_path=playbook_path,
                replace_file=replace_file,
                actor=_actor_from_payload(payload),
                source="save",
            )
            history.insert(0, _history_entry(
                playbook,
                action="save",
                actor=_actor_from_payload(payload),
                previous_playbook=previous_playbook,
            ))
            write_playbook_history(history, playbook_path=playbook_path, replace_file=replace_file)
    except PlaybookTemplateError as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    except OSError:
        handler._send_json({"error": "Playbook could not be saved."}, status=500)
        return

    handler._send_json({
        "playbook": playbook,
        "active": {
            "playbook": playbook,
            "metadata": public_playbook_runtime(runtime),
        },
        "draft": public_playbook_draft(runtime),
        "history": public_playbook_history(history),
        "saved_at": datetime.now(timezone.utc).isoformat(),
    })


def handle_playbook_restore(handler, *, playbook_path=PLAYBOOK_PATH, replace_file=os.replace) -> None:
    payload = handler._read_json_payload()
    if payload is None:
        return
    history_id = str(payload.get("history_id") or "").strip()
    if not history_id:
        handler._send_json({"error": "Provide a playbook history id to restore."}, status=400)
        return

    try:
        with locked_playbook(playbook_path):
            history = read_playbook_history(playbook_path=playbook_path)
            source_entry = next((entry for entry in history if str(entry.get("id") or "") == history_id), None)
            if source_entry is None:
                handler._send_json({"error": "Playbook history entry was not found."}, status=404)
                return
            snapshot = source_entry.get("snapshot")
            if not isinstance(snapshot, dict):
                handler._send_json({"error": "Playbook history entry does not include a restorable snapshot."}, status=409)
                return
            validate_playbook(snapshot)
            previous_playbook = read_playbook_from_path(playbook_path) if playbook_path.exists() else None
            restored_playbook = json.loads(json.dumps(snapshot))
            write_playbook_atomically(restored_playbook, playbook_path=playbook_path, replace_file=replace_file)
            runtime = ensure_active_runtime_for_playbook(
                restored_playbook,
                playbook_path=playbook_path,
                replace_file=replace_file,
                actor=_actor_from_payload(payload),
                source="restore",
            )
            history.insert(0, _history_entry(
                restored_playbook,
                action="restore",
                actor=_actor_from_payload(payload),
                previous_playbook=previous_playbook,
                restored_from_id=history_id,
                summary=f"Restored playbook version from {str(source_entry.get('recorded_at') or 'history')}.",
            ))
            write_playbook_history(history, playbook_path=playbook_path, replace_file=replace_file)
    except PlaybookTemplateError as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    except OSError:
        handler._send_json({"error": "Playbook could not be restored."}, status=500)
        return

    handler._send_json({
        "playbook": restored_playbook,
        "active": {
            "playbook": restored_playbook,
            "metadata": public_playbook_runtime(runtime),
        },
        "draft": public_playbook_draft(runtime),
        "history": public_playbook_history(history),
        "restored_at": datetime.now(timezone.utc).isoformat(),
    })


def _history_entry(
    playbook: dict[str, Any],
    *,
    action: str,
    actor: str,
    previous_playbook: dict[str, Any] | None = None,
    restored_from_id: str = "",
    summary: str = "",
) -> dict[str, Any]:
    recorded_at = datetime.now(timezone.utc).isoformat()
    snapshot = json.loads(json.dumps(playbook))
    changed_clause_ids = _changed_clause_ids(previous_playbook, playbook) if previous_playbook else []
    entry = {
        "id": _history_id(recorded_at, snapshot),
        "recorded_at": recorded_at,
        "actor": actor,
        "action": action,
        "summary": summary or _history_summary(action, changed_clause_ids, playbook),
        "playbook_name": str(playbook.get("name") or ""),
        "playbook_version": str(playbook.get("version") or ""),
        "changed_clause_ids": changed_clause_ids,
        "snapshot": snapshot,
    }
    if restored_from_id:
        entry["restored_from_id"] = restored_from_id
    return entry


def _active_runtime_from_playbook(
    playbook: dict[str, Any],
    *,
    actor: str,
    source: str,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    published_at = recorded_at or datetime.now(timezone.utc).isoformat()
    active_hash = playbook_snapshot_hash(playbook)
    return {
        "active_version_id": _runtime_version_id(published_at, active_hash),
        "active_hash": active_hash,
        "published_at": published_at,
        "published_by": actor[:80] or "system",
        "playbook_name": str(playbook.get("name") or ""),
        "playbook_version": str(playbook.get("version") or ""),
        "source": source[:80] or "bootstrap",
    }


def _runtime_version_id(recorded_at: str, active_hash: str) -> str:
    digest = active_hash.removeprefix("sha256:")[:12]
    compact_time = recorded_at.replace("+00:00", "Z").replace("-", "").replace(":", "").replace(".", "")
    return f"pbv_{compact_time}_{digest}"


def _runtime_matches_active_playbook(runtime: dict[str, Any] | None, active_hash: str) -> bool:
    if not isinstance(runtime, dict):
        return False
    if runtime.get("version") != PLAYBOOK_RUNTIME_VERSION:
        return False
    if runtime.get("active_hash") != active_hash:
        return False
    return all(key in runtime for key in _ACTIVE_RUNTIME_KEYS)


def _draft_runtime_fields(runtime: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(runtime, dict):
        return {}
    return {key: runtime.get(key) for key in _DRAFT_RUNTIME_KEYS if key in runtime}


def _history_id(recorded_at: str, snapshot: dict[str, Any]) -> str:
    digest = sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]
    compact_time = recorded_at.replace("+00:00", "Z").replace("-", "").replace(":", "").replace(".", "")
    return f"pbv_{compact_time}_{digest}"


def _history_summary(action: str, changed_clause_ids: list[str], playbook: dict[str, Any]) -> str:
    if action == "restore":
        return "Restored playbook version."
    if not changed_clause_ids:
        return "Saved playbook with no clause-level policy changes."
    names = _clause_names(playbook, changed_clause_ids)
    return "Saved changes to " + ", ".join(names) + "."


def _changed_clause_ids(previous_playbook: dict[str, Any] | None, next_playbook: dict[str, Any]) -> list[str]:
    if not previous_playbook:
        return []
    previous_by_id = {
        str(clause.get("id") or ""): clause
        for clause in previous_playbook.get("clauses", [])
        if isinstance(clause, dict)
    }
    changed = []
    for clause in next_playbook.get("clauses", []):
        if not isinstance(clause, dict):
            continue
        clause_id = str(clause.get("id") or "")
        if not clause_id:
            continue
        if _stable_json(previous_by_id.get(clause_id)) != _stable_json(clause):
            changed.append(clause_id)
    return changed


def _clause_names(playbook: dict[str, Any], clause_ids: list[str]) -> list[str]:
    names_by_id = {
        str(clause.get("id") or ""): str(clause.get("name") or clause.get("id") or "")
        for clause in playbook.get("clauses", [])
        if isinstance(clause, dict)
    }
    return [names_by_id.get(clause_id, clause_id) for clause_id in clause_ids]


def _stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _actor_from_payload(payload: dict[str, Any]) -> str:
    actor = str(payload.get("actor") or "admin").strip()
    return actor[:80] or "admin"
