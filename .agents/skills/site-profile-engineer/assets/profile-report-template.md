# SITE PROFILE REPORT

**Site:** …
**Profile version:** … · **State:** …

## URL classes
| class | content | route | critical fields |
|---|---|---|---|

## Structured APIs
Endpoint, distinct pages observed, schema stability, discovery state. An
endpoint seen on one page is a lead, not a route.

## Extractors
Per field: source, locator, reliability (STABLE / MEDIUM / FRAGILE).

## Critical fields and quorum
Which critical fields have a second independent source, and which do not. A
field with one source is not disqualifying; hiding that it has one is.

## Acceptance corpus
Cases per class, negative cases, and every kind marked NOT APPLICABLE with its
reason.

## Mutation tests
Run, passed, and specifically what the profile would **not** notice.

## Fragile selectors
Every FRAGILE path, and whether a critical field depends on it.

## Certification
`CERTIFIED` / `CERTIFIED_WITH_WARNINGS` / `NOT_CERTIFIED` / `INSUFFICIENT_EVIDENCE`,
with the blocking checks listed. No percentage.

## Known limitations
What was not tested, and why. A gap named is a gap somebody can close.

## Recommended production action
ACTIVATE / KEEP LKG / REVIEW
