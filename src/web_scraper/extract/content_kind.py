"""What a response is, decided by looking at it rather than by trusting it.

``Content-Type`` is a claim, and a surprising share of the web gets it wrong:
APIs that answer JSON as ``text/plain``, error pages that answer HTML under a
JSON content type, servers that send ``application/octet-stream`` for everything.

Getting this wrong is not loud. An HTML extractor pointed at a JSON body finds
no elements and returns an empty result; a JSON parser pointed at HTML raises
and gets caught. Either way the run completes and the field is simply missing,
which is exactly the silent-corruption failure this project exists to avoid.

So the header is the first signal, not the only one, and the body gets a bounded
look. "Bounded" matters: a 200 MB video must not be decoded to find out it is a
video.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from web_scraper.contracts import ContentKind

#: How much of the body to inspect. Enough to see a doctype, a BOM, or the
#: opening of a JSON document; small enough that a huge binary costs nothing.
SNIFF_BYTES = 2048

#: Full JSON validation is attempted only up to this size. Beyond it the opening
#: character plus the content type has to be enough — parsing 50 MB to classify
#: it would cost more than the extraction it precedes.
MAX_JSON_VALIDATION_BYTES = 1_000_000

_JSON_TYPES = (
    "application/json",
    "application/ld+json",
    "application/graphql-response+json",
    "text/json",
)
_HTML_TYPES = ("text/html", "application/xhtml+xml")
_BINARY_PREFIXES = ("image/", "video/", "audio/", "font/")
_BINARY_TYPES = (
    "application/octet-stream",
    "application/pdf",
    "application/zip",
    "application/gzip",
    "application/x-tar",
)

#: Magic numbers for formats that lie about their content type often enough to
#: be worth recognising by shape.
_MAGIC: tuple[tuple[bytes, ContentKind], ...] = (
    (b"\x89PNG\r\n\x1a\n", ContentKind.BINARY),
    (b"\xff\xd8\xff", ContentKind.BINARY),  # JPEG
    (b"GIF87a", ContentKind.BINARY),
    (b"GIF89a", ContentKind.BINARY),
    (b"%PDF-", ContentKind.BINARY),
    (b"PK\x03\x04", ContentKind.BINARY),  # zip and everything built on it
    (b"\x1f\x8b", ContentKind.BINARY),  # gzip
    (b"RIFF", ContentKind.BINARY),
    (b"\x00\x00\x00", ContentKind.BINARY),  # common in media containers
)

_HTML_MARKERS = (b"<!doctype html", b"<html", b"<head", b"<body", b"<!--")


def detect_content_kind(body: bytes, headers: Mapping[str, str] | None = None) -> ContentKind:
    """Classify a response body.

    The order is deliberate: obvious binary shapes first (so nothing large is
    decoded), then the body's own opening, then the declared type. The body
    outranks the header because the body cannot be wrong about itself.
    """

    if not body:
        # An empty body has no kind. Reporting HTML here because the header said
        # so would send an extractor looking for elements in nothing.
        return ContentKind.UNKNOWN

    declared = _declared_type(headers)
    head = body[:SNIFF_BYTES]

    # 1. Binary by shape. Checked before any decode attempt.
    for magic, kind in _MAGIC:
        if head.startswith(magic):
            return kind
    if _looks_binary(head):
        return ContentKind.BINARY

    # 2. Binary by declaration, once we know it is not text-shaped.
    if declared.startswith(_BINARY_PREFIXES) or declared in _BINARY_TYPES:
        return ContentKind.BINARY

    stripped = _strip_leading(head)

    # 3. The body's own opening. A document that begins with a doctype is HTML
    #    whatever the header claims, and vice versa.
    if _starts_html(stripped):
        return ContentKind.HTML
    if stripped[:1] in (b"{", b"["):
        if _is_valid_json(body):
            return ContentKind.JSON
        # Opens like JSON and is not JSON. Falling through to the declared type
        # rather than guessing: a truncated JSON body is not TEXT, and calling
        # it JSON would hand a parser something it cannot read.
        if any(declared.startswith(t) for t in _JSON_TYPES):
            return ContentKind.JSON

    # 4. The declared type, now that the body has had its say.
    if any(declared.startswith(t) for t in _JSON_TYPES):
        return ContentKind.JSON if _is_valid_json(body) else ContentKind.TEXT
    if declared.startswith(_HTML_TYPES):
        return ContentKind.HTML
    if declared.startswith("text/"):
        return ContentKind.TEXT

    # 5. Nothing declared and nothing recognisable. TEXT only if it decodes.
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return ContentKind.BINARY
    return ContentKind.TEXT


def _declared_type(headers: Mapping[str, str] | None) -> str:
    if not headers:
        return ""
    for name, value in headers.items():
        if name.lower() == "content-type":
            return value.split(";")[0].strip().lower()
    return ""


def _strip_leading(head: bytes) -> bytes:
    """Drop a BOM and leading whitespace, both of which hide the real opening."""

    for bom in (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff"):
        if head.startswith(bom):
            head = head[len(bom) :]
            break
    return head.lstrip()


def _starts_html(stripped: bytes) -> bool:
    lowered = stripped[:512].lower()
    return any(lowered.startswith(marker) for marker in _HTML_MARKERS)


def _looks_binary(head: bytes) -> bool:
    """A NUL byte in the first block means this is not text.

    Deliberately narrow. Counting "unusual" bytes misfires on UTF-8 text in
    scripts that use the high range heavily, and misclassifying a page of
    Japanese as binary would skip its extraction entirely.
    """

    return b"\x00" in head


def _is_valid_json(body: bytes) -> bool:
    if len(body) > MAX_JSON_VALIDATION_BYTES:
        return True  # too big to verify; the opening character has to serve
    # The BOM must come off here too. Stripping it only for the shape check and
    # then handing the raw bytes to the parser makes every BOM-prefixed JSON
    # document look invalid.
    try:
        json.loads(_strip_leading(body).decode("utf-8", errors="strict"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return True
