"""One-time, idempotent backfill of ownership onto legacy ownerless matters.

After the fail-closed access fix (``matter_store._matter_owner_matches``), a
matter with no ``owner_user_id`` is invisible to authenticated users. Those
ownerless matters come from the global Gmail shared-sync (no per-user OAuth) and
from data predating ownership. This module assigns each a real owner so the data
is reachable again, without ever granting a wildcard.

It is the orchestration layer that reads ``user_store`` and the admin identity,
then delegates the actual mutation to ``matter_store`` (which stays free of a
``user_store`` dependency). Safe to run repeatedly — only ownerless matters are
touched, so a second run is a no-op.
"""
from __future__ import annotations

import os
from typing import Any

from . import matter_store, user_store

ADMIN_USERNAME_ENV = "NDA_AUTH_USERNAME"


def build_user_email_to_id() -> dict[str, str]:
    """Case-folded ``email -> user_id`` map from the user store.

    A matter's ``gmail_account`` is the connected mailbox email; a user's
    ``email`` is their login email. In the per-user OAuth flow these are the same
    address, so email equality is the resolvable signal. An email shared by more
    than one user id is ambiguous and is dropped (those matters fall through to
    the admin fallback rather than being assigned to an arbitrary user).
    """
    mapping: dict[str, str] = {}
    ambiguous: set[str] = set()
    for user in user_store.list_users():
        if not isinstance(user, dict):
            continue
        email = str(user.get("email") or "").strip().casefold()
        user_id = str(user.get("id") or "").strip()
        if not email or not user_id:
            continue
        if email in mapping and mapping[email] != user_id:
            ambiguous.add(email)
            continue
        mapping[email] = user_id
    for email in ambiguous:
        mapping.pop(email, None)
    return mapping


def resolve_admin_user_id() -> str:
    """The fallback owner for matters with no resolvable gmail mapping.

    Prefers the configured basic-auth admin (``NDA_AUTH_USERNAME``); otherwise,
    on a single-user box, the sole user. Returns "" when neither is unambiguous,
    in which case the backfill leaves those matters ownerless rather than guess.
    """
    admin_username = os.environ.get(ADMIN_USERNAME_ENV, "").strip()
    if admin_username:
        return admin_username
    users = user_store.list_users()
    if len(users) == 1 and isinstance(users[0], dict):
        return str(users[0].get("id") or "").strip()
    return ""


def run_ownerless_matter_backfill() -> dict[str, Any]:
    """Run the one-time backfill and return its summary."""
    return matter_store.migrate_ownerless_matter_ownership(
        user_email_to_id=build_user_email_to_id(),
        admin_user_id=resolve_admin_user_id(),
    )


def main() -> None:
    """One-time operator entrypoint: ``python -m nda_automation.ownership_backfill``.

    Idempotent — safe to re-run. Prints counts only (never matter content).
    """
    summary = run_ownerless_matter_backfill()
    print(
        "Ownerless-matter ownership backfill complete: "
        f"scanned={summary['scanned']} already_owned={summary['already_owned']} "
        f"assigned_by_gmail={summary['assigned_by_gmail']} "
        f"assigned_to_admin={summary['assigned_to_admin']} "
        f"skipped_unresolved={summary['skipped_unresolved']}"
    )
    if summary["skipped_unresolved"]:
        print(
            f"Note: {summary['skipped_unresolved']} matter(s) had no gmail mapping and no admin "
            "fallback; left ownerless (still visible single-tenant). Set NDA_AUTH_USERNAME or "
            "connect the owning user's Gmail, then re-run."
        )


if __name__ == "__main__":
    main()
