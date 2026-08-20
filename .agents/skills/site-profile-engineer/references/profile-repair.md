# Repairing a degraded profile

The tempting move is to find the selector that stopped matching and write a new
one. It is tempting because it works, once, and it is usually wrong twice over:
the replacement is often *more* fragile than what it replaced, and the reason
the first one broke is that the site is being redesigned — which will happen
again.

## Prefer a sturdier source over a working one

If the DOM path broke and discovery has a validated JSON endpoint carrying the
same field, propose moving the field to the endpoint. A migration to structure
is the only repair that makes the *next* redesign cheaper.

Order of preference, same as building:

```
validated API > app state > JSON-LD > stable DOM > new CSS selector
```

## The flow

```
DEGRADED profile
  -> load the last known good version
  -> load recent snapshots and the current page
  -> read DiscoveryStore
  -> find what changed
  -> propose a candidate
  -> run the corpus
  -> run the mutations
  -> certify the candidate
```

## A repair never activates

The output is a **candidate version**. It replaces the trusted version only if
it certifies at least as well — a candidate that certifies with three new
warnings is worse than the one it would replace, even though both are
technically certified, and "technically certified" is how a regression ships.

## When there is nothing to propose

If the field is absent from every sampled page and no validated route supplies
it, say so. Whether the site removed it or renamed it is a question about the
site, not one a patch can answer, and inventing a selector to fill the gap
produces a column of nulls with a confident name.
