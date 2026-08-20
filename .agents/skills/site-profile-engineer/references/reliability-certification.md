# Certification

Certification answers one question: *has this profile earned production
traffic?* Every check is a function of evidence that already exists, and none of
them consults an opinion.

## Verdicts

| verdict | meaning |
|---|---|
| `CERTIFIED` | every check passed |
| `CERTIFIED_WITH_WARNINGS` | nothing blocking, something worth reading first |
| `NOT_CERTIFIED` | a blocking check failed |
| `INSUFFICIENT_EVIDENCE` | the checks could not be run |

`INSUFFICIENT_EVIDENCE` is deliberately not a failure. "We do not know" and "it
is broken" call for different work, and collapsing them into one number is how a
profile with three samples gets described as 99% reliable.

**No percentage is ever produced.** There is no honest way to turn six pages
into a reliability figure, and the figure would be quoted long after the six
pages were forgotten.

## The checks

- profile parses, and carries no credential — including in a route's query string
- every URL class has cases, and they pass
- **at least one negative case per class**
- every critical field extracted on every positive case
- quorum conflicts under threshold
- no critical field resting only on a fragile or heuristic path
- an internal API route validated on **3+ distinct pages** with a stable schema
- pagination completeness tested where pagination exists
- freshness and promotion policies declared
- mutations run, and the profile reacted as its own field importance requires

## A profile cannot be certified if

- a critical field comes only from a heuristic;
- there are no negative tests;
- an API route was observed on one page;
- pagination is expected and completeness is untested;
- critical quorum conflicts are unresolved.

## Why negative cases are mandatory

A suite where every case is expected to succeed cannot distinguish a working
profile from one that says yes to everything. The second kind is the one that
quietly fills a dataset with the site's error page — styled, 200, and with a
`<title>`.

## Field importance

```yaml
validation:
  fields:
    score:       {importance: critical}    # missing -> FAIL
    sample_size: {importance: important}   # missing -> WARN
    description: {importance: optional}    # missing -> INFO
```

Percentage-of-fields-extracted is the metric everyone reaches for and it is
useless: losing a description and losing the price are both "one field", and
only one of them makes the dataset wrong. `required_fields` still works and
means `critical`.
