# Phase 8 report

Scope delivered: Cursor, integrated twice — as a completion backend that cannot
touch the tree, and as an agent that may, under this project's rules.

## Implemented

| Area | Modules |
|---|---|
| Provider | `ai/cursor.py` (`CursorProvider`: the local CLI driven headlessly, `--mode ask`, never `--force`), registered in `ai/registry.py` with aliases `cursor-agent`/`cursor-cli`/`composer` |
| Delegation | `agents/cursor_agent.py` (`CursorAgentRunner`: rules → Cursor `permissions.deny`, merge/restore of `.cursor/cli.json`, post-hoc diff audit), `commands/delegate.py` |
| Auth | `AuthMethod.TOOL` + `auth/methods.py::ToolAuthenticator` — a tool that holds its own session, with an optional key for CI |
| Contract | `AIProvider.requires_model` (an agent CLI has an account default; an HTTP API does not) |
| CLI | `rn-agent delegate TASK` in `cli/develop.py`, `/delegate` in `tui/router.py` |
| Wiring | `core/context.py` and `tui/session.py` pass `workspace`; `auth/manager.py` capability row |

```
rn-agent delegate "extract the header into a component"
rn-agent delegate "bump minSdk to 24" --allow-native
rn-agent --dry-run delegate "..."      # task + deny list, nothing runs
```

## Decisions

**Cursor publishes no chat-completions endpoint**, so the provider is the one in
the registry that does not speak HTTP. It overrides `_request` — the single seam
the base routes everything through — and therefore keeps the model guard, the
logging and the failure mapping that every HTTP provider has. The returned
`HttpResponse` is a status plus a parsed body, which is all `_failure` and
`_parse_completion` ever read.

**No SDK dependency.** `cursor-sdk` on PyPI is official (Cursor's own PyPI
organisation, v1.0.28), but every wheel bundles a ~50–60 MB proprietary bridge
binary, is public beta, and wraps the same documented CLI and REST surface. This
repo ships an npm wrapper around a private venv and holds to one HTTP seam, so
that is a large cost for no capability. If the SDK ever becomes the only route to
something, it belongs behind an extra, not in the default install.

**The provider is a brain, the delegate command is a pair of hands, and the
difference is a flag.** Cursor's own docs are explicit that print mode has access
to write and shell tools and that `--force` is what auto-approves them. So
`CursorProvider` passes `--mode ask` and never `--force`: proposals come back as
text and are applied through `FileManager` with backups, rules and rollback, like
every other provider. `delegate` passes `--force` deliberately, because letting
Cursor edit is the entire point of that command.

**A clean git tree is the delegate command's backup strategy**, and it is stated
as such rather than implied. `FileManager` cannot back up edits that never pass
through it; if HEAD matches the tree beforehand, `git restore .` is an exact undo.
`--allow-dirty` opts out and the report then stops claiming recoverability.
Nothing destructive ever runs — the restore command is printed, exactly as
`GitManager` does everywhere else.

**Rules are enforced by Cursor, not just reported by us.** `.rn-agent/rules.yaml`
is translated into `permissions.deny` in Cursor's own `.cursor/cli.json` before
the agent starts, so a forbidden write is refused at source. Deny beats allow and
beats `--force` in Cursor's model, which is what makes `--force` safe to pass. The
developer's own `allow` list is merged rather than replaced, and the file is
restored — including removing an empty `.cursor/` we created.

**rn-agent never reads Cursor's credential.** `ToolAuthenticator` exists because
"the tool is signed in" is a real, third mechanism alongside a key and OAuth.
Running `cursor-agent` through its documented interface is not the same act as
reading the secret it stores — one is supported, the other is what
`docs/authentication.md` forbids.

## Verification

* 762 pytest tests (up from 726), `ruff check src tests` clean, `mypy` clean on
  136 source files
* new suite: `test_cursor.py` (34)
* **the read-only claim is proved, not asserted**: the test stub writes a file
  only when `--force` is present, and the provider test then checks the project
  directory is byte-for-byte unchanged. The stub's write branch was exercised
  separately to confirm the test is not vacuous
* the whole delegate loop was driven end to end against a stub that really edits
  files: clean-tree refusal, branch creation, deny-list write and restore, the
  violation path (native file → exit 1 → restore guidance), and `--allow-native`
  permitting the same edit at exit 0

## Bugs found by the tests and fixed

1. Overriding `_request` dropped the base class's `if not response.ok` guard, so
   a CLI failure — including "not logged in" — was parsed as if it were the
   model's answer. The guard is now re-applied in the override.
2. `CursorProvider` was refused for having no model, because the base demanded
   one. Cursor has an account default and `--list-models` is a real catalogue, so
   hard-coding an id would have been a guess; `requires_model = False` says it
   properly.
3. `delegate` passed `ai.model` to Cursor even when the configured provider was
   Anthropic, which Cursor would reject. The configured model is now forwarded
   only when Cursor *is* the configured provider.
4. The clean-tree guard counted rn-agent's own untracked `.rn-agent/` as the
   developer's work, so any project without that `.gitignore` line could never
   delegate.
5. `restore_permissions` left an empty `.cursor/` behind — an untracked directory
   the developer never created.
6. `login cursor --stdin` silently stored nothing: `_read_secret` returned early
   for any provider with `requires_credential = False`, which is right for a
   *prompt* but wrong for an explicitly supplied key — it made the documented CI
   path (`CURSOR_API_KEY`) a no-op. An explicit `--api-key`/`--stdin` is now
   honoured whatever the provider requires.

## Not implemented

**Cloud Agents.** `POST https://api.cursor.com/v1/agents` runs an agent against a
*GitHub repository* in Cursor's cloud and returns a branch or PR, which is a
different lifecycle from editing the local tree (async, remote, needs repo
access). It is a genuinely useful fit for long `upgrade`/`migrate` jobs and it
would go through `JsonTransport` with no new dependency, but it is a separate
command with its own polling and reporting, and none of it is claimed anywhere in
the UI today.

**Streaming progress.** `--output-format stream-json` is parsed only far enough to
tolerate progress lines before the result object. A live tool-by-tool view in the
terminal would be a real improvement for long runs; `delegate` currently prints
the agent's summary once it finishes.
