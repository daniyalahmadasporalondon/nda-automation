from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

_LOCK = threading.Lock()
_STARTED_AT = datetime.now(timezone.utc)
_COUNTERS: dict[str, int] = {}


def increment(counter: str, amount: int = 1) -> None:
    if amount <= 0:
        return
    with _LOCK:
        _COUNTERS[counter] = _COUNTERS.get(counter, 0) + amount


def snapshot() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    with _LOCK:
        counters = dict(sorted(_COUNTERS.items()))
    return {
        "started_at": _STARTED_AT.isoformat(),
        "checked_at": now.isoformat(),
        "uptime_seconds": max(0, int((now - _STARTED_AT).total_seconds())),
        "counters": counters,
    }


def reset() -> None:
    global _STARTED_AT
    with _LOCK:
        _STARTED_AT = datetime.now(timezone.utc)
        _COUNTERS.clear()
