from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ["NDA_DATA_DIR"]).expanduser() if os.environ.get("NDA_DATA_DIR") else ROOT / "data"
MATTERS_PATH = DATA_DIR / "matters.json"
UPLOADS_DIR = DATA_DIR / "uploads"
_MATTERS_LOCK = threading.RLock()


class MatterStoreError(RuntimeError):
    pass


def list_matters() -> list[dict[str, Any]]:
    with _MATTERS_LOCK:
        matters = _load_matters()
    return sorted(matters, key=lambda matter: str(matter.get("created_at") or ""), reverse=True)


def get_matter(matter_id: str) -> dict[str, Any] | None:
    with _MATTERS_LOCK:
        for matter in _load_matters():
            if matter.get("id") == matter_id:
                return matter
    return None


def get_source_document_bytes(matter: dict[str, Any]) -> bytes | None:
    stored_filename = str(matter.get("stored_filename") or "")
    if not stored_filename:
        return None
    source_path = (UPLOADS_DIR / stored_filename).resolve()
    if source_path.parent != UPLOADS_DIR.resolve() or not source_path.is_file():
        return None
    return source_path.read_bytes()


def update_matter_stage(matter_id: str, board_column: str) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc).isoformat()
    with _MATTERS_LOCK:
        matters = _load_matters()
        for index, matter in enumerate(matters):
            if matter.get("id") != matter_id:
                continue
            updated_matter = {
                **matter,
                "board_column": board_column,
                "status": "closed" if board_column == "signed_closed" else "active",
                "updated_at": now,
            }
            matters[index] = updated_matter
            _save_matters(matters)
            return updated_matter
    return None


def create_matter(
    *,
    source_filename: str,
    document_bytes: bytes,
    extracted_text: str,
    review_result: dict[str, Any],
    triage: dict[str, Any],
    source_type: str = "gmail_demo",
    board_column: str = "gmail_demo",
) -> dict[str, Any]:
    matter_id = f"matter_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()
    safe_source_name = _safe_filename(source_filename)
    stored_filename = f"{matter_id}-{safe_source_name}"

    with _MATTERS_LOCK:
        UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        (UPLOADS_DIR / stored_filename).write_bytes(document_bytes)

        matter: dict[str, Any] = {
            "id": matter_id,
            "created_at": now,
            "updated_at": now,
            "source_type": source_type,
            "source_filename": source_filename,
            "stored_filename": stored_filename,
            "document_title": Path(source_filename).stem or "Untitled NDA",
            "counterparty_name": "",
            "status": "active",
            "board_column": board_column,
            "extracted_text": extracted_text,
            "review_result": review_result,
            **triage,
        }
        matters = _load_matters()
        matters.append(matter)
        _save_matters(matters)
    return matter


def _load_matters() -> list[dict[str, Any]]:
    if not MATTERS_PATH.is_file():
        return []
    try:
        with MATTERS_PATH.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise MatterStoreError("Matter store could not be read.") from exc
    except json.JSONDecodeError as exc:
        raise MatterStoreError("Matter store is not valid JSON.") from exc
    if not isinstance(payload, list):
        raise MatterStoreError("Matter store must contain a JSON list.")
    return payload


def _save_matters(matters: list[dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary_path = MATTERS_PATH.with_suffix(".json.tmp")
    with temporary_path.open("w", encoding="utf-8") as handle:
        json.dump(matters, handle, indent=2)
    temporary_path.replace(MATTERS_PATH)


def _safe_filename(filename: str) -> str:
    basename = Path(filename).name or "nda.docx"
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", basename).strip("-._")
    if not safe_name.lower().endswith(".docx"):
        safe_name = f"{safe_name or 'nda'}.docx"
    return safe_name or "nda.docx"
