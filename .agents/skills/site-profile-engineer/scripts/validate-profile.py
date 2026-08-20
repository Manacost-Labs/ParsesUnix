#!/usr/bin/env python3
"""Validate a Site Profile package without touching the network.

Checks the four package files exist, the profile parses, the corpus has a
negative case per class, and the evidence carries no URLs or bodies. Everything
here is also checked by `ws-profile certify`; this exists so the check is one
command away when the package is not yet registered.

    python scripts/validate-profile.py site_profiles/example.test
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src"))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    package = Path(argv[1])

    from web_scraper.profile_engineering.corpus import load_corpus
    from web_scraper.profiles.model import ProfileError, load_profile

    problems: list[str] = []
    for name in ("profile.yaml", "corpus.yaml", "evidence.json", "README.md"):
        if not (package / name).exists():
            problems.append(f"missing {name}")

    try:
        profile = load_profile(package / "profile.yaml")
    except (ProfileError, OSError) as exc:
        errors = getattr(exc, "errors", [str(exc)])
        problems.extend(str(e) for e in errors)
        profile = None

    if profile is not None and (package / "corpus.yaml").exists():
        corpus = load_corpus(package / "corpus.yaml")
        for name in profile.url_classes:
            if not corpus.negative_cases(name):
                problems.append(
                    f"{name}: no negative case. A suite of happy paths cannot tell a "
                    "working profile from one that accepts anything."
                )

    evidence = package / "evidence.json"
    if evidence.exists():
        text = evidence.read_text(encoding="utf-8")
        if "https://" in text or "<html" in text:
            problems.append("evidence.json contains URLs or page content; it is committed")

    if problems:
        print(f"{package}: NOT VALID")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"{package}: package is well formed (this is not certification)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
