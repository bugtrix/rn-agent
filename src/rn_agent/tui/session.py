"""One interactive session: the brain, the account, the model, the conversation.

The requirement this module exists for is "switch model mid-conversation without
losing anything". That means exactly one thing may change when you run
``/model``: which provider object the next request goes to. The scanned project,
the conversation history, the knowledge store and the run bookkeeping all belong
to the session, not to the provider, so they survive.

How the swap reaches the rest of the agent is worth knowing. Every existing
command asks ``context.ai`` for a provider, and that is a ``cached_property`` -
so the session builds the provider itself and seeds that cache. ``/review`` typed
in the terminal therefore runs against the model you picked a second ago, through
the same code path as ``rn-agent review`` on the command line. No command needed
changing, and there is no second way to reach a model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..ai.registry import build_provider, resolve_spec
from ..ai.types import Message
from ..auth.authenticator import AuthMethod
from ..auth.manager import AuthenticationManager, auth_for
from ..core.config import update_user_config
from ..core.context import AgentContext
from ..core.logging import get_logger
from ..errors import ProviderError, RNAgentError

if TYPE_CHECKING:  # imported lazily: the registry pulls in the cache file
    from ..ai.models import ModelInfo, ModelRegistry
    from ..ai.provider import AIProvider

#: How many turns to keep. Long enough for a working conversation, bounded so a
#: day-long session cannot silently grow the context sent to a model.
MAX_HISTORY_TURNS = 40


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    """Everything the banner and the status bar need, resolved once."""

    project_name: str | None
    project_root: str
    rn_version: str | None
    provider: str | None
    provider_label: str | None
    model: str | None
    auth_method: AuthMethod | None
    connected: bool
    account: str | None
    git_branch: str | None
    git_dirty: bool | None
    scanned: bool
    turns: int
    dry_run: bool

    @property
    def ready(self) -> bool:
        if not (self.provider and self.connected):
            return False
        # Cursor's CLI picks an account default when no model is stored.
        if self.provider == "cursor":
            return True
        return bool(self.model)

    @property
    def auth_label(self) -> str:
        if self.auth_method is None:
            return "none"
        return self.auth_method.label


@dataclass
class SessionManager:
    """The state one interactive run owns."""

    context: AgentContext
    auth: AuthenticationManager
    registry: ModelRegistry
    logger: logging.Logger = field(default_factory=lambda: get_logger("tui"))
    history: list[Message] = field(default_factory=list)
    #: Flags the terminal was started with, replayed onto every command run so
    #: `--dry-run rn-agent` and a dry-run slash command mean the same thing.
    dry_run: bool = False
    assume_yes: bool = False
    verbose: bool = False
    #: Set when the developer switched provider/model in this session.
    switched: bool = False
    _provider_cache: AIProvider | None = field(default=None, init=False, repr=False)

    # -- identity ----------------------------------------------------------
    @property
    def provider_name(self) -> str | None:
        return self.context.config.ai.provider

    @property
    def model_name(self) -> str | None:
        config = self.context.config.ai
        if config.model:
            return config.model
        if config.provider:
            try:
                return resolve_spec(config.provider).default_model or None
            except RNAgentError:
                return None
        return None

    # -- switching ---------------------------------------------------------
    def switch_provider(self, name: str, *, persist: bool = True) -> str:
        """Make ``name`` the active provider, keeping the conversation.

        The model is *not* carried across providers - a Claude model id means
        nothing to Gemini - so it resets to the new provider's default and the
        caller is expected to offer ``/model`` next.
        """
        spec = resolve_spec(name)
        entry = auth_for(spec.name)
        self.context.config.ai.provider = spec.name
        self.context.config.ai.model = spec.default_model or None
        self._invalidate()
        self.switched = True
        if persist:
            self._persist({"ai": {"provider": spec.name, "model": self.context.config.ai.model}})
        self.logger.info("session provider is now %s (%s)", spec.name, entry.method.value)
        return spec.name

    def switch_model(self, model: str, *, persist: bool = True) -> str:
        """Point the next request at ``model``. Nothing else changes."""
        self.context.config.ai.model = model
        self._invalidate()
        self.switched = True
        if persist:
            self._persist({"ai": {"model": model}})
        self.logger.info("session model is now %s", model)
        return model

    def set_task_model(self, task: str, model: str, *, persist: bool = True) -> None:
        """Bind a role (``migration``, ``debugging``, …) to a model."""
        setattr(self.context.config.ai.models, task, model)
        self._invalidate()
        if persist:
            self._persist({"ai": {"models": {task: model}}})

    def _persist(self, patch: dict[str, Any]) -> None:
        """Write the choice to the user config, so the next run remembers it."""
        if self.context.dry_run:
            return
        try:
            update_user_config(patch)
        except RNAgentError as error:  # pragma: no cover - unwritable home
            self.logger.warning("could not save the choice: %s", error.message)

    # -- the provider ------------------------------------------------------
    def _invalidate(self) -> None:
        self._provider_cache = None
        # Drop the context's cached provider so every command rebuilds against
        # the new selection on its next call.
        self.context.__dict__.pop("ai", None)

    def provider(self) -> AIProvider:
        """The provider for the active selection, built on demand.

        Also seeded into ``context.ai`` so existing commands - which know nothing
        about this session - use the same object.
        """
        if self._provider_cache is not None:
            return self._provider_cache
        name = self.provider_name
        if not name:
            raise ProviderError(
                "no AI provider selected",
                hint="Run /login to connect an account, or /provider to choose one.",
            )
        spec = resolve_spec(name)
        credential = self.auth.credential(spec.name)
        if spec.requires_credential and not credential:
            raise ProviderError(
                f"{spec.name} is not connected",
                hint=f"Run /login {spec.name}.",
            )
        provider = build_provider(
            self.context.config.ai,
            credential=credential,
            provider_name=spec.name,
            model=self.model_name,
            **self._provider_extras(spec.name),
        )
        self._provider_cache = provider
        self.context.__dict__["ai"] = provider
        return provider

    def _provider_extras(self, name: str) -> dict[str, Any]:
        """Provider-specific construction flags that depend on *how* we signed in.

        Google's API takes an API key in one header and an OAuth bearer token in
        another, so the provider has to be told which it was handed. Getting this
        from the authenticator - rather than guessing from the token's shape -
        is what keeps the displayed auth method and the actual request in step.
        """
        if name == "cursor":
            # A local CLI reads a directory, not a URL.
            return {"workspace": str(self.context.paths.project_root)}
        if name == "vertex":
            # The Claude-on-Vertex URL is project-scoped, and that project pays.
            return {
                "project": self.context.config.ai.project,
                "region": self.context.config.ai.region,
            }
        if name != "google":
            return {}
        authenticator = self.auth.for_provider(name)
        uses_oauth = getattr(authenticator, "uses_oauth", None)
        return {"oauth": bool(uses_oauth()) if callable(uses_oauth) else False}

    def ready(self) -> bool:
        """Whether a request could be made right now, without making one."""
        try:
            self.provider()
        except RNAgentError:
            return False
        return True

    # -- conversation ------------------------------------------------------
    def remember(self, role: str, content: str) -> None:
        if not content.strip():
            return
        self.history.append(Message(role, content))
        excess = len(self.history) - MAX_HISTORY_TURNS
        if excess > 0:
            del self.history[:excess]

    def clear_history(self) -> int:
        count = len(self.history)
        self.history.clear()
        return count

    # -- models ------------------------------------------------------------
    def available_models(self, *, refresh: bool = False) -> list[ModelInfo]:
        """The active provider's catalogue, discovered or cached."""
        name = self.provider_name
        if not name:
            return []
        spec = resolve_spec(name)
        return list(
            self.registry.discover(
                spec.name,
                build=self.provider if self.auth.connected(spec.name) else None,
                connected=self.auth.connected(spec.name),
                suggested=spec.suggested_models,
                refresh=refresh,
            )
        )

    def all_models(self, *, refresh: bool = False) -> list[Any]:
        """Grouped models for the picker: this provider first, then the others."""
        entries: list[tuple[str, str, bool, tuple[str, ...]]] = []
        for entry in self.auth.providers():
            try:
                spec = resolve_spec(entry.provider)
            except RNAgentError:  # pragma: no cover - table and registry agree
                continue
            entries.append(
                (
                    entry.provider,
                    entry.label,
                    self.auth.connected(entry.provider),
                    spec.suggested_models,
                )
            )
        return self.registry.grouped(
            active_provider=self.provider_name,
            active_model=self.model_name,
            providers=entries,
            build=self._build_for,
            refresh=refresh,
        )

    def _build_for(self, name: str) -> AIProvider:
        """A provider instance for discovery, for any connected provider."""
        spec = resolve_spec(name)
        return build_provider(
            self.context.config.ai,
            credential=self.auth.credential(spec.name),
            provider_name=spec.name,
            model=None,
            **self._provider_extras(spec.name),
        )

    # -- reporting ---------------------------------------------------------
    def snapshot(self) -> StatusSnapshot:
        """What the banner and status bar show. Never performs a request."""
        config = self.context.config.ai
        name = config.provider
        state = self.auth.state(name) if name else None
        entry = auth_for(name) if name and name in {p.provider for p in self.auth.providers()} else None
        project = None
        rn_version = None
        scanned = self.context.has_project_context()
        if scanned:
            try:
                project = self.context.project
                rn_version = project.rn_version
            except RNAgentError:  # pragma: no cover - unreadable context file
                scanned = False
        git = self.context.git
        branch: str | None = None
        dirty: bool | None = None
        if git.is_repository():
            status = git.status()
            branch, dirty = status.branch, status.dirty
        return StatusSnapshot(
            project_name=(project.name if project else None) or self.context.root.name,
            project_root=str(self.context.root),
            rn_version=rn_version,
            provider=name,
            provider_label=entry.label if entry else name,
            model=self.model_name,
            auth_method=state.method if state else None,
            connected=bool(state and state.connected),
            account=state.account if state else None,
            git_branch=branch,
            git_dirty=dirty,
            scanned=scanned,
            turns=len(self.history),
            dry_run=self.context.dry_run,
        )
