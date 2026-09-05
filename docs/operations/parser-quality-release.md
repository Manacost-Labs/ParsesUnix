# Parser quality patch 0.10.2

This is a bounded release of T04/T05 in the API and T11–T16 in the engine,
not completion of the full parser-quality audit. The user explicitly authorized
main integration and production rollout. No extra paid collection, new active source cohort,
production database repair, or all-source freshness claim is part of this patch.

## Contracts

- Required fields are checked for each staged row's URL class. Quorum never
  replaces required fields; previously accepted quorum-only profiles still work.
- Each class must meet its own completeness threshold. An absent class is not
  assumed deleted: this patch does not implement full-snapshot pagination.
- Critical or unclassified conflicts block promotion even without a baseline.
  Explicit noncritical conflicts retain `_extractor_conflicts` and the selected
  extractor's provenance. Identical field-specific observations count once.
- Corpus `records_field` explicitly names an extracted list of nonempty objects.
  Optional `record_identity_field` counts distinct string/integer identities;
  absent/invalid identities cannot prove completeness. Without an identity field,
  exact duplicate objects count once. Without a collection field, the existing
  single-record extraction path produces zero or one record, never column count.
- `expect_min_records` is enforced; `expect_values` checks typed JSON values,
  including identifiers. `0` and `false` are present values. Existing corpus files
  remain readable but must be rerun to establish evidence under the new rules.

Example corpus case:

```yaml
id: listing
url_class: cards
kind: normal
fixture: listing
expect_verdict: OK
records_field: cards
record_identity_field: id
expect_min_records: 1000
expect_values:
  patch: "example-patch"
```

## Reproducible artifact

The wheel was built twice with CPython 3.12.13, setuptools 82.0.1 and
`SOURCE_DATE_EPOCH=1788566400`, using `uv build --wheel --no-build-isolation`.
Both copies have SHA-256
`3ea80ec34a77d8c2d674c3add4918e5a78477aa6323c8042c832d89d803055ed`.
The API pins the release URL and this hash. The core tag and API commit identify
the source; an unlabelled old Docker image is not evidence of a source commit.

## Rollout and recovery

1. Run canonical `make check` for both projects and their relevant security checks.
   Obtain a fresh CRITICAL review against the cumulative diff. CI invokes the
   same canonical entrypoint; browser and additional Python versions remain CI jobs.
2. Publish the verified wheel from the reviewed core commit, then verify the
   downloaded artifact hash. Integrate the API pin and source fixes into `main`.
3. Build the API image once, label it with the full reviewed API commit, and
   record its immutable image ID. Smoke-test that image with no live environment,
   network, or data mounts. Never rebuild after this test for the same rollout.
4. Verify production is clean and still at the expected previous commit/image.
   Preserve the **actual running** image ID as a rollback tag, not a possibly
   different mutable `:local` tag. Advance host source to the exact reviewed SHA.
5. Tag the tested immutable candidate ID as `hs-data-api:local` and recreate only
   API using Compose `up -d --no-build --no-deps api`. Compare the running ID,
   installed core version, source hashes, health, timer exporter and public routes.
6. On failure, retag the saved old image ID, recreate only API, and verify restored
   health. If host source must be restored, first check it stayed clean, then
   switch to the recorded old commit in detached mode; do not reset/discard edits.
   Record both runtime and host-source state rather than reporting success early.

The standalone DatasetStore upgrade adds nullable staging metadata columns.
Clean/LKG schemas remain unchanged. A synthetic old→new→old exercise preserves
records and lets the old wheel publish again. Old Runner baseline statistics are
derived from clean records, not deserialized from the new schema-v2 report.
The embedded API uses response contracts and transport helpers, not DatasetStore;
this deployment therefore does not migrate production dataset tables.

## Open work

Full pagination/snapshot-vs-delta evidence, XPath handling, source context and
upstream freshness, multi-owner paid-budget recovery, durable publication/index
reconciliation, unified readers, PostgreSQL shadow-sync repair, and long-term
quality observation remain separate tasks. This patch does not certify all 99
datasets or change their schedules and spending limits.
