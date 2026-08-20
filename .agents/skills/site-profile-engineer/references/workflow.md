# Workflow

## 1. Inspect the registry

```bash
ws-profile list
```

If the site is there, load it and its last known good version. You are amending
something that works, not starting fresh, and the difference changes every
decision below.

## 2. Check policy

`robots.txt`, terms, and the authorization boundary. A domain that disallows
this agent is `SKIPPED BY POLICY` — a complete answer, and the end of the task.

## 3. Probe representative URLs

```bash
ws-probe <url> --discover-api
```

Several, not one. A URL class is a claim that a set of pages behave the same
way; a single page cannot support it.

## 4. Read the ContentKind

HTML, JSON, or a client-rendered shell. Measured from the body, not from the
`Content-Type` — plenty of APIs answer JSON as `text/plain`, and extracting with
the wrong assumption does not raise, it silently returns nothing.

## 5. Check DiscoveryStore before rendering

An already-validated endpoint is cheaper and steadier than any render. Rendering
is the expensive answer, and it should be reached for after the cheap ones are
ruled out rather than first.

## 6–7. Classes and routes

Group pages that behave alike; split those that do not. Then build the route
hierarchy per class, structured sources first.

## 8–10. Extractors, importance, quorum

Structured sources first. Declare each field `critical` / `important` /
`optional` from what the data is for. Give critical fields a second source where
the page offers one.

## 11. Corpus

Normal, a different entity, a layout variant, empty, 404, pagination where it
exists. At least one negative case per class, or certification refuses.

## 12–13. Test and mutate

```bash
ws-profile test <site>
ws-profile certify <site>     # runs the mutations itself
```

Mutations are run, never supplied. A file asserting that mutations passed would
satisfy the one check that exists to prove breakage is noticed.

## 14–16. Evidence, certification, report

Evidence records what was measured — counts, hashed identifiers, schema
signatures — and never bodies, values or URLs. Then certify, then report, then
let an operator activate it.

## Cost

Everything above is free. A paid provider enters only on proven `BLOCKED` or
`SOFT_BLOCK`, through the existing budget and caps, and never without saying so
first.
