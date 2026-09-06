# Pagination runner

`collect_pages` is a transport-agnostic sequential cursor executor. It accepts
opaque tokens only, invokes one fetch at a time, and never treats a limit,
fetch failure, cursor loop, missing continuation, or record-count disagreement
as successful completion.

The cursor is checkpointed before the first request and after each fully
accepted page, within the same end-to-end time budget as fetches. A checkpoint
that runs out of that budget returns `checkpoint_timeout`; its own write errors
(including a storage `TimeoutError`) and cancellations propagate. A resumed
cursor must use the same scope, contain valid unique visited tokens, coherent
page/count metadata, and remain within `max_records`. The pending token is
retained after a fetch failure so callers can resume.

`requests_this_run` counts attempts when they begin, including a timed-out or
failed request. Records are deduplicated by their first valid key; a page that
would exceed `max_records` is not partially committed. Completion requires an
explicit `exhausted=True` page and, when supplied, an exact expected count.
