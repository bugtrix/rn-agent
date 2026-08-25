"""``rn-agent fix``: apply, prove, and undo when the proof fails.

The rollback path is the most important behaviour in the whole agent: a fix that
breaks the build must leave the project byte-identical to how it was found. Every
test here that touches validation checks the bytes, not just the exit code.
"""

from __future__ import annotations

import json

from rn_agent.commands.fix import FixCommand
from rn_agent.errors import RNAgentError
from rn_agent.knowledge.store import KnowledgeStore

FIXED = "export const Button = () => null;\n"
TARGET = "src/components/Button.tsx"


def proposal(path: str = TARGET, content: str = FIXED, **overrides):
    payload = {
        "id": "fix-button",
        "title": "Memoise the handler",
        "summary": "wrap the callback",
        "risk": "low",
        "edits": [{"path": path, "action": "modify", "content": content, "reason": "the fix"}],
    }
    payload.update(overrides)
    return {"proposals": [payload], "notes": []}


def seed_findings(project, findings, kind: str = "health") -> None:
    """Record findings the way `health`/`review` would."""
    paths = project.paths()
    paths.ensure()
    with KnowledgeStore(paths.knowledge_db) as store:
        run_id = store.start_run("health", dry_run=False, agent_version="test")
        store.record_findings(run_id, kind, findings)


def run(project, **kwargs):
    config = kwargs.pop("config")
    context = project.scanned(config=config, command="fix", assume_yes=True, **kwargs.pop("context", {}))
    command = FixCommand(context, **kwargs)
    command.quiet = True
    return command, command.run()


# --- selecting the work ----------------------------------------------------
def test_fix_needs_a_selector(project, fake_ai, ai_config):
    command, outcome = run(project, config=ai_config)

    assert isinstance(outcome.error, RNAgentError)
    assert "--issue" in (outcome.error.hint or "")
    assert fake_ai.calls == []


def test_fix_by_instruction_writes_the_change(project, fake_ai, ai_config):
    fake_ai.reply(proposal())

    command, outcome = run(
        project, config=ai_config, instruction="Button re-renders too often", checks=()
    )

    assert (project.root / TARGET).read_text() == FIXED
    assert command.report.applied == [TARGET]
    assert outcome.exit_code == 0


def test_fix_by_issue_id_reads_the_recorded_finding(project, fake_ai, ai_config):
    seed_findings(
        project,
        [
            {
                "id": "js.typecheck",
                "title": "TypeScript compiles",
                "detail": "tsc reported 3 errors.",
                "recommendation": "Fix the type errors.",
                "severity": "high",
                "file": TARGET,
            }
        ],
    )
    fake_ai.reply(proposal())

    command, outcome = run(project, config=ai_config, issues=("js.typecheck",), checks=())

    prompt = fake_ai.last_prompt
    assert "js.typecheck" in prompt
    assert "tsc reported 3 errors" in prompt
    # The finding's file is what gets sent as context.
    assert TARGET in prompt
    assert command.report.subject == ["js.typecheck"]
    assert outcome.exit_code == 0


def test_a_findings_exact_lines_are_sent_rather_than_re_derived(project, fake_ai, ai_config):
    """`health` already worked out the XML; the model should not guess it again."""
    seed_findings(
        project,
        [
            {
                "id": "android.permissions.missing",
                "title": "Permissions match installed modules",
                "detail": "android.permission.CAMERA is required by react-native-vision-camera.",
                "recommendation": "Add this to android/app/src/main/AndroidManifest.xml:",
                "fix": [
                    "<!-- react-native-vision-camera -->",
                    '<uses-permission android:name="android.permission.CAMERA" />',
                ],
                "severity": "high",
                "file": TARGET,
            }
        ],
    )
    fake_ai.reply(proposal())

    run(project, config=ai_config, issues=("android.permissions.missing",), checks=())

    prompt = fake_ai.last_prompt
    assert '<uses-permission android:name="android.permission.CAMERA" />' in prompt
    assert "android.permission.CAMERA" in prompt



def test_an_unknown_issue_id_alone_is_refused(project, fake_ai, ai_config):
    command, outcome = run(project, config=ai_config, issues=("nope.nothing",))

    assert isinstance(outcome.error, RNAgentError)
    assert "nope.nothing" in outcome.error.message
    assert fake_ai.calls == []


def test_a_mix_of_known_and_unknown_ids_proceeds_and_reports(project, fake_ai, ai_config):
    seed_findings(project, [{"id": "js.lint", "title": "ESLint", "file": TARGET}])
    fake_ai.reply(proposal())

    command, _ = run(
        project, config=ai_config, issues=("js.lint", "js.ghost"), checks=()
    )

    assert command.report.unknown_issues == ["js.ghost"]
    assert any("js.ghost" in note for note in command.report.notes)


def test_fix_can_use_review_findings_too(project, fake_ai, ai_config):
    seed_findings(
        project, [{"id": "unstable-callback", "title": "Unstable callback", "file": TARGET}], kind="review"
    )
    fake_ai.reply(proposal())

    command, _ = run(project, config=ai_config, issues=("unstable-callback",), checks=())

    assert command.report.subject == ["unstable-callback"]


# --- validation and rollback ----------------------------------------------
def test_failed_validation_rolls_the_fix_back(project, fake_ai, ai_config):
    original = (project.root / TARGET).read_text()
    project.local_bin("tsc", exit_code=2, output="src/components/Button.tsx(1,1): error TS1005")
    fake_ai.reply(proposal())

    command, outcome = run(
        project, config=ai_config, instruction="fix the button", checks=("typecheck",)
    )

    assert (project.root / TARGET).read_text() == original
    assert command.report.rolled_back is True
    assert outcome.exit_code == 1


def test_keep_on_failure_leaves_the_change_and_still_fails(project, fake_ai, ai_config):
    project.local_bin("tsc", exit_code=2, output="error TS1005")
    fake_ai.reply(proposal())

    command, outcome = run(
        project,
        config=ai_config,
        instruction="fix the button",
        checks=("typecheck",),
        keep_on_failure=True,
    )

    assert (project.root / TARGET).read_text() == FIXED
    assert command.report.rolled_back is False
    assert outcome.exit_code == 1


def test_passing_validation_keeps_the_change(project, fake_ai, ai_config):
    project.local_bin("tsc")
    fake_ai.reply(proposal())

    command, outcome = run(
        project, config=ai_config, instruction="fix the button", checks=("typecheck",)
    )

    assert (project.root / TARGET).read_text() == FIXED
    assert command.report.validated is True
    assert outcome.exit_code == 0


def test_no_checks_means_no_proof_claimed(project, fake_ai, ai_config):
    fake_ai.reply(proposal())

    command, _ = run(project, config=ai_config, instruction="fix", checks=())

    assert command.report.validation is None
    assert command.report.validated is None


# --- the rules still win ---------------------------------------------------
def test_a_dependency_edit_is_refused(project, fake_ai, ai_config):
    before = (project.root / "package.json").read_text()
    fake_ai.reply(proposal(path="package.json", content='{"name": "hacked"}'))

    command, outcome = run(project, config=ai_config, instruction="add a library", checks=())

    assert (project.root / "package.json").read_text() == before
    assert command.report.applied == []
    assert [item.rule for item in command.report.refused] == ["forbid_new_dependencies"]
    assert outcome.exit_code == 1


def test_allow_deps_permits_the_package_json_edit(project, fake_ai, ai_config):
    fake_ai.reply(proposal(path="package.json", content='{"name": "demo-app"}\n'))

    command, _ = run(
        project,
        config=ai_config,
        instruction="add a library",
        checks=(),
        allow_dependencies=True,
    )

    assert command.report.applied == ["package.json"]


def test_a_native_edit_needs_allow_native(project, fake_ai, ai_config):
    fake_ai.reply(proposal(path="android/gradle.properties", content="newArchEnabled=true\n"))

    refused, _ = run(project, config=ai_config, instruction="enable new arch", checks=())
    assert refused.report.applied == []

    fake_ai.reply(proposal(path="android/gradle.properties", content="newArchEnabled=true\n"))
    allowed, _ = run(
        project,
        config=ai_config,
        instruction="enable new arch",
        checks=(),
        allow_native=True,
    )
    assert allowed.report.applied == ["android/gradle.properties"]


def test_file_flag_permits_that_native_file_only(project, fake_ai, ai_config):
    manifest = "android/app/src/main/AndroidManifest.xml"
    fake_ai.reply(proposal(path=manifest, content="<manifest />\n"))

    command, _ = run(
        project,
        config=ai_config,
        instruction="add a permission",
        files=(manifest,),
        checks=(),
    )
    assert command.report.applied == [manifest]

    fake_ai.reply(proposal(path="android/gradle.properties", content="newArchEnabled=true\n"))
    other, _ = run(
        project,
        config=ai_config,
        instruction="enable new arch",
        files=(manifest,),
        checks=(),
    )
    assert other.report.applied == []
    assert [item.rule for item in other.report.refused] == [
        "forbid_native_edits_without_confirmation"
    ]


def test_allow_native_paths_in_rules_yaml_permits_the_edit(project, fake_ai, ai_config):
    manifest = "android/app/src/main/AndroidManifest.xml"
    paths = project.paths()
    paths.ensure()
    paths.rules_file.write_text(
        "rules:\n"
        "  forbid_native_edits_without_confirmation: true\n"
        "  allow_native_paths:\n"
        f"    - {manifest}\n",
        encoding="utf-8",
    )
    fake_ai.reply(proposal(path=manifest, content="<manifest />\n"))

    command, _ = run(
        project, config=ai_config, instruction="add a permission", checks=()
    )
    assert command.report.applied == [manifest]


# --- dry run and reporting -------------------------------------------------
def test_dry_run_writes_nothing(project, fake_ai, ai_config):
    original = (project.root / TARGET).read_text()
    fake_ai.reply(proposal())
    context = project.scanned(
        config=ai_config, command="fix", assume_yes=True, dry_run=True
    )
    command = FixCommand(context, instruction="fix the button", checks=("typecheck",))
    command.quiet = True

    outcome = command.run()

    assert (project.root / TARGET).read_text() == original
    assert not (project.root / ".rn-agent" / "cache" / "fix-report.json").exists()
    assert outcome.exit_code == 0


def test_the_report_records_what_happened(project, fake_ai, ai_config):
    project.local_bin("tsc")
    fake_ai.reply(proposal())

    _, outcome = run(
        project, config=ai_config, instruction="fix the button", checks=("typecheck",)
    )

    path = project.root / ".rn-agent" / "cache" / "fix-report.json"
    payload = json.loads(path.read_text())
    assert payload["task"] == "fix"
    assert payload["applied"] == [TARGET]
    assert payload["validation"]["steps"][0]["name"] == "typecheck"
    assert outcome.summary["report"] == str(path)


def test_a_backup_is_written_before_the_change(project, fake_ai, ai_config):
    fake_ai.reply(proposal())

    run(project, config=ai_config, instruction="fix the button", checks=())

    backups = list((project.root / ".rn-agent" / "cache" / "backups").rglob("Button.tsx"))
    assert backups, "the previous bytes must be recoverable"
