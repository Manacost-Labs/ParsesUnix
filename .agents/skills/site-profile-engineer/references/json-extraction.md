# Extracting from JSON

When a route returns JSON, use `kind: json`. No DOM is built — parsing a large
API response into an HTML tree to find a field a dotted path already names is
work with no purpose.

## The path subset

```
data.character.name      nested objects
data.rows.0.name         array index
data.items               a whole list
data.players[*].name     one field from every element
```

Deliberately not JSONPath: filters, recursive descent and script expressions are
all ways for a profile to express something whose cost and result nobody can
predict.

## Prefer `[*]` over an index

`0.score` means "whatever happens to be first", which is not a field. Nothing
guarantees the ordering, and the day it changes the numbers stay plausible. The
fragility check scores an indexed path as `FRAGILE` for exactly this reason.

## Validation

`required_json_paths` is the triage-checkable proof that a JSON response is the
response you wanted. An empty list is not a passing fetch: a path that resolves
to nothing means the endpoint had nothing to say, and publishing zero rows over
yesterday's data is worse than publishing nothing at all.

## Types

A profile cannot notice a number quietly becoming a string unless it declares
types. The mutation suite demonstrates this on purpose; if the dataset depends
on comparisons or sorting, say so in the report.
