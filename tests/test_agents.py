"""The AI work layer: what goes to a model, what comes back, what may be written.

These are the tests that keep the guarantees honest: the context builder must
never send a secret, the parser must refuse an unusable reply instead of
half-applying it, and the applier must obey ``rules.yaml`` even when the model
did not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rn_agent.agents.apply import EditApplier, describe_dependency_change
from rn_agent.agents.context_builder import ContextBuilder, estimate_tokens
from rn_agent.agents.engine import AIEngine
from rn_agent.agents.output import extract_json, parse_changelog, parse_proposals, parse_review
from rn_agent.agents.prompts import fix_messages, project_brief, review_messages
from rn_agent.agents.rules import ProjectRules, dependency_delta, is_native_path
from rn_agent.errors import ConfirmationDeclined, ModelOutputError
from rn_agent.models.changes import RiskLevel
from rn_agent.models.health import Severity
from rn_agent.models.proposal import EditAction, FileEdit
from rn_agent.project.scanner import ProjectScanner


def scanned(builder, **kwargs):
    """A context whose project brain is populated, like after `scan`."""
    context = builder.context(**kwargs)
    scanner = ProjectScanner(
        context.detected, context.paths, context.runner, knowledge=context.knowledge
    )
    context.set_project(scanner.scan(probe_tools=False, source_stats=context.walker.stats()))
    return context


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------
def test_rules_default_when_no_file(project):
    rules = ProjectRules.load(project.paths())

    assert rules.forbid_new_dependencies is True
    assert rules.forbid_native_edits_without_confirmation is True
    assert "package.json" in "\n".join(rules.as_prompt_lines())


def test_rules_are_read_from_the_project_file(project):
    paths = project.paths()
    paths.ensure()
    paths.rules_file.write_text(
        "rules:\n"
        "  allowed_state_management: [redux-saga]\n"
        "  forbid_new_dependencies: false\n"
        "  max_component_lines: 200\n"
        "notes:\n"
        "  - keep screens dumb\n",
        encoding="utf-8",
    )

    rules = ProjectRules.load(paths)
    prompt = "\n".join(rules.as_prompt_lines())

    assert rules.allowed_state_management == ("redux-saga",)
    assert rules.forbid_new_dependencies is False
    # An unknown key is still a rule: it reaches the model rather than vanishing.
    assert "max component lines" in prompt
    assert "keep screens dumb" in prompt


@pytest.mark.parametrize(
    "path",
    [
        "android/app/build.gradle",
        "ios/Podfile",
        "ios/Demo.xcodeproj/project.pbxproj",
        "src/native/Bridge.kt",
        "modules/Thing.swift",
    ],
)
def test_native_paths_are_recognised(path):
    assert is_native_path(path) is True


@pytest.mark.parametrize("path", ["src/App.tsx", "package.json", "docs/setup.md"])
def test_non_native_paths_are_not(path):
    assert is_native_path(path) is False


def test_rules_refuse_lockfiles_native_and_dependencies():
    rules = ProjectRules()
    edits = [
        FileEdit(path="yarn.lock", content="x"),
        FileEdit(path="android/build.gradle", content="x"),
        FileEdit(path="package.json", content="{}"),
        FileEdit(path="src/App.tsx", content="x"),
    ]

    violations = rules.violations(edits)

    refused = {violation.path for violation in violations}
    assert refused == {"yarn.lock", "android/build.gradle", "package.json"}
    assert "src/App.tsx" not in refused


def test_rules_can_be_relaxed_per_run():
    rules = ProjectRules()
    edits = [FileEdit(path="android/build.gradle", content="x"), FileEdit(path="package.json", content="{}")]

    violations = rules.violations(edits, allow_native=True, allow_dependencies=True)

    assert violations == []


def test_dependency_delta_names_the_packages():
    before = json.dumps({"dependencies": {"react": "19.0.0", "lodash": "^4.0.0"}})
    after = json.dumps({"dependencies": {"react": "19.1.0", "zustand": "^5.0.0"}})

    delta = dependency_delta(before, after)

    assert delta == {"added": ["zustand"], "removed": ["lodash"], "changed": ["react"]}
    assert "adds zustand" in describe_dependency_change(before, after)


# ---------------------------------------------------------------------------
# context builder
# ---------------------------------------------------------------------------
def test_context_builder_never_sends_a_secret(project):
    (project.root / ".env").write_text("API_KEY=sk-live-abcdef0123456789\n", encoding="utf-8")
    (project.root / "src" / "secrets.ts").write_text("export const x = 1;\n", encoding="utf-8")
    context = scanned(project)

    selected = ContextBuilder(context).select(paths=[".env", "src/secrets.ts", "src/App.tsx"])

    assert ".env" in selected.refused
    assert "src/secrets.ts" in selected.refused
    assert ".env" not in selected.paths
    assert "API_KEY" not in selected.render()


def test_context_builder_respects_the_file_budget(project):
    project.source_tree(*[f"src/components/C{index}.tsx" for index in range(30)])
    context = scanned(project)
    context.config.ai.max_context_files = 5

    selected = ContextBuilder(context).select(query="component")

    assert len(selected) == 5
    assert selected.skipped, "files dropped for budget must be reported"


def test_context_builder_respects_the_token_budget(project):
    big = "const x = 1;\n" * 4000
    (project.root / "src" / "Big.tsx").write_text(big, encoding="utf-8")
    (project.root / "src" / "Small.tsx").write_text("const y = 2;\n", encoding="utf-8")
    context = scanned(project)
    context.config.ai.max_context_tokens = estimate_tokens(big) + 1

    selected = ContextBuilder(context).select(paths=["src/Big.tsx", "src/Small.tsx"])

    assert selected.paths == ("src/Big.tsx",)
    assert "src/Small.tsx" in selected.skipped


def test_context_builder_truncates_one_huge_file_and_says_so(project):
    (project.root / "src" / "Huge.tsx").write_text("x" * 200_000, encoding="utf-8")
    context = scanned(project)
    context.config.context.max_file_kb = 4

    selected = ContextBuilder(context).select(paths=["src/Huge.tsx"])

    assert selected.files[0].truncated is True
    assert len(selected.files[0].content) <= 4 * 1024
    assert "(truncated)" in selected.render()


def test_context_builder_ranks_by_the_query(project):
    project.source_tree(
        "src/screens/CheckoutScreen.tsx",
        "src/components/Avatar.tsx",
        "src/utils/dates.ts",
    )
    context = scanned(project)

    selected = ContextBuilder(context).select(query="checkout screen crash", limit=1)

    assert selected.paths == ("src/screens/CheckoutScreen.tsx",)


def test_context_builder_reads_only_inside_the_project(project):
    context = scanned(project)

    from rn_agent.errors import UnsafePathError

    with pytest.raises(UnsafePathError):
        ContextBuilder(context).select(paths=["../outside.ts"])


def test_context_builder_selects_changed_files(project):
    project.git_init()
    (project.root / "src" / "screens" / "HomeScreen.tsx").write_text(
        "export const Home = () => null;\n", encoding="utf-8"
    )
    context = scanned(project)

    selected = ContextBuilder(context).select(changed=True)

    assert "src/screens/HomeScreen.tsx" in selected.paths


def test_context_builder_honours_exclude_globs(project):
    context = scanned(project)
    context.config.context.exclude_globs = ["src/store/*"]

    selected = ContextBuilder(context).select(paths=["src/store/index.ts", "src/services/api.ts"])

    assert selected.paths == ("src/services/api.ts",)


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------
def test_prompt_carries_the_scanned_facts_and_the_rules(project):
    context = scanned(project)
    rules = ProjectRules(allowed_state_management=("redux-saga",))

    brief = project_brief(context.project, rules)

    assert "React Native 0.81.0" in brief
    assert "yarn" in brief
    assert "redux-saga" in brief


def test_review_prompt_states_the_output_contract(project):
    context = scanned(project)
    selected = ContextBuilder(context).select(paths=["src/App.tsx"], limit=1)

    messages = review_messages(
        project=context.project, rules=ProjectRules(), context=selected, areas=["hooks"]
    )

    assert messages[0].role == "system"
    assert '"findings"' in messages[0].content
    assert "hooks" in messages[1].content


def test_fix_prompt_lists_the_issues(project):
    context = scanned(project)
    selected = ContextBuilder(context).select(paths=[], limit=0)

    messages = fix_messages(
        project=context.project,
        rules=ProjectRules(),
        context=selected,
        issues=["js.typecheck: 3 errors"],
    )

    assert "js.typecheck" in messages[1].content
    assert "COMPLETE FILE CONTENT" in messages[0].content


# ---------------------------------------------------------------------------
# output parsing
# ---------------------------------------------------------------------------
def test_extract_json_tolerates_fences_and_prose():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Sure!\n```json\n{"a": 2}\n```\nHope that helps.') == {"a": 2}
    assert extract_json('Here you go: {"a": {"b": "}"}} and that is all') == {"a": {"b": "}"}}


def test_extract_json_refuses_prose():
    with pytest.raises(ModelOutputError):
        extract_json("I cannot help with that.")


def test_parse_proposals_normalises_and_keeps_only_usable_edits():
    reply = json.dumps(
        {
            "proposals": [
                {
                    "id": "Fix The Thing!",
                    "title": "Fix the thing",
                    "risk": "banana",
                    "edits": [
                        {"path": "./src/App.tsx", "action": "modify", "content": "const a = 1;\n"},
                        {"path": "src/Broken.tsx", "action": "modify"},
                        {"path": "/etc/passwd", "action": "modify", "content": "x"},
                        {"path": "../escape.ts", "action": "modify", "content": "x"},
                        {"path": "src/Old.tsx", "action": "delete"},
                    ],
                }
            ],
            "notes": ["one note"],
        }
    )

    proposals = parse_proposals(reply, task="fix")

    edit_paths = [edit.path for edit in proposals.edits]
    assert edit_paths == ["src/App.tsx", "src/Old.tsx"]
    assert proposals.proposals[0].id == "fix-the-thing"
    assert proposals.proposals[0].risk is RiskLevel.MEDIUM
    assert proposals.notes == ["one note"]
    assert proposals.counts()["deleted"] == 1


def test_parse_proposals_refuses_an_answer_with_no_edits():
    reply = json.dumps({"proposals": [], "notes": ["I need the reducer file"]})

    with pytest.raises(ModelOutputError) as failure:
        parse_proposals(reply, task="fix")

    assert "reducer" in failure.value.message


def test_parse_proposals_refuses_a_reply_without_the_key():
    with pytest.raises(ModelOutputError, match="proposals"):
        parse_proposals(json.dumps({"changes": []}), task="fix")


def test_parse_review_normalises_severity_area_and_line():
    reply = json.dumps(
        {
            "findings": [
                {
                    "title": "Unstable callback",
                    "severity": "SEVERE",
                    "area": "vibes",
                    "file": "src/App.tsx",
                    "line": "42",
                    "confidence": "certain",
                },
                {"detail": "no title, dropped"},
            ]
        }
    )

    findings, notes = parse_review(reply)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity is Severity.MEDIUM
    assert finding.area == "other"
    assert finding.line == 42
    assert finding.confidence == "medium"
    assert notes == []


def test_parse_review_accepts_a_clean_bill_of_health():
    findings, notes = parse_review(json.dumps({"findings": [], "notes": ["looks good"]}))

    assert findings == []
    assert notes == ["looks good"]


def test_parse_changelog_requires_entries():
    entries, _ = parse_changelog(json.dumps({"entries": ["Add dark mode"]}))
    assert entries == ["Add dark mode"]

    with pytest.raises(ModelOutputError):
        parse_changelog(json.dumps({"entries": []}))


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------
def test_engine_records_usage_and_uses_the_task_model(project, fake_ai, ai_config):
    ai_config.ai.models.review = "claude-opus-4-1"
    context = scanned(project, config=ai_config)
    fake_ai.reply({"findings": []})

    engine = AIEngine(context)
    findings, notes, completion = engine.review(
        review_messages(
            project=context.project,
            rules=ProjectRules(),
            context=ContextBuilder(context).select(limit=0),
        )
    )

    assert findings == []
    assert fake_ai.transport.last["payload"]["model"] == "claude-opus-4-1"
    assert completion.usage.input_tokens == 120
    assert context.store.ai_usage_summary()["calls"] == 1
    _ = notes


def test_engine_repairs_one_unparsable_reply(project, fake_ai, ai_config):
    context = scanned(project, config=ai_config)
    fake_ai.reply("I think your code is fine, honestly.")
    fake_ai.reply({"findings": [{"title": "Missing key prop", "severity": "low"}]})

    findings, _, _ = AIEngine(context).review(
        review_messages(
            project=context.project,
            rules=ProjectRules(),
            context=ContextBuilder(context).select(limit=0),
        )
    )

    assert [finding.title for finding in findings] == ["Missing key prop"]
    assert len(fake_ai.calls) == 2
    # The retry must show the model its own answer plus the parse error.
    assert "could not be parsed" in fake_ai.last_prompt


def test_engine_gives_up_after_one_repair(project, fake_ai, ai_config):
    context = scanned(project, config=ai_config)
    fake_ai.reply("no").reply("still no")

    with pytest.raises(ModelOutputError):
        AIEngine(context).review(
            review_messages(
                project=context.project,
                rules=ProjectRules(),
                context=ContextBuilder(context).select(limit=0),
            )
        )

    assert len(fake_ai.calls) == 2


def test_engine_reports_a_truncated_answer_instead_of_parsing_it(project, fake_ai, ai_config):
    context = scanned(project, config=ai_config)
    fake_ai.reply({"findings": []}, stop_reason="max_tokens")

    with pytest.raises(ModelOutputError, match="cut off"):
        AIEngine(context).review(
            review_messages(
                project=context.project,
                rules=ProjectRules(),
                context=ContextBuilder(context).select(limit=0),
            )
        )


# ---------------------------------------------------------------------------
# applier
# ---------------------------------------------------------------------------
def test_applier_writes_backs_up_and_rolls_back(project):
    target = project.root / "src" / "components" / "Button.tsx"
    original = target.read_text()
    context = scanned(project, assume_yes=True)
    applier = EditApplier(context, rules=ProjectRules())

    outcome = applier.apply(
        [FileEdit(path="src/components/Button.tsx", content="export const Button = 1;\n")],
        reason="fix",
    )

    assert outcome.applied == ("src/components/Button.tsx",)
    assert target.read_text() == "export const Button = 1;\n"

    restored = applier.rollback()

    assert restored == ["src/components/Button.tsx"]
    assert target.read_text() == original


def test_applier_refuses_what_the_rules_forbid(project):
    context = scanned(project, assume_yes=True)
    applier = EditApplier(context, rules=ProjectRules())
    before = (project.root / "package.json").read_text()

    outcome = applier.apply(
        [
            FileEdit(path="package.json", content='{"name": "hacked"}'),
            FileEdit(path="android/build.gradle", content="// nope"),
        ],
        reason="fix",
    )

    assert outcome.applied == ()
    assert {violation.rule for violation in outcome.refused} == {
        "forbid_new_dependencies",
        "forbid_native_edits_without_confirmation",
    }
    assert (project.root / "package.json").read_text() == before


def test_applier_allows_native_edits_when_asked(project):
    context = scanned(project, assume_yes=True)
    applier = EditApplier(context, rules=ProjectRules(), allow_native=True)

    outcome = applier.apply(
        [FileEdit(path="android/gradle.properties", content="newArchEnabled=true\n")],
        reason="migrate",
    )

    assert outcome.applied == ("android/gradle.properties",)
    assert outcome.risk is RiskLevel.HIGH


def test_applier_stops_when_the_developer_declines(project):
    context = project.context(confirmer=lambda question, default: False)
    context.set_project(scanned(project).project)
    applier = EditApplier(context, rules=ProjectRules())

    with pytest.raises(ConfirmationDeclined):
        applier.apply([FileEdit(path="src/App.tsx", content="x")], reason="fix")

    assert not (project.root / "src" / "App.tsx").exists()


def test_applier_writes_nothing_in_dry_run(project):
    target = project.root / "src" / "services" / "api.ts"
    original = target.read_text()
    context = scanned(project, dry_run=True, assume_yes=True)
    applier = EditApplier(context, rules=ProjectRules())

    outcome = applier.apply(
        [FileEdit(path="src/services/api.ts", content="export const api = 2;\n")], reason="fix"
    )

    assert outcome.dry_run is True
    assert outcome.applied == ("src/services/api.ts",)
    assert target.read_text() == original


def test_applier_reports_an_edit_that_changes_nothing(project):
    target: Path = project.root / "src" / "hooks" / "useThing.ts"
    context = scanned(project, assume_yes=True)
    applier = EditApplier(context, rules=ProjectRules())

    outcome = applier.apply(
        [FileEdit(path="src/hooks/useThing.ts", content=target.read_text())], reason="fix"
    )

    assert outcome.applied == ()
    assert outcome.unchanged == ("src/hooks/useThing.ts",)


def test_applier_deletes_through_the_file_manager(project):
    target = project.root / "src" / "utils" / "dead.ts"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("export const dead = true;\n", encoding="utf-8")
    context = scanned(project, assume_yes=True)
    applier = EditApplier(context, rules=ProjectRules())

    outcome = applier.apply(
        [FileEdit(path="src/utils/dead.ts", action=EditAction.DELETE)], reason="cleanup"
    )

    assert outcome.applied == ("src/utils/dead.ts",)
    assert not target.exists()
    assert applier.rollback() == ["src/utils/dead.ts"]
    assert target.read_text() == "export const dead = true;\n"


def test_applier_honours_the_max_files_limit(project):
    context = scanned(project, assume_yes=True)
    context.config.safety.max_files_per_operation = 1
    applier = EditApplier(context, rules=ProjectRules())

    with pytest.raises(ConfirmationDeclined, match="above the configured limit"):
        applier.apply(
            [
                FileEdit(path="src/a.ts", content="a"),
                FileEdit(path="src/b.ts", content="b"),
            ],
            reason="feature",
        )
