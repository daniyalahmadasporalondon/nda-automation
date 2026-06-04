from __future__ import annotations

from urllib.parse import unquote


def parse_matter_id(path: str, *, suffix: str = "") -> str | None:
    prefix = "/api/matters/"
    if not path.startswith(prefix):
        return None
    if suffix and not path.endswith(suffix):
        return None
    raw_matter_id = path.removeprefix(prefix)
    if suffix:
        raw_matter_id = raw_matter_id.removesuffix(suffix)
    matter_id = unquote(raw_matter_id).strip("/")
    if not matter_id or "/" in matter_id:
        return None
    return matter_id


def request_owner_user_id(handler) -> str:
    return str(getattr(handler, "current_user_id", "") or "").strip()
