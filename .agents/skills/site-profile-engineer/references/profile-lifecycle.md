# Profile lifecycle

```
DRAFT -> PROBING -> VALIDATING -> CERTIFIED
                                     |
                                regression
                                     v
                                  DEGRADED -> QUARANTINED
                                     ^            |
                                     +-- repair --+
```

| state | means | may carry traffic |
|---|---|---|
| `DRAFT` | written, never run against the site | no |
| `PROBING` | being investigated | no |
| `VALIDATING` | running the corpus, not yet passing | no |
| `CERTIFIED` | passed every deterministic check | **yes** |
| `DEGRADED` | production evidence says it is getting worse | no new activation |
| `QUARANTINED` | stopped; running it would produce bad data | no |

## The one edge that matters

`-> CERTIFIED` exists only as the result of `certify_profile`. There is no
argument, flag or override that means "approve anyway". The expensive failure in
this system is not a profile that fails certification; it is one that passes
because somebody was confident.

## Degradation is not a bad night

A site with a two-hour outage produces the same first hour of signal as a site
that has been redesigned. Reacting to the first costs a day of pointless work;
reacting late to the second costs a month of quietly wrong data. So degradation
needs **sustained** evidence across a window of runs, and a single transient
failure moves nothing.

## What never changes on its own

The **last known good** version. A profile degrading does not un-trust the
version that is running; it flags that the next version needs work. Production
keeps the thing that works until a candidate earns the place.
