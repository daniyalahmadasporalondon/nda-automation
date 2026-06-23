"""Adobe PDF Services -- Export PDF (PDF -> DOCX).

Async REST flow (verified from developer.adobe.com PDF Services docs, 2026-06):
  1. Token:  POST https://pdf-services.adobe.io/token
             body (form-urlencoded): client_id, client_secret
             -> {"access_token": "..."}
  2. Asset:  POST https://pdf-services.adobe.io/assets
             headers: Authorization: Bearer <token>, x-api-key: <client_id>
             json: {"mediaType": "application/pdf"}
             -> {"assetID": "...", "uploadUri": "<presigned PUT url>"}
             then PUT the PDF bytes to uploadUri (Content-Type: application/pdf).
  3. Job:    POST https://pdf-services.adobe.io/operation/exportpdf
             headers: Authorization, x-api-key, Content-Type: application/json
             json: {"assetID": "...", "targetFormat": "docx"}
             -> 201 with a `location` response header (poll URL).
  4. Poll:   GET <location> (same auth headers) until json.status == "done"
             ("in progress" -> keep polling; "failed" -> error).
             On "done": json.asset.downloadUri is a presigned GET url.
  5. Result: GET downloadUri -> raw DOCX bytes.

Creds (ENV ONLY): ADOBE_CLIENT_ID, ADOBE_CLIENT_SECRET.
"""

from __future__ import annotations

import os
from pathlib import Path
import time

from . import _http

NAME = "adobe"
REQUIRED_ENV = ("ADOBE_CLIENT_ID", "ADOBE_CLIENT_SECRET")

_BASE = os.environ.get("ADOBE_PDF_SERVICES_BASE_URL", "https://pdf-services.adobe.io").rstrip("/")
_TOKEN_URL = f"{_BASE}/token"
_ASSETS_URL = f"{_BASE}/assets"
_EXPORT_URL = f"{_BASE}/operation/exportpdf"
_POLL_INTERVAL = float(os.environ.get("ADOBE_POLL_INTERVAL_SECONDS", "2"))
_POLL_TIMEOUT = float(os.environ.get("ADOBE_POLL_TIMEOUT_SECONDS", "300"))


def available() -> tuple[bool, str]:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name, "").strip()]
    if missing:
        return False, f"missing {', '.join(missing)}"
    return True, ""


def _token() -> str:
    import json

    _status, _headers, body = _http.post_form(
        _TOKEN_URL,
        {
            "client_id": os.environ["ADOBE_CLIENT_ID"],
            "client_secret": os.environ["ADOBE_CLIENT_SECRET"],
        },
    )
    return str(json.loads(body)["access_token"])


def _auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "x-api-key": os.environ["ADOBE_CLIENT_ID"],
    }


def convert(pdf_path: Path, out_path: Path) -> None:
    import json

    token = _token()
    headers = _auth_headers(token)

    # 1. Create asset + upload bytes.
    _s, _h, body = _http.post_json(_ASSETS_URL, {"mediaType": "application/pdf"}, headers=headers)
    asset = json.loads(body)
    asset_id, upload_uri = asset["assetID"], asset["uploadUri"]
    _http.put_bytes(upload_uri, _http.read_pdf(pdf_path), content_type="application/pdf")

    # 2. Submit export job -> poll location header.
    status, resp_headers, _ = _http.post_json(
        _EXPORT_URL, {"assetID": asset_id, "targetFormat": "docx"}, headers=headers
    )
    location = resp_headers.get("location") or resp_headers.get("Location")
    if not location:
        raise _http.HttpError(f"exportpdf returned {status} with no location header to poll")

    # 3. Poll.
    deadline = time.monotonic() + _POLL_TIMEOUT
    download_uri = ""
    while time.monotonic() < deadline:
        _ps, _ph, pbody = _http.get(location, headers=headers)
        result = json.loads(pbody)
        state = str(result.get("status", "")).lower()
        if state == "done":
            download_uri = result.get("asset", {}).get("downloadUri", "") or result.get(
                "downloadUri", ""
            )
            break
        if state == "failed":
            raise _http.HttpError(f"Adobe export job failed: {json.dumps(result)[:300]}")
        time.sleep(_POLL_INTERVAL)
    if not download_uri:
        raise _http.HttpError("Adobe export job did not reach 'done' before poll timeout")

    # 4. Download DOCX bytes.
    _ds, _dh, docx_bytes = _http.get(download_uri)
    out_path.write_bytes(docx_bytes)
