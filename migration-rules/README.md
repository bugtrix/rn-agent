# Migration rules

Deterministic, version-pinned migration edits consumed by `rn-agent migrate`
(`migration/rules.py`). The engine is implemented and tested; this directory
ships **empty on purpose** — the agent does not invent migration steps it cannot
attribute to a source.

Use these when the upstream template diff cannot help: your project customised
the file the diff touches, or the change is a one-line property you want applied
exactly, whatever else moved around it.

## Format

```yaml
# 0.81-to-0.82.yaml
from: "0.81"                 # optional; omit to match any source version
to: "0.82"                   # required: matched against the migration target
source: https://reactnative.dev/docs/upgrading
android:
  - id: gradle.wrapper
    file: android/gradle/wrapper/gradle-wrapper.properties
    action: set_property
    key: distributionUrl
    value: "https\\://services.gradle.org/distributions/gradle-8.13-all.zip"
    risk: medium
ios:
  - id: podfile.min_version
    file: ios/Podfile
    action: replace
    old: "platform :ios, min_ios_version_supported"
    new: "platform :ios, '15.1'"
    risk: high
javascript:
  - id: metro.package_exports
    file: metro.config.js
    action: ensure_line
    line: "// unstable_enablePackageExports is on by default from 0.82"
    risk: low
```

Sections are `android`, `ios` and `javascript`; each produces migration steps of
that kind, which `--kind` can filter and `--skip-native` can skip.

## Actions

| Action | What it does | Idempotent because |
|---|---|---|
| `set_property` | sets `key=value` in a `.properties` file, in place when the key exists, appended when it does not | re-running finds the value already set |
| `replace` | replaces an exact string | `old` is gone; when `new` is present the step reports "already applied" |
| `ensure_line` | appends a line unless it is already there | the line is matched before appending |

Rules are matched by major.minor: a file with `to: "0.82"` applies to a migration
targeting any `0.82.x`, and `from` (when present) must match the current series.

Anything the running version does not implement — an unknown `action`, a missing
`file` — is **skipped with a warning** and reported in the migration plan. It is
never approximated into a different edit.

## Where the upstream diff goes

Template diffs are fetched per project and cached in
`.rn-agent/cache/migrations/<from>..<to>.diff`, not here. Point `--rules-dir` at
another directory to use a different rule set (a team-wide one, for example).
