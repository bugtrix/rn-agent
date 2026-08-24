"""The one place the agent shells out.

Every external tool (node, yarn, git, gradlew, pod, xcodebuild) goes through
:class:`CommandRunner` so that timeouts, logging, redaction and dry-run
behaviour are uniform and testable. Commands are always argv lists - never a
shell string - so project paths with spaces cannot turn into injection.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..constants import DEFAULT_COMMAND_TIMEOUT
from ..core.logging import get_logger
from ..utils.redaction import redact


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Outcome of one external command."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    cwd: str
    timed_out: bool = False
    skipped: bool = False
    executable_missing: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.executable_missing

    @property
    def command(self) -> str:
        return " ".join(self.argv)

    @property
    def output(self) -> str:
        """stdout plus stderr, in that order, stripped."""
        return "\n".join(part for part in (self.stdout, self.stderr) if part).strip()

    def first_line(self) -> str:
        return self.output.splitlines()[0].strip() if self.output else ""

    def tail(self, lines: int = 40) -> str:
        return "\n".join(self.output.splitlines()[-lines:])


@dataclass(slots=True)
class CommandRunner:
    """Runs external tools with a project working directory."""

    cwd: Path
    dry_run: bool = False
    default_timeout: float = DEFAULT_COMMAND_TIMEOUT
    env_overrides: dict[str, str] = field(default_factory=dict)
    logger: logging.Logger = field(default_factory=lambda: get_logger("runner"))
    history: list[CommandResult] = field(default_factory=list)

    # -- discovery ---------------------------------------------------------
    def which(self, executable: str) -> str | None:
        return shutil.which(executable)

    def available(self, executable: str) -> bool:
        return self.which(executable) is not None

    def tool_version(
        self, executable: str, args: Sequence[str] = ("--version",), *, timeout: float = 20.0
    ) -> str | None:
        """First line of ``tool --version``, or ``None`` when unavailable.

        Runs even in dry-run mode: reading a version changes nothing.
        """
        if not self.available(executable):
            return None
        result = self.run([executable, *args], timeout=timeout, check=False, force=True)
        if not result.ok:
            return None
        line = result.first_line()
        return line or None

    # -- execution ---------------------------------------------------------
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: float | None = None,
        check: bool = False,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        force: bool = False,
        quiet: bool = False,
    ) -> CommandResult:
        """Execute ``argv``. Never raises unless ``check`` is set.

        ``quiet`` is for probes whose failure is an expected answer (does this
        keychain item exist?), so a normal "no" never looks like a fault.
        """
        argv = tuple(str(part) for part in argv)
        workdir = Path(cwd) if cwd else self.cwd

        if self.dry_run and not force:
            self.logger.info("dry-run: would execute %s (cwd=%s)", " ".join(argv), workdir)
            result = CommandResult(
                argv=argv,
                returncode=0,
                stdout="",
                stderr="",
                duration_ms=0,
                cwd=str(workdir),
                skipped=True,
            )
            self.history.append(result)
            return result

        merged_env = {**os.environ, **self.env_overrides, **(env or {})}
        started = time.perf_counter()
        self.logger.debug("executing %s (cwd=%s)", " ".join(argv), workdir)
        try:
            completed = subprocess.run(  # noqa: S603 - argv list, never shell
                argv,
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=timeout if timeout is not None else self.default_timeout,
                env=merged_env,
                input=input_text,
                check=False,
            )
            result = CommandResult(
                argv=argv,
                returncode=completed.returncode,
                stdout=completed.stdout or "",
                stderr=completed.stderr or "",
                duration_ms=int((time.perf_counter() - started) * 1000),
                cwd=str(workdir),
            )
        except FileNotFoundError:
            result = CommandResult(
                argv=argv,
                returncode=127,
                stdout="",
                stderr=f"{argv[0]}: command not found",
                duration_ms=int((time.perf_counter() - started) * 1000),
                cwd=str(workdir),
                executable_missing=True,
            )
        except subprocess.TimeoutExpired as exc:
            result = CommandResult(
                argv=argv,
                returncode=124,
                stdout=_decode(exc.stdout),
                stderr=_decode(exc.stderr) or f"timed out after {exc.timeout}s",
                duration_ms=int((time.perf_counter() - started) * 1000),
                cwd=str(workdir),
                timed_out=True,
            )
        except OSError as exc:  # pragma: no cover - permission/exec format errors
            result = CommandResult(
                argv=argv,
                returncode=126,
                stdout="",
                stderr=str(exc),
                duration_ms=int((time.perf_counter() - started) * 1000),
                cwd=str(workdir),
            )

        self.history.append(result)
        if not result.ok:
            log = self.logger.debug if quiet else self.logger.warning
            log(
                "command failed (%s): %s\n%s",
                result.returncode,
                result.command,
                redact(result.tail(20)),
            )
        if check and not result.ok:
            from ..errors import CommandExecutionError

            raise CommandExecutionError(
                f"`{result.command}` failed with exit code {result.returncode}",
                hint=redact(result.tail(10)) or None,
            )
        return result

    def node_eval(self, script: str, *, timeout: float = 30.0) -> CommandResult:
        """Run a snippet through Node - used for resolving package metadata."""
        return self.run(["node", "-e", script], timeout=timeout, force=True)


def _decode(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value
