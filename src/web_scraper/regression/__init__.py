"""Regression detection: what changed between a saved baseline and today's page.

A scraper does not usually break loudly — it breaks by returning a valid-looking
page whose data quietly moved. This package answers one question precisely:

    *given the response we recorded when this profile worked, what is different
    now, and does it explain a coverage drop?*

It compares three layers, cheapest signal first:

1. **Response layer** — verdict, status, rendering class (SSR/CSR), canonical URL.
2. **Structure layer** — JSON-LD types, embedded app state, feeds, and for JSON
   routes the set of available paths (a lost path gets a replacement suggestion).
3. **Extraction layer** — the actual field values a profile's extractors produce,
   including *source drift* (a field that used to come from JSON-LD now coming
   from a CSS selector is a warning: the stable source disappeared).
"""

from web_scraper.regression.detect import (
    FieldChange,
    RegressionReport,
    StructureChange,
    compare_bodies,
    compare_saved_to_current,
    json_paths,
)

__all__ = [
    "FieldChange",
    "RegressionReport",
    "StructureChange",
    "compare_bodies",
    "compare_saved_to_current",
    "json_paths",
]
