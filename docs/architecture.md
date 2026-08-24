# Architecture notes

Reference for contributors. The user-facing overview is in the [README](../README.md).

## Why the command contract exists

```python
class AgentCommand:
    def analyze(self) -> Analysis   # read facts, no writes
    def plan(self, analysis) -> Plan  # decide, no writes
    def execute(self, plan) -> None   # the only phase that may write
    def validate(self, plan) -> dict  # prove the result
```

`run()` sequences the phases and gives every command the same behaviour for
free: run recording in SQLite, error rendering, exit codes, dry-run and quiet
(JSON) mode. `read_only = True` skips `execute` entirely, which is *why*
`health` structurally cannot modify a project — it is not a promise in a
docstring, it is a missing call.

This also makes `--dry-run` cheap to keep honest: `plan()` already contains the
whole decision, so a preview is the same code path minus one phase.

## Fact hierarchy

Every compatibility claim follows the same order:

1. **The project's own installed metadata.**
   `node_modules/react-native/package.json` carries `peerDependencies.react`
   and `engines.node`. That is the truth for this install, and it needs no
   maintenance from us.
2. **Curated data, labelled.** `knowledge/data/advisories.yaml` carries
   renames, removals, permission requirements and dated store policies — each
   with `source` and `confidence`, both surfaced in `--verbose`.
3. **Skip.** If neither is available the check reports `SKIP` with the reason.

Consequences visible in the output:

* No `node_modules` → the React check only asserts a *major* mismatch and says
  it used the offline table.
* An unknown React Native series → `SKIP`, not a guess.
* Next year's Play `targetSdk` requirement is marked `enforce: false` and
  reported as information until its date passes.

## Version resolution and provenance

`ReactNativeInfo.version_source` records where a version came from:
`node_modules`, a lockfile name, or `package.json`. The renderer prints it
(`0.79.1 (from yarn.lock)`), because "installed" and "declared" and "locked" are
three different facts and conflating them is how wrong advice gets given.

Lockfile reading is deliberately conservative. A yarn lockfile routinely holds
several entries for one package:

```
react-native@*:
  version "0.85.2"

react-native@0.79.1:
  version "0.79.1"
```

The entry matching the declared range wins; when nothing disambiguates, the
resolver returns `None` rather than the first match it happened to find.

## Gradle reading

Gradle is a programming language, so `project/android.py` is a pragmatic reader,
not an evaluator. It handles the shapes real apps use:

* literals — `compileSdk 35`
* the RN template's indirection — `compileSdk rootProject.ext.compileSdkVersion`,
  resolved through the root `build.gradle` `ext { }` block
* AGP pinned as `classpath("com.android.tools.build:gradle:8.6.0")` **and**
  the version-less form used since RN 0.76 (reported as unknown, never guessed)
* `gradle-wrapper.properties` distribution URLs, where `gradle-7.6-all.zip`
  must coerce to `7.6.0` and not `7.6.0-all.zip`

Anything unparsable becomes `None`. One weird file never raises.

## iOS reading

Deployment target resolution is a chain, because the Podfile usually does not
contain a literal:

```ruby
platform :ios, min_ios_version_supported
```

So: `project.pbxproj` first, then a literal Podfile platform, then unknown. The
Podfile value is stored verbatim so the report can show the disagreement rather
than silently normalising it away.

Signing material is never opened — a test monkeypatches `Path.open` and asserts
no `.mobileprovision` is touched.

## The semver module

React Native projects express constraints in node-semver, so
`utils/semver.py` implements it: caret, tilde, wildcards, partials, hyphen
ranges, whitespace-separated AND, `||` OR, and node's default pre-release rule.

Two behaviours matter for correctness:

* `satisfies()` returns `True`/`False`/**`None`**. `None` means undecidable
  (`git+https://…`, `workspace:*`, `latest`, unparsable input) and callers must
  treat it as unknown. This is what keeps the peer-dependency check free of
  invented conflicts.
* A dangling operator raises instead of silently widening. `">= 20.19.4"` once
  collapsed into `"== 20.19.4"` and reported Node 22 as too old; there is now a
  regression test for exactly that string.

## Safety layering

* `FileManager` is the only writer. It resolves paths, refuses anything outside
  the project root, backs up before modifying, records a `FileChange` and
  supports `rollback()`.
* `SafetyManager` owns policy: risk classification (native and lockfile paths
  are never low risk), the confirmation gate, and secret filtering for AI
  context.
* `GitManager` is read-only apart from `create_branch`. No destructive git
  subcommand is implemented, so no AI suggestion can route through the agent to
  `git reset --hard`.

## The provider layer

A provider is four small hooks plus inherited plumbing. `AIProvider.complete()`
builds the payload (`_payload`), sends it (`_request`), and parses the answer
(`_parse_completion`); `_headers` supplies authentication. Everything else —
URL joining, HTTP error mapping, redaction, logging, the "no credential, no
object" rule — lives in the base class, so a new backend cannot forget it.

The differences worth knowing, because they are the ones that break in
production:

* **Anthropic** takes the system prompt as a top-level field and *requires*
  `max_tokens`. `_split_system()` pulls system turns out of the conversation for
  it; the OpenAI and Ollama providers re-insert them as a leading turn.
* **OpenAI** reasoning models (`o1`/`o3`/`o4`/`gpt-5`) reject `max_tokens` and a
  non-default `temperature`; the older chat models reject
  `max_completion_tokens`. The model name selects the fields, so a wrong pairing
  never reaches the API.
* **Ollama** reports token counts at the top level (`prompt_eval_count`,
  `eval_count`) rather than under `usage`, needs `stream: false`, and accepts the
  bare `host:port` form of `OLLAMA_HOST` (normalised to a URL).

`verify()` is deliberately the model-list endpoint: it is the cheapest call that
proves a credential *and* yields the account's real catalogue, which is why
`login` can warn that the selected model is not in it and why
`model --list --remote` needs no second code path. The bundled `suggested_models`
are labelled suggestions, never presented as the catalogue — the same rule as the
offline compatibility table.

`ai/http.py` exists so no provider imports a client library. Tests hand a fake
transport in; the `conftest` network guard stays armed for everything else.

## Credential handling

Resolution order is fixed and *reported*: the provider's environment variable,
then the keychain. `whoami` prints which one answered, because "I exported a key"
and "a key is in my keychain" are different facts, and conflating them is how
people debug the wrong machine.

Backends shell out through `CommandRunner` (the one place the agent executes
anything) and always pass the secret on **stdin**:

* macOS uses `security -i`, which reads subcommands from stdin, instead of
  `-w <secret>` — so the key never appears in `argv`, and therefore never in
  another user's `ps`. Exit code 44 means "no such item", which is an answer, not
  a failure, so those probes run with `quiet=True`.
* Linux uses `secret-tool`, whose `store` already reads stdin.
* Windows uses DPAPI through PowerShell. `cmdkey` is not used: it can write
  Credential Manager entries but not read them back, which is useless here.
* With no keyring at all, `FileBackend` writes `0600` and sets
  `secure = False`, which every command surfaces. A silent plaintext fallback
  would be worse than an honest one.

`CredentialStore.store()` validates the key's shape, writes, then **reads it
back and compares** — a backend that accepts a write and loses it fails loudly
instead of at the next command. The index
(`~/.config/rn-agent/credentials.json`) records which providers have a key and
in which backend, and holds no secret: keychains cannot be enumerated portably,
and guessing is not an option.

`login` verifies before it stores, so a key the provider rejects never lands in
the keychain.

## Adding a command

```python
@register
class ReviewCommand(AgentCommand[ReviewAnalysis, ReviewPlan]):
    name = "review"
    description = "Analyse components, hooks and performance"
    read_only = True

    def analyze(self) -> ReviewAnalysis:
        project = self.context.project      # the shared brain
        ...
```

Then one Typer function in `cli/app.py` that builds the context and calls
`run()`. Nothing else needs to change: the registry, logging, run recording and
dry-run all pick it up.

A command that needs a model asks the context for one:

```python
    def plan(self, analysis: ReviewAnalysis) -> ReviewPlan:
        completion = self.context.ai.complete(
            [Message.system(RULES), Message.user(prompt)], task="review"
        )
        self.context.record_ai_usage(completion)
```

`context.ai` is lazy, so a read-only command that never touches it makes no
network call and builds no provider; it raises `ProviderError` (exit code 10)
when AI is unconfigured or disabled, rather than degrading into a guess.
`record_ai_usage()` is the accounting hook for the `ai_usage` table.
