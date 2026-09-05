# Offline profile comparison

Compare a candidate package profile with a baseline against the exact same
saved acceptance corpus and fixtures:

```text
ws-profile --root site_profiles compare example.test \
  --baseline-profile /reviewed/example.test/profile.yaml
```

The candidate defaults to `site_profiles/example.test/profile.yaml`; pass
`--candidate-profile PATH` to compare two explicitly named files. The command
prints JSON only. Each case contains the baseline and candidate outcomes plus
deltas for verdict, pass/fail, found fields, record count, and quorum conflicts.

This is an offline profile comparison, not a replay of two application or wheel
versions. It performs no fetches and does not write the profile registry, alter
profile state, activate either profile, or change fixtures.

Exit status is zero only when the candidate passes every case and the corpus is
unambiguous. A candidate failure remains non-zero even when the baseline also
fails. Invalid profiles/corpus, missing class coverage, missing fixtures, and
duplicate case IDs fail closed.
