# Pagination

The mechanics are easy. The hard part is knowing you saw everything.

## Completeness

"The crawl ended" and "the crawl completed" are the same observation and
different facts. A profile that paginates has to declare how it knows:

- an expected count the site publishes, compared against what was collected;
- a last page that explicitly carries no cursor;
- a natural key set that stops growing.

Without one of those, an incomplete crawl looks exactly like a complete one, and
the dataset silently loses its tail.

## The empty-tail trap

Many sites answer `200` with an empty list for any page beyond the last. A
crawler that stops on an error never stops. Stop on an empty page, and treat a
depth limit as an **alert**, not a normal ending.

## Not applicable is a complete answer

A class with one page has nothing to paginate. Declare that in `corpus.yaml`:

```yaml
not_applicable:
  - url_class: article
    kind: pagination
    reason: an article is a single page; there is nothing to paginate
```

Omitting it instead leaves "there is no pagination here" and "nobody tested
pagination" looking identical, and only one of them is fine.
