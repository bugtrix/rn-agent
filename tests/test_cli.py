"""CLI behaviour: exit codes, JSON output, dry-run, and the written state."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from rn_agent.cli.app import app
from rn_agent.constants import APP_VERSION

runner = CliRunner()


def invoke(*args: str):
    return runner.invoke(app, list(args))


# --- basics ----------------------------------------------------------------
def test_no_arguments_opens_the_terminal(project):
    """Bare `rn-agent` is the interactive terminal, not a usage message.

    Under pytest there is no tty, so the terminal prints its status and the
    command list instead of taking over the screen - which is exactly what a
    piped session or a CI job should get.
    """
    result = invoke("--path", str(project.root))

    assert result.exit_code == 0, result.output
    assert "React Native Agent" in result.output
    assert "/login" in result.output and "/migrate" in result.output
    assert "not interactive" in result.output


def test_no_arguments_outside_a_project_explains_itself(tmp_path: Path):
    result = invoke("--path", str(tmp_path))

    assert result.exit_code == 2
    assert "package.json" in result.output


def test_help_still_lists_every_command():
    result = invoke("--help")

    assert result.exit_code == 0
    for name in ("scan", "health", "review", "migrate", "release"):
        assert name in result.output


def test_version_flag():
    result = invoke("--version")
    assert result.exit_code == 0
    assert APP_VERSION in result.output


def test_outside_a_react_native_project_exits_with_a_hint(tmp_path: Path):
    result = invoke("--path", str(tmp_path), "scan")
    assert result.exit_code == 2
    assert "package.json" in result.output


def test_non_react_native_node_project_is_rejected(tmp_path: Path):
    (tmp_path / "package.json").write_text(json.dumps({"name": "api", "dependencies": {}}))
    result = invoke("--path", str(tmp_path), "scan")
    assert result.exit_code == 2
    assert "react-native" in result.output


# --- scan ------------------------------------------------------------------
def test_scan_creates_the_agent_directory(project):
    result = invoke("--path", str(project.root), "scan", "--no-tools")
    assert result.exit_code == 0, result.output

    agent_dir = project.root / ".rn-agent"
    for name in (
        "config.yaml",
        "project-context.json",
        "architecture.yaml",
        "dependencies.json",
        "rules.yaml",
        "decisions.md",
        ".gitignore",
    ):
        assert (agent_dir / name).is_file(), name
    for name in ("cache", "logs", "knowledge"):
        assert (agent_dir / name).is_dir(), name


def test_scan_context_contents(project):
    invoke("--path", str(project.root), "scan", "--no-tools")
    payload = json.loads((project.root / ".rn-agent" / "project-context.json").read_text())
    assert payload["react_native"]["version"] == "0.81.0"
    assert payload["package_manager"]["name"] == "yarn"
    assert payload["android"]["present"] is True
    assert payload["ios"]["present"] is True
    assert payload["architecture"]["language"] == "typescript"
    assert payload["schema_version"] == 1
    assert payload["agent_version"] == APP_VERSION


def test_scan_output_mentions_key_facts(project):
    result = invoke("--path", str(project.root), "scan", "--no-tools")
    assert "React Native 0.81.0" in result.output
    assert "yarn" in result.output
    assert "Architecture (inferred)" in result.output


def test_scan_dry_run_writes_nothing(project):
    result = invoke("--path", str(project.root), "--dry-run", "scan", "--no-tools")
    assert result.exit_code == 0
    assert "dry run" in result.output.lower()
    assert not (project.root / ".rn-agent").exists()


def test_scan_json_output(project):
    result = invoke("--path", str(project.root), "--json", "scan", "--no-tools")
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["react_native"]["version"] == "0.81.0"
    assert isinstance(payload["dependencies"], list)


def test_scan_show_reads_the_stored_context(project):
    invoke("--path", str(project.root), "scan", "--no-tools")
    result = invoke("--path", str(project.root), "scan", "--show")
    assert result.exit_code == 0
    assert "React Native 0.81.0" in result.output


def test_scan_show_without_a_scan_explains_itself(project):
    result = invoke("--path", str(project.root), "scan", "--show")
    assert result.exit_code == 3
    assert "rn-agent scan" in result.output


def test_scan_records_the_run_in_the_knowledge_store(project):
    invoke("--path", str(project.root), "scan", "--no-tools")
    from rn_agent.knowledge.store import KnowledgeStore

    with KnowledgeStore(project.root / ".rn-agent" / "knowledge" / "knowledge.db") as store:
        runs = store.recent_runs()
        assert runs and runs[0]["command"] == "scan"
        assert runs[0]["status"] == "ok"


def test_scan_seeds_rules_from_the_detected_architecture(project):
    project.write_package_json(dependencies={"redux-saga": "^1.3.0", "@reduxjs/toolkit": "^2.2.0"})
    invoke("--path", str(project.root), "scan", "--no-tools")
    import yaml

    rules = yaml.safe_load((project.root / ".rn-agent" / "rules.yaml").read_text())
    assert "redux-saga" in rules["rules"]["allowed_state_management"]
    assert rules["rules"]["forbid_new_dependencies"] is True
    assert rules["rules"]["allow_native_paths"] == []


def test_scan_does_not_overwrite_edited_rules(project):
    invoke("--path", str(project.root), "scan", "--no-tools")
    rules_file = project.root / ".rn-agent" / "rules.yaml"
    rules_file.write_text("rules:\n  custom: true\n")
    invoke("--path", str(project.root), "scan", "--no-tools")
    assert "custom: true" in rules_file.read_text()


# --- health ----------------------------------------------------------------
def test_health_runs_without_a_previous_scan(project):
    """health must work standalone by refreshing the shared context itself."""
    result = invoke("--path", str(project.root), "health")
    assert result.exit_code in (0, 1)
    assert "Health Score" in result.output


def test_health_reports_a_score_and_areas(project):
    project.git_init()
    invoke("--path", str(project.root), "scan", "--no-tools")
    result = invoke("--path", str(project.root), "health")
    assert "Health Score" in result.output
    assert "By area" in result.output
    assert "React Native" in result.output


def test_health_exit_code_is_one_when_critical(project):
    project.android(compile_sdk=34, target_sdk=35)  # target above compile -> critical
    result = invoke("--path", str(project.root), "health")
    assert result.exit_code == 1
    assert "Critical" in result.output


def test_health_exit_code_zero_on_a_clean_project(project):
    from rn_agent.knowledge.data import load_knowledge_data

    requirement = load_knowledge_data().required_target_sdk()
    target = requirement.target_sdk if requirement else 35
    project.git_init()
    project.installed("react-native", "0.81.0", peer={"react": "^19.1.0"}, engines={"node": ">=18"})
    project.installed("react", "19.1.0")
    project.android(compile_sdk=target, target_sdk=target)
    project.ios(pods_rn="0.81.0")
    result = invoke("--path", str(project.root), "health")
    assert result.exit_code == 0, result.output


def test_health_fail_under_threshold(project):
    result = invoke("--path", str(project.root), "health", "--fail-under", "100")
    assert result.exit_code == 1


def test_health_area_filter(project):
    result = invoke("--path", str(project.root), "health", "--area", "ios")
    assert result.exit_code in (0, 1)
    assert "iOS" in result.output
    assert "Android" not in result.output


def test_health_json_output(project):
    result = invoke("--path", str(project.root), "--json", "health")
    assert result.exit_code in (0, 1)
    payload = json.loads(result.output)
    assert "checks" in payload
    assert any(check["id"] for check in payload["checks"])


def test_health_writes_a_report_file(project):
    invoke("--path", str(project.root), "health")
    report = project.root / ".rn-agent" / "cache" / "health-report.json"
    assert report.is_file()
    payload = json.loads(report.read_text())
    assert payload["checks"]


def test_health_dry_run_writes_nothing(project):
    invoke("--path", str(project.root), "--dry-run", "health")
    assert not (project.root / ".rn-agent" / "cache" / "health-report.json").exists()


def test_health_verbose_shows_evidence(project):
    project.lockfile("package-lock.json")
    result = invoke("--path", str(project.root), "--verbose", "health")
    assert "lockfiles" in result.output


def test_health_logs_to_the_project(project):
    invoke("--path", str(project.root), "health")
    log = project.root / ".rn-agent" / "logs" / "health.log"
    assert log.is_file()
    assert "health" in log.read_text()


# --- info ------------------------------------------------------------------
def test_info_before_and_after_scan(project):
    result = invoke("--path", str(project.root), "info")
    assert result.exit_code == 0
    assert "rn-agent scan" in result.output

    invoke("--path", str(project.root), "scan", "--no-tools")
    result = invoke("--path", str(project.root), "info")
    assert "project-context.json" in result.output
    assert "0.81.0" in result.output


def test_info_reports_no_ai_provider_yet(project):
    result = invoke("--path", str(project.root), "info")
    assert "not configured" in result.output


# --- shared brain ----------------------------------------------------------
def test_commands_share_one_context_file(project):
    """§2: health must consume what scan produced, not rescan from scratch."""
    invoke("--path", str(project.root), "scan", "--no-tools")
    context_file = project.root / ".rn-agent" / "project-context.json"
    before = context_file.read_text()
    invoke("--path", str(project.root), "health")
    assert context_file.read_text() == before


def test_health_refresh_updates_the_context(project):
    invoke("--path", str(project.root), "scan", "--no-tools")
    context_file = project.root / ".rn-agent" / "project-context.json"
    payload = json.loads(context_file.read_text())
    assert payload["android"]["target_sdk"] == 35

    project.android(compile_sdk=36, target_sdk=36)
    invoke("--path", str(project.root), "health", "--refresh")
    payload = json.loads(context_file.read_text())
    assert payload["android"]["target_sdk"] == 36


def test_health_drops_a_resolved_finding_without_refresh(project):
    """A manifest fix must leave the next health run, not wait 24 hours."""
    import os

    project.write_package_json(dependencies={"@react-native-firebase/messaging": "^21.0.0"})
    project.android(permissions=("android.permission.INTERNET",))
    invoke("--path", str(project.root), "scan", "--no-tools")
    before = invoke("--path", str(project.root), "health")
    assert "POST_NOTIFICATIONS" in before.output

    context_file = project.root / ".rn-agent" / "project-context.json"
    os.utime(context_file, (1, 1))
    project.android(
        permissions=("android.permission.INTERNET", "android.permission.POST_NOTIFICATIONS")
    )
    after = invoke("--path", str(project.root), "health")
    assert "POST_NOTIFICATIONS is required" not in after.output


def test_health_json_works_in_dry_run(project):
    """--json must emit the full report even when nothing is written."""
    result = invoke("--path", str(project.root), "--dry-run", "--json", "health")
    payload = json.loads(result.output)
    assert payload["checks"]
    assert "rn_version" in payload
    assert not (project.root / ".rn-agent" / "cache" / "health-report.json").exists()
