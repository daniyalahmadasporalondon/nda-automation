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
# Shared with security-web's request_is_admin (http_auth._admin_user_ids): the
# canonical admin set. Same parse rule (comma-split, strip, case-SENSITIVE).
ADMIN_USERS_ENV = "NDA_ADMIN_USERS"


def _admin_user_id_entries() -> list[str]:
    """Ordered, stripped, non-empty NDA_ADMIN_USERS entries (case-sensitive)."""
    raw = os.environ.get(ADMIN_USERS_ENV, "")
    return [value.strip() for value in raw.split(",") if value.strip()]


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

    Resolution order (aligned with security-web's admin definition; never guesses
    when ambiguous, so an unresolvable matter is left ownerless rather than
    mis-assigned cross-tenant):

    1. ``NDA_AUTH_USERNAME`` — the basic-auth break-glass admin. A single
       unambiguous value, and for basic-auth requests it IS ``current_user_id``,
       so a matter stamped with it is immediately reachable by the operator.
    2. else ``NDA_ADMIN_USERS`` when it names EXACTLY ONE admin. With more than
       one, "which admin owns legacy data" is ambiguous, so refuse and leave it
       ownerless rather than pick arbitrarily.
    3. else the sole user on a single-user box.
    4. else "" — leave ownerless.
    """
    admin_username = os.environ.get(ADMIN_USERNAME_ENV, "").strip()
    if admin_username:
        return admin_username
    admin_entries = _admin_user_id_entries()
    if len(admin_entries) == 1:
        return admin_entries[0]
    if len(admin_entries) > 1:
        return ""
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
