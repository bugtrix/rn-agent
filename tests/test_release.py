"""``rn-agent docs`` and ``rn-agent release``.

For ``docs``: the model may write exactly one file, and nothing else.
For ``release``: every place a React Native app states its version is found and
reported (including the ones that are missing), the changelog says where it came
from, and the agent runs no git write - the checklist does that part.
"""

from __future__ import annotations

import json

from rn_agent.commands.docs import DocsCommand
from rn_agent.commands.release import ReleaseCommand
from rn_agent.errors import RNAgentError

DOCS_PATH = "docs/PROJECT.md"
DOCS_BODY = "# Demo app\n\nA React Native app.\n"


def docs_reply(path: str = DOCS_PATH, content: str = DOCS_BODY, extra=None):
    edits = [{"path": path, "action": "create", "content": content, "reason": "docs"}]
    if extra:
        edits.append(extra)
    return {"proposals": [{"id": "docs", "title": "Write the docs", "edits": edits}]}


# ---------------------------------------------------------------------------
# docs
# ---------------------------------------------------------------------------
def run_docs(project, config, **kwargs):
    context = project.scanned(config=config, command="docs", assume_yes=True, **kwargs.pop("context", {}))
    command = DocsCommand(context, **kwargs)
    command.quiet = True
    return command, command.run()


def test_docs_writes_the_named_file(project, fake_ai, ai_config):
    fake_ai.reply(docs_reply())

    command, outcome = run_docs(project, ai_config)

    assert (project.root / DOCS_PATH).read_text() == DOCS_BODY
    assert outcome.exit_code == 0
    assert outcome.summary["output"] == DOCS_PATH
    assert outcome.summary["bytes"] == len(DOCS_BODY.encode())


def test_the_prompt_carries_the_facts_and_the_current_file(project, fake_ai, ai_config):
    target = project.root / DOCS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Old title\n\nStale prose.\n", encoding="utf-8")
    fake_ai.reply(docs_reply(content="# New title\n"))

    command, outcome = run_docs(project, ai_config)

    prompt = fake_ai.last_prompt
    assert "React Native 0.81.0" in prompt
    assert "Stale prose" in prompt
    assert outcome.summary["updated"] is True


def test_an_edit_outside_the_output_is_refused(project, fake_ai, ai_config):
    before = (project.root / "src" / "services" / "api.ts").read_text()
    fake_ai.reply(
        docs_reply(
            extra={
                "path": "src/services/api.ts",
                "action": "modify",
                "content": "// documented",
            }
        )
    )

    command, _ = run_docs(project, ai_config)

    assert (project.root / "src" / "services" / "api.ts").read_text() == before
    assert [item.rule for item in command.report.refused] == ["docs.single-output"]
    assert command.report.applied == [DOCS_PATH]


def test_an_unknown_section_is_refused_before_the_model(project, fake_ai, ai_config):
    command, outcome = run_docs(project, ai_config, sections=("vibes",))

    assert isinstance(outcome.error, RNAgentError)
    assert "vibes" in outcome.error.message
    assert fake_ai.calls == []


def test_docs_dry_run_writes_nothing(project, fake_ai, ai_config):
    fake_ai.reply(docs_reply())

    command, _ = run_docs(project, ai_config, context={"dry_run": True})

    assert not (project.root / DOCS_PATH).exists()
    assert not (project.root / ".rn-agent" / "cache" / "docs-report.json").exists()


def test_a_custom_output_path_is_honoured(project, fake_ai, ai_config):
    fake_ai.reply(docs_reply(path="ARCHITECTURE.md"))

    command, _ = run_docs(project, ai_config, output="ARCHITECTURE.md")

    assert (project.root / "ARCHITECTURE.md").is_file()
    assert command.report.applied == ["ARCHITECTURE.md"]


def test_an_empty_document_is_a_failure(project, fake_ai, ai_config):
    fake_ai.reply(docs_reply(content="   \n"))

    command, outcome = run_docs(project, ai_config)

    assert outcome.exit_code == 1
    assert any("is empty" in note for note in command.report.notes)



# ---------------------------------------------------------------------------
# release
# ---------------------------------------------------------------------------
def run_release(project, **kwargs):
    config = kwargs.pop("config", None)
    context_kwargs = kwargs.pop("context", {})
    context = project.scanned(
        command="release", assume_yes=True, config=config, **context_kwargs
    )
    command = ReleaseCommand(context, **kwargs)
    command.quiet = True
    return command, command.run()


def ios_versions(project, *, marketing: str = "1.0.0", build: str = "3") -> None:
    pbxproj = project.root / "ios" / "Demo.xcodeproj" / "project.pbxproj"
    pbxproj.write_text(
        "objects = {\n"
        "    IPHONEOS_DEPLOYMENT_TARGET = 15.1;\n"
        f"    MARKETING_VERSION = {marketing};\n"
        f"    CURRENT_PROJECT_VERSION = {build};\n"
        "    PRODUCT_BUNDLE_IDENTIFIER = com.demo.app;\n"
        "};\n",
        encoding="utf-8",
    )


def android_versions(project, *, name: str = "1.0.0", code: int = 7) -> None:
    gradle = project.root / "android" / "app" / "build.gradle"
    gradle.write_text(
        gradle.read_text().replace(
            "    defaultConfig {",
            f'    defaultConfig {{\n        versionCode {code}\n        versionName "{name}"',
        ),
        encoding="utf-8",
    )


def test_patch_minor_and_major_bumps(project):
    project.git_init()

    for bump, expected in (("patch", "1.0.1"), ("minor", "1.1.0"), ("major", "2.0.0")):
        command, outcome = run_release(project, bump=bump, changelog=False)
        assert command.report is not None
        assert command.report.next_version == expected, bump
        # Restore for the next iteration.
        payload = json.loads((project.root / "package.json").read_text())
        payload["version"] = "1.0.0"
        (project.root / "package.json").write_text(json.dumps(payload, indent=2))
        _ = outcome


def test_the_version_comes_from_the_manifest_not_a_stale_context(project):
    project.git_init()
    context = project.scanned(command="release", assume_yes=True)
    # Someone bumped package.json after the scan (an earlier release run).
    payload = json.loads((project.root / "package.json").read_text())
    payload["version"] = "2.5.0"
    (project.root / "package.json").write_text(json.dumps(payload, indent=2))

    command = ReleaseCommand(context, bump="patch", changelog=False, force=True)
    command.quiet = True
    command.run()

    assert command.report is not None
    assert command.report.current_version == "2.5.0"
    assert command.report.next_version == "2.5.1"



def test_an_explicit_version_wins(project):
    project.git_init()

    command, _ = run_release(project, version="4.2.0", changelog=False)

    assert json.loads((project.root / "package.json").read_text())["version"] == "4.2.0"
    assert command.report.bump.value == "explicit"


def test_a_bad_explicit_version_is_refused(project):
    project.git_init()

    _, outcome = run_release(project, version="tomorrow", changelog=False)

    assert isinstance(outcome.error, RNAgentError)
    assert "semantic version" in outcome.error.message


def test_android_and_ios_versions_are_updated(project):
    android_versions(project)
    ios_versions(project)
    project.git_init()

    command, _ = run_release(project, bump="minor", changelog=False)

    gradle = (project.root / "android" / "app" / "build.gradle").read_text()
    assert 'versionName "1.1.0"' in gradle
    assert "versionCode 8" in gradle

    pbxproj = (project.root / "ios" / "Demo.xcodeproj" / "project.pbxproj").read_text()
    assert "MARKETING_VERSION = 1.1.0;" in pbxproj
    assert "CURRENT_PROJECT_VERSION = 4;" in pbxproj

    labels = {(change.file, change.label) for change in command.report.changes}
    assert ("android/app/build.gradle", "versionCode") in labels
    assert ("ios/Demo.xcodeproj/project.pbxproj", "MARKETING_VERSION") in labels


def test_a_platform_without_a_version_field_is_reported(project):
    ios_versions(project)
    project.git_init()

    command, _ = run_release(project, bump="patch", changelog=False)

    assert any("versionName" in note for note in command.report.notes)


def test_the_changelog_falls_back_to_commit_subjects(project):
    project.git_init()
    commit(project, "feat: add the orders screen")

    command, _ = run_release(project, bump="patch")

    changelog = (project.root / "CHANGELOG.md").read_text()
    assert "## 1.0.1" in changelog
    assert "feat: add the orders screen" in changelog
    assert command.report.changelog_source == "commits"


def test_the_model_writes_the_changelog_when_configured(project, fake_ai, ai_config):
    project.git_init()
    commit(project, "feat: orders screen")
    fake_ai.reply({"entries": ["Add an orders screen", "Speed up the list"]})

    command, _ = run_release(project, bump="patch", config=ai_config)

    changelog = (project.root / "CHANGELOG.md").read_text()
    assert "Add an orders screen" in changelog
    assert command.report.changelog_source == "model"


def test_existing_changelog_content_is_preserved(project):
    (project.root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 0.9.0 - 2024-01-01\n\n- old news\n", encoding="utf-8"
    )
    project.git_init()
    commit(project, "fix: a bug")

    run_release(project, bump="patch")

    changelog = (project.root / "CHANGELOG.md").read_text()
    assert changelog.index("## 1.0.1") < changelog.index("## 0.9.0")
    assert "old news" in changelog


def test_a_dirty_tree_blocks_the_release(project):
    # git_init(dirty=True) leaves package.json at 1.0.1, uncommitted.
    project.git_init(dirty=True)

    command, outcome = run_release(project, bump="patch", changelog=False)

    assert outcome.exit_code == 1
    assert any("uncommitted" in blocker for blocker in command.report.blockers)
    assert json.loads((project.root / "package.json").read_text())["version"] == "1.0.1"


def test_force_proceeds_past_blockers(project):
    project.git_init(dirty=True)

    command, outcome = run_release(project, bump="patch", changelog=False, force=True)

    assert json.loads((project.root / "package.json").read_text())["version"] == "1.0.2"
    assert outcome.exit_code == 0
    assert command.report.blockers  # still reported


def test_critical_health_findings_block_the_release(project):
    project.git_init()
    commit(project, "feat: something")
    paths = project.paths()
    paths.ensure()
    (paths.cache_dir / "health-report.json").write_text(
        json.dumps(
            {
                "checks": [
                    {"id": "android.sdk", "severity": "critical", "status": "fail"},
                ]
            }
        ),
        encoding="utf-8",
    )

    command, outcome = run_release(project, bump="patch", changelog=False)

    assert outcome.exit_code == 1
    assert any("critical" in blocker for blocker in command.report.blockers)


def test_no_commits_since_the_tag_blocks_the_release(project):
    project.git_init()
    tag(project, "v1.0.0")

    command, outcome = run_release(project, bump="patch", changelog=False)

    assert outcome.exit_code == 1
    assert any("releasable commit" in blocker for blocker in command.report.blockers)


def test_the_checklist_names_the_git_steps_and_nothing_runs_them(project):
    project.git_init()
    commit(project, "feat: something")

    command, _ = run_release(project, bump="patch", changelog=False)

    checklist = " ".join(command.report.checklist)
    assert "git tag v1.0.1" in checklist
    assert "git push" in checklist
    executed = " ".join(result.command for result in command.context.runner.history)
    for forbidden in ("git tag", "git commit", "git push", "git reset"):
        assert forbidden not in executed


def test_release_dry_run_writes_nothing(project):
    project.git_init()
    commit(project, "feat: something")

    command, outcome = run_release(
        project, bump="patch", changelog=False, context={"dry_run": True}
    )

    assert json.loads((project.root / "package.json").read_text())["version"] == "1.0.0"
    assert not (project.root / ".rn-agent" / "cache" / "release-report.json").exists()
    assert command.report is not None
    assert command.report.next_version == "1.0.1"
    assert outcome.exit_code == 0


def test_the_release_report_is_written(project):
    project.git_init()
    commit(project, "feat: something")

    _, outcome = run_release(project, bump="patch", changelog=False)

    path = project.root / ".rn-agent" / "cache" / "release-report.json"
    payload = json.loads(path.read_text())
    assert payload["next_version"] == "1.0.1"
    assert "package.json" in payload["confirmed"]
    assert outcome.summary["report"] == str(path)


# --- git helpers -----------------------------------------------------------
def commit(project, subject: str) -> None:
    import subprocess

    (project.root / "src" / f"{abs(hash(subject))}.ts").write_text("export const x = 1;\n")
    subprocess.run(["git", "add", "-A"], cwd=project.root, capture_output=True, check=False)
    subprocess.run(
        ["git", "commit", "-qm", subject], cwd=project.root, capture_output=True, check=False
    )


def tag(project, name: str) -> None:
    import subprocess

    subprocess.run(["git", "tag", name], cwd=project.root, capture_output=True, check=False)
