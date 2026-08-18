# ParserUnix — cost-aware web scraper

A reliability- and cost-first framework for scraping one page or groups of URLs.
The guiding rule: build the **cheapest reliable collection path** that preserves
data quality and reports every unresolved URL. Paid providers are a last resort,
unlocked only when a classifier has proven the free levels do not work.

> Status: free core (levels **L0–L2**) under active development. Paid providers
> (L3–L4), the adaptive cost router, and the Rust L1 worker are deferred. See
> [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md).

## Repository layout

| Path | What it is |
|---|---|
| `src/web_scraper/` | The importable core: all logic lives here. |
| `.agents/skills/web-scraper/` | The portable Agent Skill (SKILL.md, references, templates) and thin CLI wrappers over the core. |
| `.claude/skills/web-scraper` | Symlink to the canonical skill (Claude Code picks it up here). |
| `tests/` | `unittest` suite (standard library only) and saved-response fixtures. |
| `docs/` | Implementation plan and research notes. |
| `tools/` | Repo maintenance scripts (e.g. the provider-doc staleness check). |

The scripts under `.agents/skills/web-scraper/scripts/` are **thin CLI wrappers**;
they re-export from `web_scraper` and contain no logic of their own.

## Core concepts

- **Verdicts** (`web_scraper.contracts.Verdict`): every response — including `200` —
  is classified by `triage.classify_response` before any retry/escalation decision.
- **Levels** L0 (JSON API / RSS / sitemap) → L1 (direct HTTP session) → L2 (browser)
  → L3/L4 (paid providers). Only a `BLOCKED`/`SOFT_BLOCK` verdict may raise the level,
  and never onto a paid level inside the free gateway.
- **Site Profiles** (`web_scraper.profiles`): per-domain declarative config —
  URL classes, canaries, routes, extractors, limits, freshness, promotion thresholds.
  Validated with no network access; secrets/cookies/tokens are rejected.
- **Fetch Gateway** (`web_scraper.fetchers.FetchGateway`): runs a URL through the
  free routes of its profile class, triaging after every attempt.

## Install

```bash
pip install -e .
# optional extras:
pip install -e '.[yaml]'      # PyYAML (a built-in fallback parser works without it)
pip install -e '.[browser]'   # Playwright — enables the L2 browser level
pip install -e '.[http]'      # Scrapling transports (real L1/L2)
```

To enable **L2** (JavaScript rendering and CSR API reconnaissance) you also need
the browser binary:

```bash
playwright install chromium
```

Without it the core still runs: L2 routes are reported as skipped rather than
failing a run, and browser tests skip automatically.

Installing exposes console commands `ws-probe`, `ws-triage`, `ws-profile`, `ws-budget`.
Without installing, the wrapper scripts locate the package via the repo layout or the
`WEB_SCRAPER_SRC` environment variable (see `scripts/_bootstrap.py`).

## Usage

```bash
# Static reconnaissance of a new target (safe, bounded, SSRF-protected):
ws-probe https://example.com/ --draft-profile draft.json

# Validate a Site Profile before any network use:
ws-profile validate .agents/skills/web-scraper/assets/templates/site-profile.yaml

# Classify a saved response:
ws-triage --status 200 --body-file page.html --canary '<article'
```

## Tests

Standard library only — no test dependencies:

```bash
python -m unittest discover -s tests -v
```

## Deployment (debian-151)

The production target is a single Debian server driven by a systemd timer:

1. `git pull` on `debian-151`, then `pip install -e .`.
2. A run configuration selects which URL groups, budget, and time window to process.
3. A systemd timer triggers the run within the nightly window; state (SQLite queue,
   snapshots) is local to the server.

Deployment units and the run loop land with Phase 2 of the plan; see
`.agents/skills/web-scraper/references/` for the operational references.

## Security & guardrails

- Scrape only public or authorized data; never bypass authentication or paywalls.
- Private, loopback, link-local, and cloud-metadata targets are blocked by default,
  on every redirect hop.
- Provider keys live in the environment or a secret store — never in code, profiles,
  logs, snapshots, or reports.
- `200 OK` is not success: content and extracted data are validated first.

## License

[MIT](LICENSE).
