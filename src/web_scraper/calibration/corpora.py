"""The corpus shipped with the package, and why each target is in it.

Every entry is a public target that permits this crawler, checked with the
project's own robots reader rather than assumed. Two things are deliberate:

* **The expectations are measured, not guessed.** Sizes, statuses and canaries
  come from a free direct fetch performed while writing this file. A benchmark
  whose "expected" column was invented would grade providers against fiction.
* **A domain that refuses is named, not dropped.** ``data.wowmeta.com`` serves
  its robots.txt behind a 403, which the RFC reads as disallow-all, so its JSON
  endpoint is excluded — recorded in ``skipped_by_policy`` so the empty
  cross-origin segment in the report has a reason attached to it.

An operator's own corpus replaces this one with ``--corpus``. This exists so
the command is runnable, and safe, out of the box.
"""

from __future__ import annotations

from web_scraper.calibration.corpus import Corpus, CorpusTarget, TargetKind
from web_scraper.contracts import ContentKind

#: Measured 2026-08-19 with a free stdlib fetch from the operator's own address.
EXAMPLE_CORPUS = Corpus(
    name="example-public-sandboxes",
    description=(
        "Public scraping sandboxes plus two live sites that permit this agent. "
        "Expectations measured directly, not assumed."
    ),
    skipped_by_policy={
        "data.wowmeta.com": (
            "robots.txt answers 403, which the RFC reads as disallow-all; the "
            "cross-origin JSON endpoint is therefore not fetched directly"
        ),
    },
    targets=(
        CorpusTarget(
            url="https://www.scrapethissite.com/pages/advanced/",
            domain="www.scrapethissite.com",
            url_class="page",
            kind=TargetKind.SSR_HTML,
            expected_content_kind=ContentKind.HTML,
            min_body_bytes=4000,
            canaries=("Advanced Topics",),
            critical_fields=("title",),
            notes="server-rendered, 9.8 KB measured",
        ),
        CorpusTarget(
            url="https://www.scrapethissite.com/pages/forms/?page_num=2",
            domain="www.scrapethissite.com",
            url_class="listing",
            kind=TargetKind.LISTING,
            expected_content_kind=ContentKind.HTML,
            min_body_bytes=20000,
            canaries=("hockey",),
            critical_fields=("title",),
            notes="page 2 of a paginated listing, 50 KB measured",
        ),
        CorpusTarget(
            url="https://www.scrapethissite.com/pages/simple/",
            domain="www.scrapethissite.com",
            url_class="listing",
            kind=TargetKind.LARGE_HTML,
            expected_content_kind=ContentKind.HTML,
            min_body_bytes=100000,
            canaries=("country-name",),
            critical_fields=("title",),
            notes="203 KB measured — large enough to catch truncation",
        ),
        CorpusTarget(
            url="https://www.scrapethissite.com/pages/ajax-javascript/?ajax=true&year=2015",
            domain="www.scrapethissite.com",
            url_class="rankings",
            kind=TargetKind.JSON_ENDPOINT,
            expected_content_kind=ContentKind.JSON,
            min_body_bytes=500,
            required_json_paths=("0.title", "0.year"),
            notes="the site's own AJAX endpoint: 16 objects, application/json",
        ),
        CorpusTarget(
            url="https://www.scrapethissite.com/pages/definitely-not-here-xyz",
            domain="www.scrapethissite.com",
            url_class="page",
            kind=TargetKind.DEAD_URL,
            expected_content_kind=ContentKind.HTML,
            expected_target_status=404,
            notes=(
                "the row that catches the defect that hit three adapters: a "
                "provider must report 404 as the TARGET's status"
            ),
        ),
        CorpusTarget(
            url="https://wowmeta.com/",
            domain="wowmeta.com",
            url_class="home",
            kind=TargetKind.CSR_SHELL,
            expected_content_kind=ContentKind.HTML,
            # The shell measures 2 035 bytes. Anything near that is the shell,
            # not the page, whatever status came with it — so the size IS the
            # test for whether rendering happened.
            min_body_bytes=10000,
            notes="client-rendered; unrendered shell measured at 2 035 bytes",
        ),
        CorpusTarget(
            url="https://hsreplay.net/",
            domain="hsreplay.net",
            url_class="home",
            kind=TargetKind.HARD_BLOCK,
            expected_content_kind=ContentKind.HTML,
            min_body_bytes=10000,
            canaries=("Hearthstone",),
            notes=(
                "anti-bot candidate. A direct fetch from the operator's own "
                "address succeeds (28 KB measured); whether it does from a "
                "provider's egress is the question this row asks"
            ),
        ),
    ),
)
