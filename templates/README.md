# React Native templates

Cache of upstream React Native template files, used by `rn-agent migrate`
(phase 5) to diff your project against the target version instead of blindly
overwriting native files.

Empty on purpose: templates are fetched and cached at migration time so they
always match the version you are moving to.
