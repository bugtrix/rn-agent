# Phase 7 report

Scope delivered: the interactive terminal, account-based authentication where
providers officially support it, a model registry, and the slash-command surface
that exposes every existing command inside the session.

## Implemented

| Area | Modules |
|---|---|
| Auth abstraction | `auth/authenticator.py` (`AuthMethod`, `AuthCapability`, `Authenticator`), `auth/methods.py` (API key / OAuth / local), `auth/manager.py` (`AuthenticationManager` + the provider capability table) |
| OAuth | `auth/oauth.py` (PKCE S256, one-shot loopback listener, RFC 8628 device grant, `client_secret.json` loader, token exchange and refresh, `TokenStore` in the keychain) |
| Providers | `ai/google.py` (`GoogleProvider`, key **and** OAuth bearer paths), `ai/vertex.py` (`VertexAnthropicProvider`: Claude on Google Cloud), registered in `ai/registry.py` |
| Models | `ai/models.py` (`ModelRegistry`: discovery, cross-run cache, grouping, `resolve`, fuzzy `search`) |
| Terminal | `tui/select.py` (the one picker), `tui/chrome.py` (banner, status bar), `tui/session.py` (`SessionManager`), `tui/router.py` (`CommandRouter`), `tui/handlers.py` (session commands), `tui/palette.py` (Ctrl+K), `tui/dialogs.py`, `tui/wizard.py`, `tui/agent.py`, `tui/app.py` |
| Routing | `agents/intent.py` (deterministic intent detection), `agents/prompts.py` (`chat_messages`, and `SYSTEM_RULES` split from the JSON contract) |
| Config | `models/config.py` (`UIConfig`: `interactive`, `colors`, `banner`, `status_bar`) |
| Wiring | `cli/app.py` (bare invocation opens the terminal), `core/context.py` (`AgentContext.auth`, credentials resolved through the manager) |

## Decisions

**The login UX is only as good as its honesty.** Anthropic prohibits third-party
use of subscription OAuth, OpenAI's "Sign in with ChatGPT" grants identity rather
than model access, and Google publishes an OAuth flow for the Gemini API. So
`/login` shows each provider's *real* mechanism, `/status` prints the reason an
API-key provider is not an account login, and the one provider that can honestly
offer "sign in with your account" does. Sources are in
[`docs/authentication.md`](authentication.md). No cookie reading, no token
scraping, no impersonation of another client, and no invented quota percentages.

**One picker, four uses.** Providers, models, the palette and every dialog are the
same widget, so navigation is learned once. It is split into a `Selector` state
machine and a thin prompt_toolkit shell, which is why arrow/filter/disabled-row
behaviour is unit-tested with no tty at all.

**Slash commands re-enter the CLI rather than reimplementing it.** `/health
--deep` calls the real Typer app in-process and catches its `SystemExit`. A flag
added to the command line works in the terminal the same day, usage errors render
exactly as they do outside, and there is nowhere for the two surfaces to drift.
The only hand-written handlers are the ones with no CLI twin (`/login`,
`/provider`, `/model`, `/status`, `/context`, `/clear`).

**Switching a model changes one thing.** `SessionManager` owns the project, the
conversation and the account; a switch rebuilds the provider and seeds
`context.ai`, which every existing command already reads. So `/review` after
`/model` runs on the model just chosen, through unchanged command code, with the
conversation intact. Ctrl+P cycles *within* the connected provider on purpose - a
keystroke should never move billing to another account.

**Credentials resolve in one place.** `AgentContext.ai` now asks the
`AuthenticationManager`, not the key-only credential store, so an OAuth session
works for `rn-agent review` on the command line exactly as it does in the
terminal. Provider construction is told *how* it was authenticated (Google needs
a bearer header rather than a key header), so the displayed auth method and the
actual request cannot disagree.

**Prose is routed deterministically.** "fix my android build" maps to `/fix` with
a regex table, not a model call - routing is a keyword problem, and paying a model
to choose a command adds latency and a new way to be wrong. The suggestion is
*offered*, never executed silently, because a command may write files. Anything
unmatched is answered as a question with budgeted, secret-filtered context.

**The model catalogue is discovered, not hard-coded.** `GoogleProvider.suggested_models`
is empty on purpose and `ModelRegistry` prefers the account's real list, caches it
across runs, and labels a fallback as `suggested`. A disconnected provider is
listed with its reason rather than hidden - "why is Opus missing?" is a worse
experience than "Opus - openai not connected".

**A dry run needs no repository.** `migrate` previously demanded a clean git tree
even to preview a plan; the wizard's "Analyze" step is a dry run, so the git gates
now apply only when something will actually be written.

**AI in a migration is consented to, once.** The engine asks before spending
tokens on a build failure, through the safety confirmer - which the command line
answers with `--yes`/`--no-ai` and the terminal turns into the `[Analyze] [Skip]`
dialog. Same decision, two surfaces.

## Verification

* 726 pytest tests (up from 573), `ruff check src tests` clean, `mypy` clean on
  133 source files
* new suites: `test_auth_methods.py` (34), `test_tui.py` (47),
  `test_google_provider.py` (21), `test_model_registry.py` (25),
  `test_vertex.py` (14)
* the device grant is tested through its own transport: the code is announced
  once, `authorization_pending` is a wait rather than a failure, and the poll
  interval is the provider's
* `login vertex` was run against Google's **real** device endpoint with a
  throwaway client id: the request shape is right and Google's own 401 is
  surfaced verbatim rather than reinterpreted
* the OAuth flow is tested end to end over a **real loopback socket**: a fake
  browser plays the provider's part, the code is exchanged, and a callback
  carrying the wrong `state` is refused
* the interactive terminal was driven through a **real pty**: the banner,
  `/status`, `/whoami`, `/help`, the provider picker and the model picker with
  arrow keys and Enter, the status bar refresh, and `/exit`
* live model discovery was exercised against a real Anthropic account through
  that pty - the picker listed the account's actual catalogue, and selecting a
  model updated the status bar
* no test performs network I/O; the `conftest` guard makes `httpx` raise, and
  every provider and registry test injects a fake transport

## Bugs found by the tests and fixed

1. `MacKeychainBackend.set` interpolates the secret into a `security -i` script,
   so a JSON token payload would have broken the write - token payloads are now
   base64url-encoded before they reach any backend, and a test asserts the stored
   value contains no spaces or quotes
2. bare `rn-agent` outside a React Native project raised instead of rendering the
   error panel, because the terminal built its session before anything could
   catch `RNAgentError`
3. the fuzzy filter matched the *concatenation* of id, label and provider, so
   `clop` "found" `claude-sonnet-4-5` by borrowing the `p` from `anthropic` -
   matching is now per field
4. Rich swallowed `[query]` in the `/help` usage column as a style tag, silently
   deleting the placeholder it documents; usage strings are escaped
5. `SYSTEM_PREAMBLE` demanded "reply with JSON only", which the conversational
   path inherited - the honesty rules are now separate from the JSON contract
6. `migrate --dry-run` required a git repository to preview a plan

## Not implemented

**Claude on an Anthropic subscription.** There is no supported mechanism; see
`docs/authentication.md`. What *is* built is the nearest legitimate path: Claude
through Google Cloud's Vertex AI (`login vertex`), account-based via Google
OAuth, no Anthropic key, billed to the developer's own Cloud project. Anthropic
publishes the models there and Google authenticates them, so the "browser login
that ends at Claude" shape the brief asked for exists - as a different product
with a different invoice, which the UI says out loud.

**A full-screen Textual layout.** The terminal is prompt_toolkit over Rich: a
scrolling conversation with full-screen pickers and dialogs. A persistent
split-pane app would be a different program, and the reports (`health`,
`migrate`, `upgrade`) are already Rich renderables that must keep working when
piped to a file.
