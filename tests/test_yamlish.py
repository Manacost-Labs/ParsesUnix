from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.profiles.yamlish import YamlishError, loads


class YamlishTests(unittest.TestCase):
    def test_parses_nested_mappings_lists_and_scalars(self) -> None:
        data = loads(
            "a:\n"
            "  b: 1\n"
            '  c: [x, "y,z", 2]\n'
            "  d:\n"
            "    - kind: css\n"
            "      fields:\n"
            '        title: "h1::text"\n'
            "    - plain\n"
            "  e: null\n"
            "  f: true\n"
            "  g: -2.5\n"
        )
        self.assertEqual(data["a"]["b"], 1)
        self.assertEqual(data["a"]["c"], ["x", "y,z", 2])
        self.assertEqual(data["a"]["d"][0]["fields"]["title"], "h1::text")
        self.assertEqual(data["a"]["d"][1], "plain")
        self.assertIsNone(data["a"]["e"])
        self.assertTrue(data["a"]["f"])
        self.assertEqual(data["a"]["g"], -2.5)

    def test_double_quoted_escapes_match_json(self) -> None:
        data = loads('m: "^https://example\\\\.com/"')
        self.assertEqual(data["m"], "^https://example\\.com/")

    def test_comments_are_ignored_but_hash_in_quotes_survives(self) -> None:
        data = loads('a: "x # y"  # trailing comment\n# full line\nb: 2\n')
        self.assertEqual(data["a"], "x # y")
        self.assertEqual(data["b"], 2)

    def test_anchors_are_rejected(self) -> None:
        with self.assertRaises(YamlishError):
            loads("a: &anchor 1\n")

    def test_block_scalars_are_rejected(self) -> None:
        with self.assertRaises(YamlishError):
            loads("a: |\n  text\n")

    def test_tabs_in_indentation_are_rejected(self) -> None:
        with self.assertRaises(YamlishError):
            loads("a:\n\tb: 1\n")

    def test_duplicate_keys_are_rejected(self) -> None:
        with self.assertRaises(YamlishError):
            loads("a: 1\na: 2\n")

    def test_apostrophe_in_plain_scalar_is_literal(self) -> None:
        data = loads("notes: it's fine  # trailing\n")
        self.assertEqual(data["notes"], "it's fine")

    def test_apostrophe_plain_scalar_with_hash_word(self) -> None:
        data = loads("a: don't  # c\nb: 2\n")
        self.assertEqual(data["a"], "don't")
        self.assertEqual(data["b"], 2)

    def test_quoted_value_still_parses_after_apostrophe_fix(self) -> None:
        data = loads("m: 'hello world'\n")
        self.assertEqual(data["m"], "hello world")

    def test_flow_mappings_of_scalars(self) -> None:
        # The shape every YAML example uses for a compact route or field list.
        self.assertEqual(loads("r: {type: rss, level: L0}\n")["r"], {"type": "rss", "level": "L0"})
        self.assertEqual(loads("e: {}\n")["e"], {})

    def test_nested_flow_mapping_is_not_torn_at_its_inner_comma(self) -> None:
        data = loads('x: {kind: css, fields: {title: "h1::text", date: "time::attr(d)"}}\n')
        self.assertEqual(
            data["x"],
            {"kind": "css", "fields": {"title": "h1::text", "date": "time::attr(d)"}},
        )

    def test_flow_mappings_inside_a_list(self) -> None:
        data = loads("routes:\n  - {type: rss, level: L0}\n  - {type: direct_http, level: L1}\n")
        self.assertEqual(data["routes"][1], {"type": "direct_http", "level": "L1"})

    def test_a_malformed_flow_mapping_fails_loudly(self) -> None:
        for bad in ("x: {a: 1\n", "x: {a}\n", "x: {a: 1, a: 2}\n"):
            with self.assertRaises(YamlishError):
                loads(bad)

    def test_the_readme_profile_example_parses(self) -> None:
        # The example a newcomer copies must work on a bare stdlib install.
        example = (
            "site: example.com\n"
            "authorization:\n"
            "  public_data_only: true\n"
            "url_classes:\n"
            "  article:\n"
            '    match: "^https://example\\\\.com/articles/"\n'
            "    routes:\n"
            "      primary: {id: articles-api, type: json_api, level: L0}\n"
            "      alternatives:\n"
            "        - {type: direct_http, level: L1}\n"
        )
        data = loads(example)
        self.assertEqual(data["url_classes"]["article"]["routes"]["primary"]["id"], "articles-api")


class YamlishPyYAMLDifferentialTests(unittest.TestCase):
    """When PyYAML is present, yamlish must agree on the profile subset."""

    def setUp(self) -> None:
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not installed")

    def test_matches_pyyaml_on_subset(self) -> None:
        import yaml

        doc = (
            "site: demo.example\n"
            "authorization:\n"
            "  public_data_only: true\n"
            'list: [a, "b,c", 2]\n'
            "nested:\n"
            "  n: -3.5\n"
            '  s: "^https://x\\\\.y/"\n'
            "  empty: null\n"
        )
        self.assertEqual(loads(doc), yaml.safe_load(doc))


if __name__ == "__main__":
    unittest.main()
