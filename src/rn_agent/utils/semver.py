"""npm-flavoured semantic versioning.

React Native projects express every constraint in node-semver syntax
(``^19.1.1``, ``~0.82.0``, ``>=20.19.4``, ``1.2 - 2.3``, ``18 || 19``), so the
agent needs a real range evaluator to answer questions such as "does the
installed React satisfy react-native's peer dependency?" without shelling out
to Node.

Supported: exact, ``=``, ``v`` prefix, ``^``, ``~``, ``>``/``>=``/``<``/``<=``,
``x``/``X``/``*`` wildcards, partial versions, hyphen ranges, whitespace AND,
``||`` OR. Pre-releases follow node-semver's default rule: they only satisfy a
comparator set that itself mentions a pre-release at the same version tuple.

Non-registry specifiers (``git+``, ``file:``, ``workspace:``, ``npm:`` aliases,
``latest``) are reported as *undecidable* rather than guessed at.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import total_ordering

_VERSION_RE = re.compile(
    r"""^\s*[v=]*\s*
    (?P<major>\d+)
    (?:\.(?P<minor>\d+))?
    (?:\.(?P<patch>\d+))?
    (?:-(?P<prerelease>[0-9A-Za-z\-.]+))?
    (?:\+(?P<build>[0-9A-Za-z\-.]+))?
    \s*$""",
    re.VERBOSE,
)

_LOOSE_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?(?:-([0-9A-Za-z\-.]+))?")

_COMPARATOR_RE = re.compile(r"^(>=|<=|>|<|=|\^|~)?\s*(.+)$")
# node-semver allows whitespace between an operator and its version
# (`">= 20.19.4"`, which React Native itself uses in engines.node). Glue them
# back together before tokenising, without touching hyphen ranges (` - `).
_OP_SPACE_RE = re.compile(r"(>=|<=|>|<|=|\^|~)\s+(?=[\dvxX*])")

_UNDECIDABLE_PREFIXES = (
    "git+",
    "git:",
    "github:",
    "file:",
    "link:",
    "workspace:",
    "portal:",
    "npm:",
    "http://",
    "https://",
    "patch:",
)
# dist-tags carry no version information; `*`/`x` are real ranges meaning
# "anything", so they stay decidable and simply match every version.
_UNDECIDABLE_EXACT = ("latest", "next", "canary", "beta", "")

_WILDCARDS = {"x", "X", "*"}


@total_ordering
@dataclass(frozen=True, slots=True)
class Version:
    """A parsed semantic version. ``build`` is ignored for ordering."""

    major: int
    minor: int = 0
    patch: int = 0
    prerelease: tuple[str | int, ...] = field(default=())
    build: str | None = None

    def __str__(self) -> str:
        text = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            text += "-" + ".".join(str(part) for part in self.prerelease)
        return text

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    @property
    def series(self) -> str:
        """``0.82.1`` -> ``0.82`` - the way the RN community names releases."""
        return f"{self.major}.{self.minor}"

    def _key(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() == other._key() and self.prerelease == other.prerelease

    def __lt__(self, other: Version) -> bool:
        if not isinstance(other, Version):  # pragma: no cover - guarded by typing
            return NotImplemented
        if self._key() != other._key():
            return self._key() < other._key()
        return _compare_prerelease(self.prerelease, other.prerelease) < 0

    def __hash__(self) -> int:
        return hash((self._key(), self.prerelease))

    def bump_major(self) -> Version:
        return Version(self.major + 1, 0, 0)

    def bump_minor(self) -> Version:
        return Version(self.major, self.minor + 1, 0)

    def bump_patch(self) -> Version:
        return Version(self.major, self.minor, self.patch + 1)


def _split_prerelease(raw: str | None) -> tuple[str | int, ...]:
    if not raw:
        return ()
    parts: list[str | int] = []
    for chunk in raw.split("."):
        parts.append(int(chunk) if chunk.isdigit() else chunk)
    return tuple(parts)


def _compare_prerelease(left: tuple[str | int, ...], right: tuple[str | int, ...]) -> int:
    """node-semver rule: no pre-release outranks any pre-release."""
    if not left and not right:
        return 0
    if not left:
        return 1
    if not right:
        return -1
    for a, b in zip(left, right, strict=False):
        if a == b:
            continue
        a_num, b_num = isinstance(a, int), isinstance(b, int)
        if a_num and b_num:
            return -1 if a < b else 1  # type: ignore[operator]
        if a_num != b_num:
            return -1 if a_num else 1
        return -1 if str(a) < str(b) else 1
    if len(left) == len(right):
        return 0
    return -1 if len(left) < len(right) else 1


def parse(text: str | None) -> Version | None:
    """Strict-ish parse of a single version. Returns ``None`` when unusable."""
    if not text:
        return None
    match = _VERSION_RE.match(str(text))
    if match is None:
        return None
    return Version(
        major=int(match.group("major")),
        minor=int(match.group("minor") or 0),
        patch=int(match.group("patch") or 0),
        prerelease=_split_prerelease(match.group("prerelease")),
        build=match.group("build"),
    )


def coerce(text: str | None) -> Version | None:
    """Pull the first ``x.y[.z]`` out of noisy text (gradle URLs, tool output).

    A loose match never keeps a "pre-release": in
    ``gradle-7.6-all.zip`` the trailing ``-all.zip`` is packaging noise, not a
    semver tag, so the result is ``7.6.0``. Genuine pre-releases arrive through
    the strict :func:`parse` path above.
    """
    if not text:
        return None
    exact = parse(text)
    if exact is not None:
        return exact
    match = _LOOSE_RE.search(str(text))
    if match is None:
        return None
    return Version(
        major=int(match.group(1)),
        minor=int(match.group(2)),
        patch=int(match.group(3) or 0),
    )


def compare(left: str | Version | None, right: str | Version | None) -> int | None:
    """-1 / 0 / 1, or ``None`` when either side cannot be parsed."""
    a = left if isinstance(left, Version) else coerce(left)
    b = right if isinstance(right, Version) else coerce(right)
    if a is None or b is None:
        return None
    if a == b:
        return 0
    return -1 if a < b else 1


def is_undecidable_range(spec: str | None) -> bool:
    """True for specifiers that carry no comparable version information."""
    if spec is None:
        return True
    text = spec.strip()
    if text.lower() in _UNDECIDABLE_EXACT:
        return True
    return text.startswith(_UNDECIDABLE_PREFIXES)


@dataclass(frozen=True, slots=True)
class _Comparator:
    op: str
    version: Version

    def allows(self, candidate: Version) -> bool:
        result = compare(candidate, self.version)
        if result is None:  # pragma: no cover - both sides already parsed
            return False
        if self.op == ">":
            return result > 0
        if self.op == ">=":
            return result >= 0
        if self.op == "<":
            return result < 0
        if self.op == "<=":
            return result <= 0
        return result == 0


def _partial(text: str) -> tuple[int | None, int | None, int | None, str | None]:
    """Split a possibly partial/wildcarded version into its pieces."""
    cleaned = text.strip().lstrip("v=").strip()
    prerelease: str | None = None
    if "+" in cleaned:
        cleaned = cleaned.split("+", 1)[0]
    if "-" in cleaned:
        cleaned, prerelease = cleaned.split("-", 1)
    pieces = cleaned.split(".") if cleaned else []
    numbers: list[int | None] = []
    for piece in pieces[:3]:
        if piece in _WILDCARDS or piece == "":
            numbers.append(None)
        elif piece.isdigit():
            numbers.append(int(piece))
        else:
            raise ValueError(f"unparsable version piece: {piece!r}")
    while len(numbers) < 3:
        numbers.append(None)
    return numbers[0], numbers[1], numbers[2], prerelease


def _expand_token(token: str) -> list[_Comparator]:
    """Turn one range token into concrete comparators."""
    text = token.strip()
    if not text or text in _WILDCARDS:
        return []

    match = _COMPARATOR_RE.match(text)
    if match is None:  # pragma: no cover - regex accepts anything non-empty
        raise ValueError(f"unparsable comparator: {token!r}")
    op, body = match.group(1) or "", match.group(2).strip()
    if body in _WILDCARDS:
        return []

    major, minor, patch, prerelease = _partial(body)
    if major is None:
        # A dangling operator ("<" with no version) must never silently widen
        # the set - that once turned ">= 20.19.4" into "== 20.19.4".
        if op:
            raise ValueError(f"comparator without a version: {token!r}")
        return []
    pre = _split_prerelease(prerelease)

    if op in {">", ">=", "<", "<="}:
        floor = Version(major, minor or 0, patch or 0, pre)
        return [_Comparator(op, floor)]

    if op == "^":
        low = Version(major, minor or 0, patch or 0, pre)
        if major > 0 or minor is None:
            high = Version(major + 1, 0, 0)
        elif minor > 0 or patch is None:
            high = Version(0, minor + 1, 0)
        else:
            high = Version(0, minor, patch + 1)
        return [_Comparator(">=", low), _Comparator("<", high)]

    if op == "~":
        low = Version(major, minor or 0, patch or 0, pre)
        high = Version(major + 1, 0, 0) if minor is None else Version(major, minor + 1, 0)
        return [_Comparator(">=", low), _Comparator("<", high)]

    # Bare or `=`: an exact version, or a partial range (1.2 / 1.2.x / 1).
    if minor is None:
        return [
            _Comparator(">=", Version(major, 0, 0)),
            _Comparator("<", Version(major + 1, 0, 0)),
        ]
    if patch is None:
        return [
            _Comparator(">=", Version(major, minor, 0)),
            _Comparator("<", Version(major, minor + 1, 0)),
        ]
    return [_Comparator("=", Version(major, minor, patch, pre))]


def _expand_set(part: str) -> list[_Comparator]:
    """Expand one AND-set, honouring hyphen ranges (``1.2.3 - 2.3.4``)."""
    tokens = _OP_SPACE_RE.sub(r"\1", part.strip()).split()
    if "-" in tokens:
        index = tokens.index("-")
        left, right = tokens[:index], tokens[index + 1 :]
        if left and right:
            low_major, low_minor, low_patch, low_pre = _partial(left[-1])
            comparators = [
                _Comparator(
                    ">=",
                    Version(
                        low_major or 0,
                        low_minor or 0,
                        low_patch or 0,
                        _split_prerelease(low_pre),
                    ),
                )
            ]
            hi_major, hi_minor, hi_patch, hi_pre = _partial(right[0])
            if hi_major is None:
                return comparators
            if hi_minor is None:
                comparators.append(_Comparator("<", Version(hi_major + 1, 0, 0)))
            elif hi_patch is None:
                comparators.append(_Comparator("<", Version(hi_major, hi_minor + 1, 0)))
            else:
                comparators.append(
                    _Comparator(
                        "<=",
                        Version(hi_major, hi_minor, hi_patch, _split_prerelease(hi_pre)),
                    )
                )
            leftovers = left[:-1] + right[1:]
            for token in leftovers:
                comparators.extend(_expand_token(token))
            return comparators
    expanded: list[_Comparator] = []
    for token in tokens:
        expanded.extend(_expand_token(token))
    return expanded


def satisfies(version: str | Version | None, spec: str | None) -> bool | None:
    """Does ``version`` satisfy the npm range ``spec``?

    ``None`` means "cannot be decided" - an unparsable version or a
    non-registry specifier. Callers must treat that as *unknown*, never as a
    failure, so the agent never invents a problem it cannot prove.
    """
    candidate = version if isinstance(version, Version) else parse(version) or coerce(version)
    if candidate is None:
        return None
    if spec is None:
        return None
    text = spec.strip()
    if is_undecidable_range(text):
        return None

    for part in text.split("||"):
        try:
            comparators = _expand_set(part.strip())
        except ValueError:
            return None
        if not comparators:
            return True
        if all(comparator.allows(candidate) for comparator in comparators):
            if not candidate.is_prerelease:
                return True
            # A pre-release only matches when the set mentions the same tuple.
            tuples = {
                (c.version.major, c.version.minor, c.version.patch)
                for c in comparators
                if c.version.is_prerelease
            }
            if (candidate.major, candidate.minor, candidate.patch) in tuples:
                return True
    return False


def highest(versions: Iterable[str | Version | None]) -> Version | None:
    """The greatest parsable version in ``versions``."""
    parsed = [v if isinstance(v, Version) else coerce(v) for v in versions]
    usable = [v for v in parsed if v is not None]
    return max(usable) if usable else None


def range_floor(spec: str | None) -> Version | None:
    """The lowest version a range admits - handy for "minimum required" text."""
    if spec is None or is_undecidable_range(spec):
        return None
    floors: list[Version] = []
    for part in spec.split("||"):
        try:
            comparators = _expand_set(part.strip())
        except ValueError:
            return None
        lows = [c.version for c in comparators if c.op in {">=", "=", ">"}]
        if lows:
            floors.append(min(lows))
    return min(floors) if floors else None
