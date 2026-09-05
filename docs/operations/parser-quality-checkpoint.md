# Parser quality checkpoint

Date: 2026-09-04

This checkpoint records the delivered ParsesUnix T11–T13 implementation. It
does not represent a package release, deployment, production repair, or live
provider run. The public package remains version 0.10.1, and the HS API wheel
pin was not changed.

## Delivered behavior

- Runner and profile acceptance use the same `extract_response` path. Response
  headers and detected content kind are passed through quorum checks, so valid
  JSON is extracted as JSON in both paths without an unnecessary DOM parse.
- Runner staging stores extractor provenance. Two full, isolated Runner runs
  were exercised with changed JSON; the second run published the changed title,
  retained typed data, and exposed current/baseline provenance in drift data.
- Flattened staging rows are read as records rather than as nested `data`
  objects. Schema drift now counts missing keys across the union of field names
  and does not count valid `0` or `False` values as missing.
- Schema snapshot metadata is version 2. Old snapshots remain readable; their
  null-rate statistics are explicitly unknown, while deterministic record
  count, critical-field, type, provenance, and pagination checks remain active
  and can block promotion. Unsupported future versions are rejected.

## Verification

- Focused verification: 62 tests passed; Ruff and mypy passed.
- Final `make check` passed: Ruff checks and formatting (198 files), mypy
  (126 source files), and 1201 unittest cases with 8 existing skips. Test
  transports are fixtures; injected error logs are expected negative cases.
- Gitleaks reported no leaks over the 87-commit scan. OSV reported “No package
  sources found”; this is not dependency coverage and is not treated as a
  security pass.

The Luna Context Scout and independent review gates ran. Their required
corrections—header propagation in acceptance, explicit legacy-baseline
handling, and changed-JSON/provenance Runner coverage—were applied. Initial
ParsesUnix and API baselines were clean and the implementation was isolated
from other worktrees.

## Remaining scope

T14–T18, budget and acquisition safeguards, versioned publication, projection
consistency, broader source coverage, package release/integration, shadow
comparison, and staged production rollout remain open under the full plan.
# Next quality checkpoint

Version 0.10.2 adds the required-field, quorum-conflict and record-acceptance
contracts described in [the release runbook](parser-quality-release.md).
The earlier checkpoint below remains a historical verification record.
