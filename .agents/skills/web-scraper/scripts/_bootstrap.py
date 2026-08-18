"""Locate the web_scraper package for the thin CLI wrappers in this directory.

Resolution order (first hit wins), deliberately narrow to avoid importing an
unrelated ``src/web_scraper`` from some ancestor directory:

1. an already-importable ``web_scraper`` (e.g. ``pip install -e .``);
2. the ``WEB_SCRAPER_SRC`` environment variable pointing at a ``src`` dir;
3. the repository layout this skill ships in: ``<repo>/src`` reached by walking
   up until a directory that both contains ``src/web_scraper/__init__.py`` and
   looks like this project's root (``pyproject.toml`` or ``.agents`` present).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _valid_src(src: Path) -> bool:
    return (src / "web_scraper" / "__init__.py").is_file()


def ensure_web_scraper_on_path() -> None:
    try:
        import web_scraper  # noqa: F401

        return
    except ModuleNotFoundError:
        pass

    env_src = os.environ.get("WEB_SCRAPER_SRC")
    if env_src:
        src = Path(env_src).expanduser().resolve()
        if _valid_src(src):
            sys.path.insert(0, str(src))
            return
        raise ModuleNotFoundError(
            f"WEB_SCRAPER_SRC={env_src!r} does not contain web_scraper/__init__.py"
        )

    for parent in Path(__file__).resolve().parents:
        src = parent / "src"
        looks_like_root = (parent / "pyproject.toml").is_file() or (parent / ".agents").is_dir()
        if looks_like_root and _valid_src(src):
            sys.path.insert(0, str(src))
            return

    raise ModuleNotFoundError(
        "web_scraper package not found. Install it (`pip install -e .` from the "
        "ParserUnix repository), or set WEB_SCRAPER_SRC to the repository's src/ directory."
    )
