"""Read-side projection tests for the human-confirmation counterparty fields.

``public_matter`` exposes, alongside the display ``counterparty`` name, the
provenance the human-confirmation UI needs:
``counterparty_confidence`` / ``counterparty_verified`` / ``counterparty_source``
and a single derived ``counterparty_needs_confirmation`` flag.

``needs_confirmation`` is True when the stored counterparty dict is missing,
``verified`` is falsey, OR ``confidence`` < 0.75. The reads are defensive and
fail open: an absent or malformed extraction degrades to needs_confirmation
rather than crashing.
"""

from __future__ import annotations

from nda_automation.matter_view import public_matter


def _matter(counterparty: object) -> dict:
    return {"id": "m1", "subject": "RE: NDA", "intake_metadata": {"counterparty": counterparty}}


def test_verified_high_confidence_does_not_need_confirmation():
    public = public_matter(
        _matter({"name": "Acme Ltd", "confidence": 0.91, "verified": True, "source": "ai"})
    )
    assert public["counterparty_needs_confirmation"] is False
    assert public["counterparty_confidence"] == 0.91
    assert public["counterparty_verified"] is True
    assert public["counterparty_source"] == "ai"


def test_verified_but_low_confidence_needs_confirmation():
    public = public_matter(
        _matter({"name": "Acme Ltd", "confidence": 0.5, "verified": True, "source": "ai"})
    )
    assert public["counterparty_needs_confirmation"] is True
    assert public["counterparty_confidence"] == 0.5
    assert public["counterparty_verified"] is True


def test_confidence_at_threshold_does_not_need_confirmation():
    # 0.75 is the inclusive lower bound for trusting a verified extraction.
    public = public_matter(
        _matter({"name": "Acme Ltd", "confidence": 0.75, "verified": True, "source": "ai"})
    )
    assert public["counterparty_needs_confirmation"] is False


def test_unverified_needs_confirmation_even_with_high_confidence():
    public = public_matter(
        _matter({"name": "Acme Ltd", "confidence": 0.99, "verified": False, "source": "ai"})
    )
    assert public["counterparty_needs_confirmation"] is True
    assert public["counterparty_verified"] is False


def test_human_override_shape_does_not_need_confirmation():
    public = public_matter(
        _matter({"name": "Given Co", "confidence": 1.0, "verified": True, "source": "human"})
    )
    assert public["counterparty_needs_confirmation"] is False
    assert public["counterparty_source"] == "human"
    assert public["counterparty_confidence"] == 1.0


def test_missing_counterparty_dict_needs_confirmation():
    public = public_matter({"id": "m1", "subject": "RE: NDA"})
    assert public["counterparty_needs_confirmation"] is True
    assert public["counterparty_confidence"] == 0.0
    assert public["counterparty_verified"] is False
    assert public["counterparty_source"] == ""


def test_malformed_intake_metadata_fails_open_to_needs_confirmation():
    for intake_metadata in ("oops", None, 42, []):
        public = public_matter({"id": "m1", "subject": "RE: NDA", "intake_metadata": intake_metadata})
        assert public["counterparty_needs_confirmation"] is True
        assert public["counterparty_confidence"] == 0.0


def test_non_dict_counterparty_value_fails_open():
    public = public_matter(_matter("not-a-dict"))
    assert public["counterparty_needs_confirmation"] is True
    assert public["counterparty_confidence"] == 0.0


def test_unparseable_confidence_degrades_to_zero_and_needs_confirmation():
    public = public_matter(
        _matter({"name": "Acme Ltd", "confidence": "abc", "verified": True, "source": "ai"})
    )
    assert public["counterparty_confidence"] == 0.0
    assert public["counterparty_needs_confirmation"] is True


def test_confirmation_fields_present_for_every_matter():
    # The four fields are always projected (never conditionally), so the UI can
    # render the field without guarding for undefined.
    public = public_matter({"id": "m1", "subject": ""})
    for key in (
        "counterparty_confidence",
        "counterparty_verified",
        "counterparty_source",
        "counterparty_needs_confirmation",
    ):
        assert key in public
