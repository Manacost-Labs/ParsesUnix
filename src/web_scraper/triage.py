"""Canonical response triage: the single source of retry/escalation decisions."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from web_scraper.contracts import ContentRules, TriageResult, Verdict

BLOCK_SIGNATURES = (
    # Interstitial/denial text that only a real block or challenge page carries.
    "just a moment",
    "attention required! | cloudflare",
    "sorry, you have been blocked",
    "enable javascript and cookies to continue",
    "cf-chl-",
    "cf_chl_",
    "cloudflare ray id",
    "verify you are human",
    "checking your browser",
    # Specific anti-bot vendor challenge markers.
    #
    # Two markers are deliberately NOT here, both verified against live pages:
    #   * a bare "captcha" — matches legitimate markup such as a WordPress
    #     theme's `tds_captcha` JS variable;
    #   * "/cdn-cgi/challenge-platform" — Cloudflare ships this JS-detection
    #     bundle on ORDINARY pages too (hsguru.com serves ~19k chars of real
    #     content alongside it), so it proves bot management is enabled, not
    #     that this response was blocked.
    # A signature must appear only when the response really is a block.
    "g-recaptcha-response",
    "hcaptcha.com/captcha",
    "px-captcha",
    "captcha-delivery.com",
    "perimeterx",
    "datadome",
)

ACCESS_DENIED_SIGNATURES = (
    "access denied",
    "permission denied",
    "login required",
    "sign in to continue",
)


#: An empty application root is the clearest CSR marker, but never the only one.
_APP_ROOT_RE = re.compile(
    r"<(?:div|main|section|app-root)\b[^>]*\bid=[\"'](?:root|app|__next|__nuxt|application)[\"'][^>]*>\s*"
    r"</(?:div|main|section|app-root)\s*>",
    re.IGNORECASE,
)
_SCRIPT_SRC_RE = re.compile(rb"<script\b[^>]*\bsrc=", re.IGNORECASE)
_HYDRATION_RE = re.compile(
    rb"__NEXT_DATA__|__NUXT__|__INITIAL_STATE__|window\.__PRELOADED_STATE__|data-reactroot",
    re.IGNORECASE,
)
_TAG_RE = re.compile(rb"<[^>]+>")
_SCRIPT_BLOCK_RE = re.compile(
    rb"<(script|style|noscript|template)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)

#: Below this much visible text a page is not carrying an article, a listing or a
#: product — whatever it is, it is not the content we came for.
CSR_MAX_VISIBLE_TEXT = 400


def _strip_script_blocks(body: bytes) -> bytes:
    """The document with script/style contents removed, markup otherwise intact."""

    return _SCRIPT_BLOCK_RE.sub(b" ", body)


def _visible_text_length(body: bytes) -> int:
    stripped = _TAG_RE.sub(b" ", _strip_script_blocks(body))
    return len(b" ".join(stripped.split()))


def looks_like_csr_shell(body: bytes, content_type: str) -> bool:
    """Is this markup a client-rendered shell rather than the page itself?

    Deliberately a combination of signals. A single marker is not enough: plenty
    of server-rendered pages contain an element called ``app``, and plenty of
    thin pages are simply thin. The shell is recognised by markup that ships a
    mounting point and a script bundle while carrying almost no readable text.
    """

    if "html" not in content_type.lower():
        return False
    if _visible_text_length(body) >= CSR_MAX_VISIBLE_TEXT:
        return False  # there is real content here, whatever else is going on

    text = body.decode("utf-8", errors="ignore")
    has_empty_root = bool(_APP_ROOT_RE.search(text))
    external_scripts = len(_SCRIPT_SRC_RE.findall(body))
    has_hydration = bool(_HYDRATION_RE.search(body))

    # Script bytes vs readable bytes: a document that is mostly script and almost
    # no text is rendering client-side even without a framework mount point.
    script_bytes = len(body) - len(_strip_script_blocks(body))
    script_dominated = script_bytes > 4 * max(1, _visible_text_length(body))

    return (
        has_empty_root
        or (external_scripts >= 2 and has_hydration)
        or (script_dominated and (external_scripts >= 1 or script_bytes > 500))
    )


def _headers_lower(headers: Mapping[str, str] | None) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in (headers or {}).items()}


def _body_bytes(body: bytes | str | None) -> bytes:
    if body is None:
        return b""
    return body if isinstance(body, bytes) else body.encode("utf-8", errors="replace")


def _find_block_signature(
    body: bytes,
    headers: Mapping[str, str],
    extra_signatures: Sequence[str] = (),
) -> str | None:
    if "challenge" in headers.get("cf-mitigated", "").lower():
        return "cf-mitigated: challenge"
    sample = body[:1_000_000].decode("utf-8", errors="ignore").lower()
    signatures = tuple(BLOCK_SIGNATURES) + tuple(s.lower() for s in extra_signatures)
    return next((signature for signature in signatures if signature in sample), None)


def _find_access_denied_signature(body: bytes) -> str | None:
    sample = body[:1_000_000].decode("utf-8", errors="ignore").lower()
    return next((signature for signature in ACCESS_DENIED_SIGNATURES if signature in sample), None)


def _json_path_exists(value: Any, path: str) -> bool:
    current = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    return current is not None


def _validate_json(body: bytes, required_paths: Sequence[str]) -> str | None:
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return f"invalid JSON: {exc}"
    missing = [path for path in required_paths if not _json_path_exists(value, path)]
    if missing:
        return "missing required JSON paths: " + ", ".join(missing)
    return None


def classify_response(
    *,
    status: int | None,
    body: bytes | str | None = None,
    headers: Mapping[str, str] | None = None,
    rules: ContentRules | None = None,
    source: str = "target",
    transport_error: str | None = None,
) -> TriageResult:
    """Classify a target or provider response without deciding retry timing."""

    if source not in {"target", "provider"}:
        raise ValueError("source must be 'target' or 'provider'")

    payload = _body_bytes(body)
    normalized_headers = _headers_lower(headers)
    selected_rules = rules or ContentRules()
    signature = _find_block_signature(payload, normalized_headers, selected_rules.stop_signatures)
    access_signature = _find_access_denied_signature(payload)

    if transport_error:
        verdict = Verdict.PROVIDER_ERROR if source == "provider" else Verdict.ORIGIN_DOWN
        return TriageResult(verdict, f"transport error: {transport_error}", status, len(payload))

    if status is None:
        verdict = Verdict.PROVIDER_ERROR if source == "provider" else Verdict.ORIGIN_DOWN
        return TriageResult(verdict, "no HTTP status", None, len(payload))

    # A provider (L3/L4) failure must never be read with target semantics: a
    # provider-side 404/403/400 means "the provider could not serve the job",
    # not "the target URL is dead / access-controlled".
    if source == "provider" and (
        status in {400, 401, 403, 404, 407, 429, 502, 510} or status >= 500
    ):
        return TriageResult(
            Verdict.PROVIDER_ERROR,
            f"provider returned HTTP {status}",
            status,
            len(payload),
            signature,
        )

    # 407 is a proxy problem (our proxy, not the target): treat it as a provider
    # error regardless of source, never as target authorization.
    if status == 407:
        return TriageResult(
            Verdict.PROVIDER_ERROR,
            "proxy authentication required (HTTP 407)",
            status,
            len(payload),
        )

    if status == 304:
        return TriageResult(
            Verdict.NOT_MODIFIED,
            "conditional request: content unchanged (HTTP 304)",
            status,
            len(payload),
        )

    if status in {404, 410}:
        return TriageResult(
            Verdict.DEAD_URL, f"target returned HTTP {status}", status, len(payload)
        )

    if status in {401, 402}:
        return TriageResult(
            Verdict.AUTH_REQUIRED,
            f"target requires authorized access (HTTP {status})",
            status,
            len(payload),
        )

    if status == 429:
        retry_after = normalized_headers.get("retry-after")
        suffix = f"; Retry-After={retry_after}" if retry_after else ""
        return TriageResult(
            Verdict.RATE_LIMITED, f"target rate limited{suffix}", status, len(payload)
        )

    if status == 403:
        if signature:
            return TriageResult(
                Verdict.BLOCKED,
                "target returned a blocking signature",
                status,
                len(payload),
                signature,
            )
        if access_signature:
            return TriageResult(
                Verdict.ACCESS_DENIED,
                "target returned an access-control message",
                status,
                len(payload),
                access_signature,
            )
        # A bare 403 with no access-control message is almost always silent bot
        # mitigation (Cloudflare/Akamai/F5 return terse 403s). Classify it as
        # BLOCKED so a free browser retry (L2) is allowed, instead of a terminal
        # ACCESS_DENIED that kills the URL. A genuine "log in" gives 401 or an
        # access-control message, both handled above.
        return TriageResult(
            Verdict.BLOCKED,
            "HTTP 403 without an access-control message (treated as silent bot mitigation)",
            status,
            len(payload),
        )

    if status >= 500:
        if signature:
            return TriageResult(
                Verdict.BLOCKED,
                "server error contains a blocking signature",
                status,
                len(payload),
                signature,
            )
        return TriageResult(
            Verdict.ORIGIN_DOWN, f"target returned HTTP {status}", status, len(payload)
        )

    if 200 <= status < 300:
        if signature:
            return TriageResult(
                Verdict.SOFT_BLOCK,
                "successful HTTP status contains a blocking signature",
                status,
                len(payload),
                signature,
            )
        if access_signature:
            return TriageResult(
                Verdict.ACCESS_DENIED,
                "successful HTTP status contains an access-control message",
                status,
                len(payload),
                access_signature,
            )
        if len(payload) < selected_rules.min_body_bytes:
            # A small 2xx body (204, {"ok":true}, a thin listing) is a
            # data-quality signal, not proof of a block. THIN_CONTENT never
            # authorizes paid escalation the way SOFT_BLOCK would.
            return TriageResult(
                Verdict.THIN_CONTENT,
                f"body is smaller than {selected_rules.min_body_bytes} bytes",
                status,
                len(payload),
            )

        content_type = normalized_headers.get("content-type", "").lower()
        expected = (selected_rules.expected_content_type or "").lower()
        if expected and expected not in content_type:
            return TriageResult(
                Verdict.PARSE_FAIL,
                f"expected content type containing {expected!r}, got {content_type!r}",
                status,
                len(payload),
            )

        needs_json = bool(selected_rules.required_json_paths) or "json" in expected
        if needs_json:
            json_error = _validate_json(payload, selected_rules.required_json_paths)
            if json_error:
                return TriageResult(Verdict.PARSE_FAIL, json_error, status, len(payload))

        if selected_rules.all_canaries:
            # Match outside <script>/<style>. A canary found only inside a JS
            # data blob is not proof the page rendered: quotes.toscrape.com/js
            # ships every quote in a script variable and 98 characters of visible
            # text, and matching raw HTML reported that as OK. Tags are kept, so
            # markup canaries like "<article" still work.
            text = _strip_script_blocks(payload).decode("utf-8", errors="ignore")
            missing = [canary for canary in selected_rules.all_canaries if canary not in text]
            if missing:
                # Distinguish "the page changed" from "the page has not rendered
                # yet". A client-rendered shell is not a broken profile: the
                # markup arrived and the data is fetched by script, so the answer
                # is a browser, not an extractor fix. Reporting PARSE_FAIL here
                # would strand every JavaScript site, because PARSE_FAIL never
                # unlocks L2.
                if looks_like_csr_shell(payload, content_type):
                    return TriageResult(
                        Verdict.CSR_REQUIRED,
                        "client-rendered shell: markup arrived but the content is "
                        f"script-loaded (canary {missing[0]!r} absent)",
                        status,
                        len(payload),
                    )
                return TriageResult(
                    Verdict.PARSE_FAIL,
                    f"canary {missing[0]!r} was not found",
                    status,
                    len(payload),
                )
        return TriageResult(
            Verdict.OK, "status and content validation passed", status, len(payload)
        )

    if 300 <= status < 400:
        return TriageResult(
            Verdict.PARSE_FAIL,
            f"unresolved redirect HTTP {status}",
            status,
            len(payload),
        )

    return TriageResult(Verdict.PARSE_FAIL, f"unclassified HTTP {status}", status, len(payload))


def _parse_headers(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("headers JSON must be an object")
    return {str(key): str(item) for key, item in value.items()}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", type=int)
    parser.add_argument("--body-file", type=Path)
    parser.add_argument("--headers-json")
    parser.add_argument("--source", choices=("target", "provider"), default="target")
    parser.add_argument("--transport-error")
    parser.add_argument("--min-body-bytes", type=int, default=200)
    parser.add_argument("--canary")
    parser.add_argument("--expected-content-type")
    parser.add_argument("--required-json-path", action="append", default=[])
    parser.add_argument("--stop-signature", action="append", default=[])
    args = parser.parse_args(argv)

    body = args.body_file.read_bytes() if args.body_file else b""
    result = classify_response(
        status=args.status,
        body=body,
        headers=_parse_headers(args.headers_json),
        source=args.source,
        transport_error=args.transport_error,
        rules=ContentRules(
            min_body_bytes=args.min_body_bytes,
            canary=args.canary,
            expected_content_type=args.expected_content_type,
            required_json_paths=tuple(args.required_json_path),
            stop_signatures=tuple(args.stop_signature),
        ),
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
