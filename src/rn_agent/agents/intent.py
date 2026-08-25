"""What did the developer actually ask for?

Typing "fix my android build" should reach the same code as ``/fix``, and typing
"can I move to 0.86?" should reach ``/compatibility`` - without a model call to
work that out. Routing is a keyword problem, and this module solves it
deterministically, for the reason the whole project is built that way: a model
call to decide *which command to run* costs money, adds latency, and can be
wrong in ways a regex cannot.

The output is a suggestion, never an action. The terminal offers it, the
developer accepts it, and anything unmatched stays a plain question answered with
project context. Confidence is reported so the UI can offer a strong match
directly and a weak one as one option among several.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

#: A React Native version, as a developer types it: 0.86, 0.86.0, v0.86.1.
VERSION_RE = re.compile(r"\bv?(\d+\.\d+(?:\.\d+)?)\b")


class Intent(StrEnum):
    """The command a request maps to, or a plain question."""

    QUESTION = "question"
    SCAN = "scan"
    HEALTH = "health"
    REVIEW = "review"
    FIX = "fix"
    FEATURE = "feature"
    TEST = "test"
    UPGRADE = "upgrade"
    MIGRATE = "migrate"
    COMPATIBILITY = "compatibility"
    DOCS = "docs"
    RELEASE = "release"

    @property
    def command(self) -> str | None:
        return None if self is Intent.QUESTION else self.value


@dataclass(frozen=True, slots=True)
class Detection:
    """A routing suggestion, with the evidence for it."""

    intent: Intent
    #: 0.0-1.0. Above ``STRONG`` the UI may offer it as the default action.
    confidence: float
    #: The phrase that matched, so the UI can say *why* it suggests this.
    reason: str = ""
    #: Arguments extracted from the request (a target version, a file path).
    arguments: tuple[str, ...] = ()

    @property
    def actionable(self) -> bool:
        return self.intent is not Intent.QUESTION

    @property
    def strong(self) -> bool:
        return self.confidence >= STRONG


STRONG = 0.7

#: ``(intent, weight, phrases)`` - ordered, first match wins within a weight.
#: Phrases are matched as whole words against the casefolded request.
RULES: tuple[tuple[Intent, float, tuple[str, ...]], ...] = (
    (
        Intent.MIGRATE,
        0.9,
        (
            "migrate react native",
            "upgrade react native",
            "update react native",
            "move to react native",
            "rn upgrade",
            "migrate rn",
            "migration",
        ),
    ),
    (
        Intent.COMPATIBILITY,
        0.85,
        (
            "compatibility",
            "compatible",
            "can i upgrade to",
            "can i move to",
            "will my app work on",
            "before i upgrade",
            "before migrating",
        ),
    ),
    (
        Intent.UPGRADE,
        0.8,
        (
            "upgrade dependencies",
            "update dependencies",
            "upgrade packages",
            "update packages",
            "bump dependencies",
            "outdated packages",
            "outdated dependencies",
        ),
    ),
    (
        Intent.HEALTH,
        0.8,
        (
            "health",
            "diagnose",
            "what is wrong",
            "whats wrong",
            "what's wrong",
            "is my project ok",
            "check my project",
            "check my setup",
            "sanity check",
        ),
    ),
    (
        Intent.RELEASE,
        0.8,
        ("release", "version bump", "bump the version", "changelog", "ship a build"),
    ),
    (
        Intent.DOCS,
        0.75,
        ("write docs", "document this project", "documentation", "readme"),
    ),
    (
        Intent.TEST,
        0.75,
        ("write tests", "generate tests", "add tests", "test coverage", "unit tests"),
    ),
    (
        Intent.REVIEW,
        0.75,
        (
            "review",
            "code quality",
            "re-render",
            "rerender",
            "performance problem",
            "performance issue",
            "is this idiomatic",
            "best practice",
        ),
    ),
    (
        Intent.FIX,
        0.75,
        (
            "fix",
            "build fails",
            "build failed",
            "build error",
            "gradle error",
            "pod install fails",
            "crash",
            "crashes",
            "red screen",
            "does not compile",
            "failing",
            "broken",
        ),
    ),
    (
        Intent.FEATURE,
        0.7,
        (
            "implement",
            "add a screen",
            "add a feature",
            "build a screen",
            "create a component",
            "add support for",
        ),
    ),
    (
        Intent.SCAN,
        0.65,
        (
            "scan",
            "analyze my project",
            "analyse my project",
            "analyze my react native project",
            "analyse my react native project",
            "what is this project",
            "detect",
        ),
    ),
)


def detect(text: str) -> Detection:
    """Map a request to a command suggestion, or leave it a question."""
    request = " ".join(text.split()).casefold()
    if not request:
        return Detection(Intent.QUESTION, 0.0)

    for intent, weight, phrases in RULES:
        for phrase in phrases:
            if phrase in request:
                return Detection(
                    intent=intent,
                    confidence=_adjust(weight, request, intent),
                    reason=phrase,
                    arguments=_arguments(request, intent),
                )
    return Detection(Intent.QUESTION, 0.0)


def _adjust(weight: float, request: str, intent: Intent) -> float:
    """Nudge confidence with evidence a single phrase cannot carry.

    A version number next to "upgrade" makes a migration far more likely; a
    question mark makes any of them more likely to be a question about the topic
    than an instruction to act on it.
    """
    score = weight
    if intent in (Intent.MIGRATE, Intent.COMPATIBILITY) and VERSION_RE.search(request):
        score += 0.05
    if request.rstrip().endswith("?"):
        score -= 0.15
    return max(0.0, min(1.0, score))


def _arguments(request: str, intent: Intent) -> tuple[str, ...]:
    """Flags the suggestion can be run with, taken from the request itself."""
    if intent in (Intent.MIGRATE, Intent.COMPATIBILITY):
        versions = VERSION_RE.findall(request)
        if versions:
            # The highest version mentioned is the target: "0.84 -> 0.86".
            target = max(versions, key=_version_key)
            return ("--to", target) if intent is Intent.MIGRATE else ("--target", target)
    return ()


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split(".") if part.isdigit())


def describe(detection: Detection) -> str:
    """One line explaining the suggestion, for the confirmation dialog."""
    if not detection.actionable:
        return "answering as a question"
    command = f"/{detection.intent.value}"
    if detection.arguments:
        command = f"{command} {' '.join(detection.arguments)}"
    return f"{command} (matched \"{detection.reason}\")"
