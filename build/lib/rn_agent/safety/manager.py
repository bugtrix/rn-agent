"""Safety manager.

Requirement §33: before any significant operation the developer must see what
will change, which files are affected, the risk, and whether a rollback is
available - then confirm.

The policy lives here, the rendering lives in ``cli/ui.py``, so the same rules
apply to every command and can be unit-tested without a terminal.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from ..core.logging import get_logger
from ..errors import ConfirmationDeclined
from ..models.changes import RiskLevel
from ..models.config import SafetyConfig
from ..utils.redaction import is_secret_path

Confirmer = Callable[[str, bool], bool]


@dataclass(frozen=True, slots=True)
class SafetyDecision:
    """Why an operation was allowed or blocked."""

    allowed: bool
    reason: str
    requires_confirmation: bool = False
    risk: RiskLevel = RiskLevel.LOW

    @property
    def blocked(self) -> bool:
        return not self.allowed


@dataclass(slots=True)
class SafetyManager:
    """Applies the project's safety policy to a proposed operation."""

    config: SafetyConfig
    dry_run: bool = False
    assume_yes: bool = False
    confirmer: Confirmer | None = None
    logger: logging.Logger = field(default_factory=lambda: get_logger("safety"))

    # -- policy ------------------------------------------------------------
    def evaluate(
        self,
        *,
        risk: RiskLevel,
        file_count: int,
        rollback_available: bool,
    ) -> SafetyDecision:
        """Decide whether an operation may proceed and whether to ask first."""
        if self.dry_run:
            return SafetyDecision(True, "dry-run: nothing will be written", False, risk)

        if file_count > self.config.max_files_per_operation:
            return SafetyDecision(
                False,
                (
                    f"operation touches {file_count} files, above the configured limit of "
                    f"{self.config.max_files_per_operation}"
                ),
                False,
                risk,
            )

        if risk is RiskLevel.LOW and self.config.auto_fix_low_risk:
            return SafetyDecision(True, "low-risk change, auto-apply enabled", False, risk)

        if not self.config.require_confirmation:
            return SafetyDecision(True, "confirmation disabled in config", False, risk)

        if self.assume_yes:
            return SafetyDecision(True, "confirmed via --yes", False, risk)

        note = "rollback available" if rollback_available else "no rollback for new files"
        return SafetyDecision(True, note, True, risk)

    def confirm(self, question: str, *, default: bool = False) -> bool:
        """Ask the developer. ``--yes`` and dry-run answer for them."""
        if self.dry_run or self.assume_yes:
            return True
        if self.confirmer is None:
            return default
        answer = self.confirmer(question, default)
        self.logger.info("confirmation %r -> %s", question, answer)
        return answer

    def require(self, question: str, *, default: bool = False) -> None:
        if not self.confirm(question, default=default):
            raise ConfirmationDeclined("aborted at your request; nothing was changed")

    # -- secret protection -------------------------------------------------
    def filter_context_files(self, paths: Sequence[str], *, allow_secrets: bool = False) -> tuple[list[str], list[str]]:
        """Split paths into (safe, refused). Refused files never reach an AI."""
        safe: list[str] = []
        refused: list[str] = []
        for path in paths:
            if not allow_secrets and is_secret_path(path):
                refused.append(path)
            else:
                safe.append(path)
        if refused:
            self.logger.info("excluded %s secret-bearing file(s) from AI context", len(refused))
        return safe, refused

    def risk_of(self, paths: Sequence[str]) -> RiskLevel:
        """Heuristic risk for a set of paths - native code is never low risk."""
        if not paths:
            return RiskLevel.LOW
        highest = RiskLevel.LOW
        for path in paths:
            posix = str(path).replace("\\", "/")
            if posix.startswith(("android/", "ios/")) or posix.endswith(
                (".gradle", ".pbxproj", ".plist", "Podfile", ".kt", ".java", ".swift", ".m", ".mm")
            ) or posix in {"package.json", "yarn.lock", "package-lock.json", "pnpm-lock.yaml"}:
                candidate = RiskLevel.HIGH
            elif posix.endswith((".config.js", ".config.ts", "tsconfig.json", "babel.config.js")):
                candidate = RiskLevel.MEDIUM
            else:
                candidate = RiskLevel.LOW
            if candidate.rank > highest.rank:
                highest = candidate
        return highest
