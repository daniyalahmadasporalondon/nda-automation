"""Tests for the incorporation-by-reference / "shall prevail" detector.

The detector flags ONLY when a document both references an external/unseen
agreement AND grants that external agreement overriding authority. It stays silent
for benign references, for the reverse polarity (this NDA prevails), and fails safe
(None, no crash) on garbage/empty input.
"""
from __future__ import annotations

import unittest

from nda_automation import incorporation_check as inc


def _matter(text: str) -> dict:
    return {"extracted_text": text}


_NDA_HEAD = (
    "MUTUAL NON-DISCLOSURE AGREEMENT\n\n"
    "This Mutual Non-Disclosure Agreement is entered into between Acme Corp and "
    "Aspora Technologies Limited.\n\n"
    "1. Confidential Information means non-public business information.\n\n"
)


class FlagsSubordination(unittest.TestCase):
    """An external agreement given overriding authority -> flag."""

    def test_subject_to_msa_which_shall_prevail(self) -> None:
        text = _NDA_HEAD + (
            "5. Precedence. This Agreement is subject to the Master Services "
            "Agreement between the parties, which shall prevail in the event of any "
            "conflict.\n"
        )
        finding = inc.detect_incorporation_override(_matter(text))
        self.assertIsNotNone(finding)
        self.assertEqual(finding["reason_code"], inc.REASON_CODE)
        self.assertIn("message", finding)
        self.assertTrue(finding["message"])

    def test_incorporated_by_reference_takes_precedence(self) -> None:
        text = _NDA_HEAD + (
            "5. The terms of the MSA are incorporated herein by reference and take "
            "precedence over the provisions of this Agreement.\n"
        )
        finding = inc.detect_incorporation_override(_matter(text))
        self.assertIsNotNone(finding)
        self.assertEqual(finding["reason_code"], inc.REASON_CODE)

    def test_msa_controls_in_event_of_conflict(self) -> None:
        text = _NDA_HEAD + (
            "5. In the event of any conflict between this Agreement and the Master "
            "Services Agreement, the Master Services Agreement controls.\n"
        )
        finding = inc.detect_incorporation_override(_matter(text))
        self.assertIsNotNone(finding)

    def test_supersedes_this_agreement(self) -> None:
        text = _NDA_HEAD + (
            "5. The Statement of Work supersedes this Agreement to the extent of any "
            "inconsistency.\n"
        )
        finding = inc.detect_incorporation_override(_matter(text))
        self.assertIsNotNone(finding)


class StaysSilent(unittest.TestCase):
    """Benign references and reverse polarity -> no flag."""

    def test_benign_defined_in_msa(self) -> None:
        text = _NDA_HEAD + (
            '5. "Confidential Information" has the meaning given to it in the Master '
            "Services Agreement between the parties.\n"
        )
        self.assertIsNone(inc.detect_incorporation_override(_matter(text)))

    def test_benign_as_described_in_sow(self) -> None:
        text = _NDA_HEAD + (
            "5. The services are as described in the SOW agreed between the "
            "parties.\n"
        )
        self.assertIsNone(inc.detect_incorporation_override(_matter(text)))

    def test_reverse_polarity_this_nda_prevails(self) -> None:
        text = _NDA_HEAD + (
            "5. This Agreement shall prevail over any conflicting terms in any other "
            "agreement, including the Master Services Agreement.\n"
        )
        self.assertIsNone(inc.detect_incorporation_override(_matter(text)))

    def test_reverse_polarity_this_nda_supersedes_prior(self) -> None:
        text = _NDA_HEAD + (
            "5. This Agreement supersedes all prior agreements and understandings "
            "between the parties relating to its subject matter.\n"
        )
        self.assertIsNone(inc.detect_incorporation_override(_matter(text)))

    def test_precedence_without_external_reference(self) -> None:
        # A precedence clause that names no external agreement -> nothing to
        # subordinate to -> silent.
        text = _NDA_HEAD + (
            "5. The Exhibits attached hereto shall prevail in the event of any "
            "conflict with the body of this Agreement.\n"
        )
        self.assertIsNone(inc.detect_incorporation_override(_matter(text)))

    def test_external_reference_without_precedence(self) -> None:
        text = _NDA_HEAD + (
            "5. This Agreement is entered into pursuant to the Master Services "
            "Agreement between the parties.\n"
        )
        self.assertIsNone(inc.detect_incorporation_override(_matter(text)))


class FailSafe(unittest.TestCase):
    """Garbage / empty / malformed input -> None, never a crash."""

    def test_empty_text(self) -> None:
        self.assertIsNone(inc.detect_incorporation_override(_matter("")))

    def test_missing_extracted_text(self) -> None:
        self.assertIsNone(inc.detect_incorporation_override({}))

    def test_none_matter(self) -> None:
        self.assertIsNone(inc.detect_incorporation_override(None))  # type: ignore[arg-type]

    def test_non_mapping_matter(self) -> None:
        self.assertIsNone(inc.detect_incorporation_override("not a matter"))  # type: ignore[arg-type]

    def test_garbage_text(self) -> None:
        self.assertIsNone(
            inc.detect_incorporation_override(_matter("$$$ \x00 prevail subject ;;; \n"))
        )


if __name__ == "__main__":
    unittest.main()
