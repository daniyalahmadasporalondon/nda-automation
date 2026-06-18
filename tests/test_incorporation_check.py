"""Tests for the incorporation-by-reference / "shall prevail" detector.

The detector resolves DIRECTION: it flags only when an external/unseen agreement is
given overriding authority OVER THIS NDA. It stays silent for benign references,
recitals, and -- critically -- the reverse polarity (this NDA prevails / supersedes,
including entire-agreement merger clauses). It fails safe (None, no crash) on
garbage/empty input.
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
    """An external agreement given overriding authority over THIS NDA -> flag."""

    def test_subject_to_msa_which_shall_prevail(self) -> None:
        text = _NDA_HEAD + (
            "5. Precedence. This Agreement is subject to the Master Services "
            "Agreement between the parties, which shall prevail in the event of any "
            "conflict.\n"
        )
        finding = inc.detect_incorporation_override(_matter(text))
        self.assertIsNotNone(finding)
        self.assertEqual(finding["reason_code"], inc.REASON_CODE)
        self.assertTrue(finding["message"])

    def test_incorporated_by_reference_other_takes_precedence(self) -> None:
        text = _NDA_HEAD + (
            "5. The terms of the MSA are incorporated herein by reference. In the "
            "event of any inconsistency, the MSA takes precedence over this "
            "Agreement.\n"
        )
        finding = inc.detect_incorporation_override(_matter(text))
        self.assertIsNotNone(finding)
        self.assertEqual(finding["reason_code"], inc.REASON_CODE)

    def test_other_agreement_controls_in_conflict(self) -> None:
        text = _NDA_HEAD + (
            "5. In the event of any conflict between this Agreement and the Master "
            "Services Agreement, the Master Services Agreement controls.\n"
        )
        self.assertIsNotNone(inc.detect_incorporation_override(_matter(text)))

    def test_this_nda_subordinate_to_external(self) -> None:
        # tp04 shape: "this Confidentiality Agreement is and shall remain subordinate
        # to the Master Subscription Agreement".
        text = _NDA_HEAD + (
            "5. This Confidentiality Agreement is and shall remain subordinate to "
            "the Master Subscription Agreement between the parties.\n"
        )
        self.assertIsNotNone(inc.detect_incorporation_override(_matter(text)))

    def test_subject_to_msa_supersede_and_govern(self) -> None:
        text = _NDA_HEAD + (
            "5. This NDA is made subject to the MSA. To the extent any provision of "
            "this NDA conflicts with the MSA, the provisions of the MSA shall "
            "supersede and govern.\n"
        )
        self.assertIsNotNone(inc.detect_incorporation_override(_matter(text)))


class DirectionResolution(unittest.TestCase):
    """The hard constraint: when THIS NDA is the prevailing subject -> SILENT."""

    def test_this_nda_prevails_over_other_with_selfname(self) -> None:
        # fp04: 'prevail' + 'conflict' + 'Master Services Agreement' all present, but
        # the prevailing SUBJECT is "this Non-Disclosure Agreement" (self-name). SAFE.
        text = _NDA_HEAD + (
            "5. The terms of the Master Services Agreement are referenced herein. In "
            "the event of any conflict between this Non-Disclosure Agreement and any "
            "other agreement between the parties, including the Master Services "
            "Agreement, the terms of this Non-Disclosure Agreement shall prevail.\n"
        )
        self.assertIsNone(inc.detect_incorporation_override(_matter(text)))

    def test_entire_agreement_merger_supersedes_prior(self) -> None:
        # fp06: "This Agreement ... supersedes ... any Master Services Agreement" --
        # subject is THIS Agreement, large subject->verb gap. Merger clause = SAFE.
        text = _NDA_HEAD + (
            "5. This Agreement constitutes the entire agreement between the parties "
            "regarding confidentiality and supersedes all prior agreements, "
            "understandings, and the confidentiality provisions of any Master "
            "Services Agreement, whether written or oral.\n"
        )
        self.assertIsNone(inc.detect_incorporation_override(_matter(text)))

    def test_this_nda_prevails_short_form(self) -> None:
        text = _NDA_HEAD + (
            "5. This Agreement shall prevail over any conflicting terms in the Master "
            "Services Agreement.\n"
        )
        self.assertIsNone(inc.detect_incorporation_override(_matter(text)))


class CrossDistance(unittest.TestCase):
    """Genuine document-level association across paragraphs (the distance trap)."""

    def test_anaphoric_prevail_far_from_incorporation(self) -> None:
        # Incorporation-by-reference in para 1; the prevail clause is in a later
        # paragraph and names the external doc only anaphorically ("that agreement").
        # No external name in the prevail sentence -> requires true cross-distance.
        text = (
            "This Non-Disclosure Agreement is entered into as of the Effective "
            "Date. The parties have entered into a Master Services Agreement, the "
            "terms of which are incorporated into this Agreement by reference.\n\n"
            "The Receiving Party shall use Confidential Information solely for the "
            "Purpose. The obligations survive for five years.\n\n"
            "In the event of any conflict or inconsistency, that agreement shall "
            "prevail.\n"
        )
        finding = inc.detect_incorporation_override(_matter(text))
        self.assertIsNotNone(finding)
        # Prove it is NOT relying on the external name appearing in the prevail
        # sentence: the prevail sentence contains no agreement name, only the
        # anaphor "that agreement".
        prevail_sentence = text.split("inconsistency,")[1]
        self.assertNotIn("master services agreement", prevail_sentence.lower())

    def test_cross_distance_this_doc_prevails_stays_silent(self) -> None:
        # Same incorporation, but the distant prevail clause favours THIS NDA.
        text = (
            "The Master Services Agreement is incorporated into this Agreement by "
            "reference.\n\n"
            "The Receiving Party shall protect Confidential Information.\n\n"
            "In the event of any conflict, this Agreement shall prevail.\n"
        )
        self.assertIsNone(inc.detect_incorporation_override(_matter(text)))


class StaysSilent(unittest.TestCase):
    """Benign references, recitals, and precedence-without-target -> no flag."""

    def test_benign_defined_in_msa(self) -> None:
        text = _NDA_HEAD + (
            '5. "Confidential Information" has the meaning given to it in the Master '
            "Services Agreement between the parties.\n"
        )
        self.assertIsNone(inc.detect_incorporation_override(_matter(text)))

    def test_benign_as_described_in_sow(self) -> None:
        text = _NDA_HEAD + (
            "5. The services are as further described in the Statement of Work "
            "agreed between the parties.\n"
        )
        self.assertIsNone(inc.detect_incorporation_override(_matter(text)))

    def test_whereas_recital_mention(self) -> None:
        text = (
            "WHEREAS the parties previously entered into a Master Services "
            "Agreement; and WHEREAS the parties now wish to exchange confidential "
            "information. The Receiving Party shall not disclose Confidential "
            "Information. This Agreement constitutes the entire agreement between "
            "the parties.\n"
        )
        self.assertIsNone(inc.detect_incorporation_override(_matter(text)))

    def test_external_reference_without_precedence(self) -> None:
        text = _NDA_HEAD + (
            "5. This Agreement is entered into pursuant to the Master Services "
            "Agreement between the parties.\n"
        )
        self.assertIsNone(inc.detect_incorporation_override(_matter(text)))

    def test_precedence_without_external_target(self) -> None:
        # Exhibits prevail over the body -- no EXTERNAL agreement involved.
        text = _NDA_HEAD + (
            "5. The Exhibits attached hereto shall prevail in the event of any "
            "conflict with the body of this Agreement.\n"
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
            inc.detect_incorporation_override(
                _matter(" � PK ]]]>>> \x00\x01 prevail???control;;;subject@@@ \n")
            )
        )


if __name__ == "__main__":
    unittest.main()
