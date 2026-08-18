"""Failure fingerprints: recognise a failure we have seen before.

Route statistics answer "does this door open on this site?". They cannot answer
"we have never touched this site, but this failure looks exactly like the
Cloudflare managed challenge we already know how to get past". A fingerprint is
the shape of a failure, normalized so that incidental differences — a rotating
ray ID, a body a few bytes longer, a different article URL — collapse to the
same signature.

The store remembers, per fingerprint, which routes were seen failing and which
route eventually recovered the URL. That evidence orders routes faster on a
domain with no history of its own.

One hard boundary: a fingerprint may influence *route ordering* only. It never
produces a verdict, never overrides triage, and never authorises paid
escalation — those remain the classifier's decisions.
"""

from web_scraper.fingerprints.model import (
    FailureFingerprint,
    FingerprintRecord,
    body_size_bucket,
    fingerprint_attempt,
)
from web_scraper.fingerprints.store import FingerprintStore

__all__ = [
    "FailureFingerprint",
    "FingerprintRecord",
    "FingerprintStore",
    "body_size_bucket",
    "fingerprint_attempt",
]
