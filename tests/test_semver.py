"""npm range evaluation - the foundation of every compatibility check."""

from __future__ import annotations

import pytest

from rn_agent.utils.semver import (
    Version,
    coerce,
    compare,
    highest,
    is_undecidable_range,
    parse,
    range_floor,
    satisfies,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.2.3", Version(1, 2, 3)),
        ("v0.81.0", Version(0, 81, 0)),
        ("=2.0.0", Version(2, 0, 0)),
        ("19.1", Version(19, 1, 0)),
        ("7", Version(7, 0, 0)),
        ("1.2.3-beta.2", Version(1, 2, 3, ("beta", 2))),
        ("1.2.3+build7", Version(1, 2, 3)),
        ("not-a-version", None),
        (None, None),
    ],
)
def test_parse(text, expected):
    assert parse(text) == expected


def test_coerce_extracts_from_noise():
    assert coerce("gradle-8.10.2-all.zip") == Version(8, 10, 2)
    assert coerce("Gradle 7.6") == Version(7, 6, 0)
    assert coerce('openjdk version "17.0.9" 2023-10-17') == Version(17, 0, 9)
    assert coerce("Xcode 16.1\nBuild version 16B40") == Version(16, 1, 0)


def test_coerce_does_not_invent_a_prerelease():
    """`gradle-7.6-all.zip` must be 7.6.0, not 7.6.0-all.zip."""
    parsed = coerce("https://services.gradle.org/distributions/gradle-7.6-all.zip")
    assert parsed == Version(7, 6, 0)
    assert parsed is not None and not parsed.is_prerelease


def test_ordering_and_prerelease_rules():
    assert Version(1, 0, 0) < Version(1, 0, 1)
    assert Version(1, 2, 0) > Version(1, 1, 9)
    # a pre-release is lower than its release
    assert Version(1, 0, 0, ("rc", 1)) < Version(1, 0, 0)
    assert Version(1, 0, 0, ("alpha",)) < Version(1, 0, 0, ("beta",))
    assert Version(1, 0, 0, ("alpha", 1)) < Version(1, 0, 0, ("alpha", 2))
    assert compare("0.81.0", "0.82.1") == -1
    assert compare("0.82.1", "0.82.1") == 0
    assert compare("bad", "0.1.0") is None


def test_series_helper():
    assert Version(0, 82, 1).series == "0.82"


@pytest.mark.parametrize(
    ("version", "spec", "expected"),
    [
        # caret
        ("19.1.1", "^19.1.1", True),
        ("19.9.9", "^19.1.1", True),
        ("20.0.0", "^19.1.1", False),
        ("0.81.5", "^0.81.0", True),
        ("0.82.0", "^0.81.0", False),
        ("0.0.4", "^0.0.3", False),
        # tilde
        ("2.0.9", "~2.0.5", True),
        ("2.1.0", "~2.0.5", False),
        ("1.5.0", "~1", True),
        # comparators, with and without spaces
        ("22.22.0", ">=20.19.4", True),
        ("22.22.0", ">= 20.19.4", True),
        ("20.19.3", ">= 20.19.4", False),
        ("16.0.0", ">= 18", False),
        ("1.0.0", "<2.0.0", True),
        ("2.0.0", "<=2.0.0", True),
        # AND / OR / hyphen / wildcard
        ("1.5.0", ">=1.0.0 <2.0.0", True),
        ("2.0.0", ">=1.0.0 <2.0.0", False),
        ("18.2.0", "^16.8.0 || ^17.0.0 || ^18.0.0", True),
        ("19.0.0", "^16.8.0 || ^17.0.0 || ^18.0.0", False),
        ("1.5.0", "1.2.3 - 2.0.0", True),
        ("2.1.0", "1.2.3 - 2.0.0", False),
        ("0.82.1", "*", True),
        ("0.82.1", "0.82.x", True),
        ("0.83.0", "0.82.x", False),
        ("3.1.0", "3", True),
        # exact
        ("19.1.0", "19.1.0", True),
        ("19.1.1", "19.1.0", False),
    ],
)
def test_satisfies(version, spec, expected):
    assert satisfies(version, spec) is expected


def test_prerelease_only_matches_a_prerelease_range():
    # node-semver default: 2.0.0-rc.1 does not satisfy ^1.0.0 or >=1.0.0
    assert satisfies("2.0.0-rc.1", "^1.0.0") is False
    assert satisfies("1.5.0-beta.1", ">=1.0.0") is False
    assert satisfies("1.5.0-beta.1", ">=1.5.0-alpha.1") is True


@pytest.mark.parametrize(
    "spec",
    [
        "git+https://github.com/org/repo.git",
        "github:org/repo",
        "file:../local-package",
        "link:../sibling",
        "workspace:*",
        "npm:@scope/alias@^1.0.0",
        "latest",
        "",
        None,
    ],
)
def test_undecidable_specifiers(spec):
    assert is_undecidable_range(spec) is True
    assert satisfies("1.0.0", spec) is None


def test_wildcards_are_decidable():
    assert is_undecidable_range("*") is False
    assert satisfies("1.2.3", "*") is True


def test_unparsable_version_is_undecidable():
    assert satisfies("not-a-version", "^1.0.0") is None


def test_dangling_operator_does_not_become_an_exact_match():
    """Regression: `">= 20.19.4"` once collapsed into `== 20.19.4`."""
    assert satisfies("22.0.0", ">= 20.19.4") is True
    assert satisfies("22.0.0", ">=") is None


def test_highest_and_floor():
    assert highest(["1.2.3", "2.0.0", "0.9.9", None, "junk"]) == Version(2, 0, 0)
    assert highest([]) is None
    assert range_floor(">=20.19.4") == Version(20, 19, 4)
    assert range_floor("^19.1.1") == Version(19, 1, 1)
    assert range_floor("git+https://x") is None
