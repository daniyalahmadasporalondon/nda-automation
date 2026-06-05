from __future__ import annotations

from urllib.parse import unquote

from ..http_auth import ADMIN_REQUIRED_MESSAGE, request_is_admin


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


def request_user_provider(handler) -> str:
    current_user = getattr(handler, "current_user", None)
    if isinstance(current_user, dict):
        return str(current_user.get("provider") or "").strip()
    return ""


def require_admin(handler, *, send_body: bool = True) -> bool:
    """Gate an admin-only handler, sending a 403 when the caller is not admin.

    Returns True when the request may proceed. The caller has already passed
    authentication (request handlers run after _authorize_request), so this only
    adds the admin authorization layer on top of that.
    """
    if request_is_admin(
        user_id=request_owner_user_id(handler),
        provider=request_user_provider(handler),
        host=str(handler.server.server_address[0]),
    ):
        return True
    handler._send_json({"error": ADMIN_REQUIRED_MESSAGE}, status=403, send_body=send_body)
    return False
