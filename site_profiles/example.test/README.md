# example.test

**State:** CERTIFIED_WITH_WARNINGS · profile v1 · synthetic package

A worked example, and the package the end-to-end tests run against. The site is
not real: every fixture under `fixtures/` was generated. That is deliberate — an
example that depends on somebody else's website stops working the day they
redesign it, and then it teaches the wrong lesson.

## Supported URL classes

| class | content | route | critical fields |
|---|---|---|---|
| `article` | HTML | direct HTTP (L1) | `title` |
| `rankings` | JSON | JSON API (L0) | `spec`, `score` |

## Data collected

Fields carry an importance rather than a flat "required" list. `title` missing
means the record is not a record; `author` missing means it is poorer; a missing
`description` is information. That difference is what stops a percentage of
fields extracted from being the metric anybody looks at.

## Routes

`rankings` reads a JSON endpoint directly and never builds a DOM. `article` is
server-rendered HTML with **two** sources for its critical field: JSON-LD first,
a CSS selector second. The `article-layout-variant` case exists to prove that
choice — it renames the CSS class, and the title survives.

## Known limitations

- **`rankings` has no second source.** A pure JSON API offers nothing to
  cross-check against, so a silent change in the endpoint would not be visible
  until the values themselves looked wrong. Recorded as a warning rather than
  hidden.
- **The profile would not notice a type change.** A score quietly becoming
  `"91.2"` instead of `91.2` keeps flowing through, compares wrong and sorts
  wrong. Two mutations demonstrate this, and both are in `evidence.json` under
  `not_noticed`. Fixing it needs declared field types, which this example does
  not have.
- The corpus has no pagination case. Both classes declare why in `corpus.yaml`
  rather than leaving it out — "there is nothing to paginate" and "nobody tested
  pagination" look identical in a corpus that simply omits it.

## Last certification

`CERTIFIED_WITH_WARNINGS` — 7 corpus cases, 3 of them negative, 16 mutations of
which 14 produced the required reaction. See `evidence.json`.
