"""External tools rn-agent installs and manages for you.

The rule for anything in here: a developer should never have to install, place on
``PATH`` or update a binary that this agent needs. The npm wrapper already builds
its own private Python runtime rather than asking for one; a managed tool is the
same idea for a vendor CLI.

What a managed tool never does:

* write to ``~/.local/bin``, ``/usr/local/bin`` or any directory on the
  developer's ``PATH``;
* edit ``~/.zshrc``, ``~/.bashrc`` or any shell profile;
* pipe a vendor's install script into a shell.

It downloads a pinned, versioned artefact over HTTPS into rn-agent's own
directory and runs it from there. A tool the developer already installed
themselves is preferred over downloading a second copy.
"""

from __future__ import annotations

from .cursor import ManagedCursorCli, cursor_cli

__all__ = ["ManagedCursorCli", "cursor_cli"]
