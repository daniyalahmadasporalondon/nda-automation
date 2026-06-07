"""User-placed PDF markup: validation, storage shaping, and PyMuPDF baking.

This module backs the interactive PDF markup feature where a reviewer places
comments / highlights / strikethroughs directly on a matter's PDF pages. Unlike
``annotated_pdf_export`` (which derives highlights from the stored REVIEW result
by *searching the page text*), the annotations here are placed by the user at an
explicit COORDINATE.

Coordinate contract (shared verbatim with the frontend)
-------------------------------------------------------
Stored coordinates are NORMALIZED relative to a page, origin TOP-LEFT, every
value in ``[0, 1]``::

    rect = {"x": 0..1, "y": 0..1, "w": 0..1, "h": 0..1}

where ``x, y`` is the top-left corner of the box and ``w, h`` are the width /
height as fractions of the page. A "comment" is a POINT (``w`` and ``h`` ~ 0).

PyMuPDF's page coordinate system *also* has its origin at the TOP-LEFT
(``page.rect``), so the normalized -> PDF-points mapping is direct::

    px0 = x * page.rect.width
    py0 = y * page.rect.height
    px1 = (x + w) * page.rect.width
    py1 = (y + h) * page.rect.height
    rect = fitz.Rect(px0, py0, px1, py1)

For a comment point we use ``fitz.Point(x * W, y * H)``.

Everything in this module is a *pure* helper: it never stamps ``created_at`` or
assigns an ``id`` (those are server responsibilities done in the route, so the
helpers stay deterministic and unit-testable).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: The annotation kinds the frontend may place.
ANNOTATION_TYPES = ("comment", "highlight", "strikethrough")

#: Upper bound on a comment / note body, to bound stored size.
MAX_ANNOTATION_TEXT_CHARS = 2000

#: Per-matter cap on stored annotations, to bound storage / DoS.
MAX_ANNOTATIONS_PER_MATTER = 500

MARKED_UP_PDF_MIME = "application/pdf"


class PdfMarkupError(ValueError):
    """A user annotation failed validation."""


class PdfMarkupDependencyError(RuntimeError):
    """PyMuPDF / fitz is unavailable for baking."""


def _clamp_unit(value: Any) -> float | None:
    """Coerce ``value`` to a float in ``[0, 1]`` (clamped), or ``None`` if not numeric."""
    if isinstance(value, bool):  # bool is an int subclass; reject it explicitly.
        return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        return None
    if number < 0.0:
        return 0.0
    if number > 1.0:
        return 1.0
    return number


def normalize_rect(value: Any) -> dict[str, float] | None:
    """Validate + clamp a normalized rect; return a clean ``{x,y,w,h}`` or ``None``.

    ``None`` means the rect is malformed (missing keys or non-numeric values) and
    the annotation should be rejected (route) or skipped (bake).
    """
    if not isinstance(value, Mapping):
        return None
    cleaned: dict[str, float] = {}
    for key in ("x", "y", "w", "h"):
        component = _clamp_unit(value.get(key))
        if component is None:
            return None
        cleaned[key] = component
    return cleaned


def normalize_text(value: Any) -> str:
    """Coerce an optional annotation body to a bounded string."""
    if value is None:
        return ""
    text = str(value)
    return text[:MAX_ANNOTATION_TEXT_CHARS]


def normalize_color(value: Any) -> str:
    """Accept a ``#rrggbb`` / ``#rgb`` hex color, else the empty string."""
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if not candidate.startswith("#"):
        return ""
    digits = candidate[1:]
    if len(digits) not in (3, 6):
        return ""
    if not all(character in "0123456789abcdefABCDEF" for character in digits):
        return ""
    return "#" + digits.lower()


def normalize_annotation_input(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a POST body into the stored annotation shape (sans server fields).

    Raises ``PdfMarkupError`` with a human-readable message on any violation. The
    returned dict has ``page``, ``type``, ``rect``, ``text`` and (optional)
    ``color`` — the route layers on ``id``, ``author`` and ``created_at``.
    """
    if not isinstance(payload, Mapping):
        raise PdfMarkupError("Annotation payload must be a JSON object.")

    annotation_type = str(payload.get("type") or "").strip().lower()
    if annotation_type not in ANNOTATION_TYPES:
        raise PdfMarkupError(
            "Annotation type must be one of: " + ", ".join(ANNOTATION_TYPES) + "."
        )

    raw_page = payload.get("page")
    if isinstance(raw_page, bool) or not isinstance(raw_page, int):
        raise PdfMarkupError("Annotation page must be a positive integer.")
    if raw_page < 1:
        raise PdfMarkupError("Annotation page must be a positive integer.")

    rect = normalize_rect(payload.get("rect"))
    if rect is None:
        raise PdfMarkupError("Annotation rect must have numeric x, y, w, h in [0, 1].")

    annotation: dict[str, Any] = {
        "page": raw_page,
        "type": annotation_type,
        "rect": rect,
        "text": normalize_text(payload.get("text")),
    }
    color = normalize_color(payload.get("color"))
    if color:
        annotation["color"] = color
    return annotation


def _hex_to_rgb(color: str) -> tuple[float, float, float] | None:
    """Convert a ``#rrggbb`` / ``#rgb`` hex string to a PyMuPDF 0..1 RGB tuple."""
    digits = color.lstrip("#")
    if len(digits) == 3:
        digits = "".join(character * 2 for character in digits)
    if len(digits) != 6:
        return None
    try:
        red = int(digits[0:2], 16) / 255.0
        green = int(digits[2:4], 16) / 255.0
        blue = int(digits[4:6], 16) / 255.0
    except ValueError:
        return None
    return red, green, blue


def bake_user_annotations(source_pdf: bytes, annotations: list) -> bytes:
    """Stamp user-placed annotations onto ``source_pdf`` and return the new bytes.

    Maps each annotation's normalized rect to PDF points against the *target*
    page's own ``page.rect`` (top-left origin on both sides — see module docstring)
    and adds the matching PyMuPDF annotation:

    * ``comment``       -> ``page.add_text_annot(point, text)`` carrying the body.
    * ``highlight``     -> ``page.add_highlight_annot(rect)`` (+ optional color).
    * ``strikethrough`` -> ``page.add_strikeout_annot(rect)`` (+ optional color).

    Annotations whose page is out of range, whose rect is malformed, or whose type
    is unknown are SKIPPED — a single bad annotation never aborts the export.
    """
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover - exercised only without the pdf extra.
        raise PdfMarkupDependencyError("PDF markup baking requires PyMuPDF/fitz.") from exc

    try:
        document = fitz.open(stream=source_pdf, filetype="pdf")
    except Exception as exc:
        raise PdfMarkupError("The source PDF could not be opened for markup.") from exc

    try:
        for raw in annotations or []:
            if not isinstance(raw, Mapping):
                continue
            annotation_type = str(raw.get("type") or "").strip().lower()
            if annotation_type not in ANNOTATION_TYPES:
                continue

            try:
                page_number = int(raw.get("page"))
            except (TypeError, ValueError):
                continue
            page_index = page_number - 1
            if page_index < 0 or page_index >= document.page_count:
                continue

            rect = normalize_rect(raw.get("rect"))
            if rect is None:
                continue

            page = document[page_index]
            width = page.rect.width
            height = page.rect.height
            px0 = rect["x"] * width
            py0 = rect["y"] * height
            px1 = (rect["x"] + rect["w"]) * width
            py1 = (rect["y"] + rect["h"]) * height
            text = normalize_text(raw.get("text"))
            color = _hex_to_rgb(str(raw.get("color") or "")) if raw.get("color") else None

            if annotation_type == "comment":
                annot = page.add_text_annot(fitz.Point(px0, py0), text)
                annot.set_info(content=text)
                annot.update()
                continue

            box = fitz.Rect(px0, py0, px1, py1)
            if box.is_empty or box.is_infinite:
                # A zero-area box can't carry a highlight/strikeout; skip it
                # rather than emitting a degenerate annotation.
                continue
            if annotation_type == "highlight":
                annot = page.add_highlight_annot(box)
            else:  # strikethrough
                annot = page.add_strikeout_annot(box)
            if color is not None:
                annot.set_colors(stroke=color)
            if text:
                annot.set_info(content=text)
            annot.update()

        output = document.write(garbage=4, deflate=True)
    finally:
        document.close()
    return bytes(output)
