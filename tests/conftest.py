"""Test fixtures.

Everything runs against synthetic React Native projects built in ``tmp_path``.
No test touches a real project, the network, an AI provider, the developer's
user config or their OS keychain - autouse fixtures below make each of those
fail loudly or point somewhere disposable.
"""

from __future__ import annotations

import json
import plistlib
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from rn_agent.ai.registry import specs
from rn_agent.constants import ENV_HOME, ENV_KEYCHAIN
from rn_agent.core.context import AgentContext
from rn_agent.core.paths import AgentPaths
from rn_agent.knowledge.data import load_knowledge_data
from rn_agent.models.config import AgentConfig
from rn_agent.net.http import HttpResponse
from rn_agent.project.detector import detect_project
from rn_agent.runner.command_runner import CommandRunner

DEFAULT_DEPENDENCIES = {
    "react": "19.1.0",
    "react-native": "0.81.0",
}
DEFAULT_DEV_DEPENDENCIES = {
    "typescript": "^5.6.0",
    "jest": "^29.7.0",
}


@dataclass
class ProjectBuilder:
    """Builds a synthetic RN project with just the pieces a test needs."""

    root: Path
    package_json: dict[str, Any] = field(default_factory=dict)

    # -- package.json ------------------------------------------------------
    def write_package_json(
        self,
        *,
        name: str = "demo-app",
        dependencies: dict[str, str] | None = None,
        dev_dependencies: dict[str, str] | None = None,
        scripts: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> ProjectBuilder:
        payload: dict[str, Any] = {
            "name": name,
            "version": "1.0.0",
            "private": True,
            "scripts": scripts or {"android": "react-native run-android", "test": "jest"},
            "dependencies": {**DEFAULT_DEPENDENCIES, **(dependencies or {})},
            "devDependencies": {**DEFAULT_DEV_DEPENDENCIES, **(dev_dependencies or {})},
        }
        payload.update(extra or {})
        self.package_json = payload
        (self.root / "package.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return self

    def lockfile(self, name: str = "yarn.lock", content: str = "") -> ProjectBuilder:
        (self.root / name).write_text(content or "# lockfile\n", encoding="utf-8")
        return self

    def yarn_lock(self, entries: dict[str, str]) -> ProjectBuilder:
        """``{"react-native@0.81.0": "0.81.0"}`` -> a yarn v1 lockfile."""
        lines = ["# yarn lockfile v1", ""]
        for key, version in entries.items():
            lines.append(f"{key}:")
            lines.append(f'  version "{version}"')
            lines.append("")
        (self.root / "yarn.lock").write_text("\n".join(lines), encoding="utf-8")
        return self

    def installed(
        self,
        name: str,
        version: str,
        *,
        peer: dict[str, str] | None = None,
        engines: dict[str, str] | None = None,
        native: tuple[str, ...] = (),
        nested: dict[str, str] | None = None,
    ) -> ProjectBuilder:
        """Create ``node_modules/<name>`` with real metadata on disk."""
        package_dir = self.root / "node_modules" / Path(*name.split("/"))
        package_dir.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {"name": name, "version": version}
        if peer:
            payload["peerDependencies"] = peer
        if engines:
            payload["engines"] = engines
        (package_dir / "package.json").write_text(json.dumps(payload), encoding="utf-8")
        for platform in native:
            (package_dir / platform).mkdir(exist_ok=True)
        for nested_name, nested_version in (nested or {}).items():
            nested_dir = package_dir / "node_modules" / Path(*nested_name.split("/"))
            nested_dir.mkdir(parents=True, exist_ok=True)
            (nested_dir / "package.json").write_text(
                json.dumps({"name": nested_name, "version": nested_version}), encoding="utf-8"
            )
        return self

    # -- config files ------------------------------------------------------
    def typescript(self, *, strict: bool = True, extends: str | None = "@react-native/typescript-config") -> ProjectBuilder:
        payload: dict[str, Any] = {"compilerOptions": {"strict": strict}}
        if extends:
            payload["extends"] = extends
        (self.root / "tsconfig.json").write_text(json.dumps(payload), encoding="utf-8")
        return self

    def metro(self, content: str | None = None) -> ProjectBuilder:
        (self.root / "metro.config.js").write_text(
            content
            or 'const {getDefaultConfig} = require("@react-native/metro-config");\nmodule.exports = getDefaultConfig(__dirname);\n',
            encoding="utf-8",
        )
        return self

    def babel(self, content: str | None = None) -> ProjectBuilder:
        (self.root / "babel.config.js").write_text(
            content or 'module.exports = {presets: ["@react-native/babel-preset"]};\n',
            encoding="utf-8",
        )
        return self

    def eslint(self) -> ProjectBuilder:
        (self.root / ".eslintrc.js").write_text("module.exports = {};\n", encoding="utf-8")
        return self

    def source_tree(self, *relative: str) -> ProjectBuilder:
        for entry in relative:
            path = self.root / entry
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("export const value = 1;\n", encoding="utf-8")
        return self

    # -- android -----------------------------------------------------------
    def android(
        self,
        *,
        gradle: str = "8.10.2",
        agp: str | None = "8.6.0",
        kotlin: str = "2.0.21",
        compile_sdk: int = 35,
        target_sdk: int = 35,
        min_sdk: int = 24,
        java: str = "17",
        new_arch: bool = False,
        hermes: bool = True,
        permissions: tuple[str, ...] = ("android.permission.INTERNET",),
        exported: bool = True,
        use_ext_indirection: bool = True,
    ) -> ProjectBuilder:
        android_dir = self.root / "android"
        (android_dir / "app" / "src" / "main").mkdir(parents=True, exist_ok=True)
        (android_dir / "gradle" / "wrapper").mkdir(parents=True, exist_ok=True)

        (android_dir / "gradle" / "wrapper" / "gradle-wrapper.properties").write_text(
            f"distributionUrl=https\\://services.gradle.org/distributions/gradle-{gradle}-all.zip\n",
            encoding="utf-8",
        )
        classpath = (
            f'        classpath("com.android.tools.build:gradle:{agp}")'
            if agp
            else '        classpath("com.android.tools.build:gradle")'
        )
        (android_dir / "build.gradle").write_text(
            "buildscript {\n"
            "    ext {\n"
            f"        buildToolsVersion = \"35.0.0\"\n"
            f"        minSdkVersion = {min_sdk}\n"
            f"        compileSdkVersion = {compile_sdk}\n"
            f"        targetSdkVersion = {target_sdk}\n"
            f"        kotlinVersion = \"{kotlin}\"\n"
            "    }\n"
            "    dependencies {\n"
            f"{classpath}\n"
            "    }\n"
            "}\n",
            encoding="utf-8",
        )
        if use_ext_indirection:
            sdk_block = (
                "    compileSdk rootProject.ext.compileSdkVersion\n"
                "    defaultConfig {\n"
                "        applicationId \"com.demo.app\"\n"
                "        minSdkVersion rootProject.ext.minSdkVersion\n"
                "        targetSdkVersion rootProject.ext.targetSdkVersion\n"
                "    }\n"
            )
        else:
            sdk_block = (
                f"    compileSdk {compile_sdk}\n"
                "    defaultConfig {\n"
                "        applicationId \"com.demo.app\"\n"
                f"        minSdkVersion {min_sdk}\n"
                f"        targetSdkVersion {target_sdk}\n"
                "    }\n"
            )
        (android_dir / "app" / "build.gradle").write_text(
            "android {\n"
            '    namespace "com.demo.app"\n'
            f"{sdk_block}"
            f"    compileOptions {{ sourceCompatibility JavaVersion.VERSION_{java} }}\n"
            "}\n",
            encoding="utf-8",
        )
        (android_dir / "gradle.properties").write_text(
            f"newArchEnabled={'true' if new_arch else 'false'}\n"
            f"hermesEnabled={'true' if hermes else 'false'}\n",
            encoding="utf-8",
        )
        permission_lines = "\n".join(
            f'  <uses-permission android:name="{permission}" />' for permission in permissions
        )
        exported_attr = ' android:exported="true"' if exported else ""
        (android_dir / "app" / "src" / "main" / "AndroidManifest.xml").write_text(
            '<manifest xmlns:android="http://schemas.android.com/apk/res/android">\n'
            f"{permission_lines}\n"
            '  <application android:name=".MainApplication">\n'
            f'    <activity android:name=".MainActivity"{exported_attr}>\n'
            "      <intent-filter>\n"
            '        <action android:name="android.intent.action.MAIN" />\n'
            "      </intent-filter>\n"
            "    </activity>\n"
            "  </application>\n"
            "</manifest>\n",
            encoding="utf-8",
        )
        return self

    # -- ios ---------------------------------------------------------------
    def ios(
        self,
        *,
        project: str = "Demo",
        deployment_target: str = "15.1",
        podfile_platform: str | None = "min_ios_version_supported",
        cocoapods: str | None = "1.15.2",
        pods_rn: str | None = None,
        pods_installed: bool = True,
        privacy_manifest: bool = True,
        usage_descriptions: tuple[str, ...] = (),
        workspace: bool = True,
        entitlements: bool = False,
    ) -> ProjectBuilder:
        ios_dir = self.root / "ios"
        (ios_dir / f"{project}.xcodeproj").mkdir(parents=True, exist_ok=True)
        (ios_dir / project).mkdir(parents=True, exist_ok=True)
        if workspace:
            (ios_dir / f"{project}.xcworkspace").mkdir(exist_ok=True)
        pods_dir = ios_dir / "Pods"
        if pods_installed:
            pods_dir.mkdir(exist_ok=True)
        elif pods_dir.exists():
            shutil.rmtree(pods_dir)

        platform_line = (
            f"platform :ios, {podfile_platform}\n"
            if podfile_platform and not podfile_platform[0].isdigit()
            else f"platform :ios, '{podfile_platform}'\n"
            if podfile_platform
            else ""
        )
        (ios_dir / "Podfile").write_text(
            platform_line + "use_frameworks! :linkage => :static\n", encoding="utf-8"
        )
        lock_lines = []
        if pods_rn:
            lock_lines.append("PODS:")
            lock_lines.append(f"  - React-Core ({pods_rn})")
        lock_lines.append("")
        lock_lines.append("PODFILE CHECKSUM: deadbeef")
        if cocoapods:
            lock_lines.append("")
            lock_lines.append(f"COCOAPODS: {cocoapods}")
        (ios_dir / "Podfile.lock").write_text("\n".join(lock_lines) + "\n", encoding="utf-8")

        (ios_dir / f"{project}.xcodeproj" / "project.pbxproj").write_text(
            "objects = {\n"
            f"    IPHONEOS_DEPLOYMENT_TARGET = {deployment_target};\n"
            "    PRODUCT_BUNDLE_IDENTIFIER = com.demo.app;\n"
            "};\n",
            encoding="utf-8",
        )
        plist: dict[str, Any] = {
            "CFBundleDisplayName": project,
            "CFBundleIdentifier": "com.demo.app",
        }
        for key in usage_descriptions:
            plist[key] = "Because the app needs it"
        (ios_dir / project / "Info.plist").write_bytes(plistlib.dumps(plist))
        privacy_path = ios_dir / project / "PrivacyInfo.xcprivacy"
        if privacy_manifest:
            privacy_path.write_bytes(plistlib.dumps({}))
        elif privacy_path.exists():
            privacy_path.unlink()
        if entitlements:
            (ios_dir / project / f"{project}.entitlements").write_bytes(
                plistlib.dumps({"aps-environment": "development"})
            )
        (ios_dir / project / "AppDelegate.swift").write_text("// app delegate\n", encoding="utf-8")
        return self

    # -- git ---------------------------------------------------------------
    def git_init(self, *, commit: bool = True, dirty: bool = False) -> ProjectBuilder:
        run = lambda *args: subprocess.run(  # noqa: E731
            ["git", *args], cwd=self.root, capture_output=True, text=True, check=False
        )
        run("init", "-q")
        run("config", "user.email", "test@example.com")
        run("config", "user.name", "Test")
        (self.root / ".gitignore").write_text(".rn-agent/cache/\n.rn-agent/logs/\n.rn-agent/knowledge/\n", encoding="utf-8")
        if commit:
            run("add", "-A")
            run("commit", "-qm", "initial")
        if dirty:
            (self.root / "package.json").write_text(
                json.dumps({**self.package_json, "version": "1.0.1"}, indent=2), encoding="utf-8"
            )
        return self

    # -- helpers -----------------------------------------------------------
    def full(self, **overrides: Any) -> ProjectBuilder:
        """A realistic, healthy project."""
        self.write_package_json(
            dependencies=overrides.pop("dependencies", None),
            dev_dependencies=overrides.pop("dev_dependencies", None),
        )
        self.typescript()
        self.metro()
        self.babel()
        self.eslint()
        self.lockfile("yarn.lock")
        self.android(**overrides.pop("android", {}))
        self.ios(**overrides.pop("ios", {}))
        self.source_tree(
            "src/components/Button.tsx",
            "src/screens/HomeScreen.tsx",
            "src/services/api.ts",
            "src/store/index.ts",
            "src/hooks/useThing.ts",
            "__tests__/App.test.tsx",
        )
        return self

    def paths(self) -> AgentPaths:
        return AgentPaths.for_project(self.root)

    def context(self, **kwargs: Any) -> AgentContext:
        """Build the shared AgentContext exactly like the CLI does."""
        detected = detect_project(self.root)
        paths = AgentPaths.for_project(self.root)
        config = kwargs.pop("config", None) or AgentConfig()
        return AgentContext(
            detected=detected,
            paths=paths,
            config=config,
            command=kwargs.pop("command", "test"),
            **kwargs,
        )

    def scanned(self, **kwargs: Any) -> AgentContext:
        """A context whose brain is populated, as after `rn-agent scan`.

        Commands past phase 1 read ``context.project``; building it here keeps
        every test from re-implementing the scan.
        """
        from rn_agent.project.scanner import ProjectScanner

        context = self.context(**kwargs)
        scanner = ProjectScanner(
            context.detected, context.paths, context.runner, knowledge=context.knowledge
        )
        context.set_project(
            scanner.scan(probe_tools=False, source_stats=context.walker.stats())
        )
        return context

    def local_bin(self, name: str, *, exit_code: int = 0, output: str = "") -> Path:
        """A stand-in for a locally installed node tool (``node_modules/.bin``).

        The validator deliberately runs the project's own binaries, so a test
        that needs a passing or failing check installs one of these.
        """
        target = self.root / "node_modules" / ".bin" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        body = f"echo {output!r}\n" if output else ""
        target.write_text(f"#!/bin/sh\n{body}exit {exit_code}\n", encoding="utf-8")
        target.chmod(0o755)
        return target


@pytest.fixture
def builder(tmp_path: Path) -> ProjectBuilder:
    root = tmp_path / "app"
    root.mkdir()
    return ProjectBuilder(root=root)


@pytest.fixture
def project(builder: ProjectBuilder) -> ProjectBuilder:
    """A complete, healthy synthetic project."""
    return builder.full()


@pytest.fixture
def knowledge():
    return load_knowledge_data()


@pytest.fixture
def runner(builder: ProjectBuilder) -> CommandRunner:
    return CommandRunner(cwd=builder.root)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may reach the network or an AI provider."""

    def forbidden(*args, **kwargs):  # pragma: no cover - guard
        raise AssertionError("tests must not perform network I/O")

    monkeypatch.setattr("httpx.Client.request", forbidden, raising=False)
    monkeypatch.setattr("httpx.AsyncClient.request", forbidden, raising=False)


@pytest.fixture(autouse=True)
def _isolated_user_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may read or write the real user config, keychain or API keys."""
    home = tmp_path / "user-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv(ENV_HOME, str(home))
    # The 0600 file backend is the only one that works identically everywhere.
    monkeypatch.setenv(ENV_KEYCHAIN, "file")
    for spec in specs():
        if spec.env_var:
            monkeypatch.delenv(spec.env_var, raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)


@dataclass
class FakeTransport:
    """Replays queued HTTP responses and records what was sent.

    Providers take a transport, so a test can assert the exact request shape
    without a socket - and an unexpected call fails instead of escaping.
    """

    replies: list[HttpResponse | Exception] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def queue(self, *, status: int = 200, body: dict[str, Any] | None = None, text: str = "") -> FakeTransport:
        self.replies.append(HttpResponse(status=status, body=body or {}, text=text))
        return self

    def fail(self, error: Exception) -> FakeTransport:
        self.replies.append(error)
        return self

    @property
    def last(self) -> dict[str, Any]:
        assert self.calls, "no request was made"
        return self.calls[-1]

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Any,
        payload: dict[str, Any] | None = None,
        timeout: float = 120.0,
    ) -> HttpResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "payload": payload,
                "timeout": timeout,
            }
        )
        if not self.replies:
            raise AssertionError(f"unexpected {method} {url}")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def wired_transport(monkeypatch: pytest.MonkeyPatch, transport: FakeTransport) -> FakeTransport:
    """The transport every provider built without an explicit one will use."""
    monkeypatch.setattr("rn_agent.ai.provider.default_transport", lambda: transport)
    return transport


# ---------------------------------------------------------------------------
# AI-backed commands
# ---------------------------------------------------------------------------
AI_MODEL = "claude-sonnet-4-5"
AI_KEY = "sk-ant-test-0123456789abcdef"


def anthropic_body(
    text: str,
    *,
    model: str = AI_MODEL,
    stop_reason: str = "end_turn",
    input_tokens: int = 120,
    output_tokens: int = 90,
) -> dict[str, Any]:
    """An Anthropic Messages response carrying ``text``."""
    return {
        "id": "msg_test",
        "model": model,
        "stop_reason": stop_reason,
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


@dataclass
class FakeAI:
    """Queues model replies for the provider the commands will build.

    The whole provider stack is exercised (payload shape, headers, parsing,
    accounting); only the socket is replaced.
    """

    transport: FakeTransport

    def reply(self, payload: Any, **kwargs: Any) -> FakeAI:
        """Queue one reply. A dict/list is sent as JSON, a string verbatim."""
        text = payload if isinstance(payload, str) else json.dumps(payload)
        self.transport.queue(body=anthropic_body(text, **kwargs))
        return self

    def raw(self, *, status: int = 200, body: dict[str, Any] | None = None) -> FakeAI:
        self.transport.queue(status=status, body=body or {})
        return self

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.transport.calls

    @property
    def last_prompt(self) -> str:
        """Every message of the last request, flattened - for asserting context."""
        payload = self.transport.last["payload"] or {}
        parts = [str(payload.get("system") or "")]
        parts.extend(str(message.get("content", "")) for message in payload.get("messages", []))
        return "\n".join(parts)


@pytest.fixture
def ai_config() -> AgentConfig:
    """Configuration with a provider selected, as `rn-agent login` would leave it."""
    config = AgentConfig()
    config.ai.provider = "anthropic"
    config.ai.model = AI_MODEL
    return config


@pytest.fixture
def fake_ai(monkeypatch: pytest.MonkeyPatch, wired_transport: FakeTransport) -> FakeAI:
    """A configured, credentialed provider whose transport is a queue."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", AI_KEY)
    return FakeAI(transport=wired_transport)


@pytest.fixture
def ai_project(project: ProjectBuilder) -> ProjectBuilder:
    """A project whose `.rn-agent/config.yaml` selects a provider and model."""
    import yaml

    paths = project.paths()
    paths.ensure()
    paths.config_file.write_text(
        yaml.safe_dump({"ai": {"provider": "anthropic", "model": AI_MODEL}}),
        encoding="utf-8",
    )
    return project
