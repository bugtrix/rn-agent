"""The shared project brain, wired once per invocation.

Requirement §2: every command shares one ``ProjectContext``, ``AIProvider``,
``KnowledgeStore``, ``GitManager``, ``FileManager``, ``CommandRunner``,
``ChangeManager``, ``SafetyManager`` and ``Logger``. This class *is* that
sharing point - commands receive it and never build their own collaborators.

Construction is lazy: ``scan`` needs the filesystem but not the stored context,
``health`` needs the stored context but no writer, so each piece is created on
first use and cached.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..constants import APP_VERSION
from ..filesystem.manager import FileManager
from ..filesystem.walker import ProjectWalker
from ..git.manager import GitManager
from ..knowledge.data import KnowledgeData, load_knowledge_data
from ..knowledge.store import KnowledgeStore
from ..models.config import AgentConfig
from ..models.project import ProjectContext
from ..project.detector import DetectedProject, detect_project
from ..project.scanner import load_context
from ..runner.command_runner import CommandRunner
from ..safety.manager import SafetyManager
from .config import load_config
from .logging import configure_logging, get_logger
from .paths import AgentPaths

if TYPE_CHECKING:  # the AI stack is imported on first use, not at startup
    from ..ai.provider import AIProvider
    from ..ai.types import Completion
    from ..auth.store import CredentialStore


@dataclass
class AgentContext:
    """Everything a command may need. Built by the CLI, injected everywhere."""

    detected: DetectedProject
    paths: AgentPaths
    config: AgentConfig
    command: str = "rn-agent"
    dry_run: bool = False
    assume_yes: bool = False
    verbose: bool = False
    confirmer: Callable[[str, bool], bool] | None = None
    logger: logging.Logger = field(default_factory=lambda: get_logger())
    run_id: int | None = None
    _project_context: ProjectContext | None = field(default=None, repr=False)

    # -- construction ------------------------------------------------------
    @classmethod
    def create(
        cls,
        *,
        command: str,
        start: Path | None = None,
        dry_run: bool = False,
        assume_yes: bool = False,
        verbose: bool = False,
        confirmer: Callable[[str, bool], bool] | None = None,
        detected: DetectedProject | None = None,
    ) -> AgentContext:
        project = detected or detect_project(start)
        paths = AgentPaths.for_project(project.root)
        config = load_config(paths)
        level = "DEBUG" if verbose else config.logging.level
        # Every real run gets its own log file, even the first one (before any
        # scan has created .rn-agent). A dry run only logs when the directory
        # already exists, so it never leaves a trace in a fresh project.
        log_dir: Path | None = None
        if config.logging.keep_logs:
            if not dry_run:
                paths.ensure()
                log_dir = paths.logs_dir
            elif paths.exists():
                log_dir = paths.logs_dir
        logger = configure_logging(
            log_dir,
            command=command,
            level=level,
            enabled=config.logging.keep_logs,
        )
        return cls(
            detected=project,
            paths=paths,
            config=config,
            command=command,
            dry_run=dry_run,
            assume_yes=assume_yes,
            verbose=verbose,
            confirmer=confirmer,
            logger=logger,
        )

    # -- identity ----------------------------------------------------------
    @property
    def root(self) -> Path:
        return self.detected.root

    @property
    def agent_version(self) -> str:
        return APP_VERSION

    # -- shared collaborators ---------------------------------------------
    @cached_property
    def runner(self) -> CommandRunner:
        return CommandRunner(
            cwd=self.root,
            dry_run=self.dry_run,
            logger=get_logger("runner"),
        )

    @cached_property
    def git(self) -> GitManager:
        return GitManager(root=self.root, runner=self.runner)

    @cached_property
    def files(self) -> FileManager:
        return FileManager(
            paths=self.paths,
            command=self.command,
            dry_run=self.dry_run,
            create_backups=self.config.safety.create_backups,
        )

    @cached_property
    def safety(self) -> SafetyManager:
        return SafetyManager(
            config=self.config.safety,
            dry_run=self.dry_run,
            assume_yes=self.assume_yes,
            confirmer=self.confirmer,
        )

    @cached_property
    def knowledge(self) -> KnowledgeData:
        return load_knowledge_data()

    @cached_property
    def store(self) -> KnowledgeStore:
        return KnowledgeStore(self.paths.knowledge_db if not self.dry_run else ":memory:")

    @cached_property
    def walker(self) -> ProjectWalker:
        git_files: list[str] | None = None
        if self.git.is_repository():
            result = self.runner.run(
                ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                cwd=self.root,
                timeout=45.0,
                force=True,
            )
            if result.ok:
                git_files = [line for line in result.stdout.splitlines() if line.strip()]
        return ProjectWalker(paths=self.paths, git_files=git_files)

    # -- AI (lazy on purpose: `scan` and `health` never build a provider) ---
    @cached_property
    def credentials(self) -> CredentialStore:
        from ..auth.session import build_store

        return build_store(logger=get_logger("auth"))

    @cached_property
    def ai(self) -> AIProvider:
        """The configured provider, built from *your* credential.

        Raises :class:`ProviderError` when AI is unconfigured or disabled, so a
        command that needs a model fails with an actionable message instead of
        silently degrading.
        """
        from ..ai.registry import build_provider, resolve_spec
        from ..errors import ProviderError

        if not self.config.ai.enabled:
            raise ProviderError(
                "AI is disabled for this project (ai.enabled: false)",
                hint="Set ai.enabled: true in .rn-agent/config.yaml to use AI commands.",
            )
        spec = resolve_spec(self.config.ai.provider)
        credential = self.credentials.require(spec)
        return build_provider(
            self.config.ai,
            credential=credential.value if credential else None,
            provider_name=spec.name,
            logger=get_logger("ai"),
        )

    def ai_ready(self) -> bool:
        """Whether an AI request could be made - no network, no exception."""
        from ..errors import ProviderError

        try:
            return self.ai is not None
        except ProviderError:
            return False

    def record_ai_usage(self, completion: Completion) -> None:
        """Account for one model call in the project's knowledge store."""
        if self.dry_run:
            return
        try:
            self.store.record_ai_usage(
                command=self.command,
                provider=completion.provider,
                model=completion.model,
                task=completion.task,
                input_tokens=completion.usage.input_tokens,
                output_tokens=completion.usage.output_tokens,
            )
        except Exception as exc:  # accounting must never break a command
            self.logger.warning("could not record AI usage: %s", exc)

    # -- the brain ---------------------------------------------------------
    @property
    def project(self) -> ProjectContext:
        """The stored scan result. Raises ``ProjectNotScanned`` if absent."""
        if self._project_context is None:
            self._project_context = load_context(self.paths)
        return self._project_context

    def set_project(self, context: ProjectContext) -> ProjectContext:
        self._project_context = context
        return context

    def has_project_context(self) -> bool:
        return self._project_context is not None or self.paths.context_file.is_file()

    def ensure_project(
        self,
        *,
        refresh: bool = False,
        probe_tools: bool = False,
        stale_seconds: float = 24 * 60 * 60,
    ) -> tuple[ProjectContext, bool]:
        """The brain, rescanned when it is missing, stale or unusable.

        Returns ``(project, refreshed)``. Every command past phase 1 uses this
        rather than raising ``ProjectNotScanned``: a developer should not have to
        remember to re-run ``scan`` before asking a question about their project,
        and a stale answer is worse than a two-second rescan.
        """
        from ..project.scanner import ProjectScanner, context_age_seconds, save_context

        age = context_age_seconds(self.paths)
        stale = age is not None and age > stale_seconds
        if not refresh and not stale and self.has_project_context():
            try:
                return self.project, False
            except Exception as exc:  # a corrupt context file must not be fatal
                self.logger.warning("stored context unusable (%s); rescanning", exc)

        scanner = ProjectScanner(self.detected, self.paths, self.runner, knowledge=self.knowledge)
        project = scanner.scan(
            probe_tools=probe_tools,
            git_info=self.git.describe(),
            source_stats=self.walker.stats(),
        )
        self.set_project(project)
        if not self.dry_run:
            try:
                save_context(self.paths, project)
            except OSError as exc:  # pragma: no cover - read-only project
                self.logger.warning("could not persist refreshed context: %s", exc)
        return project, True

    # -- run bookkeeping ---------------------------------------------------
    def begin_run(self) -> int | None:
        if self.dry_run:
            return None
        try:
            self.run_id = self.store.start_run(
                self.command, dry_run=self.dry_run, agent_version=self.agent_version
            )
        except Exception as exc:  # storage must never break a command
            self.logger.warning("could not record run: %s", exc)
            self.run_id = None
        return self.run_id

    def end_run(self, *, status: str, exit_code: int = 0, summary: dict[str, Any] | None = None) -> None:
        if self.run_id is None:
            return
        try:
            self.store.finish_run(
                self.run_id, status=status, exit_code=exit_code, summary=summary or {}
            )
        except Exception as exc:  # pragma: no cover - storage failure
            self.logger.warning("could not finalise run: %s", exc)

    def close(self) -> None:
        if "store" in self.__dict__:
            self.store.close()
