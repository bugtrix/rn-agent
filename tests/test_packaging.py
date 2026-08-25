"""Packaging consistency.

The npm wrapper and the Python package are released together, so a version
mismatch would ship a CLI that reports one version and behaves like another.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from rn_agent import __version__
from rn_agent.constants import APP_VERSION

ROOT = Path(__file__).resolve().parents[1]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_version_is_consistent_everywhere():
    pyproject = read("pyproject.toml")
    package_json = json.loads(read("npm/package.json"))
    match = re.search(r'^version = "([^"]+)"', pyproject, re.MULTILINE)
    assert match, "pyproject.toml has no version"
    assert match.group(1) == __version__ == APP_VERSION == package_json["version"]


def test_npm_wrapper_files_exist():
    for relative in (
        "npm/package.json",
        "npm/bin/rn-agent.js",
        "npm/lib/install.js",
        "npm/lib/runtime.js",
    ):
        assert (ROOT / relative).is_file(), relative


def test_npm_package_declares_the_bin_and_postinstall():
    package_json = json.loads(read("npm/package.json"))
    assert package_json["bin"]["rn-agent"] == "bin/rn-agent.js"
    assert "install.js" in package_json["scripts"]["postinstall"]
    assert package_json["engines"]["node"].startswith(">=18")


def test_console_script_is_declared():
    assert 'rn-agent = "rn_agent.cli.app:main"' in read("pyproject.toml")


def test_knowledge_data_is_packaged():
    assert 'rn_agent = ["knowledge/data/*.yaml"' in read("pyproject.toml")
    data_dir = ROOT / "src" / "rn_agent" / "knowledge" / "data"
    assert {path.name for path in data_dir.glob("*.yaml")} == {
        "advisories.yaml",
        "libraries.yaml",
    }


def test_every_subpackage_is_importable_and_therefore_shipped():
    """`packages.find` only picks up directories with an ``__init__.py``.

    A provider stack that is not a package would install as an empty namespace
    and fail at `rn-agent login` on a user's machine, not in this repo.
    """
    src = ROOT / "src" / "rn_agent"
    packages = {path.parent.name for path in src.rglob("__init__.py")}
    assert {"ai", "auth", "net", "tui", "agents", "validation", "migration", "upgrade"} <= packages

    from rn_agent.ai import provider_names
    from rn_agent.auth import BACKENDS

    assert set(provider_names()) == {"anthropic", "openai", "google", "vertex", "cursor", "ollama"}
    assert "file" in BACKENDS


def test_the_type_marker_is_present_because_pyproject_ships_it():
    """``package-data`` promises ``py.typed``; a missing file breaks consumers."""
    assert (ROOT / "src" / "rn_agent" / "py.typed").is_file()
    assert "py.typed" in read("pyproject.toml")


def test_prompt_toolkit_is_declared_because_the_terminal_needs_it():
    """The interactive terminal is not optional, so neither is its dependency."""
    assert "prompt_toolkit" in read("pyproject.toml")


def test_wrapper_requires_python_311_or_newer():
    runtime = read("npm/lib/runtime.js")
    assert "MIN_PYTHON = [3, 11]" in runtime
    # newest first, so a machine with several interpreters picks the best one
    assert runtime.index('"python3.13"') < runtime.index('"python3.11"')


def test_wrapper_never_hard_fails_npm_install():
    """A machine without Python must not break an unrelated `npm ci`."""
    install = read("npm/lib/install.js")
    assert "process.exit(0)" in install


def test_wrapper_forwards_the_exit_code():
    binary = read("npm/bin/rn-agent.js")
    assert "process.exit(result.status" in binary
    assert 'stdio: "inherit"' in binary


def test_project_config_never_stores_credentials():
    """§7: the project config must not be a place secrets can land."""
    from rn_agent.core.config import DEFAULT_CONFIG_YAML

    lowered = DEFAULT_CONFIG_YAML.lower()
    # No key may *hold* a credential. `allow_secret_files` is a policy flag
    # about which files may enter an AI prompt, so it is explicitly fine.
    for forbidden in ("api_key", "apikey", "password", "bearer", "auth_token", "secret:"):
        assert forbidden not in lowered, forbidden
    # ...and it says so, pointing at the OS keychain instead.
    assert "keychain" in lowered


def test_config_model_has_no_credential_fields():
    from rn_agent.models.config import AgentConfig

    fields = set(AgentConfig.model_json_schema()["$defs"]["AIConfig"]["properties"])
    assert not fields & {"api_key", "token", "secret", "password"}
