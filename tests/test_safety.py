from __future__ import annotations

import sys
import unittest
from pathlib import Path
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.probe.safety import (  # noqa: E402
    SENSITIVE_REQUEST_HEADERS,
    UnsafeTarget,
    ValidatingRedirectHandler,
    pick_safe_address,
    validate_public_url,
)

PUBLIC = lambda h, p, **k: [(2, 1, 6, "", ("93.184.216.34", p))]  # noqa: E731
PRIVATE = lambda h, p, **k: [(2, 1, 6, "", ("10.0.0.7", p))]  # noqa: E731
CGNAT = lambda h, p, **k: [(2, 1, 6, "", ("100.64.1.5", p))]  # noqa: E731


class SsrfClassificationTests(unittest.TestCase):
    def test_cgnat_is_blocked(self) -> None:
        with self.assertRaises(UnsafeTarget):
            validate_public_url("http://100.64.1.5/")

    def test_metadata_and_loopback_blocked(self) -> None:
        for url in ("http://169.254.169.254/latest", "http://127.0.0.1/", "http://0.0.0.0/"):
            with self.assertRaises(UnsafeTarget):
                validate_public_url(url)

    def test_ipv6_ula_and_linklocal_blocked(self) -> None:
        for url in ("http://[fc00::1]/", "http://[fe80::1]/", "http://[::1]/"):
            with self.assertRaises(UnsafeTarget):
                validate_public_url(url)

    def test_public_literal_allowed(self) -> None:
        validate_public_url("https://1.1.1.1/")

    def test_out_of_range_port_is_unsafe_target_not_valueerror(self) -> None:
        with self.assertRaises(UnsafeTarget):
            validate_public_url("http://example.com:99999/")

    def test_hostname_resolving_to_private_is_blocked(self) -> None:
        with self.assertRaises(UnsafeTarget):
            validate_public_url("http://intranet.example/", resolver=PRIVATE)

    def test_hostname_resolving_to_cgnat_is_blocked(self) -> None:
        with self.assertRaises(UnsafeTarget):
            validate_public_url("http://pod.example/", resolver=CGNAT)


class PinningTests(unittest.TestCase):
    def test_pick_returns_validated_ip_for_public_host(self) -> None:
        self.assertEqual(pick_safe_address("example.com", 443, resolver=PUBLIC), "93.184.216.34")

    def test_pick_refuses_rebinding_to_private(self) -> None:
        with self.assertRaises(UnsafeTarget):
            pick_safe_address("evil.example", 443, resolver=PRIVATE)

    def test_pick_passes_through_public_literal(self) -> None:
        self.assertEqual(pick_safe_address("1.1.1.1", 443, resolver=PRIVATE), "1.1.1.1")

    def test_pick_allow_private_skips_validation(self) -> None:
        self.assertEqual(pick_safe_address("intranet", 80, resolver=PRIVATE, allow_private=True), "10.0.0.7")


class RedirectHeaderStrippingTests(unittest.TestCase):
    def handler(self):
        return ValidatingRedirectHandler(allow_private=False, resolver=PUBLIC)

    def test_cross_host_redirect_strips_credentials(self) -> None:
        handler = self.handler()
        req = Request(
            "https://a.example/start",
            headers={"Authorization": "Bearer secret", "Cookie": "sid=abc", "Accept": "*/*"},
        )
        new = handler.redirect_request(req, None, 302, "Found", {}, "https://b.example/next")
        keys = {k.lower() for k in new.headers}
        self.assertNotIn("authorization", keys)
        self.assertNotIn("cookie", keys)
        self.assertIn("accept", keys)

    def test_same_host_redirect_keeps_headers(self) -> None:
        handler = self.handler()
        req = Request("https://a.example/start", headers={"Authorization": "Bearer secret"})
        new = handler.redirect_request(req, None, 302, "Found", {}, "https://a.example/next")
        self.assertIn("authorization", {k.lower() for k in new.headers})

    def test_redirect_to_private_is_refused(self) -> None:
        handler = ValidatingRedirectHandler(allow_private=False, resolver=PRIVATE)
        req = Request("https://a.example/start")
        with self.assertRaises(UnsafeTarget):
            handler.redirect_request(req, None, 302, "Found", {}, "https://internal.example/x")

    def test_sensitive_set_is_lowercase(self) -> None:
        self.assertTrue(all(name == name.lower() for name in SENSITIVE_REQUEST_HEADERS))


if __name__ == "__main__":
    unittest.main()
