# Stage 1 acceptance — real domains

Free-core (L0–L2) run against the three authorized domains, run on 2026-08-18.
Each run seeded the homepage, one real content URL, and one deliberately dead URL
(to exercise quarantine). No paid providers were used. Profiles and full reports
are alongside this file (`*.profile.json`, `*.report.json`).

The acceptance question is not "did every page return data" but "did the system
classify each real response correctly and never spend money to get past a block".
On that bar it passes.

## Results

| Domain | Class (probe) | Homepage verdict @ L1 | 404 URL | Paid calls | Notes |
|---|---|---|---|---|---|
| **hs-manacost.ru** | SSR (WordPress) | **OK** → extracted → promoted | quarantined | 0 | Fully resolved free; 2/2 live URLs OK at L1, 2 clean rows via heuristic extractor. |
| **hsguru.com** | Cloudflare-fronted | **SOFT_BLOCK** (real `/cdn-cgi/challenge-platform`) | quarantined | 0 | Protected page `/leaderboard/player-stats` → `AUTH_REQUIRED` (login), correctly terminal. |
| **hsreplay.net** | Cloudflare managed challenge | **SOFT_BLOCK** (real `/cdn-cgi/challenge-platform`) | quarantined | 0 | Correct challenge detection; needs an L2 browser to pass. |

## What this proves

- **Correct start level.** Plain SSR (hs-manacost) is served at L1 and resolved
  end-to-end: fetch → triage OK → extract → stage → validate → atomic promote.
- **Cost safety holds on real infrastructure.** Two Cloudflare-fronted sites
  returned genuine challenge markers and were classified `SOFT_BLOCK`; the free
  core attempted a free browser escalation and **never made a paid request**
  (`paid_calls: 0` everywhere). This is the central invariant of the project.
- **No silent skips.** Every seeded URL ended with a verdict or a durable status;
  the fabricated dead URLs were quarantined on all three domains.
- **Access control is respected.** `hsguru.com/leaderboard/player-stats` returned
  authorization-required and was left terminal (no bypass attempt).
- **A real precision bug was found and fixed** during acceptance: a bare
  `captcha` signature matched a WordPress theme's `tds_captcha` JS variable and
  wrongly flagged hs-manacost as `SOFT_BLOCK`. Signatures were tightened to
  specific vendor challenge markers (see the triage change in the same session).

## Open item (environment, not a code defect)

hsguru.com and hsreplay.net sit behind Cloudflare's JS challenge. Passing it
requires **L2** — a real browser or a browser-fingerprinted client
(Playwright / Scrapling). Neither is installed in this local environment, so the
free core correctly stops at `SOFT_BLOCK` without paying. On `debian-151` with
`pip install -e '.[browser]' && playwright install chromium` (or `.[http]` for
Scrapling), the L2 route runs and these domains are expected to resolve for free.
This matches the plan's stated prerequisite for the Cloudflare-class domain.
