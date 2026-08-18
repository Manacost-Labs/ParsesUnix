"""Live browser tests against a local server (skipped when Playwright is absent).

These cover the L2 code that unit tests with fake transports cannot reach: real
Chromium navigation, the SSRF route guard, XHR JSON capture, and candidate
extraction. The fixture site is served from localhost, so the tests are hermetic
(no external network) and safe to run in CI.

Because the target is loopback, these tests pass ``allow_private=True`` — that
flag exists exactly for authorized internal targets and is never the default.
"""

from __future__ import annotations

import http.server
import json
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.fetchers.base import TransportUnavailable
from web_scraper.fetchers.transports import (
    PlaywrightRenderTransport,
    playwright_available,
)
from web_scraper.probe.browser import browser_recon
from web_scraper.probe.safety import UnsafeTarget

SPA_HTML = """<!DOCTYPE html>
<html><head><title>Catalog</title></head>
<body>
<div id="root"></div>
<script>
fetch('/api/items').then(r => r.json()).then(data => {
  document.getElementById('root').textContent = data.items[0].title;
});
</script>
</body></html>
"""

API_PAYLOAD = {
    "items": [
        {"id": 1, "title": "Rendered Title", "price": 42, "publishedAt": "2026-08-18T10:00:00Z"}
    ]
}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/items"):
            body = json.dumps(API_PAYLOAD).encode()
            content_type = "application/json"
        else:
            body = SPA_HTML.encode()
            content_type = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence the test server
        pass


@unittest.skipUnless(playwright_available(), "Playwright is not installed")
class BrowserLiveTests(unittest.TestCase):
    server: http.server.HTTPServer
    base: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_render_transport_returns_js_rendered_dom(self) -> None:
        transport = PlaywrightRenderTransport(allow_private=True, timeout=30)
        response = transport.fetch(f"{self.base}/catalog")
        self.assertEqual(response.status, 200)
        # The text only exists after the page's fetch() resolved, so this proves
        # we return the rendered DOM rather than the served HTML.
        self.assertIn(b"Rendered Title", response.body)

    def test_render_transport_refuses_private_target_by_default(self) -> None:
        transport = PlaywrightRenderTransport(timeout=15)  # allow_private=False
        with self.assertRaises(UnsafeTarget):
            transport.fetch(f"{self.base}/catalog")

    def test_recon_finds_the_xhr_api_and_ranks_it(self) -> None:
        report = browser_recon(
            f"{self.base}/catalog",
            target_fields=["title", "price", "published_at"],
            force=True,
            allow_private=True,
            timeout_s=30,
        )
        self.assertTrue(report.executed)
        self.assertTrue(report.conclusive)
        self.assertEqual(report.navigation_verdict, "OK")
        self.assertGreaterEqual(report.captured_count, 1)
        self.assertTrue(report.candidates, "the /api/items XHR should be a candidate")

        candidate = report.candidates[0]
        self.assertIn("/api/items", candidate["url"])
        self.assertEqual(candidate["route"]["level"], "L0")  # promotes CSR to a cheap route
        matched = candidate["matched_fields"]
        self.assertIn("title", matched)
        self.assertIn("price", matched)
        self.assertIn("published_at", matched)  # camelCase publishedAt resolved

    def test_recon_captures_carry_no_sensitive_headers(self) -> None:
        from web_scraper.probe.browser import _capture_with_playwright

        captured, status, _html = _capture_with_playwright(
            f"{self.base}/catalog",
            timeout_s=30,
            max_captures=10,
            max_json_bytes=400_000,
            headless=True,
            allow_private=True,
        )
        self.assertEqual(status, 200)
        self.assertTrue(captured)
        for capture in captured:
            header_names = {name.lower() for name in capture.request_headers}
            self.assertNotIn("cookie", header_names)
            self.assertNotIn("authorization", header_names)
            self.assertNotIn("x-api-key", header_names)
            # Ordinary headers survive, so this is not vacuously true.
            self.assertTrue(header_names)


@unittest.skipIf(playwright_available(), "Playwright installed; unavailable path not reachable")
class BrowserUnavailableTests(unittest.TestCase):
    def test_render_transport_raises_install_hint(self) -> None:
        transport = PlaywrightRenderTransport()
        with self.assertRaises(TransportUnavailable) as caught:
            transport.fetch("https://1.1.1.1/")
        self.assertIn("playwright", str(caught.exception).lower())


if __name__ == "__main__":
    unittest.main()
