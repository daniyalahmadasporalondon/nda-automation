"""Regression: the counterparty email typed at NDA generation must persist onto
the matter so the send-for-signature path can resolve the counterparty signer.

Confirmed bug (before the fix): the draft-intake form sends
``counterparty.email`` in the generate payload, but ``intake_from_payload`` never
read it and the generated matter was created with no ``reply_to``/``sender``.
Opening that matter later to send for signature showed a blank email and the
send failed (400, SignerResolutionError) because
``gmail_integration.matter_reply_recipient`` returned ``""`` and
``docusign_workflow._counterparty_signer`` returned ``None``.

These tests drive the REAL generation workflow (the function the
``POST /api/generate-nda`` route calls) and assert the email lands in the field
the send path reads, that the outbound resolver returns it, and that the
signer-resolution step yields a counterparty signer with no
``SignerResolutionError``. A parallel test confirms generation WITHOUT an email
still succeeds and simply leaves the email unset.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from nda_automation import (
    docusign_workflow,
    gmail_integration,
    matter_store,
    nda_generation_workflow,
)
from nda_automation.review_engine import ACTIVE_REVIEW_ENGINE_ENV


# Deterministic prod-matching config: AI generation kill-switch OFF, deterministic
# review engine selected. The network-free self-check / ship-gate still run.
_DETERMINISTIC_ENV = {
    ACTIVE_REVIEW_ENGINE_ENV: "deterministic",
    "NDA_GENERATION_AI_ENABLED": "false",
}

_OWNER = "google:test-owner"


def _payload(counterparty_email=None):
    body = {
        "signing_entity_id": "aspora_technology",
        "intake": {
            "counterparty_name": "Counterparty Signing Limited",
            "project": "evaluating a potential commercial relationship",
            "term_years": 2,
            "nda_type": "mutual",
        },
        "counterparty": {"name": "Counterparty Signing Limited"},
    }
    if counterparty_email is not None:
        body["counterparty"]["email"] = counterparty_email
    return body


class GenerationCounterpartyEmailTests(unittest.TestCase):
    def _matter_store_patches(self, data_dir):
        data_path = matter_store.Path(data_dir)
        return (
            patch.object(matter_store, "DATA_DIR", data_path),
            patch.object(matter_store, "MATTERS_PATH", data_path / "matters.json"),
            patch.object(matter_store, "UPLOADS_DIR", data_path / "uploads"),
        )

    def _generate(self, payload):
        with tempfile.TemporaryDirectory() as data_dir:
            p = self._matter_store_patches(data_dir)
            with p[0], p[1], p[2], patch.dict(os.environ, _DETERMINISTIC_ENV):
                result = nda_generation_workflow.generate_nda_from_payload(
                    payload, owner_user_id=_OWNER
                )
                # Re-read the matter from the store so we assert the PERSISTED shape
                # (json round-tripped), not just the in-memory dict.
                reloaded = matter_store.get_matter(
                    str(result.matter.get("id") or ""), owner_user_id=_OWNER
                )
                self.assertIsNotNone(reloaded, "generated matter was not persisted")
                return reloaded

    def test_typed_email_persists_and_send_path_resolves_counterparty_signer(self):
        email = "jane.signer@example.com"
        matter = self._generate(_payload(email))

        # 1. The email is persisted in the field the send path reads (reply_to),
        #    canonicalised.
        self.assertEqual(matter.get("reply_to"), email)

        # 2. The outbound resolver returns it.
        self.assertEqual(gmail_integration.matter_reply_recipient(matter), email)

        # 3. Send-for-signature resolves a counterparty signer (no
        #    SignerResolutionError) carrying that email.
        signer = docusign_workflow._counterparty_signer(matter)
        self.assertIsNotNone(signer, "counterparty signer did not resolve")
        self.assertEqual(signer["email"], email)

        # And the full signer-resolution step does not raise.
        try:
            signers = docusign_workflow._resolve_signers(matter, None)
        except docusign_workflow.SignerResolutionError as error:  # pragma: no cover
            self.fail(f"SignerResolutionError raised for a matter with an email: {error}")
        resolved_emails = {getattr(s, "email", None) for s in signers}
        self.assertIn(email, resolved_emails)

    def test_email_is_canonicalised_from_display_name_form(self):
        # The FE may send "Name <email>"; it must persist canonicalised so the
        # send path resolves it.
        matter = self._generate(_payload("Jane Signer <Jane.Signer@Example.com>"))
        self.assertEqual(matter.get("reply_to"), "jane.signer@example.com")
        self.assertEqual(
            gmail_integration.matter_reply_recipient(matter), "jane.signer@example.com"
        )

    def test_generation_without_email_still_succeeds_and_leaves_email_unset(self):
        # No email key at all.
        matter = self._generate(_payload(None))
        self.assertTrue(matter.get("id"))
        self.assertFalse(matter.get("reply_to"), "no email should leave reply_to unset")
        self.assertEqual(gmail_integration.matter_reply_recipient(matter), "")
        # The counterparty signer is unresolvable (as before the fix) -- the send
        # path simply has no auto-resolved counterparty contact.
        self.assertIsNone(docusign_workflow._counterparty_signer(matter))

    def test_blank_email_behaves_exactly_like_no_email(self):
        matter = self._generate(_payload("   "))
        self.assertFalse(matter.get("reply_to"))
        self.assertEqual(gmail_integration.matter_reply_recipient(matter), "")

    def test_implausible_email_is_dropped_not_crashed(self):
        # A garbage value must not crash generation and must not persist a junk
        # contact (it would only fail the send later); it behaves like no email.
        matter = self._generate(_payload("not-an-email"))
        self.assertTrue(matter.get("id"))
        self.assertFalse(matter.get("reply_to"))
        self.assertEqual(gmail_integration.matter_reply_recipient(matter), "")


if __name__ == "__main__":
    unittest.main()
