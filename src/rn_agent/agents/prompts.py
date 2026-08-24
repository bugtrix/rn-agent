"""What the agent actually says to a model.

Two rules shape every prompt here:

* **Facts, then constraints, then the request.** The model is handed the scanned
  project (versions, package manager, inferred architecture) and the project's
  own ``rules.yaml`` before it is asked for anything, because an answer that
  ignores the existing architecture is worse than no answer.
* **One machine-readable shape per task.** Every prompt ends with the exact JSON
  contract ``agents/output.py`` parses. Prose answers are rejected there rather
  than guessed at here.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..ai.types import Message
from ..models.project import ProjectContext
from ..models.validation import ValidationReport
from .context_builder import PromptContext
from .rules import ProjectRules

SYSTEM_PREAMBLE = """\
You are rn-agent, a senior React Native engineer working inside a developer's \
existing project. You are careful, concrete and honest.

Hard requirements:
- Work with the project as it is. Match its language, style, folder layout and libraries.
- Never invent APIs, files, versions or facts. If you need something you cannot see, say so.
- Prefer the smallest correct change over a rewrite.
- Reply with JSON only: no prose before or after, no markdown fence around the JSON.\
"""

EDIT_CONTRACT = """\
Reply with this exact JSON shape:

{
  "proposals": [
    {
      "id": "short-kebab-id",
      "title": "one line",
      "summary": "what this changes and why",
      "addresses": ["finding id you are fixing, if any"],
      "risk": "low|medium|high",
      "edits": [
        {
          "path": "src/relative/path.tsx",
          "action": "create|modify|delete",
          "content": "THE COMPLETE FILE CONTENT AFTER YOUR CHANGE",
          "reason": "why this file changes"
        }
      ],
      "commands": ["command the developer should run afterwards, if any"]
    }
  ],
  "notes": ["anything you could not do, or that needs a human decision"]
}

Rules for edits:
- `content` is the ENTIRE file after the change, not a diff, not a fragment,
  with no elisions such as "// ... rest unchanged".
- Use `action: "delete"` (and omit `content`) only when a file must go.
- Paths are relative to the project root and use forward slashes.
- If you cannot make a change safely, return no proposal for it and explain in `notes`.\
"""

REVIEW_CONTRACT = """\
Reply with this exact JSON shape:

{
  "findings": [
    {
      "id": "short-kebab-id",
      "title": "one line",
      "severity": "critical|high|medium|low|info",
      "area": "architecture|components|hooks|state|navigation|performance|types|native|testing|security|accessibility|other",
      "file": "src/relative/path.tsx",
      "line": 42,
      "detail": "what is wrong and why it matters in this project",
      "recommendation": "the concrete change to make",
      "snippet": "the exact code you are referring to",
      "confidence": "low|medium|high"
    }
  ],
  "notes": ["observations that are not findings"]
}

Severity means impact on this app: `critical` breaks a build or ships a bug to
users, `low` is a papercut. Report only what the code you were given shows -
no speculation about files you cannot see, and no style nitpicking.\
"""

CHANGELOG_CONTRACT = """\
Reply with this exact JSON shape:

{
  "entries": ["user-facing change, imperative mood, one line each"],
  "notes": ["anything ambiguous in the commit history"]
}

Group nothing, number nothing, and do not invent changes that the commits do not
show. Skip merge commits, version bumps and pure chores.\
"""


def project_brief(project: ProjectContext, rules: ProjectRules) -> str:
    """The scanned facts a model needs before it may propose anything."""
    native = project.react_native
    architecture = project.architecture
    lines = [
        "PROJECT FACTS (from rn-agent scan - these are measured, not guessed):",
        f"- React Native {native.version or 'unknown'}"
        + (f" (from {native.version_source})" if native.version_source else ""),
        f"- React {native.react_version or 'unknown'}, "
        f"TypeScript {'yes' if native.typescript else 'no'}"
        + (f" {native.typescript_version}" if native.typescript_version else ""),
        f"- Package manager: {project.package_manager.name}",
        f"- New architecture: {_tristate(native.new_architecture)}, "
        f"Hermes: {_tristate(native.hermes_enabled)}",
        f"- Platforms present: android={project.android.present}, ios={project.ios.present}",
        f"- Source root: {architecture.source_root or 'project root'}",
    ]
    for label, values in (
        ("State management", architecture.state_management),
        ("Navigation", architecture.navigation),
        ("API layer", architecture.api_layer),
        ("Data fetching", architecture.data_fetching),
        ("Styling", architecture.styling),
        ("Forms", architecture.forms),
        ("Testing", architecture.testing),
    ):
        if values:
            lines.append(f"- {label}: {', '.join(values)}")
    if architecture.conventions:
        conventions = ", ".join(f"{key}={value}" for key, value in architecture.conventions.items())
        lines.append(f"- Conventions: {conventions}")
    lines.append("")
    lines.append("PROJECT RULES (from .rn-agent/rules.yaml - the developer's own constraints):")
    lines.extend(rules.as_prompt_lines())
    return "\n".join(lines)


def _system(project: ProjectContext, rules: ProjectRules, contract: str) -> Message:
    return Message.system(
        f"{SYSTEM_PREAMBLE}\n\n{project_brief(project, rules)}\n\n{contract}"
    )


def _files_block(context: PromptContext) -> str:
    if not context:
        return "No project files were included in this request."
    return f"PROJECT FILES ({len(context)}):\n\n{context.render()}"


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------
def review_messages(
    *,
    project: ProjectContext,
    rules: ProjectRules,
    context: PromptContext,
    areas: Sequence[str] = (),
    instruction: str | None = None,
) -> list[Message]:
    focus = (
        f"Focus only on these areas: {', '.join(areas)}."
        if areas
        else "Cover architecture, components, hooks, state, performance and types."
    )
    extra = f"\n\nThe developer adds: {instruction}" if instruction else ""
    return [
        _system(project, rules, REVIEW_CONTRACT),
        Message.user(
            f"Review this React Native code. {focus}\n\n{_files_block(context)}{extra}"
        ),
    ]


# ---------------------------------------------------------------------------
# fix
# ---------------------------------------------------------------------------
def fix_messages(
    *,
    project: ProjectContext,
    rules: ProjectRules,
    context: PromptContext,
    issues: Sequence[str] = (),
    instruction: str | None = None,
) -> list[Message]:
    if issues:
        request = "Fix exactly these reported problems:\n" + "\n".join(
            f"- {issue}" for issue in issues
        )
    elif instruction:
        request = f"Fix this: {instruction}"
    else:  # pragma: no cover - the command requires one of the two
        request = "Fix the defects visible in the files below."
    return [
        _system(project, rules, EDIT_CONTRACT),
        Message.user(
            f"{request}\n\n"
            "Change only what the fix requires. Keep every unrelated line byte-identical.\n\n"
            f"{_files_block(context)}"
        ),
    ]


# ---------------------------------------------------------------------------
# feature
# ---------------------------------------------------------------------------
def feature_messages(
    *,
    project: ProjectContext,
    rules: ProjectRules,
    context: PromptContext,
    description: str,
) -> list[Message]:
    return [
        _system(project, rules, EDIT_CONTRACT),
        Message.user(
            f"Implement this feature: {description}\n\n"
            "Follow the project's existing patterns exactly - the same folder layout, "
            "naming, state management, navigation and styling as the files below. "
            "Wire the feature in (registration, exports, navigation entries) rather than "
            "leaving orphaned files.\n\n"
            f"{_files_block(context)}"
        ),
    ]


# ---------------------------------------------------------------------------
# test
# ---------------------------------------------------------------------------
def test_messages(
    *,
    project: ProjectContext,
    rules: ProjectRules,
    context: PromptContext,
    framework: str,
    conventions: Sequence[str] = (),
) -> list[Message]:
    convention_text = (
        "Existing test files to imitate: " + ", ".join(conventions) if conventions else
        "There are no existing tests; place them next to the code under test."
    )
    return [
        _system(project, rules, EDIT_CONTRACT),
        Message.user(
            f"Write {framework} tests for the code below.\n\n"
            f"{convention_text}\n"
            "Test real behaviour and edge cases the code actually has: rendering with the "
            "props it declares, the branches it contains, error paths, and hook state "
            "transitions. Do not test implementation details, do not assert on snapshots, "
            "and do not mock the module under test.\n\n"
            f"{_files_block(context)}"
        ),
    ]


# ---------------------------------------------------------------------------
# docs
# ---------------------------------------------------------------------------
def docs_messages(
    *,
    project: ProjectContext,
    rules: ProjectRules,
    context: PromptContext,
    sections: Sequence[str],
    target: str,
    existing: str | None = None,
) -> list[Message]:
    current = (
        f"The current {target} is:\n\n```markdown\n{existing}\n```\n\n"
        "Update it in place: keep anything still accurate, correct what drifted."
        if existing
        else f"{target} does not exist yet; write it from scratch."
    )
    return [
        _system(project, rules, EDIT_CONTRACT),
        Message.user(
            f"Write developer documentation for this project into `{target}`.\n"
            f"Sections to cover: {', '.join(sections)}.\n\n"
            f"{current}\n\n"
            "Describe only what the facts and files below show - no aspirational "
            "architecture, no invented scripts, no badges.\n\n"
            f"{_files_block(context)}"
        ),
    ]


# ---------------------------------------------------------------------------
# build-error repair (migration and post-change validation)
# ---------------------------------------------------------------------------
def error_fix_messages(
    *,
    project: ProjectContext,
    rules: ProjectRules,
    context: PromptContext,
    report: ValidationReport,
    what_changed: str,
) -> list[Message]:
    return [
        _system(project, rules, EDIT_CONTRACT),
        Message.user(
            f"{what_changed}\n\n"
            "The project now fails to build. Fix the cause, not the symptom: do not "
            "silence an error, loosen a type, or delete a failing test.\n\n"
            f"FAILURES:\n```\n{report.failure_text()}\n```\n\n"
            f"{_files_block(context)}"
        ),
    ]


# ---------------------------------------------------------------------------
# release notes
# ---------------------------------------------------------------------------
def changelog_messages(
    *,
    project: ProjectContext,
    rules: ProjectRules,
    version: str,
    commits: Sequence[str],
) -> list[Message]:
    listing = "\n".join(f"- {commit}" for commit in commits)
    return [
        _system(project, rules, CHANGELOG_CONTRACT),
        Message.user(
            f"Write the changelog for version {version} of this app, from these commits:\n\n"
            f"{listing}"
        ),
    ]


def _tristate(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return "on" if value else "off"
