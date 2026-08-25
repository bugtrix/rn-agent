"""Health analyzers.

Each rule is tested twice: it must fire on a project that has the problem, and
stay silent on one that does not. That is what stops the tool from crying wolf.
"""

from __future__ import annotations

from datetime import date

from rn_agent.analyzers import ANALYZERS
from rn_agent.analyzers.android_analyzer import AndroidAnalyzer
from rn_agent.analyzers.base import AnalyzerInput, summarize
from rn_agent.analyzers.ios_analyzer import IOSAnalyzer
from rn_agent.analyzers.js_analyzer import JavaScriptAnalyzer
from rn_agent.analyzers.project_analyzer import ProjectAnalyzer
from rn_agent.analyzers.rn_analyzer import ReactNativeAnalyzer
from rn_agent.filesystem.walker import ProjectWalker
from rn_agent.git.manager import GitManager
from rn_agent.knowledge.data import load_knowledge_data
from rn_agent.models.health import CheckStatus, HealthCheck, HealthReport, Severity
from rn_agent.project.detector import detect_project
from rn_agent.project.scanner import ProjectScanner
from rn_agent.runner.command_runner import CommandRunner


def build_context(builder, *, probe_tools: bool = False):
    """Assemble the context exactly like ScanCommand does (git + sources)."""
    detected = detect_project(builder.root)
    paths = builder.paths()
    runner = CommandRunner(cwd=builder.root)
    git = GitManager(root=builder.root, runner=runner)
    walker = ProjectWalker(paths=paths)
    scanner = ProjectScanner(detected, paths, runner, knowledge=load_knowledge_data())
    return scanner.scan(
        probe_tools=probe_tools, git_info=git.describe(), source_stats=walker.stats()
    )


def analyzer_input(builder, *, deep: bool = False, project=None):
    return AnalyzerInput(
        project=project or build_context(builder),
        knowledge=load_knowledge_data(),
        root=builder.root,
        runner=CommandRunner(cwd=builder.root),
        deep=deep,
    )


def find(checks: list[HealthCheck], check_id: str) -> HealthCheck | None:
    return next((check for check in checks if check.id == check_id), None)


def ids(checks: list[HealthCheck]) -> set[str]:
    return {check.id for check in checks}


def problems(checks: list[HealthCheck]) -> set[str]:
    return {check.id for check in checks if check.is_problem}


# --- project analyzer ------------------------------------------------------
def test_lockfile_conflict_is_high(project):
    project.lockfile("package-lock.json")
    checks = ProjectAnalyzer(analyzer_input(project)).run()
    check = find(checks, "project.lockfile.conflict")
    assert check and check.severity is Severity.HIGH
    assert "package-lock.json" in check.detail


def test_single_lockfile_passes(project):
    checks = ProjectAnalyzer(analyzer_input(project)).run()
    assert find(checks, "project.lockfile").status is CheckStatus.PASS
    assert "project.lockfile.conflict" not in ids(checks)


def test_missing_lockfile_warns(builder):
    builder.write_package_json().android().ios()
    checks = ProjectAnalyzer(analyzer_input(builder)).run()
    assert find(checks, "project.lockfile.missing") is not None


def test_git_absence_warns(project):
    checks = ProjectAnalyzer(analyzer_input(project)).run()
    assert find(checks, "project.git.missing") is not None


def test_clean_git_passes(project):
    project.git_init()
    checks = ProjectAnalyzer(analyzer_input(project)).run()
    assert find(checks, "project.git").status is CheckStatus.PASS
    assert find(checks, "project.agent_dir").status is CheckStatus.PASS


def test_dirty_git_warns(project):
    project.git_init(dirty=True)
    checks = ProjectAnalyzer(analyzer_input(project)).run()
    assert find(checks, "project.git.dirty") is not None


def test_node_engine_is_read_from_installed_react_native(project):
    """The requirement comes from node_modules, not a hard-coded table."""
    project.installed("react-native", "0.81.0", engines={"node": ">= 20.19.4"}, peer={"react": "^19.1.0"})
    project.installed("react", "19.1.0")
    context = build_context(project, probe_tools=False)
    context.tooling.node = "22.11.0"
    checks = ProjectAnalyzer(analyzer_input(project, project=context)).run()
    check = find(checks, "project.node.version")
    assert check.status is CheckStatus.PASS
    assert "engines.node" in (check.source or "")


def test_node_engine_violation_fails(project):
    project.installed("react-native", "0.81.0", engines={"node": ">= 20.19.4"})
    context = build_context(project, probe_tools=False)
    context.tooling.node = "18.20.0"
    checks = ProjectAnalyzer(analyzer_input(project, project=context)).run()
    check = find(checks, "project.node.version")
    assert check.status is CheckStatus.FAIL
    assert check.severity is Severity.HIGH


def test_node_check_skipped_without_node(project):
    context = build_context(project, probe_tools=False)
    context.tooling.node = None
    checks = ProjectAnalyzer(analyzer_input(project, project=context)).run()
    assert find(checks, "project.node.version").status is CheckStatus.SKIP


# --- react native analyzer -------------------------------------------------
def test_react_peer_mismatch_is_critical(project):
    project.installed("react-native", "0.81.0", peer={"react": "^19.1.0"})
    project.installed("react", "18.3.1")
    checks = ReactNativeAnalyzer(analyzer_input(project)).run()
    check = find(checks, "rn.react.mismatch")
    assert check and check.severity is Severity.CRITICAL
    assert "peerDependencies" in (check.source or "")


def test_react_peer_match_passes(project):
    project.installed("react-native", "0.81.0", peer={"react": "^19.1.0"})
    project.installed("react", "19.1.0")
    checks = ReactNativeAnalyzer(analyzer_input(project)).run()
    assert find(checks, "rn.react").status is CheckStatus.PASS


def test_react_major_mismatch_uses_offline_table(builder):
    """Without node_modules only a *major* disagreement is asserted."""
    builder.write_package_json(dependencies={"react-native": "0.79.1", "react": "18.3.1"})
    builder.android().ios().lockfile("yarn.lock")
    checks = ReactNativeAnalyzer(analyzer_input(builder)).run()
    check = find(checks, "rn.react.mismatch")
    assert check and check.severity is Severity.HIGH
    assert check.source == "offline compatibility table"


def test_react_minor_difference_without_node_modules_is_not_a_problem(builder):
    builder.write_package_json(dependencies={"react-native": "0.79.1", "react": "19.0.0"})
    builder.android().ios().lockfile("yarn.lock")
    checks = ReactNativeAnalyzer(analyzer_input(builder)).run()
    assert "rn.react.mismatch" not in problems(checks)


def test_unknown_rn_series_skips_instead_of_guessing(builder):
    builder.write_package_json(dependencies={"react-native": "9.99.0", "react": "1.0.0"})
    builder.android().ios().lockfile("yarn.lock")
    checks = ReactNativeAnalyzer(analyzer_input(builder)).run()
    assert find(checks, "rn.react").status is CheckStatus.SKIP


def test_installed_version_outside_declared_range_fails(project):
    project.write_package_json(dependencies={"react-native": "^0.81.0"})
    project.installed("react-native", "0.79.1", peer={"react": "^19.0.0"})
    project.installed("react", "19.0.0")
    checks = ReactNativeAnalyzer(analyzer_input(project)).run()
    assert find(checks, "rn.version.drift") is not None


def test_legacy_babel_preset_fails(project):
    project.babel('module.exports = {presets: ["metro-react-native-babel-preset"]};\n')
    checks = ReactNativeAnalyzer(analyzer_input(project)).run()
    check = find(checks, "rn.babel.legacy_preset")
    assert check and check.severity is Severity.HIGH


def test_reanimated_without_plugin_fails(project):
    project.write_package_json(dependencies={"react-native-reanimated": "^3.16.0"})
    project.babel('module.exports = {presets: ["@react-native/babel-preset"]};\n')
    checks = ReactNativeAnalyzer(analyzer_input(project)).run()
    check = find(checks, "rn.babel.reanimated")
    assert check and check.severity is Severity.HIGH


def test_reanimated_with_plugin_passes(project):
    project.write_package_json(dependencies={"react-native-reanimated": "^3.16.0"})
    project.babel(
        'module.exports = {presets: ["@react-native/babel-preset"], plugins: ["react-native-reanimated/plugin"]};\n'
    )
    checks = ReactNativeAnalyzer(analyzer_input(project)).run()
    assert find(checks, "rn.babel").status is CheckStatus.PASS


def test_hermes_disabled_warns(project):
    project.android(hermes=False)
    checks = ReactNativeAnalyzer(analyzer_input(project)).run()
    assert find(checks, "rn.hermes").status is CheckStatus.WARN


def test_new_architecture_platform_mismatch_fails(project):
    project.android(new_arch=True)
    context = build_context(project)
    context.ios.new_architecture = False
    checks = ReactNativeAnalyzer(analyzer_input(project, project=context)).run()
    check = find(checks, "rn.new_arch.mismatch")
    assert check and check.severity is Severity.HIGH


def test_types_react_major_mismatch_warns(project):
    project.installed("react-native", "0.81.0", peer={"react": "^19.1.0"})
    project.installed("react", "19.1.0")
    project.installed("@types/react", "18.3.1")
    project.write_package_json(dev_dependencies={"@types/react": "^18.3.1"})
    checks = ReactNativeAnalyzer(analyzer_input(project)).run()
    assert "rn.types_react" in problems(checks) or "rn.types_react.major" in problems(checks)


# --- javascript analyzer ---------------------------------------------------
def test_deprecated_types_react_native_flagged(project):
    project.write_package_json(dev_dependencies={"@types/react-native": "^0.73.0"})
    checks = JavaScriptAnalyzer(analyzer_input(project)).run()
    check = find(checks, "js.deprecated.@types/react-native")
    assert check and check.severity is Severity.HIGH
    assert check.recommendation == "Uninstall @types/react-native."


def test_no_deprecated_packages_passes(project):
    checks = JavaScriptAnalyzer(analyzer_input(project)).run()
    assert find(checks, "js.deprecated").status is CheckStatus.PASS


def test_runtime_duplicate_react_is_critical(project):
    project.installed("some-lib", "1.0.0", nested={"react": "18.2.0"})
    project.installed("react-native", "0.81.0")
    project.write_package_json(dependencies={"some-lib": "^1.0.0"})
    checks = JavaScriptAnalyzer(analyzer_input(project)).run()
    check = find(checks, "js.duplicate_react")
    assert check and check.severity is Severity.CRITICAL


def test_types_only_duplicate_is_high_not_critical(project):
    project.installed("@types/react-native", "0.73.0", nested={"react-native": "0.73.0"})
    project.installed("react-native", "0.81.0")
    project.write_package_json(dev_dependencies={"@types/react-native": "^0.73.0"})
    checks = JavaScriptAnalyzer(analyzer_input(project)).run()
    check = find(checks, "js.duplicate_react.types")
    assert check and check.severity is Severity.HIGH
    assert "js.duplicate_react" not in problems(checks)


def test_peer_conflict_reported_from_real_metadata(project):
    project.installed("react", "19.1.0")
    project.installed("react-native", "0.81.0", peer={"react": "^19.1.0"})
    project.installed("react-redux", "8.1.3", peer={"react": "^16.8 || ^17.0 || ^18.0"})
    project.write_package_json(dependencies={"react-redux": "^8.1.3"})
    checks = JavaScriptAnalyzer(analyzer_input(project)).run()
    check = find(checks, "js.peer_dependencies.conflict")
    assert check is not None
    assert any("react-redux" in value for value in check.evidence.values())


def test_peer_check_skipped_without_node_modules(project):
    checks = JavaScriptAnalyzer(analyzer_input(project)).run()
    assert find(checks, "js.peer_dependencies").status is CheckStatus.SKIP


def test_undecidable_peer_specs_are_ignored(project):
    project.installed("react", "19.1.0")
    project.installed("weird-lib", "1.0.0", peer={"react": "workspace:*"})
    project.write_package_json(dependencies={"weird-lib": "^1.0.0"})
    checks = JavaScriptAnalyzer(analyzer_input(project)).run()
    assert "js.peer_dependencies.conflict" not in problems(checks)


def test_tsconfig_without_strict_warns(project):
    project.typescript(strict=False)
    checks = JavaScriptAnalyzer(analyzer_input(project)).run()
    assert find(checks, "js.tsconfig.strict") is not None


def test_missing_tsconfig_fails(project):
    (project.root / "tsconfig.json").unlink()
    checks = JavaScriptAnalyzer(analyzer_input(project)).run()
    check = find(checks, "js.tsconfig.missing")
    assert check and check.severity is Severity.HIGH


def test_duplicate_dependency_declaration_warns(project):
    project.write_package_json(
        dependencies={"axios": "^1.7.0"}, dev_dependencies={"axios": "^1.6.0"}
    )
    checks = JavaScriptAnalyzer(analyzer_input(project)).run()
    assert find(checks, "js.duplicate_declaration") is not None


def test_javascript_only_project_warns_about_types(builder):
    builder.write_package_json(dev_dependencies={})
    builder.metro().babel().lockfile("yarn.lock").android().ios()
    (builder.root / "package.json").write_text(
        (builder.root / "package.json").read_text().replace('"typescript": "^5.6.0",', "")
    )
    checks = JavaScriptAnalyzer(analyzer_input(builder)).run()
    assert find(checks, "js.typescript.missing") is not None


# --- android analyzer ------------------------------------------------------
def test_agp8_with_gradle7_is_critical(project):
    project.android(agp="8.1.1", gradle="7.6")
    checks = AndroidAnalyzer(analyzer_input(project)).run()
    check = find(checks, "android.agp.gradle")
    assert check and check.severity is Severity.CRITICAL


def test_agp8_with_gradle8_passes(project):
    project.android(agp="8.6.0", gradle="8.10.2")
    checks = AndroidAnalyzer(analyzer_input(project)).run()
    assert find(checks, "android.agp").status is CheckStatus.PASS


def test_unpinned_agp_is_skipped_not_guessed(project):
    project.android(agp=None)
    checks = AndroidAnalyzer(analyzer_input(project)).run()
    assert find(checks, "android.agp").status is CheckStatus.SKIP


def test_target_above_compile_sdk_is_critical(project):
    project.android(compile_sdk=34, target_sdk=35)
    checks = AndroidAnalyzer(analyzer_input(project)).run()
    check = find(checks, "android.sdk.target_above_compile")
    assert check and check.severity is Severity.CRITICAL


def test_play_policy_uses_dated_requirements(project):
    project.android(compile_sdk=33, target_sdk=33)
    checks = AndroidAnalyzer(analyzer_input(project)).run()
    check = find(checks, "android.play_policy")
    knowledge = load_knowledge_data()
    requirement = knowledge.required_target_sdk()
    assert requirement is not None and requirement.effective <= date.today()
    assert check and check.status is CheckStatus.FAIL
    assert str(requirement.target_sdk) in check.detail


def test_play_policy_satisfied_passes(project):
    requirement = load_knowledge_data().required_target_sdk()
    target = requirement.target_sdk if requirement else 35
    project.android(compile_sdk=target, target_sdk=target)
    checks = AndroidAnalyzer(analyzer_input(project)).run()
    # Either the in-force check passes, or it is superseded by the note about
    # next year's (unverified) requirement - both are non-problems.
    policy = find(checks, "android.play_policy") or find(checks, "android.play_policy.upcoming")
    assert policy is not None and not policy.is_problem


def test_missing_exported_attribute_is_critical(project):
    project.android(exported=False)
    checks = AndroidAnalyzer(analyzer_input(project)).run()
    check = find(checks, "android.manifest.exported")
    assert check and check.severity is Severity.CRITICAL


def test_missing_module_permission_names_it_and_the_line_to_add(project):
    """A count is not actionable: say which permission, and give the XML."""
    project.write_package_json(dependencies={"@react-native-firebase/messaging": "^21.0.0"})
    project.android(permissions=("android.permission.INTERNET",))
    checks = AndroidAnalyzer(analyzer_input(project)).run()
    check = find(checks, "android.permissions.missing")
    assert check and check.severity is Severity.HIGH
    # The name is in the line the developer reads, not only in --verbose evidence.
    assert "android.permission.POST_NOTIFICATIONS" in check.detail
    # And who wants it, so removing the module is a visible alternative.
    assert "@react-native-firebase/messaging" in check.detail
    assert check.fix == [
        "<!-- @react-native-firebase/messaging -->",
        '<uses-permission android:name="android.permission.POST_NOTIFICATIONS" />',
    ]
    # The file to edit is named, not left as "the manifest".
    assert "AndroidManifest.xml" in (check.recommendation or "")


def test_several_missing_permissions_are_all_listed(project):
    project.write_package_json(
        dependencies={
            "react-native-ble-plx": "^3.2.0",
            "react-native-vision-camera": "^4.6.0",
        }
    )
    project.android(permissions=())
    checks = AndroidAnalyzer(analyzer_input(project)).run()
    check = find(checks, "android.permissions.missing")

    assert check
    for name in (
        "android.permission.BLUETOOTH_SCAN",
        "android.permission.BLUETOOTH_CONNECT",
        "android.permission.CAMERA",
    ):
        assert name in check.detail
        assert f'<uses-permission android:name="{name}" />' in check.fix
    # Each permission carries the module that needs it, as a manifest comment.
    assert check.fix.count("<!-- react-native-ble-plx -->") == 2
    assert "these" in (check.recommendation or "")


def test_a_permission_two_modules_want_names_both(project):
    project.write_package_json(
        dependencies={
            "@react-native-community/geolocation": "^3.3.0",
            "react-native-geolocation-service": "^5.3.1",
        }
    )
    project.android(permissions=())
    checks = AndroidAnalyzer(analyzer_input(project)).run()
    check = find(checks, "android.permissions.missing")

    assert check
    # One permission, two dependents: the XML must not be duplicated, and both
    # modules must be named or removing one looks sufficient.
    assert check.fix.count('<uses-permission android:name="android.permission.ACCESS_FINE_LOCATION" />') == 1
    comment = next(line for line in check.fix if line.startswith("<!--"))
    assert "@react-native-community/geolocation" in comment
    assert "react-native-geolocation-service" in comment


def test_declared_module_permission_passes(project):
    project.write_package_json(dependencies={"@react-native-firebase/messaging": "^21.0.0"})
    project.android(
        permissions=("android.permission.INTERNET", "android.permission.POST_NOTIFICATIONS")
    )
    checks = AndroidAnalyzer(analyzer_input(project)).run()
    assert find(checks, "android.permissions").status is CheckStatus.PASS


def test_android_checks_skipped_when_absent(builder):
    builder.write_package_json(dependencies={"expo": "^52.0.0"}).lockfile("yarn.lock")
    checks = AndroidAnalyzer(analyzer_input(builder)).run()
    assert len(checks) == 1
    assert checks[0].status is CheckStatus.SKIP


# --- ios analyzer ----------------------------------------------------------
def test_pods_react_native_mismatch_is_high(project):
    project.write_package_json(dependencies={"react-native": "0.81.0"})
    project.ios(pods_rn="0.79.1")
    checks = IOSAnalyzer(analyzer_input(project)).run()
    check = find(checks, "ios.pods.rn_match")
    assert check and check.severity is Severity.HIGH


def test_pods_react_native_match_passes(project):
    project.write_package_json(dependencies={"react-native": "0.81.0"})
    project.ios(pods_rn="0.81.0")
    checks = IOSAnalyzer(analyzer_input(project)).run()
    assert find(checks, "ios.pods.rn_match").status is CheckStatus.PASS


def test_missing_podfile_lock_fails(project):
    (project.root / "ios" / "Podfile.lock").unlink()
    checks = IOSAnalyzer(analyzer_input(project)).run()
    check = find(checks, "ios.podfile_lock.missing")
    assert check and check.severity is Severity.HIGH


def test_pods_not_installed_warns(project):
    project.ios(pods_installed=False)
    checks = IOSAnalyzer(analyzer_input(project)).run()
    assert find(checks, "ios.pods.not_installed") is not None


def test_deployment_target_mismatch_warns(project):
    project.ios(deployment_target="14.0", podfile_platform="15.1")
    checks = IOSAnalyzer(analyzer_input(project)).run()
    assert find(checks, "ios.deployment_target.mismatch") is not None


def test_deployment_target_helper_is_not_a_mismatch(project):
    project.ios(deployment_target="15.1", podfile_platform="min_ios_version_supported")
    checks = IOSAnalyzer(analyzer_input(project)).run()
    assert find(checks, "ios.deployment_target").status is CheckStatus.PASS


def test_missing_usage_description_names_the_key_and_the_plist_entry(project):
    project.write_package_json(dependencies={"react-native-vision-camera": "^4.6.0"})
    project.ios(usage_descriptions=())
    checks = IOSAnalyzer(analyzer_input(project)).run()
    check = find(checks, "ios.usage_descriptions")
    assert check and check.severity is Severity.CRITICAL
    assert "NSCameraUsageDescription" in check.detail
    assert "react-native-vision-camera" in check.detail
    assert check.fix == [
        "<!-- react-native-vision-camera -->",
        "<key>NSCameraUsageDescription</key>",
        "<string>Explain why the app needs this</string>",
    ]
    # An empty string is an App Store rejection, so the entry ships a prompt to
    # replace rather than an empty <string/>.
    assert "replacing each placeholder" in (check.recommendation or "")
    assert "Info.plist" in (check.recommendation or "")


def test_present_usage_description_passes(project):
    project.write_package_json(dependencies={"react-native-vision-camera": "^4.6.0"})
    project.ios(usage_descriptions=("NSCameraUsageDescription",))
    checks = IOSAnalyzer(analyzer_input(project)).run()
    assert find(checks, "ios.usage_descriptions").status is CheckStatus.PASS


def test_optional_usage_keys_do_not_fail(project):
    project.write_package_json(dependencies={"react-native-maps": "^1.18.0"})
    project.ios(usage_descriptions=())
    checks = IOSAnalyzer(analyzer_input(project)).run()
    assert find(checks, "ios.usage_descriptions").status is CheckStatus.PASS


def test_missing_privacy_manifest_warns(project):
    project.ios(privacy_manifest=False)
    checks = IOSAnalyzer(analyzer_input(project)).run()
    check = find(checks, "ios.privacy_manifest")
    assert check and check.status is CheckStatus.WARN
    assert check.source


def test_push_module_without_entitlements_warns(project):
    project.write_package_json(dependencies={"@react-native-firebase/messaging": "^21.0.0"})
    project.ios(entitlements=False)
    checks = IOSAnalyzer(analyzer_input(project)).run()
    assert find(checks, "ios.entitlements.missing") is not None


def test_push_module_with_entitlements_passes(project):
    project.write_package_json(dependencies={"@react-native-firebase/messaging": "^21.0.0"})
    project.ios(entitlements=True)
    checks = IOSAnalyzer(analyzer_input(project)).run()
    assert find(checks, "ios.entitlements").status is CheckStatus.PASS


# --- scoring ---------------------------------------------------------------
def make_check(severity: Severity, status: CheckStatus = CheckStatus.FAIL) -> HealthCheck:
    return HealthCheck(
        id=f"x.{severity.value}", category="project", title="t", status=status, severity=severity
    )


def test_score_is_explainable():
    report = HealthReport(
        checks=[
            make_check(Severity.CRITICAL),
            make_check(Severity.HIGH),
            make_check(Severity.MEDIUM),
            make_check(Severity.LOW),
        ]
    )
    # 100 - (10 + 5 + 2 + 1)
    assert report.score == 82
    assert report.grade == "good"


def test_passing_and_skipped_checks_cost_nothing():
    report = HealthReport(
        checks=[
            make_check(Severity.CRITICAL, CheckStatus.PASS),
            make_check(Severity.HIGH, CheckStatus.SKIP),
        ]
    )
    assert report.score == 100
    assert report.grade == "excellent"
    assert report.problems == []


def test_score_never_goes_negative():
    report = HealthReport(checks=[make_check(Severity.CRITICAL) for _ in range(20)])
    assert report.score == 0
    assert report.grade == "at risk"


def test_counts_and_categories():
    report = HealthReport(
        checks=[
            HealthCheck(id="a", category="android", title="a", status=CheckStatus.FAIL, severity=Severity.CRITICAL),
            HealthCheck(id="b", category="ios", title="b", status=CheckStatus.PASS),
            HealthCheck(id="c", category="ios", title="c", status=CheckStatus.SKIP),
        ]
    )
    counts = report.counts()
    assert counts["critical"] == 1
    assert counts["passed"] == 1
    assert counts["skipped"] == 1
    categories = report.categories()
    assert categories["ios"]["total"] == 2
    assert categories["android"]["problems"] == 1


def test_problems_are_sorted_by_severity():
    report = HealthReport(
        checks=[make_check(Severity.LOW), make_check(Severity.CRITICAL), make_check(Severity.HIGH)]
    )
    assert [check.severity for check in report.problems] == [
        Severity.CRITICAL,
        Severity.HIGH,
        Severity.LOW,
    ]


# --- full run --------------------------------------------------------------
def test_healthy_project_scores_well(project):
    project.git_init()
    project.installed("react-native", "0.81.0", peer={"react": "^19.1.0"}, engines={"node": ">=18"})
    project.installed("react", "19.1.0")
    project.installed("typescript", "5.6.0")
    requirement = load_knowledge_data().required_target_sdk()
    target = requirement.target_sdk if requirement else 35
    project.android(compile_sdk=target, target_sdk=target)
    project.ios(pods_rn="0.81.0", privacy_manifest=True)

    context = build_context(project, probe_tools=False)
    context.tooling.node = "20.19.4"
    data = analyzer_input(project, project=context)
    checks: list[HealthCheck] = []
    for analyzer_class in ANALYZERS:
        checks.extend(analyzer_class(data).run())
    report = HealthReport(checks=checks, rn_version=context.rn_version)

    assert report.critical == []
    assert report.score >= 90, [check.id for check in report.problems]


def test_every_analyzer_survives_a_minimal_project(builder):
    """A bare project must produce skips, not exceptions."""
    builder.write_package_json()
    context = build_context(builder, probe_tools=False)
    data = analyzer_input(builder, project=context)
    for analyzer_class in ANALYZERS:
        checks = analyzer_class(data).run()
        assert isinstance(checks, list) and checks


# ---------------------------------------------------------------------------
# a finding names what it found
# ---------------------------------------------------------------------------
def test_a_finding_names_a_few_items_and_counts_the_rest():
    """Naming everything can be a page; naming nothing is useless."""
    assert summarize([]) == ""
    assert summarize(["b", "a"]) == "a; b"
    assert summarize(["c", "b", "a"]) == "a; b; c"
    assert summarize(["d", "c", "b", "a"]) == "a; b; c; and 1 more"
    assert summarize(["d", "c", "b", "a"], limit=4) == "a; b; c; d"


def test_unsatisfied_peers_are_named_not_just_counted(project):
    project.write_package_json(
        dependencies={"react": "19.1.0", "react-native": "0.81.0", "some-lib": "1.0.0"}
    )
    project.installed("react", "19.1.0")
    project.installed("react-native", "0.81.0")
    project.installed("some-lib", "1.0.0", peer={"react": ">=20.0.0"})
    checks = JavaScriptAnalyzer(analyzer_input(project)).run()
    check = find(checks, "js.peer_dependencies.conflict")

    assert check
    # The package and the requirement are in the line the developer reads.
    assert "some-lib" in check.detail
    assert ">=20.0.0" in check.detail
