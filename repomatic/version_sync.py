# Copyright Kevin Deldycke <kevin@deldycke.com> and contributors.
#
# This program is Free Software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.

"""Self-hosted dependency-version updaters: the replacement for Renovate.

Backs the `sync-tool-versions`, `sync-action-pins`, and `sync-workflow-pins`
commands. Each discovers the latest eligible upstream version from a datasource
(GitHub releases, PyPI, or npm), gated by the shared `[tool.repomatic]
minimum-release-age` cooldown (the GitHub/PyPI/npm counterpart to uv's
`exclude-newer`, which guards `sync-uv-lock`), then rewrites the pinned version
in place.

The datasource adapters and version selection live here; the file I/O and
checksum recompute that the commands drive stay in {mod}`repomatic.cli`. The
string-level helpers (`set_tool_version`, `find_action_pins`,
`find_workflow_literals`, and the `apply_*` rewriters) are pure so they can be
unit-tested without network access.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import date, timedelta
from functools import cache
from typing import NamedTuple

from click_extra import parse_friendly_duration

from .github.gh import api_headers
from .github.releases import (
    GitHubReleasesUnavailable,
    extract_version,
    get_release_tags,
)
from .http import FetchError, get_text
from .humanize import SECONDS_PER_DAY
from .npm import get_release_dates as npm_release_dates
from .pypi import get_release_dates as pypi_release_dates
from .versions import is_newer, safe_version

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from packaging.version import Version

MINIMUM_RELEASE_AGE_URL = "https://repomatic.net/configuration#minimum-release-age"
"""Docs anchor for the `minimum-release-age` cooldown, linked from PR bodies."""

MIN_AGE_HELD_BACK_NOTE = (
    "Newer releases already published but withheld because they are still"
    f" inside the [`minimum-release-age`]({MINIMUM_RELEASE_AGE_URL}) cooldown"
    " window."
)
"""Intro paragraph for the version-sync held-back section.

The GitHub/PyPI/npm counterpart to
{data}`repomatic.dep_report.EXCLUDE_NEWER_HELD_BACK_NOTE`.
"""

ACTION_PIN_RE = re.compile(
    r"(?P<prefix>uses:\s*)"
    r"(?P<slug>[\w.-]+/[\w.-]+)"
    r"@(?P<sha>[0-9a-f]{40})"
    r"(?P<gap>\s*#\s*)"
    r"(?P<ref>v?\d[\w.-]*)"
)
"""Match a SHA-pinned GitHub Action `uses:` reference with its version comment.

The `slug/slug@<40-hex>` shape only matches `owner/repo` actions, so local
`./…` refs and reusable-workflow refs carrying a subpath
(`owner/repo/.github/workflows/x.yaml@…`) are skipped automatically.
"""

# npm install commands: `npm install awesome-lint@2.3.0`.
#
# `npx` counts too, and is the more common spelling in a workflow: it runs a tool
# once without installing it, which is exactly what CI wants. Left out, an
# `npx --yes html-validate@10.1.1` pin was the one version literal in a workflow
# that nothing walked forward. Its flags sit between the command and the package
# (`--yes`, `--quiet`), so they are skipped over rather than assumed absent, and
# the leading `-` in the flag pattern keeps it from swallowing the package name.
_NPM_LITERAL_RE = re.compile(
    r"(?:npm\s+(?:install|i|add)|npx)\s+(?:-{1,2}[a-z-]+\s+)*"
    r'"?(?P<package>@?[a-z0-9-]+(?:/[a-z0-9-]+)?)@(?P<version>[0-9][0-9.]*)"?'
)

# Python pins in uv/uvx commands: `uvx 'papaya-cli==11.2.8'`,
# `--with 'pkg[extra]==1.2.3'`.
_PYPI_LITERAL_RE = re.compile(
    r"(?:uvx?|--with)[^'\n]*"
    r"'(?P<package>[a-z][a-z0-9._-]*)(?:\[[^\]]+\])?==(?P<version>[0-9][0-9.]*)'"
)

SETUP_UV_PACKAGE = "uv"
"""PyPI project backing the `astral-sh/setup-uv` version pin."""

SETUP_UV_SLUG = "astral-sh/setup-uv"
"""Action slug provisioning uv, whose own pin decides what CI can verify."""

SETUP_UV_CHECKSUMS_PATH = "src/download/checksum/known-checksums.ts"
"""Path of the checksum table inside the {data}`SETUP_UV_SLUG` repository."""

GITHUB_API_CONTENTS_URL = (
    "https://api.github.com/repos/{slug}/contents/{path}?ref={ref}"
)
"""GitHub API URL for reading one file at one commit.

Requested with the `raw` media type, so the body arrives verbatim: the JSON
form base64-encodes it and caps out at 1 MB, and the checksum table is already
past half of that.
"""

_SETUP_UV_CHECKSUM_KEY_RE = re.compile(r'"[a-z0-9_-]+-(?P<version>\d+\.\d+\.\d+)"')
"""Match the uv version tail of a `KNOWN_CHECKSUMS` key.

Keys are ``{target-triple}-{version}`` (`aarch64-apple-darwin-0.12.4`). The
triple has no fixed segment count (`arm-unknown-linux-musleabihf` against
`x86_64-apple-darwin`), so the version anchors the match instead: the leading
character class holds no dot, which is what stops it eating into the version.
"""

# The uv build `astral-sh/setup-uv` installs, as a `version:` input on the step.
_SETUP_UV_VERSION_RE = re.compile(
    r"uses:[^\S\n]*astral-sh/setup-uv@[0-9a-f]{40}[^\n]*\n"
    # Intervening `with:` line, sibling inputs and their comments. Lazy, so the
    # match stops at this step's own `version:` rather than a later step's.
    r"(?:[^\S\n]+(?:with:|#[^\n]*|[a-z][a-z0-9-]*:[^\n]*)\n)*?"
    r"[^\S\n]+version:[^\S\n]*"
    r'"(?P<version>[0-9][0-9.]*)"'
)
"""Match the uv version pinned on a `setup-uv` step.

```{note}
Without this pin `setup-uv` installs the newest uv satisfying `required-version`,
which leaves the tool that enforces every other cooldown installed without one.
The two knobs are deliberately different: `required-version` stays a lower bound,
so contributors and downstream repos are never capped, while this pin fixes what
CI downloads and `sync-workflow-pins` walks it forward once a uv release clears
`minimum-release-age`. See `claude.md` § Pin uv with `required-version`.
```

Every `setup-uv` step must carry the input, which `tests/test_workflows.py`
enforces: the lazy middle section stops at the first `version:` it finds, so a
step missing one would otherwise borrow the next step's.
"""


class Candidate(NamedTuple):
    """A single release version offered by a datasource."""

    version: str
    """Comparable, display version (e.g. `1.7.12`)."""

    date: str
    """Publication date in `YYYY-MM-DD` format."""

    ref: str
    """Upstream reference to pin downstream.

    The raw git tag for GitHub releases (needed to resolve the commit SHA and
    write the pin comment); identical to `version` for PyPI and npm.
    """


class ActionPin(NamedTuple):
    """A SHA-pinned GitHub Action reference found in a workflow file."""

    slug: str
    """The `owner/repo` action slug."""

    sha: str
    """The currently pinned 40-character commit SHA."""

    ref: str
    """The version in the trailing `# vX.Y.Z` comment."""


class UpstreamRefPin(NamedTuple):
    """An upstream thin-caller `uses:` ref found in a workflow file.

    The counterpart of {class}`ActionPin` for the upstream repo's own reusable
    workflows and composite actions. Those refs carry a subpath
    (`owner/repo/.github/workflows/x.yaml@…`), which {data}`ACTION_PIN_RE`
    deliberately does not match, so they need their own parser.
    """

    version: str
    """The bare version in the trailing `# vX.Y.Z` comment, or in the tag ref."""

    sha: str | None
    """The pinned 40-character commit SHA, or `None` for a bare tag pin."""


class WorkflowLiteral(NamedTuple):
    """A version literal embedded in a workflow command."""

    ecosystem: str
    """Datasource: `npm` or `pypi`."""

    package: str
    """The package name."""

    version: str
    """The currently pinned version."""


def parse_min_age(value: str) -> timedelta:
    """Parse a `minimum-release-age` value into a {class}`~datetime.timedelta`.

    Accepts the friendly relative durations uv allows for `exclude-newer`
    (`8 days`, `2 weeks`, `36 hours`). An unrecognized value logs a warning and
    yields no cooldown.

    :param value: The configured `minimum-release-age` string.
    :return: The cooldown duration, or `timedelta(0)` when *value* does not
        parse.
    """
    duration = parse_friendly_duration(value or "")
    if duration is None:
        logging.warning(
            f"Unrecognized minimum-release-age {value!r}; applying no cooldown."
        )
        return timedelta(0)
    return duration


def min_release_age_days(value: str) -> int:
    """Convert a `minimum-release-age` value to whole days for npm's cooldown.

    npm's [`min-release-age`](https://docs.npmjs.com/cli/v11/using-npm/config#min-release-age)
    resolver option (npm 11.10.0+) refuses any package version younger than the
    given number of *days*, across the whole resolved tree, transitive
    dependencies included. It is the runtime, transitive-tree counterpart to the
    pin cooldown {func}`parse_min_age` feeds `sync-workflow-pins`: the same
    `minimum-release-age` window, enforced by npm at install time.

    Sub-day remainders round up, so a cooldown always over-protects rather than
    collapsing to `0`, npm's "no cooldown" sentinel.

    :param value: The configured `minimum-release-age` string (e.g. `8 days`).
    :return: The cooldown as a whole number of days (`0` when disabled).
    """
    return math.ceil(parse_min_age(value).total_seconds() / SECONDS_PER_DAY)


def exclude_newer_cutoff(value: str, today: date) -> str | None:
    """uv `--exclude-newer` cutoff date for a `minimum-release-age` value.

    uv's cooldown knob is an absolute date, so the relative window is resolved
    live against *today*: packages uploaded on or after the returned date drop
    out of resolution. This gates ad-hoc `uvx` tool installs (via
    {func}`repomatic.tool_runner.run_tool`) by the same window
    `sync-workflow-pins` applies to pins. The uv counterpart to
    {func}`min_release_age_days` (npm).

    :param value: The configured `minimum-release-age` string (e.g. `8 days`).
    :param today: Reference date, resolved once per run.
    :return: The cutoff as `YYYY-MM-DD`, or `None` when the cooldown is disabled
        (`0 days` or an unrecognized value), so callers omit the flag.
    """
    min_age = parse_min_age(value)
    if not min_age:
        return None
    return (today - min_age).isoformat()


def format_cooldown_note(age_label: str, cutoff: date) -> str:
    """Render the `minimum-release-age` cutoff sentence for a diff table.

    The version-sync counterpart to
    {func}`repomatic.dep_report.format_exclude_newer_note`. uv records an absolute
    `exclude-newer` timestamp; here the cooldown is a relative span, so the
    effective cutoff is `today - min_age`, recomputed each run rather than
    stored.

    :param age_label: The configured `minimum-release-age` value (e.g.
        `8 days`).
    :param cutoff: The effective cutoff date (`today - min_age`); releases
        published after it are held back.
    :return: A one-line markdown note for
        {func}`repomatic.dep_report.format_diff_table`.
    """
    return (
        f"Resolved with [`minimum-release-age`]({MINIMUM_RELEASE_AGE_URL})"
        f" cooldown `{age_label}`: releases after `{cutoff:%Y-%m-%d}` are"
        " held back."
    )


def _best_candidate(
    candidates: list[Candidate],
    *,
    allow_prerelease: bool,
    keep: Callable[[Candidate, date], bool],
) -> Candidate | None:
    """Return the highest-versioned candidate passing the *keep* predicate.

    Shared sweep for {func}`select_latest` and {func}`select_held_back`:
    candidates with an unparsable date or version are skipped, prereleases obey
    *allow_prerelease*, then *keep* (receiving the candidate and its parsed
    release date) decides eligibility and the highest PEP 440 version wins.
    """
    best: Candidate | None = None
    best_version: Version | None = None
    for candidate in candidates:
        try:
            released = date.fromisoformat(candidate.date)
        except ValueError:
            continue
        parsed = safe_version(candidate.version)
        if parsed is None:
            continue
        if parsed.is_prerelease and not allow_prerelease:
            continue
        if not keep(candidate, released):
            continue
        if best_version is None or parsed > best_version:
            best, best_version = candidate, parsed
    return best


def cleared_cooldown(released: date, cutoff: date) -> bool:
    """Whether a release dated *released* is safely older than *cutoff*.

    The comparison is **strict**, and that one character is load-bearing.
    Datasources report a release *date*, while the cooldown this gates is
    enforced downstream at *instant* granularity: uv's `--exclude-newer` cutoff
    is `now - min_age`, carrying the run's time of day. An inclusive `<=`
    therefore adopts a release published on the cutoff day but later in the day
    than the run's own clock, which uv then refuses to resolve, pinning a
    version that cannot be installed until it ages out.

    That is not hypothetical: a `sync-workflow-pins` run at `05:20` UTC adopted
    a release published at `17:07` on the cutoff date, and every binary build
    failed on `No solution found` until the window elapsed.

    Being strict costs up to 24 hours of extra window and guarantees
    correctness, since a release dated before the cutoff day is older than any
    instant on it. Same "over-protect rather than under-protect" convention as
    {func}`min_release_age_days`.

    :param released: Release date, as reported by the datasource.
    :param cutoff: The window boundary, `today - min_age`.
    :return: `True` when the release may be adopted.
    """
    return released < cutoff


def select_latest(
    candidates: list[Candidate],
    min_age: timedelta,
    today: date,
    *,
    allow_prerelease: bool = False,
) -> Candidate | None:
    """Return the highest version old enough to clear the cooldown.

    Candidates published more recently than *min_age* are held back, then the
    highest remaining PEP 440 version wins. Prereleases and versions that do
    not parse are skipped.

    :param candidates: Versions offered by a datasource.
    :param min_age: The stabilization window from `minimum-release-age`.
    :param today: Reference date for the cooldown computation.
    :param allow_prerelease: Keep prerelease versions when `True`.
    :return: The winning {class}`Candidate`, or `None` when none qualify.
    """
    cutoff = today - min_age
    return _best_candidate(
        candidates,
        allow_prerelease=allow_prerelease,
        keep=lambda _candidate, released: cleared_cooldown(released, cutoff),
    )


def select_held_back(
    candidates: list[Candidate],
    pinned: str,
    min_age: timedelta,
    today: date,
    *,
    allow_prerelease: bool = False,
) -> Candidate | None:
    """Return the highest release withheld from *pinned* only by the cooldown.

    The counterpart to {func}`select_latest`: among candidates strictly newer
    than *pinned*, keep those still inside the cooldown window (published more
    recently than *min_age*) and return the highest. These are the releases a
    later run adopts once they age out, surfaced in the `## ⏸️ Held back by
    cooldown` PR section. No extra network call is needed: the candidates are
    already in hand from the {func}`select_latest` sweep.

    :param candidates: Versions offered by a datasource.
    :param pinned: The version this run settled on; only strictly newer
        candidates can be held back.
    :param min_age: The stabilization window from `minimum-release-age`.
    :param today: Reference date for the cooldown computation.
    :param allow_prerelease: Keep prerelease versions when `True`.
    :return: The withheld {class}`Candidate`, or `None` when nothing newer is
        inside the cooldown.
    """
    cutoff = today - min_age
    return _best_candidate(
        candidates,
        allow_prerelease=allow_prerelease,
        # Negated against the same predicate select_latest adopts on, so the two
        # sets stay an exact partition: anything this keeps is a release
        # select_latest declined, never one it would already have taken.
        keep=lambda candidate, released: (
            not cleared_cooldown(released, cutoff)
            and is_newer(candidate.version, pinned)
        ),
    )


def pin_inside_cooldown(
    candidates: list[Candidate],
    pinned: str,
    min_age: timedelta,
    today: date,
) -> date | None:
    """The release date of *pinned* when it has not yet cleared the cooldown.

    Audits a pin already written to disk, where {func}`select_latest` only ever
    judges one it is about to write. The two ask the same question through
    {func}`cleared_cooldown`, so an audit can never disagree with the decision
    that produced the pin.

    Worth auditing separately because a pin can enter the tree without passing
    the selector at all: hand-edited, merged from a branch, restored from a
    revert, or written by an older release whose selector had a different
    boundary. Such a pin resolves through `uvx` in CI, where no per-package
    exemption is reachable, so it fails the whole job until it ages out.

    :param candidates: Versions offered by the datasource, as already fetched
        for the selection pass.
    :param pinned: The version currently written in the workflow.
    :param min_age: The stabilization window from `minimum-release-age`.
    :param today: Reference date for the cooldown computation.
    :return: The release date when *pinned* is still inside the window, or
        `None` when it has cleared, is not among *candidates*, or carries an
        unparsable date.
    """
    cutoff = today - min_age
    for candidate in candidates:
        if candidate.version != pinned:
            continue
        try:
            released = date.fromisoformat(candidate.date)
        except ValueError:
            return None
        return None if cleared_cooldown(released, cutoff) else released
    return None


def github_candidates(repo_url: str, tag_pattern: str | None = None) -> list[Candidate]:
    """Collect release candidates from a GitHub repository.

    :param repo_url: The repository URL.
    :param tag_pattern: Per-tool version-extraction regex (see
        {attr}`repomatic.tool_registry.ToolSpec.tag_pattern`).
    :return: One {class}`Candidate` per release whose tag yields a version.
        Empty when the API is unavailable (logged, never raised).
    """
    try:
        tags = get_release_tags(repo_url)
    except GitHubReleasesUnavailable as exc:
        logging.warning(f"Skipping {repo_url}: {exc}")
        return []
    candidates = []
    for tag, release in tags.items():
        version = extract_version(tag, tag_pattern)
        if version:
            candidates.append(Candidate(version=version, date=release.date, ref=tag))
    return candidates


def pypi_candidates(package: str) -> list[Candidate]:
    """Collect non-yanked release candidates from PyPI.

    :param package: The PyPI package name.
    :return: One {class}`Candidate` per non-yanked version.
    """
    return [
        Candidate(version=version, date=release.date, ref=version)
        for version, release in pypi_release_dates(package).items()
        if not release.yanked
    ]


def npm_candidates(package: str) -> list[Candidate]:
    """Collect release candidates from the npm registry.

    :param package: The npm package name.
    :return: One {class}`Candidate` per published version.
    """
    return [
        Candidate(version=version, date=published, ref=version)
        for version, published in npm_release_dates(package).items()
    ]


@cache
def _checksum_table(sha: str) -> frozenset[str] | None:
    """Read the uv versions listed in one `setup-uv` commit's checksum table.

    Cached per commit for the process: the content at a SHA is immutable, and
    both `sync-workflow-pins` and `lint-repo`'s coverage check read the same
    half-megabyte file.

    :param sha: The pinned {data}`SETUP_UV_SLUG` commit.
    :return: Every uv version the table names, or `None` when the file could
        not be read or yielded no key at all (a format change upstream), which
        callers treat as "unknown" rather than as "verifies nothing".
    """
    url = GITHUB_API_CONTENTS_URL.format(
        slug=SETUP_UV_SLUG, path=SETUP_UV_CHECKSUMS_PATH, ref=sha
    )
    try:
        table = get_text(
            url, headers={**api_headers(), "Accept": "application/vnd.github.raw"}
        )
    except FetchError as exc:
        logging.warning(f"Could not read the {SETUP_UV_SLUG} checksum table: {exc}")
        return None
    versions = frozenset(
        match.group("version") for match in _SETUP_UV_CHECKSUM_KEY_RE.finditer(table)
    )
    if not versions:
        logging.warning(
            f"No checksum key parsed from {SETUP_UV_CHECKSUMS_PATH} at {sha}:"
            " the upstream format changed, so the uv pin is no longer gated."
        )
        return None
    return versions


def setup_uv_verified_versions(shas: Iterable[str]) -> frozenset[str] | None:
    """uv releases every pinned `setup-uv` commit can checksum-verify.

    `setup-uv` verifies a download against a checksum table bundled into the
    action release. A version absent from that table is not refused: it is
    installed with no verification at all, on a `core.debug` line no CI log
    shows by default (`src/download/checksum/checksum.ts`). uv ships weekly and
    `setup-uv` roughly monthly, and `sync-action-pins` and `sync-workflow-pins`
    walk the two pins independently, so the uv pin drifts past the table on its
    own. Measured on 2026-08-20: `setup-uv` `v9.0.0` stopped at uv `0.11.30`
    while every workflow here pinned `0.12.3`, five releases later.

    Intersecting rather than picking one table keeps a repository mid-bump
    honest: while `sync-action-pins` has landed on some files and not others,
    the only uv a *whole* fleet can verify is one both tables carry.

    :param shas: Every distinct {data}`SETUP_UV_SLUG` commit pinned in the
        repository.
    :return: The uv versions verifiable by all of them, or `None` when no
        table could be read (no pin found, or every fetch failed), which leaves
        the caller ungated rather than blocked.
    """
    tables = [table for sha in sorted(set(shas)) if (table := _checksum_table(sha))]
    if not tables:
        return None
    return frozenset.intersection(*tables)


def set_tool_version(content: str, name: str, new_version: str) -> str:
    """Rewrite a tool's `version=` field in the `tool_registry.py` source.

    Targets the first `version="…"` inside the named `ToolSpec(` entry, stopping
    at the next entry so a later tool is never touched.

    :param content: The `tool_registry.py` source text.
    :param name: The `TOOL_REGISTRY` key (e.g. `"gitleaks"`).
    :param new_version: The version to write.
    :return: The updated source text.
    """
    pattern = re.compile(
        rf'("{re.escape(name)}":\s*ToolSpec\((?:(?!ToolSpec\().)*?\bversion=)'
        r'"[^"]*"',
        re.DOTALL,
    )
    return pattern.sub(rf'\g<1>"{new_version}"', content, count=1)


def set_with_package_version(content: str, package: str, new_version: str) -> str:
    """Rewrite a `with_packages` pin in the `tool_registry.py` source.

    Targets the `"{package}=={version}"` literal wherever it appears, unlike
    {func}`set_tool_version`, which is scoped to one `ToolSpec(` entry. Two tools
    pinning the same package therefore converge on one version rather than
    drifting apart, matching how {func}`~repomatic.sync_ops._widest_changes`
    collapses a name pinned at several versions elsewhere.

    :param content: The `tool_registry.py` source text.
    :param package: The package name as spelled in the pin (`"mdformat-gfm"`).
    :param new_version: The version to write.
    :return: The updated source text.
    """
    pattern = re.compile(rf'"{re.escape(package)}==[^"]*"')
    return pattern.sub(f'"{package}=={new_version}"', content)


def find_action_pins(content: str) -> list[ActionPin]:
    """Find every SHA-pinned GitHub Action reference in a workflow file."""
    return [
        ActionPin(slug=m.group("slug"), sha=m.group("sha"), ref=m.group("ref"))
        for m in ACTION_PIN_RE.finditer(content)
    ]


def apply_action_pins(
    content: str,
    resolved: dict[str, tuple[str, str]],
) -> tuple[str, list[tuple[str, str, str]]]:
    """Rewrite SHA-pinned actions to their resolved SHA and version comment.

    :param content: The workflow file text.
    :param resolved: Mapping of `owner/repo` slug to `(new_sha, new_ref)`.
    :return: The updated text and a list of `(slug, old_ref, new_ref)` changes
        actually applied (entries whose SHA already matched are skipped).
    """
    changes: list[tuple[str, str, str]] = []

    def replace(match: re.Match[str]) -> str:
        slug = match.group("slug")
        if slug not in resolved:
            return match.group(0)
        new_sha, new_ref = resolved[slug]
        if new_sha == match.group("sha"):
            return match.group(0)
        changes.append((slug, match.group("ref"), new_ref))
        return f"{match.group('prefix')}{slug}@{new_sha}{match.group('gap')}{new_ref}"

    return ACTION_PIN_RE.sub(replace, content), changes


def find_workflow_literals(content: str) -> list[WorkflowLiteral]:
    """Find npm and PyPI version literals embedded in a workflow file."""
    literals = [
        WorkflowLiteral("npm", m.group("package"), m.group("version"))
        for m in _NPM_LITERAL_RE.finditer(content)
    ]
    literals.extend(
        WorkflowLiteral("pypi", m.group("package"), m.group("version"))
        for m in _PYPI_LITERAL_RE.finditer(content)
    )
    literals.extend(
        WorkflowLiteral("pypi", SETUP_UV_PACKAGE, m.group("version"))
        for m in _SETUP_UV_VERSION_RE.finditer(content)
    )
    return literals


@cache
def self_pin_exemption_re(package: str) -> re.Pattern[str]:
    """Match a `uvx` command pinning *package*, capturing the flags before it.

    Memoized: the splice runs once per workflow file for the same package.
    """
    return re.compile(
        r"(?P<prefix>uvx\s+)(?P<flags>[^'\n]*)"
        rf"(?P<pin>'{re.escape(package)}(?:\[[^\]]+\])?==[0-9][0-9.]*')"
    )


def frozen_cli_invocation(package: str, version: str, exemption: str) -> str:
    """Render the frozen, cooldown-exempt `uvx` invocation of *package*.

    The one spelling both writers emit: the release freeze
    (`PrepareRelease.freeze_cli_version`) writes it wholesale, and
    {func}`apply_self_pin_exemption` converges an exemption-less command onto
    the same byte sequence, so the unfreeze pattern has exactly one shape to
    recognize.
    """
    return f"uvx --no-progress {exemption} '{package}=={version}'"


def apply_self_pin_exemption(content: str, package: str, exemption: str) -> str:
    """Splice a cooldown exemption into every `uvx` command pinning *package*.

    The upstream toolkit's inline pin moves in lockstep with the `uses:` refs,
    regardless of the cooldown, so the version it names can be minutes old.
    Every workflow exports a `UV_EXCLUDE_NEWER` covering all resolution, and
    `uvx` reads no per-package exemption from the environment or from
    `pyproject.toml`, so without the flag on the command line the freshly
    aligned pin fails to resolve until the window elapses.

    Idempotent: a command already carrying the exemption is left untouched.

    :param content: The workflow file text.
    :param package: The self-pinned distribution name.
    :param exemption: The flag to splice in, ahead of the quoted requirement.
    :return: The updated text.
    """

    def splice(match: re.Match[str]) -> str:
        if exemption in match.group("flags"):
            return match.group(0)
        # The exemption lands after the existing flags, right before the pin,
        # so the result is byte-identical to what the release freeze writes
        # (see {func}`frozen_cli_invocation`) and the unfreeze pattern only
        # ever meets one spelling.
        return (
            f"{match.group('prefix')}{match.group('flags')}"
            f"{exemption} {match.group('pin')}"
        )

    return self_pin_exemption_re(package).sub(splice, content)


def apply_workflow_literals(
    content: str,
    resolved: dict[tuple[str, str], str],
    self_pin: tuple[str, str] | None = None,
) -> tuple[str, list[tuple[str, str, str]]]:
    """Rewrite npm/PyPI version literals to their resolved version.

    :param content: The workflow file text.
    :param resolved: Mapping of `(ecosystem, package)` to the new version.
    :param self_pin: Optional `(package, exemption_flag)` for the upstream
        toolkit's own pin, whose rewrite bypasses the cooldown and therefore
        needs {func}`apply_self_pin_exemption` on the resulting command.
    :return: The updated text and a list of `(package, old_version,
        new_version)` changes actually applied.

    ```{important}
    The returned list covers version moves only. A *self_pin* splice edits the
    text while reporting nothing, because it names a package rather than moving
    a version, so a caller deciding whether to write must compare the returned
    text against its input rather than test the list. Gating on the list
    silently discards the backfill, which is what stranded downstream repos
    already pinned at the newest release.
    ```
    """
    changes: list[tuple[str, str, str]] = []

    def replace(ecosystem: str, sep: str):
        def inner(match: re.Match[str]) -> str:
            package = match.group("package")
            new_version = resolved.get((ecosystem, package))
            old_version = match.group("version")
            if not new_version or new_version == old_version:
                return match.group(0)
            changes.append((package, old_version, new_version))
            return match.group(0).replace(f"{sep}{old_version}", f"{sep}{new_version}")

        return inner

    def replace_setup_uv(match: re.Match[str]) -> str:
        """Rewrite a `setup-uv` step's `version:` input.

        Separate from {func}`replace` because the package name is implied by the
        step rather than captured from the text, and the version is delimited by
        `version: "…"` instead of a `pkg==version` separator.
        """
        new_version = resolved.get(("pypi", SETUP_UV_PACKAGE))
        old_version = match.group("version")
        if not new_version or new_version == old_version:
            return match.group(0)
        changes.append((SETUP_UV_PACKAGE, old_version, new_version))
        return match.group(0).replace(f'"{old_version}"', f'"{new_version}"')

    content = _NPM_LITERAL_RE.sub(replace("npm", "@"), content)
    content = _PYPI_LITERAL_RE.sub(replace("pypi", "=="), content)
    content = _SETUP_UV_VERSION_RE.sub(replace_setup_uv, content)
    # After the pin itself moved, so the exemption lands on the new version and a
    # file the rewrite left alone still gets a missing flag backfilled.
    if self_pin:
        content = apply_self_pin_exemption(content, *self_pin)
    return content, changes


@cache
def _upstream_ref_re(upstream_repo: str) -> re.Pattern[str]:
    """Compile the upstream `uses:` ref pattern, once per repo slug.

    Memoized because {func}`find_upstream_ref_pins` runs once per workflow
    file, in loops that always name the same upstream repository.
    """
    return re.compile(
        rf"{re.escape(upstream_repo)}/\.github/(?:workflows|actions)/[^@\s]+"
        r"@(?:(?P<sha>[0-9a-f]+)\s+#\s+)?v(?P<version>[0-9]+(?:\.[0-9]+)*)"
    )


def find_upstream_ref_pins(content: str, upstream_repo: str) -> list[UpstreamRefPin]:
    """Extract the `uses:` refs of the upstream repo's workflows, with their SHAs.

    Matches reusable-workflow and composite-action refs of *upstream_repo*,
    both SHA-pinned with a trailing version comment
    (``owner/repo/.github/workflows/lint.yaml@abc123 # v1.2.3``) and directly
    tag-pinned (``...@v1.2.3``, which yields a `None` SHA).

    Shared by `lint-repo`'s inline-pin lockstep check, `sync-workflow-pins`'
    upstream-pin alignment and `init`'s pin floor
    ({func}`~repomatic.init_project._highest_upstream_pin`), so all three read
    the refs the same way.
    """
    pattern = _upstream_ref_re(upstream_repo)
    return [UpstreamRefPin(m["version"], m["sha"]) for m in pattern.finditer(content)]


def find_upstream_ref_versions(content: str, upstream_repo: str) -> set[str]:
    """Extract the bare `uses:` ref versions of the upstream repo's workflows.

    The version-only view of {func}`find_upstream_ref_pins`.
    """
    return {pin.version for pin in find_upstream_ref_pins(content, upstream_repo)}
