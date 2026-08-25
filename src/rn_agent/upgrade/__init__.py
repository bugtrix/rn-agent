"""Dependency upgrades: what is available, and what it would cost.

The registry client is here rather than inside the command because ``migrate``
needs it too: the honest source for "what does react-native 0.82 require" is
react-native 0.82's own ``peerDependencies``, and that lives in the registry.
"""

from __future__ import annotations

from .registry import DEFAULT_REGISTRY, NpmRegistry, PackageVersion, Packument
from .versions import RnTarget, UpgradeRequest, classify_upgrade, list_rn_targets

__all__ = [
    "DEFAULT_REGISTRY",
    "NpmRegistry",
    "PackageVersion",
    "Packument",
    "RnTarget",
    "UpgradeRequest",
    "classify_upgrade",
    "list_rn_targets",
]
