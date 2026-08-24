"""Choosing what a model is allowed to see.

Three limits apply to every prompt, and all three are the developer's
configuration rather than this module's opinion:

* ``context.allow_secret_files`` - ``.env``, keystores and provisioning
  profiles are dropped by ``SafetyManager``, and the dropped list is reported;
* ``context.max_file_kb`` - a single huge file is truncated, and says so;
* ``ai.max_context_files`` / ``ai.max_context_tokens`` - the budget stops the
  selection, and everything dropped for budget is listed too.

Nothing is selected silently: :class:`PromptContext` carries what went in, what
was refused and what did not fit, so ``--verbose`` can show the developer
exactly which bytes left their machine.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from ..core.context import AgentContext
from ..errors import UnsafePathError
from ..utils.io import read_text

#: Files worth sending even when the developer named no paths at all.
ENTRY_POINTS: tuple[str, ...] = (
    "index.js",
    "index.ts",
    "index.tsx",
    "App.tsx",
    "App.jsx",
    "App.ts",
    "App.js",
)

#: Directory markers, most interesting first, used to rank candidates.
DIRECTORY_PRIORITY: tuple[tuple[str, int], ...] = (
    ("/screens/", 5),
    ("/components/", 5),
    ("/hooks/", 4),
    ("/navigation/", 4),
    ("/store/", 4),
    ("/services/", 3),
    ("/api/", 3),
    ("/utils/", 2),
    ("/lib/", 2),
)

SOURCE_SUFFIXES: frozenset[str] = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})

LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".ts": "ts",
    ".tsx": "tsx",
    ".js": "js",
    ".jsx": "jsx",
    ".mjs": "js",
    ".cjs": "js",
    ".json": "json",
    ".gradle": "groovy",
    ".kt": "kotlin",
    ".java": "java",
    ".swift": "swift",
    ".m": "objectivec",
    ".mm": "objectivec",
    ".rb": "ruby",
    ".md": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
}


def estimate_tokens(text: str) -> int:
    """Cheap, deterministic token estimate (~4 characters per token).

    Deliberately not a tokenizer: the budget must be identical for every
    provider and must not add a dependency to count characters.
    """
    return len(text) // 4 + 1


@dataclass(frozen=True, slots=True)
class ContextFile:
    """One file as the model will see it."""

    path: str
    content: str
    truncated: bool = False

    @property
    def lines(self) -> int:
        return self.content.count("\n") + 1

    @property
    def language(self) -> str:
        return LANGUAGE_BY_SUFFIX.get(Path(self.path).suffix, "")

    def render(self) -> str:
        suffix = "  (truncated)" if self.truncated else ""
        return f"### {self.path}{suffix}\n```{self.language}\n{self.content}\n```"


@dataclass(frozen=True, slots=True)
class PromptContext:
    """The files selected for one prompt, plus everything left out."""

    files: tuple[ContextFile, ...] = ()
    refused: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    approx_tokens: int = 0

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(file.path for file in self.files)

    def __bool__(self) -> bool:
        return bool(self.files)

    def __len__(self) -> int:
        return len(self.files)

    def render(self) -> str:
        return "\n\n".join(file.render() for file in self.files)

    def summary(self) -> dict[str, int]:
        return {
            "files": len(self.files),
            "refused": len(self.refused),
            "skipped": len(self.skipped),
            "approx_tokens": self.approx_tokens,
        }


class ContextBuilder:
    """Selects project files for a prompt, inside the configured budget."""

    def __init__(self, context: AgentContext) -> None:
        self.context = context
        self.config = context.config

    # -- entry point -------------------------------------------------------
    def select(
        self,
        *,
        paths: Sequence[str] = (),
        query: str | None = None,
        changed: bool = False,
        limit: int | None = None,
    ) -> PromptContext:
        """Pick files: the ones asked for, the ones git changed, or the likely ones."""
        if paths:
            candidates = self._explicit(paths)
        elif changed:
            candidates = self._changed()
        else:
            candidates = self._ranked(query)
        return self._budget(candidates, limit=limit)

    # -- candidate sources -------------------------------------------------
    def _explicit(self, paths: Sequence[str]) -> list[Path]:
        """Exactly what the developer named; a directory expands to its sources."""
        files = self.context.files
        collected: list[Path] = []
        seen: set[Path] = set()
        for entry in paths:
            try:
                resolved = files.resolve(entry)
            except UnsafePathError:
                raise
            if resolved.is_dir():
                found = sorted(
                    path
                    for path in resolved.rglob("*")
                    if path.is_file() and path.suffix in SOURCE_SUFFIXES
                )
            elif resolved.is_file():
                found = [resolved]
            else:
                continue
            for path in found:
                if path not in seen:
                    seen.add(path)
                    collected.append(path)
        return collected

    def _changed(self) -> list[Path]:
        """Files git reports as modified, staged or untracked."""
        git = self.context.git
        if not git.is_repository():
            return []
        status = git.status()
        names = list(status.modified) + list(status.staged) + list(status.untracked)
        root = self.context.root
        collected: list[Path] = []
        seen: set[str] = set()
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            path = root / name
            if path.is_file() and path.suffix in SOURCE_SUFFIXES:
                collected.append(path)
        return collected

    def _ranked(self, query: str | None) -> list[Path]:
        """Rank every source file by how likely it is to matter here."""
        root = self.context.root
        keywords = _keywords(query)
        scored: list[tuple[int, str, Path]] = []
        for path in self.context.walker.context_candidates():
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:  # pragma: no cover - walker stays inside the root
                continue
            scored.append((-self._score(relative, keywords), relative, path))
        scored.sort()
        return [path for _, _, path in scored]

    def _score(self, relative: str, keywords: tuple[str, ...]) -> int:
        lowered = f"/{relative.lower()}"
        name = relative.rsplit("/", 1)[-1]
        score = 0
        for keyword in keywords:
            if keyword in name.lower():
                score += 6
            elif keyword in lowered:
                score += 3
        for marker, weight in DIRECTORY_PRIORITY:
            if marker in lowered:
                score += weight
                break
        if name in ENTRY_POINTS:
            score += 4
        if any(marker in lowered for marker in (".test.", ".spec.", "/__tests__/")):
            score -= 4
        return score

    # -- budgeting ---------------------------------------------------------
    def _budget(self, candidates: Sequence[Path], *, limit: int | None) -> PromptContext:
        root = self.context.root
        relatives = [self._relative(path, root) for path in candidates]
        allowed, refused = self.context.safety.filter_context_files(
            relatives, allow_secrets=self.config.context.allow_secret_files
        )
        allowed_set = set(allowed)

        max_files = limit or self.config.ai.max_context_files
        max_tokens = self.config.ai.max_context_tokens
        max_bytes = max(1, self.config.context.max_file_kb) * 1024
        include = tuple(self.config.context.include_globs)
        exclude = tuple(self.config.context.exclude_globs)

        files: list[ContextFile] = []
        skipped: list[str] = []
        tokens = 0
        for path, relative in zip(candidates, relatives, strict=True):
            if relative not in allowed_set:
                continue
            if include and not any(fnmatch(relative, pattern) for pattern in include):
                skipped.append(relative)
                continue
            if any(fnmatch(relative, pattern) for pattern in exclude):
                skipped.append(relative)
                continue
            if len(files) >= max_files:
                skipped.append(relative)
                continue
            text = read_text(path)
            if text is None:
                skipped.append(relative)
                continue
            truncated = len(text.encode("utf-8", "replace")) > max_bytes
            if truncated:
                text = text[:max_bytes]
            cost = estimate_tokens(text)
            if tokens + cost > max_tokens and files:
                skipped.append(relative)
                continue
            files.append(ContextFile(path=relative, content=text, truncated=truncated))
            tokens += cost

        return PromptContext(
            files=tuple(files),
            refused=tuple(refused),
            skipped=tuple(skipped),
            approx_tokens=tokens,
        )

    @staticmethod
    def _relative(path: Path, root: Path) -> str:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:  # pragma: no cover - callers resolve inside the root
            return path.as_posix()


def _keywords(query: str | None) -> tuple[str, ...]:
    if not query:
        return ()
    words = [
        "".join(character for character in word if character.isalnum())
        for word in query.lower().split()
    ]
    return tuple(word for word in words if len(word) >= 3)
