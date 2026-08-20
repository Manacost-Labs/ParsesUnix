from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.run.config import RunConfig


class RunConfigTests(unittest.TestCase):
    def test_boolean_strings_are_rejected_instead_of_becoming_true(self) -> None:
        for key in (
            "free_canary",
            "paid_canary",
            "discover_api",
            "full_review",
            "allow_private",
            "sweep",
            "adaptive_routing",
            "browser_pool",
        ):
            with self.subTest(key=key), self.assertRaisesRegex(
                ValueError, f"{key} must be a JSON boolean"
            ):
                RunConfig.from_dict({"profile": "profile.json", key: "false"})

    def test_false_keeps_private_network_access_disabled(self) -> None:
        config = RunConfig.from_dict(
            {"profile": "profile.json", "allow_private": False}
        )

        self.assertFalse(config.allow_private)

    def test_invalid_limits_are_rejected(self) -> None:
        for key, value in (
            ("batch_size", 0),
            ("dead_zone_after_attempts", 0),
            ("max_browser_contexts", 0),
            ("deadline_seconds", 0),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                RunConfig.from_dict({"profile": "profile.json", key: value})

    def test_unknown_fields_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown run config fields: typo"):
            RunConfig.from_dict({"profile": "profile.json", "typo": True})

    def test_seed_urls_must_be_an_array_of_strings(self) -> None:
        with self.assertRaisesRegex(ValueError, "seed_urls must be an array"):
            RunConfig.from_dict(
                {"profile": "profile.json", "seed_urls": "https://example.com"}
            )

    def test_from_file_requires_a_json_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.json"
            path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "must be a JSON object"):
                RunConfig.from_file(path)


if __name__ == "__main__":
    unittest.main()
