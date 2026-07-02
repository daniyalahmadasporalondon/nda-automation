"""Gen-verify TERM boundedness check (harden/fix-term-cap).

The generator once wrote "...for a fixed period of five (5) years ... or until
the completion of the Purpose, WHICHEVER IS LATER." "whichever is later" takes
the LONGER leg and, since a Purpose rarely formally completes, makes the ordinary
confidentiality obligation de facto perpetual -- defeating the Playbook cap while
the deterministic year-check still reads "5 years" and passes. The gen-verify
harness had NO term check, so nothing caught it.

These tests assert the new ``check_term_bounded`` gate: it must FAIL a clause with
the open-ended "whichever is later" language and PASS the corrected "whichever is
earlier" clause, and it must fail a clause that drops the numeric cap entirely.
"""
from __future__ import annotations

from tests.gen_verify_harness import VerificationReport, check_term_bounded


def _defects(report: VerificationReport) -> list:
    return [f for f in report.findings if f.severity == "DEFECT"]


_GOOD_CLAUSE = (
    "TERM OF THE AGREEMENT: This Agreement shall become effective on the date of "
    "signing and shall remain in force, and the confidentiality obligations shall "
    "survive, for a fixed period of five (5) years from the date of this Agreement "
    "or until the completion of the Purpose, whichever is earlier. Notwithstanding "
    "the foregoing, trade secrets survive for as long as the law requires."
)

_BAD_CLAUSE_LATER = _GOOD_CLAUSE.replace("whichever is earlier", "whichever is later")

_BAD_CLAUSE_NO_CAP = (
    "TERM OF THE AGREEMENT: The confidentiality obligations shall survive until "
    "the completion of the Purpose."
)


def test_check_term_bounded_passes_whichever_is_earlier():
    report = VerificationReport(label="term-good")
    check_term_bounded(_GOOD_CLAUSE, report)
    assert report.clear, [f.detail for f in report.findings]
    assert not _defects(report)


def test_check_term_bounded_fails_whichever_is_later():
    report = VerificationReport(label="term-open-ended")
    check_term_bounded(_BAD_CLAUSE_LATER, report)
    assert not report.clear
    defects = _defects(report)
    assert any(f.check == "term.open_ended" for f in defects), defects


def test_check_term_bounded_fails_when_cap_value_missing():
    report = VerificationReport(label="term-no-cap")
    check_term_bounded(_BAD_CLAUSE_NO_CAP, report)
    assert not report.clear
    assert any(f.check == "term.cap_missing" for f in _defects(report))


def test_check_term_bounded_fails_when_clause_missing():
    report = VerificationReport(label="term-missing")
    check_term_bounded("GOVERNING LAW: This Agreement is governed by English law.", report)
    assert not report.clear
    assert any(f.check == "term.clause_missing" for f in _defects(report))
