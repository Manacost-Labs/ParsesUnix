"""Skill-format validation.

Closes the stage-0 item that was specified but never implemented ("проверка
формата скилла"). Skills are plain files with no compiler behind them, so a
typo in frontmatter or a link to a file that was not vendored fails silently at
runtime — the agent simply cannot load the skill. These tests make that loud.

Covers both the project's own skills and the vendored third-party ones.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_SKILLS = ROOT / ".agents" / "skills"
CLAUDE_SKILLS = ROOT / ".claude" / "skills"

#: Skills authored in this repository (the rest are vendored, see THIRD_PARTY.md).
OWN_SKILLS = {"web-scraper", "scraper-regression", "scraper-debugger"}

_LINK_RE = re.compile(r"\]\((?!https?:|#|mailto:)([A-Za-z0-9_./-]+\.(?:md|py|sh|ts|yaml|yml|json))\)")
_NAME_RE = re.compile(r'^name:\s*"?([A-Za-z0-9_-]+)"?\s*$', re.MULTILINE)
_DESC_RE = re.compile(r"^description:\s*\S", re.MULTILINE)


def skill_dirs() -> list[Path]:
    return sorted(p.parent for p in AGENT_SKILLS.glob("*/SKILL.md"))


def frontmatter(text: str) -> str | None:
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    return parts[1] if len(parts) >= 3 else None


class SkillLayoutTests(unittest.TestCase):
    def test_skills_exist(self) -> None:
        self.assertTrue(skill_dirs(), "no skills found under .agents/skills")

    def test_own_skills_are_present(self) -> None:
        names = {path.name for path in skill_dirs()}
        self.assertTrue(OWN_SKILLS <= names, f"missing own skills: {OWN_SKILLS - names}")

    def test_every_agent_skill_is_symlinked_for_claude(self) -> None:
        for skill in skill_dirs():
            with self.subTest(skill=skill.name):
                link = CLAUDE_SKILLS / skill.name
                self.assertTrue(link.is_symlink(), f"{link} is not a symlink")
                self.assertTrue(
                    (link / "SKILL.md").is_file(), f"{link} does not resolve to a skill"
                )

    def test_no_stale_symlinks(self) -> None:
        for link in CLAUDE_SKILLS.iterdir():
            with self.subTest(link=link.name):
                self.assertTrue(
                    (link / "SKILL.md").is_file(), f"{link.name} points at a missing skill"
                )


class SkillFrontmatterTests(unittest.TestCase):
    def test_frontmatter_has_name_and_description(self) -> None:
        for skill in skill_dirs():
            with self.subTest(skill=skill.name):
                block = frontmatter((skill / "SKILL.md").read_text(encoding="utf-8"))
                self.assertIsNotNone(block, "SKILL.md must open with a --- frontmatter block")
                self.assertRegex(block, _NAME_RE, "frontmatter needs a name")
                self.assertRegex(block, _DESC_RE, "frontmatter needs a description")

    def test_frontmatter_name_matches_directory(self) -> None:
        for skill in skill_dirs():
            with self.subTest(skill=skill.name):
                block = frontmatter((skill / "SKILL.md").read_text(encoding="utf-8"))
                declared = _NAME_RE.search(block)
                self.assertEqual(declared.group(1), skill.name)

    def test_own_skills_keep_frontmatter_minimal(self) -> None:
        # Recorded project decision: our own skills declare only name+description.
        # Vendored skills are exempt — they are kept byte-identical to upstream.
        for skill in skill_dirs():
            if skill.name not in OWN_SKILLS:
                continue
            with self.subTest(skill=skill.name):
                block = frontmatter((skill / "SKILL.md").read_text(encoding="utf-8"))
                keys = {
                    line.split(":", 1)[0].strip()
                    for line in block.splitlines()
                    if line.strip() and not line.startswith((" ", "\t", "-"))
                }
                self.assertEqual(keys, {"name", "description"})


class SkillLinkTests(unittest.TestCase):
    def test_relative_links_resolve(self) -> None:
        for skill in skill_dirs():
            for markdown in skill.rglob("*.md"):
                text = markdown.read_text(encoding="utf-8")
                for match in _LINK_RE.finditer(text):
                    target = (markdown.parent / match.group(1)).resolve()
                    with self.subTest(file=str(markdown.relative_to(ROOT)), link=match.group(1)):
                        self.assertTrue(target.exists(), f"broken link: {match.group(1)}")

    def test_vendored_skills_are_attributed(self) -> None:
        notice = AGENT_SKILLS / "THIRD_PARTY.md"
        self.assertTrue(notice.is_file(), "vendored skills require an attribution file")
        text = notice.read_text(encoding="utf-8")
        for skill in skill_dirs():
            if skill.name in OWN_SKILLS:
                continue
            with self.subTest(skill=skill.name):
                self.assertIn(skill.name, text, "vendored skill is not attributed")
        self.assertIn("MIT License", text)


if __name__ == "__main__":
    unittest.main()
