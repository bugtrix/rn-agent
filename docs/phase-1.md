# Phase 1 report

Scope delivered: foundation + `scan` + `health`, per the build plan's §40/§43.

## Implemented

| Area | Modules |
|---|---|
| CLI | `cli/app.py` (Typer), `cli/ui.py` (Rich primitives) |
| Core | `core/context.py` (shared brain), `core/command.py` (4-phase contract), `core/registry.py`, `core/config.py`, `core/logging.py`, `core/paths.py` |
| Project | `project/detector.py`, `packages.py`, `android.py`, `ios.py`, `architecture.py`, `scanner.py` |
| Analyzers | `analyzers/{project,rn,js,android,ios}_analyzer.py` |
| Managers | `git/manager.py`, `filesystem/manager.py`, `filesystem/walker.py`, `runner/command_runner.py`, `safety/manager.py` |
| Knowledge | `knowledge/store.py` (SQLite), `knowledge/data.py` + packaged YAML |
| Models | `models/{project,health,config,changes}.py` (pydantic) |
| Reporting | `reporting/{scan_view,health_view}.py` |
| Utils | `utils/semver.py`, `io.py`, `redaction.py` |
| Packaging | `pyproject.toml` (console script), `npm/` wrapper with private venv |

## Verification

* 252 pytest tests, `ruff check` clean, `mypy` clean on 54 source files
* validated against two real production apps (RN 0.82.1 with `node_modules`,
  RN 0.79.1 without) in `--dry-run`, leaving no trace in either
* npm wrapper installed globally from a packed tarball and executed

## Bugs found by the tests and fixed

1. `coerce()` turned `gradle-7.6-all.zip` into `7.6.0-all.zip`
2. `">= 20.19.4"` (space after operator) collapsed to an exact match, wrongly
   failing Node 22
3. `*` / `x` ranges were treated as undecidable instead of "any version",
   inflating peer-dependency conflicts from 8 to 21 on a real project
4. `git status --porcelain` output was `.strip()`ed, turning unstaged
   modifications into staged ones
5. `git check-ignore` was asked about a directory that does not exist yet, so
   `.rn-agent/cache/` never looked ignored
6. yarn.lock resolution returned the first matching entry, reporting
   `react-native@*` → 0.85.2 for a project pinned to 0.79.1
7. `health` could not write its log before the first `scan` created `.rn-agent`

## Not implemented (later phases)

`login`, `logout`, `whoami`, `provider`, `model`, `review`, `fix`, `feature`,
`test`, `upgrade`, `migrate`, `compatibility`, `docs`, `release`. No stub, fake
response or placeholder command exists for these.
