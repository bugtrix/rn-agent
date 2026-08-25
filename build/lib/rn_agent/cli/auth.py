"""AI setup commands: ``login``, ``logout``, ``whoami``, ``provider``, ``model``.

These are the only commands that work outside a React Native project: choosing a
provider is a property of *you*, not of one app. When they do run inside a
project they read its config too, so ``whoami`` answers for the app you are
standing in.

Nothing here talks to a provider except ``login`` (which verifies the key before
storing it), ``whoami --check`` and ``model --list --remote``. Everything else is
local and offline.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any

import typer

from ..ai.registry import ProviderSpec, build_provider, provider_names, resolve_spec, specs
from ..auth import session
from ..auth.store import CredentialStore
from ..core.config import load_config, update_project_config, update_user_config
from ..core.paths import AgentPaths, user_config_file
from ..errors import ProviderError, RNAgentError
from ..models.config import AIConfig, TaskModels
from ..project.detector import detect_project
from . import ui
from .options import OPTIONS

KNOWN_TASKS: tuple[str, ...] = tuple(TaskModels.model_fields)


# ---------------------------------------------------------------------------
# plumbing
# ---------------------------------------------------------------------------
@contextmanager
def _cli_errors() -> Iterator[None]:
    """Render an expected failure and exit with its code - never a traceback."""
    try:
        yield
    except RNAgentError as error:
        ui.error_panel(error.message, error.hint)
        raise typer.Exit(error.exit_code) from error


def _emit(payload: dict[str, Any], *, code: int = 0) -> None:
    if OPTIONS.json_output:
        ui.console().print_json(json.dumps(payload, default=str))
    raise typer.Exit(code)


def _paths() -> AgentPaths:
    """The project's paths when we are in one, otherwise the plain directory.

    Auth must work anywhere, so "not a React Native project" is not an error
    here - it just means only the user-level config layer exists.
    """
    start = OPTIONS.path or Path.cwd()
    try:
        return AgentPaths.for_project(detect_project(start).root)
    except RNAgentError:
        return AgentPaths.for_project(start)


def _load(paths: AgentPaths) -> AIConfig:
    return load_config(paths).ai


def _store() -> CredentialStore:
    return session.build_store()


def _write(patch: dict[str, Any], *, paths: AgentPaths, project: bool) -> Path | None:
    """Persist a config patch, or report what a dry run would have written."""
    if OPTIONS.dry_run:
        return None
    if project:
        if not paths.project_root.joinpath("package.json").is_file():
            raise ProviderError(
                "--project needs a React Native project directory",
                hint="Run it inside your app, or drop --project to save the preference for your user.",
            )
        return update_project_config(paths, patch)
    return update_user_config(patch)


def _target_label(path: Path | None, *, project: bool) -> str:
    if path is not None:
        return str(path)
    return f"dry run: {'project' if project else 'user'} config unchanged"


def _render_status(status: session.AuthStatus) -> None:
    ui.header("AI setup", status.provider or "not configured")
    rows: list[tuple[str, Any]] = [
        ("provider", status.provider or "-"),
        ("model", status.model or "-"),
        ("api host", status.base_url or "-"),
        ("credential", status.credential_label or "none"),
        ("key", status.credential_masked or "-"),
        ("storage", status.backend),
        ("ready", "yes" if status.ready else "no"),
    ]
    if status.stored:
        rows.append(("stored keys", ", ".join(status.stored)))
    if status.task_models:
        rows.append(
            ("task models", ", ".join(f"{task}={model}" for task, model in sorted(status.task_models.items())))
        )
    if not status.enabled:
        rows.append(("enabled", "no (ai.enabled: false)"))
    if status.verified is not None:
        rows.append(("verified", status.detail or ("yes" if status.verified else "no")))
    ui.key_values(rows)
    if status.backend_location and status.has_credential and not status.from_env:
        ui.blank()
        ui.warning(
            f"no OS keychain was reachable; the key is in {status.backend_location} (mode 0600)"
        )


def _announce_device(code: Any) -> None:
    """Show the code the developer types on the machine that has a browser.

    Printed rather than opened: the whole point of the device grant is that this
    machine cannot show a consent screen.
    """
    ui.blank()
    ui.bullet(f"Open [value]{code.verification_url}[/value]")
    ui.bullet(f"Enter the code [value]{code.user_code}[/value]")
    ui.note(f"waiting for approval (expires in {int(code.expires_in // 60)} min)")


def _read_secret(spec: ProviderSpec, *, api_key: str | None, from_stdin: bool) -> str | None:
    """Get the key from the least dangerous source the developer offered.

    A provider that needs no credential is never *prompted* for one - Ollama has
    no account, and Cursor's CLI holds its own session. But an explicit
    ``--api-key``/``--stdin`` is a decision the developer already made (it is how
    CI supplies ``CURSOR_API_KEY``), so it is honoured rather than dropped.
    """
    if from_stdin:
        return sys.stdin.read().strip() or None
    if api_key:
        return api_key.strip() or None
    if not spec.requires_credential:
        return None
    typed = ui.ask_secret(f"{spec.label} API key")
    if typed:
        return typed
    return None


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------
def login(
    provider_name: Annotated[
        str | None,
        typer.Argument(
            metavar="[PROVIDER]",
            help=f"One of: {', '.join(provider_names())} (default: the configured provider).",
        ),
    ] = None,
    api_key: Annotated[
        str | None,
        typer.Option(
            "--api-key",
            help="The key itself. Prefer --stdin: arguments are visible to other processes.",
        ),
    ] = None,
    from_stdin: Annotated[
        bool, typer.Option("--stdin", help="Read the key from standard input (CI-friendly).")
    ] = False,
    client_id: Annotated[
        str | None,
        typer.Option("--client-id", help="OAuth client id, for a provider that supports OAuth."),
    ] = None,
    client_secret: Annotated[
        str | None, typer.Option("--client-secret", help="OAuth client secret (installed app).")
    ] = None,
    client_file: Annotated[
        Path | None,
        typer.Option(
            "--client-file",
            help="Google's downloaded client_secret.json, instead of --client-id/--client-secret.",
        ),
    ] = None,
    device: Annotated[
        bool,
        typer.Option(
            "--device",
            help="Sign in on another machine (RFC 8628). Used automatically with no browser.",
        ),
    ] = False,
    cloud_project: Annotated[
        str | None,
        typer.Option(
            "--cloud-project",
            help="Google Cloud project that pays for Vertex AI requests.",
        ),
    ] = None,
    region: Annotated[
        str | None,
        typer.Option("--region", help="Vertex AI location (default: global)."),
    ] = None,
    model_name: Annotated[
        str | None, typer.Option("--model", help="Default model for this provider.")
    ] = None,
    base_url: Annotated[
        str | None,
        typer.Option("--base-url", help="Custom API host (gateway, or Ollama on another machine)."),
    ] = None,
    no_verify: Annotated[
        bool, typer.Option("--no-verify", help="Skip the live credential check (offline).")
    ] = False,
    project: Annotated[
        bool,
        typer.Option("--project", help="Save provider/model in .rn-agent/config.yaml, not your user config."),
    ] = False,
) -> None:
    """Connect your own AI account, by whatever mechanism that provider supports."""
    with _cli_errors():
        from ..auth.authenticator import AuthMethod
        from ..auth.manager import AuthenticationManager
        from ..auth.methods import browser_available

        paths = _paths()
        config = _load(paths)
        spec = resolve_spec(provider_name or config.provider)
        manager = AuthenticationManager()
        authenticator = manager.for_provider(spec.name)
        capability = authenticator.capability

        # Only ask for a key when a key is what this provider actually takes.
        secret: str | None = None
        if capability.method is AuthMethod.API_KEY or api_key or from_stdin:
            secret = _read_secret(spec, api_key=api_key, from_stdin=from_stdin)
            if secret is None and spec.requires_credential and not authenticator.state().connected:
                raise ProviderError(
                    f"no API key provided for {spec.name}",
                    hint=(
                        f"Pass --api-key, pipe it with --stdin, or export {spec.env_var}. "
                        f"Keys: {spec.docs_url}"
                    ),
                )

        if not OPTIONS.json_output:
            ui.header(f"Sign in · {spec.label}", f"auth: {capability.label}")
            if capability.detail:
                ui.note(capability.detail)
            if capability.unsupported_note:
                # Say why this is a key rather than an account login.
                ui.console().print(f"  [muted]{capability.unsupported_note}[/muted]")
            if capability.method is AuthMethod.OAUTH and not secret:
                # Say what is about to happen, not what usually happens: on a
                # machine with no browser this is a device code, not a redirect.
                if device or not browser_available():
                    ui.bullet("No browser here - signing in with a device code")
                else:
                    ui.bullet(f"Opening {spec.label} in your browser…")

        # Order matters. A key is verified *before* it is stored, so a credential
        # the provider rejects never reaches the keychain. An OAuth session can
        # only be verified after the flow completes, because until then there is
        # no token to check.
        identity = None
        # Ollama needs no credential but still has a server worth reaching, so
        # verification is gated on the developer's --no-verify, not on whether a
        # secret exists.
        verify = not no_verify
        if verify and secret:
            identity = _verify(
                spec, config, manager, credential=secret, model=model_name, base_url=base_url
            )

        outcome = authenticator.login(
            secret=secret,
            client_id=client_id,
            client_secret=client_secret,
            client_file=str(client_file) if client_file else None,
            device=device,
            announce=None if OPTIONS.json_output else _announce_device,
            dry_run=OPTIONS.dry_run,
        )

        if verify and identity is None and outcome.state.connected:
            identity = _verify(
                spec,
                config,
                manager,
                credential=manager.credential(spec.name),
                model=model_name,
                base_url=base_url,
            )

        patch: dict[str, Any] = {"ai": {"provider": spec.name}}
        if model_name:
            patch["ai"]["model"] = model_name
        if base_url:
            patch["ai"]["base_url"] = base_url
        if cloud_project:
            patch["ai"]["project"] = cloud_project
        if region:
            patch["ai"]["region"] = region
        written = _write(patch, paths=paths, project=project)

        effective_model = model_name or config.model_for(None) or spec.default_model
        effective_host = base_url or config.base_url or spec.base_url
        payload = {
            **outcome.as_dict(),
            "auth": capability.as_dict(),
            "model": effective_model,
            "base_url": effective_host,
            "verified": None if not verify else identity is not None and identity.ok,
            "identity": identity.as_dict() if identity else None,
            "config_file": str(written) if written else None,
        }
        if not OPTIONS.json_output:
            ui.blank()
            ui.key_values(
                [
                    ("provider", spec.label),
                    ("auth", capability.label),
                    ("model", effective_model),
                    ("api host", effective_host),
                    ("credential", outcome.state.label or "-"),
                ]
            )
            state = outcome.state
            if state.connected:
                account = f" as {state.account}" if state.account else ""
                ui.success(f"{spec.label} connected{account} · auth: {state.method.label}")
            elif capability.method is not AuthMethod.NONE:
                ui.warning(f"{spec.label} is not connected")
            if outcome.stored:
                ui.note(f"credential stored in {manager.credentials.backend.label}")
            if identity is not None:
                ui.success(identity.detail)
            elif no_verify:
                ui.note("credential not verified (--no-verify)")
            ui.note(f"config: {_target_label(written, project=project)}")
            for warning in outcome.warnings:
                ui.warning(warning)
        _emit(payload)


def _verify(
    spec: Any,
    config: AIConfig,
    manager: Any,
    *,
    credential: str | None,
    model: str | None,
    base_url: str | None,
) -> Any:
    """Check a credential against the provider's own endpoint.

    Takes the credential explicitly so a key can be checked *before* it is
    stored - which is what stops a rejected key reaching the keychain - and an
    OAuth token can be checked after the flow, when one finally exists.
    """
    from ..ai.registry import build_provider

    extras: dict[str, Any] = {}
    if spec.name == "google":
        uses_oauth = getattr(manager.for_provider("google"), "uses_oauth", None)
        extras["oauth"] = bool(uses_oauth()) if callable(uses_oauth) else False
    provider = build_provider(
        config,
        credential=credential,
        provider_name=spec.name,
        model=model,
        base_url=base_url,
        **extras,
    )
    return provider.verify()


# ---------------------------------------------------------------------------
# logout
# ---------------------------------------------------------------------------
def logout(
    provider_name: Annotated[
        str | None, typer.Argument(metavar="[PROVIDER]", help="Provider to forget.")
    ] = None,
    every: Annotated[bool, typer.Option("--all", help="Forget every stored credential.")] = False,
) -> None:
    """Remove a stored credential from your keychain."""
    with _cli_errors():
        paths = _paths()
        config = _load(paths)
        store = _store()
        if every:
            targets = [entry.provider for entry in store.stored()]
        else:
            targets = [resolve_spec(provider_name or config.provider).name]

        if OPTIONS.dry_run:
            if not OPTIONS.json_output:
                ui.note(f"dry run: would forget {', '.join(targets) or 'nothing'}")
            _emit({"removed": [], "targets": targets, "dry_run": True})

        removed = [name for name in targets if session.logout(provider=name, store=store)]
        if not OPTIONS.json_output:
            if removed:
                ui.success(f"forgot {', '.join(removed)} ({store.backend.label})")
            else:
                ui.note("nothing was stored for " + (", ".join(targets) or "any provider"))
            for name in targets:
                spec = resolve_spec(name)
                if spec.env_var and spec.env_var in os.environ:
                    ui.warning(f"{spec.env_var} is still set in this shell and takes precedence")
        _emit({"removed": removed, "targets": targets, "dry_run": False})


# ---------------------------------------------------------------------------
# whoami
# ---------------------------------------------------------------------------
def whoami(
    check: Annotated[
        bool, typer.Option("--check", help="Ask the provider whether the credential works.")
    ] = False,
) -> None:
    """Show which AI provider, model and credential are in effect."""
    with _cli_errors():
        paths = _paths()
        config = _load(paths)
        from ..auth.manager import AuthenticationManager

        status = session.status(
            config,
            _store(),
            check=check,
            sessions=AuthenticationManager().stored_sessions(),
        )
        payload = {
            **status.as_dict(),
            "user_config": str(user_config_file()),
            "project_config": str(paths.config_file) if paths.config_file.is_file() else None,
        }
        if not OPTIONS.json_output:
            _render_status(status)
            if not status.provider:
                ui.blank()
                ui.bullet("run `rn-agent login <provider>` to connect an account")
                ui.note(f"providers: {', '.join(provider_names())}")
        code = 0 if status.ready and status.verified is not False else 10
        _emit(payload, code=code)


# ---------------------------------------------------------------------------
# provider
# ---------------------------------------------------------------------------
def provider(
    name: Annotated[
        str | None, typer.Argument(metavar="[NAME]", help="Provider to make the default.")
    ] = None,
    show_list: Annotated[bool, typer.Option("--list", help="List the supported providers.")] = False,
    clear: Annotated[bool, typer.Option("--clear", help="Forget the provider preference.")] = False,
    project: Annotated[
        bool, typer.Option("--project", help="Write to .rn-agent/config.yaml instead of user config.")
    ] = False,
) -> None:
    """Show, list or choose the AI provider."""
    with _cli_errors():
        paths = _paths()
        config = _load(paths)
        store = _store()

        if clear:
            written = _write({"ai": {"provider": None, "model": None}}, paths=paths, project=project)
            if not OPTIONS.json_output:
                ui.success("provider preference cleared")
                ui.note(f"config: {_target_label(written, project=project)}")
            _emit({"provider": None, "config_file": str(written) if written else None})

        if name:
            spec = resolve_spec(name)
            written = _write({"ai": {"provider": spec.name}}, paths=paths, project=project)
            credential = store.resolve(spec)
            if not OPTIONS.json_output:
                ui.success(f"provider set to {spec.name} ({spec.label})")
                ui.note(f"config: {_target_label(written, project=project)}")
                if credential is None and spec.requires_credential:
                    ui.warning(f"no credential yet - run `rn-agent login {spec.name}`")
            _emit(
                {
                    "provider": spec.name,
                    "credential_source": credential.describe() if credential else None,
                    "config_file": str(written) if written else None,
                }
            )

        active = config.provider
        # One keychain read per provider: `store.resolve` can shell out.
        resolved = [(spec, store.resolve(spec)) for spec in specs()]
        rows = [
            (
                f"{'*' if spec.name == active else ' '} {spec.name}",
                spec.env_var or "-",
                credential.describe()
                if credential
                else ("not needed" if not spec.requires_credential else "-"),
                spec.default_model,
            )
            for spec, credential in resolved
        ]
        if not OPTIONS.json_output:
            ui.table(
                ["provider", "key env var", "credential", "default model"],
                rows,
                title="AI providers (* = active)",
            )
            if not active:
                ui.note("no provider selected; `rn-agent login <provider>` picks one")
        _emit(
            {
                "active": active,
                "providers": [
                    {
                        **spec.as_dict(),
                        "credential_source": credential.describe() if credential else None,
                    }
                    for spec, credential in resolved
                ],
            }
        )


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------
def model(
    name: Annotated[
        str | None, typer.Argument(metavar="[NAME]", help="Model id to use, e.g. claude-sonnet-4-5.")
    ] = None,
    task: Annotated[
        str | None,
        typer.Option("--task", help=f"Set the model for one task only: {', '.join(KNOWN_TASKS)}."),
    ] = None,
    show_list: Annotated[
        bool, typer.Option("--list", help="List models for the active provider.")
    ] = False,
    remote: Annotated[
        bool, typer.Option("--remote", help="With --list: ask the provider's API instead of the bundled hints.")
    ] = False,
    clear: Annotated[bool, typer.Option("--clear", help="Forget the model preference.")] = False,
    project: Annotated[
        bool, typer.Option("--project", help="Write to .rn-agent/config.yaml instead of user config.")
    ] = False,
) -> None:
    """Show, list or choose the model - globally or per task."""
    with _cli_errors():
        paths = _paths()
        config = _load(paths)
        if task and task not in KNOWN_TASKS:
            raise ProviderError(
                f"unknown task: {task}",
                hint=f"Known tasks: {', '.join(KNOWN_TASKS)}.",
            )

        if clear:
            patch: dict[str, Any] = {"ai": {"models": {task: None}}} if task else {"ai": {"model": None}}
            written = _write(patch, paths=paths, project=project)
            if not OPTIONS.json_output:
                ui.success(f"model preference cleared{f' for {task}' if task else ''}")
                ui.note(f"config: {_target_label(written, project=project)}")
            _emit({"model": None, "task": task, "config_file": str(written) if written else None})

        if show_list:
            spec = resolve_spec(config.provider)
            if remote:
                store = _store()
                credential = store.require(spec)
                instance = build_provider(
                    config,
                    credential=credential.value if credential else None,
                    provider_name=spec.name,
                )
                names, source = instance.list_models(), f"{spec.label} API"
            else:
                names, source = spec.suggested_models, "bundled suggestions"
            active = config.model_for(None) or spec.default_model
            if not OPTIONS.json_output:
                ui.table(
                    ["model", "active"],
                    [(item, "*" if item == active else "") for item in names],
                    title=f"{spec.name} models",
                )
                ui.note(f"source: {source}")
                if not remote:
                    ui.note("`rn-agent model --list --remote` asks your account instead")
            _emit({"provider": spec.name, "source": source, "models": list(names), "active": active})

        if name:
            patch = {"ai": {"models": {task: name}}} if task else {"ai": {"model": name}}
            written = _write(patch, paths=paths, project=project)
            if not OPTIONS.json_output:
                ui.success(f"model set to {name}{f' for {task}' if task else ''}")
                ui.note(f"config: {_target_label(written, project=project)}")
            _emit({"model": name, "task": task, "config_file": str(written) if written else None})

        status = session.status(config, _store())
        if not OPTIONS.json_output:
            ui.header("model", status.provider or "no provider")
            rows: list[tuple[str, Any]] = [("default", status.model or "-")]
            rows.extend(sorted(status.task_models.items()))
            ui.key_values(rows)
            ui.note("`rn-agent model <name>` sets it; --task <task> sets one task only")
        _emit({"provider": status.provider, "model": status.model, "task_models": status.task_models})


def register(app: typer.Typer) -> None:
    """Attach the AI setup commands to the root app."""
    for command in (login, logout, whoami, provider, model):
        app.command()(command)
