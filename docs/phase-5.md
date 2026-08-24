# Phase 5 report

Scope delivered: `rn-agent migrate` - React Native version migration from the
upstream template diff, with validation, one AI repair round and rollback.

## Implemented

| Area | Modules |
|---|---|
| Sources | `migration/sources.py` (`DiffSource`: fetch + cache the upstream diff) |
| Diff engine | `migration/diff.py` (parse, strict apply, template rename) |
| Local rules | `migration/rules.py` (`set_property`, `replace`, `ensure_line`) |
| Planning | `migration/planner.py` (`build_plan`) |
| History | `migration/history.py` (`.rn-agent/migration-history.json`) |
| Command | `commands/migrate.py` |
| Models | `models/migration.py` (`MigrationStep`, `MigrationPlan`, `MigrationOutcome`) |
| Reporting | `reporting/migrate_view.py` |

```
rn-agent migrate [--to VERSION] [--kind dependency|android|ios|javascript|manual]
                 [--skip-native] [--no-install] [--build] [--no-ai] [--offline]
                 [--allow-dirty] [--no-branch] [--rules-dir DIR]
```

## Decisions

**Three sources, in descending order of authority.** (1) The target's own
metadata - `react-native@0.82.1`'s `peerDependencies` and `engines` state which
React and which Node it wants, which is a fact about that version rather than a
table someone must remember to update. (2) The upstream template diff, published
by `rn-diff-purge` - the same data the Upgrade Helper website renders. (3) Local
rule files. Anything none of the three can decide becomes a `MANUAL` step with
the reason.

**Strict hunks, or a conflict.** A hunk applies only when the lines it claims to
remove and the context around them match the file as it is now. The stated line
number is a hint - real projects have drifted - so the matcher searches outward
from it and requires **exactly one** match. Zero matches or two is a conflict.
There is no fuzzy mode and no partial write: a half-applied `.pbxproj` opens in
nothing and reverts cleanly from nothing.

**"Already applied" is checked before "applies cleanly".** A hunk that only adds
lines still matches its own context after it has been applied. Checking the
result first is what stops a re-run from duplicating the addition - the bug the
tests caught.

**The template placeholder is renamed or refused.** Upstream diffs call the app
`RnDiffApp`. That is mapped to the project's real name (the iOS project name,
then the package name) in both paths and hunk content. When the mapping cannot be
made unambiguously, the step becomes `MANUAL` - writing `RnDiffApp` into someone's
Xcode project is worse than admitting the limit.

**A file the project does not have is a task, not an error.** Real apps customise
or delete template files. Those steps are reported as manual with the hunk
attached, which is the difference between a migration tool and a diff dump.

**Branch, then write.** `git.require_repository()`, then `require_clean()` unless
`--allow-dirty`, then a branch from `migration.branch_prefix`. The rollback
restores what the agent wrote - not what you had half-finished - so a dirty tree
is refused rather than absorbed.

**One AI repair round, then rollback.** When validation fails, the failures are
handed to the model once (`migration.use_ai_for_errors`), the proposal is screened
and applied, and the checks run again. If it still fails, everything the agent
wrote is rolled back and the attempt is recorded as rolled back. One round: a
model that cannot fix a build from the error text will not fix it on the fifth
attempt, and each attempt costs the developer tokens.

**Conflicts do not fail the command.** Exit code 1 means "rolled back" or
"validation failed". Steps that need a human are a warning with their own section
in the report, because a migration that applied 12 of 14 steps and named the
other two is a success worth committing.

**The diff cache lives with the project.** Diffs are cached under
`.rn-agent/cache/migrations/<from>..<to>.diff` and preferred over the network on
a re-run. The repository-level `templates/` placeholder from phase 1 was removed:
the implemented design caches per project, so a directory whose README promised
something else was dead weight.

**Local rules stay empty on purpose.** `migration-rules/` ships no files. The
loader is real and tested, an unknown action is skipped with a warning rather
than approximated, and rules are version-pinned with a `source` - but the agent
does not invent migration steps it cannot attribute.

## Verification

* `tests/test_migrate.py` (23 tests), all offline: one fake transport serves both
  the registry document and the diff text
* diff engine: a matching hunk applies; a drifted hunk is a conflict and the file
  is byte-identical afterwards; an already-applied hunk is recognised; an
  ambiguous context is a conflict; a hunk still applies when the file drifted
  *around* it; the placeholder is mapped, and refused when the name is unknown
* rules: `set_property` updates in place and is idempotent, a missing file is
  reported, an unknown action is skipped, a mismatched version pair is ignored,
  an empty directory is fine
* sources: the diff is fetched once and cached, a 404 is reported not raised,
  `--offline` performs no request
* the command: a full migration (branch created, `package.json` updated, native
  hunk applied, history written); a drifted file becoming a conflict while the
  rest proceeds; `--skip-native`; the offline table used and labelled; a failed
  build triggering one AI repair round that *fixes* it; a failed build whose
  repair does not help, ending in a full rollback verified on bytes and recorded
  as `rolled_back`; a dirty tree refused; an older target refused; dry-run
  creating no branch, no file and no history

## Bugs found by the tests and fixed

1. an append-only hunk re-applied on a second run, duplicating its added line -
   "already applied" is now checked first
2. `MigrationStep.diff` stored the hunk body without its `@@` header, so the
   applier could not re-parse what the planner had recorded; `Hunk.header` is now
   kept and the stored fragment is a valid patch
3. the validation step list ran `pod install` whenever iOS was present, which
   fails on any project whose Podfile the agent cannot evaluate; it now honours
   `migration.run_pod_install`, and the tests turn it off rather than testing
   CocoaPods
4. `DiffSource` was constructed with a `dataclasses.MISSING` default leaking
   through, which mypy caught before any test did

## Not implemented

Automatic `pod install` repair and Xcode project *structure* changes (new build
phases, new targets). Those arrive as `MANUAL` steps with the upstream hunk
attached, because applying them blind is how an unopenable project happens.
