"""Regression tests for the Drive OAuth redirect URI.

Drive must use its own ``/auth/drive/callback`` and must NOT reuse the Gmail
redirect (``NDA_GMAIL_OAUTH_REDIRECT_URI`` -> ``/auth/gmail/callback``). Reusing
it routed the Drive consent to the Gmail callback, which rejected the request on
the OAuth-state purpose mismatch ("drive" state at the "gmail" handler), so Drive
could never connect on a deployment that set the Gmail redirect.
"""

import os
import unittest
from unittest.mock import patch

from nda_automation.routes import drive as drive_routes


class _FakeServer:
    server_address = ("127.0.0.1", 8787)


class _FakeHandler:
    def __init__(self, headers):
        self.headers = headers
        self.server = _FakeServer()


def _handler():
    return _FakeHandler({
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "nda.example.com",
    })


class DriveRedirectUriTests(unittest.TestCase):
    def test_uses_drive_callback_even_when_gmail_redirect_configured(self):
        with patch.dict(
            os.environ,
            {"NDA_GMAIL_OAUTH_REDIRECT_URI": "https://nda.example.com/auth/gmail/callback"},
            clear=False,
        ):
            os.environ.pop(drive_routes.DRIVE_OAUTH_REDIRECT_URI_ENV, None)
            uri = drive_routes._drive_redirect_uri(_handler())
        self.assertEqual(uri, "https://nda.example.com/auth/drive/callback")

    def test_honors_drive_specific_redirect_override(self):
        with patch.dict(
            os.environ,
            {drive_routes.DRIVE_OAUTH_REDIRECT_URI_ENV: "https://custom.example.com/auth/drive/callback"},
            clear=False,
        ):
            uri = drive_routes._drive_redirect_uri(_handler())
        self.assertEqual(uri, "https://custom.example.com/auth/drive/callback")


if __name__ == "__main__":
    unittest.main()
