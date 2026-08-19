"""What produced this dataset, recorded so the answer survives the run.

Six months from now someone will ask why a dataset looks the way it does. By
then the config has changed, the profile has been edited, and the provider has
quietly altered its behaviour. Without a manifest the honest answer is "we don't
know", and every conclusion drawn from that data becomes unfalsifiable.

The manifest records identity, not content: commit, config hash, profile hash,
budget limits, the provider strategies in play and when their documentation was
last verified. Nothing here contains a credential, a cookie or a URL query
string — a manifest is meant to be readable by anyone debugging the run, which
is exactly why it must not carry secrets.

The provider documentation dates are in here for a specific reason. A vendor
changing its pricing or its response format is invisible at runtime; it shows up
as costs that stop adding up. Knowing that a run was executed against a contract
last verified four months earlier turns a mystery into a starting point.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any


def stable_hash(payload: Any) -> str:
    """A hash that does not change when a dict happens to iterate differently."""

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def git_commit(repo: Path | None = None) -> str | None:
    """The commit this ran from, or None outside a repository.

    Deliberately tolerant: a manifest missing its commit is far better than a run
    that refuses to start because it was launched from a tarball.
    """

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - git from PATH is the intent
            cwd=str(repo) if repo else None,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


@dataclass(frozen=True)
class ProviderFingerprint:
    """One vendor as it was configured for this run."""

    provider: str
    docs_verified_at: str | None
    strategies: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "docs_verified_at": self.docs_verified_at,
            "strategies": list(self.strategies),
        }


@dataclass(frozen=True)
class RunManifest:
    """Everything needed to explain, later, what this run was."""

    run_id: str
    started_at: str
    git_commit: str | None = None
    config_hash: str | None = None
    profile_hashes: dict[str, str] = field(default_factory=dict)
    input_url_count: int = 0
    daily_credit_limit: Decimal | None = None
    providers: tuple[ProviderFingerprint, ...] = ()
    notes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "git_commit": self.git_commit,
            "config_hash": self.config_hash,
            "profile_hashes": dict(self.profile_hashes),
            "input_url_count": self.input_url_count,
            "daily_credit_limit": (
                None if self.daily_credit_limit is None else str(self.daily_credit_limit)
            ),
            "providers": [p.to_dict() for p in self.providers],
            "notes": dict(self.notes),
        }

    def write(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n")
        return target

    def explain(self) -> str:
        lines = [
            f"run {self.run_id} started {self.started_at}",
            f"commit: {self.git_commit or 'unknown (not a git checkout)'}",
            f"config: {self.config_hash or '-'}",
            f"input URLs: {self.input_url_count}",
            f"daily credit limit: {self.daily_credit_limit if self.daily_credit_limit is not None else 'free run'}",
        ]
        for provider in self.providers:
            verified = provider.docs_verified_at or "NEVER VERIFIED"
            lines.append(
                f"  {provider.provider}: {len(provider.strategies)} strategies, "
                f"docs verified {verified}"
            )
        return "\n".join(lines)


def build_manifest(
    *,
    run_id: str,
    started_at: str,
    config: Mapping[str, Any] | None = None,
    profiles: Mapping[str, Any] | None = None,
    input_url_count: int = 0,
    daily_credit_limit: Decimal | None = None,
    providers: Sequence[Any] = (),
    repo: Path | None = None,
    notes: Mapping[str, Any] | None = None,
) -> RunManifest:
    """Assemble a manifest from the objects a run already has.

    ``providers`` are adapter instances; their strategy tables and documented
    verification dates are read off them, so the manifest cannot drift from the
    code that actually ran.
    """

    fingerprints = []
    for provider in providers:
        module = type(provider).__module__
        verified = getattr(
            __import__(module, fromlist=["DOCS_VERIFIED_AT"]), "DOCS_VERIFIED_AT", None
        )
        fingerprints.append(
            ProviderFingerprint(
                provider=getattr(provider, "name", type(provider).__name__),
                docs_verified_at=verified,
                strategies=tuple(s.to_dict() for s in provider.strategies()),
            )
        )

    return RunManifest(
        run_id=run_id,
        started_at=started_at,
        git_commit=git_commit(repo),
        config_hash=stable_hash(config) if config is not None else None,
        profile_hashes={name: stable_hash(profile) for name, profile in (profiles or {}).items()},
        input_url_count=input_url_count,
        daily_credit_limit=daily_credit_limit,
        providers=tuple(fingerprints),
        notes=dict(notes or {}),
    )
