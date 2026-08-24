# Phase 6 report

Scope delivered: `compatibility`, `docs` and `release` - the last three commands
on the roadmap.

## Implemented

| Area | Modules |
|---|---|
| Commands | `commands/compatibility.py`, `commands/docs.py`, `commands/release.py` |
| Models | `models/compatibility.py`, `models/release.py` |
| Reporting | `reporting/compatibility_view.py`, `reporting/release_view.py` |
| CLI | `cli/maintain.py` (with `upgrade` and `migrate`) |

```
rn-agent compatibility [--target VERSION] [--offline] [--no-dependencies]
rn-agent docs [--section NAME] [--output PATH] [--file F]
rn-agent release [--bump major|minor|patch] [--version X.Y.Z] [--no-changelog]
                 [--changelog-path PATH] [--force]
```

## Decisions

### compatibility

**Three statuses, and the difference between them is the product.** `OK` means a
requirement was found and this project satisfies it. `CONFLICT` means a
requirement was found and this project provably breaks it. `UNKNOWN` means no
requirement could be established - and the row says why. Unknowns are counted,
listed, and never block: they are the work to check by hand.

**The target's own metadata first.** React and Node requirements come from
`react-native@<target>`'s `peerDependencies` and `engines`. The bundled
compatibility table is the labelled fallback when the registry cannot be reached,
exactly as the health analyzers treat a missing `node_modules`.

**No invented per-series tooling numbers.** Nothing local establishes "RN 0.82
needs Gradle 8.13", so Gradle, AGP, Kotlin, `compileSdk` and the iOS deployment
target are reported as `UNKNOWN` *with the project's current value shown* and a
pointer to the migration diff, which is what actually changes them. The one
platform requirement the agent can prove - the dated Google Play `targetSdk`
policy - is used, with its source.

**A dependency conflict names its way out.** When a dependency's
`react-native` peer range excludes the target and the registry is reachable, the
report says which published version of that dependency *would* support it, or
that none does yet. That is the difference between a blocker and a to-do.

**Read-only, but it still writes its report.** `read_only = True` means
`execute()` never runs; the JSON report goes to `.rn-agent/cache`, exactly like
`health`.

### docs

**One output path, enforced.** The model may write the file the developer named
and nothing else. An edit anywhere else is refused as `docs.single-output` -
"document my project" must never become "rewrite my project".

**Update, don't replace.** When the target exists, its current content goes into
the prompt with an instruction to keep what is still accurate. A hand-written
paragraph should survive the next run.

### release

**Every place a version lives, or an honest gap.** A React Native app states its
version in `package.json`, `android/app/build.gradle` (`versionName` and
`versionCode`) and the Xcode project (`MARKETING_VERSION`,
`CURRENT_PROJECT_VERSION`). All of them are found by parsing the real files, all
of them are shown before anything is written, and a field that is *not* found is
reported as a note - because "Android was left behind" is the bug this command
exists to prevent.

**Deterministic first, AI only for prose.** The version arithmetic, the file
edits, the commit list and the blockers are all deterministic. The model is asked
for one thing - the changelog - and when it is unavailable the commit subjects are
used and the report labels the source (`model` or `commits`). Nobody signs off
release notes without knowing which.

**Blockers stop the release.** A dirty tree, no commits since the previous tag,
or a critical finding in the stored health report block it and exit 1 with
nothing written. `--force` proceeds and still prints them. The health report is
*read*, never re-run: `release` does not get to decide that your project is
healthy.

**The agent does not touch git history.** `GitManager` implements no commit, tag,
push or reset - and this command does not add one. The version files and the
changelog are written; `git commit`, `git tag v<version>` and `git push` are
printed as a checklist. A release is the last place to hand a model-adjacent
command write access to your history. A test asserts no `git tag`/`commit`/`push`
appears in the runner's history.

**Read back before claiming success.** `validate()` re-reads every file it wrote
and confirms the new version is actually present, listing anything that did not
land. A release that half-applied is a broken build, and it should not exit 0.

## Verification

* `tests/test_compatibility.py` (14), `tests/test_release.py` (21, covering both
  `docs` and `release`), plus `tests/test_cli_commands.py` (32) across all nine
  new commands
* compatibility: satisfied project ready; a React version outside the
  requirement conflicting; an old Node conflicting with the requirement quoted;
  an unreachable registry falling back to the bundled table *and saying so*;
  `--offline` making zero requests; Gradle/AGP unknown rather than invented; a
  dependency conflict naming the version that would work; an undecidable peer
  range staying unknown and not changing the exit code; `--no-dependencies`
* docs: the named file written and updated in place, an off-target edit refused,
  an unknown section refused before any model call, dry-run writing nothing
* release: patch/minor/major arithmetic; an explicit version; a bad version
  refused; Android and iOS fields updated *and* reported when absent; the
  changelog prepended with existing content preserved; the model source vs the
  commit-subject fallback; a dirty tree blocking; `--force`; critical health
  findings blocking; no commits blocking; the checklist naming `git tag` while
  the runner history contains no git write; dry-run
* exercised end to end through the installed console script on a synthetic app:
  `compatibility --offline`, `upgrade --offline`, `release --dry-run` and a real
  `release --bump patch` that updated all five version fields, wrote backups, and
  was confirmed by reading the files back

## Bugs found by the tests and fixed

1. `release` computed the next version from the scanned context, which could be
   stale after an earlier bump in the same session; it now re-reads
   `package.json`
2. the compatibility target could be `None` while the report claimed a source,
   producing "compatible with nothing"; the resolution now records
   `target_source` explicitly, including "no target could be resolved"
3. `docs` reported success when the model returned an empty file; the output is
   now read back and an empty write is a note plus a non-zero exit

## Roadmap

Every command on the phase 1-6 roadmap is implemented. No stub, fake response or
placeholder command exists anywhere in the tree.
