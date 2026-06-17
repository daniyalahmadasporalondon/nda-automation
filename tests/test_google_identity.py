"""Tests for the Google identity OAuth code exchange error surfacing.

The token endpoint returns the *real* reason (invalid_grant /
redirect_uri_mismatch / invalid_client) in the JSON body of a 4xx. The exchange
helper must surface that reason in the raised error (so the 502 the callback
returns is diagnosable) while keeping the stable user-facing prefix, and must
not leak the client secret or let a malformed body inject log lines.
"""

from __future__ import annotations

import io
import json
import time
import urllib.error
from contextlib import contextmanager
from unittest.mock import patch

from nda_automation import google_identity


def _http_error(status: int, body: object) -> urllib.error.HTTPError:
    raw = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
    return urllib.error.HTTPError(
        url=google_identity.GOOGLE_TOKEN_URL,
        code=status,
        msg="Bad Request",
        hdrs={},  # type: ignore[arg-type]
        fp=io.BytesIO(raw),
    )


def _exchange_raising(error: Exception) -> google_identity.GoogleIdentityError:
    env = {
        google_identity.GOOGLE_OAUTH_CLIENT_ID_ENV: "client-123",
        google_identity.GOOGLE_OAUTH_CLIENT_SECRET_ENV: "super-secret-value",
    }
    with patch.dict("os.environ", env, clear=False):
        with patch("urllib.request.urlopen", side_effect=error):
            try:
                google_identity.exchange_google_code("auth-code", redirect_uri="https://app/cb")
            except google_identity.GoogleIdentityError as raised:
                return raised
    raise AssertionError("Expected GoogleIdentityError")


def test_invalid_grant_reason_is_surfaced():
    raised = _exchange_raising(
        _http_error(400, {"error": "invalid_grant", "error_description": "Bad Request"})
    )
    text = str(raised)
    # Stable user-facing prefix is preserved (frontend + existing callers rely on it).
    assert text.startswith("Google OAuth token exchange failed.")
    # The real reason is now visible for diagnosis.
    assert "invalid_grant" in text


def test_redirect_uri_mismatch_reason_is_surfaced():
    raised = _exchange_raising(
        _http_error(400, {"error": "redirect_uri_mismatch", "error_description": "bad uri"})
    )
    text = str(raised)
    assert text.startswith("Google OAuth token exchange failed.")
    assert "redirect_uri_mismatch" in text


def test_secret_is_never_leaked_in_error():
    raised = _exchange_raising(
        _http_error(401, {"error": "invalid_client", "error_description": "Unauthorized"})
    )
    assert "super-secret-value" not in str(raised)
    assert "invalid_client" in str(raised)


def test_network_error_keeps_generic_message():
    raised = _exchange_raising(urllib.error.URLError("connection refused"))
    # A transport failure has no Google body to surface; the generic message stands.
    assert str(raised) == "Google OAuth token exchange failed."


def test_detail_is_single_line_and_length_capped():
    raised = _exchange_raising(
        _http_error(400, {"error": "invalid_grant", "error_description": "line one\nline two\r\n" + "x" * 500})
    )
    text = str(raised)
    # No newlines escaped into the message (log-injection / multi-line safety).
    assert "\n" not in text and "\r" not in text
    # Sanitised detail is capped well under the raw 500-char description.
    assert len(text) < 320


def test_unparseable_body_falls_back_to_raw_snippet():
    raised = _exchange_raising(_http_error(400, "not json at all"))
    text = str(raised)
    assert text.startswith("Google OAuth token exchange failed.")
    assert "not json at all" in text


# --- ID-token verification (FIX #16: exp / iss / nbf / nonce) ---------------

class _FakeTokeninfoResponse(io.BytesIO):
    """Minimal stand-in for the urlopen() context-manager / response object."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@contextmanager
def _tokeninfo(claims: dict):
    """Patch the tokeninfo HTTP call to return `claims` and pin the client id."""
    env = {google_identity.GOOGLE_OAUTH_CLIENT_ID_ENV: "client-123"}
    body = json.dumps(claims).encode("utf-8")
    with patch.dict("os.environ", env, clear=False):
        with patch(
            "urllib.request.urlopen",
            return_value=_FakeTokeninfoResponse(body),
        ):
            yield


def _valid_claims(**overrides):
    now = int(time.time())
    claims = {
        "aud": "client-123",
        "sub": "google-user-123",
        "email": "alice@example.com",
        "email_verified": "true",
        "iss": "https://accounts.google.com",
        "exp": str(now + 3600),
        "iat": str(now),
    }
    claims.update(overrides)
    return claims


def test_valid_token_passes():
    with _tokeninfo(_valid_claims()):
        profile = google_identity.verify_google_id_token("id-token")
    assert profile["sub"] == "google-user-123"


def test_expired_token_is_rejected():
    now = int(time.time())
    with _tokeninfo(_valid_claims(exp=str(now - 3600))):
        try:
            google_identity.verify_google_id_token("id-token")
        except google_identity.GoogleIdentityError as error:
            assert "expired" in str(error).lower()
        else:
            raise AssertionError("Expected expired token to be rejected")


def test_missing_exp_is_rejected():
    claims = _valid_claims()
    claims.pop("exp")
    with _tokeninfo(claims):
        try:
            google_identity.verify_google_id_token("id-token")
        except google_identity.GoogleIdentityError as error:
            assert "expiry" in str(error).lower()
        else:
            raise AssertionError("Expected missing-exp token to be rejected")


def test_wrong_issuer_is_rejected():
    with _tokeninfo(_valid_claims(iss="https://evil.example.com")):
        try:
            google_identity.verify_google_id_token("id-token")
        except google_identity.GoogleIdentityError as error:
            assert "issuer" in str(error).lower()
        else:
            raise AssertionError("Expected wrong-issuer token to be rejected")


def test_not_yet_valid_token_is_rejected():
    now = int(time.time())
    with _tokeninfo(_valid_claims(nbf=str(now + 3600))):
        try:
            google_identity.verify_google_id_token("id-token")
        except google_identity.GoogleIdentityError as error:
            assert "not yet valid" in str(error).lower()
        else:
            raise AssertionError("Expected not-yet-valid token to be rejected")


def test_nonce_mismatch_is_rejected():
    with _tokeninfo(_valid_claims(nonce="token-nonce")):
        try:
            google_identity.verify_google_id_token("id-token", expected_nonce="other-nonce")
        except google_identity.GoogleIdentityError as error:
            assert "nonce" in str(error).lower()
        else:
            raise AssertionError("Expected nonce mismatch to be rejected")


def test_missing_nonce_when_expected_is_rejected():
    with _tokeninfo(_valid_claims()):  # no nonce claim present
        try:
            google_identity.verify_google_id_token("id-token", expected_nonce="expected-nonce")
        except google_identity.GoogleIdentityError as error:
            assert "nonce" in str(error).lower()
        else:
            raise AssertionError("Expected missing nonce to be rejected")


def test_matching_nonce_passes():
    with _tokeninfo(_valid_claims(nonce="the-nonce")):
        profile = google_identity.verify_google_id_token("id-token", expected_nonce="the-nonce")
    assert profile["nonce"] == "the-nonce"
