from __future__ import annotations

import re
from typing import Dict, List, Tuple

INLINE_DIFF_MAX_MATRIX_CELLS = 40000
# Char-level diff runs ONCE per export (not per keystroke), so the larger matrix
# is affordable; this matches the frontend's char-diff guard in redline-rendering.js.
CHAR_DIFF_MAX_MATRIX_CELLS = 1_000_000
INLINE_TOKEN_PATTERN = re.compile(r"\s*(?:\d+(?:[,.]\d+)*|[^\W_]+(?:[-'’][^\W_]+)*|[^\s])|\s+")
DiffOperation = Tuple[str, str]


def diff_text_operations(original: str, replacement: str) -> List[DiffOperation]:
    old_tokens = tokenize_inline_diff(original)
    new_tokens = tokenize_inline_diff(replacement)
    if not old_tokens:
        return [("insert", token) for token in new_tokens]
    if not new_tokens:
        return [("delete", token) for token in old_tokens]
    if len(old_tokens) * len(new_tokens) > INLINE_DIFF_MAX_MATRIX_CELLS:
        return [("delete", str(original or "")), ("insert", str(replacement or ""))]
    return diff_token_operations(old_tokens, new_tokens)


def diff_text_operation_dicts(original: str, replacement: str) -> List[Dict[str, str]]:
    return [
        {"type": operation_type, "token": token}
        for operation_type, token in diff_text_operations(original, replacement)
    ]


def diff_text_char_operations(original: str, replacement: str) -> List[DiffOperation]:
    """Character-level inline diff for free-form manual edits.

    Mirrors the frontend ``charDiffOperations`` in redline-rendering.js: tokens are
    SINGLE characters, diffed with the same LCS as the token path, so only the
    changed letters redline (e.g. "color" -> "colour" inserts just "u"). The char
    tokens carry their own whitespace, so the caller must batch them VERBATIM (no
    inter-token spacing). Falls back to a whole delete+insert on a pathologically
    large matrix (its own larger guard, since the export runs this only once).
    """
    old_tokens = tokenize_inline_diff_chars(original)
    new_tokens = tokenize_inline_diff_chars(replacement)
    if not old_tokens:
        return [("insert", token) for token in new_tokens]
    if not new_tokens:
        return [("delete", token) for token in old_tokens]
    if len(old_tokens) * len(new_tokens) > CHAR_DIFF_MAX_MATRIX_CELLS:
        return [("delete", str(original or "")), ("insert", str(replacement or ""))]
    return diff_token_operations(old_tokens, new_tokens)


def tokenize_inline_diff(text: str) -> List[str]:
    return INLINE_TOKEN_PATTERN.findall(str(text or ""))


def tokenize_inline_diff_chars(text: str) -> List[str]:
    """One entry per character, tiling the text exactly (mirrors the frontend's
    ``[...str]``). Concatenating the result reproduces the input byte-for-byte."""
    return list(str(text or ""))


def diff_token_operations(old_tokens: List[str], new_tokens: List[str]) -> List[DiffOperation]:
    row_count = len(old_tokens) + 1
    column_count = len(new_tokens) + 1
    dp = [[0] * column_count for _ in range(row_count)]

    for old_index in range(len(old_tokens) - 1, -1, -1):
        for new_index in range(len(new_tokens) - 1, -1, -1):
            if old_tokens[old_index] == new_tokens[new_index]:
                dp[old_index][new_index] = dp[old_index + 1][new_index + 1] + 1
            else:
                dp[old_index][new_index] = max(dp[old_index + 1][new_index], dp[old_index][new_index + 1])

    operations: List[DiffOperation] = []
    old_index = 0
    new_index = 0
    while old_index < len(old_tokens) and new_index < len(new_tokens):
        if old_tokens[old_index] == new_tokens[new_index]:
            operations.append(("same", old_tokens[old_index]))
            old_index += 1
            new_index += 1
        elif dp[old_index + 1][new_index] >= dp[old_index][new_index + 1]:
            operations.append(("delete", old_tokens[old_index]))
            old_index += 1
        else:
            operations.append(("insert", new_tokens[new_index]))
            new_index += 1

    while old_index < len(old_tokens):
        operations.append(("delete", old_tokens[old_index]))
        old_index += 1
    while new_index < len(new_tokens):
        operations.append(("insert", new_tokens[new_index]))
        new_index += 1
    return operations
