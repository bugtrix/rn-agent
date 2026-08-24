# rn-agent

One AI-powered React Native agent with many commands and a **shared project brain**.

`rn-agent` scans your React Native project once, then every command works from
that same context: your architecture, your dependencies, your native config,
your git state. It is built for real production apps — the ones with 100+
dependencies, 35 native modules, a Gradle `ext` block and a Podfile you did not
write yourself.

> **Status: complete through phase 6.** Every command on the roadmap is
> implemented, tested and usable today: `scan`, `health`, `info`, the AI setup
> commands (`login`, `logout`, `whoami`, `provider`, `model`), the development
> commands (`review`, `fix`, `feature`, `test`), and the maintenance commands
> (`upgrade`, `migrate`, `compatibility`, `docs`, `release`). Nothing in this
> repository fakes an AI response or pretends a command exists before it works.

---

## Quick start

```bash
npm install -g rn-agent          # Node wrapper, owns a private Python runtime

cd my-react-native-project
rn-agent scan                    # build the shared project brain
rn-agent health                  # real diagnostics: RN, JS, Android, iOS
```

Nothing is sent anywhere. `scan` and `health` are 100 % local and deterministic —
zero AI calls, zero network requests.

Ready for the AI half? Connect your own account, on your own key:

```bash
rn-agent login anthropic         # or openai, or ollama for a local model
rn-agent whoami
```

---

## Contents

1. [What is rn-agent?](#what-is-rn-agent)
2. [Requirements](#requirements)
3. [Installation](#installation)
4. [Commands](#commands)
5. [rn-agent scan](#rn-agent-scan)
6. [rn-agent health](#rn-agent-health)
7. [Setting up AI](#setting-up-ai)
8. [Daily development: review, fix, feature, test](#daily-development)
9. [Maintenance: upgrade, compatibility, migrate](#maintenance)
10. [Shipping: docs and release](#shipping)
11. [Configuration](#configuration)
12. [The shared project brain](#the-shared-project-brain)
13. [Architecture](#architecture)
14. [Security](#security)
15. [Safety](#safety)
16. [Logs and state](#logs-and-state)
17. [Development](#development)
18. [Testing](#testing)
19. [Troubleshooting](#troubleshooting)
20. [Roadmap](#roadmap)

---

## What is rn-agent?

Most React Native problems are not code problems. They are *configuration*
problems: an AGP version your Gradle wrapper cannot run, a `Podfile.lock`
pinning last month's React Native, a native module whose iOS permission string
is missing so the app dies on first use, two copies of React in `node_modules`.

`rn-agent` finds those deterministically, explains them with evidence, and tells
you what to change. AI is reserved for the parts that genuinely need judgement
(complex migrations, unfamiliar build errors) and is always optional, visible
and paid for by your own provider account.

Design rules the implementation actually follows:

| Rule | How it is enforced |
|---|---|
| One agent, many commands | Every command implements `AgentCommand` and receives one `AgentContext` |
| Facts over guesses | Compatibility is read from *your* `node_modules/react-native/package.json`, not a hard-coded table |
| Never fabricate | A check whose facts are unavailable reports `SKIP`, never a scary warning |
| Deterministic first | `scan` and `health` make **zero** AI calls |
| Safe by default | `--dry-run` everywhere; the writer refuses paths outside your project |
| No secret leakage | `.env`, keystores, provisioning profiles and `google-services.json` are never read into context |

---

## Requirements

* macOS, Linux or Windows
* **Node.js 18+** (for the `npm install -g` wrapper)
* **Python 3.11+** (the agent itself; the wrapper finds or tells you how to get it)
* A React Native project (bare or Expo with native folders)

Optional, used when present: `git`, `yarn`/`pnpm`/`bun`, `java`, `pod`,
`xcodebuild`, `watchman`.

---

## Installation

### Via npm (recommended for React Native developers)

```bash
npm install -g rn-agent
rn-agent --version
```

The wrapper creates a private virtual environment (`~/Library/Caches/rn-agent`
on macOS, `~/.cache/rn-agent` on Linux, `%LOCALAPPDATA%\rn-agent` on Windows)
and installs the Python agent there. Your system Python is never modified.

Point it at a specific interpreter if you have several:

```bash
RN_AGENT_PYTHON=/opt/homebrew/bin/python3.12 npm install -g rn-agent
```

### Via pipx / pip (Python users)

```bash
pipx install rn-agent
# or
pip install rn-agent
```

### From source

```bash
git clone https://github.com/rn-agent/rn-agent.git
cd rn-agent
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
rn-agent --version
```

---

## Commands

| Command | Status | What it does |
|---|---|---|
| `rn-agent scan` | ✅ Phase 1 | Detect the project and build the shared context |
| `rn-agent health` | ✅ Phase 1 | Diagnose RN, JavaScript, Android and iOS configuration |
| `rn-agent info` | ✅ Phase 1 | Show where state lives and what has been scanned |
| `rn-agent login` / `logout` / `whoami` | ✅ Phase 2 | Connect *your* AI account; the key goes to your OS keychain |
| `rn-agent provider` / `model` | ✅ Phase 2 | Choose the provider, the model, and per-task models |
| `rn-agent review` | ✅ Phase 3 | Analyse components, hooks, state and performance with your model |
| `rn-agent fix` | ✅ Phase 3 | Fix what `health`/`review` reported, then prove the project still builds |
| `rn-agent feature` | ✅ Phase 3 | Implement a feature following your existing architecture |
| `rn-agent test` | ✅ Phase 3 | Generate tests for your code and run them |
| `rn-agent upgrade` | ✅ Phase 4 | Risk-ranked dependency upgrades, with peer and native analysis |
| `rn-agent migrate` | ✅ Phase 5 | React Native version migration from the upstream template diff |
| `rn-agent compatibility` | ✅ Phase 6 | Check the project against a React Native version before migrating |
| `rn-agent docs` | ✅ Phase 6 | Write project documentation from the scanned facts |
| `rn-agent release` | ✅ Phase 6 | Bump every version an app carries, and write the changelog |

Global flags:

```
-C, --path DIR    run against another project directory
    --dry-run     never write anything; show what would happen
-y, --yes         answer yes to confirmation prompts
-v, --verbose     evidence, sources and passed/skipped checks
    --json        machine-readable output
    --version
```

---

## rn-agent scan

```bash
rn-agent scan              # full scan, writes .rn-agent/
rn-agent scan --no-tools   # skip node/java/pod version probes (faster)
rn-agent scan --show       # print the stored context without rescanning
rn-agent --json scan       # the whole context as JSON
rn-agent --dry-run scan    # inspect a project without creating .rn-agent/
```

What it detects:

* **React Native**: installed vs declared version (and *where* the number came
  from — `node_modules`, the lockfile, or `package.json`), React, `@types/react`,
  TypeScript, Expo, Hermes, New Architecture, Metro/Babel/tsconfig presence
* **Package manager**: npm / yarn / pnpm / bun, from `packageManager` or the
  lockfile, plus a warning when several lockfiles exist
* **Dependencies**: every declared package, its installed version, its
  `peerDependencies`, and whether it ships native code (from the filesystem when
  `node_modules` exists, heuristically otherwise — and it tells you which)
* **Android**: Gradle wrapper, AGP (or "not pinned"), Kotlin, NDK, build tools,
  `compileSdk`/`targetSdk`/`minSdk` (resolving `rootProject.ext.*` indirection),
  Java compatibility, namespace, flavors, signing config names, permissions
* **iOS**: deployment target (pbxproj, then Podfile), CocoaPods version from
  `Podfile.lock`, the React-Core pod version, `use_frameworks!` linkage, bundle
  id, privacy manifest, `NS*UsageDescription` keys, entitlements, AppDelegate
  language
* **Architecture** (inferred, never imposed): state management, navigation, API
  layer, data fetching, styling, forms, validation, testing, i18n, analytics,
  plus your directory layout and conventions
* **Git**: branch, dirty state, last commit, whether `.rn-agent` is ignored
* **Toolchain**: node, npm/yarn/pnpm, java, cocoapods, xcodebuild, watchman

Example (real project, 108 dependencies):

```
╭─────────────────────────────╮
│ subone  React Native 0.82.1 │
╰─────────────────────────────╯

Project
  root               ~/work/subone-mobile
  package manager    yarn (yarn.lock) 2 lockfiles!
  platforms          android, ios

Architecture (inferred)
  source root        app
  state              redux-toolkit, redux, redux-saga
  navigation         react-navigation, react-native-bottom-tabs
  api layer          apisauce, axios
  testing            jest, react-test-renderer
```

Because the architecture is *inferred*, later phases must follow it: a project
on Redux Saga will not be handed React Query.

---

## rn-agent health

```bash
rn-agent health                     # deterministic diagnostics
rn-agent health --verbose           # evidence, sources and every passed check
rn-agent health --deep              # also run tsc --noEmit and eslint
rn-agent health --area android      # one area: project|react-native|javascript|android|ios
rn-agent health --refresh           # rescan first
rn-agent health --fail-under 80     # non-zero exit for CI gating
rn-agent --json health              # the full report as JSON
```

Exit code is `1` when any **critical** issue is found (or the score is below
`--fail-under`), so it drops straight into CI.

### What it checks

**Project** — lockfile sanity, `node_modules` presence, git state, whether
`.rn-agent` is ignored, and Node against react-native's own `engines.node`.

**React Native** — version resolution and drift from the declared range, React
against react-native's own `peerDependencies`, `@types/react` alignment, Hermes,
New Architecture consistency **across both platforms**, Metro config, Babel
config (legacy preset, missing Reanimated plugin).

**JavaScript** — TypeScript and `tsconfig` (`extends`, `strict`), ESLint,
duplicate React/React Native copies (runtime = critical, `@types/*` = high),
unsatisfied `peerDependencies` read from real package metadata, deprecated or
renamed packages, packages declared twice. With `--deep`: `tsc --noEmit` and
`eslint`.

**Android** — Gradle wrapper, AGP↔Gradle compatibility, JDK 17 for AGP 8,
Kotlin, `compileSdk`/`targetSdk`/`minSdk` coherence, the Google Play
`targetSdk` deadline (dated policy data, only enforced once the date has
passed), `android:exported` on components with intent-filters, and permissions
required by the native modules you actually installed.

**iOS** — Xcode project and workspace, deployment target agreement between
Podfile and pbxproj, `Podfile.lock` presence and whether its React-Core version
matches `package.json`, Pods installed, privacy manifest, `NS*UsageDescription`
keys required by installed modules, push entitlements.

### Scoring

```
Health Score: 76/100  good · React Native 0.82.1

Summary
  checks run         34
  passed             26
  critical           1
  high               3
```

`100 −` (10 × critical + 5 × high + 2 × medium + 1 × low), floored at 0. Every
lost point maps to a listed check — there is no hidden weighting, and a check
that could not gather facts costs nothing.

---

## Setting up AI

AI is **opt-in, per developer, on your own account**. The agent never proxies a
request through a vendor of its own, ships no shared key, and has no code path
that talks to a model without one.

```bash
rn-agent provider --list          # what is supported, and what you already have
rn-agent login anthropic          # prompts for the key, verifies it, stores it
rn-agent whoami                   # provider, model, and where the key came from
```

### Supported providers

| Provider | `login` name | Key from | Environment variable |
|---|---|---|---|
| Anthropic Claude | `anthropic` (alias `claude`) | <https://console.anthropic.com/settings/keys> | `ANTHROPIC_API_KEY` |
| OpenAI | `openai` (alias `gpt`) | <https://platform.openai.com/api-keys> | `OPENAI_API_KEY` |
| Ollama (local) | `ollama` (alias `local`) | no key — runs on your machine | `OLLAMA_HOST` (host, not a key) |

### Giving it the key

```bash
rn-agent login anthropic                      # hidden prompt (interactive terminal)
echo "$KEY" | rn-agent login openai --stdin   # CI, or anything scripted
rn-agent login openai --api-key sk-...        # last resort: argv is visible to `ps`
rn-agent login ollama                         # nothing to type; verifies the server
```

`login` **verifies before it stores**: the key is checked against the provider's
own model endpoint, and a rejected key never reaches your keychain. `--no-verify`
skips the check when you are offline.

Where the key ends up:

| Platform | Store |
|---|---|
| macOS | login keychain (`security`) |
| Linux/BSD | Secret Service (`secret-tool`: gnome-keyring, KWallet) |
| Windows | DPAPI (user-scoped), ciphertext under `~/.config/rn-agent` |
| no keyring found | a `0600` file, and every command says so out loud |

The secret always travels on **stdin**, never in `argv`, so it cannot appear in
another user's process list. `RN_AGENT_KEYCHAIN=file` forces the file store;
`RN_AGENT_KEYCHAIN=none` disables storage entirely (environment variables only).

### Which credential wins

1. the provider's environment variable (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`)
2. the keychain entry written by `rn-agent login`

`whoami` prints which one it used, because "I exported a key" and "a key is in my
keychain" are different facts:

```
  provider           anthropic
  model              claude-sonnet-4-5
  api host           https://api.anthropic.com
  credential         ANTHROPIC_API_KEY (environment)
  key                …3f9a
  storage            keychain-macos
  ready              yes
```

`rn-agent whoami --check` asks the provider whether the key still works;
`rn-agent whoami --json` is CI-friendly and exits `10` when AI is not usable.

### Choosing models

```bash
rn-agent model --list                     # bundled suggestions for your provider
rn-agent model --list --remote            # the real catalogue, from your account
rn-agent model claude-sonnet-4-5          # the default model
rn-agent model claude-opus-4-1 --task migration   # one task only
rn-agent model --clear --task migration
```

Per-task models exist because the work differs: a migration wants your strongest
model, a review can use a cheaper one. Known tasks: `default`, `migration`,
`debugging`, `review`, `feature`, `test`, `upgrade`, `docs`.

### Scope, and turning it off

`login`, `provider` and `model` write to `~/.config/rn-agent/config.yaml` (your
preference, every project). Add `--project` to write `.rn-agent/config.yaml`
instead, which is how one app pins a different model for the whole team — still
without a credential in the repository.

```bash
rn-agent logout anthropic     # forget the stored key
rn-agent logout --all
```

Set `ai.enabled: false` in a project's config and every AI command refuses to
run there. `scan` and `health` are unaffected: they make **zero** AI calls and
never build a provider.

---

## Daily development

These four commands use your model. They all follow the same contract: you see
what is proposed, your `rules.yaml` can refuse it, the change is applied through
the same writer as everything else, and if the project stops building the whole
change is rolled back.

```bash
rn-agent review                          # review the code most likely to matter
rn-agent review --changed                # only what git says you touched
rn-agent review --area hooks --area performance
rn-agent review --fail-under 80          # CI gate, same scoring as `health`

rn-agent fix --issue js.typecheck        # fix a finding by id, from health/review
rn-agent fix --about "the orders list re-renders on every keystroke"
rn-agent fix --file src/screens/Orders.tsx --check typecheck --check tests

rn-agent feature "add pull-to-refresh on the orders list"
rn-agent test src/screens/OrdersScreen.tsx
```

**How a fix is kept honest**

1. Only files you allowed are sent to the model — secrets are excluded and the
   list is printed with `--verbose`.
2. The reply must be complete file contents. No diffs, no `// ...unchanged`.
3. `rules.yaml` is enforced *before* writing: `package.json`, lockfiles and
   native files are refused unless you pass `--allow-deps` / `--allow-native`.
4. Non-low-risk changes ask for confirmation (or `-y`), and the previous bytes
   are backed up.
5. `tsc` and your test script run afterwards. If they fail, every file is
   restored byte-for-byte — `--keep` opts out.

`--json` gives the whole run as machine-readable output, and every run leaves a
report in `.rn-agent/cache/<command>-report.json`.

Findings connect the commands: `health` and `review` record ids in the knowledge
store, and `fix --issue <id>` reads them back. You never paste an error message.

```
Proposed (1)
  low  Memoise the row renderer  fix-orders-rerender
      the callback was recreated on every keystroke
      ~ src/screens/OrdersScreen.tsx

Changed (1)
  ✓ src/screens/OrdersScreen.tsx
  backups written to .rn-agent/cache/backups/

Validation
  ✓ typecheck  passed
  ✓ tests      passed
```

`rn-agent test` may only write test files. A proposal that touches production
code is refused, and generated tests that fail are rolled back — a red test
nobody trusts is worse than no test.

---

## Maintenance

### rn-agent compatibility

Run this *before* a migration. It answers one question — can this project run on
that React Native version? — and refuses to guess.

```bash
rn-agent compatibility                 # against the newest published RN
rn-agent compatibility --target 0.82.1
rn-agent compatibility --offline       # installed metadata + bundled table only
```

Requirements come from the target's own `peerDependencies` and `engines`; when
the registry is unreachable the bundled table is used **and labelled**. Every row
is `ok`, `conflict` or `unknown`, and unknowns never block — Gradle/AGP numbers
for a version you have not installed are genuinely not knowable locally, so they
are reported as unknown with your current value shown. Exit code is `1` when
there is a conflict.

### rn-agent upgrade

Deterministic: no model is involved in the decision.

```bash
rn-agent upgrade                       # minor upgrades, the default
rn-agent upgrade --target patch
rn-agent upgrade --target latest --only lodash --only axios
rn-agent upgrade --native              # include packages with native code
rn-agent upgrade --offline             # report drift without contacting npm
```

* `react-native` and `react` are always blocked, pointing at `rn-agent migrate`
  — a React Native bump is a migration, not a range rewrite.
* A peer conflict blocks the candidate and is listed. An *undecidable* range
  (`workspace:*`, a git URL) is a note, never an invented conflict.
* Native packages need a pod install and a rebuild, so they rank higher and are
  excluded unless you ask.
* `package.json` is rewritten (keeping your `^`/`~` and your indentation), your
  package manager installs, then `tsc`/tests run — and a failure restores the
  manifest.

### rn-agent migrate

```bash
rn-agent migrate                       # to the newest published version
rn-agent migrate --to 0.82.1
rn-agent migrate --skip-native         # JS and dependencies only
rn-agent migrate --build               # also run the Android/iOS builds
rn-agent migrate --offline --no-ai
```

What happens, in order: a branch (`rn-agent/migrate-0.82.1`), `package.json`
updated from the target's own requirements, the upstream template diff applied
per file, install, `pod install`, typecheck, tests — and, if that fails and AI is
configured, **one** repair round before the whole migration is rolled back.

Diffs are applied strictly. A hunk lands only when its context matches exactly;
anything drifted becomes a reported conflict with the hunk attached, never a
fuzzy patch. Files you customised or deleted become manual steps. Every attempt —
including a rolled-back one — is recorded in `.rn-agent/migration-history.json`.

Local, exact edits can be pinned in `migration-rules/*.yaml` (see that
directory's README); the loader is version-matched and skips any action this
version does not implement.

---

## Shipping

```bash
rn-agent docs                          # writes docs/PROJECT.md from the facts
rn-agent docs --section architecture --section setup -o ARCHITECTURE.md

rn-agent release --bump minor          # every version an app carries
rn-agent release --version 2.0.0 --no-changelog
rn-agent --dry-run release             # see the plan first
```

`docs` may write exactly the file you named — an edit anywhere else is refused —
and it updates in place rather than replacing prose you wrote.

`release` finds every place a version lives: `package.json`,
`android/app/build.gradle` (`versionName` **and** `versionCode`) and the Xcode
project (`MARKETING_VERSION`, `CURRENT_PROJECT_VERSION`). A field it cannot find
is reported, so a platform is never silently left behind. A dirty tree, no
commits since the last tag, or a critical finding in your last `health` report
block the release (`--force` overrides).

It does not commit, tag, push or upload. `GitManager` implements no
history-writing operation and this command does not add one — the git commands
are printed as a checklist for you to run.

---

## Configuration

`rn-agent scan` creates `.rn-agent/config.yaml`. It is safe to commit: it holds
no credentials.

```yaml
ai:
  provider: null          # set by `rn-agent login` / `rn-agent provider`
  model: null
  models:                 # optional per-task models (§9)
    default: null
    migration: null
    debugging: null
    review: null
  enabled: true
  base_url: null          # self-hosted gateway, or Ollama on another machine
  max_output_tokens: 4096
  temperature: 0.0
  timeout_seconds: 120.0
  max_context_files: 40
  max_context_tokens: 120000

safety:
  require_confirmation: true
  auto_fix_low_risk: false
  require_clean_git: false
  create_backups: true
  max_files_per_operation: 200

migration:
  create_git_branch: true
  run_android_build: true
  run_ios_build: true
  run_tests: true

context:
  respect_gitignore: true
  allow_secret_files: false
  max_file_kb: 96

logging:
  level: INFO
```

User-level preferences (which provider/model you like) live in
`~/.config/rn-agent/config.yaml` and are merged underneath the project file, so
a project can override them without you re-authenticating. Credentials are never
in either file — see [Setting up AI](#setting-up-ai).

---

## The shared project brain

```
.rn-agent/
├── config.yaml            # behaviour (safe to commit)
├── project-context.json   # the brain: everything scan learned
├── architecture.yaml      # inferred architecture (edit to correct it)
├── rules.yaml             # project rules the agent must respect
├── dependencies.json      # dependency inventory
├── decisions.md           # architectural decisions
├── knowledge/knowledge.db # SQLite: runs, findings, decisions, AI usage
├── cache/                 # reports, backups
└── logs/                  # scan.log, health.log, ...
```

Your own state lives outside every project, so nothing secret can be committed:

```
~/.config/rn-agent/              # or $XDG_CONFIG_HOME, or $RN_AGENT_HOME
├── config.yaml                  # your provider/model preference
├── credentials.json             # index: which provider, which backend. No keys.
└── credentials.enc.json         # only when no OS keychain exists (mode 0600)
```

`rules.yaml` is seeded from what was detected and is never overwritten once you
edit it:

```yaml
rules:
  allowed_state_management: [redux-toolkit, redux, redux-saga]
  allowed_navigation: [react-navigation]
  forbid_new_dependencies: true
  forbid_native_edits_without_confirmation: true
```

`health` reuses the context written by `scan` (it refreshes automatically when
the context is missing or older than a day), which is what makes this one agent
rather than a pile of scripts.

---

## Architecture

```
                    RN AGENT
                       │
                 Typer CLI (cli/)
                       │
              AgentCommand contract
          analyze → plan → execute → validate
                       │
                  AgentContext
   ┌───────────────┼───────────────┬───────────────┐
ProjectContext  KnowledgeStore  GitManager    AIProvider
   │               │               │               │
FileManager    CommandRunner   SafetyManager  CredentialStore
```

```
src/rn_agent/
├── cli/            Typer app, Rich UI, shared runtime + 3 command groups
├── core/           AgentContext, AgentCommand, registry, config, logging, paths
├── commands/       scan, health, review, fix, feature, test, upgrade,
│                   migrate, compatibility, docs, release
├── agents/         the AI work layer: rules, context budget, prompts, output
│                   parsing, one call path, apply/rollback workflow
├── ai/             AIProvider + Anthropic/OpenAI/Ollama, registry
├── auth/           keychain backends, CredentialStore, login/whoami policy
├── net/            the one HTTP seam (JsonTransport) for every subsystem
├── validation/     ProjectValidator: install, pods, tsc, eslint, tests, builds
├── upgrade/        npm registry client + deterministic upgrade planner
├── migration/      diff sources, strict diff engine, local rules, planner, history
├── project/        detector, packages, android, ios, architecture, scanner
├── analyzers/      project, react-native, javascript, android, ios
├── models/         pydantic: project, health, config, changes, proposal,
│                   review, upgrade, migration, compatibility, release, validation
├── knowledge/      SQLite store + curated offline data (YAML)
├── git/            GitManager (no destructive or history-writing operation)
├── filesystem/     FileManager (the only writer: backups, rollback), ProjectWalker
├── runner/         CommandRunner (the only place we shell out)
├── safety/         SafetyManager (risk, confirmation, secret filtering)
├── reporting/      Rich renderers, one per report shape
└── utils/          semver, io, redaction
```

Five decisions worth knowing:

**Facts before tables.** Instead of hard-coding "RN 0.81 needs React 19.1", the
agent reads `node_modules/react-native/package.json` — `peerDependencies.react`
and `engines.node` are the truth for *your* install. The bundled table is a
labelled fallback used only when dependencies are not installed, and it only
asserts *major*-version disagreements.

**A real semver engine.** Every constraint in a React Native project is
node-semver (`^19.1.1`, `>= 20.19.4`, `^16.8 || ^17.0 || ^18.0`,
`workspace:*`). `utils/semver.py` implements caret/tilde/wildcard/hyphen/OR
ranges with node's pre-release rule, and reports *undecidable* for git and
workspace specifiers rather than guessing.

**One transport, one shell-out, one writer.** Every HTTP request goes through a
`JsonTransport` (`net/http.py`) — model completions, npm registry lookups and
upstream diffs alike; every external tool goes through `CommandRunner`; every
byte written to your project goes through `FileManager`. Timeouts, error mapping,
redaction, backups and rollback therefore exist exactly once, and a test can hand
in a fake transport instead of opening a socket.

**Rules are enforced, not requested.** `.rn-agent/rules.yaml` goes into every
prompt *and* into `EditApplier.screen()`. A model that ignores "do not add
dependencies" still cannot write `package.json`: the edit is refused by path
before the safety gate is reached.

**Apply, prove, undo.** Every write-command applies the change, runs the
project's own checks, and restores the previous bytes when they fail. That
ordering is only safe because the writer backed everything up first — which is
why there is one writer.

---

## Security

* **Your AI account, your keys.** The agent never proxies requests through a
  vendor, ships no shared key, and uses official provider APIs only — no browser
  cookies, no session tokens. A provider that needs a credential refuses to be
  constructed without one.
* **Credentials in the OS keychain**, never in the project: macOS keychain,
  Linux Secret Service, Windows DPAPI. Where no keyring exists, a `0600` file is
  used and every command says so out loud instead of pretending.
* **Keys travel on stdin**, never in `argv`, so they cannot appear in another
  user's process list; they are validated before storage and read back to prove
  the write landed.
* **Verify before store.** `login` checks the key against the provider's own API
  first, so a rejected key never reaches your keychain.
* **Secrets are never read.** `.env*`, `*.pem`, `*.p8`, `*.p12`,
  `*.mobileprovision`, `*.keystore`, `*.jks`, `google-services.json`,
  `GoogleService-Info.plist`, `local.properties` and friends are excluded from
  anything that could reach a model, and the iOS parser never opens signing
  material.
* **Logs and errors are redacted.** Token-shaped strings (`sk-…`, `ghp_…`,
  `AIza…`, JWTs) are masked before anything is written to `.rn-agent/logs/` — a
  provider error that echoes your key back is masked too.
* **No network unless you ask.** `scan` and `health` make no HTTP requests at
  all. Only `login`, `whoami --check`, `model --list --remote`, and the commands
  that genuinely need a remote fact do: the AI commands (your provider),
  `upgrade`/`compatibility` (the npm registry) and `migrate` (the upstream
  template diff). `--offline` is available on all three of the latter.

---

## Safety

* `--dry-run` on every command; `scan --dry-run` does not even create
  `.rn-agent/`.
* The writer resolves every path and **refuses anything outside the project
  root** (`../`, absolute paths, symlink escapes).
* Every modification is backed up under `.rn-agent/cache/backups/<run>/` before
  new bytes land, and `rollback()` restores it byte-for-byte.
* Every change records file, before/after hash, reason, command and risk.
* `git reset --hard` and `git clean -fd` are **not implemented anywhere** in the
  agent — there is no code path, and a test asserts their absence.
* Native files (`android/`, `ios/`, `*.gradle`, `*.pbxproj`, `Podfile`) are never
  classed as low risk, so they can never be auto-applied.
* Model-proposed edits are screened against your `rules.yaml` **before** the
  safety gate: lockfiles are always refused, and `package.json` and native files
  need an explicit `--allow-deps` / `--allow-native`.
* Every write-command validates afterwards and rolls the whole change back when
  the project stops building (`--keep` opts out).
* `migrate` works on a branch, refuses a dirty tree, applies diff hunks only
  when their context matches exactly, and records every attempt — including a
  rolled-back one — in `.rn-agent/migration-history.json`.
* `release` writes version numbers and a changelog, and **never** commits, tags
  or pushes.

---

## Logs and state

```bash
tail -f .rn-agent/logs/health.log
cat .rn-agent/cache/health-report.json | jq '.checks[] | select(.status=="fail")'
rn-agent info
```

Add this to your `.gitignore` (scan writes `.rn-agent/.gitignore` for you):

```
.rn-agent/cache/
.rn-agent/logs/
.rn-agent/knowledge/
```

---

## Development

```bash
git clone https://github.com/rn-agent/rn-agent.git
cd rn-agent
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest                 # 573 tests
ruff check src tests   # lint
mypy                   # types
```

Adding a command is deliberately small: subclass `AgentCommand`, decorate it
with `@register`, and add one Typer function that builds the context and calls
`run()`. The four phases (`analyze`/`plan`/`execute`/`validate`) give you
dry-run, logging, run recording and error rendering for free.

Build the npm wrapper the way CI does:

```bash
python -m build --wheel --outdir npm/vendor
cd npm && npm pack
```

---

## Testing

```bash
pytest                          # everything
pytest tests/test_health.py     # one area
pytest -k semver                # one topic
```

573 tests covering project detection, RN/React/Node version resolution, package
manager detection, lockfile disambiguation, architecture inference, Gradle `ext`
indirection, Podfile/pbxproj/plist parsing, every health rule (fires *and* stays
silent), health scoring, git safety, file backup/rollback/traversal refusal,
safety policy, secret filtering and redaction, the SQLite store, provider request
shapes and error mapping, every keychain backend, credential precedence, the CLI
(exit codes, JSON, dry-run) and packaging consistency — plus, for phases 3-6:
the context budget and secret exclusion, model-output parsing and its refusals,
rules enforcement, apply/validate/rollback on real bytes, the npm registry client
and every upgrade risk rule, strict diff application (applies, conflicts,
already-applied, ambiguous context), migration rollback and the AI repair round,
compatibility conflict-versus-unknown, and release version discovery across
`package.json`, Gradle and the Xcode project.

Tests never touch a real project, the network, an AI provider, your user config
or your keychain: fixtures make network calls fail loudly, redirect
`RN_AGENT_HOME` into `tmp_path`, force the file credential store and unset every
provider API key.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `no package.json found` | Run inside a React Native project, or pass `-C path/to/app` |
| `does not declare react-native or expo` | You are in a monorepo root; run inside the app package |
| `Run rn-agent scan first` | A command needed the shared context; run `rn-agent scan` |
| Versions look like ranges, not numbers | `node_modules` is missing — run your install; scan says which source it used |
| `agp: not pinned` | Normal for RN 0.76+: the React Native Gradle plugin resolves AGP. Reported honestly instead of guessed |
| `Multiple lockfiles present` | Delete the ones you do not use; mixed lockfiles produce different trees per machine |
| `Node … does not satisfy … engines.node` | Install the Node version react-native asks for (nvm/fnm/volta) |
| `Podfile.lock pins React-Core X` | Run `pod install` in `ios/` after every RN version change |
| Health score seems harsh | `rn-agent health --verbose` lists every check and its evidence; each point maps to one finding |
| npm install printed a Python warning | Install Python 3.11+, then run any `rn-agent` command — the runtime self-heals |
| `no AI provider configured` | Run `rn-agent login <provider>`; `rn-agent provider --list` shows the options |
| `no credential for anthropic` | The key is neither in the keychain nor in `ANTHROPIC_API_KEY`; run `rn-agent login anthropic` |
| Key works in one shell but not another | An environment variable takes precedence over the keychain — `rn-agent whoami` prints which one was used |
| `no OS keychain was reachable` | No keyring on this machine (container/CI): the key is in a `0600` file, or export the env var instead |
| `cannot reach http://127.0.0.1:11434` | Ollama is not running — `ollama serve`, or point `--base-url` at the machine that runs it |
| `Credential rejected` (HTTP 401) | The key was revoked or belongs to another account; `rn-agent login <provider>` again |
| `the model did not return usable JSON` (exit 12) | The model ignored the output contract; retry, or pick a stronger one with `rn-agent model --list` |
| `the model's answer was cut off` (exit 12) | Raise `ai.max_output_tokens`, or narrow the request (`--file`, one `--issue`) |
| `npm registry unreachable` (exit 11) | `upgrade`/`compatibility` need the registry for target versions; retry, or use `--offline` for drift only |
| `no published diff for X -> Y` | `migrate` could not find that upstream version pair; check both versions exist, or pin the steps in `migration-rules/` |
| A migration step says `conflict` | Your file drifted from the template; the hunk is printed — apply it by hand, then re-run |
| `every proposed change was refused by your rules` | Your `rules.yaml` forbids it; use `--allow-deps` / `--allow-native`, or edit the rules |
| `release blocked` | Commit or stash first, or `--force`; the blockers are listed with their reasons |

---

## Roadmap

| Phase | Contents | Status |
|---|---|---|
| 1 | CLI, project scanner, shared context, git/file/runner/safety managers, config, logging, `scan`, `health` | **done** |
| 2 | `AIProvider` abstraction, Claude/OpenAI/Ollama, `login`/`logout`/`whoami`, `provider`, `model`, task models, OS keychain | **done** |
| 3 | `review`, `fix`, `feature`, `test` | **done** |
| 4 | `upgrade` with peer/native risk analysis | **done** |
| 5 | `migrate`: upstream template diffs, local rules, build validation, AI-assisted error fixing, rollback | **done** |
| 6 | `compatibility`, `docs`, `release` | **done** |

Per-phase reports, with the decisions and the bugs the tests caught, live in
[`docs/`](docs/): [phase 1](docs/phase-1.md), [phase 2](docs/phase-2.md),
[phase 3](docs/phase-3.md), [phase 4](docs/phase-4.md),
[phase 5](docs/phase-5.md), [phase 6](docs/phase-6.md).

## License

MIT — see [LICENSE](LICENSE).
