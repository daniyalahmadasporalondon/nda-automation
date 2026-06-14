"""CI gate: the live canonical playbook must pass the consistency lint.

Running ``lint_playbook(load_playbook())`` and asserting ZERO violations gates the
shipped playbook: any future edit that makes it internally self-contradictory fails
CI here. Do NOT weaken this assertion to hide real findings -- if the live playbook
has violations, that is the lint doing its job and the integrator reconciles it.

The lint module is built by a parallel teammate against the agreed API:
    from nda_automation.playbook_lint import lint_playbook
    lint_playbook(playbook: Mapping) -> list[LintViolation]
If it is not yet importable, this test SKIPS (rather than failing on a missing
import) so the gate is meaningful exactly when the module exists. The integrator
runs the full integrated suite with the real module present.
"""
from __future__ import annotations

import unittest

from nda_automation.checker import load_playbook

try:
    from nda_automation.playbook_lint import lint_playbook
except ImportError:  # pragma: no cover - module built in parallel
    lint_playbook = None


@unittest.skipIf(lint_playbook is None, "nda_automation.playbook_lint not yet available")
class CanonicalPlaybookLintGateTests(unittest.TestCase):
    def test_live_playbook_has_no_lint_violations(self) -> None:
        playbook = load_playbook()
        violations = lint_playbook(playbook)

        details = "\n".join(
            f"  - [{getattr(v, 'check_id', '?')}] "
            f"{getattr(v, 'clause_id', '?')}: {getattr(v, 'message', v)}"
            for v in violations
        )
        self.assertEqual(
            list(violations),
            [],
            f"Canonical playbook has {len(violations)} consistency-lint violation(s):\n{details}",
        )


if __name__ == "__main__":
    unittest.main()
