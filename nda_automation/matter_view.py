from __future__ import annotations

from typing import Any

from .gmail_integration import recipient_email


def public_matter(matter: dict[str, Any]) -> dict[str, Any]:
    recipient = recipient_email(matter.get("sender"))
    return {
        **matter,
        "recipient_email": recipient,
        "can_send_redline": bool(recipient),
    }


def public_matters(matters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [public_matter(matter) for matter in matters]
