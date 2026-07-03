"""Versioned-static-asset scanning: the source of truth for ?v= cache-bust tokens.

The app pins every static CSS/JS asset to a manual cache-bust query token
(``/static/app.js?v=20260610g`` in ``static/index.html``, and relative module
imports like ``./humanize.mjs?v=20260621humanize2`` INSIDE ``.js``/``.mjs``
files). Two consumers need one shared, exact notion of "the current token for
this file":

* **The server's immutable-caching gate** (``server._static_cache_control``): a
  ``/static/<file>?v=<token>`` request is served with
  ``Cache-Control: public, max-age=31536000, immutable`` ONLY when ``<token>``
  matches the token this process's own tree references the file with. A
  token-MATCH means the bytes being served are exactly the bytes that token was
  minted for, so caching them forever is safe. A MISSING or MISMATCHED token
  (e.g. a zero-downtime deploy race: the browser got the NEW index.html but this
  request landed on an instance still running the OLD tree) falls back to the
  no-cache+ETag revalidation default -- self-healing, never a permanent wedge.
  ``.html`` files are exempt unconditionally (index.html is the version
  manifest; it must always revalidate so new tokens propagate).

* **The CI staleness guard** (``tests/test_static_asset_manifest.py`` +
  ``static/asset-tokens.json``): with immutable caching live, a forgotten token
  bump is no longer "stale until the next revalidation" -- it is stale for a
  YEAR. The committed manifest records ``{path: {v, sha256}}`` for every
  ?v=-referenced asset; the test recomputes the tree's hashes and fails when an
  asset's bytes changed without its token (and manifest entry) changing.
  Regenerate with ``python -m nda_automation.static_versioning --write`` --
  which itself REFUSES to record changed bytes under an unchanged token.

The query token is parsed from the request's query string only; it never feeds
path resolution (the server resolves the filesystem path from the URL path
alone, before this module is consulted).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import re
import sys
from pathlib import Path
from typing import Dict, Set

# The committed manifest the CI guard checks the tree against.
MANIFEST_NAME = "asset-tokens.json"

# Source files scanned for ?v= references: the HTML shell plus every JS module
# (dynamic imports and module-relative imports carry their own tokens).
_SOURCE_SUFFIXES = {".html", ".js", ".mjs"}

# An absolute reference: any quoted "/static/<path>?v=<token>" string. Covers
# index.html's href/src attributes AND absolute URL strings inside JS.
_ABS_REF_RE = re.compile(r"""["'](?:https?://[^"'/]+)?/static/([^"'?]+)\?v=([^"'?#&]+)["']""")

# A module-relative reference inside a JS/MJS source: "./x.mjs?v=t" or
# "../x.mjs?v=t", resolved against the source file's directory. The extension
# allowlist keeps this from matching arbitrary app strings.
_REL_REF_RE = re.compile(r"""["'](\.{1,2}/[^"'?]+?\.(?:mjs|js|css))\?v=([^"'?#&]+)["']""")


def _iter_source_files(static_dir: Path):
    for path in sorted(static_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() in _SOURCE_SUFFIXES:
            yield path


def scan_versioned_references(static_dir: Path) -> Dict[str, Set[str]]:
    """Map static-dir-relative asset path -> the set of ?v= tokens referencing it.

    Scans every ``.html`` / ``.js`` / ``.mjs`` source under ``static_dir`` for
    absolute (``/static/...?v=``) and module-relative (``./...?v=``) references.
    A healthy tree maps every asset to exactly ONE token; more than one means a
    referrer was left stale when the token was bumped elsewhere.
    """
    references: Dict[str, Set[str]] = {}
    for source in _iter_source_files(static_dir):
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        source_rel_dir = source.parent.relative_to(static_dir).as_posix()
        for asset_rel, token in _ABS_REF_RE.findall(text):
            normalized = posixpath.normpath(asset_rel)
            if normalized.startswith(".."):
                continue
            references.setdefault(normalized, set()).add(token)
        for relative_ref, token in _REL_REF_RE.findall(text):
            joined = posixpath.normpath(posixpath.join(source_rel_dir, relative_ref))
            if joined.startswith(".."):
                continue
            references.setdefault(joined, set()).add(token)
    return references


def versioned_asset_tokens(static_dir: Path) -> Dict[str, str]:
    """The runtime token map: relative path -> its single agreed ?v= token.

    Fail-safe filters for the server's immutable gate:
    * ``.html`` files are excluded (they must always revalidate);
    * a path referenced with CONFLICTING tokens is excluded (one referrer is
      stale; immutable-caching either variant could wedge a client);
    * a reference to a file that does not exist is excluded.
    """
    tokens: Dict[str, str] = {}
    for asset_rel, seen in scan_versioned_references(static_dir).items():
        if asset_rel.lower().endswith(".html"):
            continue
        if len(seen) != 1:
            continue
        if not (static_dir / asset_rel).is_file():
            continue
        tokens[asset_rel] = next(iter(seen))
    return tokens


def compute_asset_manifest(static_dir: Path) -> Dict[str, Dict[str, str]]:
    """The CI-guard view of the tree: {path: {"v": token, "sha256": hash}}.

    Unlike the fail-safe runtime map, this is STRICT: conflicting tokens or a
    reference to a missing file raise, so the guard fails loudly instead of
    silently narrowing coverage. ``.html`` files are excluded -- they are served
    with revalidation, so byte changes propagate without a token bump.
    """
    manifest: Dict[str, Dict[str, str]] = {}
    references = scan_versioned_references(static_dir)
    for asset_rel in sorted(references):
        seen = references[asset_rel]
        if asset_rel.lower().endswith(".html"):
            continue
        if len(seen) != 1:
            raise ValueError(
                f"{asset_rel} is referenced with conflicting ?v= tokens {sorted(seen)}; "
                "every referrer must carry the same (current) token."
            )
        asset_file = static_dir / asset_rel
        if not asset_file.is_file():
            raise ValueError(f"{asset_rel} is referenced with a ?v= token but does not exist.")
        manifest[asset_rel] = {
            "v": next(iter(seen)),
            "sha256": hashlib.sha256(asset_file.read_bytes()).hexdigest(),
        }
    return manifest


def load_manifest(manifest_path: Path) -> Dict[str, Dict[str, str]]:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{manifest_path} must contain a JSON object.")
    return payload


def write_manifest(static_dir: Path, manifest_path: Path) -> Dict[str, Dict[str, str]]:
    """Regenerate the committed manifest -- REFUSING to launder a missing bump.

    If an asset's bytes changed but its token is still the one recorded in the
    existing manifest, writing would bless a stale-forever state (immutable
    caching means already-loaded clients never refetch that token). The writer
    raises instead, listing the assets whose tokens must be bumped first.
    """
    manifest = compute_asset_manifest(static_dir)
    if manifest_path.is_file():
        previous = load_manifest(manifest_path)
        unbumped = sorted(
            asset
            for asset, entry in manifest.items()
            if asset in previous
            and previous[asset].get("v") == entry["v"]
            and previous[asset].get("sha256") != entry["sha256"]
        )
        if unbumped:
            raise ValueError(
                "Refusing to write manifest: bytes changed without a ?v= token bump for: "
                + ", ".join(unbumped)
                + ". Bump each token where the asset is referenced, then re-run --write."
            )
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def _default_static_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "static"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--static-dir", type=Path, default=None)
    parser.add_argument("--write", action="store_true", help="Regenerate the committed manifest.")
    parser.add_argument("--check", action="store_true", help="Verify the tree matches the manifest.")
    args = parser.parse_args(argv)

    static_dir = args.static_dir or _default_static_dir()
    manifest_path = static_dir / MANIFEST_NAME

    if args.write:
        try:
            manifest = write_manifest(static_dir, manifest_path)
        except ValueError as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 1
        print(f"Wrote {manifest_path} ({len(manifest)} assets).")
        return 0

    # --check (default): recompute and compare.
    try:
        current = compute_asset_manifest(static_dir)
        committed = load_manifest(manifest_path)
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    if current == committed:
        print(f"OK: {len(current)} versioned assets match {manifest_path.name}.")
        return 0
    for asset in sorted(set(current) | set(committed)):
        if current.get(asset) != committed.get(asset):
            print(f"STALE: {asset}: tree={current.get(asset)} manifest={committed.get(asset)}", file=sys.stderr)
    print(
        "Bytes/tokens drifted from the committed manifest. Bump the ?v= token for each "
        "changed asset, then run: python -m nda_automation.static_versioning --write",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
