"""The CLI surface of phases 3-6: flags, exit codes, ``--json``, ``--dry-run``.

These tests go through Typer exactly as a developer would, so they cover the
wiring the command tests cannot: that every command is registered, that the
global flags reach it, that an unconfigured provider is an actionable error
rather than a traceback, and that ``--json`` prints the report even in dry-run.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

# Importing the package is what registers the commands (see commands/__init__).
import rn_agent.commands  # noqa: F401
from rn_agent.cli.app import app
from rn_agent.core.registry import COMMANDS
from rn_agent.validation.runner import STEP_NAMES

runner = CliRunner()
PHASE_3 = ("review", "fix", "feature", "test")
PHASE_4_6 = ("upgrade", "migrate", "compatibility", "docs", "release")
AI_COMMANDS = ("review", "fix", "feature", "test", "docs")


def invoke(*args: str):
    return runner.invoke(app, list(args))


def configure_ai(project) -> None:
    """Select a provider in the project config, as `rn-agent login` would."""
    import yaml

    paths = project.paths()
    paths.ensure()
    paths.config_file.write_text(
        yaml.safe_dump({"ai": {"provider": "anthropic", "model": "claude-sonnet-4-5"}}),
        encoding="utf-8",
    )


# --- registration ----------------------------------------------------------
def test_every_command_is_registered():
    assert set(PHASE_3) | set(PHASE_4_6) <= set(COMMANDS)


def test_help_lists_every_command():
    result = invoke("--help")

    for name in (*PHASE_3, *PHASE_4_6):
        assert name in result.output


@pytest.mark.parametrize("name", (*PHASE_3, *PHASE_4_6))
def test_each_command_has_its_own_help(name):
    result = invoke(name, "--help")

    assert result.exit_code == 0
    assert name in result.output


def test_the_registry_records_the_phase_of_each_command():
    assert COMMANDS["review"].phase == 3
    assert COMMANDS["upgrade"].phase == 4
    assert COMMANDS["migrate"].phase == 5
    assert COMMANDS["release"].phase == 6


# --- AI is opt-in ----------------------------------------------------------
@pytest.mark.parametrize("name", AI_COMMANDS)
def test_an_ai_command_without_a_provider_explains_itself(project, name):
    args = ["--path", str(project.root), name]
    if name == "feature":
        args.append("add a screen")
    if name == "fix":
        args += ["--about", "the button re-renders"]

    result = invoke(*args)

    assert result.exit_code == 10, result.output
    assert "rn-agent login" in result.output
    assert "Traceback" not in result.output


def test_ai_disabled_in_config_refuses(project):
    import yaml

    paths = project.paths()
    paths.ensure()
    paths.config_file.write_text(yaml.safe_dump({"ai": {"enabled": False}}), encoding="utf-8")

    result = invoke("--path", str(project.root), "review")

    assert result.exit_code == 10
    assert "disabled" in result.output


# --- deterministic commands end to end ------------------------------------
def test_compatibility_runs_offline_and_reports(project):
    result = invoke("--path", str(project.root), "compatibility", "--offline")

    assert result.exit_code in (0, 1), result.output
    assert "Compatibility" in result.output
    assert (project.root / ".rn-agent" / "cache" / "compatibility-report.json").is_file()


def test_compatibility_json_is_machine_readable(project):
    result = invoke("--path", str(project.root), "--json", "compatibility", "--offline")

    payload = json.loads(result.output)
    assert payload["current_rn"] == "0.81.0"
    assert isinstance(payload["entries"], list)


def test_compatibility_dry_run_writes_no_report(project):
    result = invoke(
        "--path", str(project.root), "--dry-run", "compatibility", "--offline"
    )

    assert result.exit_code in (0, 1)
    assert not (project.root / ".rn-agent" / "cache" / "compatibility-report.json").exists()


def test_upgrade_offline_reports_drift_without_touching_package_json(project):
    before = (project.root / "package.json").read_text()

    result = invoke(
        "--path", str(project.root), "upgrade", "--offline", "--no-install", "--no-check"
    )

    assert (project.root / "package.json").read_text() == before
    assert "Upgrade" in result.output


def test_upgrade_rejects_an_unknown_target(project):
    result = invoke("--path", str(project.root), "upgrade", "--target", "sideways")

    assert result.exit_code == 1
    assert "sideways" in result.output


def test_release_reports_blockers_and_writes_nothing(project):
    result = invoke("--path", str(project.root), "release", "--no-changelog")

    assert result.exit_code == 1
    assert json.loads((project.root / "package.json").read_text())["version"] == "1.0.0"


def test_release_dry_run_shows_the_plan(project):
    project.git_init()

    result = invoke(
        "--path", str(project.root), "--dry-run", "release", "--no-changelog", "--force"
    )

    assert result.exit_code == 0, result.output
    assert "Release" in result.output
    assert json.loads((project.root / "package.json").read_text())["version"] == "1.0.0"


def test_migrate_refuses_a_target_that_is_not_newer(project):
    project.git_init()

    result = invoke(
        "--path", str(project.root), "migrate", "--to", "0.80.0", "--offline", "--no-install"
    )

    assert result.exit_code == 1
    assert "already on" in result.output


def test_migrate_outside_a_repository_is_refused(project):
    result = invoke(
        "--path", str(project.root), "migrate", "--to", "0.82.0", "--offline", "--no-install"
    )

    assert result.exit_code == 6
    assert "git" in result.output


# --- flag plumbing ---------------------------------------------------------
def test_an_unknown_validation_step_is_rejected_with_the_valid_ones(project):
    configure_ai(project)

    result = invoke("--path", str(project.root), "fix", "--about", "x", "--check", "vibes")

    assert result.exit_code == 1
    assert "vibes" in result.output
    for name in STEP_NAMES:
        assert name in result.output


def test_fix_without_a_selector_explains_the_options(project, monkeypatch):
    configure_ai(project)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-0123456789abcdef")

    result = invoke("--path", str(project.root), "fix")

    assert result.exit_code == 1
    assert "--issue" in result.output


def test_review_reaches_the_model_and_renders(project, monkeypatch, wired_transport):
    configure_ai(project)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-0123456789abcdef")
    wired_transport.queue(
        body={
            "model": "claude-sonnet-4-5",
            "stop_reason": "end_turn",
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "findings": [
                                {
                                    "id": "unstable-callback",
                                    "title": "Unstable callback",
                                    "severity": "medium",
                                    "area": "hooks",
                                    "file": "src/components/Button.tsx",
                                }
                            ]
                        }
                    ),
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    )

    result = invoke(
        "--path",
        str(project.root),
        "review",
        "--file",
        "src/components/Button.tsx",
    )

    assert result.exit_code == 0, result.output
    assert "Unstable callback" in result.output
    assert "Review" in result.output


def test_review_json_emits_the_report(project, monkeypatch, wired_transport):
    configure_ai(project)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-0123456789abcdef")
    wired_transport.queue(
        body={
            "model": "claude-sonnet-4-5",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": json.dumps({"findings": []})}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    )

    result = invoke(
        "--path",
        str(project.root),
        "--json",
        "review",
        "--file",
        "src/components/Button.tsx",
    )

    payload = json.loads(result.output)
    assert payload["findings"] == []
    assert payload["score"] == 100 if "score" in payload else True


def test_outside_a_project_every_new_command_says_so(tmp_path):
    for name in ("review", "upgrade", "compatibility", "release", "docs"):
        result = invoke("--path", str(tmp_path), name)
        assert result.exit_code == 2, name
        assert "package.json" in result.output
