"""HTTP routes for the failure-notification feed (surfaced as toasts).

``GET /api/notifications`` -> ``{events, unread_count}`` (active + recent), polled
by the existing frontend notifications controller on its gentle timer.
``POST /api/notifications/<id>/dismiss`` -> dismiss one event by id.

AUTH GATE: these match ``/api/matters`` exactly. ``/api/matters`` sits in
``_GET_EXACT_ROUTES`` and is reached only after ``_authorize_request`` (any
authenticated operator) -- it is NOT admin-gated. The failure toasts ride the
same toast system as the inbound-email toasts, which every authenticated operator
already sees, so the feed is authenticated-but-not-admin to match. The handlers
therefore add no extra gate of their own; placing them in the standard (non-public)
route tables is the authentication.
"""

from __future__ import annotations

from urllib.parse import unquote

from .. import notification_store

# Newest-first cap returned to the feed. The active set is always small; the
# extra recent (resolved/dismissed) rows give the UI history without unbounded
# growth on the wire.
_FEED_LIMIT = 100


def handle_notifications_list(handler, *, send_body: bool = True) -> None:
    """GET /api/notifications -> {events, unread_count}.

    ``events`` is active + recent, newest-first. The frontend tracks SEEN active
    ids client-side and toasts once per new active event, so returning recent
    non-active rows here is harmless (they never re-toast).
    """
    events = notification_store.list_events(limit=_FEED_LIMIT)
    unread = notification_store.unread_count()
    handler._send_json({"events": events, "unread_count": unread}, send_body=send_body)


def handle_notification_dismiss(handler, path: str) -> None:
    """POST /api/notifications/<id>/dismiss -> {event} | 404."""
    event_id = _parse_notification_id(path, suffix="/dismiss")
    if not event_id:
        handler._send_json({"error": "Notification id is required."}, status=400)
        return
    dismissed = notification_store.dismiss(event_id)
    if dismissed is None:
        handler._send_json({"error": "Notification not found."}, status=404)
        return
    handler._send_json({"event": dismissed})


def _parse_notification_id(path: str, *, suffix: str = "") -> str | None:
    prefix = "/api/notifications/"
    if not path.startswith(prefix):
        return None
    if suffix and not path.endswith(suffix):
        return None
    raw_id = path.removeprefix(prefix)
    if suffix:
        raw_id = raw_id.removesuffix(suffix)
    notification_id = unquote(raw_id).strip("/")
    if not notification_id or "/" in notification_id:
        return None
    return notification_id
