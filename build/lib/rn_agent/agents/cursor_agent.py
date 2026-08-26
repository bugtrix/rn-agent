"""Handing a whole task to the Cursor agent, and holding it to this project's rules.

Every other AI path in this agent is "ask a model for a proposal, then apply it
through :class:`FileManager` with backups, rules and rollback". Cursor's agent is
a different animal: it has its own tools and edits the tree itself. Pretending
otherwise would either waste it or smuggle unreviewed writes past the safety
envelope, so this module makes the trade explicit instead.

What brackets the run:

1. **A git repository with a clean tree.** That is the whole backup strategy, and
   it is an honest one: if HEAD equals the tree before the agent starts, then
   ``git restore .`` afterwards is an exact undo. Without it there is nothing to
   go back to, so the run is refused unless the developer insists.
2. **A branch of its own**, so the previous branch tip is never moved.
3. **A deny list the agent cannot argue with.** ``.cursor/cli.json`` is Cursor's
   own project-level permission file; the rules in ``.rn-agent/rules.yaml`` are
   translated into ``permissions.deny`` entries before the agent starts. Lockfiles
   and secrets are denied unconditionally.
4. **A rules audit of what actually changed**, not of what was promised.
5. **Validation**, through the same :class:`ProjectValidator` every other command
   uses, so "it still builds" means the same thing here as everywhere else.

This module never runs a destructive git command. When validation fails it prints
the command that discards the work and lets the developer decide - the same rule
:class:`GitManager` follows.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.logging import get_logger
from ..errors import RNAgentError
from ..models.proposal import EditAction, FileEdit
from ..runner.command_runner import CommandRunner
from ..tools.cursor import MISSING_HINT, resolve_binary
from ..utils.io import read_text
from .rules import ProjectRules, RuleViolation

#: Cursor's project-level permission file.
CLI_CONFIG = ".cursor/cli.json"

#: Denied whatever the project's rules say. A lockfile is generated, never
#: hand-edited, and nothing should be reading or rewriting credentials.
ALWAYS_DENY: tuple[str, ...] = (
    "Write(**/package-lock.json)",
    "Write(**/yarn.lock)",
    "Write(**/pnpm-lock.yaml)",
    "Write(**/bun.lockb)",
    "Write(**/Podfile.lock)",
    "Write(**/*.keystore)",
    "Write(**/*.jks)",
    "Write(**/*.mobileprovision)",
    "Write(**/*.p12)",
    "Write(**/*.pem)",
    "Write(**/*.key)",
    "Write(**/.env*)",
    "Read(**/.env*)",
    "Write(.rn-agent/**)",
    "Shell(rm)",
)

#: Native build files, denied unless the task is explicitly about them.
NATIVE_DENY: tuple[str, ...] = (
    "Write(android/**)",
    "Write(ios/**)",
)

DEPENDENCY_DENY: tuple[str, ...] = ("Write(package.json)",)


@dataclass(frozen=True, slots=True)
class DelegationOutcome:
    """What the agent did, and whether it is allowed to stand."""

    ran: bool
    changed: tuple[str, ...] = ()
    violations: tuple[RuleViolation, ...] = ()
    branch: str | None = None
    summary: str = ""
    duration_ms: int = 0
    #: Set when the tree was dirty before the run, so "restore" is not exact.
    recoverable: bool = True

    @property
    def clean(self) -> bool:
        return self.ran and not self.violations

    def as_dict(self) -> dict[str, Any]:
        return {
            "ran": self.ran,
            "changed": list(self.changed),
            "violations": [
                {"rule": item.rule, "path": item.path, "detail": item.detail}
                for item in self.violations
            ],
            "branch": self.branch,
            "summary": self.summary,
            "duration_ms": self.duration_ms,
            "recoverable": self.recoverable,
        }


@dataclass(slots=True)
class CursorAgentRunner:
    """Runs the Cursor agent inside this project's guard rails."""

    root: Path
    runner: CommandRunner
    rules: ProjectRules
    model: str | None = None
    timeout: float = 900.0
    allow_native: bool = False
    allow_dependencies: bool = False
    allowed_native_paths: tuple[str, ...] = ()
    credential: str | None = None
    logger: logging.Logger = field(default_factory=lambda: get_logger("agents"))

    def _native_confirmed(self) -> bool:
        return (
            self.allow_native
            or bool(self.rules.allow_native_paths)
            or bool(self.allowed_native_paths)
        )

    # -- permissions -------------------------------------------------------
    def deny_list(self) -> list[str]:
        """The project's rules, in Cursor's permission vocabulary."""
        denied = list(ALWAYS_DENY)
        if self.rules.forbid_native_edits_without_confirmation and not self._native_confirmed():
            denied.extend(NATIVE_DENY)
        if self.rules.forbid_new_dependencies and not self.allow_dependencies:
            denied.extend(DEPENDENCY_DENY)
        return denied

    def write_permissions(self) -> tuple[Path, str | None]:
        """Merge the deny list into ``.cursor/cli.json``.

        Returns the file and whatever was there before, so the caller can put it
        back. Merging matters: a developer's own allow list is theirs, and this
        agent has no business deleting it.
        """
        path = self.root / CLI_CONFIG
        previous = read_text(path)
        payload: dict[str, Any] = {}
        if previous:
            try:
                loaded = json.loads(previous)
            except json.JSONDecodeError:
                # Leave a broken file alone rather than overwrite it silently.
                raise RNAgentError(
                    f"{CLI_CONFIG} is not valid JSON",
                    hint="Fix or remove it; rn-agent will not overwrite a file it cannot read.",
                ) from None
            if isinstance(loaded, dict):
                payload = loaded
        permissions = payload.get("permissions")
        if not isinstance(permissions, dict):
            permissions = {}
        existing = permissions.get("deny")
        merged = list(existing) if isinstance(existing, list) else []
        for entry in self.deny_list():
            if entry not in merged:
                merged.append(entry)
        permissions["deny"] = merged
        payload["permissions"] = permissions
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return path, previous

    @staticmethod
    def restore_permissions(path: Path, previous: str | None) -> None:
        """Put the developer's file back exactly, or remove every trace of ours."""
        if previous is not None:
            path.write_text(previous, encoding="utf-8")
            return
        path.unlink(missing_ok=True)
        # Leaving an empty `.cursor/` behind would show up as an untracked
        # directory the developer never created. Only remove it if it is empty.
        parent = path.parent
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()

    # -- the run -----------------------------------------------------------
    def executable(self) -> str:
        found = resolve_binary(runner=self.runner)
        if found is not None:
            return str(found)
        raise RNAgentError(
            "the Cursor CLI is not installed",
            hint=MISSING_HINT,
        )

    def argv(self, task: str) -> list[str]:
        """The command line. ``--force`` is the point of this command.

        ``delegate`` exists to let Cursor edit, so the flag that permits writes is
        deliberate here and absent from :class:`CursorProvider`. The deny list is
        what keeps ``--force`` from meaning "anything": Cursor's own docs are
        explicit that deny beats allow and beats ``--force``.
        """
        argv = [
            self.executable(),
            "--print",
            "--output-format",
            "json",
            "--trust",
            "--force",
            "--workspace",
            str(self.root),
        ]
        if self.model:
            argv += ["--model", self.model]
        argv.append(task)
        return argv

    def prompt(self, task: str) -> str:
        """The task, with this project's rules attached.

        Cursor reads the repository itself, so this is deliberately short: the
        constraints it cannot infer, and the job. Sending rn-agent's whole file
        context here would duplicate what the agent is about to read anyway.
        """
        lines = [task.strip(), "", "Constraints from this project's rules:"]
        lines.extend(self.rules.as_prompt_lines())
        lines += [
            "",
            "Make the change directly in the working tree. Do not commit, do not "
            "push, and do not run git. Report what you changed and why.",
        ]
        return "\n".join(lines)

    def run(self, task: str) -> tuple[str, int]:
        """Execute the agent. Returns its summary text and the duration."""
        from ..cli.working import working

        with working():
            result = self.runner.run(
                self.argv(self.prompt(task)),
                cwd=self.root,
                timeout=self.timeout,
                env={"CURSOR_API_KEY": self.credential} if self.credential else None,
                force=True,
            )
        if result.timed_out:
            raise RNAgentError(
                f"the Cursor agent did not finish within {self.timeout:.0f}s",
                hint="Raise --timeout, or give it a smaller task.",
            )
        if not result.ok:
            raise RNAgentError(
                f"the Cursor agent failed: {result.tail(5) or result.stderr.strip()[:200]}",
                hint="Run the same task with `cursor-agent -p --force` to see its own output.",
            )
        return _summary(result.stdout), result.duration_ms

    # -- audit -------------------------------------------------------------
    def audit(self, changed: list[str]) -> list[RuleViolation]:
        """Hold the diff to the same rules a model's proposal would face.

        The edits are read back off disk rather than taken on trust, so the audit
        describes what is actually in the tree.
        """
        edits: list[FileEdit] = []
        for path in changed:
            absolute = self.root / path
            if absolute.is_file():
                edits.append(
                    FileEdit(path=path, action=EditAction.MODIFY, content=read_text(absolute) or "")
                )
            else:
                edits.append(FileEdit(path=path, action=EditAction.DELETE))
        return self.rules.violations(
            edits,
            allow_dependencies=self.allow_dependencies,
            allow_native=self.allow_native,
            allowed_native_paths=self.allowed_native_paths,
        )


def _summary(stdout: str) -> str:
    """The agent's own account of what it did."""
    text = stdout.strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text[:2000]
    if isinstance(parsed, dict):
        value = parsed.get("result")
        if isinstance(value, str):
            return value
    return text[:2000]
