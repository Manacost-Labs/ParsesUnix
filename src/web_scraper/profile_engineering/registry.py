"""The one file that says which profiles exist and which may be trusted.

Without a registry, "is there a profile for this site?" is answered by looking
in a directory, and "may we use it?" is answered by opening the YAML and
forming an opinion. Both answers drift. The registry makes them a lookup, and
makes the trusted-version pointer something a run can read without parsing
every package on disk.

Two things it deliberately does not hold:

* **no confidence numbers.** A registry entry carries a state and the evidence
  hash that produced it. A number like 0.97 next to a site name invites reading
  it as a probability when it is usually a wish;
* **no credentials, ever.** The registry is committed, printed and pasted into
  tickets.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from web_scraper.profile_engineering.model import (
    PROFILE_SCHEMA_VERSION,
    LastKnownGood,
    ProfileIdentity,
    ProfilePackage,
    ProfileState,
)

REGISTRY_FILE = "registry.yaml"

#: Anything matching these in a registry file is refused rather than written.
_FORBIDDEN_KEYS = ("token", "key", "secret", "password", "cookie", "authorization")


@dataclass(frozen=True)
class RegistryEntry:
    """One site, as the registry sees it."""

    domain: str
    path: str
    state: ProfileState = ProfileState.DRAFT
    profile_version: int = 1
    last_verified: str = ""
    last_known_good: LastKnownGood | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": self.path,
            "status": self.state.value,
            "profile_version": self.profile_version,
        }
        if self.last_verified:
            payload["last_verified"] = self.last_verified
        if self.last_known_good is not None:
            payload["last_known_good"] = self.last_known_good.to_dict()
        if self.notes:
            payload["notes"] = self.notes
        return payload

    @classmethod
    def from_dict(cls, domain: str, payload: Mapping[str, Any]) -> RegistryEntry:
        lkg = payload.get("last_known_good")
        return cls(
            domain=domain,
            path=str(payload.get("path", f"{domain}/profile.yaml")),
            state=ProfileState(str(payload.get("status", ProfileState.DRAFT.value))),
            profile_version=int(payload.get("profile_version", 1)),
            last_verified=str(payload.get("last_verified", "")),
            last_known_good=(LastKnownGood.from_dict(lkg) if isinstance(lkg, Mapping) else None),
            notes=str(payload.get("notes", "")),
        )


@dataclass
class ProfileRegistry:
    """Every profile package under one root."""

    root: Path
    schema_version: int = PROFILE_SCHEMA_VERSION
    entries: dict[str, RegistryEntry] = field(default_factory=dict)

    # -- reading -----------------------------------------------------------

    @classmethod
    def load(cls, root: str | Path) -> ProfileRegistry:
        """Read the registry, or return an empty one for a fresh checkout."""

        directory = Path(root)
        path = directory / REGISTRY_FILE
        if not path.exists():
            return cls(root=directory)

        payload = _load_mapping(path)
        version = int(payload.get("profile_schema_version", PROFILE_SCHEMA_VERSION))
        if version > PROFILE_SCHEMA_VERSION:
            # Refusing beats guessing: a newer file may have moved a field this
            # reader would then silently interpret as its old meaning.
            raise ValueError(
                f"{path} is schema version {version}; this build understands "
                f"{PROFILE_SCHEMA_VERSION}. Upgrade before reading it."
            )
        sites = payload.get("sites") or {}
        if not isinstance(sites, Mapping):
            raise ValueError(f"{path}: 'sites' must be a mapping of domain to entry")
        return cls(
            root=directory,
            schema_version=version,
            entries={
                str(domain): RegistryEntry.from_dict(str(domain), spec)
                for domain, spec in sites.items()
                if isinstance(spec, Mapping)
            },
        )

    def get(self, domain: str) -> RegistryEntry | None:
        return self.entries.get(domain)

    def package(self, domain: str) -> ProfilePackage | None:
        """The package a registry entry points at."""

        entry = self.get(domain)
        if entry is None:
            return None
        return ProfilePackage(
            root=(self.root / entry.path).parent,
            identity=ProfileIdentity(
                domain=domain,
                profile_version=entry.profile_version,
                state=entry.state,
                last_verified_at=entry.last_verified,
            ),
        )

    def certified(self) -> list[RegistryEntry]:
        return [e for e in self.entries.values() if e.state is ProfileState.CERTIFIED]

    def needing_attention(self) -> list[RegistryEntry]:
        return [e for e in self.entries.values() if e.state.needs_attention]

    def __iter__(self) -> Iterator[RegistryEntry]:
        return iter(sorted(self.entries.values(), key=lambda e: e.domain))

    def __len__(self) -> int:
        return len(self.entries)

    # -- writing -----------------------------------------------------------

    def upsert(self, entry: RegistryEntry) -> RegistryEntry:
        self.entries[entry.domain] = entry
        return entry

    def save(self) -> Path:
        """Write the registry back, refusing anything that smells like a secret."""

        for entry in self.entries.values():
            _refuse_secrets(entry)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / REGISTRY_FILE
        path.write_text(self.render(), encoding="utf-8")
        return path

    def render(self) -> str:
        """YAML written by hand, because the package must not require a parser.

        The rest of the project can read YAML without PyYAML through the built-in
        loader; writing it the same way keeps the registry readable on a machine
        with no extras installed.
        """

        lines = [
            "# Which site profiles exist, and which of them may be trusted.",
            "# Written by `ws-profile`. Hand edits are fine; secrets are refused.",
            f"profile_schema_version: {self.schema_version}",
            "",
            "sites:",
        ]
        if not self.entries:
            lines.append("  {}")
            return "\n".join(lines) + "\n"

        for entry in sorted(self.entries.values(), key=lambda e: e.domain):
            lines.append(f"  {entry.domain}:")
            lines.append(f"    path: {entry.path}")
            lines.append(f"    status: {entry.state.value}")
            lines.append(f"    profile_version: {entry.profile_version}")
            if entry.last_verified:
                # Quoted: an unquoted ISO timestamp is parsed back as a datetime
                # by PyYAML and as a string by the built-in loader, so the same
                # registry would render two different ways on two machines.
                lines.append(f"    last_verified: {_quote(entry.last_verified)}")
            if entry.notes:
                lines.append(f"    notes: {_quote(entry.notes)}")
            lkg = entry.last_known_good
            if lkg is not None:
                lines.append("    last_known_good:")
                lines.append(f"      profile_version: {lkg.profile_version}")
                lines.append(f"      profile_hash: {lkg.profile_hash}")
                lines.append(f"      certified_at: {_quote(lkg.certified_at)}")
                lines.append(f"      evidence_hash: {lkg.evidence_hash}")
                if lkg.verdict:
                    lines.append(f"      verdict: {lkg.verdict}")
                lines.append(f"      warnings: {lkg.warnings}")
        return "\n".join(lines) + "\n"


def _quote(value: str) -> str:
    escaped = value.replace('"', '\\"')
    return f'"{escaped}"'


def _refuse_secrets(entry: RegistryEntry) -> None:
    haystack = " ".join((entry.path, entry.notes)).lower()
    for marker in _FORBIDDEN_KEYS:
        if marker in haystack:
            raise ValueError(
                f"registry entry for {entry.domain} mentions {marker!r}; the registry "
                "is committed and printed, so it never carries credentials"
            )


def _load_mapping(path: Path) -> Mapping[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml

        payload = yaml.safe_load(text)
    except ImportError:
        from web_scraper.profiles.yamlish import loads

        payload = loads(text)
    if not isinstance(payload, Mapping):
        raise ValueError(f"{path} does not contain a registry mapping")
    return payload
