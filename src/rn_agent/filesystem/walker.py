"""Source-file inventory.

Inside a git repository the authoritative "files that matter" list comes from
``git ls-files --cached --others --exclude-standard`` - one call, and it honours
``.gitignore`` exactly (§31). Outside a repository the walker falls back to
``os.walk`` with a skip list.

``.rn-agentignore`` patterns are layered on top of either source.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..constants import MAX_SOURCE_FILES, SOURCE_EXTENSIONS, SOURCE_SKIP_DIRS
from ..core.paths import AgentPaths
from ..models.project import SourceStats
from ..utils.io import read_text
from ..utils.redaction import is_secret_path

TEST_MARKERS = (".test.", ".spec.", "__tests__", "/e2e/", "/tests/")
COMPONENT_MARKERS = ("/components/", "/component/")
SCREEN_MARKERS = ("/screens/", "/screen/", "/pages/", "/views/")
HOOK_MARKERS = ("/hooks/", "/hook/")


@dataclass(slots=True)
class ProjectWalker:
    """Lists project source files, ignoring dependencies and build output."""

    paths: AgentPaths
    git_files: list[str] | None = None
    extra_ignores: tuple[str, ...] = field(default_factory=tuple)
    _cache: list[Path] | None = field(default=None, init=False, repr=False)

    @property
    def root(self) -> Path:
        return self.paths.project_root

    # -- ignore handling ---------------------------------------------------
    def agent_ignore_patterns(self) -> tuple[str, ...]:
        text = read_text(self.paths.ignore_file)
        if text is None:
            return ()
        patterns = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        return tuple(patterns)

    def is_ignored(self, relative: str) -> bool:
        posix = relative.replace(os.sep, "/")
        parts = posix.split("/")
        if any(part in SOURCE_SKIP_DIRS for part in parts):
            return True
        for pattern in self.extra_ignores + self.agent_ignore_patterns():
            candidate = pattern.rstrip("/")
            if fnmatch.fnmatch(posix, candidate) or fnmatch.fnmatch(posix, f"{candidate}/*"):
                return True
            if any(fnmatch.fnmatch(part, candidate) for part in parts):
                return True
        return False

    # -- listing -----------------------------------------------------------
    def all_files(self) -> list[Path]:
        if self._cache is not None:
            return self._cache
        if self.git_files:
            found = [
                self.root / relative
                for relative in self.git_files
                if not self.is_ignored(relative)
            ]
        else:
            found = self._walk()
        self._cache = found[:MAX_SOURCE_FILES]
        return self._cache

    def _walk(self) -> list[Path]:
        collected: list[Path] = []
        for current, dirnames, filenames in os.walk(self.root, topdown=True, followlinks=False):
            dirnames[:] = [
                name
                for name in dirnames
                if name not in SOURCE_SKIP_DIRS and not name.startswith(".")
            ]
            for name in filenames:
                path = Path(current) / name
                relative = os.path.relpath(path, self.root)
                if self.is_ignored(relative):
                    continue
                collected.append(path)
                if len(collected) >= MAX_SOURCE_FILES:
                    return collected
        return collected

    def source_files(self) -> list[Path]:
        return [path for path in self.all_files() if path.suffix in SOURCE_EXTENSIONS]

    def context_candidates(self) -> list[Path]:
        """Files an AI prompt may quote from: source only, secrets excluded."""
        return [path for path in self.source_files() if not is_secret_path(path)]

    def top_level_dirs(self) -> list[str]:
        try:
            entries = sorted(
                entry.name
                for entry in os.scandir(self.root)
                if entry.is_dir()
                and not entry.name.startswith(".")
                and entry.name not in SOURCE_SKIP_DIRS
            )
        except OSError:  # pragma: no cover - unreadable project root
            return []
        return entries

    # -- statistics --------------------------------------------------------
    def stats(self) -> SourceStats:
        files = self.source_files()
        typescript = sum(1 for path in files if path.suffix in {".ts", ".tsx"})
        javascript = len(files) - typescript
        tests = 0
        components = 0
        screens = 0
        hooks = 0
        total_lines = 0
        sizes: list[tuple[int, str]] = []

        for path in files:
            posix = f"/{path.relative_to(self.root).as_posix()}"
            lowered = posix.lower()
            if any(marker in lowered for marker in TEST_MARKERS):
                tests += 1
            if any(marker in lowered for marker in COMPONENT_MARKERS):
                components += 1
            if any(marker in lowered for marker in SCREEN_MARKERS):
                screens += 1
            if any(marker in lowered for marker in HOOK_MARKERS) or path.name.startswith("use"):
                hooks += 1
            text = read_text(path)
            if text is None:
                continue
            lines = text.count("\n") + 1
            total_lines += lines
            sizes.append((lines, posix.lstrip("/")))

        sizes.sort(reverse=True)
        return SourceStats(
            files=len(files),
            typescript_files=typescript,
            javascript_files=javascript,
            test_files=tests,
            component_files=components,
            screen_files=screens,
            hook_files=hooks,
            total_lines=total_lines,
            largest_files=[{"path": name, "lines": lines} for lines, name in sizes[:10]],
            top_level_dirs=self.top_level_dirs(),
        )
