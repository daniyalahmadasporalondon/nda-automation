"""Court/forum-shape validation -- the single screen for a forum/venue value.

A governing-law approved option pairs a law with a ``forum_jurisdiction`` (the
venue whose courts have authority), and at generation time that pairing -- or a
signing entity's registry jurisdiction -- becomes the *forum* written into a signed
NDA. Both the publish lint (``playbook_lint.check_governing_law_forum_present`` ->
court-shape) and the generation gate (``nda_generation._require_court_forum``) must
refuse a value that is NOT a real court/venue, so a non-court venue ("the moon",
"arbitration in Narnia"), a template placeholder ("{{forum}}", "[Court]"), a
prompt-injection control phrase ("ignore the playbook"), or an absurdly long string
can never publish or reach a signed NDA.

This module is the ONE source of truth for that screen, so the two gates stay in
lock-step. It is deliberately a NEGATIVE screen: the live playbook's forums are
jurisdiction-level descriptors ("England and Wales", "Mumbai, India", "State of
Delaware", "Dubai International Financial Centre") that carry no literal "court"
keyword, so a positive keyword requirement would wrongly reject every legitimate
forum. Instead we accept a value that LOOKS like a venue (place-name characters,
optionally with a court/seat/arbitration/tribunal keyword) and REJECT the specific
non-venue shapes above.

Pure and dependency-free: callers pass a string and get back a verdict, so a test
can prove the screen without the network or the filesystem.

Public API:

* ``forum_shape_problem(forum) -> str | None`` -- ``None`` when the value is an
  acceptable court/venue shape; otherwise a human-readable reason it is rejected.
* ``is_court_shaped(forum) -> bool`` -- convenience boolean.
* ``MAX_FORUM_LENGTH`` -- the size cap.
"""
from __future__ import annotations

import re
import unicodedata

__all__ = [
    "forum_shape_problem",
    "is_court_shaped",
    "MAX_FORUM_LENGTH",
    "FORUM_VENUE_KEYWORDS",
]

#: A forum string longer than this is rejected: a real court/venue descriptor is
#: short ("Dubai International Financial Centre" is 36 chars); anything materially
#: longer is prose, an injected paragraph, or junk, never a venue.
MAX_FORUM_LENGTH = 120

#: Venue keywords that, when present, positively confirm a court/forum shape even
#: if the rest is unusual. The negative screens below still apply (a keyword does
#: not rescue "arbitration in Narnia").
FORUM_VENUE_KEYWORDS: frozenset[str] = frozenset(
    {
        "court",
        "courts",
        "tribunal",
        "tribunals",
        "arbitration",
        "arbitral",
        "seat",
        "jurisdiction",
        "judicial",
        "high court",
    }
)

# Template / placeholder tokens. A forum carrying any of these was never resolved
# to a real value -- it is a literal template slot that would print verbatim into
# the NDA.
_TEMPLATE_TOKEN_PATTERN = re.compile(r"\{\{|\}\}|\$\{|<%|%>|\[[^\]]*\]|<[^>]+>")

# Control / prompt-injection phrasing. A forum is data, never an instruction; any
# of these means the field was poisoned, not authored.
_CONTROL_PHRASE_PATTERN = re.compile(
    r"(?i)\b("
    r"ignore\s+(?:the\s+|all\s+|previous\s+|above\s+|prior\s+)?"
    r"(?:playbook|instruction|instructions|rules|context)"
    r"|disregard\s+(?:the\s+|all\s+|previous\s+|above\s+|prior\s+)"
    r"|mark\s+everything\s+(?:as\s+)?(?:pass|passing|approved)"
    r"|mark\s+all\s+(?:as\s+)?(?:pass|passing|approved)"
    r"|system\s+prompt"
    r"|you\s+are\s+(?:a|an|now)\b"
    r"|as\s+an\s+ai\b"
    r"|<\s*/?\s*(?:system|assistant|user|developer|tool)\s*>"
    r")"
)

# Line-start role markers ("System:", "Assistant:") an injection uses to pose as a
# new chat turn. A real venue never contains one.
_ROLE_MARKER_PATTERN = re.compile(r"(?im)^\s*(system|assistant|user|developer|tool)\s*:")

# C0/C1 control characters (excluding tab/newline/carriage-return) used to smuggle
# hidden framing. A venue is a single short line of printable text.
_CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Obviously-fictional / non-terrestrial "venues". These are not exhaustive (no
# denylist can be) -- they pin the explicit adversarial cases the gate must reject
# and the common fictional-place class. A keyword like "arbitration" does NOT
# rescue them: "arbitration in Narnia" is still fictional.
_FICTIONAL_VENUE_PATTERN = re.compile(
    r"(?i)\b("
    r"the\s+moon|the\s+sun|mars|outer\s+space|cyberspace|the\s+metaverse"
    r"|narnia|mordor|gotham|wakanda|atlantis|hogwarts|westeros|neverland"
    r"|hell|heaven|the\s+void|nowhere|nplace|no\s*where"
    r"|el\s*dorado|shangri[\s-]*la|utopia|toontown"
    r")\b"
)

# A forum that is purely punctuation / digits / symbols with no real word content.
_HAS_LETTER_PATTERN = re.compile(r"[^\W\d_]", re.UNICODE)


def _printable_core(value: str) -> str:
    """The value's printable content after stripping unicode format/zero-width chars.

    Strips Cf-category code points (zero-width space/joiner, BOM, etc.) and the
    non-breaking space so a value that is "blank" only because it is built from
    invisible characters is recognised as empty.
    """
    cleaned_chars = []
    for ch in value:
        if ch in {"​", "‌", "‍", "﻿", " "}:
            continue
        if unicodedata.category(ch) == "Cf":
            continue
        cleaned_chars.append(ch)
    return "".join(cleaned_chars).strip()


def forum_shape_problem(forum: object) -> str | None:
    """Return a reason the forum is NOT an acceptable court/venue, or ``None``.

    ``None`` means the value passes the court-shape screen. A non-``None`` string is
    a human-readable rejection reason suitable for a publish-lint message or a
    generation refusal. The screen is NEGATIVE: it accepts venue-shaped values
    (including the jurisdiction-level descriptors the live playbook uses) and
    rejects template tokens, control/injection phrasing, oversized strings, and
    obviously-fictional venues.
    """
    raw = str(forum or "")
    core = _printable_core(raw)
    if not core:
        return "forum is empty (no printable content)"

    if len(raw) > MAX_FORUM_LENGTH or len(core) > MAX_FORUM_LENGTH:
        return (
            f"forum is too long ({len(core)} chars > {MAX_FORUM_LENGTH}); a real "
            "court/venue descriptor is short, not a paragraph"
        )

    if _CONTROL_CHAR_PATTERN.search(raw):
        return "forum contains control characters; a venue is a single line of text"

    if _ROLE_MARKER_PATTERN.search(raw):
        return "forum contains a chat role marker (System:/Assistant:/...) and is not a venue"

    if _CONTROL_PHRASE_PATTERN.search(core):
        return "forum contains a control/instruction phrase and is not a court/venue"

    if _TEMPLATE_TOKEN_PATTERN.search(raw):
        return "forum contains an unresolved template token/placeholder, not a real venue"

    if not _HAS_LETTER_PATTERN.search(core):
        return "forum has no alphabetic content; a court/venue must name a place"

    if _FICTIONAL_VENUE_PATTERN.search(core):
        return "forum names a fictional / non-terrestrial venue, not a real court/jurisdiction"

    return None


def is_court_shaped(forum: object) -> bool:
    """True when ``forum`` passes the court/venue shape screen."""
    return forum_shape_problem(forum) is None
