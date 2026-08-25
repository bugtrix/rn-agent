"""``rn-agent release`` - bump every version a React Native app carries.

A React Native app states its version in three places that drift apart:
``package.json``, ``android/app/build.gradle`` (``versionName`` /
``versionCode``) and the Xcode project (``MARKETING_VERSION`` /
``CURRENT_PROJECT_VERSION``). This command finds every one it can, shows the set
before writing, and reports the ones it could not find - because "Android was
left behind" is exactly the bug this command exists to prevent.

What it deliberately does not do: commit, tag, push, build or upload.
``GitManager`` implements no history-writing operation, and adding one here would
put a model-adjacent command in charge of your git history. The release notes and
the checklist are produced instead, and the git commands are printed for you to
run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..agents.apply import ApplyOutcome
from ..agents.engine import AIEngine
from ..agents.prompts import changelog_messages
from ..agents.rules import ProjectRules
from ..agents.workflow import EditWorkflow
from ..cli import ui
from ..core.command import AgentCommand
from ..core.context import AgentContext
from ..core.registry import register
from ..errors import ModelOutputError, ProviderError, RNAgentError
from ..models.project import ProjectContext
from ..models.proposal import EditAction, FileEdit, Proposal
from ..models.release import BumpKind, ReleasePlan, VersionChange
from ..reporting import change_view
from ..reporting.release_view import render_release
from ..utils.io import read_json, read_text, write_json
from ..utils.semver import Version, parse
from .health import CONTEXT_STALE_SECONDS

#: Commit subjects that are not release notes.
CHORE_PATTERNS: tuple[str, ...] = (
    r"^merge ",
    r"^merge$",
    r"^v?\d+\.\d+\.\d+$",
    r"^(chore|ci|build)(\(.+\))?: *(bump|release|version)",
    r"^bump version",
    r"^release \d",
)

_VERSION_NAME_RE = re.compile(r'(versionName\s+")(?P<value>[^"]+)(")')
_VERSION_CODE_RE = re.compile(r"(versionCode\s+)(?P<value>\d+)")
_MARKETING_RE = re.compile(r"(MARKETING_VERSION = )(?P<value>[0-9][^;]*)(;)")
_PROJECT_VERSION_RE = re.compile(r"(CURRENT_PROJECT_VERSION = )(?P<value>[0-9][^;]*)(;)")


@dataclass(slots=True)
class ReleaseAnalysis:
    project: ProjectContext
    current: str
    next_version: str
    commits: list[str] = field(default_factory=list)
    previous_tag: str | None = None
    blockers: list[str] = field(default_factory=list)
    changes: list[VersionChange] = field(default_factory=list)
    edits: list[FileEdit] = field(default_factory=list)
    changelog: list[str] = field(default_factory=list)
    changelog_source: str = "commits"
    notes: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class ReleasePreparedPlan:
    plan: ReleasePlan
    edits: list[FileEdit]
    workflow: EditWorkflow


class ReleaseCommand(AgentCommand[ReleaseAnalysis, ReleasePreparedPlan]):
    name = "release"
    description = "Prepare a release: versions, changelog and the checklist"
    read_only = False
    requires_context = True

    def __init__(
        self,
        context: AgentContext,
        *,
        bump: str = "patch",
        version: str | None = None,
        changelog: bool = True,
        changelog_path: str = "CHANGELOG.md",
        force: bool = False,
        verbose: bool = False,
    ) -> None:
        super().__init__(context)
        self.bump = bump
        self.version = version
        self.changelog = changelog
        self.changelog_path = changelog_path
        self.force = force
        self.verbose = verbose
        self.report: ReleasePlan | None = None
        self.outcome: ApplyOutcome | None = None
        self.confirmed: list[str] = []

    # -- phases ------------------------------------------------------------
    def analyze(self) -> ReleaseAnalysis:
        project, _ = self.context.ensure_project(stale_seconds=CONTEXT_STALE_SECONDS)
        current_text, next_text = self._versions(project)
        previous_tag, commits = self._commits()
        blockers = self._blockers(commits)
        changes, edits, notes = self._version_changes(current_text, next_text)
        changelog, source, usage = self._changelog(project, next_text, commits)
        if changelog:
            edit = self._changelog_edit(next_text, changelog)
            if edit is not None:
                edits.append(edit)
                changes.append(
                    VersionChange(
                        file=self.changelog_path,
                        label="changelog entry",
                        current=None,
                        next=next_text,
                    )
                )
        return ReleaseAnalysis(
            project=project,
            current=current_text,
            next_version=next_text,
            commits=commits,
            previous_tag=previous_tag,
            blockers=blockers,
            changes=changes,
            edits=edits,
            changelog=changelog,
            changelog_source=source,
            notes=notes,
            usage=usage,
        )

    def plan(self, analysis: ReleaseAnalysis) -> ReleasePreparedPlan:
        plan = ReleasePlan(
            bump=BumpKind.EXPLICIT if self.version else BumpKind(self.bump),
            current_version=analysis.current,
            next_version=analysis.next_version,
            changes=analysis.changes,
            commits=analysis.commits,
            previous_tag=analysis.previous_tag,
            changelog=analysis.changelog,
            changelog_source=analysis.changelog_source,
            blockers=analysis.blockers,
            checklist=self._checklist(analysis),
            notes=analysis.notes,
        )
        self.report = plan
        workflow = EditWorkflow(
            self.context,
            rules=ProjectRules.load(self.context.paths),
            task="release",
            allow_dependencies=True,
            allow_native=True,
        )
        return ReleasePreparedPlan(plan=plan, edits=analysis.edits, workflow=workflow)

    def execute(self, plan: ReleasePreparedPlan) -> None:
        if plan.plan.blockers and not self.force:
            self.logger.warning("release blocked: %s", "; ".join(plan.plan.blockers))
            return
        if not plan.edits:
            self.logger.info("no version field could be updated")
            return
        proposal = Proposal(
            id="release",
            title=f"release {plan.plan.next_version}",
            summary=", ".join(change.file for change in plan.plan.effective_changes),
            edits=plan.edits,
        )
        self.outcome = plan.workflow.apply(
            [proposal],
            reason=f"release: {plan.plan.current_version} -> {plan.plan.next_version}",
            question=(
                f"Write version {plan.plan.next_version} into "
                f"{len(plan.edits)} file(s)?"
            ),
        )

    def validate(self, plan: ReleasePreparedPlan) -> dict[str, Any]:
        """Read the files back: a release that half-landed is a broken build."""
        summary: dict[str, Any] = {
            "next_version": plan.plan.next_version,
            "blocked": bool(plan.plan.blockers) and not self.force,
        }
        if self.outcome is not None and not self.context.dry_run:
            for change in plan.plan.effective_changes:
                content = read_text(self.context.files.resolve(change.file))
                if content and change.next and change.next in content:
                    self.confirmed.append(change.file)
                else:
                    plan.plan.notes.append(
                        f"{change.file} does not contain {change.next} after the write"
                    )
            summary["confirmed"] = self.confirmed
        if self.context.dry_run:
            return summary
        try:
            self.context.paths.ensure()
            path = self.context.paths.cache_dir / "release-report.json"
            payload = plan.plan.model_dump(mode="json")
            payload["confirmed"] = self.confirmed
            payload["applied"] = list(self.outcome.applied) if self.outcome else []
            write_json(path, payload)
            summary["report"] = str(path)
        except OSError as exc:  # pragma: no cover - read-only project
            self.logger.warning("could not write release report: %s", exc)
        return summary

    def render(self, analysis: ReleaseAnalysis, plan: ReleasePreparedPlan) -> None:
        render_release(plan.plan, verbose=self.verbose)
        if self.outcome is not None:
            change_view.render_outcome(self.outcome, dry_run=self.context.dry_run)
        if analysis.usage.get("calls"):
            change_view.render_usage(
                analysis.usage,
                model=self.context.config.ai.model_for("docs"),
                provider=self.context.config.ai.provider,
            )
        ui.blank()
        if plan.plan.blockers and not self.force:
            ui.failure("release blocked; nothing was written")
            ui.note("fix the blockers, or re-run with --force if you know better")
            return
        if self.context.dry_run:
            ui.note("dry run: no version was written")
            return
        if self.confirmed:
            ui.success(f"version {plan.plan.next_version} written to {len(self.confirmed)} file(s)")
            return
        if self.outcome is None:
            ui.warning("no version field was found to update")

    def summary(self, analysis: ReleaseAnalysis, plan: ReleasePreparedPlan) -> dict[str, Any]:
        return {
            "current_version": plan.plan.current_version,
            "next_version": plan.plan.next_version,
            "bump": plan.plan.bump.value,
            "changelog_source": plan.plan.changelog_source,
            "blocked": bool(plan.plan.blockers) and not self.force,
            "confirmed": len(self.confirmed),
            **plan.plan.counts(),
        }

    def exit_code(self, analysis: ReleaseAnalysis, plan: ReleasePreparedPlan) -> int:
        if plan.plan.blockers and not self.force:
            return 1
        if self.context.dry_run:
            return 0
        if plan.edits and not self.confirmed:
            return 1
        return 0

    # -- version arithmetic ------------------------------------------------
    def _versions(self, project: ProjectContext) -> tuple[str, str]:
        # package.json is the truth right now: the scanned context may predate
        # an earlier bump in this session.
        current_text = self._package_version() or project.version
        current = parse(current_text) if current_text else None
        if current is None:
            raise RNAgentError(
                f"package.json version is missing or unusable ({current_text or 'absent'})",
                hint='Set a semver "version" in package.json, for example "1.4.2".',
            )
        if self.version:
            explicit = parse(self.version)
            if explicit is None:
                raise RNAgentError(
                    f"{self.version} is not a semantic version",
                    hint="Use MAJOR.MINOR.PATCH, for example 2.0.0.",
                )
            return str(current), str(explicit)
        if self.bump not in {kind.value for kind in BumpKind} - {BumpKind.EXPLICIT.value}:
            raise RNAgentError(
                f"unknown bump: {self.bump}",
                hint="Use --bump major, minor or patch, or pass --version X.Y.Z.",
            )
        return str(current), str(_bumped(current, self.bump))

    def _package_version(self) -> str | None:
        payload = read_json(self.context.root / "package.json", default={})
        version = payload.get("version") if isinstance(payload, dict) else None
        return version if isinstance(version, str) else None

    # -- files that carry a version ---------------------------------------
    def _version_changes(
        self, current: str, next_version: str
    ) -> tuple[list[VersionChange], list[FileEdit], list[str]]:
        changes: list[VersionChange] = []
        edits: list[FileEdit] = []
        notes: list[str] = []

        manifest = read_text(self.context.root / "package.json")
        if manifest is not None:
            try:
                payload = json.loads(manifest)
            except (json.JSONDecodeError, ValueError) as exc:
                raise RNAgentError(f"package.json is not valid JSON: {exc}") from exc
            payload["version"] = next_version
            content = json.dumps(payload, indent=2, ensure_ascii=False)
            edits.append(
                FileEdit(
                    path="package.json",
                    action=EditAction.MODIFY,
                    content=content + ("\n" if manifest.endswith("\n") else ""),
                    reason=f"version {current} -> {next_version}",
                )
            )
            changes.append(
                VersionChange(
                    file="package.json", label="version", current=current, next=next_version
                )
            )

        gradle_change, gradle_edit, gradle_note = self._gradle(next_version)
        changes.extend(gradle_change)
        if gradle_edit is not None:
            edits.append(gradle_edit)
        notes.extend(gradle_note)

        ios_change, ios_edit, ios_note = self._pbxproj(next_version)
        changes.extend(ios_change)
        if ios_edit is not None:
            edits.append(ios_edit)
        notes.extend(ios_note)
        return changes, edits, notes

    def _gradle(
        self, next_version: str
    ) -> tuple[list[VersionChange], FileEdit | None, list[str]]:
        relative = "android/app/build.gradle"
        path = self.context.root / relative
        content = read_text(path)
        if content is None:
            if self.context.project.android.present:
                return [], None, [f"{relative} was not found; Android version not updated"]
            return [], None, []

        changes: list[VersionChange] = []
        updated = content
        name_match = _VERSION_NAME_RE.search(content)
        if name_match:
            changes.append(
                VersionChange(
                    file=relative,
                    label="versionName",
                    current=name_match.group("value"),
                    next=next_version,
                )
            )
            updated = _VERSION_NAME_RE.sub(rf"\g<1>{next_version}\g<3>", updated, count=1)
        code_match = _VERSION_CODE_RE.search(updated)
        if code_match:
            current_code = int(code_match.group("value"))
            changes.append(
                VersionChange(
                    file=relative,
                    label="versionCode",
                    current=str(current_code),
                    next=str(current_code + 1),
                )
            )
            updated = _VERSION_CODE_RE.sub(rf"\g<1>{current_code + 1}", updated, count=1)
        if not changes:
            return [], None, [f"{relative} carries no versionName/versionCode to update"]
        return (
            changes,
            FileEdit(
                path=relative,
                action=EditAction.MODIFY,
                content=updated,
                reason=f"release {next_version}",
            ),
            [],
        )

    def _pbxproj(
        self, next_version: str
    ) -> tuple[list[VersionChange], FileEdit | None, list[str]]:
        xcodeproj = self.context.project.ios.xcodeproj
        if not xcodeproj:
            if self.context.project.ios.present:
                return [], None, ["no .xcodeproj was found; iOS version not updated"]
            return [], None, []
        relative = f"{xcodeproj}/project.pbxproj"
        content = read_text(self.context.root / relative)
        if content is None:
            return [], None, [f"{relative} was not found; iOS version not updated"]

        changes: list[VersionChange] = []
        updated = content
        marketing = _MARKETING_RE.search(content)
        if marketing:
            changes.append(
                VersionChange(
                    file=relative,
                    label="MARKETING_VERSION",
                    current=marketing.group("value").strip(),
                    next=next_version,
                )
            )
            updated = _MARKETING_RE.sub(rf"\g<1>{next_version}\g<3>", updated)
        build = _PROJECT_VERSION_RE.search(updated)
        if build:
            current_build = build.group("value").strip()
            bumped = str(int(current_build) + 1) if current_build.isdigit() else current_build
            if bumped != current_build:
                changes.append(
                    VersionChange(
                        file=relative,
                        label="CURRENT_PROJECT_VERSION",
                        current=current_build,
                        next=bumped,
                    )
                )
                updated = _PROJECT_VERSION_RE.sub(rf"\g<1>{bumped}\g<3>", updated)
        if not changes:
            return (
                [],
                None,
                [f"{relative} carries no MARKETING_VERSION to update"],
            )
        return (
            changes,
            FileEdit(
                path=relative,
                action=EditAction.MODIFY,
                content=updated,
                reason=f"release {next_version}",
            ),
            [],
        )

    # -- history and notes -------------------------------------------------
    def _commits(self) -> tuple[str | None, list[str]]:
        """Subjects since the previous tag, chores filtered out."""
        runner = self.context.runner
        if not self.context.git.is_repository():
            return None, []
        described = runner.run(
            ["git", "describe", "--tags", "--abbrev=0"], timeout=20.0, force=True
        )
        tag = described.first_line() if described.ok else None
        argv = ["git", "log", "--pretty=%s"]
        if tag:
            argv.append(f"{tag}..HEAD")
        result = runner.run(argv, timeout=30.0, force=True)
        if not result.ok:
            return tag, []
        subjects = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return tag, [subject for subject in subjects if not _is_chore(subject)]

    def _blockers(self, commits: list[str]) -> list[str]:
        blockers: list[str] = []
        if self.context.git.is_repository():
            status = self.context.git.status()
            if status.dirty:
                blockers.append(
                    f"the git tree has {status.total_changes} uncommitted change(s); "
                    "commit or stash them first"
                )
        if not commits:
            blockers.append("no releasable commit since the previous tag")
        critical = self._critical_health_findings()
        if critical:
            blockers.append(
                f"the last health report lists {critical} critical issue(s); "
                "run `rn-agent health` and fix them"
            )
        return blockers

    def _critical_health_findings(self) -> int:
        """Read the stored health report; never run health from here."""
        payload = read_json(self.context.paths.cache_dir / "health-report.json", default=None)
        if not isinstance(payload, dict):
            return 0
        checks = payload.get("checks")
        if not isinstance(checks, list):
            return 0
        return sum(
            1
            for check in checks
            if isinstance(check, dict)
            and check.get("severity") == "critical"
            and check.get("status") in {"fail", "warn"}
        )

    def _changelog(
        self, project: ProjectContext, next_version: str, commits: list[str]
    ) -> tuple[list[str], str, dict[str, int]]:
        if not self.changelog or not commits:
            return [], "commits", {}
        if not self.context.ai_ready():
            return list(commits), "commits", {}
        engine = AIEngine(self.context)
        try:
            entries, notes = engine.changelog(
                changelog_messages(
                    project=project,
                    rules=ProjectRules.load(self.context.paths),
                    version=next_version,
                    commits=commits,
                )
            )
        except (ProviderError, ModelOutputError) as error:
            self.logger.warning("changelog fell back to commit subjects: %s", error.message)
            return list(commits), "commits", engine.usage
        _ = notes
        return entries, "model", engine.usage

    def _changelog_edit(self, next_version: str, entries: list[str]) -> FileEdit | None:
        """Prepend a section; never rewrite what is already in the file."""
        target = self.context.files.resolve(self.changelog_path)
        existing = read_text(target) or ""
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        section = "\n".join([f"## {next_version} - {today}", "", *(f"- {entry}" for entry in entries)])
        if existing.strip():
            body = f"{section}\n\n{existing.lstrip()}"
        else:
            body = f"# Changelog\n\n{section}\n"
        return FileEdit(
            path=self.changelog_path,
            action=EditAction.MODIFY if existing else EditAction.CREATE,
            content=body if body.endswith("\n") else f"{body}\n",
            reason=f"release notes for {next_version}",
        )

    def _checklist(self, analysis: ReleaseAnalysis) -> list[str]:
        """The steps the agent deliberately leaves to you."""
        version = analysis.next_version
        steps = [
            f"git commit -am \"release {version}\"",
            f"git tag v{version}",
            "git push && git push --tags",
        ]
        if analysis.project.android.present:
            steps.append("build the Android release bundle and upload it")
        if analysis.project.ios.present:
            steps.append("archive in Xcode (or fastlane) and upload the build")
        steps.append("run `rn-agent health` on the release branch before you ship")
        return steps


def _bumped(current: Version, bump: str) -> Version:
    if bump == BumpKind.MAJOR.value:
        return Version(current.major + 1, 0, 0)
    if bump == BumpKind.MINOR.value:
        return Version(current.major, current.minor + 1, 0)
    return Version(current.major, current.minor, current.patch + 1)


def _is_chore(subject: str) -> bool:
    lowered = subject.strip().lower()
    return any(re.search(pattern, lowered) for pattern in CHORE_PATTERNS)


register(ReleaseCommand, phase=6)
