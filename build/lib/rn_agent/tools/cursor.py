"""The Cursor CLI, installed and updated by rn-agent instead of by you.

Cursor's documented install is ``curl https://cursor.com/install -fsS | bash``,
which unpacks into ``~/.local/share``, symlinks into ``~/.local/bin`` and then
tells you to edit your shell profile. That is three things this agent has no
business doing to a developer's machine, and it is the reason this module exists.

What it does instead: reads the version out of Cursor's own installer, downloads
the same versioned artefact that script would download, and unpacks it under
rn-agent's directory.

    ~/.config/rn-agent/tools/cursor-agent/<version>/cursor-agent

Nothing is placed on ``PATH``, no profile is touched, and no script is piped into
a shell. The binary is invoked by absolute path.

Honest limitations, stated rather than papered over:

* **Cursor publishes no checksum** for these artefacts, so there is none to
  verify. The transport is HTTPS to Cursor's own CDN and the version is pinned
  and recorded; that is the whole of the guarantee.
* **The artefact is large** (~75 MB). It is fetched once, on first use, after the
  developer says yes - never during `pip install` or `npm install`.
* **A tool you already installed wins.** If ``cursor-agent`` or ``agent`` is on
  your ``PATH``, that one is used and nothing is downloaded.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..core.logging import get_logger
from ..core.paths import user_config_dir
from ..errors import RNAgentError, TransportError
from ..net.http import FileTransport, JsonTransport, default_downloader, default_transport
from ..runner.command_runner import CommandRunner

#: Where Cursor's own installer script lives. It carries the current version, so
#: parsing it is how "latest" is resolved without inventing an API.
INSTALLER_URL = "https://cursor.com/install"

#: The artefact the installer downloads: one tarball per platform, one top-level
#: directory (``dist-package/``) holding the executable.
ARTEFACT_URL = "https://downloads.cursor.com/lab/{version}/{os}/{arch}/agent-cli-package.tar.gz"

EXECUTABLE_NAME = "cursor-agent"

#: The names Cursor's installer symlinks, in preference order, for the case where
#: the developer already has their own install.
PATH_NAMES: tuple[str, ...] = ("cursor-agent", "agent")

#: A version to fall back on when cursor.com cannot be reached to resolve one.
#: Pinned rather than guessed: this exact build was verified against this code.
PINNED_VERSION = "2026.08.11-e8db854"

VERSION_PATTERN = re.compile(r"\d{4}\.\d{2}\.\d{2}-[0-9a-f]{6,}")

#: Set to a version string to override resolution entirely (air-gapped CI).
ENV_VERSION = "RN_AGENT_CURSOR_VERSION"
#: Set to an absolute path to use a binary rn-agent did not install.
ENV_BINARY = "RN_AGENT_CURSOR_BIN"

MISSING_HINT = (
    f"Run `rn-agent login cursor` to install the Cursor CLI and sign in, "
    f"or set {ENV_BINARY} to the binary."
)

DOWNLOAD_TIMEOUT = 600.0

#: Browser consent can sit on Cursor's page while the developer finds an account.
LOGIN_TIMEOUT = 600.0

INSTALL_QUESTION = "The Cursor CLI is not installed yet (~75 MB). Download it now?"


def platform_slug() -> tuple[str, str]:
    """``(os, arch)`` in the vocabulary Cursor's CDN uses."""
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Darwin":
        target_os = "darwin"
    elif system == "Linux":
        target_os = "linux"
    else:
        raise RNAgentError(
            f"the Cursor CLI has no build for {system}",
            hint=(
                "Cursor ships macOS and Linux builds (Windows via WSL). Install it "
                f"yourself and point {ENV_BINARY} at the binary."
            ),
        )
    if machine in ("x86_64", "amd64"):
        arch = "x64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        raise RNAgentError(
            f"the Cursor CLI has no build for {machine}",
            hint=f"Install it yourself and point {ENV_BINARY} at the binary.",
        )
    return target_os, arch


@dataclass(slots=True)
class ManagedCursorCli:
    """Resolves - and if needed installs - the Cursor CLI, privately."""

    root: Path = field(default_factory=lambda: user_config_dir() / "tools" / EXECUTABLE_NAME)
    runner: CommandRunner | None = None
    downloader: FileTransport | None = None
    transport: JsonTransport | None = None
    logger: logging.Logger = field(default_factory=lambda: get_logger("tools"))

    # -- discovery ---------------------------------------------------------
    def own_binary(self, version: str | None = None) -> Path | None:
        """The newest binary rn-agent has installed, or a specific version."""
        if version:
            candidate = self.root / version / EXECUTABLE_NAME
            return candidate if candidate.is_file() else None
        for directory in self.installed_versions():
            candidate = self.root / directory / EXECUTABLE_NAME
            if candidate.is_file():
                return candidate
        return None

    def installed_versions(self) -> tuple[str, ...]:
        """Versions present under our directory, newest first."""
        if not self.root.is_dir():
            return ()
        names = [
            entry.name
            for entry in self.root.iterdir()
            if entry.is_dir() and not entry.name.startswith(".")
        ]
        return tuple(sorted(names, reverse=True))

    def system_binary(self) -> Path | None:
        """A Cursor CLI that is already on this machine, wherever it lives.

        Three places, in order: an explicit override, ``PATH``, and Cursor's own
        install location. The last one matters more than it looks - Cursor's
        installer puts the binary in ``~/.local/bin``, which is frequently *not*
        on ``PATH`` (its own docs end by telling you to add it). Checking there
        is the difference between reusing a 75 MB install and downloading a
        second copy of it.
        """
        override = os.environ.get(ENV_BINARY, "").strip()
        if override:
            path = Path(override).expanduser()
            if not path.is_file():
                raise RNAgentError(
                    f"{ENV_BINARY} points at {path}, which is not a file",
                    hint="Unset it to let rn-agent manage the Cursor CLI itself.",
                )
            return path
        shell_runner = self.runner or CommandRunner(cwd=Path.cwd())
        for name in PATH_NAMES:
            found = shell_runner.which(name)
            if found:
                return Path(found)
        return _vendor_install()

    def locate(self) -> Path | None:
        """Whatever is usable right now, without installing anything."""
        return self.system_binary() or self.own_binary()

    # -- installation ------------------------------------------------------
    def resolve_version(self) -> str:
        """The version Cursor's installer currently ships.

        Parsed from the installer rather than guessed: it is the vendor's own
        pointer at "current". If it cannot be read, the pinned version is used
        and said so, because refusing to work offline would be worse.
        """
        override = os.environ.get(ENV_VERSION, "").strip()
        if override:
            return override
        transport = self.transport or default_transport()
        try:
            response = transport.request("GET", INSTALLER_URL, headers={}, timeout=30.0)
        except TransportError as exc:
            self.logger.debug("could not read the Cursor installer: %s", exc)
            return PINNED_VERSION
        match = VERSION_PATTERN.search(response.text or "")
        if match is None:
            self.logger.debug("no version found in the Cursor installer; using the pinned one")
            return PINNED_VERSION
        return match.group(0)

    def artefact_url(self, version: str) -> str:
        target_os, arch = platform_slug()
        return ARTEFACT_URL.format(version=version, os=target_os, arch=arch)

    def install(
        self,
        version: str | None = None,
        *,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> Path:
        """Download and unpack one version. Returns the executable's path."""
        resolved = version or self.resolve_version()
        existing = self.own_binary(resolved)
        if existing is not None:
            return existing

        url = self.artefact_url(resolved)
        downloader = self.downloader or default_downloader()
        self.root.mkdir(parents=True, exist_ok=True)
        # Unpack beside the target and rename, so an interrupted install cannot
        # leave a half-extracted directory that looks complete.
        staging = Path(tempfile.mkdtemp(prefix=f".{resolved}-", dir=self.root))
        archive = staging / "package.tar.gz"
        try:
            self.logger.info("downloading the Cursor CLI %s", resolved)
            downloader.download(url, archive, timeout=DOWNLOAD_TIMEOUT, on_progress=on_progress)
            _extract(archive, staging)
            archive.unlink(missing_ok=True)
            binary = staging / EXECUTABLE_NAME
            if not binary.is_file():
                raise RNAgentError(
                    f"the Cursor CLI package for {resolved} contained no {EXECUTABLE_NAME}",
                    hint="Cursor may have changed the package layout; report this.",
                )
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            final = self.root / resolved
            if final.exists():
                shutil.rmtree(final, ignore_errors=True)
            staging.rename(final)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        self.logger.info("installed the Cursor CLI %s", resolved)
        return final / EXECUTABLE_NAME

    def require(
        self,
        *,
        install: bool = True,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> Path:
        """The binary to run, installing it on first use.

        ``install=False`` is for the paths that must not spend 75 MB of someone's
        bandwidth without being asked - a status render, a dry run.
        """
        found = self.locate()
        if found is not None:
            return found
        if not install:
            raise RNAgentError(
                "the Cursor CLI is not installed yet",
                hint=MISSING_HINT,
            )
        return self.install(on_progress=on_progress)

    # -- reporting ---------------------------------------------------------
    def describe(self) -> dict[str, object]:
        """What `whoami` shows: which binary, and who installed it."""
        override = os.environ.get(ENV_BINARY, "").strip()
        system = self.system_binary()
        own = self.own_binary()
        if override:
            source = "RN_AGENT_CURSOR_BIN"
        elif system is not None:
            source = "your PATH"
        elif own is not None:
            source = "managed by rn-agent"
        else:
            source = "not installed"
        binary = system or own
        return {
            "binary": str(binary) if binary else None,
            "source": source,
            "managed_versions": list(self.installed_versions()),
            "managed_root": str(self.root),
        }


def _extract(archive: Path, destination: Path) -> None:
    """Unpack the tarball, stripping its single top-level directory.

    ``filter="data"`` is what refuses absolute paths, ``..`` traversal, symlinks
    out of the tree and device files - the archive comes off the network, so it
    is treated as untrusted input even though the host is Cursor's own.
    """
    unpacked = destination / ".unpacked"
    unpacked.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:gz") as handle:
            handle.extractall(unpacked, filter="data")
    except tarfile.TarError as exc:
        raise RNAgentError(
            f"the Cursor CLI package could not be unpacked: {exc}",
            hint="Delete the partial download and try again.",
        ) from exc
    entries = [entry for entry in unpacked.iterdir() if not entry.name.startswith(".")]
    # The package has one top-level directory; Cursor's own installer strips it.
    roots = [entry for entry in entries if entry.is_dir()]
    source = roots[0] if len(roots) == 1 and len(entries) == 1 else unpacked
    for item in list(source.iterdir()):
        item.rename(destination / item.name)
    shutil.rmtree(unpacked, ignore_errors=True)


def cursor_cli(**kwargs: object) -> ManagedCursorCli:
    """The managed Cursor CLI, with rn-agent's default locations."""
    return ManagedCursorCli(**kwargs)  # type: ignore[arg-type]


def extra_bin_dirs() -> tuple[Path, ...]:
    """Directories Cursor's installer uses that are often missing from PATH."""
    home = Path.home()
    return (
        home / ".local" / "bin",
        home / "bin",
        home / ".cursor" / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
    )


def search_path() -> str:
    """``PATH`` plus Cursor's well-known bin dirs, so lookup matches OMP."""
    extras = [str(path) for path in extra_bin_dirs() if path.is_dir()]
    current = os.environ.get("PATH", "")
    return os.pathsep.join([*extras, current]) if extras else current


def resolve_binary(*, runner: CommandRunner | None = None) -> Path | None:
    """PATH, Cursor's own install, or the copy rn-agent manages. Never downloads."""
    _ = runner
    return cursor_cli().locate()


def _vendor_install() -> Path | None:
    """Cursor's own installer symlink, even when ``~/.local/bin`` is not on PATH."""
    for directory in extra_bin_dirs():
        for name in PATH_NAMES:
            candidate = directory / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate.resolve()
    return None


def offer_install(*, assume_yes: bool, confirm: Callable[[str], bool]) -> bool:
    """Whether ``run_sign_in`` should download the CLI.

    ``False`` means one is already on the machine. ``True`` means the developer
    agreed to a ~75 MB fetch. Declining is an error, not a silent skip: login
    cannot finish without the binary.
    """
    if cursor_cli().locate() is not None:
        return False
    if assume_yes or confirm(INSTALL_QUESTION):
        return True
    raise RNAgentError(
        "the Cursor CLI is not installed",
        hint=MISSING_HINT,
    )


def run_sign_in(
    *,
    install: bool = False,
    timeout: float = LOGIN_TIMEOUT,
    cli: ManagedCursorCli | None = None,
) -> Path:
    """Run ``cursor-agent login`` with this terminal attached.

    Cursor's CLI opens *its* sign-in page in the browser and stores the session
    in its own config. rn-agent never hosts that page and never copies the
    resulting credential.
    """
    manager = cli or cursor_cli()
    binary = manager.require(install=install)
    try:
        completed = subprocess.run(
            [str(binary), "login"],
            timeout=timeout,
            env={**os.environ, "PATH": search_path()},
        )
    except FileNotFoundError as exc:
        raise RNAgentError(
            "the Cursor CLI is not installed",
            hint=MISSING_HINT,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RNAgentError(
            "Cursor CLI login timed out waiting for the browser",
            hint="Finish sign-in on the page Cursor opened, or run `cursor-agent login` yourself.",
        ) from exc
    if completed.returncode != 0:
        raise RNAgentError(
            "Cursor CLI login did not finish",
            hint="Retry `rn-agent login cursor`, or run `cursor-agent login` yourself.",
        )
    return binary
