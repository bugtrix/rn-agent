"""The shared collaborators: runner, git, file manager, safety, knowledge store, AI."""

from __future__ import annotations

from pathlib import Path

import pytest

from rn_agent.ai.types import Completion, Usage
from rn_agent.auth.session import build_store
from rn_agent.core.paths import AgentPaths
from rn_agent.errors import ConfirmationDeclined, GitError, ProviderError, UnsafePathError
from rn_agent.filesystem.manager import FileManager
from rn_agent.git.manager import GitManager
from rn_agent.knowledge.store import KnowledgeStore
from rn_agent.models.changes import ChangeType, RiskLevel
from rn_agent.models.config import AgentConfig, SafetyConfig
from rn_agent.runner.command_runner import CommandRunner
from rn_agent.safety.manager import SafetyManager
from rn_agent.utils.redaction import is_secret_path, redact, redact_env


# --- command runner --------------------------------------------------------
def test_runner_captures_output(tmp_path: Path):
    runner = CommandRunner(cwd=tmp_path)
    result = runner.run(["echo", "hello"])
    assert result.ok
    assert result.stdout.strip() == "hello"
    assert result.duration_ms >= 0
    assert result.command == "echo hello"


def test_runner_reports_missing_executable(tmp_path: Path):
    runner = CommandRunner(cwd=tmp_path)
    result = runner.run(["definitely-not-a-real-binary-xyz"])
    assert not result.ok
    assert result.executable_missing
    assert result.returncode == 127


def test_runner_reports_failure_without_raising(tmp_path: Path):
    runner = CommandRunner(cwd=tmp_path)
    result = runner.run(["sh", "-c", "exit 3"])
    assert result.returncode == 3
    assert not result.ok


def test_runner_check_raises(tmp_path: Path):
    from rn_agent.errors import CommandExecutionError

    runner = CommandRunner(cwd=tmp_path)
    with pytest.raises(CommandExecutionError):
        runner.run(["sh", "-c", "exit 1"], check=True)


def test_runner_timeout(tmp_path: Path):
    runner = CommandRunner(cwd=tmp_path)
    result = runner.run(["sleep", "5"], timeout=0.2)
    assert result.timed_out
    assert not result.ok


def test_runner_dry_run_skips_execution(tmp_path: Path):
    marker = tmp_path / "created"
    runner = CommandRunner(cwd=tmp_path, dry_run=True)
    result = runner.run(["touch", str(marker)])
    assert result.skipped
    assert result.ok
    assert not marker.exists()


def test_runner_dry_run_still_reads_versions(tmp_path: Path):
    """Reading a tool version changes nothing, so it must work in dry-run."""
    runner = CommandRunner(cwd=tmp_path, dry_run=True)
    assert runner.tool_version("git") is not None


# --- git manager -----------------------------------------------------------
def test_git_detects_non_repository(builder):
    builder.write_package_json()
    manager = GitManager(root=builder.root, runner=CommandRunner(cwd=builder.root))
    assert manager.is_repository() is False
    assert manager.describe().repository is False


def test_git_describes_a_clean_repository(project):
    project.git_init()
    manager = GitManager(root=project.root, runner=CommandRunner(cwd=project.root))
    info = manager.describe()
    assert info.repository is True
    assert info.dirty is False
    assert info.branch
    assert info.last_commit
    assert info.ignores_agent_dir is True


def test_git_detects_dirty_tree(project):
    project.git_init(dirty=True)
    manager = GitManager(root=project.root, runner=CommandRunner(cwd=project.root))
    info = manager.describe()
    assert info.dirty is True
    assert info.modified >= 1
    with pytest.raises(GitError, match="uncommitted"):
        manager.require_clean()


def test_git_detects_untracked_only(project):
    project.git_init()
    (project.root / "new-file.ts").write_text("export const a = 1;\n")
    manager = GitManager(root=project.root, runner=CommandRunner(cwd=project.root))
    status = manager.status()
    assert status.untracked
    assert status.dirty is True
    manager.require_clean(allow_untracked=True)  # must not raise


def test_git_require_repository_raises_outside_git(builder):
    builder.write_package_json()
    manager = GitManager(root=builder.root, runner=CommandRunner(cwd=builder.root))
    with pytest.raises(GitError, match="not a git repository"):
        manager.require_repository()


def test_git_creates_a_unique_branch(project):
    project.git_init()
    manager = GitManager(root=project.root, runner=CommandRunner(cwd=project.root))
    first = manager.create_branch("rn-agent/migrate-0.82")
    second = manager.create_branch("rn-agent/migrate-0.82")
    assert first == "rn-agent/migrate-0.82"
    assert second == "rn-agent/migrate-0.82-2"


def test_git_manager_has_no_destructive_operations():
    """§13: `git reset --hard` / `git clean -fd` must not exist at all."""
    source = Path(GitManager.__module__.replace(".", "/") + ".py")
    text = (Path(__file__).resolve().parents[1] / "src" / source).read_text(encoding="utf-8")
    for forbidden in ("reset", "clean", "checkout -f", "push --force"):
        assert f'"{forbidden}"' not in text


# --- file manager ----------------------------------------------------------
def test_file_manager_creates_and_records(project):
    paths = AgentPaths.for_project(project.root).ensure()
    files = FileManager(paths=paths, command="fix")
    change = files.write("src/new.ts", "export const a = 1;\n", reason="add helper")
    assert (project.root / "src" / "new.ts").read_text() == "export const a = 1;\n"
    assert change.change_type is ChangeType.CREATE
    assert change.applied is True
    assert change.reason == "add helper"
    assert change.command == "fix"
    assert change.after_hash


def test_file_manager_backs_up_before_modifying(project):
    paths = AgentPaths.for_project(project.root).ensure()
    files = FileManager(paths=paths, command="fix")
    target = project.root / "src" / "services" / "api.ts"
    original = target.read_text()

    change = files.write("src/services/api.ts", "// replaced\n", reason="rewrite", risk=RiskLevel.MEDIUM)
    assert change.change_type is ChangeType.MODIFY
    assert change.backup and Path(change.backup).read_text() == original
    assert target.read_text() == "// replaced\n"


def test_file_manager_rollback_restores_everything(project):
    paths = AgentPaths.for_project(project.root).ensure()
    files = FileManager(paths=paths, command="fix")
    target = project.root / "src" / "services" / "api.ts"
    original = target.read_text()

    files.write("src/services/api.ts", "// replaced\n", reason="rewrite")
    files.write("src/brand-new.ts", "// new\n", reason="create")
    restored = files.rollback()

    assert target.read_text() == original
    assert not (project.root / "src" / "brand-new.ts").exists()
    assert len(restored) == 2


def test_file_manager_dry_run_writes_nothing(project):
    paths = AgentPaths.for_project(project.root).ensure()
    files = FileManager(paths=paths, command="fix", dry_run=True)
    change = files.write("src/nope.ts", "// nope\n", reason="preview")
    assert not (project.root / "src" / "nope.ts").exists()
    assert change.applied is False
    assert change.dry_run is True
    assert len(files.changes) == 1


def test_file_manager_refuses_paths_outside_the_project(project):
    paths = AgentPaths.for_project(project.root).ensure()
    files = FileManager(paths=paths, command="fix")
    for escape in ("../outside.ts", "/etc/hosts", "src/../../outside.ts"):
        with pytest.raises(UnsafePathError):
            files.write(escape, "x", reason="attack")


def test_file_manager_skips_identical_content(project):
    paths = AgentPaths.for_project(project.root).ensure()
    files = FileManager(paths=paths, command="fix")
    target = project.root / "src" / "services" / "api.ts"
    change = files.write("src/services/api.ts", target.read_text(), reason="no-op")
    assert change.applied is False
    assert "no change needed" in change.reason


def test_file_manager_summary_and_risk(project):
    paths = AgentPaths.for_project(project.root).ensure()
    files = FileManager(paths=paths, command="feature")
    files.write("src/a.ts", "a\n", reason="x", risk=RiskLevel.LOW)
    files.write("android/app/build.gradle", "// changed\n", reason="native", risk=RiskLevel.HIGH)
    assert files.summary()["total"] == 2
    assert files.changes.highest_risk is RiskLevel.HIGH
    assert files.changes.rollback_available is True


# --- safety ----------------------------------------------------------------
def test_safety_dry_run_never_blocks():
    manager = SafetyManager(config=SafetyConfig(), dry_run=True)
    decision = manager.evaluate(risk=RiskLevel.CRITICAL, file_count=999, rollback_available=False)
    assert decision.allowed
    assert decision.requires_confirmation is False


def test_safety_requires_confirmation_by_default():
    manager = SafetyManager(config=SafetyConfig())
    decision = manager.evaluate(risk=RiskLevel.HIGH, file_count=3, rollback_available=True)
    assert decision.allowed
    assert decision.requires_confirmation is True


def test_safety_auto_applies_low_risk_only_when_enabled():
    config = SafetyConfig(auto_fix_low_risk=True)
    manager = SafetyManager(config=config)
    assert manager.evaluate(risk=RiskLevel.LOW, file_count=1, rollback_available=True).requires_confirmation is False
    assert manager.evaluate(risk=RiskLevel.HIGH, file_count=1, rollback_available=True).requires_confirmation is True


def test_safety_blocks_oversized_operations():
    manager = SafetyManager(config=SafetyConfig(max_files_per_operation=5))
    decision = manager.evaluate(risk=RiskLevel.LOW, file_count=6, rollback_available=True)
    assert decision.blocked
    assert "above the configured limit" in decision.reason


def test_safety_require_raises_when_declined():
    manager = SafetyManager(config=SafetyConfig(), confirmer=lambda question, default: False)
    with pytest.raises(ConfirmationDeclined):
        manager.require("Proceed?")


def test_safety_assume_yes_skips_the_prompt():
    asked: list[str] = []
    manager = SafetyManager(
        config=SafetyConfig(),
        assume_yes=True,
        confirmer=lambda question, default: asked.append(question) or True,
    )
    manager.require("Proceed?")
    assert asked == []


def test_safety_filters_secret_files_from_context():
    manager = SafetyManager(config=SafetyConfig())
    safe, refused = manager.filter_context_files(
        [
            "src/App.tsx",
            ".env",
            ".env.production",
            "android/app/google-services.json",
            "ios/Demo/GoogleService-Info.plist",
            "android/local.properties",
            "certs/key.p12",
        ]
    )
    assert safe == ["src/App.tsx"]
    assert len(refused) == 6


def test_safety_risk_of_paths():
    manager = SafetyManager(config=SafetyConfig())
    assert manager.risk_of(["src/App.tsx"]) is RiskLevel.LOW
    assert manager.risk_of(["babel.config.js"]) is RiskLevel.MEDIUM
    assert manager.risk_of(["android/app/build.gradle"]) is RiskLevel.HIGH
    assert manager.risk_of(["package.json"]) is RiskLevel.HIGH
    assert manager.risk_of(["src/App.tsx", "ios/Podfile"]) is RiskLevel.HIGH


# --- redaction -------------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.staging",
        "keys/private.pem",
        "AuthKey_ABC123.p8",
        "release.keystore",
        "android/local.properties",
        "ios/Demo/GoogleService-Info.plist",
        "profile.mobileprovision",
    ],
)
def test_secret_paths_are_recognised(path):
    assert is_secret_path(path) is True


@pytest.mark.parametrize("path", ["src/App.tsx", "package.json", "android/build.gradle"])
def test_normal_paths_are_not_secret(path):
    assert is_secret_path(path) is False


def test_redact_masks_token_shapes():
    text = (
        "key=sk-ant-api03-abcdefghijklmnopqrstuvwxyz "
        "gh token ghp_abcdefghijklmnopqrstuvwxyz1234 "
        "google AIzaSyA1234567890abcdefghijklmnopqrs"
    )
    cleaned = redact(text)
    assert "sk-ant-api03" not in cleaned
    assert "ghp_" not in cleaned
    assert "AIzaSy" not in cleaned
    assert "[redacted]" in cleaned


def test_redact_env_masks_sensitive_keys():
    masked = redact_env({"PATH": "/usr/bin", "API_KEY": "secret", "AUTH_TOKEN": "t"})
    assert masked["PATH"] == "/usr/bin"
    assert masked["API_KEY"] == "[redacted]"
    assert masked["AUTH_TOKEN"] == "[redacted]"


# --- knowledge store -------------------------------------------------------
def test_store_records_runs_and_findings(tmp_path: Path):
    with KnowledgeStore(tmp_path / "knowledge.db") as store:
        run_id = store.start_run("health", dry_run=False, agent_version="0.1.0")
        store.record_findings(
            run_id,
            "health",
            [
                {"id": "android.sdk", "severity": "critical", "title": "SDK levels"},
                {"id": "ios.pods", "severity": "high", "title": "Pods"},
            ],
        )
        store.finish_run(run_id, status="ok", exit_code=0, summary={"score": 71})

        runs = store.recent_runs()
        assert len(runs) == 1
        assert runs[0]["status"] == "ok"
        findings = store.latest_findings("health")
        assert {finding["id"] for finding in findings} == {"android.sdk", "ios.pods"}
        assert store.last_run("health")["command"] == "health"


def test_store_keeps_context_history_bounded(tmp_path: Path):
    with KnowledgeStore(tmp_path / "knowledge.db") as store:
        for index in range(25):
            store.save_context({"index": index}, rn_version="0.81.0")
        history = store.context_history(limit=50)
        assert len(history) <= 20


def test_store_records_decisions_and_ai_usage(tmp_path: Path):
    with KnowledgeStore(tmp_path / "knowledge.db") as store:
        store.record_decision("state", "keep redux-saga", rationale="already used", command="feature")
        assert store.decisions()[0]["decision"] == "keep redux-saga"

        store.record_ai_usage(
            command="migrate", provider="claude", model="sonnet", input_tokens=1200, output_tokens=300
        )
        usage = store.ai_usage_summary()
        assert usage == {"calls": 1, "input_tokens": 1200, "output_tokens": 300}


def test_store_key_values(tmp_path: Path):
    with KnowledgeStore(tmp_path / "knowledge.db") as store:
        store.set_value("last_migration", {"from": "0.79.1", "to": "0.81.0"})
        assert store.get_value("last_migration")["to"] == "0.81.0"
        assert store.get_value("missing", "fallback") == "fallback"
        assert store.stats()["runs"] == 0


def test_store_survives_reopen(tmp_path: Path):
    path = tmp_path / "knowledge.db"
    with KnowledgeStore(path) as store:
        store.start_run("scan", dry_run=False, agent_version="0.1.0")
    with KnowledgeStore(path) as store:
        assert len(store.recent_runs()) == 1


# --- the shared AI provider ------------------------------------------------
def _ai_config(**ai: object) -> AgentConfig:
    return AgentConfig.model_validate({"ai": ai})


def test_context_refuses_to_build_a_provider_without_one_configured(project):
    context = project.context(config=_ai_config())

    with pytest.raises(ProviderError) as failure:
        _ = context.ai
    assert "no AI provider configured" in failure.value.message
    assert context.ai_ready() is False


def test_context_respects_the_disable_switch(project):
    context = project.context(config=_ai_config(provider="openai", enabled=False))

    with pytest.raises(ProviderError, match="AI is disabled"):
        _ = context.ai


def test_context_builds_the_configured_provider_from_the_stored_key(project):
    build_store().store("openai", "sk-test-openai-0123456789")
    context = project.context(config=_ai_config(provider="openai", model="gpt-5-mini"))

    provider = context.ai

    assert provider.name == "openai"
    assert provider.model == "gpt-5-mini"
    assert provider.masked_credential == "…6789"
    assert context.ai_ready() is True


def test_context_needs_no_credential_for_a_local_model(project):
    context = project.context(config=_ai_config(provider="ollama"))

    assert context.ai.name == "ollama"
    assert context.ai_ready() is True


def test_context_records_token_usage_for_the_project(project):
    context = project.context(command="feature")
    context.paths.ensure()

    context.record_ai_usage(
        Completion("ok", "openai", "gpt-5", Usage(120, 30), task="feature")
    )

    assert context.store.ai_usage_summary() == {
        "calls": 1,
        "input_tokens": 120,
        "output_tokens": 30,
    }
    context.close()


def test_a_dry_run_records_no_usage(project):
    context = project.context(command="feature", dry_run=True)

    context.record_ai_usage(Completion("ok", "openai", "gpt-5", Usage(1, 1)))

    assert context.store.ai_usage_summary()["calls"] == 0
    context.close()
