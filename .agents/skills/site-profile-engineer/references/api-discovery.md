# From an observed request to a route

A page that renders in a browser almost always fetched its data from somewhere.
That somewhere is cheaper, more stable and more complete than the markup around
it — and it is not a route until it has earned the name.

## The ladder

```
observed  -> is it noise?           analytics answer JSON too
          -> did it carry auth?     rendering authorised it, not us
          -> is it public?          a discovered URL is still an SSRF vector
          -> what shape is it?      schema signature, no values
          -> PROMISING
          -> seen on several DISTINCT pages, same shape
          -> VALIDATED
          -> a draft an operator reads
```

## Diversity is the requirement

Re-rendering the same page is not independent evidence. **Three distinct pages**
is the default before an endpoint may be used as a route. An endpoint that
answered once during one render is a detail of that render.

Where a class genuinely has one page, say so explicitly in the report rather
than lowering the bar quietly.

## Never replay authorisation

If the observed request carried a cookie or an `Authorization` header, the
session we were given for rendering authorised it — not us. Such a candidate is
rejected, and the rejection is recorded so "why did it not find the API?" has an
answer better than silence.

## Check before you render

`DiscoveryStore` persists across runs. Read it before deciding a page needs a
browser: the endpoint may already be validated, and rendering is the expensive
answer.
