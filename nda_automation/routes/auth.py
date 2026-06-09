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


def handle_login_page(handler, *, send_body: bool = True) -> None:
    if google_identity.google_oauth_configured():
        action = '<a class="login-button" href="/auth/google/start">Continue with Google</a>'
    else:
        action = "<p>Google login is not configured for this deployment.</p>"
    handler._send_html(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NDA Automation Login</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; min-height: 100vh; display: grid; place-items: center; background: #f7f7fb; color: #202124; }}
    main {{ width: min(420px, calc(100vw - 32px)); background: #fff; border: 1px solid #dfe1e5; border-radius: 8px; padding: 28px; box-shadow: 0 18px 40px rgba(60, 64, 67, .12); }}
    h1 {{ font-size: 24px; margin: 0 0 8px; }}
    p {{ color: #5f6368; line-height: 1.45; }}
    .login-button {{ display: inline-flex; align-items: center; justify-content: center; margin-top: 12px; height: 44px; padding: 0 18px; border-radius: 6px; background: #1a73e8; color: #fff; text-decoration: none; font-weight: 600; }}
  </style>
</head>
<body>
  <main>
    <h1>NDA Automation</h1>
    <p>Sign in to access your matter workspace.</p>
    {action}
  </main>
</body>
</html>""",
        send_body=send_body,
    )


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
