"""Cursor, reached through its own CLI instead of an HTTP API.

Cursor publishes no chat-completions endpoint. What it publishes is a coding
*agent*: the `cursor-agent` CLI (and a Cloud Agents REST API for repo-scoped
runs). So this provider is the one in the registry that does not speak HTTP - it
runs the local binary and reads the JSON it prints, which is the documented
automation contract (``-p --output-format json``).

Two deliberate choices, both about staying inside rn-agent's safety envelope:

* **No ``--mode``, ``--trust``, and never ``--force``.** This CLI's ``--mode``
  only accepts ``ask`` and ``plan``. ``ask`` is why it answers "I'm in Ask mode"
  and refuses file-change JSON. Omitting ``--mode`` is the coding agent (there
  is no ``--mode agent``). Print mode has write and shell tools, and
  ``--force`` / ``--yolo`` auto-approve them - so those flags stay off.
  ``--trust`` only skips Cursor's workspace-trust prompt. ``rn-agent delegate``
  is the opt-in for letting Cursor edit the tree itself.
* **No SDK dependency.** ``cursor-sdk`` on PyPI is official but ships a bundled
  bridge binary (~50-60 MB per wheel, proprietary, public beta) and wraps the
  same documented CLI and REST surface. rn-agent ships an npm wrapper with a
  private venv, so a 50 MB dependency for one provider is a real cost for no
  capability. If the SDK later becomes the only route to something, it belongs
  behind an extra, not in the default install.

Docs: https://cursor.com/docs/cli/headless
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar

from ..errors import ProviderError
from ..net.http import DEFAULT_TIMEOUT, HttpResponse
from ..runner.command_runner import CommandRunner
from ..tools.cursor import MISSING_HINT, resolve_binary
from .provider import AIProvider, ProviderIdentity
from .types import Completion, Message

#: Shared ``ai.timeout_seconds`` is sized for one HTTP call. Cursor is a local
#: agent and routinely needs several minutes.
AGENT_TIMEOUT = 600.0

DOCS = "https://cursor.com/docs/cli/headless"
KEYS_URL = "https://cursor.com/dashboard?tab=integrations"
#: This CLI's ``--mode`` only accepts ``ask`` and ``plan``. Anything else,
#: including the word "agent", must be omitted so the coding agent runs.
CLI_MODES = frozenset({"ask", "plan"})

#: Cursor's own Ask mode refuses to propose file writes. This block sits in
#: every prompt so the CLI does not tell the developer to "switch to Agent mode".
HOST_CONTRACT = """\
You are the model for rn-agent, not Cursor Ask mode. The host applies every
file change after you reply. The developer may type any prompt, like an IDE.
Use the host JSON tools (read, grep, glob, npm, search, fetch, write, delete,
rename) or a short prose answer. To rename a screen, use rename (from + to),
not a second file. Do not use Cursor's own Write or Shell tools. Do not say
you are in Ask mode. Do not tell the developer to switch modes.\
"""


class CursorProvider(AIProvider):
    """The Cursor agent, driven headlessly as a completion backend."""

    name: ClassVar[str] = "cursor"
    label: ClassVar[str] = "Cursor CLI"
    env_var: ClassVar[str | None] = "CURSOR_API_KEY"
    #: The CLI can already hold a session from `cursor-agent login`, so a key is
    #: accepted but not demanded. ``verify()`` is what reports which one is live.
    requires_credential: ClassVar[bool] = False
    default_model: ClassVar[str] = ""
    #: Cursor already has an account default, and `--list-models` is the real
    #: catalogue, so an unset model means "let Cursor choose" - not an error.
    requires_model: ClassVar[bool] = False
    suggested_models: ClassVar[tuple[str, ...]] = ("composer-2.5",)
    docs_url: ClassVar[str] = DOCS
    unreachable_hint: ClassVar[str | None] = MISSING_HINT

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        mode: str | None = None,
        workspace: str | None = None,
        binary: str | None = None,
        **extra: Any,
    ) -> None:
        # The base wants a transport it will never use; a local binary has no
        # host. Keep the signature honest rather than inventing a base_url.
        extra.pop("transport", None)
        extra.pop("base_url", None)
        super().__init__(transport=_NoTransport(), **extra)
        if self.timeout < AGENT_TIMEOUT:
            self.timeout = AGENT_TIMEOUT
        self.mode = _cli_mode(mode)
        self.workspace = workspace
        self._binary = Path(binary).expanduser() if binary else None
        # The agent reads the repository it is standing in, so the workspace is
        # the working directory rather than an argument on every call.
        self._runner = runner or CommandRunner(
            cwd=Path(workspace) if workspace else Path.cwd(), logger=self._logger
        )

    # -- discovery ---------------------------------------------------------
    def executable(self) -> str:
        """The CLI to run: PATH, ``~/.local/bin``, or rn-agent's managed copy."""
        if self._binary is not None and self._binary.is_file():
            return str(self._binary)
        found = resolve_binary(runner=self._runner)
        if found is not None:
            return str(found)
        raise ProviderError(
            "the Cursor CLI is not installed",
            hint=self.unreachable_hint,
        )

    # -- the contract ------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        """A subprocess has no headers. The key travels in the environment."""
        return {}

    def _payload(
        self,
        messages: list[Message],
        *,
        model: str,
        system: str | None,
        max_output_tokens: int,
        temperature: float,
    ) -> dict[str, Any]:
        """Flatten the conversation into one prompt.

        The CLI takes a single prompt string, not a message array, so roles are
        rendered as labelled blocks. ``max_output_tokens`` and ``temperature``
        have no CLI equivalent and are deliberately dropped rather than faked.

        There is no length limit here: the prompt is written to the CLI's stdin,
        not passed as an argument, so ``ARG_MAX`` never enters into it.
        """
        _ = max_output_tokens, temperature
        system_text, chat = self._split_system(messages, system)
        blocks: list[str] = [HOST_CONTRACT]
        if system_text:
            blocks.append(system_text)
        for message in chat:
            label = "Developer" if message.role == "user" else "Assistant"
            blocks.append(f"[{label}]\n{message.content}")
        return {"prompt": "\n\n".join(blocks), "model": model}

    def _parse_completion(
        self, body: Mapping[str, Any], *, model: str, task: str | None
    ) -> Completion:
        """Read the documented ``{type: "result", result: "..."}`` object."""
        text = body.get("result")
        if not isinstance(text, str):
            raise ProviderError(
                "the Cursor CLI returned no result text",
                hint="Run the same prompt with `cursor-agent -p` to see what it printed.",
            )
        return Completion(
            text=text,
            provider=self.name,
            model=_reported_model(body) or model,
            # The CLI reports real token counts (camelCase, nested under
            # `usage`), so `/status` accounting is exact rather than blank.
            usage=self._usage(body, input_key="inputTokens", output_key="outputTokens"),
            stop_reason="error" if body.get("is_error") else None,
            task=task,
        )

    # -- transport ---------------------------------------------------------
    def _request(
        self, method: str, path: str, *, payload: dict[str, Any] | None = None
    ) -> HttpResponse:
        """Run the CLI instead of sending a request.

        This is the one seam the base class routes everything through, so
        overriding it keeps the model guard, the logging and the failure mapping
        above and below this method exactly as every HTTP provider has them.
        The returned :class:`HttpResponse` is a status plus a parsed body - which
        is all `_failure` and `_parse_completion` ever read.
        """
        _ = method, path
        prompt = str((payload or {}).get("prompt") or "")
        model = cli_model_id(str((payload or {}).get("model") or ""))
        argv = [self.executable(), "--print", "--output-format", "json", "--trust"]
        if self.mode:
            argv += ["--mode", self.mode]
        if model:
            argv += ["--model", model]
        if self.workspace:
            argv += ["--workspace", self.workspace]
        # The prompt goes on stdin, not argv. rn-agent's context budget can build
        # a prompt of a hundred thousand characters or more, and while ARG_MAX is
        # usually 1 MiB it is a platform limit with the environment counted
        # against it - stdin has no such ceiling and the CLI reads it happily.
        result = self._runner.run(
            argv,
            timeout=self.timeout,
            env=self._env(),
            input_text=prompt,
            # A completion is a read. Dry-run must not turn it into a no-op that
            # looks like an empty answer from the model.
            force=True,
        )
        if result.executable_missing:  # pragma: no cover - guarded by executable()
            raise ProviderError("the Cursor CLI is not installed", hint=self.unreachable_hint)
        if result.timed_out:
            raise ProviderError(
                f"the Cursor CLI did not finish within {self.timeout:.0f}s",
                hint=(
                    "Cursor is a local agent, not a single API call. "
                    "Set ai.timeout_seconds to 600 or more in .rn-agent/config.yaml."
                ),
            )
        response = _as_response(result.returncode, result.stdout, result.stderr)
        # The base `_request` maps a bad status onto an actionable error; keeping
        # that here is the difference between reporting a CLI failure and parsing
        # it as though it were an answer.
        if not response.ok:
            raise self._failure(response)
        return response

    def _failure(self, response: HttpResponse) -> ProviderError:
        """Keep workspace-trust and a rejected model distinct from a real outage."""
        detail = (self._error_message(response) or "").strip()
        folded = detail.casefold()
        text = (response.text or "").casefold()
        if "trust" in folded or "trust" in text:
            return ProviderError(
                "the Cursor CLI needs workspace trust for this project",
                hint=(
                    "Headless runs pass --trust so this prompt is skipped. "
                    "Retry with this build. --yolo / --force are not used here; "
                    "they auto-approve writes."
                ),
            )
        if "cannot use this model" in folded:
            asked = detail.split(":", 1)[-1].split("Available", 1)[0].strip().rstrip(".")
            return ProviderError(
                f"Cursor rejected this model: {asked or 'unknown'}",
                hint=(
                    "Cursor wants a model id, not the pretty name. "
                    "Run `/model composer-2.5` or pick again from `/model` "
                    "(the id is the token before ' - ')."
                ),
            )
        if "invalid" in folded and ("--mode" in folded or "allowed choices" in folded):
            return ProviderError(
                "the Cursor CLI rejected --mode",
                hint=(
                    "This CLI only accepts --mode ask or --mode plan. "
                    "The coding agent is the default: omit --mode."
                ),
            )
        return super()._failure(response)

    def _env(self) -> dict[str, str]:
        """Pass the key through the environment, never on the command line."""
        from ..tools.cursor import search_path

        env = {"PATH": search_path()}
        if self._credential:
            env["CURSOR_API_KEY"] = self._credential
        return env

    # -- catalogue ---------------------------------------------------------
    def list_models(self) -> tuple[str, ...]:
        """Ask the CLI what this account may use (``--list-models``)."""
        result = self._runner.run(
            [self.executable(), "--list-models"],
            timeout=self.timeout,
            env=self._env(),
            force=True,
        )
        if not result.ok:
            return ()
        return _model_names(result.stdout)

    def verify(self) -> ProviderIdentity:
        """Check the CLI's own auth status - no model call, so nothing is billed."""
        result = self._runner.run(
            [self.executable(), "status", "--format", "json"],
            timeout=self.timeout,
            env=self._env(),
            force=True,
        )
        if not result.ok:
            raise ProviderError(
                "the Cursor CLI is not signed in",
                hint="Run `cursor-agent login`, or export CURSOR_API_KEY.",
            )
        account = _account(result.stdout)
        models = self.list_models()
        detail = f"Cursor CLI signed in{f' as {account}' if account else ''}"
        if self._credential:
            detail = f"{detail} (CURSOR_API_KEY in use)"
        return ProviderIdentity(provider=self.name, ok=True, detail=detail, models=models)


def _cli_mode(raw: str | None) -> str | None:
    """``ask`` / ``plan``, or ``None`` so the coding agent runs.

    Passing ``agent`` is a CLI error: allowed choices are only ask and plan.
    """
    if not raw:
        return None
    folded = raw.strip().casefold()
    if folded in CLI_MODES:
        return folded
    return None


class _NoTransport:
    """Satisfies the transport slot for a provider that never sends a request."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> HttpResponse:  # pragma: no cover - unreachable by construction
        raise ProviderError(
            "the Cursor provider runs a local CLI and sends no HTTP request",
            hint="This is a bug: _request should have handled the call.",
        )


def _as_response(returncode: int, stdout: str, stderr: str) -> HttpResponse:
    """Map a CLI exit into the status/body pair the base class understands.

    The CLI prints its result object on success and writes a message to stderr
    on failure, so a non-zero exit becomes a 500 carrying that message - which
    `_failure` then renders with the provider's name and hint.
    """
    body = _first_json_object(stdout)
    if returncode != 0:
        message = stderr.strip() or _text(body) or f"exit code {returncode}"
        return HttpResponse(status=500, body={"error": message}, text=stdout)
    if body.get("is_error"):
        return HttpResponse(status=500, body={"error": _text(body) or "the agent reported an error"}, text=stdout)
    return HttpResponse(status=200, body=body, text=stdout)


def _first_json_object(stdout: str) -> dict[str, Any]:
    """The result object, whether the CLI printed one object or NDJSON.

    ``--output-format json`` prints a single object, but a CLI is free to print
    progress lines first, so the last parseable object with a ``result`` wins and
    a plain object is accepted as-is.
    """
    text = stdout.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    best: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(candidate, dict):
            continue
        if candidate.get("type") == "result" or "result" in candidate or not best:
            best = candidate
    return best


def _text(body: Mapping[str, Any]) -> str:
    for key in ("result", "message", "error"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _reported_model(body: Mapping[str, Any]) -> str:
    value = body.get("model")
    return value if isinstance(value, str) and value else ""


def _account(stdout: str) -> str:
    body = _first_json_object(stdout)
    for key in ("email", "account", "user"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _model_names(stdout: str) -> tuple[str, ...]:
    """Model ids from either a JSON array/object or one-per-line text.

    Cursor's human listing is ``id - Pretty Name``. Only the id is valid for
    ``--model``; keeping the label made `/model` persist a string the CLI
    then rejected.
    """
    text = stdout.strip()
    if not text:
        return ()
    try:
        parsed: Any = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, Mapping):
        parsed = parsed.get("models")
    if isinstance(parsed, Sequence) and not isinstance(parsed, (str, bytes)):
        names = []
        for entry in parsed:
            if isinstance(entry, str) and entry:
                names.append(cli_model_id(entry))
            elif isinstance(entry, Mapping):
                value = entry.get("id") or entry.get("name")
                if isinstance(value, str) and value:
                    names.append(cli_model_id(value))
        return tuple(name for name in names if name)
    return tuple(
        cli_model_id(line)
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith(("Available", "Models"))
    )


def cli_model_id(raw: str) -> str:
    """The token Cursor accepts as ``--model``.

    ``claude-opus-5-thinking-high - Claude Opus 5 1M Thinking`` is a listing
    line. The CLI only accepts the slug on the left.
    """
    text = raw.strip().lstrip("*-• ").strip()
    if " - " not in text:
        return text
    slug, _, pretty = text.partition(" - ")
    slug = slug.strip()
    if slug and " " not in slug and pretty.strip():
        return slug
    return text


def preferred_model(models: Sequence[str]) -> str | None:
    """Pick a Cursor model id to persist after login.

    Composer is Cursor's own default family; anything else is first in the
    account catalogue. ``None`` when the CLI reported no models.
    """
    names = [name for name in models if name]
    if not names:
        return None
    for name in names:
        if "composer" in name.casefold():
            return name
    return names[0]
