from __future__ import annotations

from http.cookies import SimpleCookie
import os
from urllib.parse import parse_qs, urlparse

from .. import app_settings, gmail_integration, google_identity, user_store
from ..http_auth import _basic_auth_credentials, _basic_auth_matches


def handle_auth_status(handler, *, send_body: bool = True) -> None:
    user = current_session_user(handler) or _current_basic_user(handler)
    handler._send_json(
        {
            "authenticated": user is not None,
            "user": user_store.public_user(user),
            "login_url": "/auth/google/start" if google_identity.google_oauth_configured() else "",
            "logout_url": "/api/auth/logout",
            "google_oauth_configured": google_identity.google_oauth_configured(),
        },
        send_body=send_body,
    )


_GOOGLE_G_SVG = (
    '<svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">'
    '<path fill="#4285F4" d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844a4.14 4.14 0 0 1-1.796 2.716v2.259h2.908c1.702-1.567 2.684-3.875 2.684-6.615z"/>'
    '<path fill="#34A853" d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332A8.997 8.997 0 0 0 9 18z"/>'
    '<path fill="#FBBC05" d="M3.964 10.71A5.41 5.41 0 0 1 3.682 9c0-.593.102-1.17.282-1.71V4.958H.957A8.996 8.996 0 0 0 0 9c0 1.452.348 2.827.957 4.042l3.007-2.332z"/>'
    '<path fill="#EA4335" d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0A8.997 8.997 0 0 0 .957 4.958L3.964 7.29C4.672 5.163 6.656 3.58 9 3.58z"/>'
    "</svg>"
)

# Split-screen login: a purple brand panel (left) + the Google sign-in (right).
# Self-contained (the login page does not load the app's styles.css). __ACTION__
# is replaced with the Continue-with-Google button or a not-configured notice.
_LOGIN_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sign in &middot; Aspora NDA</title>
  <style>
    @font-face { font-family: "Inter"; font-display: swap; font-weight: 100 900; src: url("/static/assets/fonts/InterVariable.woff2") format("woff2"); }
    * { box-sizing: border-box; }
    html, body { height: 100%; margin: 0; }
    body {
      font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      color: #170a33;
      display: grid;
      grid-template-columns: 1.05fr 1fr;
      min-height: 100vh;
    }
    .brand {
      position: relative;
      overflow: hidden;
      color: #fff;
      padding: 52px 60px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      background:
        radial-gradient(110% 70% at 0% 0%, rgba(255, 255, 255, 0.16), rgba(255, 255, 255, 0) 46%),
        linear-gradient(150deg, #3d1293 0%, #5424c9 52%, #7c3aed 100%);
    }
    .brand-logo img { height: 30px; width: auto; display: block; filter: brightness(0) invert(1); }
    .brand-copy h2 { font-size: 40px; line-height: 1.1; font-weight: 750; letter-spacing: -0.02em; margin: 0 0 20px; max-width: 13ch; }
    .brand-copy p { font-size: 16.5px; line-height: 1.55; color: rgba(255, 255, 255, 0.74); margin: 0; max-width: 44ch; }
    .brand-footer { font-size: 13px; color: rgba(255, 255, 255, 0.6); }
    .login { display: grid; place-items: center; padding: 48px; background: #ffffff; }
    .login-inner { width: min(380px, 100%); }
    .login h1 { font-size: 30px; font-weight: 760; letter-spacing: -0.01em; margin: 0 0 8px; }
    .login .sub { color: #5d5470; font-size: 15px; line-height: 1.5; margin: 0 0 30px; }
    .google-btn {
      display: flex; align-items: center; justify-content: center; gap: 12px;
      width: 100%; height: 50px; border: 0; border-radius: 13px;
      background: #5b2bd6; color: #fff; font-weight: 700; font-size: 15px;
      text-decoration: none; cursor: pointer;
      transition: background 0.15s ease, transform 0.08s ease;
    }
    .google-btn:hover { background: #6a3ee0; }
    .google-btn:active { transform: translateY(1px); }
    .google-btn .g { width: 26px; height: 26px; border-radius: 7px; background: #fff; display: grid; place-items: center; }
    .help { margin: 20px 0 0; font-size: 13.5px; color: #8e869e; }
    .help a { color: #5b2bd6; text-decoration: none; font-weight: 600; }
    .not-configured { color: #b42318; font-size: 14px; line-height: 1.5; }
    @media (max-width: 880px) {
      body { grid-template-columns: 1fr; }
      .brand { display: none; }
      .login { min-height: 100vh; }
    }
  </style>
</head>
<body>
  <section class="brand">
    <div class="brand-logo">
      <img src="/static/assets/aspora-logo.png" alt="Aspora">
    </div>
    <div class="brand-copy">
      <h2>AI-first NDA review, end to end.</h2>
      <p>Import, review against your playbook, redline, and send. Every NDA in one workspace, reviewed in minutes.</p>
    </div>
    <div class="brand-footer">&copy; 2026 Aspora &middot; All rights reserved</div>
  </section>
  <section class="login">
    <div class="login-inner">
      <h1>Sign in</h1>
      <p class="sub">Welcome back. Sign in with your Aspora Google account.</p>
      __ACTION__
      <p class="help">Need access? Ask your Aspora admin.</p>
    </div>
  </section>
</body>
</html>"""


def handle_login_page(handler, *, send_body: bool = True) -> None:
    if google_identity.google_oauth_configured():
        action = (
            '<a class="google-btn" href="/auth/google/start">'
            '<span class="g">' + _GOOGLE_G_SVG + "</span>"
            "Continue with Google</a>"
        )
    else:
        action = '<p class="not-configured">Google login is not configured for this deployment.</p>'
    handler._send_html(_LOGIN_PAGE_HTML.replace("__ACTION__", action), send_body=send_body)


def handle_google_start(handler, *, send_body: bool = True) -> None:
    if not google_identity.google_oauth_configured():
        handler._send_json({"error": "Google OAuth is not configured."}, status=503, send_body=send_body)
        return
    query = parse_qs(urlparse(handler.path).query)
    next_path = query.get("next", ["/"])[0]
    state = user_store.create_login_state(next_path=next_path)
    redirect_uri = _google_redirect_uri(handler)
    # One Google sign-in covers login (identity) AND Gmail+Drive access: request
    # the identity scopes plus the unified connect scopes, with offline access so
    # the callback can save a refresh token. The user grants everything once
    # instead of logging in and then separately connecting Gmail.
    connect_scopes = gmail_integration._gmail_oauth_scopes_for_role("all")
    scopes = list(google_identity.GOOGLE_IDENTITY_SCOPES) + [
        scope for scope in connect_scopes if scope not in google_identity.GOOGLE_IDENTITY_SCOPES
    ]
    auth_url = google_identity.build_google_authorization_url(
        redirect_uri=redirect_uri,
        state=state,
        scopes=scopes,
        access_type="offline",
        prompt="select_account consent",
    )
    handler._send_redirect(
        auth_url,
        headers={"Set-Cookie": _state_cookie(handler, state)},
        send_body=send_body,
    )


def handle_google_callback(handler, *, send_body: bool = True) -> None:
    query = parse_qs(urlparse(handler.path).query)
    if query.get("error"):
        handler._send_json({"error": "Google login was not completed."}, status=400, send_body=send_body)
        return
    code = query.get("code", [""])[0]
    state = query.get("state", [""])[0]
    if not code or not state or state != _cookie_value(handler, user_store.OAUTH_STATE_COOKIE_NAME):
        handler._send_json({"error": "Google login state is invalid."}, status=400, send_body=send_body)
        return
    state_record = user_store.consume_login_state(state)
    if state_record is None:
        handler._send_json({"error": "Google login state is invalid or expired."}, status=400, send_body=send_body)
        return
    try:
        token_response = google_identity.exchange_google_code(code, redirect_uri=_google_redirect_uri(handler))
        profile = google_identity.verify_google_id_token(str(token_response.get("id_token") or ""))
        user = user_store.upsert_google_user(profile)
        session_token = user_store.create_session(str(user.get("id") or ""))
    except (google_identity.GoogleIdentityError, user_store.UserStoreError) as error:
        handler._send_json({"error": str(error)}, status=502, send_body=send_body)
        return

    # The single sign-in also granted Gmail + Drive; persist those tokens so the
    # user is connected without a second consent. Best-effort: login must still
    # succeed even if no usable token came back (then the user can reconnect from
    # Admin), so any failure here is swallowed.
    owner_user_id = str(user.get("id") or "")
    try:
        connected_roles = gmail_integration.save_user_gmail_oauth_token(owner_user_id, token_response, role="all")
        app_settings.update_gmail_settings({"inbound_enabled": True, "outbound_enabled": True})
        if "drive" in connected_roles:
            app_settings.update_drive_settings({"enabled": True})
    except Exception:  # pragma: no cover - connect-side save is best-effort
        pass

    next_path = str(state_record.get("next_path") or "/")
    handler._send_redirect(
        next_path,
        headers={"Set-Cookie": _session_cookie(handler, session_token)},
        send_body=send_body,
    )


def handle_logout(handler) -> None:
    user_store.delete_session(_cookie_value(handler, user_store.SESSION_COOKIE_NAME))
    handler._send_json(
        {"authenticated": False, "user": None},
        headers={"Set-Cookie": _expired_cookie(handler, user_store.SESSION_COOKIE_NAME)},
    )


def current_session_user(handler) -> dict | None:
    return user_store.user_for_session_token(_cookie_value(handler, user_store.SESSION_COOKIE_NAME))


def _current_basic_user(handler) -> dict | None:
    username = os.environ.get("NDA_AUTH_USERNAME", "").strip()
    password = os.environ.get("NDA_AUTH_PASSWORD", "")
    auth_header = handler.headers.get("Authorization", "")
    if not username or not password or not _basic_auth_matches(auth_header, username, password):
        return None
    credentials = _basic_auth_credentials(auth_header)
    user_id = credentials[0] if credentials else username
    return {
        "id": user_id,
        "provider": "basic",
        "email": user_id,
        "name": user_id,
        "picture": "",
    }


def _google_redirect_uri(handler) -> str:
    configured = google_identity.configured_redirect_uri()
    if configured:
        return configured
    return f"{_request_base_url(handler)}/auth/google/callback"


def _request_base_url(handler) -> str:
    scheme = handler.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip() or "http"
    host = handler.headers.get("X-Forwarded-Host", "").split(",", 1)[0].strip()
    if not host:
        host = handler.headers.get("Host", "").strip()
    if not host:
        server_host, server_port = handler.server.server_address[:2]
        host = f"{server_host}:{server_port}"
    return f"{scheme}://{host}"


def _cookie_value(handler, name: str) -> str:
    cookie = SimpleCookie()
    try:
        cookie.load(handler.headers.get("Cookie", ""))
    except Exception:
        return ""
    morsel = cookie.get(name)
    return morsel.value if morsel is not None else ""


def _session_cookie(handler, token: str) -> str:
    return _cookie_header(
        handler,
        user_store.SESSION_COOKIE_NAME,
        token,
        max_age=user_store.SESSION_TTL_SECONDS,
    )


def _state_cookie(handler, state: str) -> str:
    return _cookie_header(
        handler,
        user_store.OAUTH_STATE_COOKIE_NAME,
        state,
        max_age=user_store.LOGIN_STATE_TTL_SECONDS,
    )


def _expired_cookie(handler, name: str) -> str:
    return _cookie_header(handler, name, "", max_age=0)


def _cookie_header(handler, name: str, value: str, *, max_age: int) -> str:
    safe_value = str(value).replace(";", "").replace("\r", "").replace("\n", "")
    parts = [
        f"{name}={safe_value}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={max_age}",
    ]
    if _request_base_url(handler).startswith("https://"):
        parts.append("Secure")
    return "; ".join(parts)
