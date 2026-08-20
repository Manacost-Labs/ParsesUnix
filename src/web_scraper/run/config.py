"""Run configuration: which profile, which state paths, which window/limits.

A Site Profile is per-domain (routes, extractors, thresholds). A run needs the
extra per-execution facts a profile does not carry: where state lives, the time
window, batch size, and whether this is a full revision.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_CONFIG_KEYS = frozenset(
    {
        "profile",
        "state_dir",
        "run_id",
        "free_canary",
        "paid_canary",
        "discover_api",
        "seed_urls",
        "deadline_seconds",
        "batch_size",
        "full_review",
        "allow_private",
        "dead_zone_after_attempts",
        "sweep",
        "adaptive_routing",
        "browser_pool",
        "max_browser_contexts",
        "daily_credit_limit",
    }
)


def _boolean(data: dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a JSON boolean")
    return value


def _integer(data: dict[str, Any], key: str, default: int, *, minimum: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    if value < minimum:
        raise ValueError(f"{key} must be at least {minimum}")
    return value


def _optional_positive_number(data: dict[str, Any], key: str) -> float | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number or null")
    result = float(value)
    if result <= 0:
        raise ValueError(f"{key} must be greater than zero")
    return result


def _string(data: dict[str, Any], key: str, default: str = "") -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _string_tuple(data: dict[str, Any], key: str) -> tuple[str, ...]:
    value = data.get(key, ())
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{key} must be an array of non-empty strings")
    return tuple(value)


@dataclass(frozen=True)
class RunConfig:
    profile_path: Path
    state_dir: Path
    #: Identifies one run WINDOW, so a crashed run resumes its phases. Defaults
    #: to the state directory's name, which is stable across restarts of the
    #: same scheduled job. A completed cycle does not block the next one - the
    #: phase controller starts fresh once all phases are done.
    run_id: str = ""
    #: Run a stratified free canary before the crawl. Cheap, and it is the only
    #: thing that stops a 100k run against a redesigned site.
    free_canary: bool = True
    #: Run a paid canary before the paid phases. Off means the paid batch starts
    #: on yesterday's statistics alone.
    paid_canary: bool = True
    #: Observe network traffic during browser renders and propose cheaper
    #: structured routes. Costs nothing: it rides along with renders that were
    #: happening anyway.
    discover_api: bool = True

    seed_urls: tuple[str, ...] = ()
    deadline_seconds: float | None = None  # run-window cap; None = until the queue drains
    batch_size: int = 20
    full_review: bool = False  # ignore freshness intervals; re-check everything
    allow_private: bool = False
    dead_zone_after_attempts: int = 3
    sweep: bool = False  # phase-A HEAD sweep to quarantine dead URLs before the main pass
    #: Let past runs reorder the profile's routes. Off reproduces the declared ladder.
    adaptive_routing: bool = True
    #: Share one browser across the run instead of launching per URL.
    browser_pool: bool = True
    #: Concurrent domain contexts; each costs memory even when idle.
    max_browser_contexts: int = 4
    #: Daily credit ceiling for paid providers. None disables paid work entirely.
    daily_credit_limit: str | None = None

    @property
    def effective_run_id(self) -> str:
        return self.run_id or f"run:{self.state_dir.name}"

    @property
    def queue_path(self) -> Path:
        return self.state_dir / "queue.sqlite3"

    @property
    def dataset_path(self) -> Path:
        return self.state_dir / "dataset.sqlite3"

    @property
    def freshness_path(self) -> Path:
        return self.state_dir / "freshness.sqlite3"

    @property
    def route_stats_path(self) -> Path:
        return self.state_dir / "route_stats.sqlite3"

    @property
    def budget_path(self) -> Path:
        return self.state_dir / "budget.sqlite3"

    @property
    def fingerprints_path(self) -> Path:
        return self.state_dir / "fingerprints.sqlite3"

    @property
    def snapshot_dir(self) -> Path:
        return self.state_dir / "snapshots"

    @classmethod
    def from_file(cls, path: str | Path) -> RunConfig:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("run config must be a JSON object")
        return cls.from_dict(data, base_dir=Path(path).parent)

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, base_dir: Path | None = None) -> RunConfig:
        unknown = sorted(set(data) - _CONFIG_KEYS)
        if unknown:
            raise ValueError(f"unknown run config fields: {', '.join(unknown)}")
        base = base_dir or Path.cwd()

        def resolve(key: str, default: str | None = None) -> Path:
            if default is None and key not in data:
                raise ValueError(f"missing required run config field: {key}")
            p = _string(data, key, default or "")
            if not p.strip():
                raise ValueError(f"{key} must not be empty")
            path = Path(p)
            return path if path.is_absolute() else (base / path)

        return cls(
            profile_path=resolve("profile"),
            state_dir=resolve("state_dir", "state"),
            run_id=_string(data, "run_id"),
            free_canary=_boolean(data, "free_canary", True),
            paid_canary=_boolean(data, "paid_canary", True),
            discover_api=_boolean(data, "discover_api", True),
            seed_urls=_string_tuple(data, "seed_urls"),
            deadline_seconds=_optional_positive_number(data, "deadline_seconds"),
            batch_size=_integer(data, "batch_size", 20, minimum=1),
            full_review=_boolean(data, "full_review", False),
            allow_private=_boolean(data, "allow_private", False),
            dead_zone_after_attempts=_integer(
                data, "dead_zone_after_attempts", 3, minimum=1
            ),
            sweep=_boolean(data, "sweep", False),
            adaptive_routing=_boolean(data, "adaptive_routing", True),
            browser_pool=_boolean(data, "browser_pool", True),
            max_browser_contexts=_integer(
                data, "max_browser_contexts", 4, minimum=1
            ),
            daily_credit_limit=(
                _string(data, "daily_credit_limit")
                if data.get("daily_credit_limit") is not None
                else None
            ),
        )
