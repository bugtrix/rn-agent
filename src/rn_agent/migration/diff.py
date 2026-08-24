"""Unified diffs, applied strictly or not at all.

This is the most dangerous code in the agent, so it is the most conservative. A
hunk applies only when the lines it claims to remove and the context around them
match the file as it is now. The stated line number is a hint: the file has
almost certainly drifted, so the matcher searches outward from it and requires
**exactly one** match. Zero matches, or two, is a conflict.

There is no fuzzy mode, no "ignore whitespace", no partial write. Half-applying
a hunk to ``project.pbxproj`` produces a project that neither opens nor reverts
cleanly, and no convenience is worth that. A conflict is reported for the
developer to resolve by hand, with the hunk printed.

The upstream React Native diffs name their app ``RnDiffApp``. Mapping that to a
real project is a rename, not a guess: it is applied when the project's own name
is known, and a hunk whose meaning depends on an unknown mapping is refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

PLACEHOLDER_NAMES: tuple[str, ...] = ("RnDiffApp", "rndiffapp", "RNDIFFAPP")

_HUNK_RE = re.compile(r"^@@ -(?P<old>\d+)(?:,(?P<old_count>\d+))? \+(?P<new>\d+)(?:,(?P<new_count>\d+))? @@")
_SEARCH_WINDOW = 200


class HunkResult(StrEnum):
    APPLIED = "applied"
    ALREADY = "already"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class Hunk:
    """One ``@@`` block: the lines to expect, and the lines to leave behind."""

    old_start: int
    lines: tuple[str, ...]
    #: The ``@@ -a,b +c,d @@`` line. Kept so a stored hunk stays a valid patch
    #: fragment: a conflict is reported to the developer as something they can
    #: read, and the applier can re-parse what the planner recorded.
    header: str = ""

    @property
    def before(self) -> list[str]:
        """Context plus removed lines - what must be in the file now."""
        return [line[1:] for line in self.lines if line[:1] in (" ", "-")]

    @property
    def after(self) -> list[str]:
        """Context plus added lines - what should be there afterwards."""
        return [line[1:] for line in self.lines if line[:1] in (" ", "+")]

    @property
    def text(self) -> str:
        body = "\n".join(self.lines)
        return f"{self.header}\n{body}" if self.header else body


@dataclass(slots=True)
class DiffFile:
    """Every hunk for one path, plus what kind of change it is."""

    old_path: str
    new_path: str
    hunks: list[Hunk] = field(default_factory=list)
    #: A file the diff creates or deletes outright.
    created: bool = False
    deleted: bool = False
    binary: bool = False

    @property
    def path(self) -> str:
        return self.new_path if self.new_path != "/dev/null" else self.old_path

    @property
    def text(self) -> str:
        return "\n\n".join(hunk.text for hunk in self.hunks)


def parse_diff(text: str) -> list[DiffFile]:
    """Split a unified diff into per-file hunk lists."""
    files: list[DiffFile] = []
    current: DiffFile | None = None
    hunk_lines: list[str] | None = None
    hunk_start = 0
    hunk_header = ""

    def close_hunk() -> None:
        nonlocal hunk_lines
        if current is not None and hunk_lines:
            current.hunks.append(
                Hunk(old_start=hunk_start, lines=tuple(hunk_lines), header=hunk_header)
            )
        hunk_lines = None

    for raw in text.splitlines():
        if raw.startswith("diff --git"):
            close_hunk()
            parts = raw.split()
            old = _strip_prefix(parts[2]) if len(parts) > 3 else ""
            new = _strip_prefix(parts[3]) if len(parts) > 3 else ""
            current = DiffFile(old_path=old, new_path=new)
            files.append(current)
            continue
        if current is None:
            continue
        if raw.startswith("--- "):
            close_hunk()
            path = raw[4:].strip()
            current.old_path = _strip_prefix(path)
            current.created = path == "/dev/null"
            continue
        if raw.startswith("+++ "):
            close_hunk()
            path = raw[4:].strip()
            current.new_path = _strip_prefix(path)
            current.deleted = path == "/dev/null"
            continue
        if raw.startswith("Binary files") or raw.startswith("GIT binary patch"):
            current.binary = True
            continue
        match = _HUNK_RE.match(raw)
        if match:
            close_hunk()
            hunk_start = int(match.group("old"))
            hunk_header = raw
            hunk_lines = []
            continue
        if hunk_lines is not None:
            if raw.startswith(("+", "-", " ")):
                hunk_lines.append(raw)
            elif raw.startswith("\\"):  # "\ No newline at end of file"
                continue
            else:
                close_hunk()
    close_hunk()
    return [entry for entry in files if entry.hunks or entry.binary]


def rename_placeholder(text: str, *, project_name: str | None) -> tuple[str, bool]:
    """Replace the upstream template's app name. ``(text, decided)``.

    ``decided`` is False when the text needs the mapping and the project name is
    unknown - the caller then refuses the step instead of writing ``RnDiffApp``
    into a real project.
    """
    needs = any(placeholder in text for placeholder in PLACEHOLDER_NAMES)
    if not needs:
        return text, True
    if not project_name:
        return text, False
    replaced = text
    for placeholder in PLACEHOLDER_NAMES:
        if placeholder.islower():
            replaced = replaced.replace(placeholder, project_name.lower())
        elif placeholder.isupper():
            replaced = replaced.replace(placeholder, project_name.upper())
        else:
            replaced = replaced.replace(placeholder, project_name)
    return replaced, True


def apply_hunk(content: str, hunk: Hunk) -> tuple[str | None, HunkResult]:
    """Apply one hunk, or explain why it cannot be applied.

    Returns ``(new_content, result)``. ``new_content`` is ``None`` for anything
    but :attr:`HunkResult.APPLIED`, so a caller cannot accidentally write a
    partially patched file.

    "Already applied" is checked *before* "applies cleanly", because a hunk that
    only adds lines still matches its own context afterwards - applying it twice
    would duplicate the addition.
    """
    lines = content.splitlines()
    before = hunk.before
    after = hunk.after
    if not before:
        return None, HunkResult.CONFLICT

    removes = any(line.startswith("-") for line in hunk.lines)
    already = (
        _find(lines, after, around=hunk.old_start - 1) if after and after != before else []
    )
    if len(already) == 1 and not removes:
        return None, HunkResult.ALREADY

    matches = _find(lines, before, around=hunk.old_start - 1)
    if len(matches) != 1:
        # The result is present and the source is not: someone already did this.
        if len(already) == 1:
            return None, HunkResult.ALREADY
        return None, HunkResult.CONFLICT

    index = matches[0]
    patched = [*lines[:index], *after, *lines[index + len(before) :]]
    trailing = "\n" if content.endswith("\n") or not content else ""
    return "\n".join(patched) + trailing, HunkResult.APPLIED


def apply_hunks(content: str, hunks: list[Hunk]) -> tuple[str | None, HunkResult, int]:
    """Apply every hunk in order. ``(content, result, applied_count)``.

    One conflicting hunk fails the whole file: the caller gets ``None`` and the
    file on disk is never touched.
    """
    current = content
    applied = 0
    for hunk in hunks:
        patched, result = apply_hunk(current, hunk)
        if result is HunkResult.CONFLICT:
            return None, HunkResult.CONFLICT, applied
        if result is HunkResult.ALREADY:
            continue
        current = patched or current
        applied += 1
    if applied == 0:
        return None, HunkResult.ALREADY, 0
    return current, HunkResult.APPLIED, applied


def _find(lines: list[str], needle: list[str], *, around: int) -> list[int]:
    """Indexes where ``needle`` occurs, searched outward from ``around``.

    Stops at two matches: ambiguity is a conflict, and there is no point
    scanning the rest of a 20 000-line ``pbxproj`` to count them.
    """
    span = len(needle)
    if span == 0 or span > len(lines):
        return []
    found: list[int] = []
    limit = len(lines) - span
    start = max(0, min(around, limit))
    offsets = [0]
    for step in range(1, _SEARCH_WINDOW + 1):
        offsets.extend((step, -step))
    seen: set[int] = set()
    for offset in offsets:
        index = start + offset
        if index < 0 or index > limit or index in seen:
            continue
        seen.add(index)
        if lines[index : index + span] == needle:
            found.append(index)
            if len(found) > 1:
                return found
    return found


def _strip_prefix(path: str) -> str:
    """``a/android/build.gradle`` -> ``android/build.gradle``."""
    cleaned = path.strip().split("\t")[0]
    for prefix in ("a/", "b/"):
        if cleaned.startswith(prefix):
            return cleaned[len(prefix) :]
    return cleaned
