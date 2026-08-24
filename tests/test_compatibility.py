"""``rn-agent compatibility``: what it claims, and what it refuses to claim.

The distinction that matters is ``CONFLICT`` versus ``UNKNOWN``. A conflict is a
fact ("react 19.1.0 does not satisfy ^18"); an unknown is the absence of one, and
must never be dressed up as either a pass or a blocker.
"""

from __future__ import annotations

import json

from rn_agent.commands.compatibility import CompatibilityCommand
from rn_agent.errors import TransportError
from rn_agent.models.compatibility import CompatArea, CompatStatus
from rn_agent.net.http import HttpResponse


def packument(name: str, versions: dict[str, dict], latest: str) -> dict:
    return {
        "name": name,
        "dist-tags": {"latest": latest},
        "versions": {
            number: {"version": number, **payload} for number, payload in versions.items()
        },
    }


class Registry:
    def __init__(self, documents: dict[str, dict]) -> None:
        self.documents = documents
        self.calls: list[str] = []

    def request(self, method, url, *, headers, payload=None, timeout=120.0):
        self.calls.append(url)
        name = url.rsplit("/", 1)[-1].replace("%2F", "/")
        document = self.documents.get(name)
        if document is None:
            return HttpResponse(status=404, body={}, text="")
        return HttpResponse(status=200, body=document, text="")


class Unreachable:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def request(self, method, url, *, headers, payload=None, timeout=120.0):
        self.calls.append(url)
        raise TransportError("refused")


def rn_document(target: str, *, react: str = "^19.1.0", node: str = ">=20.19.4") -> dict:
    return packument(
        "react-native",
        {
            "0.81.0": {"peerDependencies": {"react": "^19.1.0"}},
            target: {"peerDependencies": {"react": react}, "engines": {"node": node}},
        },
        latest=target,
    )


def run(project, transport, *, context=None, **kwargs):
    """Run the command with the registry client wired to a fake transport."""
    import rn_agent.commands.compatibility as module

    context = context or project.scanned(command="compatibility")
    command = CompatibilityCommand(context, **kwargs)
    command.quiet = True
    real = module.NpmRegistry
    module.NpmRegistry = lambda *a, **k: real(transport=transport)  # type: ignore[assignment]
    try:
        outcome = command.run()
    finally:
        module.NpmRegistry = real  # type: ignore[assignment]
    return command, outcome


def entry_for(report, name):
    return next(item for item in report.entries if item.name == name)


# --- the happy path --------------------------------------------------------
def test_a_satisfied_project_is_ready(project):
    project.installed("react-native", "0.81.0", peer={"react": "^19.1.0"})
    project.installed("react", "19.1.0")
    transport = Registry({"react-native": rn_document("0.82.0")})

    command, outcome = run(project, transport, target="0.82.0")

    report = command.report
    assert report is not None
    react = entry_for(report, "react")
    assert react.status is CompatStatus.OK
    assert "peerDependencies" in (react.source or "")
    assert report.ready is True
    assert outcome.exit_code == 0


def test_a_react_version_outside_the_requirement_is_a_conflict(project):
    project.installed("react", "19.1.0")
    transport = Registry({"react-native": rn_document("0.82.0", react="^18.0.0")})

    command, outcome = run(project, transport, target="0.82.0")

    react = entry_for(command.report, "react")
    assert react.status is CompatStatus.CONFLICT
    assert "^18.0.0" in react.detail
    assert outcome.exit_code == 1


def test_an_old_node_is_a_conflict_with_the_requirement_quoted(project):
    project.installed("react", "19.1.0")
    transport = Registry({"react-native": rn_document("0.82.0", node=">=20.19.4")})
    context = project.scanned(command="compatibility")
    context.project.tooling.node = "18.20.4"  # the machine's node, as scan found it

    command, outcome = run(project, transport, context=context, target="0.82.0")

    node = entry_for(command.report, "node")
    assert node.status is CompatStatus.CONFLICT
    assert ">=20.19.4" in (node.required or "")
    assert node.current == "18.20.4"
    assert outcome.exit_code == 1


# --- degrading honestly ----------------------------------------------------
def test_an_unreachable_registry_falls_back_to_the_bundled_table(project):
    project.installed("react", "19.1.0")
    transport = Unreachable()

    command, _ = run(project, transport, target="0.81.0")

    report = command.report
    assert report is not None
    assert report.registry_available is False
    react = entry_for(report, "react")
    assert react.source == "bundled compatibility table (offline)"
    assert any("bundled table" in note for note in report.notes)


def test_offline_makes_no_request_at_all(project):
    transport = Registry({"react-native": rn_document("0.82.0")})

    command, _ = run(project, transport, offline=True)

    assert transport.calls == []
    assert command.report is not None
    assert command.report.registry_available is False


def test_no_target_and_no_registry_describes_the_current_version(project):
    command, _ = run(project, Unreachable())

    report = command.report
    assert report is not None
    assert report.target_rn == "0.81.0"
    assert "no target" in (report.target_source or "")


def test_gradle_and_agp_are_unknown_rather_than_invented(project):
    transport = Registry({"react-native": rn_document("0.82.0")})

    command, _ = run(project, transport, target="0.82.0")

    gradle = entry_for(command.report, "gradle")
    assert gradle.area is CompatArea.TOOLING
    assert gradle.status is CompatStatus.UNKNOWN
    assert gradle.required is None
    assert gradle.current == "8.10.2"  # the project's real value is still reported


# --- dependencies ----------------------------------------------------------
def test_a_dependency_that_excludes_the_target_is_a_conflict(project):
    project.write_package_json(dependencies={"react-native-thing": "^2.0.0"})
    project.installed("react-native-thing", "2.0.0", peer={"react-native": "<0.82.0"})
    transport = Registry({"react-native": rn_document("0.82.0")})

    command, outcome = run(project, transport, target="0.82.0")

    thing = entry_for(command.report, "react-native-thing")
    assert thing.status is CompatStatus.CONFLICT
    assert "does not support" in thing.detail
    assert outcome.exit_code == 1


def test_the_report_names_the_version_that_would_work(project):
    project.write_package_json(dependencies={"react-native-thing": "^2.0.0"})
    project.installed("react-native-thing", "2.0.0", peer={"react-native": "<0.82.0"})
    transport = Registry(
        {
            "react-native": rn_document("0.82.0"),
            "react-native-thing": packument(
                "react-native-thing",
                {
                    "2.0.0": {"peerDependencies": {"react-native": "<0.82.0"}},
                    "3.0.0": {"peerDependencies": {"react-native": ">=0.82.0"}},
                },
                latest="3.0.0",
            ),
        }
    )

    command, _ = run(project, transport, target="0.82.0")

    thing = entry_for(command.report, "react-native-thing")
    assert "react-native-thing 3.0.0 does" in thing.detail


def test_an_undecidable_peer_range_is_unknown_not_a_blocker(project):
    project.write_package_json(dependencies={"react-native-thing": "^2.0.0"})
    project.installed("react-native-thing", "2.0.0", peer={"react-native": "workspace:*"})
    transport = Registry({"react-native": rn_document("0.82.0")})

    command, outcome = run(project, transport, target="0.82.0")

    thing = entry_for(command.report, "react-native-thing")
    assert thing.status is CompatStatus.UNKNOWN
    assert outcome.exit_code == 0
    assert command.report.ready is True


def test_a_dependency_with_no_metadata_is_simply_absent(project):
    project.write_package_json(dependencies={"lodash": "^4.17.0"})
    transport = Registry({"react-native": rn_document("0.82.0")})

    command, _ = run(project, transport, target="0.82.0")

    assert all(item.name != "lodash" for item in command.report.entries)


def test_dependencies_can_be_skipped(project):
    project.write_package_json(dependencies={"react-native-thing": "^2.0.0"})
    project.installed("react-native-thing", "2.0.0", peer={"react-native": "<0.82.0"})
    transport = Registry({"react-native": rn_document("0.82.0")})

    command, outcome = run(
        project, transport, target="0.82.0", include_dependencies=False
    )

    assert command.report.by_area(CompatArea.DEPENDENCY) == []
    assert outcome.exit_code == 0


# --- persistence -----------------------------------------------------------
def test_the_report_file_carries_every_entry(project):
    project.installed("react", "19.1.0")
    transport = Registry({"react-native": rn_document("0.82.0")})

    _, outcome = run(project, transport, target="0.82.0")

    path = project.root / ".rn-agent" / "cache" / "compatibility-report.json"
    payload = json.loads(path.read_text())
    assert payload["target_rn"] == "0.82.0"
    assert {entry["name"] for entry in payload["entries"]} >= {"react", "node"}
    assert outcome.summary["report"] == str(path)


def test_compatibility_writes_no_project_file(project):
    before = sorted(p.name for p in project.root.iterdir())
    transport = Registry({"react-native": rn_document("0.82.0")})

    run(project, transport, target="0.82.0")

    after = sorted(p.name for p in project.root.iterdir())
    assert after == sorted({*before, ".rn-agent"})
