"""Lookups and staged file edits for the interactive chat.

The TUI conversation is a Cursor-style agent loop: the model reads the project
or the web, then stages writes and deletes. Secrets stay out, paths cannot
escape the tree, and the actual bytes are written only after one confirm.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.context import AgentContext
from ..errors import ModelOutputError, UnsafePathError
from ..models.proposal import EditAction, FileEdit
from ..upgrade.registry import NpmRegistry
from ..utils.io import read_text
from ..utils.redaction import is_secret_path, redact
from .output import extract_json
from .web import fetch_page, search_web

TOOL_NAMES = frozenset(
    {"read", "grep", "glob", "npm", "search", "fetch", "write", "delete", "rename"}
)
MAX_ROUNDS = 16
MAX_READ_CHARS = 8_000
MAX_WRITE_CHARS = 200_000
MAX_GREP_HITS = 24
MAX_GREP_FILES = 200
MAX_GLOB_HITS = 40
MAX_NPM_RECENT = 8
_NPM_NAME = re.compile(r"^(@[A-Za-z0-9._~-]+/)?[A-Za-z0-9._~-]+$")


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One inspect step, for the UI and for the next model turn."""

    name: str
    detail: str
    result: str
    summary: str = ""
    edits: tuple[FileEdit, ...] = field(default_factory=tuple)

    @property
    def label(self) -> str:
        """Wait-line wording while the next model call runs."""
        if self.name == "read":
            return f"Reading {self.detail}"
        if self.name == "grep":
            return f"Searching {self.detail}"
        if self.name == "npm":
            return f"Looking up {self.detail}"
        if self.name == "search":
            return f"Searching the web for {self.detail}"
        if self.name == "fetch":
            return f"Reading {self.detail}"
        if self.name == "write":
            return f"Writing {self.detail}"
        if self.name == "delete":
            return f"Removing {self.detail}"
        if self.name == "rename":
            return f"Renaming {self.detail}"
        return f"Locating {self.detail}"


def parse_tool(text: str) -> dict[str, Any] | None:
    """A tool request, or ``None`` when the reply is ordinary prose.

    Only a JSON object (optionally fenced) counts. Prose that *mentions* a
    tool example must still be treated as the answer.
    """
    stripped = text.strip()
    if not stripped.startswith("{") and not stripped.startswith("```"):
        return None
    try:
        payload = extract_json(stripped)
    except ModelOutputError:
        return None
    name = str(payload.get("tool") or "").strip().casefold()
    if name not in TOOL_NAMES:
        return None
    parsed: dict[str, Any] = {}
    for key, value in payload.items():
        if value is None:
            continue
        parsed[key] = value if isinstance(value, (dict, list)) else str(value)
    return parsed


def run_tool(context: AgentContext, payload: dict[str, Any]) -> ToolCall:
    name = str(payload.get("tool") or "").casefold()
    if name == "read":
        return _read(context, _field(payload, "path"))
    if name == "grep":
        return _grep(
            context,
            _field(payload, "pattern"),
            path=_field(payload, "path"),
        )
    if name == "npm":
        return _npm(_field(payload, "package", "name", "path"))
    if name == "search":
        return _search(_field(payload, "query", "q", "pattern"))
    if name == "fetch":
        return _fetch(_field(payload, "url", "path"))
    if name == "write":
        return _write(context, payload)
    if name == "delete":
        return _delete(context, payload)
    if name == "rename":
        return _rename(context, payload)
    return _glob(context, _field(payload, "pattern", "path"))


def edits_from_reply(text: str) -> list[FileEdit]:
    """Whole-file proposals in a final JSON answer, or empty."""
    from .output import parse_proposals

    try:
        return list(parse_proposals(text, task="chat").edits)
    except ModelOutputError:
        return []


def merge_edits(existing: list[FileEdit], incoming: list[FileEdit]) -> list[FileEdit]:
    """Last write to a path wins, so a later delete can replace a write."""
    by_path = {edit.path: edit for edit in existing}
    for edit in incoming:
        by_path[edit.path] = edit
    return list(by_path.values())


def _read(context: AgentContext, path: str) -> ToolCall:
    relative = path.strip()
    if relative.startswith("./"):
        relative = relative[2:]
    if not relative:
        return ToolCall("read", "(none)", "No path given.", "missing path")
    try:
        target = context.files.resolve(relative)
    except UnsafePathError as error:
        return ToolCall("read", relative, error.message, "outside project")
    if is_secret_path(relative) or is_secret_path(target.name):
        return ToolCall("read", relative, "Refused: this looks like a secret-bearing file.", "refused")
    text = read_text(target)
    if text is None:
        return ToolCall("read", relative, f"{relative} was not found.", "missing")
    body = redact(text)
    truncated = ""
    if len(body) > MAX_READ_CHARS:
        body = body[:MAX_READ_CHARS]
        truncated = "  (truncated)"
    return ToolCall("read", relative, body, f"{len(body):,} chars{truncated}")


def _grep(context: AgentContext, pattern: str, *, path: str) -> ToolCall:
    needle = pattern.strip()
    if not needle:
        return ToolCall("grep", "(none)", "No pattern given.", "missing pattern")
    root = context.paths.project_root
    scope = path.strip()
    if scope.startswith("./"):
        scope = scope[2:]
    try:
        start = context.files.resolve(scope) if scope else root
    except UnsafePathError as error:
        return ToolCall("grep", needle, error.message, "outside project")
    hits: list[str] = []
    files_seen = 0
    lowered = needle.casefold()
    for file in _iter_files(start, allow_modules=_allows_modules(scope)):
        files_seen += 1
        if files_seen > MAX_GREP_FILES:
            hits.append("… further files skipped")
            break
        relative = _rel(file, root)
        if is_secret_path(relative):
            continue
        text = read_text(file)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if lowered not in line.casefold():
                continue
            hits.append(f"{relative}:{number}: {redact(line).strip()}")
            if len(hits) >= MAX_GREP_HITS:
                return ToolCall(
                    "grep",
                    needle,
                    "\n".join(hits),
                    f"{len(hits)} matches (capped)",
                )
    if not hits:
        return ToolCall("grep", needle, f"No matches for {needle!r}.", "0 matches")
    files = len({item.split(":", 1)[0] for item in hits if ":" in item})
    return ToolCall("grep", needle, "\n".join(hits), f"{len(hits)} matches · {files} files")


def _glob(context: AgentContext, pattern: str) -> ToolCall:
    spec = pattern.strip() or "*"
    root = context.paths.project_root
    matches: list[str] = []
    try:
        found = sorted(root.glob(spec))
    except (OSError, ValueError) as error:
        return ToolCall("glob", spec, f"Invalid pattern: {error}", "invalid")
    for path in found:
        try:
            resolved = context.files.resolve(path)
        except UnsafePathError:
            continue
        relative = _rel(resolved, root)
        if is_secret_path(relative):
            continue
        if not _allows_modules(spec) and "node_modules" in Path(relative).parts:
            continue
        matches.append(relative)
        if len(matches) >= MAX_GLOB_HITS:
            break
    if not matches:
        return ToolCall("glob", spec, f"No files matched {spec}.", "0 files")
    return ToolCall("glob", spec, "\n".join(matches), f"{len(matches)} files")


def _npm(name: str) -> ToolCall:
    package = name.strip()
    if not package or not _NPM_NAME.match(package) or len(package) > 214:
        return ToolCall("npm", package or "(none)", "Not a valid npm package name.", "invalid")
    registry = NpmRegistry()
    document = registry.packument(package)
    if document is None:
        if not registry.available:
            return ToolCall(
                "npm",
                package,
                "npm registry unreachable (registry.npmjs.org). Retry when you are online.",
                "unreachable",
            )
        return ToolCall("npm", package, f"{package} was not found on the npm registry.", "missing")
    newest = document.newest()
    recent = [entry.version for entry in reversed(document.stable()[-MAX_NPM_RECENT:])]
    lines = [
        f"package: {document.name}",
        f"latest dist-tag: {document.latest or '-'}",
        f"newest stable: {newest.version if newest else '-'}",
    ]
    if recent:
        lines.append("recent stable: " + ", ".join(recent))
    lines.append("source: https://registry.npmjs.org/" + package)
    return ToolCall("npm", package, "\n".join(lines), f"latest {document.latest or '-'}")


def _search(query: str) -> ToolCall:
    body, summary = search_web(query)
    return ToolCall("search", query.strip() or "(none)", body, summary)


def _fetch(url: str) -> ToolCall:
    body, summary = fetch_page(url)
    return ToolCall("fetch", url.strip() or "(none)", body, summary)


def _write(context: AgentContext, payload: dict[str, Any]) -> ToolCall:
    rows = payload.get("files")
    if isinstance(rows, list) and rows:
        edits: list[FileEdit] = []
        notes: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            edit, error = _one_write(context, row)
            if error:
                notes.append(error)
                continue
            if edit is not None:
                edits.append(edit)
                notes.append(f"queued {edit.action.value} {edit.path}")
        if not edits:
            return ToolCall("write", f"{len(rows)} files", "\n".join(notes) or "No files to write.", "refused")
        return ToolCall(
            "write",
            f"{len(edits)} file(s)",
            "\n".join(notes),
            f"{len(edits)} queued",
            edits=tuple(edits),
        )
    edit, error = _one_write(context, payload)
    if error or edit is None:
        return ToolCall("write", _field(payload, "path") or "(none)", error or "No path given.", "refused")
    return ToolCall(
        "write",
        edit.path,
        f"Queued {edit.action.value} of {edit.path} ({edit.lines} lines). "
        "If this is a rename, emit rename or delete the old path next — do not summarize yet.",
        f"{edit.lines} lines queued",
        edits=(edit,),
    )


def _delete(context: AgentContext, payload: dict[str, Any]) -> ToolCall:
    relative, error = _chat_path(context, _field(payload, "path"))
    if error or relative is None:
        return ToolCall("delete", _field(payload, "path") or "(none)", error or "No path given.", "refused")
    edit = FileEdit(path=relative, action=EditAction.DELETE, reason=_field(payload, "reason"))
    return ToolCall(
        "delete",
        relative,
        f"Queued delete of {relative}. Continue, or finish with a short summary.",
        "queued",
        edits=(edit,),
    )


def _rename(context: AgentContext, payload: dict[str, Any]) -> ToolCall:
    """Move a file to a new path. Optional ``content`` is the file after the move."""
    source, src_error = _chat_path(context, _field(payload, "from", "path", "source"))
    dest, dst_error = _chat_path(context, _field(payload, "to", "dest", "destination"))
    if src_error or source is None:
        return ToolCall("rename", _field(payload, "from", "path") or "(none)", src_error or "No source path.", "refused")
    if dst_error or dest is None:
        return ToolCall("rename", source, dst_error or "No destination path.", "refused")
    if source == dest:
        return ToolCall("rename", source, "Source and destination are the same path.", "refused")
    content = payload.get("content")
    if content is None:
        content = payload.get("text")
    if not isinstance(content, str) or not content:
        existing = read_text(context.files.resolve(source))
        if existing is None:
            return ToolCall("rename", f"{source} → {dest}", f"{source} was not found.", "missing")
        content = existing
    if len(content) > MAX_WRITE_CHARS:
        return ToolCall(
            "rename",
            f"{source} → {dest}",
            f"{dest}: file is larger than {MAX_WRITE_CHARS:,} characters.",
            "refused",
        )
    create = FileEdit(
        path=dest,
        action=EditAction.MODIFY if context.files.exists(dest) else EditAction.CREATE,
        content=content,
        reason=_field(payload, "reason") or f"rename {source}",
    )
    delete = FileEdit(path=source, action=EditAction.DELETE, reason=_field(payload, "reason") or f"rename to {dest}")
    return ToolCall(
        "rename",
        f"{source} → {dest}",
        f"Queued rename of {source} to {dest}. Update imports next, then summarize.",
        "queued",
        edits=(create, delete),
    )


def _one_write(context: AgentContext, payload: Mapping[str, Any]) -> tuple[FileEdit | None, str | None]:
    relative, error = _chat_path(context, _field(payload, "path"))
    if error or relative is None:
        return None, error or "No path given."
    content = payload.get("content")
    if content is None:
        content = payload.get("text")
    if not isinstance(content, str) or not content:
        return None, f"{relative}: write needs the entire file in `content`."
    if len(content) > MAX_WRITE_CHARS:
        return None, f"{relative}: file is larger than {MAX_WRITE_CHARS:,} characters."
    exists = context.files.exists(relative)
    action = EditAction.MODIFY if exists else EditAction.CREATE
    return (
        FileEdit(path=relative, action=action, content=content, reason=_field(payload, "reason")),
        None,
    )


def _chat_path(context: AgentContext, path: str) -> tuple[str | None, str | None]:
    relative = path.strip().replace("\\", "/")
    if relative.startswith("./"):
        relative = relative[2:]
    if not relative or relative.startswith("/") or relative.startswith("~") or ".." in relative.split("/"):
        return None, "Not a project-relative path."
    if is_secret_path(relative) or is_secret_path(Path(relative).name):
        return None, "Refused: this looks like a secret-bearing file."
    try:
        context.files.resolve(relative)
    except UnsafePathError as error:
        return None, error.message
    return relative, None


def _field(payload: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is None or isinstance(value, (dict, list)):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _iter_files(start: Path, *, allow_modules: bool) -> Iterator[Path]:
    import os

    if start.is_file():
        yield start
        return
    if not start.is_dir():
        return
    skip = {"node_modules", ".git", "Pods", "build", "dist", "DerivedData"}
    if allow_modules:
        skip = skip - {"node_modules"}
    for current, dirnames, filenames in os.walk(start, topdown=True, followlinks=False):
        dirnames[:] = [name for name in dirnames if name not in skip and not name.startswith(".")]
        for name in filenames:
            yield Path(current) / name


def _allows_modules(spec: str) -> bool:
    folded = spec.replace("\\", "/").casefold()
    return "node_modules" in folded


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name
