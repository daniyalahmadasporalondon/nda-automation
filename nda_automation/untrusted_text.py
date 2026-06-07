"""Shared neutralizer for attacker-controlled text before it enters an AI prompt.

Several AI seams embed third-party text (a counterparty's NDA paragraphs, an
inbound email's subject/body, attachment previews) into a model packet. That text
is untrusted DATA, never instructions, but a prompt-injection payload can still try
to pose as a new chat turn ("System:", "Assistant:") or smuggle control characters
to break out of the surrounding JSON framing.

This module owns the one shared defence: strip control characters and defang
line-start role markers so the data cannot impersonate an instruction block. It is
deliberately dependency-free so any prompt builder can import it without pulling in
network transport or settings. Content is otherwise preserved so the model can still
classify/review the document; each caller's hard output constraint (enumerated
attachment ids, quote grounding) is what ultimately bounds what the data can do.
"""
from __future__ import annotations

import re

# Markers an injection payload uses to impersonate a new turn/role or a control
# delimiter at the start of a line. We defang them so the data cannot pose as a
# separate instruction block.
INJECTION_MARKER_PATTERN = re.compile(
    r"(?im)^\s*(system|assistant|user|developer|tool)\s*:",
)
# C0/C1 control characters (excluding tab/newline/carriage-return) that could be
# used to smuggle hidden framing.
CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def neutralize_untrusted_text(value: object, max_chars: int | None = None) -> str:
    """Render attacker-controlled text inert before it enters a prompt.

    Strips control characters and defangs line-start role markers
    ("System:", "Assistant:", ...) so the text cannot impersonate an instruction
    block. When ``max_chars`` is given the result is truncated to that length.
    Content is otherwise preserved.
    """
    text = str(value or "")
    text = CONTROL_CHAR_PATTERN.sub(" ", text)
    text = INJECTION_MARKER_PATTERN.sub(lambda match: match.group(0).replace(":", " -"), text)
    if max_chars is not None:
        return text[:max_chars]
    return text
