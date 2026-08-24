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

`net/http.py` exists so nothing imports a client library. It began as
`ai/http.py`; phases 4 and 5 gave it two more consumers - the npm registry and
the upstream React Native diffs - so it moved out of `ai/` rather than being
imported sideways. `TransportError` is therefore no longer a `ProviderError`
(exit 11, not 10); `AIProvider._request` translates it, so an AI network failure
still reports 10 with the provider's own hint. Tests hand a fake transport in;
the `conftest` network guard stays armed for everything else.

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

## The AI work layer

Six commands ask a model for something (`review`, `fix`, `feature`, `test`,
`docs`, and the repair round inside `migrate`), so the parts they share live in
`agents/` rather than in each of them:

| Module | Responsibility |
|---|---|
| `rules.py` | `.rn-agent/rules.yaml` as prompt text **and** as a checker |
| `context_builder.py` | which files may be sent, inside the configured budget |
| `prompts.py` | the exact wording, and the JSON output contract per task |
| `output.py` | decoding a reply, or refusing it |
| `engine.py` | one call path: per-task model, accounting, one repair retry |
| `apply.py` | rules -> risk -> consent -> write -> rollback |
| `workflow.py` | apply, then prove, then undo when the proof fails |

Two of those deserve the emphasis.

**Rules are enforcement.** `as_prompt_lines()` tells the model the constraints;
`violations()` is what actually holds. A model that ignores "do not add
dependencies" still cannot write `package.json`, because `EditApplier.screen()`
refuses the edit by path before the safety gate is reached. Lockfiles are refused
unconditionally - they are generated files.

**The reply is whole files, never a patch.** A hunk that fails to apply leaves a
half-edited file; a full replacement either lands or does not, and `FileManager`
holds the previous bytes. `output.py` drops an edit with no content, normalises
an unknown severity to `medium` and an unknown area to `other`, and rejects an
absolute or `..` path outright - so a creative reply cannot widen the vocabulary
the rest of the agent trusts.

`AIEngine` refuses a truncated answer rather than parsing half a file, and
retries exactly once on an unparsable one, handing the parse error back. Two
failures is an error: a model that cannot honour the contract twice will not
honour it on the fifth attempt, and the developer pays per token.

## Proof, and undoing

`validation/runner.py` runs the project's *own* tools - `node_modules/.bin/tsc`,
the project's test script, `android/gradlew` - never a tool it fetched itself.
A step that cannot run reports `SKIP` with the reason, which is why
`ValidationReport` distinguishes `ok` (nothing failed) from `proved` (something
actually ran and passed). "The tests pass" and "there are no tests" are different
facts and only one of them is evidence.

Every write-command therefore reads: apply -> prove -> roll back on failure. That
ordering is only safe because `FileManager` backed the previous bytes up first,
which is the reason there is exactly one writer.

## Strict diff application

`migration/diff.py` is the most dangerous code in the agent, so it is the most
conservative. A hunk applies only when the lines it claims to remove and the
context around them match the file as it is now. The stated line number is a
hint - real projects have drifted - so the matcher searches outward and requires
**exactly one** match; zero or two is a conflict. There is no fuzzy mode and no
partial write.

Two subtleties the tests pinned down:

* **"Already applied" is checked first.** A hunk that only adds lines still
  matches its own context afterwards, so checking "does it apply?" first would
  duplicate the addition on a re-run.
* **`Hunk.header` is kept.** A stored hunk has to remain a valid patch fragment:
  the planner records it, the applier re-parses it, and a conflict prints it for
  the developer.

Upstream diffs name the app `RnDiffApp`. That is mapped to the project's real
name in paths *and* content; when the mapping cannot be made unambiguously the
step becomes `MANUAL`, because writing `RnDiffApp` into someone's Xcode project
is worse than admitting the limit.

## Adding a command

```python
class ReviewCommand(AgentCommand[ReviewAnalysis, ReviewPlan]):
    name = "review"
    description = "Analyse components, hooks and performance"
    read_only = True

    def analyze(self) -> ReviewAnalysis:
        project, _ = self.context.ensure_project()   # the shared brain, refreshed
        ...

register(ReviewCommand, phase=3)
```

Then one Typer function in `cli/develop.py` or `cli/maintain.py`:

```python
def review(...) -> None:
    """Analyse components, hooks, state and performance with your model."""
    from ..commands.review import ReviewCommand

    context = build_context("review")
    execute(ReviewCommand(context, ...))
```

`cli/runtime.py` owns everything around the command - building the context,
rendering an expected failure as a panel, suppressing the Rich report in `--json`
mode, serialising `command.report`, and exiting with the command's code - so a
new command needs one function and nothing else. The command module is imported
*inside* the function, which is why `rn-agent scan --help` still loads no AI code.

A command that needs a model asks the context for one, through the engine:

```python
    findings, notes, completion = AIEngine(self.context).review(
        prompts.review_messages(project=project, rules=rules, context=selected)
    )
```

`context.ai` is lazy, so a command that never touches it makes no network call
and builds no provider; it raises `ProviderError` (exit code 10) when AI is
unconfigured or disabled, rather than degrading into a guess. `AIEngine` calls
`record_ai_usage()`, the accounting hook for the `ai_usage` table.
