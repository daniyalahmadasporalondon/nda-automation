"""Recurrence guard tests for the static cache-bust-token bug.

The guard (``scripts/cache_bust_guard.py``) fails a commit/CI run when a static
asset referenced from ``static/index.html`` changes without its ``?v=`` token
being bumped -- the exact bug class behind the 12 hand-fixed stale tokens.

These tests pin three things:

* the pure core (:func:`find_violations`) catches a stale-token change and is
  quiet when the token *is* bumped (the proof-of-catch);
* the real shipped ``static/index.html`` parses into sensible tokens; and
* the guard is actually wired -- the pre-commit hook calls it and CI runs the
  pytest gate that includes this file.
"""
import importlib.util
import stat
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUARD_PATH = ROOT / "scripts" / "cache_bust_guard.py"


def _load_guard():
    spec = importlib.util.spec_from_file_location("cache_bust_guard", GUARD_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    # Register before exec so dataclass type resolution (which looks the module
    # up in sys.modules) works under Python 3.12+ when loading a script by path.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


class FindViolationsTests(unittest.TestCase):
    """The pure core: changed-asset -> token-not-bumped detection."""

    OLD_INDEX = (
        '<link href="/static/styles.css?v=20260612a" rel="stylesheet">\n'
        '<script src="/static/app.js?v=20260610g"></script>\n'
        '<script src="/static/js/config.js?v=20260607e"></script>\n'
    )

    def test_stale_token_is_caught(self):
        # app.js bytes changed (it is in the changed set) but the index.html
        # token is identical -> this is the bug we are guarding against.
        violations = guard.find_violations(
            old_index_html=self.OLD_INDEX,
            new_index_html=self.OLD_INDEX,  # token unchanged
            changed_asset_paths={"static/app.js"},
        )
        self.assertEqual(len(violations), 1)
        v = violations[0]
        self.assertEqual(v.asset_path, "static/app.js")
        self.assertEqual(v.old_token, "20260610g")
        self.assertEqual(v.new_token, "20260610g")
        self.assertIn("?v=", v.describe())
        self.assertIn("static/app.js", v.describe())

    def test_bumped_token_is_clean(self):
        new_index = self.OLD_INDEX.replace("app.js?v=20260610g", "app.js?v=20260610h")
        violations = guard.find_violations(
            old_index_html=self.OLD_INDEX,
            new_index_html=new_index,
            changed_asset_paths={"static/app.js"},
        )
        self.assertEqual(violations, [])

    def test_unchanged_asset_is_ignored(self):
        # No assets in the changed set -> nothing to check, even with identical
        # index.html.
        violations = guard.find_violations(
            old_index_html=self.OLD_INDEX,
            new_index_html=self.OLD_INDEX,
            changed_asset_paths=set(),
        )
        self.assertEqual(violations, [])

    def test_multiple_changed_only_unbumped_flagged(self):
        # styles.css token bumped, config.js token NOT bumped; both changed.
        new_index = self.OLD_INDEX.replace(
            "styles.css?v=20260612a", "styles.css?v=20260612b"
        )
        violations = guard.find_violations(
            old_index_html=self.OLD_INDEX,
            new_index_html=new_index,
            changed_asset_paths={"static/styles.css", "static/js/config.js"},
        )
        flagged = {v.asset_path for v in violations}
        self.assertEqual(flagged, {"static/js/config.js"})

    def test_dropped_reference_for_changed_asset_is_flagged(self):
        # The asset used to be referenced with a token, the bytes changed, but
        # the reference/token disappeared -> it can no longer be cache-busted.
        new_index = self.OLD_INDEX.replace(
            '<script src="/static/app.js?v=20260610g"></script>\n', ""
        )
        violations = guard.find_violations(
            old_index_html=self.OLD_INDEX,
            new_index_html=new_index,
            changed_asset_paths={"static/app.js"},
        )
        self.assertEqual([v.asset_path for v in violations], ["static/app.js"])
        self.assertIsNone(violations[0].new_token)


class ParseTokensTests(unittest.TestCase):
    def test_parses_path_and_token(self):
        tokens = guard.parse_tokens(
            '<script src="/static/js/modules/global-bridge.mjs?v=20260608b"></script>'
        )
        self.assertEqual(
            tokens, {"static/js/modules/global-bridge.mjs": "20260608b"}
        )

    def test_real_index_html_has_tokens(self):
        index_html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        tokens = guard.parse_tokens(index_html)
        # Sanity: the shipped shell references many tokenised assets and the
        # well-known entrypoints are present.
        self.assertGreater(len(tokens), 20)
        self.assertIn("static/app.js", tokens)
        self.assertIn("static/styles.css", tokens)
        # Every parsed token is non-empty.
        self.assertTrue(all(tok for tok in tokens.values()))

    def test_no_duplicate_script_or_stylesheet_tags(self):
        # A merge/token-unify once shipped index.html referencing
        # review-workstation-rendering.js TWICE, which re-ran its top-level
        # `let` in the shared global scope -> a redeclaration SyntaxError that
        # aborted the module and broke document rendering on prod. Guard against
        # any asset being referenced by more than one <script>/<link> tag.
        import collections
        import re

        index_html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        srcs = [
            u.split("?")[0]
            for u in re.findall(r'<script\b[^>]*\bsrc="([^"]+)"', index_html)
        ]
        hrefs = [
            u.split("?")[0]
            for u in re.findall(r'<link\b[^>]*\bhref="([^"]+\.css[^"]*)"', index_html)
        ]
        dupes = {
            base: count
            for base, count in collections.Counter(srcs + hrefs).items()
            if count > 1
        }
        self.assertEqual(
            dupes, {}, f"duplicate <script>/<link> tags in index.html: {dupes}"
        )


class WiringTests(unittest.TestCase):
    def test_guard_script_exists_and_is_executable(self):
        self.assertTrue(GUARD_PATH.is_file())
        mode = GUARD_PATH.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR, "guard script should be executable")

    def test_pre_commit_hook_invokes_guard(self):
        hook = ROOT / "scripts" / "hooks" / "pre-commit"
        self.assertTrue(hook.is_file())
        self.assertTrue(hook.stat().st_mode & stat.S_IXUSR)
        body = hook.read_text(encoding="utf-8")
        self.assertIn("cache_bust_guard.py", body)
        self.assertIn("--staged", body)

    def test_ci_runs_pytest_gate(self):
        # The guard is wired into CI via this very test file being collected by
        # the existing `python -m pytest -q` step.
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("python -m pytest -q", workflow)

    def test_ci_runs_dedicated_guard_step(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("scripts/cache_bust_guard.py", workflow)


class CliEndToEndTests(unittest.TestCase):
    """End-to-end proof in a throwaway git repo: a real stale-token commit
    makes the guard's --staged mode exit non-zero, and bumping fixes it."""

    def _run_guard(self, repo: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(GUARD_PATH), *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
        )

    def _git(self, repo: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)

    def test_staged_mode_catches_stale_and_passes_when_bumped(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._git(repo, "init", "-q")
            self._git(repo, "config", "user.email", "t@example.com")
            self._git(repo, "config", "user.name", "t")
            (repo / "static").mkdir()
            (repo / "scripts").mkdir()
            # Mirror the guard into the throwaway repo so its REPO_ROOT resolves
            # to this repo (the guard derives root from its own location).
            (repo / "scripts" / "cache_bust_guard.py").write_text(
                GUARD_PATH.read_text(encoding="utf-8"), encoding="utf-8"
            )
            asset = repo / "static" / "app.js"
            index = repo / "static" / "index.html"
            asset.write_text("console.log('v1');\n", encoding="utf-8")
            index.write_text(
                '<script src="/static/app.js?v=20260101a"></script>\n',
                encoding="utf-8",
            )
            self._git(repo, "add", "-A")
            self._git(repo, "commit", "-q", "-m", "initial")

            # Change the asset bytes but NOT the token, then stage.
            asset.write_text("console.log('v2 changed');\n", encoding="utf-8")
            self._git(repo, "add", "static/app.js")
            stale = self._run_guard(repo, "--staged")
            self.assertEqual(
                stale.returncode, 1, msg=f"stderr={stale.stderr}\nstdout={stale.stdout}"
            )
            self.assertIn("static/app.js", stale.stderr)
            self.assertIn("?v=", stale.stderr)

            # Now bump the token and stage index.html too -> clean.
            index.write_text(
                '<script src="/static/app.js?v=20260101b"></script>\n',
                encoding="utf-8",
            )
            self._git(repo, "add", "static/index.html")
            fixed = self._run_guard(repo, "--staged")
            self.assertEqual(
                fixed.returncode, 0, msg=f"stderr={fixed.stderr}\nstdout={fixed.stdout}"
            )


class WholeTreeGuardTests(unittest.TestCase):
    """The absolute (``--tree``) guard catches staleness split across commits.

    The range-diff guard (``--staged`` / ``--base``) only inspects assets that
    changed *within one diff range*. The real prod failure was: a token bumped
    in commit A, then the asset's bytes changed in a *later* commit B that never
    touched index.html. The whole-tree guard compares each asset's current bytes
    against its bytes as of the commit that last bumped its token, so it catches
    that case regardless of how the change was split across commits.
    """

    def _run_guard(self, repo: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(GUARD_PATH), *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
        )

    def _git(self, repo: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)

    def test_tree_mode_catches_stale_split_across_commits(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._git(repo, "init", "-q")
            self._git(repo, "config", "user.email", "t@example.com")
            self._git(repo, "config", "user.name", "t")
            (repo / "static").mkdir()
            (repo / "scripts").mkdir()
            (repo / "scripts" / "cache_bust_guard.py").write_text(
                GUARD_PATH.read_text(encoding="utf-8"), encoding="utf-8"
            )
            asset = repo / "static" / "app.js"
            index = repo / "static" / "index.html"

            # Commit A: introduce asset + token, then bump the token (so a real
            # "token last changed" commit exists).
            asset.write_text("console.log('v1');\n", encoding="utf-8")
            index.write_text(
                '<script src="/static/app.js?v=20260101a"></script>\n',
                encoding="utf-8",
            )
            self._git(repo, "add", "-A")
            self._git(repo, "commit", "-q", "-m", "introduce")
            index.write_text(
                '<script src="/static/app.js?v=20260101b"></script>\n',
                encoding="utf-8",
            )
            self._git(repo, "add", "-A")
            self._git(repo, "commit", "-q", "-m", "bump token (A)")

            # Range-diff guard against A would be clean here. Now commit B
            # changes ONLY the asset bytes -- no index.html touch, no token bump.
            asset.write_text("console.log('v2 changed');\n", encoding="utf-8")
            self._git(repo, "add", "-A")
            self._git(repo, "commit", "-q", "-m", "change asset only (B)")

            stale = self._run_guard(repo, "--tree")
            self.assertEqual(
                stale.returncode,
                1,
                msg=f"--tree should flag split staleness.\nstderr={stale.stderr}",
            )
            self.assertIn("static/app.js", stale.stderr)

            # Commit C: bump the token to match the new bytes -> clean.
            index.write_text(
                '<script src="/static/app.js?v=20260101c"></script>\n',
                encoding="utf-8",
            )
            self._git(repo, "add", "-A")
            self._git(repo, "commit", "-q", "-m", "bump token (C)")
            fixed = self._run_guard(repo, "--tree")
            self.assertEqual(
                fixed.returncode,
                0,
                msg=f"--tree should be clean after bump.\nstderr={fixed.stderr}",
            )

    def test_shipped_tree_is_not_stale(self):
        """The real shipped HEAD must have no stale versioned assets.

        This is the live guard: if a future commit changes a versioned asset
        without bumping its ?v= token in static/index.html, this fails.
        """
        result = self._run_guard(ROOT, "--tree")
        self.assertEqual(
            result.returncode,
            0,
            msg=(
                "Shipped static/index.html has stale cache-bust tokens "
                f"(bump them):\n{result.stderr}"
            ),
        )

    def test_ci_runs_tree_guard_step(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("--tree", workflow)


if __name__ == "__main__":
    unittest.main()
