"""Loader for the curated offline data files.

The YAML in ``knowledge/data`` is packaged with the wheel. It is deliberately
*advisory*: analyzers prefer facts read from the project's own ``node_modules``
and only fall back here, carrying the ``source``/``confidence`` markers into the
report so a developer can see where a claim came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..utils.io import read_yaml
from ..utils.semver import Version, coerce

DATA_DIR = Path(__file__).parent / "data"


@dataclass(frozen=True, slots=True)
class LibrarySignature:
    name: str
    label: str
    match: str = "exact"

    def matches(self, package: str) -> bool:
        if self.match == "prefix":
            return package.startswith(self.name)
        return package == self.name


@dataclass(frozen=True, slots=True)
class DeprecatedPackage:
    name: str
    reason: str
    replacement: str | None
    severity: str
    source: str | None = None
    confidence: str = "medium"
    applies_when_rn_at_least: str | None = None

    def applies(self, rn_version: Version | None) -> bool:
        if self.applies_when_rn_at_least is None:
            return True
        floor = coerce(self.applies_when_rn_at_least)
        if floor is None or rn_version is None:
            return True
        return rn_version >= floor


@dataclass(frozen=True, slots=True)
class PermissionRequirement:
    package: str
    ios_keys: tuple[str, ...]
    android_permissions: tuple[str, ...]
    required: bool
    note: str | None = None
    source: str | None = None


@dataclass(frozen=True, slots=True)
class TargetSdkRequirement:
    target_sdk: int
    effective: date
    enforce: bool
    confidence: str

    @property
    def in_force(self) -> bool:
        return self.enforce and self.effective <= date.today()


@dataclass(frozen=True, slots=True)
class RNCompatEntry:
    series: str
    react: str | None
    node: str | None
    confidence: str


@dataclass(frozen=True, slots=True)
class KnowledgeData:
    """Everything loaded from ``knowledge/data``."""

    library_signatures: dict[str, tuple[LibrarySignature, ...]] = field(default_factory=dict)
    deprecated: tuple[DeprecatedPackage, ...] = ()
    javascript_only: frozenset[str] = frozenset()
    permissions: tuple[PermissionRequirement, ...] = ()
    target_sdk_requirements: tuple[TargetSdkRequirement, ...] = ()
    target_sdk_source: str | None = None
    privacy_manifest_effective: date | None = None
    privacy_manifest_source: str | None = None
    rn_compat: dict[str, RNCompatEntry] = field(default_factory=dict)
    rn_compat_note: str | None = None

    # -- queries -----------------------------------------------------------
    def roles_for(self, package: str) -> list[tuple[str, str]]:
        """``[(role, label), ...]`` for one package name."""
        found: list[tuple[str, str]] = []
        for role, signatures in self.library_signatures.items():
            for signature in signatures:
                if signature.matches(package):
                    found.append((role, signature.label))
                    break
        return found

    def deprecated_for(self, package: str) -> DeprecatedPackage | None:
        for entry in self.deprecated:
            if entry.name == package:
                return entry
        return None

    def permission_for(self, package: str) -> PermissionRequirement | None:
        for entry in self.permissions:
            if entry.package == package:
                return entry
        return None

    def required_target_sdk(self, *, today: date | None = None) -> TargetSdkRequirement | None:
        """The highest requirement whose deadline has already passed."""
        reference = today or date.today()
        active = [
            requirement
            for requirement in self.target_sdk_requirements
            if requirement.enforce and requirement.effective <= reference
        ]
        return max(active, key=lambda item: item.target_sdk) if active else None

    def upcoming_target_sdk(self, *, today: date | None = None) -> TargetSdkRequirement | None:
        reference = today or date.today()
        upcoming = [
            requirement
            for requirement in self.target_sdk_requirements
            if requirement.effective > reference or not requirement.enforce
        ]
        return min(upcoming, key=lambda item: item.effective) if upcoming else None

    def compat_for_series(self, series: str | None) -> RNCompatEntry | None:
        if series is None:
            return None
        return self.rn_compat.get(series)


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:  # pragma: no cover - malformed data file
            return None
    return None


@lru_cache(maxsize=1)
def load_knowledge_data(data_dir: Path | None = None) -> KnowledgeData:
    """Parse and cache the packaged YAML data."""
    directory = data_dir or DATA_DIR
    libraries = read_yaml(directory / "libraries.yaml", default={}) or {}
    advisories = read_yaml(directory / "advisories.yaml", default={}) or {}

    signatures: dict[str, tuple[LibrarySignature, ...]] = {}
    for role, entries in libraries.items():
        if not isinstance(entries, list):
            continue
        parsed = [
            LibrarySignature(
                name=str(entry.get("name", "")),
                label=str(entry.get("label") or entry.get("name", "")),
                match=str(entry.get("match", "exact")),
            )
            for entry in entries
            if isinstance(entry, dict) and entry.get("name")
        ]
        signatures[role] = tuple(parsed)

    deprecated = tuple(
        DeprecatedPackage(
            name=str(entry.get("name", "")),
            reason=str(entry.get("reason", "")),
            replacement=entry.get("replacement"),
            severity=str(entry.get("severity", "low")),
            source=entry.get("source"),
            confidence=str(entry.get("confidence", "medium")),
            applies_when_rn_at_least=entry.get("applies_when_rn_at_least"),
        )
        for entry in advisories.get("deprecated_packages", [])
        if isinstance(entry, dict) and entry.get("name")
    )

    permissions = tuple(
        PermissionRequirement(
            package=str(entry.get("package", "")),
            ios_keys=tuple(entry.get("ios_keys") or ()),
            android_permissions=tuple(entry.get("android_permissions") or ()),
            required=bool(entry.get("required", False)),
            note=entry.get("note"),
            source=entry.get("source"),
        )
        for entry in advisories.get("permission_requirements", [])
        if isinstance(entry, dict) and entry.get("package")
    )

    policies = advisories.get("platform_policies", {}) or {}
    android_policy = policies.get("android_target_sdk", {}) or {}
    target_sdk = tuple(
        TargetSdkRequirement(
            target_sdk=int(entry["target_sdk"]),
            effective=_as_date(entry.get("effective")) or date.min,
            enforce=bool(entry.get("enforce", False)),
            confidence=str(entry.get("confidence", "medium")),
        )
        for entry in android_policy.get("requirements", [])
        if isinstance(entry, dict) and entry.get("target_sdk")
    )
    ios_policy = policies.get("ios_privacy_manifest", {}) or {}

    compat_block = advisories.get("react_native_compatibility", {}) or {}
    compat = {
        str(series): RNCompatEntry(
            series=str(series),
            react=(entry or {}).get("react"),
            node=(entry or {}).get("node"),
            confidence=str((entry or {}).get("confidence", "medium")),
        )
        for series, entry in (compat_block.get("versions") or {}).items()
    }

    return KnowledgeData(
        library_signatures=signatures,
        deprecated=deprecated,
        javascript_only=frozenset(advisories.get("javascript_only_packages") or ()),
        permissions=permissions,
        target_sdk_requirements=target_sdk,
        target_sdk_source=android_policy.get("source"),
        privacy_manifest_effective=_as_date(ios_policy.get("effective")),
        privacy_manifest_source=ios_policy.get("source"),
        rn_compat=compat,
        rn_compat_note=compat_block.get("note"),
    )
