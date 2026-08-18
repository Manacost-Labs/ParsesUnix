"""Value normalization applied before schema validation and quorum comparison.

Without this, a validator rejects correct data and two extractors "disagree"
over pure formatting ("1 234,50 ₽" vs "1234.5").
"""

from __future__ import annotations

import html
import re
import unicodedata
from urllib.parse import urljoin

_WS_RE = re.compile(r"\s+")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?")


def normalize_text(value: str) -> str:
    text = html.unescape(value)
    text = text.replace(" ", " ")  # nbsp
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C" or ch in "\t\n")
    return _WS_RE.sub(" ", text).strip()


def normalize_number(value: str) -> float | None:
    """Parse a human-formatted number: strips currency, spaces, thousands seps."""

    cleaned = re.sub(r"[^\d,.\-]", "", value.strip())
    if not cleaned:
        return None
    # Decide the decimal separator by which appears last.
    last_comma = cleaned.rfind(",")
    last_dot = cleaned.rfind(".")
    if last_comma > last_dot:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def normalize_url_value(value: str, base_url: str | None) -> str:
    text = value.strip()
    return urljoin(base_url, text) if base_url else text


def normalize_value(value, *, kind: str = "text", base_url: str | None = None):
    """Normalize by declared field kind: text | number | url | date | raw."""

    if value is None:
        return None
    if kind == "raw":
        return value
    if not isinstance(value, str):
        # numbers/bools pass through; containers are returned as-is
        return value
    if kind == "number":
        return normalize_number(value)
    if kind == "url":
        return normalize_url_value(value, base_url)
    if kind == "date":
        normalized = normalize_text(value)
        match = _ISO_DATE_RE.match(normalized)
        return normalized if match else normalized  # ISO kept; free-form left for the validator
    return normalize_text(value)
