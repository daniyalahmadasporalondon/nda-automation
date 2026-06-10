"""Tests for the Google OAuth authorization URL (account chooser + login hint).

Connecting Gmail/Drive must let the operator PICK the account: the URL forces
`prompt=select_account` (so Google always shows the chooser instead of silently
reusing the browser's active session) and passes `login_hint` to pre-select the
signed-in app account.
"""

import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from nda_automation import google_connection


class GoogleAuthorizationUrlTests(unittest.TestCase):
    def _build(self, **kwargs):
        with patch.dict(
            os.environ,
            {
                "NDA_GOOGLE_OAUTH_CLIENT_ID": "client-123",
                "NDA_GOOGLE_OAUTH_CLIENT_SECRET": "secret-xyz",
            },
            clear=False,
        ):
            return google_connection.build_authorization_url(
                redirect_uri="https://nda.example.com/auth/drive/callback",
                state="state-token",
                role="drive",
                **kwargs,
            )

    def test_forces_account_chooser(self):
        query = parse_qs(urlparse(self._build()).query)
        self.assertEqual(query["prompt"], ["select_account consent"])

    def test_includes_login_hint_when_provided(self):
        query = parse_qs(urlparse(self._build(login_hint="daniyal.ahmad@aspora.com")).query)
        self.assertEqual(query["login_hint"], ["daniyal.ahmad@aspora.com"])

    def test_omits_login_hint_when_empty(self):
        query = parse_qs(urlparse(self._build(login_hint="")).query)
        self.assertNotIn("login_hint", query)


if __name__ == "__main__":
    unittest.main()
