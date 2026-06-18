"""Display-derivation tests for the matter view's DocuSign-aware counterparty.

The dashboard card / Overview surface "who the NDA is with / who got the envelope".
For a DocuSign matter the ACTUAL party is the envelope's COUNTERPARTY signer, which
can diverge from the inbound reply recipient. ``public_matter`` now surfaces that
real signer (name + ``counterparty_email``) when an envelope exists, while keeping
``recipient_email`` (the reply recipient that the Gmail redline send/confirm contract
reads) byte-for-byte unchanged.

These tests pin:
  (a) a DocuSign matter whose envelope signer differs from the reply recipient
      DISPLAYS the envelope signer;
  (b) a non-DocuSign matter still displays the reply recipient unchanged;
  (c) a matter where they agree is unchanged;
  (d) the Aspora INTERNAL signer is never shown as the counterparty when a real
      counterparty signer exists;
  plus malformed/missing-data fail-open edges (degrade to the reply recipient,
  never crash) and the byte-unchanged ``recipient_email`` invariant.
"""

from __future__ import annotations

from nda_automation.matter_view import public_matter


def _base_matter(**overrides) -> dict:
    matter = {
        "id": "m-docusign",
        "subject": "Mutual NDA — Pranav <> Aspora",
        "reply_to": "pranav@acme.com",
        "sender": "pranav@acme.com",
        "received_at": "2026-06-10T09:00:00Z",
    }
    matter.update(overrides)
    return matter


def _docusign_block(signers, *, envelope_id="env-123") -> dict:
    return {
        "envelope_id": envelope_id,
        "status": "sent",
        "signers": signers,
    }


# (a) Envelope signer differs from the reply recipient -> display the signer.
def test_docusign_counterparty_signer_overrides_reply_recipient():
    matter = _base_matter(
        docusign=_docusign_block(
            [
                {"name": "Pranav Sharma", "email": "pranav.new@acme.com", "role": "counterparty"},
                {"name": "Daniyal Ahmad", "email": "daniyal.ahmad@aspora.com", "role": "aspora"},
            ]
        )
    )
    public = public_matter(matter, detail=False)
    # The displayed counterparty email reflects the ACTUAL envelope signer.
    assert public["counterparty_email"] == "pranav.new@acme.com"
    # The displayed counterparty NAME reflects the envelope signer name.
    assert public["counterparty"] == "Pranav Sharma"
    # recipient_email (the reply recipient / send-confirm source) is UNCHANGED.
    assert public["recipient_email"] == "pranav@acme.com"


# (b) No DocuSign envelope -> reply recipient unchanged.
def test_non_docusign_matter_displays_reply_recipient_unchanged():
    matter = _base_matter()
    public = public_matter(matter, detail=False)
    assert public["recipient_email"] == "pranav@acme.com"
    # counterparty_email falls back to the reply recipient.
    assert public["counterparty_email"] == "pranav@acme.com"


def test_docusign_block_without_envelope_id_falls_back():
    # A bare docusign block with no envelope id has no real recipient yet.
    matter = _base_matter(
        docusign=_docusign_block(
            [{"name": "Pranav", "email": "pranav.new@acme.com", "role": "counterparty"}],
            envelope_id="",
        )
    )
    public = public_matter(matter, detail=False)
    assert public["counterparty_email"] == "pranav@acme.com"
    assert public["recipient_email"] == "pranav@acme.com"


# (c) Signer agrees with the reply recipient -> unchanged.
def test_docusign_signer_agrees_with_reply_recipient():
    matter = _base_matter(
        docusign=_docusign_block(
            [
                {"name": "Pranav Sharma", "email": "pranav@acme.com", "role": "counterparty"},
                {"name": "Daniyal Ahmad", "email": "daniyal.ahmad@aspora.com", "role": "aspora"},
            ]
        )
    )
    public = public_matter(matter, detail=False)
    assert public["counterparty_email"] == "pranav@acme.com"
    assert public["recipient_email"] == "pranav@acme.com"


# (d) The Aspora internal signer is never shown as the counterparty.
def test_aspora_internal_signer_never_shown_as_counterparty():
    # Aspora signer is listed FIRST; the counterparty must still be selected.
    matter = _base_matter(
        docusign=_docusign_block(
            [
                {"name": "Daniyal Ahmad", "email": "daniyal.ahmad@aspora.com", "role": "aspora"},
                {"name": "Pranav Sharma", "email": "pranav.new@acme.com", "role": "counterparty"},
            ]
        )
    )
    public = public_matter(matter, detail=False)
    assert public["counterparty_email"] == "pranav.new@acme.com"
    assert public["counterparty"] == "Pranav Sharma"
    assert public["counterparty_email"] != "daniyal.ahmad@aspora.com"


def test_only_aspora_signer_falls_back_never_shows_internal():
    # If somehow only the Aspora signer is present, we fall back to the reply
    # recipient rather than surfacing the internal signer as the counterparty.
    matter = _base_matter(
        docusign=_docusign_block(
            [{"name": "Daniyal Ahmad", "email": "daniyal.ahmad@aspora.com", "role": "aspora"}]
        )
    )
    public = public_matter(matter, detail=False)
    assert public["counterparty_email"] == "pranav@acme.com"
    assert public["counterparty_email"] != "daniyal.ahmad@aspora.com"


# --- fail-open / malformed-data edges: degrade to reply recipient, never crash ---


def test_malformed_signers_list_falls_back():
    matter = _base_matter(docusign={"envelope_id": "env-1", "signers": "not-a-list"})
    public = public_matter(matter, detail=False)
    assert public["counterparty_email"] == "pranav@acme.com"


def test_missing_signers_falls_back():
    matter = _base_matter(docusign={"envelope_id": "env-1"})
    public = public_matter(matter, detail=False)
    assert public["counterparty_email"] == "pranav@acme.com"


def test_docusign_not_a_dict_falls_back():
    matter = _base_matter(docusign="garbage")
    public = public_matter(matter, detail=False)
    assert public["counterparty_email"] == "pranav@acme.com"


def test_counterparty_signer_with_invalid_email_falls_back():
    matter = _base_matter(
        docusign=_docusign_block(
            [{"name": "Pranav", "email": "not-an-email", "role": "counterparty"}]
        )
    )
    public = public_matter(matter, detail=False)
    # Invalid signer email -> fall back to the reply recipient, do not crash.
    assert public["counterparty_email"] == "pranav@acme.com"


def test_signer_without_explicit_role_is_treated_as_counterparty():
    # An override-send may omit role; a non-aspora signer is the counterparty.
    matter = _base_matter(
        docusign=_docusign_block(
            [{"name": "Pranav", "email": "pranav.new@acme.com"}]
        )
    )
    public = public_matter(matter, detail=False)
    assert public["counterparty_email"] == "pranav.new@acme.com"


# --- defense-in-depth: Aspora signer with a BLANK role is still never counterparty ---


def test_blank_role_aspora_domain_signer_first_is_not_counterparty():
    # A stale/unstamped override could list the Aspora signer FIRST with NO role.
    # The role filter alone would not skip it (role is blank, not "aspora"); the
    # aspora.com-domain backstop must skip it and select the external party.
    matter = _base_matter(
        docusign=_docusign_block(
            [
                {"name": "Daniyal Ahmad", "email": "daniyal.ahmad@aspora.com"},
                {"name": "Pranav Sharma", "email": "pranav.new@acme.com"},
            ]
        )
    )
    public = public_matter(matter, detail=False)
    assert public["counterparty_email"] == "pranav.new@acme.com"
    assert public["counterparty"] == "Pranav Sharma"
    assert public["counterparty_email"] != "daniyal.ahmad@aspora.com"


def test_only_blank_role_aspora_domain_signer_falls_back():
    # If the ONLY signer is an aspora.com address with a blank role, fall back to
    # the reply recipient rather than surfacing the internal signer.
    matter = _base_matter(
        docusign=_docusign_block(
            [{"name": "Daniyal Ahmad", "email": "daniyal.ahmad@aspora.com"}]
        )
    )
    public = public_matter(matter, detail=False)
    assert public["counterparty_email"] == "pranav@acme.com"
    assert public["counterparty_email"] != "daniyal.ahmad@aspora.com"


def test_signer_without_name_keeps_derived_counterparty_name():
    # A signer carrying only an email must not blank out the display name; the
    # email is surfaced but the name override is skipped.
    matter = _base_matter(
        intake_metadata={
            "counterparty": {"name": "Acme Ltd", "confidence": 0.95, "verified": True, "source": "ai"}
        },
        docusign=_docusign_block(
            [{"name": "", "email": "pranav.new@acme.com", "role": "counterparty"}]
        ),
    )
    public = public_matter(matter, detail=False)
    assert public["counterparty_email"] == "pranav.new@acme.com"
    # Name override skipped (empty signer name) -> keeps the AI-derived name.
    assert public["counterparty"] == "Acme Ltd"
