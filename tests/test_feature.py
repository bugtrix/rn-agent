"""``rn-agent feature`` and ``rn-agent test``: writing code, and proving it.

Both commands exist to produce code that fits *this* project, so the tests check
the two guardrails that make that true: the model is shown the existing
architecture and rules, and anything it produces that does not compile (or does
not pass) is rolled back rather than left behind.
"""

from __future__ import annotations

import json

from rn_agent.commands.feature import FeatureCommand
from rn_agent.commands.test import TestCommand as GenerateTests
from rn_agent.commands.test import is_test_path
from rn_agent.errors import RNAgentError

#: An existing fixture file - what `test` generates tests for.
SCREEN = "src/screens/HomeScreen.tsx"
#: A file the project does not have - what `feature` creates.
NEW_SCREEN = "src/screens/OrdersScreen.tsx"
NEW_SCREEN_CODE = "export const OrdersScreen = () => null;\n"
TEST_FILE = "src/screens/__tests__/HomeScreen.test.tsx"
TEST_CODE = "it('renders', () => {});\n"


def edits(*pairs, action: str = "create"):
    return [
        {"path": path, "action": action, "content": content, "reason": "generated"}
        for path, content in pairs
    ]


def reply(*pairs, action: str = "create", **overrides):
    payload = {
        "id": "orders-screen",
        "title": "Add the orders screen",
        "summary": "a screen plus its route",
        "edits": edits(*pairs, action=action),
    }
    payload.update(overrides)
    return {"proposals": [payload], "notes": []}


# ---------------------------------------------------------------------------
# feature
# ---------------------------------------------------------------------------
def run_feature(project, config, **kwargs):
    context = project.scanned(config=config, command="feature", assume_yes=True)
    command = FeatureCommand(context, **kwargs)
    command.quiet = True
    return command, command.run()


def test_feature_creates_the_files_it_names(project, fake_ai, ai_config):
    fake_ai.reply(reply((NEW_SCREEN, NEW_SCREEN_CODE)))

    command, outcome = run_feature(
        project, ai_config, description="add an orders screen", checks=()
    )

    assert (project.root / NEW_SCREEN).read_text() == NEW_SCREEN_CODE
    assert command.report.applied == [NEW_SCREEN]
    assert outcome.summary["created"] == 1
    assert outcome.exit_code == 0


def test_the_prompt_carries_the_architecture_and_the_request(project, fake_ai, ai_config):
    project.write_package_json(dependencies={"redux-saga": "^1.3.0", "@reduxjs/toolkit": "^2.2.0"})
    fake_ai.reply(reply((NEW_SCREEN, NEW_SCREEN_CODE)))

    run_feature(project, ai_config, description="add an orders screen", checks=())

    prompt = fake_ai.last_prompt
    assert "redux-saga" in prompt
    assert "add an orders screen" in prompt


def test_a_blank_description_is_refused(project, fake_ai, ai_config):
    command, outcome = run_feature(project, ai_config, description="   ")

    assert isinstance(outcome.error, RNAgentError)
    assert fake_ai.calls == []


def test_a_new_dependency_is_refused_unless_allowed(project, fake_ai, ai_config):
    before = (project.root / "package.json").read_text()
    fake_ai.reply(
        reply(
            (NEW_SCREEN, NEW_SCREEN_CODE),
            ("package.json", '{"name": "demo-app", "dependencies": {"zustand": "^5"}}'),
            action="modify",
        )
    )

    command, _ = run_feature(
        project, ai_config, description="add an orders screen", checks=()
    )

    assert (project.root / "package.json").read_text() == before
    assert [item.path for item in command.report.refused] == ["package.json"]


def test_a_feature_that_does_not_compile_is_rolled_back(project, fake_ai, ai_config):
    project.local_bin("tsc", exit_code=2, output="error TS2304: Cannot find name 'Orders'")
    fake_ai.reply(reply((NEW_SCREEN, NEW_SCREEN_CODE)))

    command, outcome = run_feature(
        project, ai_config, description="add an orders screen", checks=("typecheck",)
    )

    assert not (project.root / NEW_SCREEN).exists()
    assert command.report.rolled_back is True
    assert outcome.exit_code == 1


def test_a_feature_that_compiles_survives(project, fake_ai, ai_config):
    project.local_bin("tsc")
    fake_ai.reply(reply((NEW_SCREEN, NEW_SCREEN_CODE)))

    command, outcome = run_feature(
        project, ai_config, description="add an orders screen", checks=("typecheck",)
    )

    assert (project.root / NEW_SCREEN).is_file()
    assert command.report.validated is True
    assert outcome.exit_code == 0


def test_feature_dry_run_writes_nothing(project, fake_ai, ai_config):
    fake_ai.reply(reply((NEW_SCREEN, NEW_SCREEN_CODE)))
    context = project.scanned(
        config=ai_config, command="feature", assume_yes=True, dry_run=True
    )
    command = FeatureCommand(context, description="add an orders screen", checks=())
    command.quiet = True

    command.run()

    assert not (project.root / NEW_SCREEN).exists()
    assert not (project.root / ".rn-agent" / "cache" / "feature-report.json").exists()


# ---------------------------------------------------------------------------
# test generation
# ---------------------------------------------------------------------------
def run_test_command(project, config, **kwargs):
    context = project.scanned(config=config, command="test", assume_yes=True)
    command = GenerateTests(context, **kwargs)
    command.quiet = True
    return command, command.run()


def test_is_test_path_recognises_the_conventions():
    assert is_test_path("src/App.test.tsx")
    assert is_test_path("src/screens/__tests__/Home.tsx")
    assert is_test_path("app/thing.spec.ts")
    assert not is_test_path("src/App.tsx")
    assert not is_test_path("src/testing/helpers.ts")


def test_generated_tests_are_written_and_run(project, fake_ai, ai_config):
    project.local_bin("jest", output="PASS")
    fake_ai.reply(reply((TEST_FILE, TEST_CODE)))

    command, outcome = run_test_command(project, ai_config, targets=(SCREEN,))

    assert (project.root / TEST_FILE).read_text() == TEST_CODE
    assert command.report.validated is True
    assert outcome.exit_code == 0


def test_failing_generated_tests_are_rolled_back(project, fake_ai, ai_config):
    project.local_bin("jest", exit_code=1, output="FAIL 1 test failed")
    fake_ai.reply(reply((TEST_FILE, TEST_CODE)))

    command, outcome = run_test_command(project, ai_config, targets=(SCREEN,))

    assert not (project.root / TEST_FILE).exists()
    assert command.report.rolled_back is True
    assert outcome.exit_code == 1


def test_keep_on_failure_keeps_the_failing_tests(project, fake_ai, ai_config):
    project.local_bin("jest", exit_code=1)
    fake_ai.reply(reply((TEST_FILE, TEST_CODE)))

    command, outcome = run_test_command(
        project, ai_config, targets=(SCREEN,), keep_on_failure=True
    )

    assert (project.root / TEST_FILE).is_file()
    assert outcome.exit_code == 1


def test_a_proposal_that_edits_production_code_is_refused(project, fake_ai, ai_config):
    before = (project.root / "src" / "screens" / "HomeScreen.tsx").read_text()
    fake_ai.reply(
        reply(
            (TEST_FILE, TEST_CODE),
            ("src/screens/HomeScreen.tsx", "// rewritten"),
            action="modify",
        )
    )
    project.local_bin("jest")

    command, _ = run_test_command(project, ai_config, targets=(SCREEN,))

    assert (project.root / "src" / "screens" / "HomeScreen.tsx").read_text() == before
    assert [item.rule for item in command.report.refused] == ["test.only-test-files"]
    assert command.report.applied == [TEST_FILE]


def test_no_run_writes_without_executing(project, fake_ai, ai_config):
    project.local_bin("jest", exit_code=1)
    fake_ai.reply(reply((TEST_FILE, TEST_CODE)))

    command, outcome = run_test_command(
        project, ai_config, targets=(SCREEN,), run_tests=False
    )

    assert (project.root / TEST_FILE).is_file()
    assert command.report.validation is None
    assert outcome.exit_code == 0


def test_existing_tests_are_offered_as_conventions(project, fake_ai, ai_config):
    project.local_bin("jest")
    fake_ai.reply(reply((TEST_FILE, TEST_CODE)))

    run_test_command(project, ai_config, targets=(SCREEN,))

    assert "__tests__/App.test.tsx" in fake_ai.last_prompt


def test_the_framework_comes_from_the_project(project, fake_ai, ai_config):
    project.local_bin("jest")
    project.write_package_json(
        dev_dependencies={"@testing-library/react-native": "^12.4.0"}
    )
    fake_ai.reply(reply((TEST_FILE, TEST_CODE)))

    command, outcome = run_test_command(project, ai_config, targets=(SCREEN,))

    assert "@testing-library/react-native" in outcome.summary["framework"]
    assert "@testing-library/react-native" in fake_ai.last_prompt


def test_no_framework_is_an_actionable_error(builder, fake_ai, ai_config):
    # The shared fixture always ships jest, so write the manifest by hand.
    (builder.root / "package.json").write_text(
        json.dumps(
            {
                "name": "bare-app",
                "version": "1.0.0",
                "dependencies": {"react": "19.1.0", "react-native": "0.81.0"},
            }
        ),
        encoding="utf-8",
    )
    builder.source_tree("src/App.tsx")
    context = builder.scanned(config=ai_config, command="test", assume_yes=True)
    command = GenerateTests(context)
    command.quiet = True

    outcome = command.run()

    assert isinstance(outcome.error, RNAgentError)
    assert "jest" in (outcome.error.hint or "")
    assert fake_ai.calls == []


def test_framework_override_wins(project, fake_ai, ai_config):
    project.local_bin("jest")
    fake_ai.reply(reply((TEST_FILE, TEST_CODE)))

    _, outcome = run_test_command(
        project, ai_config, targets=(SCREEN,), framework="vitest"
    )

    assert outcome.summary["framework"] == "vitest"
    assert outcome.summary["framework_source"] == "--framework"


def test_the_test_report_is_written(project, fake_ai, ai_config):
    project.local_bin("jest")
    fake_ai.reply(reply((TEST_FILE, TEST_CODE)))

    run_test_command(project, ai_config, targets=(SCREEN,))

    payload = json.loads(
        (project.root / ".rn-agent" / "cache" / "test-report.json").read_text()
    )
    assert payload["task"] == "test"
    assert payload["applied"] == [TEST_FILE]
