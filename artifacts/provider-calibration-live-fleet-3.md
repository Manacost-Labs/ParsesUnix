# Provider calibration

**Session** `live-fleet-3` · **live network` · commit `7f4b9a0350d7b94cc61e81d3eb7f897e79f86db5`
**Corpus** `example-public-sandboxes` (sha256:6551b42d04426e59) — 14 target(s) across 3 domain(s)

## Session totals

- planned calls: 252
- attempted: 103  ·  ineligible: 92  ·  early-stopped: 57
- validated results: 64
- spend: exact $0.258380, provisional $0, 33 call(s) with unknown cost
- USD per validated result (session): not computable
- status fidelity: 96.9%

## Skipped by policy

- `data.wowmeta.com` — robots.txt answers 403, which the RFC reads as disallow-all; the cross-origin JSON endpoint is therefore not fetched directly

## Strategies

| strategy | domain/class | validated | Wilson LB | p50 | p95 | USD/request | USD/validated |
|---|---|---:|---:|---:|---:|---:|---:|
| `brightdata:unlocker` | hsreplay.net/home | 1/1 | 0.206 | 5890.0 | 5890.0 | - | **1 call(s) with unattributed cost** |
| `brightdata:unlocker` | www.scrapethissite.com/listing | 4/4 | 0.510 | 2720.0 | 10292.0 | - | **4 call(s) with unattributed cost** |
| `brightdata:unlocker` | www.scrapethissite.com/page | 3/3 | 0.439 | 1978.0 | 5012.0 | - | **5 call(s) with unattributed cost** |
| `brightdata:unlocker` | www.scrapethissite.com/rankings | 3/3 | 0.439 | 1394.0 | 5194.0 | - | **3 call(s) with unattributed cost** |
| `brightdata:unlocker_render` | hsreplay.net/home | 0/0 | 0.000 | 3165.0 | 3165.0 | - | **1 call(s) with unattributed cost** |
| `brightdata:unlocker_render` | wowmeta.com/home | 0/0 | 0.000 | 3621.0 | 3621.0 | - | **1 call(s) with unattributed cost** |
| `brightdata:unlocker_render` | www.scrapethissite.com/listing | 4/4 | 0.510 | 18118.0 | 18738.0 | - | **4 call(s) with unattributed cost** |
| `brightdata:unlocker_render` | www.scrapethissite.com/page | 3/3 | 0.439 | 14222.0 | 17445.0 | - | **5 call(s) with unattributed cost** |
| `brightdata:unlocker_render` | www.scrapethissite.com/rankings | 3/3 | 0.439 | 12874.0 | 18644.0 | - | **3 call(s) with unattributed cost** |
| `firecrawl:auto` | wowmeta.com/home | 1/1 | 0.206 | 1626.0 | 1626.0 | 0.000830 | 0.000830 |
| `firecrawl:auto` | www.scrapethissite.com/page | 0/1 | 0.000 | 1461.0 | 1461.0 | - | **1 call(s) with unattributed cost** |
| `firecrawl:basic` | wowmeta.com/home | 1/1 | 0.206 | 1543.0 | 1543.0 | 0.000830 | 0.000830 |
| `firecrawl:basic` | www.scrapethissite.com/page | 0/0 | 0.000 | 1498.0 | 1498.0 | 0.000830 | **no validated result to divide by** |
| `firecrawl:cached` | wowmeta.com/home | 1/1 | 0.206 | 862.0 | 862.0 | 0.000830 | 0.000830 |
| `firecrawl:cached` | www.scrapethissite.com/page | 0/0 | 0.000 | 691.0 | 691.0 | 0.000830 | **no validated result to divide by** |
| `firecrawl:enhanced` | wowmeta.com/home | 1/1 | 0.206 | 2490.0 | 2490.0 | 0.000830 | 0.000830 |
| `firecrawl:enhanced` | www.scrapethissite.com/page | 0/0 | 0.000 | 2308.0 | 2308.0 | 0.000830 | **no validated result to divide by** |
| `scrape.do:normal` | hsreplay.net/home | 1/1 | 0.206 | 5459.0 | 5459.0 | 0.000290 | 0.000290 |
| `scrape.do:normal` | wowmeta.com/home | 0/1 | 0.000 | 328.0 | 328.0 | 0.000290 | **no validated result to divide by** |
| `scrape.do:normal` | www.scrapethissite.com/listing | 4/4 | 0.510 | 2228.0 | 3354.0 | 0.000290 | 0.000290 |
| `scrape.do:normal` | www.scrapethissite.com/page | 3/5 | 0.231 | 663.0 | 1056.0 | - | **2 call(s) with unattributed cost** |
| `scrape.do:normal` | www.scrapethissite.com/rankings | 3/3 | 0.439 | 714.0 | 1475.0 | 0.000290 | 0.000290 |
| `scrape.do:render` | wowmeta.com/home | 0/1 | 0.000 | 6338.0 | 6338.0 | 0.001450 | **no validated result to divide by** |
| `scrape.do:render` | www.scrapethissite.com/listing | 1/1 | 0.206 | 3951.0 | 3951.0 | 0.001450 | 0.001450 |
| `scrape.do:render` | www.scrapethissite.com/page | 0/0 | 0.000 | 6694.0 | 6694.0 | 0.001450 | **no validated result to divide by** |
| `scrape.do:super` | hsreplay.net/home | 1/1 | 0.206 | 1151.0 | 1151.0 | 0.002900 | 0.002900 |
| `scrape.do:super` | www.scrapethissite.com/listing | 1/1 | 0.206 | 1219.0 | 1219.0 | 0.002900 | 0.002900 |
| `scrape.do:super` | www.scrapethissite.com/page | 0/2 | 0.000 | - | - | - | **2 call(s) with unattributed cost** |
| `scrape.do:super_render` | hsreplay.net/home | 1/1 | 0.206 | 11308.0 | 11308.0 | 0.007250 | 0.007250 |
| `scrape.do:super_render` | wowmeta.com/home | 0/1 | 0.000 | 1460.0 | 1460.0 | 0.007250 | **no validated result to divide by** |
| `scrape.do:super_render` | www.scrapethissite.com/page | 0/0 | 0.000 | 5077.0 | 5077.0 | 0.007250 | **no validated result to divide by** |
| `zenrows:auto` | hsreplay.net/home | 1/1 | 0.206 | 4943.0 | 4943.0 | 0.001000 | 0.001000 |
| `zenrows:auto` | wowmeta.com/home | 0/1 | 0.000 | 2186.0 | 2186.0 | 0.001000 | **no validated result to divide by** |
| `zenrows:auto` | www.scrapethissite.com/listing | 4/4 | 0.510 | 4291.0 | 4460.0 | 0.001000 | 0.001000 |
| `zenrows:auto` | www.scrapethissite.com/page | 3/3 | 0.439 | 19286.0 | 20591.0 | 0.010600 | 0.017667 |
| `zenrows:auto` | www.scrapethissite.com/rankings | 3/3 | 0.439 | 2527.0 | 3918.0 | 0.001000 | 0.001000 |
| `zenrows:basic` | hsreplay.net/home | 1/1 | 0.206 | 1154.0 | 1154.0 | 0.001000 | 0.001000 |
| `zenrows:basic` | wowmeta.com/home | 0/1 | 0.000 | 806.0 | 806.0 | 0.001000 | **no validated result to divide by** |
| `zenrows:basic` | www.scrapethissite.com/listing | 4/4 | 0.510 | 1344.0 | 1958.0 | 0.001000 | 0.001000 |
| `zenrows:basic` | www.scrapethissite.com/page | 3/3 | 0.439 | 812.0 | 5442.0 | 0.001000 | 0.001667 |
| `zenrows:basic` | www.scrapethissite.com/rankings | 3/3 | 0.439 | 642.0 | 1944.0 | 0.001000 | 0.001000 |
| `zenrows:js` | wowmeta.com/home | 0/1 | 0.000 | 2140.0 | 2140.0 | 0.005000 | **no validated result to divide by** |
| `zenrows:js` | www.scrapethissite.com/listing | 1/1 | 0.206 | 3850.0 | 3850.0 | 0.005000 | 0.005000 |
| `zenrows:js` | www.scrapethissite.com/page | 0/0 | 0.000 | 3210.0 | 3210.0 | 0.005000 | **no validated result to divide by** |
| `zenrows:js_premium` | hsreplay.net/home | 1/1 | 0.206 | 18774.0 | 18774.0 | 0.025000 | 0.025000 |
| `zenrows:js_premium` | wowmeta.com/home | 0/1 | 0.000 | 2557.0 | 2557.0 | 0.025000 | **no validated result to divide by** |
| `zenrows:js_premium` | www.scrapethissite.com/page | 0/0 | 0.000 | 7672.0 | 7672.0 | 0.025000 | **no validated result to divide by** |
| `zenrows:premium` | hsreplay.net/home | 0/1 | 0.000 | 18907.0 | 18907.0 | 0.000000 | **no validated result to divide by** |
| `zenrows:premium` | www.scrapethissite.com/page | 0/1 | 0.000 | 6453.0 | 6453.0 | - | **1 call(s) with unattributed cost** |

## Status fidelity

Did the vendor report the status the site actually gives? This is the measurement that catches the defect class which cost this project the most: a dead URL reported as a success is re-fetched, and re-billed, on every run for as long as the crawl exists.

| provider | correct | checked | fidelity |
|---|---:|---:|---:|
| `firecrawl` | 10 | 10 | 100.0% |
| `scrape.do` | 22 | 22 | 100.0% |
| `zenrows` | 37 | 38 | 97.4% |
| `brightdata` | 25 | 27 | 92.6% |

## Winner by segment

**csr_shell** — no certified winner. Ahead on the evidence so far: `firecrawl:basic` at $0.000830/validated, confidence bound 0.207. Certifying 0.50 needs 4 consecutive validated results on this segment.
**dead_url** — no certified winner. Ahead on the evidence so far: `brightdata:unlocker` at $unpriced/validated, confidence bound 0.000. Certifying 0.50 needs 4 consecutive validated results on this segment.
**hard_block** — no certified winner. Ahead on the evidence so far: `scrape.do:normal` at $0.000290/validated, confidence bound 0.207. Certifying 0.50 needs 4 consecutive validated results on this segment.
**json_endpoint** — no certified winner. Ahead on the evidence so far: `scrape.do:normal` at $0.000290/validated, confidence bound 0.438. Certifying 0.50 needs 4 consecutive validated results on this segment.
**large_html** — no certified winner. Ahead on the evidence so far: `scrape.do:normal` at $0.000290/validated, confidence bound 0.207. Certifying 0.50 needs 4 consecutive validated results on this segment.
**listing** — no certified winner. Ahead on the evidence so far: `scrape.do:normal` at $0.000290/validated, confidence bound 0.438. Certifying 0.50 needs 4 consecutive validated results on this segment.
**ssr_html** — no certified winner. Ahead on the evidence so far: `scrape.do:normal` at $0.000290/validated, confidence bound 0.438. Certifying 0.50 needs 4 consecutive validated results on this segment.

## Vendor concentration

37.9% of paid calls went to `zenrows`. Reported, not balanced: whether that concentration is acceptable is the operator's call, not the router's.
