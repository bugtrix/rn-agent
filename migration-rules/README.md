# Migration rules

Deterministic, version-to-version migration rules consumed by `rn-agent migrate`
(phase 5). Empty on purpose: the migration engine is not implemented yet, and
this repository does not ship rules it cannot execute.

Planned format:

```yaml
# 0.80-to-0.81.yaml
from: "0.80"
to: "0.81"
source: https://reactnative.dev/docs/upgrading
android:
  - id: gradle.wrapper
    file: android/gradle/wrapper/gradle-wrapper.properties
    action: set_property
    key: distributionUrl
    value: ".../gradle-8.13-all.zip"
    risk: medium
ios:
  - id: podfile.min_version
    ...
```
