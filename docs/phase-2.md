# Phase 2 report

Scope delivered: the AI provider abstraction and credential handling, plus the
five commands that configure them — per the build plan's §5/§7/§9.

## Implemented

| Area | Modules |
|---|---|
| Providers | `ai/provider.py` (contract), `ai/anthropic.py`, `ai/openai.py`, `ai/ollama.py`, `ai/registry.py`, `ai/types.py` |
| Transport | `ai/http.py` (`JsonTransport` protocol, `HttpxTransport`, `TransportError`) |
| Credentials | `auth/keychain.py` (macOS `security`, Secret Service, Windows DPAPI, `0600` file, disabled), `auth/store.py` (precedence + index), `auth/session.py` (status/login/logout policy) |
| CLI | `cli/auth.py` (`login`, `logout`, `whoami`, `provider`, `model`), `cli/options.py` (shared flags), `cli/ui.py` (hidden prompt) |
| Wiring | `core/context.py` (`ai`, `credentials`, `ai_ready()`, `record_ai_usage()`), `core/config.py` (patch writers), `core/paths.py` (user credential paths), `models/config.py` (request policy) |

Commands:

```
rn-agent login [PROVIDER] [--api-key K | --stdin] [--model M] [--base-url U] [--no-verify] [--project]
rn-agent logout [PROVIDER] [--all]
rn-agent whoami [--check]
rn-agent provider [NAME] [--list] [--clear] [--project]
rn-agent model [NAME] [--task T] [--list [--remote]] [--clear] [--project]
```

## Decisions

**Verify, then store.** `login` calls the provider's own model endpoint with the
key before writing it anywhere, so a rejected key never reaches a keychain. The
same call doubles as the model catalogue, which is why `login` can warn that the
chosen model is not in *this* account's list.

**Secrets on stdin, never in argv.** macOS uses `security -i` (subcommands on
stdin) rather than `-w <secret>`; `secret-tool store` and PowerShell read stdin
too. A key therefore never appears in another user's `ps` output. Every write is
read back and compared, because a silent write is not a write.

**Environment beats keychain, and the agent says which it used.** The same
provenance rule the scanner applies to versions: `whoami` prints
`ANTHROPIC_API_KEY (environment)` or the backend name, because "I exported a key"
and "a key is in my keychain" are different facts.

**A labelled fallback, not a silent one.** Containers and CI often have no
keyring. Instead of failing, the file backend stores the key `0600` under
`~/.config/rn-agent` and reports `backend_secure: false`, which the CLI turns
into a visible warning — the same "labelled fallback" pattern as the offline
compatibility table.

**Windows uses DPAPI, not `cmdkey`.** `cmdkey` can write Credential Manager
entries but not read them back, which is useless for an agent. DPAPI ciphertext
is user-scoped, so the trust boundary matches a keychain.

**One transport seam.** Providers never touch `httpx` directly; they take a
`JsonTransport`. That is what lets 352 tests assert exact request shapes with no
sockets, while the network guard in `conftest.py` stays armed.

**Config patches, not dumps.** `provider`/`model`/`login` merge a small patch
into the target YAML instead of writing a serialised model, so today's defaults
are not frozen into the file. Patch merges treat `null` as "clear this"; file
*layering* still treats `null` as "not set here" (`_deep_merge(null_clears=...)`).

**Phase 1 stays offline.** `AgentContext.ai` is a `cached_property` with
function-local imports, so `scan` and `health` neither build a provider nor pay
the import cost. `ai.enabled: false` makes the property refuse outright.

## Verification

* 352 pytest tests (up from 252), `ruff check` clean, `mypy` clean on 68 files
* new suites: `tests/test_ai_providers.py` (26), `tests/test_auth.py` (37),
  `tests/test_cli_ai.py` (29), plus context-wiring tests in `test_managers.py`
* exercised against a local HTTP server for the paths a fake transport cannot
  prove: real `HttpxTransport` verification, a real completion round trip
  (`/api/chat` → text + token counts + stop reason), a real `401` (key redacted
  out of the error), and a refused connection (Ollama hint)
* exercised against the real macOS keychain: store → read → delete → "not found"
  (exit 44), then cleaned up
* `--dry-run login` verified and stored nothing; a login run inside a project
  left no key anywhere under the project root (asserted by test)

## Bugs found by the tests and fixed

1. `login --model` / `--base-url` rendered the *stale* config value, because the
   status panel was built from the config loaded before the patch was written —
   both are now threaded into `status()`
2. the "no OS keychain" warning was emitted twice (renderer and session), and the
   renderer's copy could not name the file; `AuthStatus.backend_location` now
   carries it and the session no longer duplicates the message
3. `credential_source` mixed a machine value with a human phrase, so
   `source == "env"` never matched — split into `credential_source` (`env` /
   backend) and `credential_label`
4. an expected keychain miss logged `command failed (44)` to the terminal;
   `CommandRunner.run(quiet=True)` now demotes expected-failure probes to debug
5. `_deep_merge` skipped `null`, so `--clear` could not clear anything
6. the `provider --list` table wrapped at 80 columns, splitting
   `ANTHROPIC_API_KEY` across lines

## Not implemented (later phases)

`review`, `fix`, `feature`, `test`, `upgrade`, `migrate`, `compatibility`,
`docs`, `release`. No stub, fake response or placeholder command exists for
these. `ai/` is used by nothing but the setup commands yet: phase 3 is what
starts sending prompts, and `AgentContext.record_ai_usage()` is already the
accounting hook it will call.
