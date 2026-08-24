"""Proving a change did not break the project.

One runner, used by every command that writes: ``fix``, ``feature``, ``test``,
``upgrade``, ``migrate`` and ``release``. A step the project cannot run reports
``SKIP`` with the reason, never a pass.
"""

from __future__ import annotations

from .runner import STEP_NAMES, ProjectValidator

__all__ = ["STEP_NAMES", "ProjectValidator"]
