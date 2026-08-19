from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / ".agents/skills/web-scraper/scripts"
sys.path.insert(0, str(SCRIPTS))

from triage import ContentRules, Verdict, classify_response


class TriageTests(unittest.TestCase):
    def test_validated_200_is_ok(self) -> None:
        result = classify_response(
            status=200,
            body="x" * 300 + "<article>",
            headers={"Content-Type": "text/html; charset=utf-8"},
            rules=ContentRules(min_body_bytes=200, canary="<article", expected_content_type="html"),
        )
        self.assertEqual(result.verdict, Verdict.OK)

    def test_challenge_with_200_is_soft_block(self) -> None:
        result = classify_response(status=200, body="<title>Just a moment...</title>")
        self.assertEqual(result.verdict, Verdict.SOFT_BLOCK)
        self.assertTrue(result.paid_escalation_allowed)

    def test_dead_url_never_escalates(self) -> None:
        result = classify_response(status=404, body="not found")
        self.assertEqual(result.verdict, Verdict.DEAD_URL)
        self.assertFalse(result.paid_escalation_allowed)

    def test_target_rate_limit_is_not_provider_error(self) -> None:
        result = classify_response(status=429, headers={"Retry-After": "30"})
        self.assertEqual(result.verdict, Verdict.RATE_LIMITED)
        self.assertIn("30", result.reason)

    def test_provider_failure_is_separate(self) -> None:
        result = classify_response(status=502, source="provider")
        self.assertEqual(result.verdict, Verdict.PROVIDER_ERROR)

    def test_missing_json_path_is_parse_fail(self) -> None:
        result = classify_response(
            status=200,
            body='{"data": {"title": "ok"}}' + " " * 200,
            headers={"Content-Type": "application/json"},
            rules=ContentRules(
                min_body_bytes=1,
                expected_content_type="json",
                required_json_paths=("data.title", "data.published_at"),
            ),
        )
        self.assertEqual(result.verdict, Verdict.PARSE_FAIL)

    def test_bare_403_is_blocked_so_a_browser_retry_is_allowed(self) -> None:
        # A terse 403 with no access-control message is silent bot mitigation;
        # it must not terminally kill the URL.
        result = classify_response(status=403, body="Forbidden")
        self.assertEqual(result.verdict, Verdict.BLOCKED)

    def test_403_with_access_message_is_terminal_access_denied(self) -> None:
        result = classify_response(status=403, body="Login required to continue")
        self.assertEqual(result.verdict, Verdict.ACCESS_DENIED)
        self.assertFalse(result.paid_escalation_allowed)

    def test_access_denied_with_200_does_not_escalate(self) -> None:
        result = classify_response(status=200, body="Access denied" + " " * 300)
        self.assertEqual(result.verdict, Verdict.ACCESS_DENIED)
        self.assertFalse(result.paid_escalation_allowed)

    def test_small_2xx_is_thin_content_not_paid_escalation(self) -> None:
        result = classify_response(
            status=200,
            body='{"ok":true}',
            headers={"Content-Type": "application/json"},
            rules=ContentRules(min_body_bytes=500),
        )
        self.assertEqual(result.verdict, Verdict.THIN_CONTENT)
        self.assertFalse(result.paid_escalation_allowed)

    def test_provider_404_is_provider_error_not_dead_url(self) -> None:
        result = classify_response(status=404, source="provider")
        self.assertEqual(result.verdict, Verdict.PROVIDER_ERROR)

    def test_407_is_provider_error_not_auth_required(self) -> None:
        result = classify_response(status=407)
        self.assertEqual(result.verdict, Verdict.PROVIDER_ERROR)

    def test_captcha_substring_in_markup_is_not_a_block(self) -> None:
        # A theme JS var like tds_captcha="" must not read as an anti-bot block.
        body = "x" * 400 + '<script>var tds_captcha="";</script>'
        result = classify_response(
            status=200,
            body=body,
            headers={"Content-Type": "text/html"},
            rules=ContentRules(min_body_bytes=200),
        )
        self.assertNotEqual(result.verdict, Verdict.SOFT_BLOCK)

    def test_specific_captcha_vendor_marker_is_a_block(self) -> None:
        body = 'blocked <script src="https://ct.captcha-delivery.com/c.js"></script>' + " " * 400
        result = classify_response(status=200, body=body, headers={"Content-Type": "text/html"})
        self.assertEqual(result.verdict, Verdict.SOFT_BLOCK)


if __name__ == "__main__":
    unittest.main()


class CsrShellTests(unittest.TestCase):
    """A client-rendered shell needs a browser, not an extractor fix."""

    RULES = ContentRules(min_body_bytes=100, canary="<article")
    HTML = {"Content-Type": "text/html"}

    SHELL = (
        b"<!DOCTYPE html><html><head><title>App</title>"
        b'<script src="/static/main.js"></script><script src="/static/vendor.js"></script>'
        b'</head><body><div id="root"></div>'
        b"<noscript>You need to enable JavaScript to run this app.</noscript></body></html>"
    )

    def classify(self, body: bytes, rules: ContentRules | None = None):
        return classify_response(
            status=200, body=body, headers=self.HTML, rules=rules or self.RULES
        )

    def test_an_empty_app_root_is_csr_not_a_parse_failure(self) -> None:
        result = self.classify(self.SHELL)
        self.assertEqual(result.verdict, Verdict.CSR_REQUIRED)

    def test_csr_unlocks_the_browser_but_never_authorizes_payment(self) -> None:
        from web_scraper.contracts import FREE_ESCALATION_VERDICTS

        result = self.classify(self.SHELL)
        self.assertIn(result.verdict, FREE_ESCALATION_VERDICTS)
        self.assertFalse(result.paid_escalation_allowed)

    def test_a_redesigned_server_rendered_page_is_still_a_parse_failure(self) -> None:
        # Real content, different markup: the profile is wrong, not the renderer.
        redesigned = (
            b'<html><body><div class="story"><h1>Title</h1><p>'
            + b"word " * 200
            + b"</p></div></body></html>"
        )
        self.assertEqual(self.classify(redesigned).verdict, Verdict.PARSE_FAIL)

    def test_a_mount_point_carrying_real_text_is_not_a_shell(self) -> None:
        # Plenty of server-rendered pages wrap content in <div id="app">.
        served = (
            b'<html><body><div id="app"><h1>Title</h1><p>'
            + b"word " * 200
            + b"</p></div></body></html>"
        )
        self.assertEqual(self.classify(served).verdict, Verdict.PARSE_FAIL)

    def test_a_script_only_document_with_hydration_markers_is_csr(self) -> None:
        hydrated = (
            b'<html><head><script src="/a.js"></script><script src="/b.js"></script></head>'
            b"<body><script>window.__NEXT_DATA__={}</script></body></html>"
        )
        self.assertEqual(self.classify(hydrated).verdict, Verdict.CSR_REQUIRED)

    def test_a_challenge_page_is_still_a_block_not_csr(self) -> None:
        # Block detection runs first: a challenge that happens to be script-heavy
        # must not be mistaken for an unrendered application.
        challenge = (
            b'<html><title>Just a moment...</title><body><div id="root"></div></body></html>'
        )
        self.assertEqual(self.classify(challenge).verdict, Verdict.SOFT_BLOCK)

    def test_json_responses_are_never_classified_as_shells(self) -> None:
        result = classify_response(
            status=200,
            body=b'{"items": []}',
            headers={"Content-Type": "application/json"},
            rules=ContentRules(min_body_bytes=2, canary="items"),
        )
        self.assertEqual(result.verdict, Verdict.OK)


class CanaryScopeTests(unittest.TestCase):
    """A canary hiding in a script tag is not evidence that the page rendered."""

    RULES = ContentRules(min_body_bytes=100, canary="Einstein")
    HTML = {"Content-Type": "text/html"}

    def test_a_canary_only_inside_a_script_does_not_pass(self) -> None:
        # The shape of quotes.toscrape.com/js: every quote lives in a JS array and
        # the document itself carries almost no text. Matching raw HTML reported
        # this as OK, which is silent corruption - extraction would find nothing.
        body = (
            b"<html><body><div class='quotes'></div>"
            b"<script>var data=[{author:'Einstein',text:'"
            + b"x" * 800
            + b"'}];render(data);</script></body></html>"
        )
        result = classify_response(status=200, body=body, headers=self.HTML, rules=self.RULES)
        self.assertEqual(result.verdict, Verdict.CSR_REQUIRED)

    def test_the_same_content_rendered_server_side_passes(self) -> None:
        body = (
            b"<html><body><div class='quote'><span>Einstein</span><p>"
            + b"word " * 200
            + b"</p></div></body></html>"
        )
        result = classify_response(status=200, body=body, headers=self.HTML, rules=self.RULES)
        self.assertEqual(result.verdict, Verdict.OK)

    def test_a_markup_canary_still_works(self) -> None:
        # Script contents are removed but tags are kept, so "<article" matches.
        body = b"<html><body><article><p>" + b"word " * 200 + b"</p></article></body></html>"
        result = classify_response(
            status=200,
            body=body,
            headers=self.HTML,
            rules=ContentRules(min_body_bytes=100, canary="<article"),
        )
        self.assertEqual(result.verdict, Verdict.OK)
