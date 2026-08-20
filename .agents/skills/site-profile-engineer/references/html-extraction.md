# Extracting from HTML

Source order, most durable first:

1. **JSON-LD** — a structure the site publishes on purpose, for search engines
   that will notice if it breaks.
2. **App state** — `__NEXT_DATA__`, `__INITIAL_STATE__`. The same data the page
   renders from, so it cannot disagree with what the user sees.
3. **Semantic meta** — OpenGraph and friends. Shallow but stable.
4. **Stable CSS** — ids, `data-*` attributes the page's own code uses, semantic
   elements.
5. **Heuristic** — last resort, and never the only source for a critical field.

## Selector reliability

| | example | why |
|---|---|---|
| `STABLE` | `#main-content`, `[data-spec-id]` | somebody chose it on purpose |
| `MEDIUM` | `.product-title`, `article time` | a name, but not a contract |
| `FRAGILE` | `div > div:nth-child(3)`, `.css-1x2y3z4` | describes today's layout, or a build artefact |

Penalised: positional selectors, generated class names, deep descendant chains,
long direct-child chains. A `data-testid` is worth something and not much — it
is a promise to the site's own test suite, not to us, and it is deleted when the
test is.

A **critical** field may not rest on a `FRAGILE` path alone. It may rest on a
fragile one plus a stable one; that is what two sources are for.

## Canaries

A canary proves the page is the page. Choose a structural marker or something
specific to the class — not a phrase that appears in the footer of every page on
the site. `World of Warcraft` on a WoW site proves nothing; `<article` or a
class-specific heading does.

Match outside `<script>`: a value found only in a JS blob is not proof the page
rendered.
