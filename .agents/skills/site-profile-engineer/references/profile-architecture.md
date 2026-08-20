# Profile architecture

## A package, not a file

```
site_profiles/<domain>/
├── profile.yaml     the claim
├── corpus.yaml      what it has to survive
├── evidence.json    what was measured
├── README.md        what a human needs to know
├── fixtures/        recorded responses the corpus runs on
└── history/         metadata of past versions
```

The package is what gets certified, versioned and rolled back — never the YAML
alone. A profile without the corpus it passed is a claim with the evidence
removed.

`registry.yaml` at the root says which packages exist, what state each is in,
and which version is currently trusted. No confidence numbers live there: a
`0.97` next to a site name invites being read as a probability when it is
usually a wish.

## URL classes

A class is a claim that a set of pages behave the same way. Split when they do
not: a listing, an entity page and a search result are three classes even on one
domain. One extractor for a whole domain is the failure classes exist to
prevent.

Name them after what the page *is* — `article`, `rankings`, `listing`,
`character` — not after the path that happens to serve it today.

## Route preference

```
validated public structured API
  > direct JSON
  > HTML structured data (JSON-LD, microdata)
  > app state (__NEXT_DATA__, __INITIAL_STATE__)
  > stable DOM
  > local browser
  > paid provider
```

Only when the route is **permitted, validated and stable**. A cheaper route that
is none of those is not cheaper; it is the same cost plus a future incident.

## Two sources for critical fields

Where the page offers one, take it. A critical field with JSON-LD *and* a CSS
selector survives the markup being rewritten, and the disagreement between them
is the earliest possible warning that something changed. Where the site offers
only one — a pure JSON API — say so in the report rather than inventing a second.
