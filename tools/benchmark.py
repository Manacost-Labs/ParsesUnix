"""Reproducible performance benchmark for ParsesUnix.

Deliberately separate from the soak. The soak proves invariants — nothing is
lost, nothing is double-paid — and its assertions must hold at any size. This
measures *cost in time and memory*, which is a different question with different
failure modes: a benchmark that fails is a signal to investigate, not a bug.

Two workloads, and they are never mixed in a report:

``synthetic``
    A generated population run through the real pipeline with scripted
    transports. Measures the machinery: queue, triage, extraction, accounting,
    SQLite. Says nothing about any real website.

``real``
    A small sample of permitted public targets. Measures what actually happens
    over a network, and is small on purpose — a benchmark that hammers someone
    else's site to produce a table is not one worth having.

Nothing here is asserted in CI. Wall-clock numbers on shared hardware are not a
gate; they are evidence for a human.

    python tools/benchmark.py synthetic --urls 10000
    python tools/benchmark.py real --urls 100
    python tools/benchmark.py discovery-overhead
    python tools/benchmark.py providers            # provider calibration, plan only
    python tools/benchmark.py providers --live     # ...and actually call them

The ``providers`` workload is the one that spends money, so it lives in the
package rather than here — ``web_scraper.calibration``, installed as
``ws-benchmark``. This file forwards to it so both doors open on the same room:
a second implementation of the same benchmark is how two answers to one question
appear.
"""

from __future__ import annotations

import argparse
import gc
import json
import statistics
import sys
import tempfile
import time
import tracemalloc
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


#: Permitted public targets. Sandboxes built for scraping practice, plus sites
#: whose robots.txt allows the generic agent. Anything that disallows this
#: crawler is not benchmarked, whatever the numbers would look like.
REAL_TARGETS: tuple[tuple[str, str], ...] = (
    ("books.toscrape.com", "https://books.toscrape.com/"),
    ("books.toscrape.com", "https://books.toscrape.com/catalogue/page-2.html"),
    ("quotes.toscrape.com", "https://quotes.toscrape.com/"),
    ("quotes.toscrape.com", "https://quotes.toscrape.com/page/2/"),
    ("scrapethissite.com", "https://www.scrapethissite.com/pages/simple/"),
)


@dataclass
class Latencies:
    """Percentiles, computed rather than approximated."""

    samples: list[float] = field(default_factory=list)

    def add(self, milliseconds: float) -> None:
        self.samples.append(milliseconds)

    def percentile(self, fraction: float) -> float | None:
        if not self.samples:
            return None
        ordered = sorted(self.samples)
        index = min(int(len(ordered) * fraction), len(ordered) - 1)
        return ordered[index]

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": len(self.samples),
            "p50_ms": _round(self.percentile(0.50)),
            "p95_ms": _round(self.percentile(0.95)),
            "p99_ms": _round(self.percentile(0.99)),
            "mean_ms": _round(statistics.fmean(self.samples)) if self.samples else None,
            "max_ms": _round(max(self.samples)) if self.samples else None,
        }


@dataclass
class BenchmarkResult:
    workload: str
    urls: int
    wall_seconds: float
    latency: Latencies
    peak_rss_mb: float | None = None
    cpu_seconds: float | None = None
    verdicts: dict[str, int] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ms_per_url(self) -> float | None:
        """End-to-end wall time per URL — the number that includes everything."""

        return round(self.wall_seconds * 1000 / self.urls, 3) if self.urls else None

    @property
    def urls_per_second(self) -> float | None:
        return round(self.urls / self.wall_seconds, 1) if self.wall_seconds > 0 else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "workload": self.workload,
            "urls": self.urls,
            "wall_seconds": round(self.wall_seconds, 3),
            "urls_per_second": self.urls_per_second,
            "ms_per_url": self.ms_per_url,
            "latency": self.latency.to_dict(),
            "peak_rss_mb": self.peak_rss_mb,
            "cpu_seconds": self.cpu_seconds,
            "verdicts": self.verdicts,
            **self.extra,
        }

    def describe(self) -> str:
        total = sum(self.verdicts.values()) or 1
        ok = self.verdicts.get("OK", 0)
        lines = [
            f"workload:          {self.workload}",
            f"URLs:              {self.urls}",
            f"wall:              {self.wall_seconds:.2f} s",
            f"URLs/sec:          {self.urls_per_second}",
            "",
            f"valid fetch:       {ok / total:.1%}",
        ]
        latency = self.latency.to_dict()
        for key in ("p50_ms", "p95_ms", "p99_ms"):
            # `is not None`, not truthiness: a sub-millisecond p50 rounds to 0.0,
            # which is a measurement, and printing "n/a" for it hides the result.
            value = latency[key]
            lines.append(
                f"{key + ':':<19}{value} ms" if value is not None else f"{key + ':':<19}n/a"
            )
        if self.urls and self.wall_seconds > 0:
            lines.append(f"{'per URL (wall):':<19}{self.wall_seconds * 1000 / self.urls:.2f} ms")
        if self.peak_rss_mb is not None:
            lines.append(f"peak RSS:          {self.peak_rss_mb} MB")
        if self.cpu_seconds is not None:
            lines.append(f"CPU:               {self.cpu_seconds:.2f} s")
        if self.verdicts:
            lines.append("")
            lines.append("verdicts:")
            for name, count in sorted(self.verdicts.items(), key=lambda kv: -kv[1]):
                lines.append(f"  {name:<16} {count:>7}  {count / total:6.1%}")
        for key, value in self.extra.items():
            lines.append(f"{key + ':':<19}{value}")
        return "\n".join(lines)


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def _rss_mb() -> float | None:
    """Peak RSS without adding a dependency for it."""

    try:
        import resource
    except ImportError:  # pragma: no cover - Windows
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports kilobytes, macOS bytes.
    return round(peak / (1024 * 1024 if peak > 10**7 else 1024), 1)


# ---------------------------------------------------------------------------
# synthetic
# ---------------------------------------------------------------------------


def synthetic(urls: int) -> BenchmarkResult:
    """The machinery, at size, with scripted transports.

    Measures queue, triage, extraction, accounting and SQLite. Says nothing
    about any real website, and the report labels it so.
    """

    from web_scraper.fetchers import FetchGateway, Pacer, RawResponse
    from web_scraper.profiles import parse_profile
    from web_scraper.run.config import RunConfig
    from web_scraper.run.runner import Runner
    from web_scraper.storage import load_saved_response

    fixtures = ROOT / "tests" / "fixtures"
    bodies = {
        name: load_saved_response(fixtures / name)
        for name in ("success", "blocked", "origin-down", "dead-url", "csr-shell")
    }
    # Proportions chosen to look like a real crawl rather than a happy path.
    mix = (
        ["success"] * 70
        + ["csr-shell"] * 10
        + ["origin-down"] * 10
        + ["blocked"] * 5
        + ["dead-url"] * 5
    )

    profile = parse_profile(
        {
            "site": "bench.example",
            "authorization": {"public_data_only": True},
            "url_classes": {
                "page": {
                    "match": r"^https://bench\.example/",
                    "expected_content_type": "html",
                    "validation": {
                        "min_body_bytes": 300,
                        "canary": "<article",
                        "required_fields": ["title"],
                    },
                    "routes": {"primary": {"type": "direct_http", "level": "L1"}},
                    "extractors": [{"kind": "json_ld"}, {"kind": "heuristic"}],
                    "quorum_fields": ["title"],
                    "retry": {"max_attempts": 1, "backoff_seconds": 0},
                }
            },
        }
    )

    latency = Latencies()

    class Scripted:
        def fetch(self, url: str, *, headers: object = None) -> RawResponse:
            started = time.perf_counter()
            index = abs(hash(url)) % len(mix)
            saved = bodies[mix[index]]
            response = RawResponse(
                requested_url=url,
                final_url=url,
                status=saved.status,
                headers=saved.headers,
                body=saved.body,
                elapsed_ms=1,
            )
            latency.add((time.perf_counter() - started) * 1000)
            return response

    class NoWait(Pacer):
        def __init__(self) -> None:
            super().__init__(min_interval_s=0.0, jitter_s=0.0, sleep=lambda _s: None)

    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp)
        config = RunConfig(
            profile_path=state / "p.json",
            state_dir=state,
            seed_urls=tuple(f"https://bench.example/item/{i}" for i in range(urls)),
            browser_pool=False,
            free_canary=False,
            discover_api=False,
            batch_size=200,
        )
        runner = Runner(
            config,
            profile=profile,
            gateway=FetchGateway(profile, transport_provider=lambda *_: Scripted(), pacer=NoWait()),
            wall_clock=lambda: 1000.0,
        )
        gc.collect()
        cpu_before = time.process_time()
        started = time.perf_counter()
        result = runner.run()
        wall = time.perf_counter() - started
        cpu = time.process_time() - cpu_before

    report = result.report
    return BenchmarkResult(
        workload="synthetic performance (scripted transports, no network)",
        urls=urls,
        wall_seconds=wall,
        latency=latency,
        peak_rss_mb=_rss_mb(),
        cpu_seconds=cpu,
        verdicts=report["metrics"]["verdicts"],
        extra={
            "unaccounted": report["accounting"]["unaccounted"],
            "paid_calls": report["metrics"]["paid_calls"],
            "cost_credits": report["metrics"]["cost_credits"],
        },
    )


# ---------------------------------------------------------------------------
# real
# ---------------------------------------------------------------------------


def real(urls: int) -> BenchmarkResult:
    """A small sample over the network, on permitted targets only.

    Small on purpose: a benchmark that hammers someone else's site to produce a
    prettier table is not one worth having.
    """

    from web_scraper.contracts import ContentRules
    from web_scraper.extract import detect_content_kind
    from web_scraper.probe.static import default_fetch
    from web_scraper.triage import classify_response

    latency = Latencies()
    verdicts: dict[str, int] = {}
    kinds: dict[str, int] = {}
    fetched = 0

    targets = [REAL_TARGETS[i % len(REAL_TARGETS)] for i in range(urls)]
    cpu_before = time.process_time()
    started = time.perf_counter()
    for _, url in targets:
        call_started = time.perf_counter()
        try:
            response = default_fetch(url, max_body_bytes=500_000)
        except Exception as exc:  # noqa: BLE001 - a benchmark records failures
            verdicts[type(exc).__name__] = verdicts.get(type(exc).__name__, 0) + 1
            continue
        latency.add((time.perf_counter() - call_started) * 1000)
        fetched += 1

        kind = detect_content_kind(response.body, response.headers)
        kinds[kind.value] = kinds.get(kind.value, 0) + 1
        verdict = classify_response(
            status=response.status,
            body=response.body,
            headers=response.headers,
            rules=ContentRules(min_body_bytes=500),
        ).verdict
        verdicts[verdict.value] = verdicts.get(verdict.value, 0) + 1

    wall = time.perf_counter() - started
    cpu = time.process_time() - cpu_before
    return BenchmarkResult(
        workload="real network (permitted public targets)",
        urls=len(targets),
        wall_seconds=wall,
        latency=latency,
        peak_rss_mb=_rss_mb(),
        cpu_seconds=cpu,
        verdicts=verdicts,
        extra={
            "fetched": fetched,
            "content_kinds": kinds,
            "targets": sorted({host for host, _ in targets}),
            "paid_calls": 0,
        },
    )


# ---------------------------------------------------------------------------
# discovery overhead
# ---------------------------------------------------------------------------


def discovery_overhead(observations: int = 5000) -> BenchmarkResult:
    """What discovery costs the render it rides along with.

    No target ratio is asserted. The point is that the overhead is measured and
    bounded rather than assumed to be small.
    """

    from web_scraper.discovery import DiscoveryCollector, ObservedRequest

    body = json.dumps(
        {"data": {"players": [{"id": i, "name": "x", "score": i} for i in range(20)]}}
    ).encode()
    resolver: Callable[..., Sequence[Any]] = lambda host, port, **kw: [  # noqa: E731
        (2, 1, 6, "", ("93.184.216.34", port))
    ]

    latency = Latencies()
    tracemalloc.start()
    cpu_before = time.process_time()
    started = time.perf_counter()

    collector = DiscoveryCollector(wanted_fields=("name", "score"), resolver=resolver)
    for i in range(observations):
        call = time.perf_counter()
        collector.observe(
            ObservedRequest(
                url=f"https://bench.example/api/e{i % 20}?page={i}",
                method="GET",
                status=200,
                content_type="application/json",
                resource_type="xhr",
                body=body,
                page_url=f"https://bench.example/p/{i % 200}",
            )
        )
        latency.add((time.perf_counter() - call) * 1000)

    candidates = collector.candidates()
    wall = time.perf_counter() - started
    cpu = time.process_time() - cpu_before
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    return BenchmarkResult(
        workload="discovery overhead (per observation)",
        urls=observations,
        wall_seconds=wall,
        latency=latency,
        peak_rss_mb=round(peak / (1024 * 1024), 1),
        cpu_seconds=cpu,
        verdicts={},
        extra={
            "candidates_kept": len(candidates),
            "per_observation_ms": _round(wall * 1000 / observations, 4),
        },
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "workload", choices=("synthetic", "real", "discovery-overhead", "providers")
    )
    parser.add_argument("--urls", type=int, default=1000)
    parser.add_argument("--json", action="store_true")
    args, rest = parser.parse_known_args(argv)

    if args.workload == "providers":
        from web_scraper.calibration.cli import main as calibration_main

        return calibration_main(["providers", *rest])

    if args.workload == "synthetic":
        result = synthetic(args.urls)
    elif args.workload == "real":
        result = real(args.urls)
    else:
        result = discovery_overhead(args.urls)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(result.describe())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
