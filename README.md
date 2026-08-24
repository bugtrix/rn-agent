# rn-agent

One AI-powered React Native agent with many commands and a **shared project brain**.

`rn-agent` scans your React Native project once, then every command works from
that same context: your architecture, your dependencies, your native config,
your git state. It is built for real production apps — the ones with 100+
dependencies, 35 native modules, a Gradle `ext` block and a Podfile you did not
write yourself.

> **Status: Phase 2.** `scan`, `health` and the AI setup commands (`login`,
> `logout`, `whoami`, `provider`, `model`) are implemented, tested and usable
> today. `review`, `fix`, `feature`, `test`, `upgrade` and `migrate` land in
> later phases (see [Roadmap](#roadmap)). Nothing in this repository fakes an
> AI response or pretends a command exists before it works.

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
8. [Configuration](#configuration)
9. [The shared project brain](#the-shared-project-brain)
10. [Architecture](#architecture)
11. [Security](#security)
12. [Safety](#safety)
13. [Logs and state](#logs-and-state)
14. [Development](#development)
15. [Testing](#testing)
16. [Troubleshooting](#troubleshooting)
17. [Roadmap](#roadmap)

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
| `rn-agent review` / `fix` / `feature` / `test` | ⏳ Phase 3 | Daily development work |
| `rn-agent upgrade` | ⏳ Phase 4 | Risk-ranked dependency upgrades |
| `rn-agent migrate` | ⏳ Phase 5 | React Native version migration from the official docs |

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
├── cli/            Typer app + Rich UI primitives + AI setup commands
├── core/           AgentContext, AgentCommand, registry, config, logging, paths
├── commands/       ScanCommand, HealthCommand
├── ai/             AIProvider + Anthropic/OpenAI/Ollama, registry, transport
├── auth/           keychain backends, CredentialStore, login/whoami policy
├── project/        detector, packages, android, ios, architecture, scanner
├── analyzers/      project, react-native, javascript, android, ios
├── models/         pydantic: project, health, config, changes
├── knowledge/      SQLite store + curated offline data (YAML)
├── git/            GitManager (no destructive operation exists in it)
├── filesystem/     FileManager (backups, rollback), ProjectWalker
├── runner/         CommandRunner (the only place we shell out)
├── safety/         SafetyManager (risk, confirmation, secret filtering)
├── reporting/      Rich renderers
└── utils/          semver, io, redaction
```

Three decisions worth knowing:

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

**One transport, one shell-out.** Providers never touch a client library
directly: every request goes through a `JsonTransport` (`ai/http.py`) and every
keychain call through `CommandRunner`, so timeouts, error mapping and redaction
exist once — and a test can hand a provider a fake transport instead of a socket.

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
  all; only `login`, `whoami --check` and `model --list --remote` do.

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

pytest                 # 352 tests
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

352 tests covering project detection, RN/React/Node version resolution, package
manager detection, lockfile disambiguation, architecture inference, Gradle `ext`
indirection, Podfile/pbxproj/plist parsing, every health rule (fires *and* stays
silent), health scoring, git safety, file backup/rollback/traversal refusal,
safety policy, secret filtering and redaction, the SQLite store, provider request
shapes and error mapping, every keychain backend, credential precedence, the CLI
(exit codes, JSON, dry-run) and packaging consistency.

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

---

## Roadmap

| Phase | Contents | Status |
|---|---|---|
| 1 | CLI, project scanner, shared context, git/file/runner/safety managers, config, logging, `scan`, `health` | **done** |
| 2 | `AIProvider` abstraction, Claude/OpenAI/Ollama, `login`/`logout`/`whoami`, `provider`, `model`, task models, OS keychain | **done** |
| 3 | `review`, `fix`, `feature`, `test` | next |
| 4 | `upgrade` with peer/native risk analysis | planned |
| 5 | `migrate`: official RN docs + Upgrade Helper diffs, template comparison, Android/iOS migration, build validation, AI-assisted error fixing, rollback | planned |
| 6 | `compatibility`, `docs`, `release` | planned |

## License

MIT — see [LICENSE](LICENSE).
