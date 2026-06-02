from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from ..checker import PLAYBOOK_PATH, PlaybookTemplateError, validate_playbook

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for local dev portability.
    fcntl = None

_PLAYBOOK_LOCK = threading.RLock()


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


def write_playbook_atomically(playbook: dict, *, playbook_path=PLAYBOOK_PATH, replace_file=os.replace) -> None:
    data = json.dumps(playbook, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    temporary_path = playbook_path.with_name(f".{playbook_path.name}.tmp")
    try:
        with temporary_path.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        replace_file(temporary_path, playbook_path)
    except OSError:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


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
            write_playbook_atomically(playbook, playbook_path=playbook_path, replace_file=replace_file)
    except PlaybookTemplateError as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    except OSError:
        handler._send_json({"error": "Playbook could not be saved."}, status=500)
        return

    handler._send_json({"playbook": playbook, "saved_at": datetime.now(timezone.utc).isoformat()})
