# Provider calibration

**Session** `live-zyte-unbounded` · **live network` · commit `7f4b9a0350d7b94cc61e81d3eb7f897e79f86db5`
**Corpus** `example-public-sandboxes` (sha256:6551b42d04426e59) — 14 target(s) across 3 domain(s)

## Session totals

- planned calls: 42
- attempted: 23  ·  ineligible: 1  ·  early-stopped: 18
- validated results: 17
- spend: exact $0, provisional $0, 23 call(s) with unknown cost
- USD per validated result (session): not computable
- status fidelity: 100.0%

## Skipped by policy

- `data.wowmeta.com` — robots.txt answers 403, which the RFC reads as disallow-all; the cross-origin JSON endpoint is therefore not fetched directly

## Strategies

| strategy | domain/class | validated | Wilson LB | p50 | p95 | USD/request | USD/validated |
|---|---|---:|---:|---:|---:|---:|---:|
| `zyte:browser` | hsreplay.net/home | 1/1 | 0.206 | 37301.0 | 37301.0 | - | **1 call(s) with unattributed cost** |
| `zyte:browser` | wowmeta.com/home | 1/1 | 0.206 | 6291.0 | 6291.0 | - | **1 call(s) with unattributed cost** |
| `zyte:browser` | www.scrapethissite.com/listing | 1/1 | 0.206 | 7152.0 | 7152.0 | - | **1 call(s) with unattributed cost** |
| `zyte:browser` | www.scrapethissite.com/page | 0/0 | 0.000 | 5978.0 | 5978.0 | - | **2 call(s) with unattributed cost** |
| `zyte:browser_capture` | hsreplay.net/home | 1/1 | 0.206 | 45995.0 | 45995.0 | - | **1 call(s) with unattributed cost** |
| `zyte:browser_capture` | wowmeta.com/home | 1/1 | 0.206 | 5001.0 | 5001.0 | - | **1 call(s) with unattributed cost** |
| `zyte:browser_capture` | www.scrapethissite.com/listing | 1/1 | 0.206 | 9214.0 | 9214.0 | - | **1 call(s) with unattributed cost** |
| `zyte:browser_capture` | www.scrapethissite.com/page | 0/0 | 0.000 | 6759.0 | 6759.0 | - | **2 call(s) with unattributed cost** |
| `zyte:http` | hsreplay.net/home | 1/1 | 0.206 | 1376.0 | 1376.0 | - | **1 call(s) with unattributed cost** |
| `zyte:http` | www.scrapethissite.com/listing | 4/4 | 0.510 | 1574.0 | 1814.0 | - | **4 call(s) with unattributed cost** |
| `zyte:http` | www.scrapethissite.com/page | 3/3 | 0.439 | 1115.0 | 2092.0 | - | **5 call(s) with unattributed cost** |
| `zyte:http` | www.scrapethissite.com/rankings | 3/3 | 0.439 | 1077.0 | 1175.0 | - | **3 call(s) with unattributed cost** |

## Status fidelity

Did the vendor report the status the site actually gives? This is the measurement that catches the defect class which cost this project the most: a dead URL reported as a success is re-fetched, and re-billed, on every run for as long as the crawl exists.

| provider | correct | checked | fidelity |
|---|---:|---:|---:|
| `zyte` | 23 | 23 | 100.0% |

## Winner by segment

**csr_shell** — no certified winner. Ahead on the evidence so far: `zyte:browser` at $unpriced/validated, confidence bound 0.207. Certifying 0.50 needs 4 consecutive validated results on this segment.
**dead_url** — no certified winner. Ahead on the evidence so far: `zyte:http` at $unpriced/validated, confidence bound 0.000. Certifying 0.50 needs 4 consecutive validated results on this segment.
**hard_block** — no certified winner. Ahead on the evidence so far: `zyte:http` at $unpriced/validated, confidence bound 0.207. Certifying 0.50 needs 4 consecutive validated results on this segment.
**json_endpoint** — no certified winner. Ahead on the evidence so far: `zyte:http` at $unpriced/validated, confidence bound 0.438. Certifying 0.50 needs 4 consecutive validated results on this segment.
**large_html** — no certified winner. Ahead on the evidence so far: `zyte:http` at $unpriced/validated, confidence bound 0.207. Certifying 0.50 needs 4 consecutive validated results on this segment.
**listing** — no certified winner. Ahead on the evidence so far: `zyte:http` at $unpriced/validated, confidence bound 0.438. Certifying 0.50 needs 4 consecutive validated results on this segment.
**ssr_html** — no certified winner. Ahead on the evidence so far: `zyte:http` at $unpriced/validated, confidence bound 0.438. Certifying 0.50 needs 4 consecutive validated results on this segment.

## Vendor concentration

100.0% of paid calls went to `zyte`. Reported, not balanced: whether that concentration is acceptable is the operator's call, not the router's.

## Discovery

- `zyte:browser_capture`: 12 candidate(s), 6 validated, from 12 observed request(s) over 2 page(s)
