"""``rn-agent upgrade``: the registry client, the risk rules, the rewrite.

No network: the ``transport`` fixture serves abbreviated packuments, which is
also how the registry client's own behaviour (scoped-name encoding, the
``Accept`` header, pre-release exclusion, caching) gets exercised.
"""

from __future__ import annotations

import json

import pytest

from rn_agent.commands.upgrade import UpgradeCommand
from rn_agent.errors import RNAgentError, TransportError
from rn_agent.models.changes import RiskLevel
from rn_agent.models.upgrade import ChangeKind
from rn_agent.net.http import HttpResponse
from rn_agent.upgrade.planner import plan_upgrades
from rn_agent.upgrade.registry import ABBREVIATED, NpmRegistry


def packument(name: str, versions: dict[str, dict], latest: str | None = None) -> dict:
    return {
        "name": name,
        "dist-tags": {"latest": latest or sorted(versions)[-1]},
        "versions": {
            number: {"version": number, **payload} for number, payload in versions.items()
        },
    }


class Registry:
    """A transport that answers per package name, and records what was asked."""

    def __init__(self, documents: dict[str, dict]) -> None:
        self.documents = documents
        self.calls: list[str] = []
        self.headers: list[dict] = []

    def request(self, method, url, *, headers, payload=None, timeout=120.0):
        self.calls.append(url)
        self.headers.append(dict(headers))
        name = url.rsplit("/", 1)[-1].replace("%2F", "/")
        document = self.documents.get(name)
        if document is None:
            return HttpResponse(status=404, body={}, text="not found")
        return HttpResponse(status=200, body=document, text="")


class Unreachable:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def request(self, method, url, *, headers, payload=None, timeout=120.0):
        self.calls.append(url)
        raise TransportError(f"cannot reach {url}: refused")


def plan_for(project, documents, **kwargs):
    context = project.scanned(command="upgrade")
    registry = NpmRegistry(transport=Registry(documents))
    return plan_upgrades(project=context.project, registry=registry, **kwargs)


# ---------------------------------------------------------------------------
# the registry client
# ---------------------------------------------------------------------------
def test_scoped_names_are_encoded_and_the_abbreviated_document_requested():
    transport = Registry(
        {"@react-navigation/native": packument("@react-navigation/native", {"7.0.0": {}})}
    )
    registry = NpmRegistry(transport=transport)

    document = registry.packument("@react-navigation/native")

    assert document is not None
    assert transport.calls == ["https://registry.npmjs.org/@react-navigation%2Fnative"]
    assert transport.headers[0]["accept"] == ABBREVIATED


def test_a_packument_is_fetched_once():
    transport = Registry({"lodash": packument("lodash", {"4.17.21": {}})})
    registry = NpmRegistry(transport=transport)

    registry.packument("lodash")
    registry.packument("lodash")

    assert len(transport.calls) == 1


def test_prereleases_are_never_the_newest():
    transport = Registry(
        {"lodash": packument("lodash", {"4.17.21": {}, "5.0.0-rc.1": {}}, latest="4.17.21")}
    )
    document = NpmRegistry(transport=transport).packument("lodash")

    assert document is not None
    newest = document.newest()
    assert newest is not None and newest.version == "4.17.21"


def test_an_unreachable_registry_stops_asking():
    transport = Unreachable()
    registry = NpmRegistry(transport=transport)

    assert registry.packument("lodash") is None
    assert registry.packument("axios") is None
    assert registry.available is False
    assert len(transport.calls) == 1


# ---------------------------------------------------------------------------
# the planner
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("policy", "expected"),
    [("patch", "4.17.21"), ("minor", "4.18.0"), ("latest", "5.1.0")],
)
def test_policies_pick_different_targets(project, policy, expected):
    project.write_package_json(dependencies={"lodash": "^4.17.0"})
    project.installed("lodash", "4.17.0")
    documents = {
        "lodash": packument(
            "lodash",
            {"4.17.0": {}, "4.17.21": {}, "4.18.0": {}, "5.1.0": {}, "5.2.0-beta.1": {}},
            latest="5.1.0",
        )
    }

    plan = plan_for(project, documents, policy=policy)
    candidate = next(item for item in plan.candidates if item.name == "lodash")

    assert candidate.target == expected
    assert candidate.spec == f"^{expected}"


def test_react_native_and_react_are_blocked_and_point_at_migrate(project):
    plan = plan_for(project, {}, policy="latest")

    blocked = {item.name: item for item in plan.blocked}
    assert "migrate" in (blocked["react-native"].blocked_reason or "")
    assert "migrate" in (blocked["react"].blocked_reason or "")
    assert blocked["react-native"].risk is RiskLevel.CRITICAL


def test_a_peer_conflict_blocks_the_candidate(project):
    project.write_package_json(dependencies={"react-redux": "^8.0.0"})
    project.installed("react-redux", "8.0.0")
    documents = {
        "react-redux": packument(
            "react-redux",
            {"8.0.0": {}, "9.0.0": {"peerDependencies": {"react": "^18.0.0"}}},
            latest="9.0.0",
        )
    }

    plan = plan_for(project, documents, policy="latest")
    candidate = next(item for item in plan.candidates if item.name == "react-redux")

    assert candidate.blocked is True
    assert candidate.peer_conflicts and "react 19.1.0" in candidate.peer_conflicts[0]
    assert candidate.risk is RiskLevel.HIGH


def test_an_undecidable_peer_range_is_not_a_conflict(project):
    project.write_package_json(dependencies={"thing": "^1.0.0", "other": "workspace:*"})
    project.installed("thing", "1.0.0")
    documents = {
        "thing": packument(
            "thing", {"1.0.0": {}, "1.1.0": {"peerDependencies": {"other": "^2.0.0"}}}
        ),
        "other": packument("other", {"1.0.0": {}}),
    }

    plan = plan_for(project, documents, policy="minor")
    candidate = next(item for item in plan.candidates if item.name == "thing")

    assert candidate.peer_conflicts == []
    assert candidate.blocked is False


def test_native_packages_are_excluded_unless_asked_for(project):
    project.write_package_json(dependencies={"react-native-reanimated": "^3.6.0"})
    project.installed("react-native-reanimated", "3.6.0", native=("android", "ios"))
    documents = {
        "react-native-reanimated": packument(
            "react-native-reanimated", {"3.6.0": {}, "3.7.0": {}}
        )
    }

    excluded = plan_for(project, documents, policy="minor")
    candidate = next(
        item for item in excluded.candidates if item.name == "react-native-reanimated"
    )
    assert candidate.blocked is True
    assert "--native" in (candidate.blocked_reason or "")
    assert candidate.risk is RiskLevel.HIGH  # native minor is never medium

    included = plan_for(project, documents, policy="minor", include_native=True)
    candidate = next(
        item for item in included.candidates if item.name == "react-native-reanimated"
    )
    assert candidate.blocked is False
    assert candidate.actionable is True


def test_only_and_skip_filter_the_candidates(project):
    project.write_package_json(dependencies={"lodash": "^4.17.0", "axios": "^1.6.0"})
    project.installed("lodash", "4.17.0")
    project.installed("axios", "1.6.0")
    documents = {
        "lodash": packument("lodash", {"4.17.0": {}, "4.18.0": {}}),
        "axios": packument("axios", {"1.6.0": {}, "1.7.0": {}}),
    }

    only = plan_for(project, documents, policy="minor", only=["lodash"])
    assert [item.name for item in only.candidates] == ["lodash"]

    skipped = plan_for(project, documents, policy="minor", skip=["lodash"])
    assert "lodash" not in [item.name for item in skipped.candidates]


def test_an_up_to_date_package_reports_no_change(project):
    project.write_package_json(dependencies={"lodash": "^4.18.0"})
    project.installed("lodash", "4.18.0")
    documents = {"lodash": packument("lodash", {"4.17.0": {}, "4.18.0": {}})}

    plan = plan_for(project, documents, policy="minor")
    candidate = next(item for item in plan.candidates if item.name == "lodash")

    assert candidate.change is ChangeKind.NONE
    assert candidate.actionable is False


def test_offline_invents_no_target(project):
    context = project.scanned(command="upgrade")

    plan = plan_upgrades(project=context.project, registry=None, policy="latest")

    assert plan.registry_available is False
    assert all(item.target is None for item in plan.candidates)
    assert any("offline" in note for note in plan.notes)


# ---------------------------------------------------------------------------
# the command
# ---------------------------------------------------------------------------
def run(project, documents, **kwargs):
    context = project.scanned(command="upgrade", assume_yes=True, **kwargs.pop("context", {}))
    command = UpgradeCommand(context, **kwargs)
    command.quiet = True
    registry = NpmRegistry(transport=Registry(documents))
    original = command.analyze

    def analyze():
        analysis = original()
        analysis.registry = registry
        return analysis

    command.analyze = analyze  # type: ignore[method-assign]
    return command, command.run()


def test_an_unknown_policy_is_refused(project):
    context = project.scanned(command="upgrade")
    command = UpgradeCommand(context, policy="sideways")
    command.quiet = True

    outcome = command.run()

    assert isinstance(outcome.error, RNAgentError)
    assert "sideways" in outcome.error.message


def test_applying_rewrites_the_declared_range(project):
    project.write_package_json(dependencies={"lodash": "~4.17.0"})
    project.installed("lodash", "4.17.0")
    documents = {"lodash": packument("lodash", {"4.17.0": {}, "4.17.21": {}})}

    command, outcome = run(project, documents, policy="patch", install=False, checks=())

    payload = json.loads((project.root / "package.json").read_text())
    assert payload["dependencies"]["lodash"] == "~4.17.21"
    assert outcome.exit_code == 0
    assert outcome.summary["applied"] is True


def test_failed_validation_restores_package_json(project):
    project.write_package_json(dependencies={"lodash": "^4.17.0"})
    project.installed("lodash", "4.17.0")
    project.local_bin("tsc", exit_code=2, output="error TS2307")
    before = (project.root / "package.json").read_text()
    documents = {"lodash": packument("lodash", {"4.17.0": {}, "4.18.0": {}})}

    command, outcome = run(
        project, documents, policy="minor", install=False, checks=("typecheck",)
    )

    assert (project.root / "package.json").read_text() == before
    assert outcome.exit_code == 1
    assert outcome.summary["rolled_back"] is True


def test_dry_run_changes_nothing(project):
    project.write_package_json(dependencies={"lodash": "^4.17.0"})
    project.installed("lodash", "4.17.0")
    before = (project.root / "package.json").read_text()
    documents = {"lodash": packument("lodash", {"4.17.0": {}, "4.18.0": {}})}

    command, outcome = run(
        project,
        documents,
        policy="minor",
        install=False,
        checks=(),
        context={"dry_run": True},
    )

    assert (project.root / "package.json").read_text() == before
    assert command.report is not None
    assert command.report.selected[0].target == "4.18.0"
    assert outcome.exit_code == 0


def test_an_unreachable_registry_fails_the_command(project):
    context = project.scanned(command="upgrade", assume_yes=True)
    command = UpgradeCommand(context, policy="minor", install=False, checks=())
    command.quiet = True
    registry = NpmRegistry(transport=Unreachable())
    original = command.analyze

    def analyze():
        analysis = original()
        analysis.registry = registry
        return analysis

    command.analyze = analyze  # type: ignore[method-assign]
    outcome = command.run()

    assert outcome.exit_code == 1
    assert outcome.summary["registry_available"] is False


def test_the_report_is_written(project):
    project.write_package_json(dependencies={"lodash": "^4.17.0"})
    project.installed("lodash", "4.17.0")
    documents = {"lodash": packument("lodash", {"4.17.0": {}, "4.18.0": {}})}

    _, outcome = run(project, documents, policy="minor", install=False, checks=())

    path = project.root / ".rn-agent" / "cache" / "upgrade-report.json"
    payload = json.loads(path.read_text())
    assert payload["policy"] == "minor"
    assert payload["applied"] == ["package.json"]
    assert outcome.summary["report"] == str(path)
