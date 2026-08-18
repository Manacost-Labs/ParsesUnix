from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.profiles import ProfileError, load_profile, parse_profile

TEMPLATE = ROOT / ".agents/skills/web-scraper/assets/templates/site-profile.yaml"


def minimal_profile() -> dict:
    return {
        "site": "demo-news.example",
        "authorization": {"public_data_only": True},
        "url_classes": {
            "article": {
                "match": "^https://demo-news\\.example/articles/",
                "expected_content_type": "html",
                "validation": {"min_body_bytes": 500, "canary": "<article"},
                "routes": {"primary": {"type": "direct_http", "level": "L1"}},
                "extractors": [{"kind": "json_ld", "schema_type": "Article"}],
            }
        },
    }


class ProfileValidationTests(unittest.TestCase):
    def test_bundled_template_is_valid(self) -> None:
        profile = load_profile(TEMPLATE)
        self.assertEqual(profile.site, "example.com")
        article = profile.url_classes["article"]
        self.assertEqual(article.primary_route.level.value, "L1")
        self.assertEqual(len(article.alternative_routes), 2)
        self.assertIn("<article", article.content_rules().all_canaries)

    def test_minimal_profile_is_valid_and_gets_defaults(self) -> None:
        profile = parse_profile(minimal_profile())
        article = profile.url_classes["article"]
        self.assertEqual(article.limits["daily_paid_credits"], 0)
        self.assertEqual(article.retry["max_attempts"], 2)
        self.assertTrue(article.matches("https://demo-news.example/articles/x"))

    def test_missing_routes_is_rejected(self) -> None:
        data = minimal_profile()
        del data["url_classes"]["article"]["routes"]
        with self.assertRaises(ProfileError) as caught:
            parse_profile(data)
        self.assertTrue(any("routes" in error for error in caught.exception.errors))

    def test_unanchored_match_is_rejected(self) -> None:
        data = minimal_profile()
        data["url_classes"]["article"]["match"] = "articles/"
        with self.assertRaises(ProfileError):
            parse_profile(data)

    def test_invalid_regex_is_rejected(self) -> None:
        data = minimal_profile()
        data["url_classes"]["article"]["match"] = "^https://demo(["
        with self.assertRaises(ProfileError):
            parse_profile(data)

    def test_route_level_mismatch_is_rejected(self) -> None:
        data = minimal_profile()
        data["url_classes"]["article"]["routes"]["primary"] = {"type": "json_api", "level": "L2"}
        with self.assertRaises(ProfileError):
            parse_profile(data)

    def test_validation_without_content_proof_is_rejected(self) -> None:
        data = minimal_profile()
        data["url_classes"]["article"]["validation"] = {"min_body_bytes": 100}
        with self.assertRaises(ProfileError):
            parse_profile(data)

    def test_missing_authorization_is_rejected(self) -> None:
        data = minimal_profile()
        del data["authorization"]
        with self.assertRaises(ProfileError):
            parse_profile(data)

    def test_unknown_key_typo_is_rejected(self) -> None:
        data = minimal_profile()
        data["url_classes"]["article"]["frehsness"] = {"max_age_hours": 1}
        with self.assertRaises(ProfileError) as caught:
            parse_profile(data)
        self.assertTrue(any("frehsness" in error for error in caught.exception.errors))

    def test_quorum_must_be_subset_of_required_fields(self) -> None:
        data = minimal_profile()
        data["url_classes"]["article"]["validation"]["required_fields"] = ["title"]
        data["url_classes"]["article"]["quorum_fields"] = ["title", "price"]
        with self.assertRaises(ProfileError):
            parse_profile(data)

    def test_promote_bounds_are_enforced(self) -> None:
        data = minimal_profile()
        data["url_classes"]["article"]["promote"] = {"min_completeness": 1.5}
        with self.assertRaises(ProfileError):
            parse_profile(data)


class ProfileSecretTests(unittest.TestCase):
    def test_cookie_key_is_rejected(self) -> None:
        data = minimal_profile()
        data["url_classes"]["article"]["x-extra"] = {"cookies": "session=abc"}
        with self.assertRaises(ProfileError) as caught:
            parse_profile(data)
        self.assertTrue(any("forbidden" in error for error in caught.exception.errors))

    def test_authorization_header_key_is_rejected(self) -> None:
        data = minimal_profile()
        data["url_classes"]["article"]["x-headers"] = {"Authorization": "Basic Zm9vOmJhcg=="}
        with self.assertRaises(ProfileError):
            parse_profile(data)

    def test_bearer_token_value_is_rejected(self) -> None:
        data = minimal_profile()
        data["notes"] = "use Bearer abcdef123456789 for the api"
        with self.assertRaises(ProfileError):
            parse_profile(data)

    def test_aws_key_value_is_rejected(self) -> None:
        data = minimal_profile()
        data["url_classes"]["article"]["notes"] = "key AKIAIOSFODNN7EXAMPLE"
        with self.assertRaises(ProfileError):
            parse_profile(data)

    def test_policy_authorization_section_is_allowed(self) -> None:
        parse_profile(minimal_profile())  # must not raise


class ProfileRouteAndOverlapTests(unittest.TestCase):
    def test_third_party_route_url_is_rejected(self) -> None:
        data = minimal_profile()
        data["url_classes"]["article"]["routes"]["alternatives"] = [
            {"type": "json_api", "level": "L0", "url": "https://evil.example/api/x"}
        ]
        with self.assertRaises(ProfileError) as caught:
            parse_profile(data)
        self.assertTrue(any("does not belong to site" in e for e in caught.exception.errors))

    def test_relative_route_url_is_rejected(self) -> None:
        data = minimal_profile()
        data["url_classes"]["article"]["routes"]["alternatives"] = [
            {"type": "json_api", "level": "L0", "url": "/api/x"}
        ]
        with self.assertRaises(ProfileError) as caught:
            parse_profile(data)
        self.assertTrue(any("absolute http(s)" in e for e in caught.exception.errors))

    def test_same_site_subdomain_route_is_accepted(self) -> None:
        data = minimal_profile()
        data["url_classes"]["article"]["routes"]["alternatives"] = [
            {"type": "json_api", "level": "L0", "url": "https://api.demo-news.example/v1/{id}"}
        ]
        parse_profile(data)  # must not raise

    def test_required_fields_alone_is_not_a_content_proof(self) -> None:
        data = minimal_profile()
        data["url_classes"]["article"]["validation"] = {
            "min_body_bytes": 200,
            "required_fields": ["title"],
        }
        with self.assertRaises(ProfileError) as caught:
            parse_profile(data)
        self.assertTrue(any("content proof" in e for e in caught.exception.errors))

    def test_overlapping_classes_are_rejected(self) -> None:
        data = minimal_profile()
        data["url_classes"]["catchall"] = {
            "match": "^https://demo-news\\.example/",  # matches everything the article class does
            "validation": {"canary": "x"},
            "routes": {"primary": {"type": "direct_http", "level": "L1"}},
            "extractors": [{"kind": "heuristic"}],
        }
        with self.assertRaises(ProfileError) as caught:
            parse_profile(data)
        self.assertTrue(any("both match" in e for e in caught.exception.errors))


if __name__ == "__main__":
    unittest.main()
