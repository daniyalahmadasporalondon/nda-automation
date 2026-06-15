#!/usr/bin/env python3
"""Recurrence guard for the static cache-bust-token bug.

Background
----------
``static/index.html`` references every CSS/JS asset with a manual cache-bust
query token, e.g. ``/static/app.js?v=20260610g``. The browser pins itself to a
versioned URL: if an asset's *bytes* change but its ``?v=`` token does **not**,
already-loaded clients keep serving the stale asset forever (the server hands
out fresh bytes, the browser never re-requests them). We just had to hand-fix
12 such stale tokens.

This guard makes that bug class fail loudly at commit / CI time: if a static
asset referenced from ``index.html`` is changed in a commit but its ``?v=``
token in ``index.html`` is *not* also changed, the guard reports a violation.

Design
------
* :func:`find_violations` is the pure core. It takes the *old* and *new*
  ``index.html`` text plus the set of changed asset paths, and returns the list
  of assets whose bytes changed without a token bump. It has no git or
  filesystem dependency so it is trivially unit-testable.
* :func:`run_git_guard` is the thin git wrapper used by the pre-commit hook
  (``--staged``) and by CI (``--base <ref>``). It resolves what changed from git
  and feeds the pure core.

Exit codes (CLI): 0 = clean, 1 = violations found, 2 = usage / git error.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_REL = "static/index.html"

# Matches href="/static/....?v=TOKEN" and src="/static/....?v=TOKEN".
# Group 1 = asset path after /static/ (no query), group 2 = the version token.
_REF_RE = re.compile(
    r'(?:href|src)="/static/([^"?]+)\?v=([^"]+)"'
)


@dataclass(frozen=True)
class Violation:
    """A referenced asset that changed without its ``?v=`` token changing."""

    asset_path: str  # repo-root-relative, e.g. "static/app.js"
    old_token: Optional[str]
    new_token: Optional[str]

    def describe(self) -> str:
        if self.old_token is None or self.new_token is None:
            return (
                f"{self.asset_path}: changed, but is not referenced with a ?v= "
                f"token in {INDEX_REL} (cannot be cache-busted)"
            )
        return (
            f"{self.asset_path}: bytes changed but ?v= token did not "
            f"(still ?v={self.new_token}). Bump the token in {INDEX_REL}."
        )


def parse_tokens(index_html: str) -> Dict[str, str]:
    """Map repo-root-relative asset path -> version token from index.html.

    ``href="/static/app.js?v=20260610g"`` -> ``{"static/app.js": "20260610g"}``.
    """
    tokens: Dict[str, str] = {}
    for asset, token in _REF_RE.findall(index_html):
        tokens["static/" + asset] = token
    return tokens


def find_violations(
    *,
    old_index_html: str,
    new_index_html: str,
    changed_asset_paths: Iterable[str],
) -> List[Violation]:
    """Pure core: which changed assets were *not* re-tokenised.

    Parameters
    ----------
    old_index_html / new_index_html:
        Contents of ``static/index.html`` before and after the change.
    changed_asset_paths:
        Repo-root-relative paths of static assets that changed in this commit
        (e.g. ``{"static/app.js"}``). Paths that index.html does not reference
        with a ``?v=`` token are ignored *unless* they live under ``static/``
        with a known asset extension (those are reported as un-bustable).
    """
    old_tokens = parse_tokens(old_index_html)
    new_tokens = parse_tokens(new_index_html)

    violations: List[Violation] = []
    for asset in sorted(set(changed_asset_paths)):
        new_token = new_tokens.get(asset)
        if new_token is None:
            # The asset changed but index.html does not (any longer) reference
            # it with a ?v= token. If it is a bustable asset type under static/,
            # that is itself a problem (it can never be cache-busted). Assets
            # not referenced at all (fonts, images served directly, etc.) are
            # out of scope and skipped.
            if asset in old_tokens:
                # It used to be referenced and the reference/token vanished
                # while the bytes changed -- treat as un-bustable.
                violations.append(Violation(asset, old_tokens.get(asset), None))
            continue
        old_token = old_tokens.get(asset)
        if old_token == new_token:
            violations.append(Violation(asset, old_token, new_token))
    return violations


# --- git-backed wiring ------------------------------------------------------


def _git(args: Sequence[str], repo_root: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _is_tracked_asset(path: str) -> bool:
    """Only guard CSS/JS assets under static/ (these carry ?v= tokens)."""
    return path.startswith("static/") and path.rsplit(".", 1)[-1] in {"css", "js", "mjs"}


def _changed_files(*, staged: bool, base: Optional[str], repo_root: Path) -> List[str]:
    if staged:
        out = _git(["diff", "--cached", "--name-only", "--diff-filter=ACMR"], repo_root)
    else:
        if not base:
            raise ValueError("a base ref is required when not running in --staged mode")
        out = _git(
            ["diff", "--name-only", "--diff-filter=ACMR", f"{base}...HEAD"], repo_root
        )
    return [line.strip() for line in out.splitlines() if line.strip()]


def _index_html_at(rev: Optional[str], *, staged: bool, repo_root: Path) -> str:
    """Read index.html as of a revision.

    * ``rev=None`` + ``staged=True`` -> the staged (index) version.
    * ``rev`` given -> that committed revision (empty string if absent).
    """
    try:
        if staged and rev is None:
            return _git(["show", f":{INDEX_REL}"], repo_root)
        return _git(["show", f"{rev}:{INDEX_REL}"], repo_root)
    except subprocess.CalledProcessError:
        return ""


def _discover_repo_root() -> Path:
    """Resolve the git toplevel from the current working directory.

    The guard runs *inside* the repo it is checking (pre-commit hook, CI
    checkout), so the repo is wherever git is invoked from -- not necessarily
    where this script file lives (it may have been vendored elsewhere). Falling
    back to this module's location keeps direct ``python scripts/...`` calls
    working from the repo root too.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        return Path(out.stdout.strip())
    except (subprocess.CalledProcessError, OSError):
        return REPO_ROOT


def run_git_guard(
    *,
    staged: bool,
    base: Optional[str],
    repo_root: Optional[Path] = None,
) -> List[Violation]:
    """Resolve git state and run :func:`find_violations`.

    * Pre-commit mode (``staged=True``): compare the staged index.html against
      ``HEAD`` and look at staged asset changes.
    * CI mode (``base`` given): compare the working/HEAD index.html against the
      base ref and look at assets changed in ``base...HEAD``.

    ``repo_root`` defaults to the git toplevel discovered from the current
    working directory.
    """
    if repo_root is None:
        repo_root = _discover_repo_root()
    changed = _changed_files(staged=staged, base=base, repo_root=repo_root)
    changed_assets = [p for p in changed if _is_tracked_asset(p)]
    if not changed_assets:
        return []

    if staged:
        old_index = _index_html_at("HEAD", staged=False, repo_root=repo_root)
        new_index = _index_html_at(None, staged=True, repo_root=repo_root)
    else:
        old_index = _index_html_at(base, staged=False, repo_root=repo_root)
        new_index = _index_html_at("HEAD", staged=False, repo_root=repo_root)

    return find_violations(
        old_index_html=old_index,
        new_index_html=new_index,
        changed_asset_paths=changed_assets,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--staged",
        action="store_true",
        help="pre-commit mode: check staged changes against HEAD",
    )
    group.add_argument(
        "--base",
        metavar="REF",
        help="CI mode: check changes in REF...HEAD (e.g. origin/main)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        violations = run_git_guard(staged=args.staged, base=args.base)
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(f"cache-bust guard: git error: {exc}\n")
        return 2
    except ValueError as exc:
        sys.stderr.write(f"cache-bust guard: {exc}\n")
        return 2

    if not violations:
        return 0

    sys.stderr.write(
        "cache-bust guard: stale ?v= cache-bust token(s) detected.\n"
        "An asset changed without bumping its ?v= token in static/index.html,\n"
        "so already-loaded browsers will keep serving the STALE asset.\n\n"
    )
    for violation in violations:
        sys.stderr.write(f"  - {violation.describe()}\n")
    sys.stderr.write(
        "\nFix: bump each asset's ?v= token in static/index.html "
        "(convention: ?v=YYYYMMDD<letter>).\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
