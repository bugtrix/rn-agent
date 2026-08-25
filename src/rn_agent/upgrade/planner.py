"""Deciding which upgrades are worth doing, and what each one risks.

Everything here is deterministic: given the same project and the same registry
answers, the same plan comes out. The model is not involved in the decision at
all - a dependency bump is arithmetic over version ranges plus knowledge of what
native code costs, and inventing an opinion would only add uncertainty.

Three rules do most of the work:

* **React Native is not a dependency bump.** ``react-native`` and ``react`` are
  always blocked here and pointed at ``rn-agent migrate``, because moving them
  means template diffs, pods and a rebuild.
* **A peer conflict blocks.** The target's own ``peerDependencies`` are checked
  against what the project has; ``satisfies() is None`` (undecidable) is a note,
  never a conflict.
* **Native code costs more.** A package that ships ``android/`` or ``ios/``
  needs a pod install and a rebuild, so it is never low risk and is left out
  unless the developer asks for it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ..models.changes import RiskLevel
from ..models.project import DependencyInfo, DependencyKind, ProjectContext
from ..models.upgrade import ChangeKind, UpgradeCandidate, UpgradePlan
from ..utils.semver import Version, parse, range_floor, satisfies
from .registry import NpmRegistry, PackageVersion, Packument

POLICIES: tuple[str, ...] = ("patch", "minor", "latest")

#: Only these are upgraded. A peer or optional entry is not ours to move.
UPGRADABLE_KINDS: frozenset[DependencyKind] = frozenset(
    {DependencyKind.PROD, DependencyKind.DEV}
)

MIGRATE_HINT = "a React Native version change: use `rn-agent upgrade --to <version>`"


def plan_upgrades(
    *,
    project: ProjectContext,
    registry: NpmRegistry | None,
    policy: str = "minor",
    only: Sequence[str] = (),
    skip: Sequence[str] = (),
    include_native: bool = False,
) -> UpgradePlan:
    """Build the risk-ranked plan for one project."""
    if policy not in POLICIES:  # pragma: no cover - the CLI validates first
        raise ValueError(f"unknown upgrade policy: {policy}")

    wanted = {name.casefold() for name in only}
    unwanted = {name.casefold() for name in skip}
    candidates: list[UpgradeCandidate] = []
    notes: list[str] = []
    registry_available = registry is not None and registry.available

    for dependency in project.dependencies:
        if dependency.kind not in UPGRADABLE_KINDS:
            continue
        name = dependency.name
        if wanted and name.casefold() not in wanted:
            continue
        if name.casefold() in unwanted:
            continue
        candidates.append(
            _candidate(
                dependency,
                project=project,
                registry=registry,
                policy=policy,
                include_native=include_native,
            )
        )

    if registry is not None and not registry.available:
        registry_available = False
        notes.append(
            "the npm registry could not be reached; no target versions were resolved "
            "and only installed-versus-declared drift is reported"
        )
    elif registry is None:
        notes.append("running offline (--offline): no target versions were resolved")

    undecidable = [
        candidate.name
        for candidate in candidates
        if candidate.change is ChangeKind.UNKNOWN and registry_available
    ]
    if undecidable:
        notes.append(
            f"{len(undecidable)} package(s) could not be compared "
            f"(git, workspace or tag specifiers): {', '.join(sorted(undecidable)[:5])}"
        )
    if any(candidate.native for candidate in candidates) and not include_native:
        notes.append(
            "native packages were analysed but left out; add --native to include them "
            "(they need a pod install and a rebuild)"
        )

    return UpgradePlan(
        policy=policy,
        candidates=candidates,
        registry_available=registry_available,
        install_command=project.package_manager.install_command,
        notes=notes,
    )


def _candidate(
    dependency: DependencyInfo,
    *,
    project: ProjectContext,
    registry: NpmRegistry | None,
    policy: str,
    include_native: bool,
) -> UpgradeCandidate:
    current_text = dependency.installed or _declared_floor(dependency.declared)
    current = parse(current_text)
    candidate = UpgradeCandidate(
        name=dependency.name,
        kind=dependency.kind,
        declared=dependency.declared,
        installed=dependency.installed,
        native=dependency.native,
    )
    reasons: list[str] = []
    policy_block = _blocked_by_policy(dependency.name)
    if policy_block is not None:
        return candidate.model_copy(
            update={
                "change": ChangeKind.NONE,
                "risk": RiskLevel.CRITICAL,
                "blocked": True,
                "blocked_reason": policy_block,
                "reasons": [policy_block],
                "source": "policy",
            }
        )

    document = registry.packument(dependency.name) if registry is not None else None
    if document is None:
        reason = (
            "registry unreachable; target unknown"
            if registry is None or not registry.available
            else "not published on the registry"
        )
        return candidate.model_copy(
            update={
                "change": ChangeKind.UNKNOWN,
                "reasons": [reason],
                "blocked": True,
                "blocked_reason": reason,
            }
        )

    newest = document.newest()
    latest = newest.version if newest else None
    target = _target(document, current=current, policy=policy)
    if target is None or current is None:
        reason = (
            "no comparable published version"
            if current is not None
            else f"current version is undecidable ({dependency.declared or 'unknown'})"
        )
        return candidate.model_copy(
            update={
                "change": ChangeKind.UNKNOWN,
                "latest": latest,
                "reasons": [reason],
                "blocked": True,
                "blocked_reason": reason,
                "source": "registry",
            }
        )

    target_version = parse(target.version)
    change = _change_kind(current, target_version)
    if change is ChangeKind.NONE:
        return candidate.model_copy(
            update={
                "change": ChangeKind.NONE,
                "latest": latest,
                "target": target.version,
                "reasons": ["already up to date for this policy"],
                "source": "registry",
            }
        )

    reasons.append(f"{change.value} change {current} -> {target.version}")
    if dependency.native:
        platforms = ", ".join(dependency.platforms) or "android, ios"
        reasons.append(f"ships native code ({platforms}): needs a pod install and a rebuild")
    if target.deprecated:
        reasons.append(f"deprecated: {target.deprecated}")

    conflicts = _peer_conflicts(target.peer_dependencies, project=project)
    risk = _risk(change, native=dependency.native, conflicts=bool(conflicts))
    blocked = False
    blocked_reason: str | None = None
    if conflicts:
        blocked = True
        blocked_reason = f"peer conflict: {conflicts[0]}"
    elif dependency.native and not include_native:
        blocked = True
        blocked_reason = "native package; add --native to upgrade it"

    return candidate.model_copy(
        update={
            "latest": latest,
            "target": target.version,
            "change": change,
            "risk": risk,
            "reasons": reasons,
            "peer_conflicts": conflicts,
            "blocked": blocked,
            "blocked_reason": blocked_reason,
            "source": "registry",
        }
    )


def _blocked_by_policy(name: str) -> str | None:
    if name in {"react-native", "react"}:
        return MIGRATE_HINT
    return None


def _declared_floor(declared: str | None) -> str | None:
    floor = range_floor(declared)
    return str(floor) if floor else None


def _target(
    document: Packument, *, current: Version | None, policy: str
) -> PackageVersion | None:
    """The newest stable version the policy allows, above ``current``."""
    stable = document.stable()
    if not stable:
        return None
    if current is None:
        return document.newest()
    allowed = [entry for entry in stable if entry.parsed and entry.parsed > current]
    if policy == "patch":
        allowed = [
            entry
            for entry in allowed
            if entry.parsed
            and (entry.parsed.major, entry.parsed.minor) == (current.major, current.minor)
        ]
    elif policy == "minor":
        allowed = [entry for entry in allowed if entry.parsed and entry.parsed.major == current.major]
    if not allowed:
        # Nothing newer under this policy: report the current version as target,
        # which the caller turns into ChangeKind.NONE.
        return next((entry for entry in stable if entry.parsed == current), stable[-1])
    return allowed[-1]


def _change_kind(current: Version | None, target: Version | None) -> ChangeKind:
    if current is None or target is None:
        return ChangeKind.UNKNOWN
    if target == current:
        return ChangeKind.NONE
    if target < current:
        return ChangeKind.NONE
    if target.major != current.major:
        return ChangeKind.MAJOR
    if target.minor != current.minor:
        return ChangeKind.MINOR
    return ChangeKind.PATCH


def _risk(change: ChangeKind, *, native: bool, conflicts: bool) -> RiskLevel:
    if conflicts:
        return RiskLevel.CRITICAL if native else RiskLevel.HIGH
    if change is ChangeKind.MAJOR:
        return RiskLevel.CRITICAL if native else RiskLevel.HIGH
    if change is ChangeKind.MINOR:
        return RiskLevel.HIGH if native else RiskLevel.MEDIUM
    if change is ChangeKind.PATCH:
        return RiskLevel.MEDIUM if native else RiskLevel.LOW
    return RiskLevel.LOW


def _peer_conflicts(
    peers: Mapping[str, str], *, project: ProjectContext
) -> list[str]:
    """Peers of the target that this project provably does not satisfy."""
    conflicts: list[str] = []
    for peer, requirement in peers.items():
        have = _project_version(peer, project)
        if have is None:
            continue
        verdict = satisfies(have, requirement)
        if verdict is False:
            conflicts.append(f"{peer} {have} does not satisfy {requirement}")
    return conflicts


def _project_version(name: str, project: ProjectContext) -> str | None:
    """What this project has of ``name``: installed first, declared second.

    Without ``node_modules`` the installed version is unknown, but the declared
    range still pins a lowest admissible version - enough to prove a conflict.
    """
    if name == "react-native":
        return project.react_native.version or _declared_floor(
            project.react_native.declared_range
        )
    if name == "react":
        return project.react_native.react_version or _declared_floor(
            project.react_native.react_declared_range
        )
    dependency = project.dependency(name)
    if dependency is None:
        return None
    return dependency.installed or _declared_floor(dependency.declared)
