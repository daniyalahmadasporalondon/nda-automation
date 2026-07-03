"""CI staleness guard for immutable-cached versioned statics.

With ``Cache-Control: public, max-age=31536000, immutable`` live for
token-matched ``?v=`` requests (see ``server._static_cache_control``), a
forgotten token bump is no longer "stale until the next revalidation" -- an
already-loaded browser keeps the OLD bytes for a YEAR because it never
re-requests a URL it cached immutable. This guard makes that bug class fail in
CI:

* ``static/asset-tokens.json`` is the committed manifest: for every asset
  referenced with a ``?v=`` token anywhere in the static tree (index.html AND
  module imports inside .js/.mjs files), it records the token and the asset's
  content sha256.
* ``test_tree_matches_committed_manifest`` recomputes both from the working
  tree. Change an asset's bytes without bumping its token(s) and regenerating
  the manifest, and this test fails with the offending paths.
* Regeneration itself is guarded: ``write_manifest`` REFUSES to record changed
  bytes under an unchanged token, so the manifest cannot be used to launder a
  missing bump.

Complements scripts/cache_bust_guard.py (git-diff based, index.html-only): this
guard is absolute (whole tree vs committed state) and covers tokens inside
.mjs/.js sources, which the git guard does not parse.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nda_automation import static_versioning

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = REPO_ROOT / "static"
MANIFEST_PATH = STATIC_DIR / static_versioning.MANIFEST_NAME

_REGEN_HINT = (
    "Bump the ?v= token everywhere the asset is referenced, then run: "
    "python -m nda_automation.static_versioning --write"
)


# --------------------------------------------------------------------------- #
# The absolute tree guard
# --------------------------------------------------------------------------- #
def test_tree_matches_committed_manifest():
    assert MANIFEST_PATH.is_file(), (
        f"{MANIFEST_PATH} is missing. Generate it with "
        "python -m nda_automation.static_versioning --write"
    )
    current = static_versioning.compute_asset_manifest(STATIC_DIR)
    committed = static_versioning.load_manifest(MANIFEST_PATH)

    stale = sorted(
        asset
        for asset in set(current) | set(committed)
        if current.get(asset) != committed.get(asset)
    )
    assert current == committed, (
        f"Versioned static assets drifted from {MANIFEST_PATH.name}: {stale}. "
        f"{_REGEN_HINT}"
    )


def test_manifest_covers_mjs_internal_tokens():
    # The whole POINT over the index.html-only git guard: tokens carried by
    # module imports inside .mjs files must be covered by the manifest.
    committed = static_versioning.load_manifest(MANIFEST_PATH)
    assert "js/modules/humanize.mjs" in committed
    assert committed["js/modules/humanize.mjs"]["v"]
    assert len(committed["js/modules/humanize.mjs"]["sha256"]) == 64


def test_manifest_excludes_html_files():
    # .html is served with revalidation (never immutable), so its bytes
    # propagate without a token bump -- requiring one would be a false gate.
    committed = static_versioning.load_manifest(MANIFEST_PATH)
    assert not any(asset.lower().endswith(".html") for asset in committed)


# --------------------------------------------------------------------------- #
# Unit coverage of the scanning + writer-refusal mechanics (isolated tmp tree)
# --------------------------------------------------------------------------- #
def _make_static_tree(root: Path) -> Path:
    static = root / "static"
    (static / "js" / "modules").mkdir(parents=True)
    (static / "index.html").write_text(
        '<link rel="stylesheet" href="/static/styles.css?v=tok1">\n'
        '<script src="/static/app.js?v=tok2"></script>\n'
        '<a href="/static/guide.html?v=tokhtml">guide</a>\n',
        encoding="utf-8",
    )
    (static / "styles.css").write_text("body{}", encoding="utf-8")
    (static / "app.js").write_text(
        'import("./js/modules/mod.mjs?v=tok3");\n', encoding="utf-8"
    )
    (static / "guide.html").write_text("<p>guide</p>", encoding="utf-8")
    (static / "js" / "modules" / "mod.mjs").write_text(
        'import { x } from "../helpers.mjs?v=tok4";\nexport const y = 1;\n',
        encoding="utf-8",
    )
    (static / "js" / "helpers.mjs").write_text("export const x = 1;\n", encoding="utf-8")
    return static


def test_scan_resolves_absolute_and_relative_references(tmp_path):
    static = _make_static_tree(tmp_path)
    tokens = static_versioning.versioned_asset_tokens(static)

    assert tokens == {
        "styles.css": "tok1",
        "app.js": "tok2",
        "js/modules/mod.mjs": "tok3",  # relative import from app.js (static root)
        "js/helpers.mjs": "tok4",  # ../ import resolved against js/modules/
    }
    # guide.html is referenced with a token but excluded (html always revalidates).
    assert "guide.html" not in tokens


def test_conflicting_tokens_are_excluded_at_runtime_and_fail_the_manifest(tmp_path):
    static = _make_static_tree(tmp_path)
    # A second referrer pins styles.css to a DIFFERENT token: one of them is stale.
    (static / "js" / "stale.js").write_text(
        'const href = "/static/styles.css?v=tokOLD";\n', encoding="utf-8"
    )

    # Runtime map: fail-safe -- the conflicted asset simply gets no immutable.
    tokens = static_versioning.versioned_asset_tokens(static)
    assert "styles.css" not in tokens
    assert tokens["app.js"] == "tok2"  # unaffected assets keep their token

    # CI manifest: strict -- the conflict is a loud failure.
    with pytest.raises(ValueError, match="conflicting"):
        static_versioning.compute_asset_manifest(static)


def test_reference_to_missing_file_fails_the_manifest(tmp_path):
    static = _make_static_tree(tmp_path)
    (static / "js" / "broken.js").write_text(
        'import("./does-not-exist.mjs?v=tok9");\n', encoding="utf-8"
    )

    tokens = static_versioning.versioned_asset_tokens(static)
    assert "js/does-not-exist.mjs" not in tokens  # runtime: fail-safe skip

    with pytest.raises(ValueError, match="does not exist"):
        static_versioning.compute_asset_manifest(static)


def test_write_manifest_refuses_changed_bytes_under_unchanged_token(tmp_path):
    static = _make_static_tree(tmp_path)
    manifest_path = static / static_versioning.MANIFEST_NAME
    static_versioning.write_manifest(static, manifest_path)

    # Change the asset's bytes WITHOUT bumping its ?v= token anywhere.
    (static / "styles.css").write_text("body{color:red}", encoding="utf-8")

    with pytest.raises(ValueError, match="styles.css"):
        static_versioning.write_manifest(static, manifest_path)

    # Bump the token in the referrer and the writer accepts the regeneration.
    index = static / "index.html"
    index.write_text(index.read_text(encoding="utf-8").replace("tok1", "tok1b"), encoding="utf-8")
    manifest = static_versioning.write_manifest(static, manifest_path)
    assert manifest["styles.css"]["v"] == "tok1b"
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == manifest


def test_changed_bytes_without_bump_fail_the_check(tmp_path):
    # End-to-end of the CI failure mode: bytes drift after the manifest was
    # committed; the recomputed manifest no longer equals the committed one.
    static = _make_static_tree(tmp_path)
    manifest_path = static / static_versioning.MANIFEST_NAME
    static_versioning.write_manifest(static, manifest_path)

    (static / "js" / "helpers.mjs").write_text("export const x = 2;\n", encoding="utf-8")

    current = static_versioning.compute_asset_manifest(static)
    committed = static_versioning.load_manifest(manifest_path)
    assert current != committed
    assert current["js/helpers.mjs"]["v"] == committed["js/helpers.mjs"]["v"]  # token unchanged
    assert current["js/helpers.mjs"]["sha256"] != committed["js/helpers.mjs"]["sha256"]
