"""Turning a model's reply into typed data - or refusing it.

Models are asked for JSON and usually comply, sometimes wrapped in a markdown
fence or a sentence of politeness. This module tolerates the wrapping and
nothing else: an answer that cannot be decoded raises
:class:`~rn_agent.errors.ModelOutputError` instead of being half-guessed into a
file write.

Normalisation is strict on purpose. An unknown severity becomes ``medium``, an
unknown area becomes ``other``, an edit without content is dropped - so a
creative reply cannot widen the vocabulary the rest of the agent trusts.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from ..ai.types import Completion
from ..errors import ModelOutputError
from ..models.changes import RiskLevel
from ..models.health import Severity
from ..models.proposal import EditAction, FileEdit, Proposal, ProposalSet
from ..models.review import CONFIDENCE_LEVELS, REVIEW_AREAS, ReviewFinding
from ..utils.redaction import redact

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(?P<body>.*?)```", re.DOTALL)
_SLUG_RE = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------
def extract_json(text: str) -> dict[str, Any]:
    """The JSON object in a model reply, however it was wrapped."""
    candidates: list[str] = []
    stripped = text.strip()
    if stripped:
        candidates.append(stripped)
    candidates.extend(match.group("body").strip() for match in _FENCE_RE.finditer(text))
    balanced = _first_object(text)
    if balanced:
        candidates.append(balanced)

    for candidate in candidates:
        try:
            loaded = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(loaded, dict):
            return loaded
    raise ModelOutputError(
        "the model did not return usable JSON",
        hint=(
            "Re-run with --verbose to see the reply, or pick a stronger model "
            "(`rn-agent model --list`)."
        ),
    )


def _first_object(text: str) -> str | None:
    """Scan for the first balanced ``{...}``, ignoring braces inside strings."""
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, character in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            if depth == 0:
                start = index
            depth += 1
        elif character == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                return text[start : index + 1]
    return None


# ---------------------------------------------------------------------------
# proposals (fix / feature / test / docs / error repair)
# ---------------------------------------------------------------------------
def parse_proposals(
    text: str, *, task: str, completion: Completion | None = None
) -> ProposalSet:
    """Decode an edit reply. Raises when nothing usable came back."""
    payload = extract_json(text)
    raw_proposals = payload.get("proposals")
    if not isinstance(raw_proposals, list):
        raise ModelOutputError(
            "the model's reply has no `proposals` list",
            hint="This is usually a weaker model ignoring the output contract; try another.",
        )

    proposals: list[Proposal] = []
    used_ids: set[str] = set()
    for index, entry in enumerate(raw_proposals, start=1):
        if not isinstance(entry, Mapping):
            continue
        edits = _parse_edits(entry.get("edits"))
        if not edits:
            continue
        identifier = _unique(_slug(entry.get("id"), fallback=f"{task}-{index}"), used_ids)
        proposals.append(
            Proposal(
                id=identifier,
                title=_text(entry.get("title")) or identifier,
                summary=_text(entry.get("summary")),
                edits=edits,
                commands=_string_list(entry.get("commands")),
                risk=_risk(entry.get("risk")),
                addresses=_string_list(entry.get("addresses")),
                notes=_string_list(entry.get("notes")),
            )
        )

    notes = _string_list(payload.get("notes"))
    if not proposals:
        detail = f" The model said: {redact('; '.join(notes))}" if notes else ""
        raise ModelOutputError(
            f"the model proposed no usable file changes.{detail}",
            hint="Narrow the request (`--file`), or ask again with more context.",
        )
    return ProposalSet(
        task=task,
        proposals=proposals,
        notes=notes,
        provider=completion.provider if completion else None,
        model=completion.model if completion else None,
        input_tokens=completion.usage.input_tokens if completion else 0,
        output_tokens=completion.usage.output_tokens if completion else 0,
    )


def _parse_edits(raw: Any) -> list[FileEdit]:
    if not isinstance(raw, list):
        return []
    edits: list[FileEdit] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        path = _clean_path(entry.get("path"))
        if not path:
            continue
        action = _action(entry.get("action"))
        content = entry.get("content")
        content = content if isinstance(content, str) else None
        if action is not EditAction.DELETE and content is None:
            continue
        edits.append(
            FileEdit(
                path=path,
                action=action,
                content=None if action is EditAction.DELETE else content,
                reason=_text(entry.get("reason")),
            )
        )
    return edits


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------
def parse_review(text: str) -> tuple[list[ReviewFinding], list[str]]:
    """``(findings, notes)``. An empty finding list is a valid review."""
    payload = extract_json(text)
    raw = payload.get("findings")
    if not isinstance(raw, list):
        raise ModelOutputError(
            "the model's reply has no `findings` list",
            hint="Re-run with --verbose to see the reply, or try another model.",
        )
    findings: list[ReviewFinding] = []
    used_ids: set[str] = set()
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, Mapping):
            continue
        title = _text(entry.get("title"))
        if not title:
            continue
        identifier = _unique(_slug(entry.get("id"), fallback=f"review-{index}"), used_ids)
        findings.append(
            ReviewFinding(
                id=identifier,
                title=title,
                severity=_severity(entry.get("severity")),
                area=_area(entry.get("area")),
                file=_clean_path(entry.get("file")) or None,
                line=_line(entry.get("line")),
                detail=_text(entry.get("detail")),
                recommendation=_text(entry.get("recommendation")) or None,
                snippet=_text(entry.get("snippet")) or None,
                confidence=_confidence(entry.get("confidence")),
            )
        )
    return findings, _string_list(payload.get("notes"))


# ---------------------------------------------------------------------------
# changelog
# ---------------------------------------------------------------------------
def parse_changelog(text: str) -> tuple[list[str], list[str]]:
    payload = extract_json(text)
    entries = _string_list(payload.get("entries"))
    if not entries:
        raise ModelOutputError(
            "the model returned no changelog entries",
            hint="Write the notes by hand, or re-run `rn-agent release --no-changelog`.",
        )
    return entries, _string_list(payload.get("notes"))


# ---------------------------------------------------------------------------
# normalisation helpers
# ---------------------------------------------------------------------------
def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, Sequence):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _clean_path(value: Any) -> str:
    """Normalise a model-supplied path; reject the obviously unusable ones.

    Path *safety* is still enforced by ``FileManager.resolve``; this only keeps
    junk (absolute paths, ``..``, URLs) from reaching it.
    """
    if not isinstance(value, str):
        return ""
    path = value.strip().replace("\\", "/").lstrip()
    while path.startswith("./"):
        path = path[2:]
    if not path or path.startswith("/") or path.startswith("~"):
        return ""
    if ".." in path.split("/") or "://" in path:
        return ""
    if len(path) > 2 and path[1] == ":":  # C:\... on Windows
        return ""
    return path


def _slug(value: Any, *, fallback: str) -> str:
    text = _text(value).lower()
    slug = _SLUG_RE.sub("-", text).strip("-")
    return slug[:60] or fallback


def _unique(candidate: str, used: set[str]) -> str:
    identifier = candidate
    suffix = 2
    while identifier in used:
        identifier = f"{candidate}-{suffix}"
        suffix += 1
    used.add(identifier)
    return identifier


def _action(value: Any) -> EditAction:
    text = _text(value).lower()
    for action in EditAction:
        if text == action.value:
            return action
    return EditAction.MODIFY


def _risk(value: Any) -> RiskLevel:
    text = _text(value).lower()
    for risk in RiskLevel:
        if text == risk.value:
            return risk
    return RiskLevel.MEDIUM


def _severity(value: Any) -> Severity:
    text = _text(value).lower()
    for severity in Severity:
        if text == severity.value:
            return severity
    return Severity.MEDIUM


def _area(value: Any) -> str:
    text = _text(value).lower()
    return text if text in REVIEW_AREAS else "other"


def _confidence(value: Any) -> str:
    text = _text(value).lower()
    return text if text in CONFIDENCE_LEVELS else "medium"


def _line(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
        return number or None
    return None
