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
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from . import matter_store

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for local dev portability.
    fcntl = None

LOGGER = logging.getLogger(__name__)

# Hard cap on stored ids per owner. Bounds the on-disk file (and the in-memory set)
# regardless of inbox size. When exceeded we keep the MOST-RECENTLY-marked ids and
# evict the oldest, so the live frontier (the ids most likely to re-surface on the
# next poll) is always retained; an evicted old id simply falls back to the full
# path once -- correct, just not free.
MAX_LEDGER_ENTRIES = 20000

# Hard cap on tracked TRANSIENT-FAILURE attempt counters per owner (the quarantine
# accounting below). Attempt entries are short-lived by design -- an id is dropped
# the moment its message reaches a terminal outcome (marked processed or
# quarantined) -- so this cap only bites a pathological inbox with thousands of
# concurrently-failing messages; eviction keeps the most-recently-touched counters.
MAX_ATTEMPT_ENTRIES = 5000

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


# Maximum seconds to wait for the on-disk ledger flock (mirrors
# matter_store._LOCK_TIMEOUT_SECONDS semantics: non-blocking flock polled at
# 10ms, bounded deadline, never sleep inside a kernel call).
_LEDGER_LOCK_TIMEOUT_SECONDS = 10


@contextmanager
def _ledger_flock(owner: str):
    """Cross-process exclusive lock over one owner's ledger writes.

    NDA_PROCESS_ROLE (web/worker split, docs/PROCESS_ROLES.md) means the ledger
    has concurrent WRITERS in different processes on one shared volume: the
    worker's scheduled-poll session flush, the web manual-import session flush
    (POST /api/gmail/import), and the web bulk-archive re-import guard
    (mark_messages_processed). The whole-file tmp+os.replace write is atomic
    but last-writer-wins, so without this lock one writer's marks silently
    erase another's -> duplicate imports, re-spent AI intake, and bulk-archived
    junk resurrecting. Every writer therefore takes this flock and MERGES with
    the re-read on-disk state before writing (see ProcessedLedgerSession.flush).

    Mirrors the flock idiom of ``matter_store._locked_store`` /
    ``server._gmail_sync_process_lock``: LOCK_EX|LOCK_NB polled at 10ms with a
    bounded deadline. Timeout raises ``TimeoutError`` (an ``OSError``), so the
    best-effort flush path degrades exactly like any other write failure: the
    unwritten marks simply re-process next poll.
    """
    directory = _owner_dir(owner)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / f"{_LEDGER_FILENAME}.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        if fcntl is not None:
            deadline = time.monotonic() + _LEDGER_LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            "Gmail processed-message ledger lock could not be acquired "
                            f"within the timeout ({_LEDGER_LOCK_TIMEOUT_SECONDS}s)."
                        ) from exc
                    time.sleep(0.01)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _merge_with_on_disk(
    on_disk: dict[str, object],
    session_marked: list[str],
    session_attempts: dict[str, int],
    session_quarantined: dict[str, dict[str, object]],
) -> tuple[list[str], dict[str, int], dict[str, dict[str, object]]]:
    """UNION the current on-disk state with a session's DELTA (never its snapshot).

    Called under ``_ledger_flock`` so the on-disk view cannot move beneath us.
    The inputs are only what the session itself CHANGED since load -- ids it
    marked (``session_marked``), attempt counters it touched, quarantine
    records it added -- NOT the full load-time snapshot. Merging the snapshot
    would resurrect another writer's REMOVALS (an admin
    ``requeue_quarantined_message`` racing an open session): the on-disk state
    is the removal authority, and a delta the session never touched cannot
    overrule it. Sessions themselves only ever ADD or terminate retry
    counting, so this union is lossless in both directions:

    * ``message_ids``: on-disk order first (it keeps its oldest-first order),
      then the session's newly-marked ids not already on disk, in mark order.
      The write re-applies the size cap, so an id the OTHER writer evicted
      while we held it merely re-enters at the tail (most-recent, correct)
      IF this session re-marked it -- an untouched id stays evicted/removed.
    * ``quarantined``: keyed union; the session's record wins for an id the
      session actually quarantined (it is the newer observation). On-disk
      records the session never touched pass through untouched -- and on-disk
      ABSENCE of a record the session never touched stays absent.
    * ``attempts``: keyed union taking max(disk, session) per touched id --
      two processes that each counted failures never LOSE progress toward the
      quarantine threshold (max undercounts a true sum, which only delays
      quarantine by a poll -- safe). Then every id terminal in the merged view
      (marked processed) drops its counter, preserving the invariant that the
      attempts blob only holds still-retrying ids and honoring terminal
      deletions from BOTH sides.
    """
    disk_ids_raw = on_disk.get("message_ids")
    disk_ids: list[str] = list(disk_ids_raw) if isinstance(disk_ids_raw, list) else []
    disk_id_set = set(disk_ids)
    merged_ids = disk_ids + [mid for mid in session_marked if mid not in disk_id_set]

    disk_quarantined = on_disk.get("quarantined")
    merged_quarantined: dict[str, dict[str, object]] = (
        dict(disk_quarantined) if isinstance(disk_quarantined, dict) else {}
    )
    merged_quarantined.update(session_quarantined)

    disk_attempts = on_disk.get("attempts")
    merged_attempts: dict[str, int] = dict(disk_attempts) if isinstance(disk_attempts, dict) else {}
    for message_id, count in session_attempts.items():
        try:
            merged_attempts[message_id] = max(int(merged_attempts.get(message_id, 0)), int(count))
        except (TypeError, ValueError):
            merged_attempts[message_id] = int(count)
    terminal = set(merged_ids)
    for message_id in list(merged_attempts):
        if message_id in terminal:
            del merged_attempts[message_id]
    return merged_ids, merged_attempts, merged_quarantined


def _read_payload(owner: str) -> dict[str, object]:
    """Read + normalise the persisted ledger payload for ``owner``.

    Returns ``{"message_ids": [...], "attempts": {...}, "quarantined": {...},
    "degraded": bool}``. ``message_ids`` is ordered oldest-first (so the
    most-recent-first eviction has a stable notion of "oldest"); ``attempts`` maps
    message id -> transient-failure attempt count; ``quarantined`` maps message id
    -> ``{"attempts", "reason", "last_at"}`` (a DISTINCT keyed structure so
    quarantined ids are findable/removable without touching the normal marks -- the
    requeue path below depends on this). A missing file, a legacy ids-only file, or
    extra keys written by a NEWER version all degrade cleanly (rollback-safe both
    directions). ``degraded`` is True only when the file EXISTED but was unreadable
    (corrupt) -- callers use it to avoid clobbering a recoverable file with a
    from-empty rewrite.
    """
    path = _ledger_path(owner)
    degraded = False
    try:
        file_exists = path.is_file()
    except OSError:  # pragma: no cover - stat failure treated as missing
        file_exists = False
    payload: object = {}
    if file_exists:
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            payload = {}
            degraded = True
    attempts: dict[str, int] = {}
    quarantined: dict[str, dict[str, object]] = {}
    if isinstance(payload, dict):
        attempts = _coerce_attempts(payload.get("attempts"))
        quarantined = _coerce_quarantined(payload.get("quarantined"))
    elif file_exists and not isinstance(payload, list):
        degraded = True
    message_ids = _coerce_id_list(payload)
    if file_exists and isinstance(payload, dict) and not message_ids and not attempts and not quarantined:
        # A present-but-unusable dict (e.g. hand-mangled keys) is also treated as
        # degraded ONLY when it yields nothing -- an intact empty ledger
        # ({"message_ids": []}) is NOT degraded.
        if not isinstance(payload.get("message_ids"), list):
            degraded = True
    return {
        "message_ids": message_ids,
        "attempts": attempts,
        "quarantined": quarantined,
        "degraded": degraded,
    }


def _read_ids(owner: str) -> list[str]:
    """The persisted processed ids for ``owner`` as an ORDERED list (oldest-first)."""
    ids = _read_payload(owner)["message_ids"]
    return ids if isinstance(ids, list) else []


def _coerce_attempts(payload: object) -> dict[str, int]:
    """Normalise a loaded attempts blob into ``{message_id: positive count}``."""
    if not isinstance(payload, dict):
        return {}
    attempts: dict[str, int] = {}
    for key, value in payload.items():
        message_id = str(key or "").strip()
        if not message_id:
            continue
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count > 0:
            attempts[message_id] = count
    return attempts


def _coerce_quarantined(payload: object) -> dict[str, dict[str, object]]:
    """Normalise a loaded quarantined blob into ``{id: {attempts, reason, last_at}}``.

    Accepts the canonical keyed-dict shape and, defensively, a bare id list (an
    early/hand-edited file), which becomes entries with empty metadata.
    """
    quarantined: dict[str, dict[str, object]] = {}
    if isinstance(payload, dict):
        for key, value in payload.items():
            message_id = str(key or "").strip()
            if not message_id:
                continue
            entry = value if isinstance(value, dict) else {}
            try:
                attempts = max(0, int(entry.get("attempts") or 0))
            except (TypeError, ValueError):
                attempts = 0
            quarantined[message_id] = {
                "attempts": attempts,
                "reason": str(entry.get("reason") or ""),
                "last_at": str(entry.get("last_at") or ""),
                "filename": str(entry.get("filename") or ""),
            }
    elif isinstance(payload, list):
        for value in payload:
            message_id = str(value or "").strip()
            if message_id and message_id not in quarantined:
                quarantined[message_id] = {"attempts": 0, "reason": "", "last_at": "", "filename": ""}
    return quarantined


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


def _cap_mapping_most_recent(mapping: dict, cap: int = MAX_ATTEMPT_ENTRIES) -> dict:
    """Keep the most-recently-touched entries (insertion-order tail) of a mapping.

    Shared by ``_write_ids`` (the attempts/quarantined size caps) and the
    session's post-flush adoption, so the in-memory view after a flush is
    capped EXACTLY as the persisted payload was.
    """
    if len(mapping) <= cap:
        return mapping
    return dict(list(mapping.items())[-cap:])


def _write_ids(
    owner: str,
    ordered_ids: list[str],
    attempts: dict[str, int] | None = None,
    quarantined: dict[str, dict[str, object]] | None = None,
) -> None:
    """Atomically persist the ledger payload (ids oldest-first) for ``owner``.

    Writes ``tmp`` then ``os.replace`` onto the final path so a reader never sees a
    partial file (matches the cursor's durability contract). Capped most-recent-first
    before writing. The ``attempts`` / ``quarantined`` sections are OMITTED when
    empty so a ledger that never quarantined stays byte-identical to the legacy
    ids-only shape (an OLDER reader simply ignores the extra keys either way).
    """
    capped = _cap_most_recent_first(ordered_ids)
    payload: dict[str, object] = {"message_ids": capped}
    if attempts:
        payload["attempts"] = _cap_mapping_most_recent(attempts)
    if quarantined:
        payload["quarantined"] = _cap_mapping_most_recent(quarantined)
    path = _ledger_path(owner)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f"{path.name}.tmp")
    with temporary_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
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
    re-writes the whole file per call, which the batch session avoids. Delegates
    to the session so the write inherits the session's merge-under-flock
    cross-process safety.
    """
    target = str(message_id or "").strip()
    if not target:
        return
    mark_messages_processed([target], owner)


def requeue_quarantined_message(message_id: str, owner: str) -> bool:
    """MANUAL RECOVERY: release a quarantined message so the next poll retries it.

    Removes ``message_id`` from the processed marks, the quarantined records, AND
    the attempt counters, so the message falls through the ledger pre-skip and the
    quarantine pre-check and re-enters the full import path with a clean slate.
    Returns True when the message was quarantined (or marked) and is now released.

    Operator one-liner (run in the app environment on the host):

        python -c "from nda_automation.gmail_processed_ledger import \\
            requeue_quarantined_message as r; print(r('<message_id>', '<owner>'))"
    """
    target = str(message_id or "").strip()
    if not target:
        return False
    # REMOVAL is the one operation a union-merge cannot express, so the whole
    # read-modify-write runs under the cross-process flock: re-read inside the
    # lock, remove, write. (A poll session that loaded BEFORE this requeue and
    # flushes after may union the id back in -- same as the pre-split
    # semantics; this operator action is meant to run between polls, and a
    # re-requeue recovers.)
    with _ledger_flock(owner):
        payload = _read_payload(owner)
        ordered: list[str] = list(payload["message_ids"])  # type: ignore[arg-type]
        attempts: dict[str, int] = dict(payload["attempts"])  # type: ignore[arg-type]
        quarantined: dict[str, dict[str, object]] = dict(payload["quarantined"])  # type: ignore[arg-type]
        removed = False
        if target in ordered:
            ordered = [existing for existing in ordered if existing != target]
            removed = True
        if attempts.pop(target, None) is not None:
            removed = True
        if quarantined.pop(target, None) is not None:
            removed = True
        if removed:
            _write_ids(owner, ordered, attempts, quarantined)
    return removed


def quarantined_messages(owner: str) -> dict[str, dict[str, object]]:
    """The durable quarantine records for ``owner`` (id -> attempts/reason/last_at)."""
    payload = _read_payload(owner)
    quarantined = payload["quarantined"]
    return dict(quarantined) if isinstance(quarantined, dict) else {}


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
        payload = _read_payload(owner)
        # Ordered (oldest-first) so eviction is stable; the set is the membership
        # fast-path. New marks this session are tracked so flush can no-op when empty.
        self._ordered: list[str] = list(payload["message_ids"])  # type: ignore[arg-type]
        self._known: set[str] = set(self._ordered)
        # Transient-failure attempt counters (message id -> count) feeding the
        # poison-message quarantine, plus the durable KEYED quarantine records
        # (id -> attempts/reason/last_at; distinct from the plain marks so an
        # operator can find + requeue them without touching normal marks).
        self._attempts: dict[str, int] = dict(payload["attempts"])  # type: ignore[arg-type]
        self._quarantined: dict[str, dict[str, object]] = dict(payload["quarantined"])  # type: ignore[arg-type]
        # The file existed but was unreadable: flushing from this empty in-memory
        # view would CLOBBER a possibly-recoverable file with only this poll's
        # marks. flush() sidelines the corrupt file first (forensics + safety).
        self._load_degraded = bool(payload.get("degraded"))
        self._dirty = False
        # DELTA tracking (web/worker split): flush() merges ONLY what THIS
        # session actually changed -- ids marked, attempt counters touched,
        # quarantine records added -- with the re-read on-disk state. Merging
        # the whole load-time SNAPSHOT instead would resurrect another writer's
        # REMOVALS: an admin ``requeue_quarantined_message`` racing an open poll
        # session would see its released id (and its stale quarantine record)
        # silently re-added by the poll's flush, putting the message back into
        # never-reprocess state.
        self._session_marked: list[str] = []
        self._session_attempts: dict[str, int] = {}
        self._session_quarantined: dict[str, dict[str, object]] = {}

    def is_processed(self, message_id: str) -> bool:
        target = str(message_id or "").strip()
        if not target:
            return False
        return target in self._known

    def mark(self, message_id: str) -> None:
        """Record ``message_id`` as processed IN MEMORY (no write until :meth:`flush`)."""
        target = str(message_id or "").strip()
        if not target:
            return
        if target in self._attempts:
            # A terminal outcome ends retry counting; drop the counter so the
            # attempts blob only ever holds still-retrying ids. Dropped from the
            # session delta too -- the merge's terminal-drop (any id processed in
            # the merged view loses its counter) makes the deletion durable.
            del self._attempts[target]
            self._session_attempts.pop(target, None)
            self._dirty = True
        if target in self._known:
            return
        self._known.add(target)
        self._ordered.append(target)
        self._session_marked.append(target)
        self._dirty = True

    # -- transient-failure attempt counting (poison-message quarantine) ------ #
    def attempt_count(self, message_id: str) -> int:
        """How many recorded transient-failure attempts ``message_id`` has."""
        target = str(message_id or "").strip()
        if not target:
            return 0
        return int(self._attempts.get(target, 0))

    def record_attempt(self, message_id: str) -> int:
        """Count one more transient-failure attempt; returns the new total.

        In-memory until :meth:`flush` (same write-once contract as :meth:`mark`).
        Re-inserted at the tail so the attempts blob's insertion order tracks
        most-recently-touched, which is what the size cap keeps.
        """
        target = str(message_id or "").strip()
        if not target:
            return 0
        count = int(self._attempts.pop(target, 0)) + 1
        self._attempts[target] = count
        self._session_attempts.pop(target, None)
        self._session_attempts[target] = count
        self._dirty = True
        return count

    def quarantine(
        self, message_id: str, *, reason: str = "", attempts: int = 0, filename: str = ""
    ) -> None:
        """Terminally quarantine ``message_id``: processed + a keyed quarantine record.

        A quarantined message is skipped before any fetch/AI work on future polls
        (exactly like any processed id) and carries a durable
        ``{attempts, reason, last_at, filename}`` record so an operator can see
        WHAT failed and WHY straight from ``quarantined_messages()`` (no log
        archaeology) and release it via :func:`requeue_quarantined_message`.
        """
        target = str(message_id or "").strip()
        if not target:
            return
        self.mark(target)
        if target not in self._quarantined:
            record: dict[str, object] = {
                "attempts": max(0, int(attempts or 0)),
                "reason": str(reason or "")[:200],
                "last_at": datetime.now(timezone.utc).isoformat(),
                "filename": str(filename or "")[:200],
            }
            self._quarantined[target] = record
            self._session_quarantined[target] = record
            self._dirty = True

    def quarantined_ids(self) -> set[str]:
        return set(self._quarantined)

    @property
    def dirty(self) -> bool:
        """True when at least one NEW id was marked since load (flush will write)."""
        return self._dirty

    def processed_ids(self) -> set[str]:
        return set(self._known)

    def flush(self) -> bool:
        """MERGE-UNDER-FLOCK write of the ledger if anything new was marked.

        Cross-process safe (web/worker split): under ``_ledger_flock`` the
        on-disk ledger is RE-READ and UNIONED with this session's DELTA -- only
        what this session itself marked/counted/quarantined, never the whole
        load-time snapshot (``_merge_with_on_disk``) -- before the atomic
        tmp+os.replace write. That gives both directions of safety: a concurrent
        writer's MARKS survive (no last-writer-wins erasure between the worker
        poll, a web manual import, and the web bulk-archive guard), and a
        concurrent writer's REMOVALS survive too (an admin
        ``requeue_quarantined_message`` that raced this session is not undone by
        replaying our stale snapshot of the removed id).

        Best-effort: a write/lock failure is logged and swallowed (the unwritten
        ids simply re-process next poll -- correct, just not free), so
        persistence can never break the poll. Returns whether a write was
        performed.
        """
        if not self._dirty:
            return False
        try:
            with _ledger_flock(self._owner):
                if self._load_degraded:
                    # The load saw an EXISTING but unreadable file. Never clobber
                    # it in place: sideline it (forensics + recoverability) and
                    # only then write the fresh ledger. Sideline failure aborts
                    # the write -- the marks re-process next poll (correct, just
                    # not free) rather than risking silent loss of up to 20k
                    # prior marks.
                    self._sideline_corrupt_file()
                    self._load_degraded = False
                on_disk = _read_payload(self._owner)
                if on_disk.get("degraded"):
                    # The file became unreadable AFTER our load (rewritten
                    # corrupt by something else): sideline it too and merge
                    # against empty.
                    self._sideline_corrupt_file()
                    on_disk = {"message_ids": [], "attempts": {}, "quarantined": {}}
                merged_ids, merged_attempts, merged_quarantined = _merge_with_on_disk(
                    on_disk,
                    self._session_marked,
                    self._session_attempts,
                    self._session_quarantined,
                )
                _write_ids(self._owner, merged_ids, merged_attempts, merged_quarantined)
        except OSError:
            LOGGER.warning(
                "Failed to persist Gmail processed-message ledger for owner",
                exc_info=True,
            )
            return False
        # Adopt EXACTLY the persisted view (same caps _write_ids applied) so
        # later marks/flushes on this session build on the on-disk truth --
        # including the other writer's marks AND removals -- and never keep an
        # uncapped superset alive in memory. The delta trackers reset: their
        # changes are durable now, so the next flush merges only what comes
        # after this point.
        self._ordered = _cap_most_recent_first(merged_ids)
        self._known = set(self._ordered)
        self._attempts = _cap_mapping_most_recent(merged_attempts)
        self._quarantined = _cap_mapping_most_recent(merged_quarantined)
        self._session_marked = []
        self._session_attempts = {}
        self._session_quarantined = {}
        self._dirty = False
        return True

    def _sideline_corrupt_file(self) -> None:
        """Rename an unreadable ledger file to ``*.corrupt`` before rewriting."""
        path = _ledger_path(self._owner)
        if not path.is_file():
            return
        corrupt_path = path.with_name(f"{path.name}.corrupt")
        os.replace(path, corrupt_path)
        LOGGER.warning(
            "Gmail processed-message ledger was unreadable; sidelined to %s before rewrite",
            corrupt_path,
        )


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
