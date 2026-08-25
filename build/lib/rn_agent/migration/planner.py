"""Turning "move to 0.82" into a list of small, reversible steps.

The plan is built from three sources, in descending order of authority:

1. **The target's own metadata** - ``react-native@<target>``'s
   ``peerDependencies`` and ``engines`` say which React and which Node it wants.
   That is a fact about the version you are moving to, not a table someone
   maintained.
2. **The upstream template diff** - what the React Native template itself
   changed between the two versions, per file.
3. **Local rule files** - exact edits a human wrote down, with a source.

Anything none of the three can decide becomes a ``MANUAL`` step with the reason.
A migration that admits "do this bit yourself" is worth far more than one that
guesses at a ``.pbxproj``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..knowledge.data import KnowledgeData
from ..models.changes import RiskLevel
from ..models.migration import MigrationPlan, MigrationStep, StepKind
from ..models.project import ProjectContext
from ..upgrade.registry import NpmRegistry
from ..utils.semver import coerce, parse
from .diff import DiffFile, parse_diff, rename_placeholder
from .rules import MigrationRule, RuleSet
from .sources import DiffDocument

#: Files whose risk is high whatever the diff says.
HIGH_RISK_MARKERS: tuple[str, ...] = (
    ".pbxproj",
    "gradle-wrapper.properties",
    "build.gradle",
    "Podfile",
    "settings.gradle",
    "gradle.properties",
    "AppDelegate",
    "MainApplication",
    "MainActivity",
)


@dataclass(frozen=True, slots=True)
class PlanInputs:
    """Everything the planner is allowed to look at."""

    project: ProjectContext
    root: Path
    from_version: str
    to_version: str
    diff: DiffDocument | None = None
    rules: RuleSet | None = None
    registry: NpmRegistry | None = None
    knowledge: KnowledgeData | None = None
    skip_native: bool = False
    kinds: tuple[str, ...] = ()
    diff_reason: str | None = None
    docs_url: str | None = None


def build_plan(inputs: PlanInputs) -> MigrationPlan:
    """The ordered steps for one React Native version migration."""
    steps: list[MigrationStep] = []
    notes: list[str] = []
    sources: list[str] = []

    dependency_step, dependency_notes = _dependency_step(inputs)
    notes.extend(dependency_notes)
    if dependency_step is not None:
        steps.append(dependency_step)

    if inputs.diff is not None:
        sources.append(inputs.diff.source)
        file_steps, file_notes = _diff_steps(inputs, inputs.diff)
        steps.extend(file_steps)
        notes.extend(file_notes)
    else:
        notes.append(
            inputs.diff_reason
            or "the upstream template diff was unavailable; native steps are limited to local rules"
        )

    rule_set = inputs.rules
    if rule_set is not None and rule_set.rules:
        sources.extend(rule_set.files)
        steps.extend(_rule_step(rule) for rule in rule_set.rules)
        if rule_set.skipped:
            notes.append(
                f"{len(rule_set.skipped)} local rule(s) used an action this version does not "
                f"implement and were skipped: {', '.join(rule_set.skipped)}"
            )

    if inputs.docs_url:
        sources.append(inputs.docs_url)

    wanted = set(inputs.kinds)
    if wanted:
        steps = [step for step in steps if step.kind.value in wanted or not step.automatic]

    return MigrationPlan(
        from_version=inputs.from_version,
        to_version=inputs.to_version,
        steps=steps,
        sources=list(dict.fromkeys(sources)),
        notes=notes,
        offline=inputs.diff is None,
    )


# ---------------------------------------------------------------------------
# dependencies
# ---------------------------------------------------------------------------
def _dependency_step(inputs: PlanInputs) -> tuple[MigrationStep | None, list[str]]:
    """The ``package.json`` change, from the target's own requirements."""
    notes: list[str] = []
    payload: dict[str, str] = {"react-native": inputs.to_version}
    source = "package.json"

    react_range: str | None = None
    node_range: str | None = None
    if inputs.registry is not None:
        peers = inputs.registry.peer_dependencies("react-native", inputs.to_version)
        engines = inputs.registry.engines("react-native", inputs.to_version)
        if peers:
            react_range = peers.get("react")
            source = f"react-native@{inputs.to_version} peerDependencies"
        if engines:
            node_range = engines.get("node")
    if react_range is None:
        entry = (
            inputs.knowledge.compat_for_series(_series(inputs.to_version))
            if inputs.knowledge
            else None
        )
        if entry is not None:
            react_range = entry.react
            node_range = node_range or entry.node
            source = "bundled compatibility table (offline)"
            notes.append(
                f"React requirement for {inputs.to_version} came from the bundled table "
                f"(confidence: {entry.confidence}), not from the package itself"
            )
        else:
            notes.append(
                f"no React requirement could be established for react-native "
                f"{inputs.to_version}; React is left as it is"
            )

    if react_range:
        pinned = _pin(react_range)
        if pinned:
            payload["react"] = pinned
            if inputs.project.dependency("@types/react") is not None:
                payload["@types/react"] = f"^{pinned}"
    if node_range:
        notes.append(f"react-native {inputs.to_version} requires Node {node_range}")

    changes = ", ".join(f"{name} -> {version}" for name, version in payload.items())
    return (
        MigrationStep(
            id="dependency.package-json",
            kind=StepKind.DEPENDENCY,
            title=f"Update package.json to React Native {inputs.to_version}",
            file="package.json",
            detail=changes,
            risk=RiskLevel.MEDIUM,
            source=source,
            payload=payload,
        ),
        notes,
    )


def _pin(requirement: str) -> str | None:
    """The lowest version a range admits, as a concrete number to write."""
    from ..utils.semver import range_floor

    floor = range_floor(requirement)
    return str(floor) if floor else None


def _series(version: str) -> str | None:
    parsed = parse(version) or coerce(version)
    return f"{parsed.major}.{parsed.minor}" if parsed else None


# ---------------------------------------------------------------------------
# the upstream diff
# ---------------------------------------------------------------------------
def _diff_steps(
    inputs: PlanInputs, document: DiffDocument
) -> tuple[list[MigrationStep], list[str]]:
    steps: list[MigrationStep] = []
    notes: list[str] = []
    project_name = _project_name(inputs.project)

    for entry in parse_diff(document.text):
        path, decided = rename_placeholder(entry.path, project_name=project_name)
        if not decided:
            steps.append(
                _manual(
                    f"diff.{_slug(entry.path)}",
                    title=f"Update {entry.path} by hand",
                    detail=(
                        "the upstream path contains the template app name and this project's "
                        "name could not be determined"
                    ),
                    source=document.source,
                )
            )
            continue
        if path == "package.json":
            continue  # the dependency step owns this file
        if entry.binary:
            steps.append(
                _manual(
                    f"diff.{_slug(path)}",
                    title=f"Replace {path} from the template",
                    detail="binary file: copy it from the upstream template yourself",
                    source=document.source,
                    file=path,
                )
            )
            continue

        target = inputs.root / path
        kind = _kind_for(path)
        if kind in (StepKind.ANDROID, StepKind.IOS) and inputs.skip_native:
            continue
        if entry.created and not target.exists():
            steps.append(
                _manual(
                    f"diff.{_slug(path)}",
                    title=f"Add {path} from the template",
                    detail="the upstream template adds this file; create it from the diff",
                    source=document.source,
                    file=path,
                    diff=entry.text,
                )
            )
            continue
        if not target.is_file():
            steps.append(
                _manual(
                    f"diff.{_slug(path)}",
                    title=f"{path} is not in this project",
                    detail=(
                        "the template changed a file you do not have (customised or removed); "
                        "check the diff yourself"
                    ),
                    source=document.source,
                    file=path,
                    diff=entry.text,
                )
            )
            continue

        content_ok = _hunks_decidable(entry, project_name=project_name)
        if not content_ok:
            steps.append(
                _manual(
                    f"diff.{_slug(path)}",
                    title=f"Update {path} by hand",
                    detail=(
                        "the change renames the template app and this project's name could "
                        "not be determined"
                    ),
                    source=document.source,
                    file=path,
                    diff=entry.text,
                )
            )
            continue

        steps.append(
            MigrationStep(
                id=f"diff.{_slug(path)}",
                kind=kind,
                title=f"Apply the template change to {path}",
                file=path,
                detail=f"{len(entry.hunks)} hunk(s) from the upstream diff",
                risk=_risk_for(path, kind),
                source=document.source,
                diff=entry.text,
            )
        )
    if not steps:
        notes.append("the upstream diff contained no change that applies to this project")
    return steps, notes


def _hunks_decidable(entry: DiffFile, *, project_name: str | None) -> bool:
    for hunk in entry.hunks:
        _, decided = rename_placeholder(hunk.text, project_name=project_name)
        if not decided:
            return False
    return True


def _project_name(project: ProjectContext) -> str | None:
    """The app's real name, for un-templating upstream paths and content."""
    if project.ios.project_name:
        return project.ios.project_name
    if project.name:
        return project.name
    namespace = project.android.namespace or project.android.application_id
    if namespace:
        return namespace.rsplit(".", 1)[-1]
    return None


def _kind_for(path: str) -> StepKind:
    if path.startswith("android/"):
        return StepKind.ANDROID
    if path.startswith("ios/"):
        return StepKind.IOS
    return StepKind.JAVASCRIPT


def _risk_for(path: str, kind: StepKind) -> RiskLevel:
    if any(marker in path for marker in HIGH_RISK_MARKERS):
        return RiskLevel.HIGH
    if kind in (StepKind.ANDROID, StepKind.IOS):
        return RiskLevel.HIGH
    return RiskLevel.MEDIUM


# ---------------------------------------------------------------------------
# local rules
# ---------------------------------------------------------------------------
def _rule_step(rule: MigrationRule) -> MigrationStep:
    payload = {
        key: value
        for key, value in (
            ("action", rule.action.value),
            ("key", rule.key),
            ("value", rule.value),
            ("old", rule.old),
            ("new", rule.new),
            ("line", rule.line),
        )
        if value is not None
    }
    return MigrationStep(
        id=f"rule.{rule.id}",
        kind=rule.kind,
        title=rule.title,
        file=rule.file,
        detail=rule.detail or f"{rule.action.value} {rule.key or rule.old or rule.line or ''}".strip(),
        risk=rule.risk,
        source=rule.source,
        payload=payload,
    )


def _manual(
    identifier: str,
    *,
    title: str,
    detail: str,
    source: str | None,
    file: str | None = None,
    diff: str | None = None,
) -> MigrationStep:
    return MigrationStep(
        id=identifier,
        kind=StepKind.MANUAL,
        title=title,
        file=file,
        detail=detail,
        risk=RiskLevel.HIGH if file and any(m in file for m in HIGH_RISK_MARKERS) else RiskLevel.MEDIUM,
        source=source,
        diff=diff,
    )


def _slug(path: str) -> str:
    return path.replace("/", ".").replace(" ", "-")
