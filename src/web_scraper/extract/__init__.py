"""Extraction layer: pull fields from the most redesign-resistant source first.

Chain order (reliability-layers.md, layer 2):
    JSON-LD -> app state (__NEXT_DATA__/__INITIAL_STATE__) -> meta/OG -> CSS -> heuristic

Each field carries its ``source`` (provenance), and critical fields can be put
to a two-extractor quorum so a silent extractor mismatch surfaces as a
``conflict`` instead of a wrong value.
"""

from web_scraper.extract.chain import (
    ExtractionResult,
    FieldValue,
    extract_fields,
    run_quorum,
)
from web_scraper.extract.normalize import normalize_value

__all__ = [
    "ExtractionResult",
    "FieldValue",
    "extract_fields",
    "normalize_value",
    "run_quorum",
]
