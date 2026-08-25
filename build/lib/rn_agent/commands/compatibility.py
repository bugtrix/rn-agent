"""``rn-agent compatibility`` - can this project run on that React Native?

The command exists to be run *before* ``migrate``, and its value is entirely in
what it refuses to claim. Three statuses, and the difference between them is the
whole point:

* ``OK`` - a requirement was found and this project satisfies it;
* ``CONFLICT`` - a requirement was found and this project provably breaks it;
* ``UNKNOWN`` - no requirement could be established, and the report says why.

The best source is the target version's own metadata: what
``react-native@0.82.1`` states in ``peerDependencies`` and ``engines`` is a fact
about 0.82.1, not a table someone has to remember to update. The bundled table is
the labelled fallback, and per-series Gradle/AGP numbers - which nothing local
can establish - stay ``UNKNOWN`` rather than being invented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..cli import ui
from ..core.command import AgentCommand
from ..core.context import AgentContext
from ..core.registry import register
from ..models.compatibility import (
    CompatArea,
    CompatibilityEntry,
    CompatibilityReport,
    CompatStatus,
)
from ..models.project import DependencyInfo, ProjectContext
from ..reporting.compatibility_view import render_compatibility
from ..upgrade.registry import NpmRegistry, Packument
from ..utils.io import write_json
from ..utils.semver import parse, range_floor, satisfies
from .health import CONTEXT_STALE_SECONDS

UPGRADE_DOCS = "https://reactnative.dev/docs/upgrading"


@dataclass(slots=True)
class CompatibilityAnalysis:
    project: ProjectContext
    target: str | None
    target_source: str
    registry: NpmRegistry | None
    rn_document: Packument | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CompatibilityPlan:
    report: CompatibilityReport


class CompatibilityCommand(AgentCommand[CompatibilityAnalysis, CompatibilityPlan]):
    name = "compatibility"
    description = "Check this project against a React Native version before you migrate"
    read_only = True
    requires_context = True

    def __init__(
        self,
        context: AgentContext,
        *,
        target: str | None = None,
        offline: bool = False,
        include_dependencies: bool = True,
        verbose: bool = False,
    ) -> None:
        super().__init__(context)
        self.target = target
        self.offline = offline
        self.include_dependencies = include_dependencies
        self.verbose = verbose
        self.report: CompatibilityReport | None = None

    # -- phases ------------------------------------------------------------
    def analyze(self) -> CompatibilityAnalysis:
        project, _ = self.context.ensure_project(stale_seconds=CONTEXT_STALE_SECONDS)
        registry = None if self.offline else NpmRegistry()
        notes: list[str] = []

        document = registry.packument("react-native") if registry is not None else None
        newest = document.newest() if document is not None else None
        target: str | None
        source: str
        if self.target:
            target, source = self.target, "requested"
        elif newest is not None:
            target, source = newest.version, "newest published react-native"
        else:
            target = project.rn_version
            source = "current version (no target could be resolved)"
            notes.append(
                "no target version was given and the registry could not answer, so this "
                "report describes the version you are already on"
            )
        return CompatibilityAnalysis(
            project=project,
            target=target,
            target_source=source,
            registry=registry,
            rn_document=document,
            notes=notes,
        )

    def plan(self, analysis: CompatibilityAnalysis) -> CompatibilityPlan:
        entries: list[CompatibilityEntry] = []
        notes = list(analysis.notes)

        entries.extend(self._runtime_entries(analysis, notes))
        entries.extend(self._platform_entries(analysis))
        if self.include_dependencies:
            dependency_entries, dependency_notes = self._dependency_entries(analysis)
            entries.extend(dependency_entries)
            notes.extend(dependency_notes)

        report = CompatibilityReport(
            current_rn=analysis.project.rn_version,
            target_rn=analysis.target,
            target_source=analysis.target_source,
            entries=entries,
            notes=notes,
            registry_available=(
                analysis.registry is not None and analysis.registry.available
            ),
        )
        unknown = len(report.unknowns)
        if unknown:
            report.notes.append(
                f"{unknown} row(s) could not be decided; unknowns do not block a migration, "
                "they are work to check by hand"
            )
        self.report = report
        return CompatibilityPlan(report=report)

    def validate(self, plan: CompatibilityPlan) -> dict[str, Any]:
        if self.context.dry_run:
            return {}
        path = self.context.paths.cache_dir / "compatibility-report.json"
        try:
            self.context.paths.ensure()
            write_json(path, plan.report.model_dump(mode="json"))
        except OSError as exc:  # pragma: no cover - read-only project
            self.logger.warning("could not write compatibility report: %s", exc)
            return {}
        return {"report": str(path)}

    def render(self, analysis: CompatibilityAnalysis, plan: CompatibilityPlan) -> None:
        render_compatibility(plan.report, verbose=self.verbose)
        ui.blank()
        if plan.report.blockers:
            ui.failure(f"{len(plan.report.blockers)} blocker(s) before you can move")
            ui.note("`rn-agent upgrade --only <package>` clears most dependency conflicts")
        else:
            ui.success(f"no known blocker for React Native {plan.report.target_rn}")
            ui.note(f"`rn-agent migrate --to {plan.report.target_rn}` when you are ready")

    def summary(self, analysis: CompatibilityAnalysis, plan: CompatibilityPlan) -> dict[str, Any]:
        return {
            "current": plan.report.current_rn,
            "target": plan.report.target_rn,
            "target_source": plan.report.target_source,
            "registry_available": plan.report.registry_available,
            "ready": plan.report.ready,
            **plan.report.counts(),
        }

    def exit_code(self, analysis: CompatibilityAnalysis, plan: CompatibilityPlan) -> int:
        return 1 if plan.report.blockers else 0

    # -- entry builders ----------------------------------------------------
    def _runtime_entries(
        self, analysis: CompatibilityAnalysis, notes: list[str]
    ) -> list[CompatibilityEntry]:
        """React and Node, from the target's own metadata when possible."""
        target = analysis.target
        project = analysis.project
        react_requirement: str | None = None
        node_requirement: str | None = None
        source = None
        confidence = "high"

        if analysis.registry is not None and target:
            peers = analysis.registry.peer_dependencies("react-native", target)
            engines = analysis.registry.engines("react-native", target)
            if peers is not None:
                react_requirement = peers.get("react")
                source = f"react-native@{target} peerDependencies"
            if engines is not None:
                node_requirement = engines.get("node")
        if react_requirement is None and target:
            parsed = parse(target)
            series = f"{parsed.major}.{parsed.minor}" if parsed else None
            entry = self.context.knowledge.compat_for_series(series)
            if entry is not None:
                react_requirement = entry.react
                node_requirement = node_requirement or entry.node
                source = "bundled compatibility table (offline)"
                confidence = entry.confidence
                notes.append(
                    "React and Node requirements came from the bundled table, not from the "
                    "package itself - install node_modules or go online for the real values"
                )

        return [
            self._compare(
                name="react",
                area=CompatArea.RUNTIME,
                requirement=react_requirement,
                current=project.react_native.react_version
                or _floor(project.react_native.react_declared_range),
                source=source,
                confidence=confidence,
                missing_detail=f"react-native {target} does not state a React requirement",
            ),
            self._compare(
                name="node",
                area=CompatArea.RUNTIME,
                requirement=node_requirement,
                current=project.tooling.node,
                source=source,
                confidence=confidence,
                missing_detail=f"react-native {target} does not state a Node requirement",
            ),
        ]

    def _platform_entries(self, analysis: CompatibilityAnalysis) -> list[CompatibilityEntry]:
        """What the platforms have now, and the one requirement we can prove."""
        project = analysis.project
        entries: list[CompatibilityEntry] = []

        requirement = self.context.knowledge.required_target_sdk()
        if project.android.present:
            entries.append(
                self._compare(
                    name="android targetSdk",
                    area=CompatArea.PLATFORM,
                    requirement=f">={requirement.target_sdk}" if requirement else None,
                    current=str(project.android.target_sdk) if project.android.target_sdk else None,
                    source=self.context.knowledge.target_sdk_source,
                    confidence=requirement.confidence if requirement else "medium",
                    missing_detail="no enforced Play requirement is known for this date",
                )
            )
            for label, value in (
                ("gradle", project.android.gradle_version),
                ("android gradle plugin", project.android.agp_version),
                ("kotlin", project.android.kotlin_version),
                ("compileSdk", str(project.android.compile_sdk) if project.android.compile_sdk else None),
            ):
                entries.append(
                    CompatibilityEntry(
                        name=label,
                        area=CompatArea.TOOLING,
                        required=None,
                        current=value,
                        status=CompatStatus.UNKNOWN,
                        detail=(
                            f"react-native {analysis.target} does not publish its required "
                            f"{label}; the migration diff is what changes it"
                        ),
                        source=UPGRADE_DOCS,
                    )
                )
        if project.ios.present:
            entries.append(
                CompatibilityEntry(
                    name="ios deployment target",
                    area=CompatArea.PLATFORM,
                    required=None,
                    current=project.ios.deployment_target,
                    status=CompatStatus.UNKNOWN,
                    detail=(
                        f"react-native {analysis.target} does not publish its minimum iOS "
                        "version; the Podfile in the migration diff sets it"
                    ),
                    source=UPGRADE_DOCS,
                )
            )
        return entries

    def _dependency_entries(
        self, analysis: CompatibilityAnalysis
    ) -> tuple[list[CompatibilityEntry], list[str]]:
        """Every dependency that states a ``react-native`` peer range."""
        target = analysis.target
        entries: list[CompatibilityEntry] = []
        notes: list[str] = []
        undecided = 0

        for dependency in analysis.project.dependencies:
            requirement, source = self._peer_requirement(dependency, analysis)
            if requirement is None:
                continue
            verdict = satisfies(target, requirement) if target else None
            if verdict is True:
                entries.append(
                    CompatibilityEntry(
                        name=dependency.name,
                        area=CompatArea.DEPENDENCY,
                        required=requirement,
                        current=dependency.effective_version,
                        status=CompatStatus.OK,
                        detail=f"supports react-native {target}",
                        source=source,
                    )
                )
                continue
            if verdict is False:
                entries.append(
                    CompatibilityEntry(
                        name=dependency.name,
                        area=CompatArea.DEPENDENCY,
                        required=requirement,
                        current=dependency.effective_version,
                        status=CompatStatus.CONFLICT,
                        detail=self._resolution(dependency, analysis, target),
                        source=source,
                    )
                )
                continue
            undecided += 1
            entries.append(
                CompatibilityEntry(
                    name=dependency.name,
                    area=CompatArea.DEPENDENCY,
                    required=requirement,
                    current=dependency.effective_version,
                    status=CompatStatus.UNKNOWN,
                    detail=f"cannot decide {requirement} against {target}",
                    source=source,
                )
            )
        if undecided:
            notes.append(
                f"{undecided} dependency range(s) carry no comparable version "
                "(git, workspace or tag specifiers)"
            )
        if not analysis.project.node_modules_present:
            notes.append(
                "node_modules is missing, so peer requirements came from the registry only; "
                "install dependencies for the most accurate answer"
            )
        return entries, notes

    def _peer_requirement(
        self, dependency: DependencyInfo, analysis: CompatibilityAnalysis
    ) -> tuple[str | None, str | None]:
        """The dependency's ``react-native`` peer range, and where it came from."""
        installed = dependency.peer_dependencies.get("react-native")
        if installed:
            return installed, f"{dependency.name} installed metadata"
        if analysis.registry is None:
            return None, None
        version = dependency.installed or _floor(dependency.declared)
        if version is None:
            return None, None
        peers = analysis.registry.peer_dependencies(dependency.name, version)
        if not peers:
            return None, None
        requirement = peers.get("react-native")
        if requirement is None:
            return None, None
        return requirement, f"{dependency.name}@{version} peerDependencies"

    def _resolution(
        self, dependency: DependencyInfo, analysis: CompatibilityAnalysis, target: str | None
    ) -> str:
        """For a conflict: is there a version that would work?"""
        base = f"does not support react-native {target}"
        if analysis.registry is None or target is None:
            return f"{base}; check the package's own compatibility notes"
        document = analysis.registry.packument(dependency.name)
        if document is None:
            return base
        for entry in reversed(document.stable()):
            requirement = entry.peer_dependencies.get("react-native")
            if requirement and satisfies(target, requirement) is True:
                return f"{base}; {dependency.name} {entry.version} does"
        return f"{base}; no published version of {dependency.name} does yet"

    def _compare(
        self,
        *,
        name: str,
        area: CompatArea,
        requirement: str | None,
        current: str | None,
        source: str | None,
        confidence: str,
        missing_detail: str,
    ) -> CompatibilityEntry:
        """One row, with ``UNKNOWN`` whenever a fact is missing."""
        if requirement is None:
            return CompatibilityEntry(
                name=name,
                area=area,
                required=None,
                current=current,
                status=CompatStatus.UNKNOWN,
                detail=missing_detail,
                source=source,
                confidence=confidence,
            )
        if current is None:
            return CompatibilityEntry(
                name=name,
                area=area,
                required=requirement,
                current=None,
                status=CompatStatus.UNKNOWN,
                detail=f"{name} version is unknown in this project",
                source=source,
                confidence=confidence,
            )
        verdict = satisfies(current, requirement)
        if verdict is True:
            status, detail = CompatStatus.OK, f"{current} satisfies {requirement}"
        elif verdict is False:
            status, detail = CompatStatus.CONFLICT, f"{current} does not satisfy {requirement}"
        else:
            status, detail = (
                CompatStatus.UNKNOWN,
                f"cannot compare {current} with {requirement}",
            )
        return CompatibilityEntry(
            name=name,
            area=area,
            required=requirement,
            current=current,
            status=status,
            detail=detail,
            source=source,
            confidence=confidence,
        )


def _floor(spec: str | None) -> str | None:
    floor = range_floor(spec)
    return str(floor) if floor else None


register(CompatibilityCommand, phase=6)
