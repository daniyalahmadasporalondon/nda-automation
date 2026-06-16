"""Durable per-owner "already-processed" ledger for inbound Gmail messages.

The inbound poll re-surfaces the SAME messages newest-first on every cycle (we
hold only ``gmail.readonly``, so imported mail cannot be labelled or archived). The
attachment dedup index and the drain cursor already stop the *heavy* re-work
(re-download + re-extract + duplicate matter), but BOTH still require fetching the
full message and running the AI intake/triage classifiers to discover there is
nothing new to do. This ledger closes that last gap: a durable per-owner set of
message ids that have reached a TERMINAL outcome, checked BEFORE the
``messages().get`` + the ``gmail_intake`` classifier + the ``gmail_triage``
attachment-selector calls, so an already-processed message costs nothing.

Durability + shape mirror the drain cursor (``matter_store.gmail_inbound_cursor``):
rooted at ``matter_store.DATA_DIR / gmail / <sanitized-owner> /
gmail-processed-messages.json``, written via an atomic ``tmp -> os.replace`` write,
owner sanitized with the same ``matter_store`` routine so the ledger and the cursor
agree on the per-owner directory key.

Two access shapes:

* Stateless one-shots (``load_processed_message_ids`` / ``mark_message_processed`` /
  ``is_message_processed``) for ad-hoc callers and tests.
* A load-once / mark-many / write-once :class:`ProcessedLedgerSession` (REFINEMENT
  A) so a poll over N messages reads the file ONCE up front, accumulates marks in
  memory, and writes the file at most ONCE at the end -- never re-reading or
  re-writing the whole file per message.

The ledger is a COMPLEMENTARY guard layered over the existing cursor drain: a
"processed" skip is a cheap pre-fetch short-circuit (like the dedup gate), so it
never advances/stalls the cursor and never hides genuinely-new mail (an unseen id
is simply absent from the set and falls through to the full path).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Iterable

from . import matter_store

LOGGER = logging.getLogger(__name__)

# Hard cap on stored ids per owner. Bounds the on-disk file (and the in-memory set)
# regardless of inbox size. When exceeded we keep the MOST-RECENTLY-marked ids and
# evict the oldest, so the live frontier (the ids most likely to re-surface on the
# next poll) is always retained; an evicted old id simply falls back to the full
# path once -- correct, just not free.
MAX_LEDGER_ENTRIES = 20000

_LEDGER_DIRNAME = "gmail"
_LEDGER_FILENAME = "gmail-processed-messages.json"


def _owner_dir(owner: str) -> Path:
    """The per-owner ledger directory, keyed identically to the drain cursor.

    Reuses ``matter_store``'s owner sanitizer so the ledger and the cursor share the
    same per-owner identity (and the same DATA_DIR root), and so a hostile owner
    token can never escape the gmail ledger tree.
    """
    sanitized = matter_store._clean_owner_user_id(owner)  # noqa: SLF001 - shared sanitizer
    # An empty/garbage owner still gets a stable, in-tree bucket rather than writing
    # to the gmail dir root (keeps every owner's ledger a sibling directory).
    if not sanitized:
        sanitized = "_default"
    return matter_store.DATA_DIR / _LEDGER_DIRNAME / sanitized


def _ledger_path(owner: str) -> Path:
    return _owner_dir(owner) / _LEDGER_FILENAME


def _read_ids(owner: str) -> list[str]:
    """Read the persisted ids for ``owner`` as an ORDERED list (oldest-first).

    Order is preserved so the most-recent-first eviction has a stable notion of
    "oldest". A missing/corrupt file degrades to an empty list -- a ledger read is
    best-effort and must never break the poll.
    """
    path = _ledger_path(owner)
    try:
        if not path.is_file():
            return []
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return []
    return _coerce_id_list(payload)


def _coerce_id_list(payload: object) -> list[str]:
    """Normalise a loaded payload into a de-duplicated ordered list of id strings.

    Accepts the canonical ``{"message_ids": [...]}`` shape and, defensively, a bare
    JSON list, so a hand-edited or legacy file is tolerated. Preserves first-seen
    order and drops blanks/dupes.
    """
    if isinstance(payload, dict):
        raw = payload.get("message_ids")
    else:
        raw = payload
    if not isinstance(raw, list):
        return []
    ordered: list[str] = []
    seen: set[str] = set()
    for value in raw:
        message_id = str(value or "").strip()
        if not message_id or message_id in seen:
            continue
        seen.add(message_id)
        ordered.append(message_id)
    return ordered


def _cap_most_recent_first(ordered_ids: list[str]) -> list[str]:
    """Evict the OLDEST ids when over the cap, keeping the most recent.

    ``ordered_ids`` is oldest-first (append order), so the newest marks are at the
    tail; we keep the tail ``MAX_LEDGER_ENTRIES``.
    """
    if len(ordered_ids) <= MAX_LEDGER_ENTRIES:
        return ordered_ids
    return ordered_ids[-MAX_LEDGER_ENTRIES:]


def _write_ids(owner: str, ordered_ids: list[str]) -> None:
    """Atomically persist ``ordered_ids`` (oldest-first) for ``owner``.

    Writes ``tmp`` then ``os.replace`` onto the final path so a reader never sees a
    partial file (matches the cursor's durability contract). Capped most-recent-first
    before writing.
    """
    capped = _cap_most_recent_first(ordered_ids)
    path = _ledger_path(owner)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f"{path.name}.tmp")
    with temporary_path.open("w", encoding="utf-8") as handle:
        json.dump({"message_ids": capped}, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_path, path)


# --------------------------------------------------------------------------- #
# Stateless one-shot API
# --------------------------------------------------------------------------- #
def load_processed_message_ids(owner: str) -> set[str]:
    """The set of message ids already processed for ``owner`` (best-effort)."""
    return set(_read_ids(owner))


def is_message_processed(message_id: str, owner: str) -> bool:
    """True when ``message_id`` is already in ``owner``'s processed ledger."""
    target = str(message_id or "").strip()
    if not target:
        return False
    return target in load_processed_message_ids(owner)


def mark_message_processed(message_id: str, owner: str) -> None:
    """Persist ``message_id`` as processed for ``owner`` (read-modify-write, once).

    Re-marking an existing id is a no-op (no rewrite). Prefer
    :class:`ProcessedLedgerSession` in a loop -- this one-shot re-reads and
    re-writes the whole file per call, which the batch session avoids.
    """
    target = str(message_id or "").strip()
    if not target:
        return
    ordered = _read_ids(owner)
    if target in ordered:
        return
    ordered.append(target)
    _write_ids(owner, ordered)


# --------------------------------------------------------------------------- #
# Batch session: load once, mark many, write once (REFINEMENT A)
# --------------------------------------------------------------------------- #
class ProcessedLedgerSession:
    """A load-once / mark-many / write-once view of one owner's processed ledger.

    Constructed once at the start of a poll (reads the file ONCE). The loop calls
    :meth:`is_processed` to skip and :meth:`mark` to record terminal outcomes
    (in-memory, no I/O). :meth:`flush` writes the file at most ONCE at the end -- and
    is a complete no-op when nothing new was marked this poll, so a steady-state poll
    that imports nothing performs ZERO ledger writes.
    """

    def __init__(self, owner: str) -> None:
        self._owner = owner
        # Ordered (oldest-first) so eviction is stable; the set is the membership
        # fast-path. New marks this session are tracked so flush can no-op when empty.
        self._ordered: list[str] = _read_ids(owner)
        self._known: set[str] = set(self._ordered)
        self._dirty = False

    def is_processed(self, message_id: str) -> bool:
        target = str(message_id or "").strip()
        if not target:
            return False
        return target in self._known

    def mark(self, message_id: str) -> None:
        """Record ``message_id`` as processed IN MEMORY (no write until :meth:`flush`)."""
        target = str(message_id or "").strip()
        if not target or target in self._known:
            return
        self._known.add(target)
        self._ordered.append(target)
        self._dirty = True

    @property
    def dirty(self) -> bool:
        """True when at least one NEW id was marked since load (flush will write)."""
        return self._dirty

    def processed_ids(self) -> set[str]:
        return set(self._known)

    def flush(self) -> bool:
        """Write the ledger ONCE if anything new was marked; else a no-op.

        Best-effort: a write failure is logged and swallowed (the unwritten ids
        simply re-process next poll -- correct, just not free), so persistence can
        never break the poll. Returns whether a write was performed.
        """
        if not self._dirty:
            return False
        try:
            _write_ids(self._owner, self._ordered)
        except OSError:
            LOGGER.warning(
                "Failed to persist Gmail processed-message ledger for owner",
                exc_info=True,
            )
            return False
        self._dirty = False
        return True


def mark_messages_processed(message_ids: Iterable[str], owner: str) -> int:
    """Batch one-shot: add ``message_ids`` to ``owner``'s ledger in a single write.

    A convenience for callers that already have the terminal-id set in hand and do
    not need the session's interleaved ``is_processed``. Returns the number of NEW
    ids persisted (0 means every id was already present, and no rewrite happened).
    """
    session = ProcessedLedgerSession(owner)
    before = len(session.processed_ids())
    for message_id in message_ids:
        session.mark(message_id)
    session.flush()
    return len(session.processed_ids()) - before
