from __future__ import annotations

from typing import Any, TypedDict

from .gmail_integration import recipient_email


class PublicMatter(TypedDict, total=False):
    id: str
    sender: str
    recipient_email: str
    can_send_redline: bool


def public_matter(matter: dict[str, Any]) -> PublicMatter:
    recipient = recipient_email(matter.get("sender"))
    return {
        **matter,
        "recipient_email": recipient,
        "can_send_redline": bool(recipient),
    }


def public_matters(matters: list[dict[str, Any]]) -> list[PublicMatter]:
    return [public_matter(matter) for matter in matters]
