"""Content fingerprinting for near-duplicate NDA detection — a pure leaf.

The Corpus tab surfaces a ``duplicate_document`` signal: "this matter's document
content is a near-duplicate of another matter's". The hard constraint is that this
must NOT reintroduce a per-build O(n²) full-text diff (the perf cost we just
removed). The fingerprint design makes the comparison a cheap scalar op:

* **Exact-dup key** — a sha256 of the *normalized* text. Two byte-identical (modulo
  whitespace/case) documents share this hex digest; a single ``==`` settles it.
* **Near-dup key** — a 64-bit **SimHash** over word-3-shingles. Two documents that
  differ only in small edits land at a tiny Hamming distance; similarity is
  ``1 - hamming(a, b) / 64`` and is computed with a single XOR + popcount. No
  token-by-token diff, no per-pair text walk — just two 64-bit integers.

A fingerprint is computed **once** per matter from its already-available
``extracted_text`` (NEVER by re-extracting the document) and stored on the matter.
Later corpus builds read the stored fingerprint and only ever scalar-compare. This
module owns the math and the (de)serialization; the lazy compute-and-cache and the
match resolution live in :mod:`corpus_index`.

Pure leaf: no I/O, no repository, no app imports. Fully unit-testable in isolation.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Optional

# Stamped on a stored fingerprint dict so a future algorithm change can invalidate
# old fingerprints by version rather than silently comparing incompatible hashes.
FINGERPRINT_SCHEMA_VERSION = 1

# SimHash width. 64 bits fits a Python int trivially and gives a fine-grained
# Hamming distance (1/64 ≈ 1.6% resolution), so the >=0.90 threshold maps to a
# Hamming distance of at most 6 bits.
_SIMHASH_BITS = 64
_SIMHASH_MASK = (1 << _SIMHASH_BITS) - 1

# Word-shingle width. 3-grams of words are the standard near-dup unit: long enough
# that incidental single-word collisions don't dominate, short enough that a few
# scattered edits leave most shingles intact.
_SHINGLE_SIZE = 3

# A document must carry at least this many normalized words to be fingerprintable
# for near-dup. Below it there are too few shingles for a SimHash to be meaningful
# (and two trivially-short docs would falsely collide), so near-dup is skipped and
# only the exact-dup sha256 is trusted.
_MIN_WORDS_FOR_SIMHASH = _SHINGLE_SIZE

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def normalize_text(text: str) -> str:
    """Casefold + collapse all runs of whitespace to single spaces; strip ends.

    The normalization the exact-dup sha256 is taken over: it makes
    formatting-only differences (case, indentation, blank-line counts, trailing
    spaces) collapse to one canonical string so a re-sent identical NDA hashes
    identically. Tolerant of a non-str input (coerced via ``str``).
    """
    return " ".join(str(text or "").split()).casefold()


def _words(normalized: str) -> list[str]:
    return _WORD_RE.findall(normalized)


def _shingle_hashes(words: list[str]) -> list[int]:
    """64-bit hash of each consecutive word-3-shingle. Empty when too few words."""
    if len(words) < _SHINGLE_SIZE:
        return []
    hashes: list[int] = []
    for i in range(len(words) - _SHINGLE_SIZE + 1):
        shingle = " ".join(words[i : i + _SHINGLE_SIZE])
        digest = hashlib.sha1(shingle.encode("utf-8")).digest()  # noqa: S324 -- not a security hash
        hashes.append(int.from_bytes(digest[:8], "big"))
    return hashes


def _simhash(words: list[str]) -> Optional[int]:
    """64-bit SimHash over the word-3-shingles, or ``None`` when too few words.

    Standard SimHash: for each shingle hash, each set bit votes +1 and each clear
    bit votes -1 across that bit column; the output bit is 1 where the column sum is
    positive. Similar documents share most shingles, so their SimHashes agree on
    most bits (small Hamming distance).
    """
    shingles = _shingle_hashes(words)
    if not shingles:
        return None
    columns = [0] * _SIMHASH_BITS
    for h in shingles:
        for bit in range(_SIMHASH_BITS):
            if (h >> bit) & 1:
                columns[bit] += 1
            else:
                columns[bit] -= 1
    value = 0
    for bit in range(_SIMHASH_BITS):
        if columns[bit] > 0:
            value |= 1 << bit
    return value & _SIMHASH_MASK


def compute_fingerprint(extracted_text: str) -> dict[str, Any] | None:
    """Compute the stored-fingerprint dict from a matter's ``extracted_text``.

    Returns ``{schema_version, exact, simhash, word_count}`` where:

    * ``exact``     — sha256 hex of the normalized text (always present);
    * ``simhash``   — 64-bit int as a decimal string, or ``None`` when the doc has
                      fewer than ``_MIN_WORDS_FOR_SIMHASH`` words (near-dup skipped);
    * ``word_count``— normalized word count (lets a consumer skip empty docs).

    Returns ``None`` for an empty/whitespace-only document — there is no content to
    fingerprint, so such a matter never participates in dup detection (two empty
    docs must not be flagged as duplicates of each other). Never raises.
    """
    normalized = normalize_text(extracted_text)
    if not normalized:
        return None
    words = _words(normalized)
    if not words:
        return None
    exact = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    simhash = _simhash(words) if len(words) >= _MIN_WORDS_FOR_SIMHASH else None
    return {
        "schema_version": FINGERPRINT_SCHEMA_VERSION,
        "exact": exact,
        "simhash": None if simhash is None else str(simhash),
        "word_count": len(words),
    }


def _coerce_simhash(value: Any) -> Optional[int]:
    """Read a stored simhash (decimal string or int) back to a masked int, or None."""
    if value is None:
        return None
    try:
        return int(value) & _SIMHASH_MASK
    except (TypeError, ValueError):
        return None


def is_valid_fingerprint(fingerprint: Any) -> bool:
    """True when ``fingerprint`` is a current-schema dict with a usable ``exact``.

    A stored fingerprint from an older schema version (or any odd shape) is treated
    as absent so the lazy-cache path recomputes it rather than scalar-comparing
    incompatible hashes.
    """
    return (
        isinstance(fingerprint, dict)
        and fingerprint.get("schema_version") == FINGERPRINT_SCHEMA_VERSION
        and isinstance(fingerprint.get("exact"), str)
        and bool(fingerprint["exact"])
    )


def is_exact_match(a: Any, b: Any) -> bool:
    """True when two stored fingerprints share an ``exact`` sha256 (identical text).

    The exact-dup oracle, kept separate from :func:`similarity` so the corpus can
    apply DIFFERENT gating to the two dup paths: an exact match (byte-identical
    modulo whitespace/case) is a true duplicate regardless of counterparty, whereas a
    near match (SimHash similarity < 1.0) is only a meaningful resend signal WITHIN a
    counterparty (two genuinely-different deals from one template score high on
    SimHash but are not duplicates). Defensive against odd/legacy fingerprints
    (returns ``False`` rather than raising).
    """
    if not is_valid_fingerprint(a) or not is_valid_fingerprint(b):
        return False
    return a["exact"] == b["exact"]


def similarity(a: Any, b: Any) -> float:
    """Scalar similarity in ``[0.0, 1.0]`` between two stored-fingerprint dicts.

    This is the ONLY comparison done per pair — no text is read here:

    * identical ``exact`` sha256          -> ``1.0`` (exact duplicate);
    * else both carry a ``simhash``       -> ``1 - hamming(a, b) / 64`` (XOR+popcount);
    * else (one/both lack a simhash and the exacts differ) -> ``0.0``.

    Defensive against missing/odd fields (returns ``0.0`` rather than raising), so a
    hand-edited or legacy stored fingerprint can never break the corpus build.
    """
    if not is_valid_fingerprint(a) or not is_valid_fingerprint(b):
        return 0.0
    if a["exact"] == b["exact"]:
        return 1.0
    sim_a = _coerce_simhash(a.get("simhash"))
    sim_b = _coerce_simhash(b.get("simhash"))
    if sim_a is None or sim_b is None:
        return 0.0
    hamming = ((sim_a ^ sim_b) & _SIMHASH_MASK).bit_count()
    return 1.0 - (hamming / _SIMHASH_BITS)
