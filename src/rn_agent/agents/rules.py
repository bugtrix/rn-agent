"""The project's own rules, as the agent must obey them.

``rn-agent scan`` seeds ``.rn-agent/rules.yaml`` from the architecture it
detected; the developer edits it and it is never overwritten. This module turns
that file into two things:

* prompt text, so the model is told the constraints up front;
* a checker, so a model that ignores them is caught *before* anything is written.

The second part is the one that matters. A prompt is a request; ``violations()``
is the enforcement.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any

from ..core.paths import AgentPaths
from ..models.proposal import FileEdit
from ..utils.io import read_yaml

#: Paths that are native build configuration, whatever their extension.
NATIVE_PREFIXES: tuple[str, ...] = ("android/", "ios/")
NATIVE_SUFFIXES: tuple[str, ...] = (
    ".gradle",
    ".gradle.kts",
    ".pbxproj",
    ".plist",
    ".entitlements",
    ".xcconfig",
    ".podspec",
    ".kt",
    ".java",
    ".swift",
    ".m",
    ".mm",
    ".h",
)
NATIVE_NAMES: tuple[str, ...] = ("Podfile", "Podfile.lock", "gradle-wrapper.properties")

LOCKFILE_NAMES: tuple[str, ...] = (
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lockb",
    "bun.lock",
)


def is_native_path(path: str) -> bool:
    """True for native build/config files, whose risk is never low."""
    posix = path.replace("\\", "/")
    name = posix.rsplit("/", 1)[-1]
    return (
        posix.startswith(NATIVE_PREFIXES)
        or posix.endswith(NATIVE_SUFFIXES)
        or name in NATIVE_NAMES
    )


def _project_relative_native(path: str) -> str:
    """Strip to ``android/...`` / ``ios/...`` so absolute ``--file`` paths match."""
    posix = path.replace("\\", "/").lstrip("./")
    for marker in ("android/", "ios/"):
        index = posix.find(marker)
        if index >= 0:
            return posix[index:]
    return posix


def native_path_allowed(path: str, patterns: Sequence[str]) -> bool:
    """Whether ``path`` matches a developer-approved native glob or exact path."""
    posix = _project_relative_native(path)
    name = posix.rsplit("/", 1)[-1]
    for pattern in patterns:
        pat = _project_relative_native(str(pattern))
        if not pat:
            continue
        if posix == pat or fnmatch(posix, pat) or fnmatch(name, pat):
            return True
    return False


def is_lockfile(path: str) -> bool:
    return path.replace("\\", "/").rsplit("/", 1)[-1] in LOCKFILE_NAMES


@dataclass(frozen=True, slots=True)
class RuleViolation:
    """One rule an edit would break."""

    rule: str
    path: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"{self.path}: {self.detail}"


@dataclass(frozen=True, slots=True)
class ProjectRules:
    """``.rn-agent/rules.yaml``, parsed. An absent file means the defaults."""

    follow_existing_architecture: bool = True
    allowed_state_management: tuple[str, ...] = ()
    allowed_navigation: tuple[str, ...] = ()
    api_layer: tuple[str, ...] = ()
    styling: tuple[str, ...] = ()
    testing: tuple[str, ...] = ()
    language: str = "typescript"
    forbid_new_dependencies: bool = True
    forbid_native_edits_without_confirmation: bool = True
    #: Native paths the developer has already approved in rules.yaml.
    #: Globs are allowed (``android/**/AndroidManifest.xml``).
    allow_native_paths: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    #: Keys the developer added that this version does not interpret. They are
    #: still shown to the model - an unknown rule is a rule, not noise.
    extra: Mapping[str, Any] = field(default_factory=dict)

    KNOWN_KEYS = (
        "follow_existing_architecture",
        "allowed_state_management",
        "allowed_navigation",
        "api_layer",
        "styling",
        "testing",
        "language",
        "forbid_new_dependencies",
        "forbid_native_edits_without_confirmation",
        "allow_native_paths",
    )

    # -- loading -----------------------------------------------------------
    @classmethod
    def load(cls, paths: AgentPaths) -> ProjectRules:
        payload = read_yaml(paths.rules_file, default={}) or {}
        if not isinstance(payload, dict):
            return cls()
        raw = payload.get("rules")
        rules: dict[str, Any] = raw if isinstance(raw, dict) else {}
        return cls(
            follow_existing_architecture=bool(rules.get("follow_existing_architecture", True)),
            allowed_state_management=_as_tuple(rules.get("allowed_state_management")),
            allowed_navigation=_as_tuple(rules.get("allowed_navigation")),
            api_layer=_as_tuple(rules.get("api_layer")),
            styling=_as_tuple(rules.get("styling")),
            testing=_as_tuple(rules.get("testing")),
            language=str(rules.get("language") or "typescript"),
            forbid_new_dependencies=bool(rules.get("forbid_new_dependencies", True)),
            forbid_native_edits_without_confirmation=bool(
                rules.get("forbid_native_edits_without_confirmation", True)
            ),
            allow_native_paths=_as_tuple(rules.get("allow_native_paths")),
            notes=_as_tuple(payload.get("notes")),
            extra={
                key: value for key, value in rules.items() if key not in cls.KNOWN_KEYS
            },
        )

    # -- prompt ------------------------------------------------------------
    def as_prompt_lines(self) -> list[str]:
        """The constraints, as the model is told them."""
        lines = [f"- Language: {self.language}."]
        if self.follow_existing_architecture:
            lines.append(
                "- Follow the existing architecture exactly; do not introduce a different "
                "pattern or library for something the project already solves."
            )
        for label, values in (
            ("state management", self.allowed_state_management),
            ("navigation", self.allowed_navigation),
            ("API layer", self.api_layer),
            ("styling", self.styling),
            ("testing", self.testing),
        ):
            if values:
                lines.append(f"- Allowed {label}: {', '.join(values)}. Use nothing else.")
        if self.forbid_new_dependencies:
            lines.append(
                "- Do not add, remove or change any dependency in package.json. If a change "
                "truly needs a new package, say so in `notes` and propose no edit for it."
            )
        if self.forbid_native_edits_without_confirmation:
            lines.append(
                "- Do not edit native files (android/, ios/, *.gradle, *.pbxproj, Podfile) "
                "unless the request is explicitly about them, or the path is listed in "
                "allow_native_paths."
            )
            if self.allow_native_paths:
                lines.append(
                    "- Native paths already approved: "
                    + ", ".join(self.allow_native_paths)
                    + "."
                )
        lines.extend(
            f"- {key.replace('_', ' ')}: {json.dumps(value, default=str)}"
            for key, value in self.extra.items()
        )
        lines.extend(f"- {note}" for note in self.notes)
        return lines

    # -- enforcement -------------------------------------------------------
    def violations(
        self,
        edits: Iterable[FileEdit],
        *,
        allow_dependencies: bool = False,
        allow_native: bool = False,
        allowed_native_paths: Sequence[str] = (),
    ) -> list[RuleViolation]:
        """Which edits break a rule. An empty list means "all clear"."""
        permitted_native = tuple(self.allow_native_paths) + tuple(allowed_native_paths)
        found: list[RuleViolation] = []
        for edit in edits:
            path = edit.path.replace("\\", "/")
            name = path.rsplit("/", 1)[-1]
            if is_lockfile(path):
                found.append(
                    RuleViolation(
                        "lockfile",
                        path,
                        "lockfiles are produced by your package manager, never edited by hand",
                    )
                )
            elif (
                self.forbid_native_edits_without_confirmation
                and not allow_native
                and is_native_path(path)
                and not native_path_allowed(path, permitted_native)
            ):
                found.append(
                    RuleViolation(
                        "forbid_native_edits_without_confirmation",
                        path,
                        "native file; re-run with --allow-native, or add it to "
                        "rules.allow_native_paths",
                    )
                )
            elif self.forbid_new_dependencies and not allow_dependencies and name == "package.json":
                found.append(
                    RuleViolation(
                        "forbid_new_dependencies",
                        path,
                        "package.json edits are refused; use `rn-agent upgrade` or --allow-deps",
                    )
                )
        return found


def dependency_delta(before: str | None, after: str | None) -> dict[str, list[str]]:
    """Which dependencies a ``package.json`` rewrite would add, drop or move.

    Lets a refusal name the package instead of saying "package.json touched".
    """
    sections = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")

    def collect(text: str | None) -> dict[str, str]:
        if not text:
            return {}
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return {}
        if not isinstance(payload, dict):
            return {}
        merged: dict[str, str] = {}
        for section in sections:
            block = payload.get(section)
            if isinstance(block, dict):
                merged.update({str(key): str(value) for key, value in block.items()})
        return merged

    old = collect(before)
    new = collect(after)
    return {
        "added": sorted(set(new) - set(old)),
        "removed": sorted(set(old) - set(new)),
        "changed": sorted(name for name in set(old) & set(new) if old[name] != new[name]),
    }


def _as_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value if item is not None)
    return ()
