"""Command registry.

Requirement §29: the command system must be extensible. A new command
registers itself here and the CLI picks it up - no edits to the router.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .command import AgentCommand
from .context import AgentContext

CommandFactory = Callable[[AgentContext], AgentCommand]


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    description: str
    factory: CommandFactory
    read_only: bool
    phase: int = 1

    def build(self, context: AgentContext) -> AgentCommand:
        return self.factory(context)


COMMANDS: dict[str, CommandSpec] = {}


def register(command_class: type[AgentCommand], *, phase: int = 1) -> type[AgentCommand]:
    """Register a command class by its ``name`` attribute."""
    spec = CommandSpec(
        name=command_class.name,
        description=command_class.description,
        factory=command_class,
        read_only=command_class.read_only,
        phase=phase,
    )
    COMMANDS[spec.name] = spec
    return command_class


def resolve(name: str) -> CommandSpec:
    try:
        return COMMANDS[name]
    except KeyError as exc:  # pragma: no cover - CLI validates names first
        raise KeyError(f"unknown command: {name}") from exc


def available() -> list[CommandSpec]:
    return sorted(COMMANDS.values(), key=lambda spec: (spec.phase, spec.name))
