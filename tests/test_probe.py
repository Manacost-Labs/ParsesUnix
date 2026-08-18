from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / ".agents/skills/web-scraper/scripts"
sys.path.insert(0, str(SCRIPTS))

from probe import UnsafeTarget, validate_public_url


class ProbeSafetyTests(unittest.TestCase):
    def test_rejects_localhost_literal(self) -> None:
        with self.assertRaises(UnsafeTarget):
            validate_public_url("http://127.0.0.1/admin")

    def test_rejects_cloud_metadata_address(self) -> None:
        with self.assertRaises(UnsafeTarget):
            validate_public_url("http://169.254.169.254/latest/meta-data")

    def test_rejects_credentials_in_url(self) -> None:
        with self.assertRaises(UnsafeTarget):
            validate_public_url("https://user:secret@example.com/")

    def test_accepts_public_address_literal(self) -> None:
        validate_public_url("https://1.1.1.1/")


if __name__ == "__main__":
    unittest.main()
