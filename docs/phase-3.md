# Phase 3 report

Scope delivered: the AI work layer plus the four daily-development commands -
`review`, `fix`, `feature`, `test`.

## Implemented

| Area | Modules |
|---|---|
| Transport | `net/http.py` (moved out of `ai/`: three subsystems speak HTTP now) |
| AI layer | `agents/rules.py`, `agents/context_builder.py`, `agents/prompts.py`, `agents/output.py`, `agents/engine.py`, `agents/apply.py`, `agents/workflow.py` |
| Validation | `validation/runner.py`, `models/validation.py` |
| Commands | `commands/review.py`, `commands/fix.py`, `commands/feature.py`, `commands/test.py` |
| Models | `models/proposal.py` (`FileEdit`, `Proposal`, `ProposalSet`, `EditRunReport`), `models/review.py` |
| Reporting | `reporting/change_view.py` (shared by every write-command), `reporting/review_view.py` |
| CLI | `cli/develop.py`, `cli/runtime.py` (context/exit/JSON plumbing shared with `app.py`) |
| Errors | `TransportError` (11), `ModelOutputError` (12) |

Commands:

```
rn-agent review [--file F] [--changed] [--area A] [--about TEXT] [--limit N] [--fail-under N]
rn-agent fix [--issue ID] [--file F] [--about TEXT] [--changed] [--check STEP] [--no-check]
             [--allow-native] [--allow-deps] [--keep]
rn-agent feature "DESCRIPTION" [--file F] [--allow-deps] [--check STEP] [--no-check] [--keep]
rn-agent test [TARGET ...] [--framework NAME] [--no-run] [--keep]
```

## Decisions

**Whole files, not patches.** A model returns the complete content of every file
it changes. A hunk that fails to apply leaves a half-edited file; a full
replacement either lands or does not, and `FileManager` has the previous bytes.
The one place the agent *does* apply hunks is `migrate`, against upstream
diffs - and there a mismatch is a reported conflict, never a fuzzy apply.

**`rules.yaml` is enforcement, not decoration.** The rules go into the prompt
*and* into `EditApplier.screen()`. A model that ignores "do not add
dependencies" still cannot write `package.json`: the edit is refused by path
before the safety gate is even reached. Lockfiles are refused outright - they
are produced by a package manager, never edited by hand.

**Apply, prove, undo.** `fix`, `feature` and `test` apply the change, then run
the project's own checks (`tsc`, `eslint`, the test script), then roll the whole
thing back if the checks fail. `--keep` opts out. This ordering is only safe
because every write went through `FileManager` with a backup, so `rollback()`
restores byte-for-byte - which the tests assert by comparing file contents, not
exit codes.

**"Not validated" is not "passed".** `ValidationReport.ok` is true when nothing
failed; `proved` is true only when something actually ran. A project with no
`tsc` and no test script gets `SKIP` with the reason, and the render says the
change was *not* validated rather than implying success.

**A review may only speak about what it was shown.** Findings whose `file` was
never sent to the model are dropped and counted in `notes`. The review score
reuses the health penalty table, so 93/100 means the same thing in both reports.

**`test` may only write tests.** A proposal touching anything that is not a
`.test.`/`.spec.`/`__tests__` path is refused: "write me tests" must never turn
into a rewrite of the code under test. Generated tests that fail are rolled back,
because a red test nobody trusts is worse than no test.

**One model call path.** `AIEngine` selects the per-task model
(`ai.models.<task>`), records usage in the `ai_usage` table, logs the redacted
reply, refuses a truncated answer instead of parsing half a file, and retries
**once** on an unparsable reply - handing the parse error back to the model. Two
failures is an error, not a loop: the developer is paying per token.

**Secrets never reach a prompt.** `ContextBuilder` runs every candidate through
`SafetyManager.filter_context_files` and reports what it refused, what it
truncated (`context.max_file_kb`) and what did not fit the budget
(`ai.max_context_files`, `ai.max_context_tokens`). `--verbose` prints the list,
so a developer can audit exactly which bytes left their machine.

**The transport moved.** `ai/http.py` became `net/http.py` because the npm
registry (phase 4) and the upstream diffs (phase 5) need the same seam.
`TransportError` is no longer a `ProviderError`; providers translate it, so an
AI network failure still reports exit code 10 while a registry failure reports
11.

## Verification

* 573 pytest tests (up from 352), `ruff check` clean, `mypy` clean on 113 files
* new suites: `test_agents.py` (44), `test_validation.py` (17),
  `test_review.py` (14), `test_fix.py` (16), `test_feature.py` (18),
  `test_cli_commands.py` (32)
* the rollback path is asserted on bytes: after a failed `tsc`, the target file
  is compared with its original content, not just the exit code
* validation runs the project's *real* binaries - the tests install a
  `node_modules/.bin/tsc` (and `jest`) that exits 0 or 2 on demand, so the
  runner's skip/pass/fail branches are exercised through `subprocess`
* no test performs network I/O: providers get a fake transport, and the
  `conftest` guard makes `httpx` raise

## Bugs found by the tests and fixed

1. `ai/__init__` re-exported the transport after the move, leaving two import
   paths for one seam - removed, and `auth/session.py` repointed
2. `EditApplier` reached into `FileManager._backup` to delete a file; deletes
   now belong to `FileManager.delete()`, the only writer, and roll back like any
   other change
3. `AIEngine._decode` used PEP 695 generics, which are Python 3.12 syntax - the
   project targets 3.11
4. peer-conflict detection returned "unknown" for `react` whenever
   `node_modules` was missing; it now falls back to the declared range's floor,
   which is enough to prove a conflict
5. the review score was computed before out-of-area findings were dropped, so
   `--area` changed the visible list but not the number

## Not implemented (later phases)

Nothing from this phase. `upgrade` (phase 4), `migrate` (phase 5) and
`compatibility`/`docs`/`release` (phase 6) followed in the same pass; see their
reports.
