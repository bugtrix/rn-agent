"""Local, deterministic migration rules.

The upstream diff describes a *template*; a real app has usually customised the
files it touches. These rule files are the escape hatch: small, exact,
version-pinned edits ("set this Gradle property to that") that apply cleanly
whatever else the project has changed.

The directory legitimately ships empty. The agent does not invent migration
steps: a rule exists because someone wrote it down with a ``source``, and an
action this version does not implement is skipped with a warning rather than
approximated into something else.

```yaml
# migration-rules/0.80-to-0.81.yaml
from: "0.80"
to: "0.81"
source: https://reactnative.dev/docs/upgrading
android:
  - id: gradle.wrapper
    file: android/gradle/wrapper/gradle-wrapper.properties
    action: set_property
    key: distributionUrl
    value: "https\\://services.gradle.org/distributions/gradle-8.13-all.zip"
    risk: medium
```
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from ..core.logging import get_logger
from ..models.changes import RiskLevel
from ..models.migration import StepKind
from ..utils.io import read_yaml
from ..utils.semver import Version, coerce

#: Rule sections, and the migration step kind each produces.
SECTIONS: dict[str, StepKind] = {
    "android": StepKind.ANDROID,
    "ios": StepKind.IOS,
    "javascript": StepKind.JAVASCRIPT,
}


class RuleAction(StrEnum):
    SET_PROPERTY = "set_property"
    REPLACE = "replace"
    ENSURE_LINE = "ensure_line"


class RuleOutcome(StrEnum):
    APPLIED = "applied"
    ALREADY = "already"
    MISSING = "missing"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class MigrationRule:
    """One exact edit, with the file it belongs to and where it came from."""

    id: str
    kind: StepKind
    file: str
    action: RuleAction
    risk: RiskLevel = RiskLevel.MEDIUM
    key: str | None = None
    value: str | None = None
    old: str | None = None
    new: str | None = None
    line: str | None = None
    source: str | None = None
    detail: str = ""

    @property
    def title(self) -> str:
        return self.detail or f"{self.action.value} in {self.file}"


@dataclass(slots=True)
class RuleSet:
    """The rules that match one migration, and which files defined them."""

    rules: list[MigrationRule] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.rules)


def load_rules(
    directory: Path,
    *,
    from_version: str | None,
    to_version: str | None,
    logger: logging.Logger | None = None,
) -> RuleSet:
    """Every rule whose ``from``/``to`` covers this migration."""
    log = logger or get_logger("migration")
    result = RuleSet()
    if not directory.is_dir():
        return result

    target = coerce(to_version)
    source = coerce(from_version)
    for path in sorted(directory.glob("*.y*ml")):
        payload = read_yaml(path, default=None)
        if not isinstance(payload, dict):
            continue
        if not _covers(payload, source=source, target=target):
            continue
        result.files.append(payload.get("source") or str(path))
        for section, kind in SECTIONS.items():
            entries = payload.get(section)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                rule = _parse_rule(entry, kind=kind, default_source=payload.get("source"))
                if rule is None:
                    identifier = str(entry.get("id") or entry.get("action") or "?")
                    result.skipped.append(f"{path.name}:{identifier}")
                    log.warning(
                        "skipping migration rule %s in %s: unsupported action %r",
                        identifier,
                        path.name,
                        entry.get("action"),
                    )
                    continue
                result.rules.append(rule)
    return result


def apply_rule(content: str | None, rule: MigrationRule) -> tuple[str | None, RuleOutcome]:
    """Apply one rule to a file's text. ``(new_content, outcome)``."""
    if content is None:
        return None, RuleOutcome.MISSING
    if rule.action is RuleAction.SET_PROPERTY:
        return _set_property(content, rule)
    if rule.action is RuleAction.REPLACE:
        if not rule.old:
            return None, RuleOutcome.CONFLICT
        if rule.old not in content:
            already = bool(rule.new) and rule.new is not None and rule.new in content
            return None, RuleOutcome.ALREADY if already else RuleOutcome.CONFLICT
        return content.replace(rule.old, rule.new or ""), RuleOutcome.APPLIED
    # The only remaining action is ENSURE_LINE; the parser rejects anything else.
    if not rule.line:
        return None, RuleOutcome.CONFLICT
    if rule.line in content.splitlines():
        return None, RuleOutcome.ALREADY
    separator = "" if content.endswith("\n") or not content else "\n"
    return f"{content}{separator}{rule.line}\n", RuleOutcome.APPLIED


def _set_property(content: str, rule: MigrationRule) -> tuple[str | None, RuleOutcome]:
    """``key=value`` in a ``.properties`` file, in place when already present."""
    if not rule.key:
        return None, RuleOutcome.CONFLICT
    value = rule.value or ""
    pattern = re.compile(rf"^(\s*){re.escape(rule.key)}\s*=.*$", re.MULTILINE)
    match = pattern.search(content)
    if match is None:
        separator = "" if content.endswith("\n") or not content else "\n"
        return f"{content}{separator}{rule.key}={value}\n", RuleOutcome.APPLIED
    if match.group(0).strip() == f"{rule.key}={value}":
        return None, RuleOutcome.ALREADY
    return pattern.sub(f"\\g<1>{rule.key}={value}", content, count=1), RuleOutcome.APPLIED


def _covers(payload: dict[str, object], *, source: Version | None, target: Version | None) -> bool:
    """A rule file applies when the migration crosses its ``from`` -> ``to``."""
    rule_from = coerce(str(payload.get("from") or "")) if payload.get("from") else None
    rule_to = coerce(str(payload.get("to") or "")) if payload.get("to") else None
    if rule_to is None or target is None:
        return False
    if (rule_to.major, rule_to.minor) != (target.major, target.minor):
        return False
    if rule_from is None or source is None:
        return True
    return (rule_from.major, rule_from.minor) == (source.major, source.minor)


def _parse_rule(
    entry: dict[str, object], *, kind: StepKind, default_source: object
) -> MigrationRule | None:
    raw_action = str(entry.get("action") or "").strip()
    try:
        action = RuleAction(raw_action)
    except ValueError:
        return None
    file = str(entry.get("file") or "").strip()
    if not file:
        return None
    risk_text = str(entry.get("risk") or "medium").lower()
    try:
        risk = RiskLevel(risk_text)
    except ValueError:
        risk = RiskLevel.MEDIUM
    source = entry.get("source") or default_source
    return MigrationRule(
        id=str(entry.get("id") or f"{kind.value}.{action.value}"),
        kind=kind,
        file=file,
        action=action,
        risk=risk,
        key=_optional(entry.get("key")),
        value=_optional(entry.get("value")),
        old=_optional(entry.get("old")),
        new=_optional(entry.get("new")),
        line=_optional(entry.get("line")),
        source=str(source) if source else None,
        detail=str(entry.get("detail") or ""),
    )


def _optional(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
