# Phase 4 report

Scope delivered: `rn-agent upgrade` - risk-ranked dependency upgrades with peer
and native analysis.

## Implemented

| Area | Modules |
|---|---|
| Registry | `upgrade/registry.py` (`NpmRegistry`, `Packument`, `PackageVersion`) |
| Planning | `upgrade/planner.py` (`plan_upgrades`) |
| Command | `commands/upgrade.py` |
| Models | `models/upgrade.py` (`UpgradeCandidate`, `UpgradePlan`, `ChangeKind`) |
| Reporting | `reporting/upgrade_view.py` |

```
rn-agent upgrade [--target patch|minor|latest] [--only PKG] [--skip PKG] [--native]
                 [--no-install] [--check STEP] [--no-check] [--offline]
```

## Decisions

**No AI.** An upgrade decision is arithmetic over version ranges plus knowledge
of what native code costs. A model would add uncertainty to something that is
already decidable, so `upgrade` never builds a provider. The same plan comes out
for the same inputs, every time.

**The abbreviated packument.** The registry is asked for
`application/vnd.npm.install-v1+json` - what npm itself installs from. It is an
order of magnitude smaller than the full document and still carries the two
things that decide an upgrade: each version's `peerDependencies` and its
`engines`. Scoped names are encoded as `@scope%2Fname`, which is what the
registry expects.

**React Native is not a dependency bump.** `react-native` and `react` are always
blocked here, with a reason pointing at `rn-agent migrate`. Moving them means
template diffs, pods and a rebuild - a different command with a branch and a
rollback, not a range rewrite.

**Peer conflicts block; undecidable ranges do not.** For each candidate target,
that version's own `peerDependencies` are checked against what the project has
(installed version, else the declared range's floor). `satisfies() is False` is a
conflict and blocks; `None` - `workspace:*`, a git URL, a dist-tag - is a note.
This is why the peer check produces no invented conflicts.

**Native code is never a quiet upgrade.** A package that ships `android/` or
`ios/` needs a pod install and a rebuild, so it is at least one risk level above
the same jump in a JS-only package, and it is excluded unless `--native` is
passed. It is still listed and analysed, because "why did it not upgrade X" is
the question this command exists to answer.

**Unreachable is a reported state, not an exception.** One transport failure sets
`available = False` and every later lookup returns `None` - there is no point
asking a dead host 100 more times. The plan then reports `registry_available:
false`, invents no target versions, and still shows installed-versus-declared
drift, which is a real fact. The command exits 1 so a script notices.

**`package.json` goes through the same envelope as any other write.** It is
exactly the file `rules.yaml` forbids touching, so this command - and only this
command - passes `allow_dependencies=True`. The confirmation gate, the backup and
the rollback still apply, and the file's existing indentation is preserved rather
than reformatted.

**Rollback tells you about the lockfile.** If validation fails, `package.json` is
restored - but `node_modules` may already have moved, so the closing line names
the install command to run. Restoring a manifest silently would leave a project
in a state neither file agrees on.

## Verification

* `tests/test_upgrade.py` (20 tests), all offline: a fake transport serves
  abbreviated packuments and records the URLs and headers it was asked for
* covered: the three policies picking three different targets from one
  packument; pre-releases never selected; `react`/`react-native` blocked with the
  migrate hint; a peer conflict blocking and an undecidable range not;
  native exclusion and its higher risk; `--only`/`--skip`; an up-to-date package
  reporting `none`; scoped-name encoding; the `Accept` header; per-process
  caching; an unreachable registry (no invented targets, exit 1)
* the write path is asserted on bytes: the declared operator (`^`/`~`) survives
  the rewrite, and a failed typecheck restores the original `package.json`
  exactly
* exercised end to end through the console script on a synthetic app:
  `rn-agent upgrade --offline` reports drift and touches nothing

## Bugs found by the tests and fixed

1. `Packument.newest()` preferred the `latest` dist-tag even when it pointed at a
   pre-release; it now falls back to the newest stable version
2. peer checks silently skipped `react` when `node_modules` was absent, so a
   real conflict looked like "no conflict"; the declared range's floor is used
   instead
3. `_target()` returned the newest version when the policy admitted nothing
   newer, which reported a "change" of a package to itself - it now resolves to
   `ChangeKind.NONE`

## Not implemented

Lockfile updates. The agent rewrites `package.json` and runs the project's own
package manager; it does not attempt to edit `yarn.lock` or
`package-lock.json` - those are generated files, and hand-editing them is exactly
the sort of thing this project refuses to do.
