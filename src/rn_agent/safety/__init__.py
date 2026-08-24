"""Safety gates: confirmation, risk policy, dry-run, secret protection."""

from __future__ import annotations

from .manager import SafetyDecision, SafetyManager

__all__ = ["SafetyDecision", "SafetyManager"]
