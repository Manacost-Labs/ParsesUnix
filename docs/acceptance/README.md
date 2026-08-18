# Stage 1 acceptance — real domains

Free-core (L0–L2) runs against the three authorized domains, with Playwright/Chromium
installed so the L2 browser route is genuinely available. Each run seeded the
homepage, one real content URL, and one deliberately dead URL (to exercise
quarantine). No paid providers were used. Profiles and full reports are alongside
this file (`*.profile.json`, `*.report.json`).

## Results

| Domain | Shape | Live URLs resolved | Level used | Paid calls | 404 URL |
|---|---|---:|---|---:|---|
| **hsguru.com** | SSR behind Cloudflare bot-management | 1 / 2 | L1 | 0 | quarantined |
| **hs-manacost.ru** | SSR (WordPress) | 2 / 2 | L1 | 0 | quarantined |
| **hsreplay.net** | SPA shell + SSR metadata, Cloudflare | 2 / 2 | L1 | 0 | quarantined |

`hsguru.com/leaderboard/player-stats` is the only unresolved live URL: it returns
authorization-required (Battle.net sign-in) and is correctly terminal — the
system does not attempt to get around a login.

Extractor provenance came out as expected: `json_ld` and `meta` on hsreplay,
`heuristic` where no structured data is published.

## What this proves

- **Every live public page resolved on the cheapest level (L1), with zero paid
  requests.** The cost-safety invariant holds on real infrastructure, including
  two Cloudflare-fronted sites.
- **No silent skips.** Every seeded URL ended with a verdict or a durable status;
  the fabricated dead URLs were quarantined on all three domains.
- **Access control is respected** (the Battle.net-gated page stays unresolved).
- **The full chain runs end to end**: fetch → triage → extract (with quorum) →
  stage → whole-dataset validation → atomic promote.

## Corrections found during acceptance (both were real bugs in our triage)

Acceptance replaced two wrong conclusions with measured ones:

1. **`captcha` as a block signature — false positive.** It matched a WordPress
   theme's `tds_captcha` JS variable, flagging hs-manacost.ru as `SOFT_BLOCK`
   (a paid-escalation verdict) on an ordinary page.
2. **`/cdn-cgi/challenge-platform` as a block signature — false positive.**
   Cloudflare ships this JS-detection bundle on *normal* pages too: hsguru.com
   serves ~19k characters of real content alongside it. An earlier version of
   this document reported hsguru.com and hsreplay.net as "blocked" purely because
   of this marker. Verified with a real browser and with plain HTTP, both sites
   serve their content fine.

Both markers were removed; block detection now relies on interstitial/denial text
(`just a moment`, `sorry, you have been blocked`, `enable javascript and cookies
to continue`, …), the `cf-mitigated: challenge` header, and specific anti-bot
vendor markers. The rule this enforces: **a signature must appear only when the
response really is a block.**

## Notable finding: L2 is not automatically "stronger" than L1

On `hsreplay.net/decks/`, plain HTTP (L1) returns the page, while **headless
Chromium receives `307 → 403` and a Cloudflare Turnstile challenge**. A headless
browser is easier to fingerprint than a plain HTTP client, so escalating to L2 can
make things worse, not better. This is exactly why the ladder tries L1 first and
why alternative routes at the same level matter more than raw level.

That asymmetry also exposed a real gap, now fixed: browser recon used to report
"0 candidates" for a blocked navigation, which is indistinguishable from "this
site has no JSON API". `BrowserReconReport` now carries `navigation_status`,
`navigation_verdict` and `conclusive`, and adds an explicit note when the browser
never saw the real page.
