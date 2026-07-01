"""Google API transport timeout bound.

Both googleapiclient ``build()`` sites (Gmail ``_gmail_service`` and Drive
``_drive_service``) must hand the client an authorized, socket-timeout-bounded
transport so a hung Gmail/Drive backend can never wedge the caller thread
forever. These tests pin the contract:

* ``build()`` receives ``http=`` (the authorized transport) and NOT
  ``credentials=`` (googleapiclient rejects both together).
* the underlying ``httplib2.Http`` is constructed with the configured timeout —
  30s by default, the override when ``NDA_GOOGLE_API_TIMEOUT_SECONDS`` is set.
* fail-open: if ``google_auth_httplib2`` is unavailable, ``build()`` still runs
  with ``credentials=`` + ``cache_discovery=False`` and no exception escapes.
* env fail-open: a non-integer env value falls back to the 30s default.
"""

from __future__ import annotations

import builtins
import importlib
import unittest
from unittest.mock import patch

from nda_automation import drive_integration, gmail_integration


class _FakeCreds:
    """A stand-in for google.oauth2 credentials; identity is all we assert on."""


def _reload_timeout_constants():
    """Re-read the module-level timeout constants from the current env.

    The constants are computed at import time, so a test that mutates
    ``NDA_GOOGLE_API_TIMEOUT_SECONDS`` must reload the modules for the new value
    to take effect. Reloading is cheap and leaves the public API intact.
    """
    importlib.reload(gmail_integration)
    importlib.reload(drive_integration)


class _BuildSpy:
    """Records the kwargs passed to ``googleapiclient.discovery.build``."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return object()  # the built service is opaque to these tests

    @property
    def last(self) -> dict:
        return self.calls[-1]["kwargs"]


class _HttpSpy:
    """Records the timeout passed to ``httplib2.Http(...)``."""

    def __init__(self):
        self.timeouts: list = []

    def __call__(self, *args, **kwargs):
        self.timeouts.append(kwargs.get("timeout"))
        return object()


class _AuthorizedHttpSpy:
    """Records the (creds, http) pair passed to AuthorizedHttp."""

    def __init__(self):
        self.calls: list[tuple] = []

    def __call__(self, creds, http=None):
        self.calls.append((creds, http))
        return object()


def _patch_transport(build_spy, http_spy, authed_spy):
    """Patch build + the httplib2/google_auth_httplib2 transport for one call.

    We patch the names in the modules the ``_*_service`` functions import from,
    so the local ``import httplib2`` / ``from google_auth_httplib2 import
    AuthorizedHttp`` inside the service resolve to our spies.
    """
    import google_auth_httplib2
    import httplib2
    from googleapiclient import discovery

    return [
        patch.object(discovery, "build", build_spy),
        patch.object(httplib2, "Http", http_spy),
        patch.object(google_auth_httplib2, "AuthorizedHttp", authed_spy),
    ]


class GmailServiceTimeoutTests(unittest.TestCase):
    def _run_gmail(self, build_spy, http_spy, authed_spy):
        with patch.object(gmail_integration, "_credentials_for_role", return_value=_FakeCreds()):
            for ctx in _patch_transport(build_spy, http_spy, authed_spy):
                ctx.start()
                self.addCleanup(ctx.stop)
            return gmail_integration._gmail_service("reviewer")

    def test_gmail_passes_bounded_http_default_timeout(self):
        build_spy, http_spy, authed_spy = _BuildSpy(), _HttpSpy(), _AuthorizedHttpSpy()
        self._run_gmail(build_spy, http_spy, authed_spy)

        # build() got the authorized transport, not raw credentials.
        self.assertIn("http", build_spy.last)
        self.assertNotIn("credentials", build_spy.last)
        self.assertIs(build_spy.last["cache_discovery"], False)
        # httplib2.Http was constructed with the 30s default deadline.
        self.assertEqual(http_spy.timeouts, [30])
        # AuthorizedHttp wrapped our creds.
        self.assertEqual(len(authed_spy.calls), 1)
        self.assertIsInstance(authed_spy.calls[0][0], _FakeCreds)

    def test_gmail_honors_env_override(self):
        with patch.dict("os.environ", {"NDA_GOOGLE_API_TIMEOUT_SECONDS": "7"}):
            _reload_timeout_constants()
            self.addCleanup(_reload_timeout_constants)  # restore default after test
            build_spy, http_spy, authed_spy = _BuildSpy(), _HttpSpy(), _AuthorizedHttpSpy()
            self._run_gmail(build_spy, http_spy, authed_spy)
            self.assertEqual(http_spy.timeouts, [7])
            self.assertIn("http", build_spy.last)
            self.assertNotIn("credentials", build_spy.last)


class DriveServiceTimeoutTests(unittest.TestCase):
    def _run_drive(self, build_spy, http_spy, authed_spy):
        with patch.object(
            drive_integration.google_connection,
            "credentials_for_role",
            return_value=_FakeCreds(),
        ):
            for ctx in _patch_transport(build_spy, http_spy, authed_spy):
                ctx.start()
                self.addCleanup(ctx.stop)
            return drive_integration._drive_service("user-1")

    def test_drive_passes_bounded_http_default_timeout(self):
        build_spy, http_spy, authed_spy = _BuildSpy(), _HttpSpy(), _AuthorizedHttpSpy()
        self._run_drive(build_spy, http_spy, authed_spy)

        self.assertIn("http", build_spy.last)
        self.assertNotIn("credentials", build_spy.last)
        self.assertIs(build_spy.last["cache_discovery"], False)
        self.assertEqual(http_spy.timeouts, [30])
        self.assertEqual(len(authed_spy.calls), 1)
        self.assertIsInstance(authed_spy.calls[0][0], _FakeCreds)

    def test_drive_honors_env_override(self):
        with patch.dict("os.environ", {"NDA_GOOGLE_API_TIMEOUT_SECONDS": "12"}):
            _reload_timeout_constants()
            self.addCleanup(_reload_timeout_constants)
            build_spy, http_spy, authed_spy = _BuildSpy(), _HttpSpy(), _AuthorizedHttpSpy()
            self._run_drive(build_spy, http_spy, authed_spy)
            self.assertEqual(http_spy.timeouts, [12])
            self.assertIn("http", build_spy.last)
            self.assertNotIn("credentials", build_spy.last)


class ImportErrorFailOpenTests(unittest.TestCase):
    """When google_auth_httplib2 is missing, service construction still works."""

    @staticmethod
    def _import_raiser(name, *args, **kwargs):
        if name == "google_auth_httplib2" or name.startswith("google_auth_httplib2."):
            raise ImportError("simulated missing google_auth_httplib2")
        return _ImportErrorFailOpenReal(name, *args, **kwargs)

    def test_gmail_falls_open_to_credentials_build(self):
        build_spy = _BuildSpy()
        from googleapiclient import discovery

        with patch.object(gmail_integration, "_credentials_for_role", return_value=_FakeCreds()):
            with patch.object(discovery, "build", build_spy):
                with patch.object(builtins, "__import__", side_effect=self._import_raiser):
                    # Must not raise.
                    gmail_integration._gmail_service("reviewer")

        self.assertIn("credentials", build_spy.last)
        self.assertNotIn("http", build_spy.last)
        self.assertIs(build_spy.last["cache_discovery"], False)

    def test_drive_falls_open_to_credentials_build(self):
        build_spy = _BuildSpy()
        from googleapiclient import discovery

        with patch.object(
            drive_integration.google_connection,
            "credentials_for_role",
            return_value=_FakeCreds(),
        ):
            with patch.object(discovery, "build", build_spy):
                with patch.object(builtins, "__import__", side_effect=self._import_raiser):
                    drive_integration._drive_service("user-1")

        self.assertIn("credentials", build_spy.last)
        self.assertNotIn("http", build_spy.last)
        self.assertIs(build_spy.last["cache_discovery"], False)


class EnvFailOpenTests(unittest.TestCase):
    def test_non_integer_env_falls_back_to_default(self):
        with patch.dict("os.environ", {"NDA_GOOGLE_API_TIMEOUT_SECONDS": "not-an-int"}):
            _reload_timeout_constants()
            self.addCleanup(_reload_timeout_constants)
            self.assertEqual(gmail_integration.GMAIL_API_TIMEOUT_SECONDS, 30)
            self.assertEqual(drive_integration.DRIVE_API_TIMEOUT_SECONDS, 30)

    def test_below_minimum_env_clamps_to_one(self):
        with patch.dict("os.environ", {"NDA_GOOGLE_API_TIMEOUT_SECONDS": "0"}):
            _reload_timeout_constants()
            self.addCleanup(_reload_timeout_constants)
            self.assertEqual(gmail_integration.GMAIL_API_TIMEOUT_SECONDS, 1)
            self.assertEqual(drive_integration.DRIVE_API_TIMEOUT_SECONDS, 1)


# Bound at module scope so the ImportError raiser can chain to the genuine import.
_ImportErrorFailOpenReal = builtins.__import__


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
