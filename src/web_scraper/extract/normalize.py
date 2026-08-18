"""Value normalization applied before schema validation and quorum comparison.

Without this, a validator rejects correct data and two extractors "disagree"
over pure formatting ("1 234,50 ₽" vs "1234.5").
"""

from __future__ import annotations

import html
import re
import unicodedata
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin

_WS_RE = re.compile(r"\s+")


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


def normalize_date(value: str) -> str:
    """Normalize a date to ISO-8601 with an explicit timezone where possible.

    Two extractors reporting the same instant in different notations
    (``2026-08-12T09:30:00Z`` vs ``Wed, 12 Aug 2026 09:30:00 GMT``) must compare
    equal, or the quorum reports a false conflict. A value that parses as
    neither ISO nor RFC 2822 is returned as cleaned text for the validator to
    judge — it is never guessed at.
    """

    text = normalize_text(value)
    if not text:
        return text
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    for parse in (datetime.fromisoformat, parsedate_to_datetime):
        try:
            parsed = parse(candidate)
        except (ValueError, TypeError):
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()
    return text


def normalize_value(value: Any, *, kind: str = "text", base_url: str | None = None) -> Any:
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
        return normalize_date(value)
    return normalize_text(value)
