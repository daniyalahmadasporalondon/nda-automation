"""One-time migration: rename legacy Drive matter folders to the readable grammar.

Older versions filed matters under opaque folder names such as
``2026-05-30-ana_3tn842t7k`` (and, later, ``2026-06-07 - Acme - thr_1``). Both
repeat the counterparty (already the parent folder) and end in a gibberish id.
:func:`nda_automation.drive_integration.derive_matter_folder_name` now produces a
human name — ``{YYYY-MM-DD} · {document title} · {ref}`` — and this module
back-fills that name onto folders already in the user's Drive.

It is deliberately split into two phases so nothing in Drive changes without an
explicit go-ahead:

* :func:`plan_folder_renames` is **read-only**. It walks the existing
  ``NDAs/{counterparty}/{matter}/`` tree, resolves each matter folder back to its
  matter (authoritatively via the ``metadata/matter_summary.json`` the sync
  writes, else best-effort from the folder name), computes the new name, and
  returns a plan with a per-folder ``action``:

    - ``rename``           — will be renamed (carries ``match_source``);
    - ``already_current``  — name already matches the grammar; left alone;
    - ``unmatched``        — could not be tied to a known matter; left alone;
    - ``conflict``         — the new name collides with another folder; left alone.

* :func:`apply_folder_renames` executes ONLY the ``rename`` entries of a plan.

The Drive calls live in :mod:`drive_integration`; this module owns the
matching/planning logic. A small ``main`` CLI prints the plan and, with
``--apply``, performs the renames — the intended vehicle for the one-time run in
the environment where Drive is connected.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable

from . import app_settings, drive_integration, matter_store

# Separators legacy folder names used between fields; the trailing token after the
# last of these is the matter key candidate for the name-based fallback.
_LEGACY_SEPARATORS = (" · ", " - ", "_")
# A name-parsed key shorter than this is too ambiguous to trust (a 1-2 char tail
# could coincidentally equal some unrelated short matter id), so it is ignored.
_MIN_NAME_KEY_LENGTH = 6

MatterLookup = Callable[[str], "dict[str, Any] | None"]


def _default_lookup(owner_user_id: str) -> MatterLookup:
    def lookup(matter_id: str) -> dict[str, Any] | None:
        return matter_store.get_matter(matter_id, owner_user_id=owner_user_id)

    return lookup


def _matter_id_from_summary(
    matter_folder_id: str, *, owner_user_id: str, service: Any | None
) -> str:
    """Read ``metadata/matter_summary.json`` inside a matter folder for its id.

    The authoritative match: the sync writes the matter id into the summary, so it
    is correct regardless of how the folder itself was named. Returns ``""`` when
    no summary is present (a pre-summary folder) or it cannot be parsed.
    """
    metadata_id = drive_integration.find_folder(
        name=drive_integration.METADATA_FOLDER_NAME,
        parent_id=matter_folder_id,
        owner_user_id=owner_user_id,
        service=service,
    )
    if not metadata_id:
        return ""
    summary_id = drive_integration.find_child_file(
        name=drive_integration.MATTER_SUMMARY_FILENAME,
        parent_id=metadata_id,
        owner_user_id=owner_user_id,
        service=service,
    )
    if not summary_id:
        return ""
    raw = drive_integration.download_file_bytes(
        file_id=summary_id, owner_user_id=owner_user_id, service=service
    )
    if not raw:
        return ""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return ""
    if isinstance(data, dict):
        return str(data.get("matter_id") or "").strip()
    return ""


def _matter_key_candidates_from_name(folder_name: str) -> list[str]:
    """Best-effort matter-id candidates parsed from a legacy folder name.

    Legacy names end in the matter key (``..._3tn842t7k`` or ``... - thr_1``); we
    return the trailing token both bare and ``matter_``-prefixed so a store lookup
    can resolve either id shape. Used only when no summary is available.
    """
    name = str(folder_name or "").strip()
    if not name:
        return []
    tail = name
    for sep in _LEGACY_SEPARATORS:
        if sep in tail:
            tail = tail.rsplit(sep, 1)[-1].strip()
    # Too short to be a real matter id -> refuse to guess (avoids matching an
    # unrelated matter whose id coincidentally equals a tiny tail token).
    if len(tail) < _MIN_NAME_KEY_LENGTH:
        return []
    candidates: list[str] = []
    for value in (tail, f"matter_{tail}"):
        if value and value not in candidates:
            candidates.append(value)
    return candidates


def _resolve_matter(
    folder: dict[str, str],
    *,
    lookup: MatterLookup,
    owner_user_id: str,
    service: Any | None,
) -> tuple[dict[str, Any] | None, str, str]:
    """Resolve a matter folder to ``(matter, matter_id, match_source)``.

    ``match_source`` is ``"summary"`` (authoritative), ``"name"`` (parsed, lower
    confidence), or ``""`` (unmatched).
    """
    matter_id = _matter_id_from_summary(
        folder["id"], owner_user_id=owner_user_id, service=service
    )
    if matter_id:
        matter = lookup(matter_id)
        if matter is not None:
            return matter, matter_id, "summary"

    for candidate in _matter_key_candidates_from_name(folder["name"]):
        matter = lookup(candidate)
        if matter is not None:
            return matter, str(matter.get("id") or candidate), "name"
    return None, "", ""


def plan_folder_renames(
    *,
    owner_user_id: str = "",
    root_folder_id: str = "",
    service: Any | None = None,
    lookup_matter: MatterLookup | None = None,
) -> dict[str, Any]:
    """Read-only: compute the rename plan for the existing Drive matter tree.

    Returns ``{"root_found": bool, "entries": [...], "counts": {action: n}}``. No
    Drive writes occur. ``root_folder_id`` (defaulting to the admin Drive setting)
    is the PARENT under which the app-created ``NDAs`` root lives.
    """
    drive_service = service or drive_integration._drive_service(owner_user_id)
    lookup = lookup_matter or _default_lookup(owner_user_id)

    parent = str(root_folder_id or "").strip() or _configured_root_parent()
    ndas_root = drive_integration.find_folder(
        name=drive_integration.DEFAULT_ROOT_FOLDER_NAME,
        parent_id=parent,
        service=drive_service,
    )
    if not ndas_root:
        return {"root_found": False, "entries": [], "counts": {}}

    entries: list[dict[str, Any]] = []
    counterparty_folders = drive_integration.list_child_folders(
        parent_id=ndas_root, service=drive_service
    )
    for cp in counterparty_folders:
        entries.extend(
            _plan_counterparty(
                cp,
                lookup=lookup,
                owner_user_id=owner_user_id,
                service=drive_service,
            )
        )

    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry["action"]] = counts.get(entry["action"], 0) + 1
    return {"root_found": True, "entries": entries, "counts": counts}


def _plan_counterparty(
    counterparty: dict[str, str],
    *,
    lookup: MatterLookup,
    owner_user_id: str,
    service: Any | None,
) -> list[dict[str, Any]]:
    """Plan the renames within one counterparty folder, detecting name collisions."""
    matter_folders = drive_integration.list_child_folders(
        parent_id=counterparty["id"], service=service
    )
    existing_by_name = {f["name"]: f["id"] for f in matter_folders}
    claimed_targets: dict[str, str] = {}
    entries: list[dict[str, Any]] = []

    for folder in matter_folders:
        entry: dict[str, Any] = {
            "folder_id": folder["id"],
            "counterparty": counterparty["name"],
            "old_name": folder["name"],
            "new_name": "",
            "matter_id": "",
            "match_source": "",
            "action": "unmatched",
            "reason": "",
        }
        matter, matter_id, source = _resolve_matter(
            folder, lookup=lookup, owner_user_id=owner_user_id, service=service
        )
        if matter is None:
            entry["reason"] = "no matching matter (no summary id and name did not resolve)"
            entries.append(entry)
            continue

        new_name = drive_integration.derive_matter_folder_name(
            matter, matter_id, counterparty["name"]
        )
        entry.update({"new_name": new_name, "matter_id": matter_id, "match_source": source})

        if new_name == folder["name"]:
            entry["action"] = "already_current"
        elif (
            existing_by_name.get(new_name, folder["id"]) != folder["id"]
            or claimed_targets.get(new_name, folder["id"]) != folder["id"]
        ):
            entry["action"] = "conflict"
            entry["reason"] = f"another folder already uses the name {new_name!r}"
        elif source != "summary":
            # Only the authoritative summary match is auto-applied. A name-parsed
            # match could resolve to the wrong matter, so it is surfaced for human
            # review and never renamed automatically.
            entry["action"] = "review"
            entry["reason"] = "matched only by folder name, not the stored summary id"
            claimed_targets[new_name] = folder["id"]
        else:
            entry["action"] = "rename"
            claimed_targets[new_name] = folder["id"]
        entries.append(entry)
    return entries


def apply_folder_renames(
    entries: list[dict[str, Any]],
    *,
    owner_user_id: str = "",
    service: Any | None = None,
) -> dict[str, Any]:
    """Execute ONLY the ``action == "rename"`` entries of a plan.

    Returns ``{"renamed": n, "failed": n, "results": [...]}``. Each result records
    the outcome per folder; a single failed rename is captured and does not abort
    the rest.
    """
    drive_service = service or drive_integration._drive_service(owner_user_id)
    results: list[dict[str, Any]] = []
    renamed = 0
    failed = 0
    for entry in entries:
        if entry.get("action") != "rename":
            continue
        try:
            drive_integration.rename_file(
                file_id=entry["folder_id"],
                new_name=entry["new_name"],
                owner_user_id=owner_user_id,
                service=drive_service,
            )
        except Exception as exc:  # noqa: BLE001 - report per-folder, keep going
            failed += 1
            results.append({**entry, "ok": False, "error": str(exc)})
            continue
        renamed += 1
        results.append({**entry, "ok": True, "error": ""})
    return {"renamed": renamed, "failed": failed, "results": results}


def _configured_root_parent() -> str:
    try:
        return str(app_settings.drive_settings().get("folder_id") or "").strip()
    except Exception:  # noqa: BLE001 - settings are advisory for the migration
        return ""


# --- CLI -------------------------------------------------------------------
def format_plan(plan: dict[str, Any]) -> str:
    """Render a plan as a readable text table for the dry-run output."""
    if not plan.get("root_found"):
        return "No 'NDAs' folder found in Drive — nothing to migrate."
    entries = plan.get("entries") or []
    if not entries:
        return "The NDAs folder has no matter folders — nothing to migrate."

    lines: list[str] = []
    order = ["rename", "review", "already_current", "conflict", "unmatched"]
    by_action: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        by_action.setdefault(entry["action"], []).append(entry)

    for action in order:
        group = by_action.get(action) or []
        if not group:
            continue
        lines.append(f"\n[{action}] ({len(group)})")
        for entry in group:
            cp = entry["counterparty"]
            if action in ("rename", "review"):
                src = entry.get("match_source") or "?"
                lines.append(f"  {cp}/  {entry['old_name']}")
                lines.append(f"       -> {entry['new_name']}   (matched by {src})")
            elif action == "already_current":
                lines.append(f"  {cp}/  {entry['old_name']}  (already current)")
            else:
                lines.append(f"  {cp}/  {entry['old_name']}  -- {entry.get('reason') or action}")

    counts = plan.get("counts") or {}
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    lines.append(f"\nPlan summary: {summary}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rename legacy Drive NDA folders to the readable grammar."
    )
    parser.add_argument("--owner", default="", help="Owner user id whose Drive to migrate.")
    parser.add_argument("--root", default="", help="Parent folder id of the NDAs root (defaults to the admin Drive setting).")
    parser.add_argument("--apply", action="store_true", help="Perform the renames (default is a dry-run plan only).")
    parser.add_argument("--json", action="store_true", help="Emit the plan as JSON instead of a table.")
    parser.add_argument(
        "--allow-ownerless",
        action="store_true",
        help="Permit an empty --owner (single-tenant only; an empty owner matches ALL tenants' matters).",
    )
    args = parser.parse_args(argv)

    if not args.owner and not args.allow_ownerless:
        print(
            "Refusing to run with an empty --owner: an empty owner matches every "
            "tenant's matters, which can mislabel folders across tenants. Pass "
            "--owner <id>, or --allow-ownerless if this is a single-tenant Drive.",
            file=sys.stderr,
        )
        return 2

    plan = plan_folder_renames(owner_user_id=args.owner, root_folder_id=args.root)
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
    else:
        print(format_plan(plan))

    if not args.apply:
        counts = plan.get("counts") or {}
        rename_count = counts.get("rename", 0)
        review_count = counts.get("review", 0)
        if rename_count:
            print(f"\nDry run only. Re-run with --apply to rename {rename_count} folder(s).")
        if review_count:
            print(f"{review_count} folder(s) matched only by name -> listed under [review], NOT auto-renamed.")
        return 0

    result = apply_folder_renames(plan.get("entries") or [], owner_user_id=args.owner)
    print(f"\nApplied: renamed={result['renamed']}, failed={result['failed']}")
    for r in result["results"]:
        if not r["ok"]:
            print(f"  FAILED {r['counterparty']}/{r['old_name']} -> {r['new_name']}: {r['error']}")
    return 1 if result["failed"] else 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main(sys.argv[1:]))
