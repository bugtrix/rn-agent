"""``rn-agent migrate``: strict diffs, honest conflicts, real rollback.

The behaviours under test are the ones that decide whether a migration is
trustworthy: a hunk applies only when its context matches, a drifted file is
reported rather than mangled, the upstream template's placeholder name is either
mapped or refused, and a project that does not build afterwards is restored
byte-for-byte.
"""

from __future__ import annotations

import json

from rn_agent.commands.migrate import MigrateCommand
from rn_agent.errors import RNAgentError, TransportError
from rn_agent.migration.diff import HunkResult, apply_hunks, parse_diff, rename_placeholder
from rn_agent.migration.rules import RuleOutcome, apply_rule, load_rules
from rn_agent.migration.sources import DiffSource
from rn_agent.models.migration import StepKind, StepState
from rn_agent.net.http import HttpResponse

GRADLE_PROPERTIES = "android/gradle.properties"


def migration_config(**overrides):
    """A config whose migration steps suit a synthetic project.

    ``pod install`` is off: the fixture's Podfile is a stub, and CocoaPods
    failing on it would test the fixture rather than the migration.
    """
    from rn_agent.models.config import AgentConfig

    config = overrides.pop("config", None) or AgentConfig()
    config.migration.run_pod_install = False
    for key, value in overrides.items():
        setattr(config.migration, key, value)
    return config


DIFF = """\
diff --git a/android/gradle.properties b/android/gradle.properties
--- a/android/gradle.properties
+++ b/android/gradle.properties
@@ -1,2 +1,3 @@
 newArchEnabled=false
 hermesEnabled=true
+bundleCompression=none
"""


def packument(name: str, versions: dict[str, dict], latest: str) -> dict:
    return {
        "name": name,
        "dist-tags": {"latest": latest},
        "versions": {
            number: {"version": number, **payload} for number, payload in versions.items()
        },
    }


class Remote:
    """Serves registry documents and diff text over one fake transport."""

    def __init__(self, *, documents: dict[str, dict] | None = None, diff: str | None = None,
                 diff_status: int = 200, fail: bool = False) -> None:
        self.documents = documents or {}
        self.diff = diff
        self.diff_status = diff_status
        self.fail = fail
        self.calls: list[str] = []

    def request(self, method, url, *, headers, payload=None, timeout=120.0):
        self.calls.append(url)
        if self.fail:
            raise TransportError(f"cannot reach {url}")
        if url.endswith(".diff"):
            if self.diff is None or self.diff_status != 200:
                return HttpResponse(status=self.diff_status or 404, body={}, text="")
            return HttpResponse(status=200, body={}, text=self.diff)
        name = url.rsplit("/", 1)[-1].replace("%2F", "/")
        document = self.documents.get(name)
        if document is None:
            return HttpResponse(status=404, body={}, text="")
        return HttpResponse(status=200, body=document, text="")


def rn_documents(target: str = "0.82.0", react: str = "^19.1.0", node: str = ">=20.19.4") -> dict:
    return {
        "react-native": packument(
            "react-native",
            {
                "0.81.0": {"peerDependencies": {"react": "^19.1.0"}},
                target: {
                    "peerDependencies": {"react": react},
                    "engines": {"node": node},
                },
            },
            latest=target,
        )
    }


# ---------------------------------------------------------------------------
# the diff engine
# ---------------------------------------------------------------------------
def test_a_matching_hunk_applies():
    files = parse_diff(DIFF)
    assert [entry.path for entry in files] == [GRADLE_PROPERTIES]

    content = "newArchEnabled=false\nhermesEnabled=true\n"
    patched, result, applied = apply_hunks(content, files[0].hunks)

    assert result is HunkResult.APPLIED
    assert applied == 1
    assert patched == "newArchEnabled=false\nhermesEnabled=true\nbundleCompression=none\n"


def test_a_drifted_hunk_is_a_conflict_and_changes_nothing():
    files = parse_diff(DIFF)
    content = "newArchEnabled=true\nhermesEnabled=false\n"  # the project customised both

    patched, result, applied = apply_hunks(content, files[0].hunks)

    assert result is HunkResult.CONFLICT
    assert patched is None
    assert applied == 0


def test_an_already_applied_hunk_is_recognised():
    files = parse_diff(DIFF)
    content = "newArchEnabled=false\nhermesEnabled=true\nbundleCompression=none\n"

    patched, result, _ = apply_hunks(content, files[0].hunks)

    assert result is HunkResult.ALREADY
    assert patched is None


def test_an_ambiguous_context_is_a_conflict():
    diff = (
        "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n"
        "@@ -1,1 +1,2 @@\n line\n+added\n"
    )
    files = parse_diff(diff)

    _, result, _ = apply_hunks("line\nline\n", files[0].hunks)

    assert result is HunkResult.CONFLICT


def test_a_hunk_applies_even_when_the_file_drifted_around_it():
    diff = (
        "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n"
        "@@ -40,2 +40,3 @@\n anchor\n tail\n+added\n"
    )
    files = parse_diff(diff)
    content = "prelude\n" * 3 + "anchor\ntail\n"

    patched, result, _ = apply_hunks(content, files[0].hunks)

    assert result is HunkResult.APPLIED
    assert patched is not None and patched.endswith("anchor\ntail\nadded\n")


def test_the_template_placeholder_is_mapped_or_refused():
    text = "ios/RnDiffApp/AppDelegate.swift"

    mapped, decided = rename_placeholder(text, project_name="Demo")
    assert decided is True
    assert mapped == "ios/Demo/AppDelegate.swift"

    _, decided = rename_placeholder(text, project_name=None)
    assert decided is False

    untouched, decided = rename_placeholder("android/build.gradle", project_name=None)
    assert decided is True
    assert untouched == "android/build.gradle"


# ---------------------------------------------------------------------------
# local rules
# ---------------------------------------------------------------------------
def write_rule(directory, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "0.81-to-0.82.yaml").write_text(body, encoding="utf-8")


def test_set_property_updates_and_is_idempotent(tmp_path):
    write_rule(
        tmp_path / "rules",
        "from: '0.81'\nto: '0.82'\nsource: https://reactnative.dev/docs/upgrading\n"
        "android:\n"
        "  - id: newarch\n"
        f"    file: {GRADLE_PROPERTIES}\n"
        "    action: set_property\n"
        "    key: newArchEnabled\n"
        "    value: 'true'\n"
        "    risk: high\n",
    )

    rules = load_rules(tmp_path / "rules", from_version="0.81.0", to_version="0.82.0")
    assert len(rules) == 1
    rule = rules.rules[0]
    assert rule.kind is StepKind.ANDROID

    patched, outcome = apply_rule("newArchEnabled=false\nhermesEnabled=true\n", rule)
    assert outcome is RuleOutcome.APPLIED
    assert patched == "newArchEnabled=true\nhermesEnabled=true\n"

    _, outcome = apply_rule(patched, rule)
    assert outcome is RuleOutcome.ALREADY


def test_a_missing_file_is_reported_not_created(tmp_path):
    write_rule(
        tmp_path / "rules",
        "from: '0.81'\nto: '0.82'\n"
        "ios:\n  - id: podfile\n    file: ios/Podfile\n    action: replace\n"
        "    old: 'min_ios_version_supported'\n    new: \"'15.1'\"\n",
    )
    rules = load_rules(tmp_path / "rules", from_version="0.81.0", to_version="0.82.0")

    _, outcome = apply_rule(None, rules.rules[0])

    assert outcome is RuleOutcome.MISSING


def test_an_unknown_action_is_skipped_not_guessed(tmp_path):
    write_rule(
        tmp_path / "rules",
        "from: '0.81'\nto: '0.82'\n"
        "android:\n  - id: weird\n    file: android/build.gradle\n    action: teleport\n",
    )

    rules = load_rules(tmp_path / "rules", from_version="0.81.0", to_version="0.82.0")

    assert rules.rules == []
    assert rules.skipped == ["0.81-to-0.82.yaml:weird"]


def test_rules_for_another_version_pair_are_ignored(tmp_path):
    write_rule(tmp_path / "rules", "from: '0.70'\nto: '0.71'\nandroid: []\n")

    rules = load_rules(tmp_path / "rules", from_version="0.81.0", to_version="0.82.0")

    assert len(rules) == 0


def test_an_empty_rules_directory_is_fine(tmp_path):
    assert len(load_rules(tmp_path / "nothing", from_version="0.81.0", to_version="0.82.0")) == 0


# ---------------------------------------------------------------------------
# the diff source
# ---------------------------------------------------------------------------
def test_the_diff_is_cached_after_the_first_fetch(project):
    remote = Remote(diff=DIFF)
    source = DiffSource(cache_dir=project.paths().cache_dir, transport=remote)

    first = source.fetch("0.81.0", "0.82.0")
    second = source.fetch("0.81.0", "0.82.0")

    assert first is not None and first.cached is False
    assert second is not None and second.cached is True
    assert len(remote.calls) == 1
    assert source.cache_path("0.81.0", "0.82.0").is_file()


def test_a_missing_diff_is_reported_not_raised(project):
    source = DiffSource(
        cache_dir=project.paths().cache_dir, transport=Remote(diff_status=404)
    )

    assert source.fetch("0.81.0", "9.9.9") is None
    assert source.reason is not None and "no published diff" in source.reason


def test_offline_does_not_fetch(project):
    remote = Remote(diff=DIFF)
    source = DiffSource(cache_dir=project.paths().cache_dir, transport=remote)

    assert source.fetch("0.81.0", "0.82.0", offline=True) is None
    assert remote.calls == []


# ---------------------------------------------------------------------------
# the command
# ---------------------------------------------------------------------------
def run(project, remote, **kwargs):
    """Run the command with one fake transport behind both remote sources."""
    context_kwargs = kwargs.pop("context", {})
    context_kwargs.setdefault("config", migration_config())
    context = project.scanned(command="migrate", assume_yes=True, **context_kwargs)
    command = MigrateCommand(context, **kwargs)
    command.quiet = True
    original = command.analyze

    def analyze():
        import rn_agent.commands.migrate as module

        real_registry = module.NpmRegistry
        real_source = module.DiffSource
        module.NpmRegistry = lambda *a, **k: real_registry(transport=remote)  # type: ignore[assignment]
        module.DiffSource = lambda **k: real_source(transport=remote, **k)  # type: ignore[assignment]
        try:
            return original()
        finally:
            module.NpmRegistry = real_registry  # type: ignore[assignment]
            module.DiffSource = real_source  # type: ignore[assignment]

    command.analyze = analyze  # type: ignore[method-assign]
    return command, command.run()


def test_a_full_migration_applies_and_records(project):
    project.git_init()
    project.local_bin("tsc")
    project.local_bin("jest")
    remote = Remote(documents=rn_documents(), diff=DIFF)

    command, outcome = run(project, remote, to_version="0.82.0", install=False)

    manifest = json.loads((project.root / "package.json").read_text())
    assert manifest["dependencies"]["react-native"] == "0.82.0"
    assert "bundleCompression=none" in (project.root / GRADLE_PROPERTIES).read_text()
    assert outcome.exit_code == 0

    history = json.loads((project.root / ".rn-agent" / "migration-history.json").read_text())
    assert history[-1]["to_version"] == "0.82.0"
    assert history[-1]["rolled_back"] is False
    assert command.report is not None
    assert command.report.branch and command.report.branch.startswith("rn-agent/migrate")


def test_a_drifted_native_file_becomes_a_conflict(project):
    # Customise the file first, then commit: the tree must be clean to migrate.
    (project.root / GRADLE_PROPERTIES).write_text(
        "newArchEnabled=true\nhermesEnabled=false\n", encoding="utf-8"
    )
    project.git_init()
    project.local_bin("tsc")
    project.local_bin("jest")
    before = (project.root / GRADLE_PROPERTIES).read_text()
    remote = Remote(documents=rn_documents(), diff=DIFF)

    command, outcome = run(project, remote, to_version="0.82.0", install=False)

    assert (project.root / GRADLE_PROPERTIES).read_text() == before
    step = next(item for item in command.report.steps if item.file == GRADLE_PROPERTIES)
    assert step.state is StepState.CONFLICT
    assert "by hand" in (step.reason or "")
    # The rest of the migration still happened.
    assert outcome.exit_code == 0


def test_skip_native_leaves_the_platforms_alone(project):
    project.git_init()
    project.local_bin("tsc")
    project.local_bin("jest")
    before = (project.root / GRADLE_PROPERTIES).read_text()
    remote = Remote(documents=rn_documents(), diff=DIFF)

    command, _ = run(
        project, remote, to_version="0.82.0", install=False, skip_native=True
    )

    assert (project.root / GRADLE_PROPERTIES).read_text() == before
    assert all(step.kind is not StepKind.ANDROID for step in command.report.steps)


def test_the_offline_table_is_used_and_labelled(project):
    project.git_init()
    project.local_bin("tsc")
    project.local_bin("jest")
    remote = Remote()  # no registry document, no diff

    command, _ = run(project, remote, to_version="0.82.0", install=False, offline=True)

    assert command.report is not None
    assert command.report.offline is True
    assert remote.calls == []


def test_failed_validation_rolls_the_whole_migration_back(project, fake_ai, ai_config):
    project.git_init()
    project.local_bin("tsc", exit_code=2, output="error TS2307: Cannot find module")
    manifest_before = (project.root / "package.json").read_text()
    gradle_before = (project.root / GRADLE_PROPERTIES).read_text()
    remote = Remote(documents=rn_documents(), diff=DIFF)
    # The repair round gets its one retry, and still cannot help.
    fake_ai.reply({"proposals": [], "notes": ["I cannot fix this"]})
    fake_ai.reply({"proposals": [], "notes": ["I cannot fix this"]})

    command, outcome = run(
        project,
        remote,
        to_version="0.82.0",
        install=False,
        context={"config": migration_config(config=ai_config)},
    )

    assert (project.root / "package.json").read_text() == manifest_before
    assert (project.root / GRADLE_PROPERTIES).read_text() == gradle_before
    assert outcome.exit_code == 1
    assert outcome.summary["rolled_back"] is True
    history = json.loads((project.root / ".rn-agent" / "migration-history.json").read_text())
    assert history[-1]["rolled_back"] is True


def test_an_ai_repair_round_can_save_the_migration(project, fake_ai, ai_config):
    # tsc fails while src/broken.ts exists; the repair deletes it.
    (project.root / "src" / "broken.ts").write_text("export const broken = ;\n", encoding="utf-8")
    project.git_init()
    project.local_bin("jest")
    tsc = project.local_bin("tsc")
    tsc.write_text(
        "#!/bin/sh\nif [ -f src/broken.ts ]; then echo 'error TS1109'; exit 2; fi\nexit 0\n",
        encoding="utf-8",
    )
    tsc.chmod(0o755)
    remote = Remote(documents=rn_documents(), diff=DIFF)
    fake_ai.reply(
        {
            "proposals": [
                {
                    "id": "repair",
                    "title": "remove the broken module",
                    "edits": [{"path": "src/broken.ts", "action": "delete"}],
                }
            ]
        }
    )

    command, outcome = run(
        project,
        remote,
        to_version="0.82.0",
        install=False,
        context={"config": migration_config(config=ai_config)},
    )

    assert not (project.root / "src" / "broken.ts").exists()
    assert command.outcome is not None and command.outcome.ai_fixes == 1
    assert outcome.exit_code == 0
    assert outcome.summary["rolled_back"] is False


def test_a_dirty_tree_is_refused_unless_allowed(project):
    project.git_init(dirty=True)
    remote = Remote(documents=rn_documents(), diff=DIFF)

    _, outcome = run(project, remote, to_version="0.82.0", install=False)

    assert isinstance(outcome.error, RNAgentError)
    assert "uncommitted" in outcome.error.message


def test_an_older_target_is_refused(project):
    project.git_init()
    remote = Remote(documents=rn_documents(), diff=DIFF)

    _, outcome = run(project, remote, to_version="0.80.0", install=False)

    assert isinstance(outcome.error, RNAgentError)
    assert "already on" in outcome.error.message


def test_dry_run_writes_nothing_and_creates_no_branch(project):
    project.git_init()
    manifest_before = (project.root / "package.json").read_text()
    remote = Remote(documents=rn_documents(), diff=DIFF)

    command, outcome = run(
        project, remote, to_version="0.82.0", install=False, context={"dry_run": True}
    )

    assert (project.root / "package.json").read_text() == manifest_before
    assert not (project.root / ".rn-agent" / "migration-history.json").exists()
    assert command.report is not None and command.report.branch is None
    assert outcome.exit_code == 0
