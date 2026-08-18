from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / ".agents/skills/web-scraper/scripts"
sys.path.insert(0, str(SCRIPTS))

from triage import ContentRules, Verdict, classify_response  # noqa: E402


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
            status=200, body='{"ok":true}', headers={"Content-Type": "application/json"},
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


if __name__ == "__main__":
    unittest.main()
