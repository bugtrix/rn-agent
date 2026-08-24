"""``rn-agent review``: an opinion, with its evidence and its limits.

The rules under test are the ones that keep a model's answer usable: it may only
report on code it was actually shown, its score means the same thing as the
health score, and a reply it cannot produce is an error rather than an empty
report.
"""

from __future__ import annotations

import json

import pytest

from rn_agent.commands.review import ReviewCommand
from rn_agent.errors import ModelOutputError, RNAgentError
from rn_agent.knowledge.store import KnowledgeStore
from rn_agent.models.health import Severity


def finding(**overrides):
    payload = {
        "id": "unstable-callback",
        "title": "Callback recreated on every render",
        "severity": "high",
        "area": "hooks",
        "file": "src/components/Button.tsx",
        "line": 12,
        "detail": "The handler is a new function each render.",
        "recommendation": "Wrap it in useCallback.",
    }
    payload.update(overrides)
    return payload


def run(project, fake_ai, ai_config, **kwargs):
    context = project.scanned(config=ai_config, command="review")
    command = ReviewCommand(context, **kwargs)
    command.quiet = True
    return command, command.run()


# --- the normal path -------------------------------------------------------
def test_review_reports_findings_and_scores_them(project, fake_ai, ai_config):
    fake_ai.reply(
        {
            "findings": [
                finding(),
                finding(id="missing-key", severity="medium", area="components"),
            ],
            "notes": ["the store is well organised"],
        }
    )

    command, outcome = run(project, fake_ai, ai_config, files=("src/components/Button.tsx",))

    report = command.report
    assert report is not None
    assert [item.id for item in report.sorted_findings] == ["unstable-callback", "missing-key"]
    # 100 - (high 5 + medium 2): the same penalty table as `health`.
    assert report.score == 93
    assert report.grade == "excellent"
    assert outcome.exit_code == 0
    assert outcome.summary["score"] == 93


def test_review_exits_non_zero_on_a_critical_finding(project, fake_ai, ai_config):
    fake_ai.reply({"findings": [finding(severity="critical")]})

    _, outcome = run(project, fake_ai, ai_config, files=("src/components/Button.tsx",))

    assert outcome.exit_code == 1


def test_review_of_clean_code_exits_zero(project, fake_ai, ai_config):
    fake_ai.reply({"findings": [], "notes": ["nothing to report"]})

    command, outcome = run(project, fake_ai, ai_config, files=("src/services/api.ts",))

    assert outcome.exit_code == 0
    assert command.report is not None
    assert command.report.score == 100


@pytest.mark.parametrize(("threshold", "expected"), [(90, 0), (96, 1)])
def test_review_fail_under(project, fake_ai, ai_config, threshold, expected):
    fake_ai.reply({"findings": [finding(severity="high")]})

    _, outcome = run(
        project,
        fake_ai,
        ai_config,
        files=("src/components/Button.tsx",),
        fail_under=threshold,
    )

    assert outcome.exit_code == expected


# --- keeping the model honest ---------------------------------------------
def test_a_finding_about_an_unseen_file_is_dropped(project, fake_ai, ai_config):
    fake_ai.reply(
        {
            "findings": [
                finding(),
                finding(id="invented", file="src/screens/NeverSent.tsx"),
            ]
        }
    )

    command, _ = run(project, fake_ai, ai_config, files=("src/components/Button.tsx",))

    assert command.report is not None
    assert [item.id for item in command.report.findings] == ["unstable-callback"]
    assert any("not sent to the model" in note for note in command.report.notes)


def test_area_filter_reaches_the_prompt_and_the_report(project, fake_ai, ai_config):
    fake_ai.reply(
        {
            "findings": [
                finding(area="hooks"),
                finding(id="off-topic", area="styling"),
            ]
        }
    )

    command, _ = run(
        project,
        fake_ai,
        ai_config,
        files=("src/components/Button.tsx",),
        areas=("hooks",),
    )

    assert "hooks" in fake_ai.last_prompt
    assert command.report is not None
    assert [item.id for item in command.report.findings] == ["unstable-callback"]
    assert any("outside the requested areas" in note for note in command.report.notes)


def test_unknown_area_is_refused_before_any_model_call(project, fake_ai, ai_config):
    context = project.scanned(config=ai_config, command="review")
    command = ReviewCommand(context, areas=("vibes",))
    command.quiet = True

    outcome = command.run()

    assert isinstance(outcome.error, RNAgentError)
    assert "vibes" in outcome.error.message
    assert fake_ai.calls == []


def test_the_prompt_carries_the_code_and_the_rules(project, fake_ai, ai_config):
    (project.root / "src" / "components" / "Button.tsx").write_text(
        "export const Button = () => <Pressable onPress={() => {}} />;\n", encoding="utf-8"
    )
    paths = project.paths()
    paths.ensure()
    paths.rules_file.write_text(
        "rules:\n  allowed_state_management: [redux-saga]\n", encoding="utf-8"
    )
    fake_ai.reply({"findings": []})

    run(project, fake_ai, ai_config, files=("src/components/Button.tsx",))

    prompt = fake_ai.last_prompt
    assert "Pressable" in prompt
    assert "redux-saga" in prompt
    assert "React Native 0.81.0" in prompt


def test_an_unusable_reply_is_an_error_not_an_empty_report(project, fake_ai, ai_config):
    fake_ai.reply("I would need to see more of the code.")
    fake_ai.reply("Still cannot help.")

    command, outcome = run(project, fake_ai, ai_config, files=("src/services/api.ts",))

    assert isinstance(outcome.error, ModelOutputError)
    assert outcome.exit_code == 12
    assert command.report is None


# --- persistence -----------------------------------------------------------
def test_findings_are_written_and_recorded_for_fix(project, fake_ai, ai_config):
    fake_ai.reply({"findings": [finding()]})

    _, outcome = run(project, fake_ai, ai_config, files=("src/components/Button.tsx",))

    report_file = project.root / ".rn-agent" / "cache" / "review-report.json"
    assert report_file.is_file()
    payload = json.loads(report_file.read_text())
    assert payload["findings"][0]["id"] == "unstable-callback"
    assert outcome.summary["report"] == str(report_file)

    with KnowledgeStore(project.root / ".rn-agent" / "knowledge" / "knowledge.db") as store:
        stored = store.latest_findings("review")
    assert [item["id"] for item in stored] == ["unstable-callback"]


def test_dry_run_writes_no_report(project, fake_ai, ai_config):
    fake_ai.reply({"findings": [finding()]})
    context = project.scanned(config=ai_config, command="review", dry_run=True)
    command = ReviewCommand(context, files=("src/components/Button.tsx",))
    command.quiet = True

    command.run()

    assert not (project.root / ".rn-agent" / "cache" / "review-report.json").exists()


def test_review_never_writes_project_files(project, fake_ai, ai_config):
    before = (project.root / "src" / "components" / "Button.tsx").read_text()
    fake_ai.reply(
        {
            "findings": [finding()],
            "proposals": [
                {
                    "id": "sneaky",
                    "title": "rewrite",
                    "edits": [{"path": "src/components/Button.tsx", "content": "gone"}],
                }
            ],
        }
    )

    run(project, fake_ai, ai_config, files=("src/components/Button.tsx",))

    assert (project.root / "src" / "components" / "Button.tsx").read_text() == before


def test_severity_counts_reach_the_summary(project, fake_ai, ai_config):
    fake_ai.reply(
        {
            "findings": [
                finding(id="a", severity="critical"),
                finding(id="b", severity="low"),
            ]
        }
    )

    command, outcome = run(project, fake_ai, ai_config, files=("src/components/Button.tsx",))

    assert outcome.summary["critical"] == 1
    assert outcome.summary["low"] == 1
    assert command.report is not None
    assert command.report.by_severity(Severity.CRITICAL)[0].id == "a"
