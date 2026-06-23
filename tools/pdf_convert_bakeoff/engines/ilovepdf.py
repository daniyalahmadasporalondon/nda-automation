"""iLovePDF / iLoveAPI -- PDF -> Word.

DOC-LOOKUP CAVEAT (verified 2026-06 against www.iloveapi.com/docs/api-reference
AND the official ilovepdf/ilovepdf-php SDK task classes):

  The iLoveAPI developer REST `process` API does NOT publicly expose a PDF->Word
  tool. The documented {tool} values are: compress, extract, htmlpdf, imagepdf,
  merge, officepdf, pagenumber, pdfa, pdfjpg, pdfocr, protect, repair, rotate,
  split, unlock, validatepdfa, editpdf, watermark, pdfmarkdown, summarize,
  pdfextract, splitsmart, formsdetect. `officepdf` is Office->PDF (the WRONG
  direction). PDF->Word exists in the iLovePDF consumer web app but is not a
  task class in the developer SDK.

This adapter implements the FULL correct async REST flow (auth -> start -> upload
-> process -> download) and parameterises the tool name via ILOVEPDF_TOOL
(default "pdfword") so it works immediately IF/WHEN iLoveAPI ships the tool or if
your account has it enabled. As shipped it will surface the API's tool error
clearly per (doc, engine) rather than silently producing a wrong-direction
output. See README "iLovePDF caveat".

Flow:
  1. Auth:    POST https://api.ilovepdf.com/v1/auth   json: {"public_key": ...}
              -> {"token": "<JWT>"}
  2. Start:   GET https://api.ilovepdf.com/v1/start/{tool}   (Bearer JWT)
              -> {"server": "<assigned>", "task": "<id>"}
  3. Upload:  POST https://{server}/v1/upload   multipart: task, file
              -> {"server_filename": "..."}
  4. Process: POST https://{server}/v1/process  json: {task, tool,
              files:[{server_filename, filename}]}
  5. Download: GET https://{server}/v1/download/{task}  -> the converted file bytes
              (a single DOCX, or a ZIP if the tool returns multiple files).

Creds (ENV ONLY): ILOVEPDF_PUBLIC_KEY, ILOVEPDF_SECRET_KEY.
(The SECRET key signs self-issued JWTs for advanced use; the public-key /v1/auth
exchange above is sufficient for the synchronous flow, so SECRET is required for
parity/skip-gating but not transmitted by this simple flow.)
"""

from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from . import _http

NAME = "ilovepdf"
REQUIRED_ENV = ("ILOVEPDF_PUBLIC_KEY", "ILOVEPDF_SECRET_KEY")

_AUTH_URL = "https://api.ilovepdf.com/v1/auth"
_START_BASE = "https://api.ilovepdf.com/v1/start"
_TOOL = os.environ.get("ILOVEPDF_TOOL", "pdfword")


def available() -> tuple[bool, str]:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name, "").strip()]
    if missing:
        return False, f"missing {', '.join(missing)}"
    return True, ""


def _jwt() -> str:
    _s, _h, body = _http.post_json(_AUTH_URL, {"public_key": os.environ["ILOVEPDF_PUBLIC_KEY"]})
    return str(json.loads(body)["token"])


def convert(pdf_path: Path, out_path: Path) -> None:
    token = _jwt()
    bearer = {"Authorization": f"Bearer {token}"}

    # 1. Start -> assigned server + task.
    _s, _h, body = _http.get(f"{_START_BASE}/{_TOOL}", headers=bearer)
    start = json.loads(body)
    server, task = start["server"], start["task"]

    # 2. Upload the PDF.
    _us, _uh, ubody = _http.post_multipart(
        f"https://{server}/v1/upload",
        files={"file": (Path(pdf_path).name, _http.read_pdf(pdf_path), "application/pdf")},
        form_fields={"task": task},
        headers=bearer,
    )
    server_filename = json.loads(ubody)["server_filename"]

    # 3. Process.
    _http.post_json(
        f"https://{server}/v1/process",
        {
            "task": task,
            "tool": _TOOL,
            "files": [{"server_filename": server_filename, "filename": Path(pdf_path).name}],
        },
        headers=bearer,
    )

    # 4. Download (may be a single DOCX or a ZIP wrapping it).
    _ds, _dh, payload = _http.get(f"https://{server}/v1/download/{task}", headers=bearer)
    out_path.write_bytes(_unwrap_docx(payload))


def _unwrap_docx(payload: bytes) -> bytes:
    """iLovePDF returns a ZIP when a task yields multiple files. If the payload is a
    ZIP containing exactly one .docx, return that member; otherwise return as-is
    (a bare .docx) and let downstream DOCX validation catch a true mismatch."""
    try:
        with ZipFile(BytesIO(payload)) as archive:
            docx_members = [n for n in archive.namelist() if n.lower().endswith(".docx")]
            if len(docx_members) == 1:
                return archive.read(docx_members[0])
    except BadZipFile:
        pass
    return payload
