"""Git manager (subprocess based - no extra dependency).

Requirement §13: know whether we are in a repository, whether the tree is
dirty, warn before modifying, and create a branch for large operations.

Hard rule enforced here: this class contains **no** destructive git operation.
``git reset --hard`` and ``git clean -fd`` are not implemented at all, so no
code path - and no AI suggestion - can invoke them through the agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..core.logging import get_logger
from ..errors import GitError
from ..models.project import GitInfo
from ..runner.command_runner import CommandRunner

FORBIDDEN_ARGUMENTS = frozenset({"--hard", "-fd", "-fdx", "--force"})


@dataclass(frozen=True, slots=True)
class GitStatus:
    """Parsed ``git status --porcelain=v1``."""

    branch: str | None
    detached: bool
    modified: tuple[str, ...] = ()
    staged: tuple[str, ...] = ()
    untracked: tuple[str, ...] = ()
    conflicted: tuple[str, ...] = ()

    @property
    def dirty(self) -> bool:
        return bool(self.modified or self.staged or self.untracked or self.conflicted)

    @property
    def total_changes(self) -> int:
        return len(self.modified) + len(self.staged) + len(self.untracked) + len(self.conflicted)


@dataclass(slots=True)
class GitManager:
    """Read-only by default; the only mutation offered is branch creation."""

    root: Path
    runner: CommandRunner
    logger: object = field(default_factory=lambda: get_logger("git"))

    # -- basics ------------------------------------------------------------
    def available(self) -> bool:
        return self.runner.available("git")

    def _git(self, *args: str, force: bool = True) -> tuple[bool, str]:
        result = self.runner.run(["git", *args], cwd=self.root, timeout=30.0, force=force)
        return result.ok, result.output

    def _git_raw(self, *args: str) -> tuple[bool, str]:
        """Like :meth:`_git` but preserves leading whitespace.

        ``git status --porcelain`` encodes the index state in column 1 and the
        worktree state in column 2, so ``" M file"`` (unstaged modification)
        must not be stripped into ``"M file"`` (staged modification).
        """
        result = self.runner.run(["git", *args], cwd=self.root, timeout=30.0, force=True)
        return result.ok, result.stdout

    def is_repository(self) -> bool:
        if not self.available():
            return False
        ok, output = self._git("rev-parse", "--is-inside-work-tree")
        return ok and output.strip().startswith("true")

    def repository_root(self) -> Path | None:
        ok, output = self._git("rev-parse", "--show-toplevel")
        if not ok or not output.strip():
            return None
        return Path(output.strip())

    def current_branch(self) -> tuple[str | None, bool]:
        """``(branch, detached)``."""
        ok, output = self._git("symbolic-ref", "--quiet", "--short", "HEAD")
        if ok and output.strip():
            return output.strip(), False
        ok, output = self._git("rev-parse", "--short", "HEAD")
        return (output.strip() or None, True) if ok else (None, False)

    def status(self) -> GitStatus:
        branch, detached = self.current_branch()
        ok, output = self._git_raw("status", "--porcelain=v1", "--untracked-files=normal")
        if not ok:
            return GitStatus(branch=branch, detached=detached)
        modified: list[str] = []
        staged: list[str] = []
        untracked: list[str] = []
        conflicted: list[str] = []
        for line in output.splitlines():
            if len(line) < 4:
                continue
            index_state, worktree_state, path = line[0], line[1], line[3:].strip()
            if index_state == "?" and worktree_state == "?":
                untracked.append(path)
                continue
            if "U" in (index_state, worktree_state) or (
                index_state == "A" and worktree_state == "A"
            ):
                conflicted.append(path)
                continue
            if index_state not in (" ", "?"):
                staged.append(path)
            if worktree_state not in (" ", "?"):
                modified.append(path)
        return GitStatus(
            branch=branch,
            detached=detached,
            modified=tuple(modified),
            staged=tuple(staged),
            untracked=tuple(untracked),
            conflicted=tuple(conflicted),
        )

    def last_commit(self) -> tuple[str | None, str | None]:
        ok, output = self._git("log", "-1", "--pretty=%h%x1f%s")
        if not ok or "\x1f" not in output:
            return None, None
        commit, subject = output.strip().split("\x1f", 1)
        return commit or None, subject or None

    def remotes(self) -> list[str]:
        ok, output = self._git("remote")
        return [line.strip() for line in output.splitlines() if line.strip()] if ok else []

    def is_ignored(self, path: str) -> bool:
        """Ask git whether a path is ignored.

        Directory patterns in .gitignore carry a trailing slash and only match
        directories, which git cannot verify for a path that does not exist yet.
        Probing a file *inside* the directory answers the question either way.
        """
        candidates = [path, f"{path.rstrip('/')}/.rn-agent-probe"]
        for candidate in candidates:
            result = self.runner.run(
                ["git", "check-ignore", "-q", candidate], cwd=self.root, timeout=15.0, force=True
            )
            if result.returncode == 0:
                return True
        return False

    def tracked_files(self, *patterns: str) -> list[str]:
        ok, output = self._git("ls-files", *patterns)
        return [line.strip() for line in output.splitlines() if line.strip()] if ok else []

    # -- reporting ---------------------------------------------------------
    def describe(self) -> GitInfo:
        """Snapshot for the project context. Never raises."""
        if not self.is_repository():
            return GitInfo(repository=False)
        status = self.status()
        commit, subject = self.last_commit()
        repository_root = self.repository_root()
        return GitInfo(
            repository=True,
            root=str(repository_root) if repository_root else None,
            branch=status.branch,
            detached=status.detached,
            dirty=status.dirty,
            untracked=len(status.untracked),
            modified=len(status.modified),
            staged=len(status.staged),
            last_commit=commit,
            last_commit_subject=subject,
            remotes=self.remotes(),
            ignores_agent_dir=self.is_ignored(".rn-agent/cache"),
        )

    # -- safety gates ------------------------------------------------------
    def require_repository(self) -> None:
        if not self.is_repository():
            raise GitError(
                "this project is not a git repository",
                hint="Run `git init` first - the agent will not modify files it cannot help you undo.",
            )

    def require_clean(self, *, allow_untracked: bool = True) -> None:
        self.require_repository()
        status = self.status()
        blocking = list(status.modified) + list(status.staged) + list(status.conflicted)
        if not allow_untracked:
            blocking += list(status.untracked)
        if blocking:
            preview = ", ".join(blocking[:5])
            more = f" (+{len(blocking) - 5} more)" if len(blocking) > 5 else ""
            raise GitError(
                f"git tree has uncommitted changes: {preview}{more}",
                hint="Commit or stash your work, or re-run with --allow-dirty.",
            )

    def branch_exists(self, name: str) -> bool:
        result = self.runner.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
            cwd=self.root,
            timeout=15.0,
            force=True,
        )
        return result.returncode == 0

    def create_branch(self, name: str, *, checkout: bool = True) -> str:
        """Create (and optionally switch to) a branch for a large operation."""
        self.require_repository()
        candidate = name
        suffix = 2
        while self.branch_exists(candidate):
            candidate = f"{name}-{suffix}"
            suffix += 1
        args = ["checkout", "-b", candidate] if checkout else ["branch", candidate]
        result = self.runner.run(["git", *args], cwd=self.root, timeout=30.0)
        if result.skipped:
            return candidate
        if not result.ok:
            raise GitError(f"could not create branch {candidate}: {result.tail(5)}")
        return candidate

    def diff_names(self, *, staged: bool = False) -> list[str]:
        args = ["diff", "--name-only"]
        if staged:
            args.append("--cached")
        ok, output = self._git(*args)
        return [line.strip() for line in output.splitlines() if line.strip()] if ok else []
