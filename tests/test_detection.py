"""Project detection, package-manager detection and dependency reading."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rn_agent.errors import NotAReactNativeProject
from rn_agent.knowledge.data import load_knowledge_data
from rn_agent.project.detector import detect_project, find_project_root
from rn_agent.project.packages import (
    collect_dependencies,
    detect_package_manager,
    lockfile_package_version,
    read_installed_package,
)


# --- detection -------------------------------------------------------------
def test_detects_a_react_native_project(project):
    detected = detect_project(project.root)
    assert detected.root == project.root
    assert detected.name == "demo-app"
    assert detected.react_native_declared == "0.81.0"
    assert detected.platforms == ("android", "ios")
    assert "package.json" in detected.markers


def test_detects_from_a_subdirectory(project):
    nested = project.root / "src" / "components"
    detected = detect_project(nested)
    assert detected.root == project.root


def test_rejects_a_directory_without_package_json(tmp_path: Path):
    with pytest.raises(NotAReactNativeProject) as error:
        detect_project(tmp_path / "nowhere")
    assert error.value.exit_code == 2
    assert error.value.hint


def test_rejects_a_node_project_that_is_not_react_native(tmp_path: Path):
    root = tmp_path / "api"
    root.mkdir()
    (root / "package.json").write_text(json.dumps({"name": "api", "dependencies": {"express": "^4"}}))
    with pytest.raises(NotAReactNativeProject, match="does not declare react-native"):
        detect_project(root)


def test_detects_expo_managed_project(builder):
    builder.write_package_json(dependencies={"expo": "^52.0.0"})
    detected = detect_project(builder.root)
    assert detected.expo_declared == "^52.0.0"
    assert detected.is_expo_managed is True
    assert detected.platforms == ()


def test_monorepo_app_wins_over_workspace_root(tmp_path: Path):
    workspace = tmp_path / "monorepo"
    (workspace).mkdir()
    (workspace / "package.json").write_text(
        json.dumps({"name": "monorepo", "private": True, "workspaces": ["apps/*"]})
    )
    app = workspace / "apps" / "mobile"
    app.mkdir(parents=True)
    (app / "package.json").write_text(
        json.dumps({"name": "mobile", "dependencies": {"react-native": "0.81.0", "react": "19.1.0"}})
    )
    detected = detect_project(app)
    assert detected.root == app
    assert detected.workspace_root == workspace


def test_find_project_root_returns_none_outside_any_package(tmp_path: Path):
    assert find_project_root(tmp_path) is None


# --- package manager -------------------------------------------------------
@pytest.mark.parametrize(
    ("lockfile", "expected"),
    [
        ("package-lock.json", "npm"),
        ("yarn.lock", "yarn"),
        ("pnpm-lock.yaml", "pnpm"),
        ("bun.lockb", "bun"),
    ],
)
def test_package_manager_from_lockfile(builder, lockfile, expected):
    builder.write_package_json().lockfile(lockfile)
    manager = detect_package_manager(builder.root, builder.package_json)
    assert manager.name == expected
    assert manager.lockfile == lockfile


def test_package_manager_field_wins_over_lockfile(builder):
    builder.write_package_json(extra={"packageManager": "pnpm@9.1.0"}).lockfile("yarn.lock")
    manager = detect_package_manager(builder.root, builder.package_json)
    assert manager.name == "pnpm"
    assert manager.version == "9.1.0"


def test_multiple_lockfiles_are_all_reported(builder):
    builder.write_package_json().lockfile("yarn.lock").lockfile("package-lock.json")
    manager = detect_package_manager(builder.root, builder.package_json)
    assert set(manager.lockfiles_found) == {"yarn.lock", "package-lock.json"}
    assert manager.name in {"yarn", "npm"}


def test_no_lockfile_is_unknown(builder):
    builder.write_package_json()
    manager = detect_package_manager(builder.root, builder.package_json)
    assert manager.name == "unknown"
    assert manager.install_command == "npm install"


def test_workspaces_are_recorded(builder):
    builder.write_package_json(extra={"workspaces": ["packages/*"]})
    manager = detect_package_manager(builder.root, builder.package_json)
    assert manager.workspaces == ["packages/*"]


# --- dependencies ----------------------------------------------------------
def test_dependencies_prefer_installed_metadata(builder):
    builder.write_package_json(dependencies={"react-native": "^0.81.0"})
    builder.installed("react-native", "0.81.4", peer={"react": "^19.1.0"}, engines={"node": ">=20"})
    dependencies, present, method = collect_dependencies(
        builder.root, builder.package_json, load_knowledge_data()
    )
    assert present is True
    assert method == "filesystem"
    rn = next(dependency for dependency in dependencies if dependency.name == "react-native")
    assert rn.installed == "0.81.4"
    assert rn.declared == "^0.81.0"
    assert rn.peer_dependencies == {"react": "^19.1.0"}


def test_native_detection_uses_the_filesystem_when_installed(builder):
    builder.write_package_json(
        dependencies={"react-native-reanimated": "^3.16.0", "react-native-paper": "^5.0.0"}
    )
    builder.installed("react-native-reanimated", "3.16.0", native=("android", "apple"))
    builder.installed("react-native-paper", "5.12.0")
    dependencies, _, method = collect_dependencies(
        builder.root, builder.package_json, load_knowledge_data()
    )
    by_name = {dependency.name: dependency for dependency in dependencies}
    assert method == "filesystem"
    assert by_name["react-native-reanimated"].native is True
    assert by_name["react-native-paper"].native is False


def test_native_detection_falls_back_to_heuristics(builder):
    builder.write_package_json(
        dependencies={"react-native-reanimated": "^3.16.0", "react-native-paper": "^5.0.0"}
    )
    dependencies, present, method = collect_dependencies(
        builder.root, builder.package_json, load_knowledge_data()
    )
    by_name = {dependency.name: dependency for dependency in dependencies}
    assert present is False
    assert method == "heuristic"
    assert by_name["react-native-reanimated"].native is True
    # curated js-only list keeps this out of the native set
    assert by_name["react-native-paper"].native is False


def test_podspec_only_package_counts_as_native(builder):
    builder.write_package_json(dependencies={"react-native-thing": "^1.0.0"})
    builder.installed("react-native-thing", "1.0.0")
    (builder.root / "node_modules" / "react-native-thing" / "Thing.podspec").write_text("x")
    dependencies, _, _ = collect_dependencies(
        builder.root, builder.package_json, load_knowledge_data()
    )
    thing = next(d for d in dependencies if d.name == "react-native-thing")
    assert thing.native is True
    assert "ios" in thing.platforms


def test_read_installed_package_missing_returns_none(builder):
    builder.write_package_json()
    assert read_installed_package(builder.root / "node_modules", "nope") is None


# --- lockfile resolution ---------------------------------------------------
def test_yarn_lock_picks_the_entry_matching_the_declared_range(builder):
    """Regression: a transitive `react-native@*` must not win over the app's pin."""
    builder.write_package_json(dependencies={"react-native": "0.79.1"})
    builder.yarn_lock({"react-native@*": "0.85.2", "react-native@0.79.1": "0.79.1"})
    assert (
        lockfile_package_version(builder.root, "yarn.lock", "react-native", declared="0.79.1")
        == "0.79.1"
    )


def test_yarn_lock_single_entry_needs_no_range(builder):
    builder.write_package_json()
    builder.yarn_lock({"react-native@0.81.0": "0.81.0"})
    assert lockfile_package_version(builder.root, "yarn.lock", "react-native") == "0.81.0"


def test_yarn_lock_ambiguity_returns_none(builder):
    builder.write_package_json()
    builder.yarn_lock({"react-native@*": "0.85.2", "react-native@0.79.1": "0.79.1"})
    assert lockfile_package_version(builder.root, "yarn.lock", "react-native") is None


def test_yarn_lock_comma_separated_keys(builder):
    builder.write_package_json()
    (builder.root / "yarn.lock").write_text(
        '# yarn lockfile v1\n\n"react-native@^0.81.0", react-native@0.81.0:\n  version "0.81.0"\n',
        encoding="utf-8",
    )
    assert (
        lockfile_package_version(builder.root, "yarn.lock", "react-native", declared="^0.81.0")
        == "0.81.0"
    )


def test_npm_lock_uses_the_hoisted_entry(builder):
    builder.write_package_json()
    (builder.root / "package-lock.json").write_text(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "node_modules/react-native": {"version": "0.81.0"},
                    "node_modules/other/node_modules/react-native": {"version": "0.70.0"},
                },
            }
        ),
        encoding="utf-8",
    )
    assert lockfile_package_version(builder.root, "package-lock.json", "react-native") == "0.81.0"


def test_unknown_lockfile_format_returns_none(builder):
    builder.write_package_json().lockfile("pnpm-lock.yaml")
    assert lockfile_package_version(builder.root, "pnpm-lock.yaml", "react-native") is None
