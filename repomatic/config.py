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

"""Configuration schema and loading for `[tool.repomatic]` in `pyproject.toml`.

Defines the `Config` dataclass, its TOML serialization helpers, and the
`load_repomatic_config` function that reads, validates, and returns a typed
`Config` instance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from textwrap import dedent

from click_extra import (
    CONFIG_PATH_METADATA_KEY,
    NORMALIZE_KEYS_METADATA_KEY,
    ColumnSpec,
    make_schema_callable,
    schema_field_infos,
)
from extra_platforms import ALL_AGENTS, ALL_CI

from .pyproject import read_pyproject_toml

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import Any, Final


@dataclass
class CacheConfig:
    """Nested schema for `[tool.repomatic.cache]`."""

    dir: str = ""
    """Override the binary cache directory path.

    When empty (the default), the cache uses the platform convention:
    `~/Library/Caches/repomatic` on macOS, `$XDG_CACHE_HOME/repomatic`
    or `~/.cache/repomatic` on Linux, `%LOCALAPPDATA%\\repomatic\\Cache`
    on Windows. The `REPOMATIC_CACHE_DIR` environment variable takes
    precedence over this setting.
    """

    github_release_ttl: int = 604800
    """Freshness TTL for cached single-release bodies (seconds).

    GitHub release bodies are immutable once published, so a long TTL (7 days)
    is safe. Set to `0` to disable caching for single-release lookups.
    """

    github_releases_ttl: int = 86400
    """Freshness TTL for cached all-releases responses (seconds).

    New releases can appear at any time, so a shorter TTL (24 hours) balances
    freshness with API savings.
    """

    max_age: int = 30
    """Auto-purge cached entries older than this many days.

    Set to `0` to disable auto-purge. The `REPOMATIC_CACHE_MAX_AGE`
    environment variable takes precedence over this setting.
    """

    npm_ttl: int = 86400
    """Freshness TTL for cached npm registry metadata (seconds).

    New npm versions can appear at any time, so a 24-hour TTL balances
    freshness with request savings. Set to `0` to disable caching for npm
    lookups.
    """

    pypi_ttl: int = 86400
    """Freshness TTL for cached PyPI metadata (seconds).

    PyPI metadata changes when new versions are published. A 24-hour TTL
    avoids redundant API calls while keeping data reasonably current.
    """


@dataclass
class DependencyGraphConfig:
    """Nested schema for `[tool.repomatic.dependency-graph]`."""

    all_extras: bool = True
    """Whether to include all optional extras in the graph.

    When `True`, the `update-dep-graph` command behaves as if
    `--all-extras` was passed.
    """

    all_groups: bool = True
    """Whether to include all dependency groups in the graph.

    When `True`, the `update-dep-graph` command behaves as if
    `--all-groups` was passed. Projects that want to exclude development
    dependency groups (docs, test, typing) from their published graph can
    set this to `false`.
    """

    level: int | None = None
    """Maximum depth of the dependency graph.

    `None` means unlimited. `1` = directly-declared deps only, `2` = adds
    their deps, etc. Equivalent to `--level`.
    """

    no_extras: list[str] = field(default_factory=list)
    """Optional extras to exclude from the graph.

    Equivalent to passing `--no-extra` for each entry. Takes precedence
    over `dependency-graph.all-extras`.
    """

    no_groups: list[str] = field(default_factory=list)
    """Dependency groups to exclude from the graph.

    Equivalent to passing `--no-group` for each entry. Takes precedence
    over `dependency-graph.all-groups`.
    """

    output: str = "./docs/assets/dependencies.mmd"
    """Path where the dependency graph Mermaid diagram should be written.

    The dependency graph visualizes the project's dependency tree in Mermaid format.
    """


@dataclass
class DocsConfig:
    """Nested schema for `[tool.repomatic.docs]`."""

    apidoc_exclude: list[str] = field(default_factory=list)
    """Glob patterns for modules to exclude from `sphinx-apidoc`.

    Passed as positional exclude arguments after the source directory
    (e.g., `["setup.py", "tests"]`).
    """

    apidoc_extra_args: list[str] = field(default_factory=list)
    """Extra arguments appended to the `sphinx-apidoc` invocation.

    The base flags `--no-toc --module-first` are always applied.
    Use this for project-specific options (e.g., `["--implicit-namespaces"]`).
    """

    update_script: str = "./docs/docs_update.py"
    """Path to a Python script run after `sphinx-apidoc` to generate dynamic content.

    Resolved relative to the repository root. Must reside under the `docs/`
    directory for security. Set to an empty string to disable.
    """


@dataclass
class AgentLayout:
    """Where one AI coding agent expects its assets to live."""

    skills: str
    """Directory holding one folder per skill."""

    subagents: str
    """Directory holding subagent definitions.

    Named for what it holds, not for the agent reading it.
    """

    settings: str
    """File holding the agent's project-scoped settings.

    A file rather than a directory, unlike its siblings: it is the one asset
    repomatic merges into rather than writes whole, so the path has to name the
    document itself.
    """


AGENT_LAYOUTS: Final[dict[str, AgentLayout]] = {
    "claude_code": AgentLayout(
        skills="./.claude/skills/",
        subagents="./.claude/agents/",
        settings="./.claude/settings.json",
    ),
}
"""Asset layout per agent, keyed by `extra_platforms.ALL_AGENTS` trait ID.

Only agents repomatic can actually lay out appear here. `cline` and `cursor`
are valid trait IDs but have no Agent Skills layout to target, so selecting
one is rejected rather than silently producing a Claude Code tree.
"""

DEFAULT_AGENT: Final[str] = "claude_code"
"""Agent assumed when `[tool.repomatic.flavor] agent` is unset."""

DEFAULT_CI: Final[str] = "github_ci"
"""CI system assumed when `[tool.repomatic.flavor] ci` is unset."""

CLOUDFLARE_PLACEMENT_MODES: Final[frozenset[str]] = frozenset((
    "",
    "off",
    "smart",
))
"""Values `site.cloudflare-placement` accepts, empty meaning unmanaged.

The vocabulary of the Pages project's `placement.mode` field, which is what
`repomatic cloudflare-pages` writes the setting through. Anything else would
be PATCHed to the live project verbatim and rejected there, far from the
`pyproject.toml` line that caused it.
"""

SITE_DEPLOY_TARGETS: Final[frozenset[str]] = frozenset((
    "cloudflare-pages",
    "github-pages",
))
"""Hosts a repository's built site can be published to.

One deploy job per target, each with the permissions its own host needs, so a
value outside this set has no job at all behind it. `Config.__post_init__`
rejects one rather than letting the workflow run green and publish nothing.
"""


def deploys_to(site_deploy: str, target: str, *, is_sphinx: bool) -> bool:
    """Whether a repository declaring *site_deploy* publishes its site to *target*.

    The one routing rule behind both the `lint-repo` audit and the setup
    guide, which used to carry mirror copies kept in sync by prose. The
    GitHub Pages half stays gated on Sphinx, because the Docs workflow is the
    only publisher repomatic runs for that host and it only builds Sphinx
    trees. The Cloudflare half follows the declaration alone: a repository
    whose site is built by its own workflow still needs the project and the
    credentials the checks and the guide cover.
    """
    if site_deploy != target:
        return False
    if target == "github-pages":
        return is_sphinx
    return True


def location_path(location: str) -> str:
    """Normalize a `*.location` config value into a bare repo-relative path.

    The location defaults carry a `./` prefix (they read as paths in the
    reference table) and a directory location a trailing slash; neither
    belongs in a registry target or an `output_dir / path` join. One
    normalizer keeps every consumer spelling the same value the same way.

    :param location: A `Config` location field value, class default or
        resolved instance value alike.
    :return: The path with no `./` prefix and no trailing slash.
    """
    return location.removeprefix("./").rstrip("/")


def _resolve_flavor(
    value: str, group: Iterable[Any], supported: set[str], key: str
) -> str:
    """Normalize a flavor value, then check it twice.

    First against the full extra-platforms vocabulary, which catches a typo or
    an ecosystem nobody has heard of, then against the subset repomatic can
    actually target. Keeping the two apart means a real-but-unimplemented
    ecosystem reports as unsupported rather than as a spelling mistake.

    Trait IDs are underscore-separated (`claude_code`) while `[tool.repomatic]`
    keys are hyphenated, so both spellings are accepted.

    :param group: extra-platforms trait group defining the vocabulary.
    :param supported: Trait IDs repomatic implements.
    :param key: Dotted config path, used in the error messages.
    :raises ValueError: When *value* is unknown upstream or unsupported here.
    """
    normalized = value.strip().lower().replace("-", "_")
    known = {trait.id for trait in group}
    if normalized not in known:
        msg = (
            f"Unknown [tool.repomatic] {key} = {value!r}. Expected an "
            f"extra-platforms trait ID: {', '.join(sorted(known))}."
        )
        raise ValueError(msg)
    if normalized not in supported:
        msg = (
            f"Unsupported [tool.repomatic] {key} = {value!r}. repomatic "
            f"targets: {', '.join(sorted(supported))}."
        )
        raise ValueError(msg)
    return normalized


@dataclass
class FlavorConfig:
    """Nested schema for `[tool.repomatic.flavor]`.

    Declares which ecosystem repomatic is targeting, so a future decision has
    one place to branch on instead of a new flag per feature.

    ```{note}
    Values are trait IDs from
    [extra-platforms](https://github.com/kdeldycke/extra-platforms), which
    already models both AI agents and CI systems. Borrowing its vocabulary
    brings its detection helpers (`current_agent()`, `is_github_ci()`) and its
    naming along for free, instead of repomatic maintaining a parallel enum.
    ```

    ```{caution}
    Defaults are static, never detected. Deriving them from `current_agent()`
    would make a repository's effective configuration depend on which tool
    happened to invoke `repomatic` last, so `repomatic metadata` would stop
    being reproducible.
    ```
    """

    agent: str = DEFAULT_AGENT
    """AI coding agent whose asset layout the bundled skills and agents target.

    Accepts a `extra_platforms.ALL_AGENTS` trait ID present in
    {data}`~repomatic.config.AGENT_LAYOUTS`. Hyphens are normalized, so
    `claude-code` works too.
    """

    ci: str = DEFAULT_CI
    """CI system the bundled workflows target.

    Accepts a `extra_platforms.ALL_CI` trait ID. Only `github_ci` is
    implemented: every bundled workflow is a GitHub Actions workflow, so any
    other value is rejected rather than quietly emitting the wrong thing.
    """

    def __post_init__(self) -> None:
        """Normalize and validate both flavors against their vocabularies."""
        self.agent = _resolve_flavor(
            self.agent, ALL_AGENTS, set(AGENT_LAYOUTS), "flavor.agent"
        )
        self.ci = _resolve_flavor(self.ci, ALL_CI, {DEFAULT_CI}, "flavor.ci")

    @property
    def layout(self) -> AgentLayout:
        """Asset layout for the selected agent."""
        return AGENT_LAYOUTS[self.agent]


@dataclass
class GitignoreConfig:
    """Nested schema for `[tool.repomatic.gitignore]`."""

    extra_categories: list[str] = field(default_factory=list)
    """Additional gitignore template categories to fetch from gitignore.io.

    List of template names (e.g., `["Python", "Node", "Terraform"]`) to combine
    with the generated `.gitignore` content.
    """

    extra_content: str = field(
        default_factory=lambda: dedent(
            """
            # Claude Code local files.
            .claude/scheduled_tasks.lock
            .claude/settings.local.json
            **/.claude/.cc-writes/

            # Sphinx linkcheck output.
            docs/_linkcheck/
            """
        ).strip()
    )
    """Content appended at the end of the generated `.gitignore` file.

    "Appended" describes where the string lands, after the gitignore.io block,
    not how a downstream value combines with the default above: setting this key
    **replaces** that default wholesale, so the entries shown there are lost
    unless the override repeats them. {func}`repomatic.gitignore.orphaned_rules`
    catches that for any rule an earlier sync already wrote to disk, but not for
    one this repository never materialized, so copy the default and extend it
    rather than writing only the new lines. Reach for {attr}`extra_categories`
    instead when adding whole gitignore.io templates: that one is additive.

    The `.cc-writes` entry is the one carrying a `**/` prefix, because it is the
    one Claude Code does not place at the repository root: the directory is
    staged beside whichever working directory the session tracks, so a single
    `cd` into a subtree leaves one there instead. Anchoring it would miss every
    copy but the root's.
    """

    location: str = "./.gitignore"
    """File path of the `.gitignore` to update, relative to the root of the repository.
    """

    sync: bool = True
    """Whether `.gitignore` sync is enabled for this project.

    Projects that manage their own `.gitignore` and do not want the autofix job
    to overwrite it can set this to `false`.
    """


@dataclass
class LabelsConfig:
    """Nested schema for `[tool.repomatic.labels]`."""

    content_rules: dict[str, list[str]] = field(default_factory=dict)
    """Per-label patterns matched against an issue or pull request's text.

    The `[tool.repomatic.labels.content-rules]` table maps each label to the
    patterns that apply it, evaluated by `apply-labels` against the title and
    body. Any one pattern matching applies the label:

    ```toml
    [tool.repomatic.labels.content-rules]
    "🥭 mango" = ["mango", "papaya"]
    "🐛 bug" = []
    ```

    A bare pattern is a literal keyword, matched case-insensitively on word
    boundaries; the `/regex/flags` form passes a regex through instead, with
    `i`, `m` and `s` honored. An entry for a label the bundled defaults also
    carry replaces the default entry, and an empty list disables it (see
    {data}`repomatic.labels.DEFAULT_CONTENT_RULES`).
    """

    extra: list[dict[str, str | bool | list[str]]] = field(default_factory=list)
    """Inline label definitions applied at sync time under the `default` profile.

    Each entry is a mapping carrying `labelmaker`'s per-label specification:
    `name` (required), `color` (single color or multi-color list),
    `description`, `create`, `update`, `enforce-case`, `rename-from` and
    `on-rename-clash`. A `rename-from` list renames an existing label in
    place, preserving its issue and PR associations. Entries are serialized
    into a temporary TOML file as `[[profiles.default.labels]]` blocks and
    applied by `labelmaker apply`, so no `extra-labels/*.toml` file needs
    committing.

    For label sets that need multiple profiles, commit a hand-written file
    under `extra-labels/` or download one via `extra-files` instead.
    """

    extra_files: list[str] = field(default_factory=list)
    """URLs of additional label definition files (JSON, JSON5, TOML, or YAML).

    Each URL is downloaded into `extra-labels/` and applied separately by
    `labelmaker`. For inline definitions that need no external file, use
    `extra` instead.
    """

    file_rules: dict[str, list[str]] = field(default_factory=dict)
    """Per-label globs matched against the paths a pull request changes.

    The `[tool.repomatic.labels.file-rules]` table maps each label to the
    globs that apply it, evaluated by `apply-labels` against the changed
    files. The label applies when any changed file matches the glob set:

    ```toml
    [tool.repomatic.labels.file-rules]
    "🥭 mango" = ["orchard/**", "!orchard/generated/**"]
    ```

    Globs follow the `minimatch` dialect (`**` crosses directories, `{a,b}`
    expands, a leading dot needs no special casing), and a `!`-prefixed entry
    subtracts from the label's other globs the way a `.gitignore` line would.
    An entry for a label the bundled defaults also carry replaces the default
    entry, and an empty list disables it (see
    {data}`repomatic.labels.DEFAULT_FILE_RULES`).
    """

    sync: bool = True
    """Whether label sync is enabled for this project.

    Projects that manage their own repository labels and do not want the
    labels workflow to overwrite them can set this to `false`.
    """


@dataclass
class LintDepsConfig:
    """Nested schema for `[tool.repomatic.lint-deps]`."""

    allow: dict[str, str] = field(default_factory=dict)
    """Packages that may ship from somewhere other than PyPI, and why.

    `lint-deps` blocks a release whose dependencies do not all resolve from
    the index its users will install from. A handful of arrangements are
    legitimate exceptions: a member of the same monorepo published under its
    own name, a private mirror an internal project genuinely targets. Name
    each one here, mapped to the reason it is safe:

    ```toml
    [tool.repomatic]
    lint-deps.allow = { papaya = "monorepo workspace member, published separately" }
    ```

    A mapping rather than a list, deliberately: the reason is the point.
    An exemption without one is indistinguishable from a forgotten
    development shortcut six months later, which is the exact thing this gate
    exists to catch. The reason renders in the report and in the release PR
    banner, so an accepted exception stays visible instead of disappearing.

    Per-package only, with no global off switch, following
    `exclude-newer-package`: an exemption narrow enough to name is one
    somebody weighed. Listing a package does not silence its transitive
    dependencies, which stay gated on their own.
    """

    comment_word_threshold: int = 40
    """Word count above which `lint-deps` warns about a floor comment.

    A floor comment justifies the version in force: what breaks below it, and
    where the project would notice. It is not a running log of every earlier
    floor, which is what it turns into when each bump appends a paragraph and
    deletes nothing. `lint-deps` emits a non-fatal warning for every comment
    longer than this many words. Set to `0` to disable the check.

    It starts at the same 40 words as `changelog.bullet-word-threshold`, and
    stays an independent knob: both cap a paragraph written for a reader who
    came looking for one fact, but a project that wants its floors terser than
    its release notes says so here alone.
    """


@dataclass
class MetricsConfig:
    """Nested schema for `[tool.repomatic.metrics]`."""

    charts: list[dict[str, str | list[str]]] = field(default_factory=list)
    """Charts to draw from the accumulated history, one array-of-tables entry each.

    Each entry carries an `output` path, an optional `metric` (`stars` by
    default, and only a metric the store accrues can be charted), an optional
    `mode` (`absolute`, the default, or `relative`) measuring the horizontal
    axis, an optional `scale` (`linear`, the default, or `logarithmic`)
    measuring the vertical one, an optional `only` list naming the subjects to
    plot in draw order, and an optional `title` used as the chart's accessible
    name:

    ```toml
    [[tool.repomatic.metrics.charts]]
    output = "./docs/assets/star-history.svg"

    [[tool.repomatic.metrics.charts]]
    mode = "relative"
    output = "./docs/assets/star-history-by-age.svg"

    [[tool.repomatic.metrics.charts]]
    only = [ "apricot" ]
    output = "./docs/assets/star-history-apricot.svg"

    [[tool.repomatic.metrics.charts]]
    scale = "logarithmic"
    output = "./docs/assets/star-history-compared.svg"
    ```

    The two axes are independent, and a chart comparing projects of different
    sizes usually wants both: `mode = "relative"` slides every curve onto a
    common origin, and `scale = "logarithmic"` keeps the smallest of them off
    the axis.

    An entry omitting `only` plots every declared subject. Declaring none of
    these leaves the history accruing with nothing drawn from it, which is a
    valid way to collect first and decide later.
    """

    colors: dict[str, list[str]] = field(default_factory=dict)
    """Per-subject `[light, dark]` hex pairs overriding the positional palette.

    Hues are assigned from {data}`repomatic.metric_chart.SERIES_PALETTE` in
    draw order, so a subject keeps its colour as long as the order holds. Pin
    one here when it must survive a reordering, or when a chart plots more
    curves than the palette holds:

    ```toml
    [tool.repomatic.metrics.colors]
    apricot = [ "#2a78d6", "#3987e5" ]
    ```
    """

    forges: dict[str, str] = field(default_factory=dict)
    """Self-hosted forge instances, mapping each host to the software it runs.

    Merged over {data}`repomatic.forge.FORGE_APIS`, which only knows the three
    public hosts. A self-hosted instance is never guessed from its name, so an
    undeclared host raises rather than sampling nothing:

    ```toml
    [tool.repomatic.metrics.forges]
    "gitlab.example.org" = "gitlab"
    "codeberg.example.org" = "forgejo"
    ```

    Values are `forgejo`, `github` or `gitlab`; Gitea instances read as
    `forgejo`, whose API they share.
    """

    predecessors: dict[str, str] = field(default_factory=dict)
    """Retired forerunners, mapping the subject they precede to their own repository.

    A project that reopened under a new repository carries an audience it
    inherited rather than one it gathered, which a by-age chart would otherwise
    misreport as the fastest start in the field:

    ```toml
    [tool.repomatic.metrics.predecessors]
    papaya = "old-owner/papaya"
    ```

    Drawn in the successor's own hue to tie the two together, but dashed and
    never joined to it: the counts are independent tallies on separate
    repositories, so a continuous line would claim a running total no
    repository ever showed. The forerunner's line stops where its successor's
    begins.
    """

    skip: dict[str, str] = field(default_factory=dict)
    """Subjects deliberately left unmeasured, mapped to the reason why.

    A mapping rather than a list, following `lint-deps.allow`: the reason is
    the point. A project absent from both tables is an oversight a conformance
    test can report, while one listed here is a decision:

    ```toml
    [tool.repomatic.metrics.skip]
    papaya = "Ships in a distribution package with no public repository."
    ```

    Nothing is sampled for them, and whatever renders the readings leaves their
    cells empty.
    """

    store: str = "./docs/assets/metrics.csv"
    """Where the readings accumulate, one row per subject, metric and date."""

    subjects: dict[str, str] = field(default_factory=dict)
    """Repositories to track, mapping each subject name to its repository.

    The name labels the curve and keys its colour, so it is what a reader sees.
    A bare `owner/name` is GitHub; anything else is a full URL on whichever
    forge hosts it:

    ```toml
    [tool.repomatic.metrics.subjects]
    apricot = "apricot-org/apricot"
    papaya = "https://gitlab.com/papaya/papaya"
    ```

    Every subject is read for every metric its forge answers. The two deep
    collectors are GitHub-only and skip the rest with a note: an exact star
    reconstruction reads per-star timestamps, and the archive backfill mines
    `github.com` pages.
    """

    sync: bool = False
    """Whether `sample-metrics` records readings for this repository.

    Opt-in, and the gate on the `metrics.yaml` workflow: a repository tracking
    nothing should not carry a weekly job, and an accumulating store is a
    commitment a maintainer makes deliberately.
    """


@dataclass
class SyncRunnerImagesConfig:
    """Nested schema for `[tool.repomatic.sync-runner-images]`."""

    ignore: list[str] = field(default_factory=list)
    """Runner labels never to propose, whatever GitHub announces about them.

    A `sync-*` job regenerates on every push, so a proposal declined by closing
    its pull request comes back on the next one. Without somewhere to record
    the decision, the only way to stop a proposal already considered and
    rejected is to disable the whole operation. Naming the label here is the
    one-line commit that makes a "no" stick:

    ```toml
    [tool.repomatic.sync-runner-images]
    # 26.04 stays out until its capacity settles: queue time matters more here
    # than the compute it wins.
    ignore = [ "ubuntu-26.04", "ubuntu-26.04-arm" ]
    ```

    Applies to both shapes: an ignored label is neither probed when it arrives
    nor proposed as a successor when something retires onto it.
    """


@dataclass
class TestMatrixConfig:
    """Nested schema for `[tool.repomatic.test-matrix]`.

    Keys inside `replace` and `variations` are GitHub Actions matrix
    identifiers (e.g., `os`, `python-version`) and must not be
    normalized to snake_case. Click Extra's
    `click_extra.normalize_keys = False` metadata on the parent field
    prevents this.
    """

    exclude: list[dict[str, str]] = field(default_factory=list)
    """Extra exclude rules applied to both full and PR test matrices.

    Each entry is a dict of GitHub Actions matrix keys (like
    `{"os": "windows-11-arm"}`) that removes matching combinations.
    Additive to the upstream default excludes.
    """

    full_include: list[dict[str, str]] = field(
        default_factory=list,
        metadata={CONFIG_PATH_METADATA_KEY: "full-include"},
    )
    """Full-matrix-only job rows, added as standalone matrix combinations.

    Each entry is a dict of GitHub Actions matrix keys fully describing one job
    (like `{"os": "ubuntu-26.04-arm", "python-version": "3.10",
    "click-version": "8.3.1"}`). Unlike `include`, these are appended as
    independent rows of the full matrix, never merged into the base
    cross-product, so a cell can't overwrite a shipped-config job that shares
    its `os` and `python-version`. Keys left out inherit the matrix defaults
    (the single-key `include` entries, plus `state: stable`), so a cell lists
    only what differs from the shipped configuration.

    Use this for heterogeneous coverage, like pinning each release of a
    dependency to its own runner and Python, where carving the same shape from
    the base cross-product with `exclude` would take many rules. Like
    `variations` and `unstable`, it touches the full matrix only; the PR matrix
    stays a curated reduced set. Adding any entry makes the full matrix emit as
    a flat job list (`{"include": [...]}`), which GitHub runs verbatim with no
    cross-product expansion.
    """

    include: list[dict[str, str]] = field(default_factory=list)
    """Extra include directives applied to both full and PR test matrices.

    Each entry is a dict of GitHub Actions matrix keys that adds or augments
    matrix combinations. Additive to the upstream default includes.

    Because includes apply to both matrices, a directive whose keys are not PR
    base axes is risky. In the PR matrix only `os` and `python-version` are base
    axes, so a key like `click-version` (injected by another include) has
    nothing to match and GitHub's expansion adds the directive to every PR job,
    overwriting it. To flag a value continue-on-error, prefer `unstable` over an
    `include` carrying `state: unstable`.
    """

    remove: dict[str, list[str]] = field(default_factory=dict)
    """Per-axis value removals applied to both full and PR test matrices.

    Outer key is the variation/axis ID (e.g., `os`, `python-version`).
    Inner list contains values to drop from that axis. Applied after
    replacements but before excludes, includes, and variations.
    """

    replace: dict[str, dict[str, str]] = field(default_factory=dict)
    """Per-axis value replacements applied to both full and PR test matrices.

    Outer key is the variation/axis ID (e.g., `os`, `python-version`).
    Inner dict maps old values to new values. Applied before removals,
    excludes, includes, and variations.
    """

    unstable: list[dict[str, str]] = field(default_factory=list)
    """Full-matrix-only combinations to flag continue-on-error in CI.

    Each entry is a dict of GitHub Actions matrix keys (like
    `{"click-version": "main"}`). Every full-matrix combination matching an
    entry gets a `state: unstable` value, which `tests.yaml` reads to set
    `continue-on-error`. Like `variations`, this applies to the full matrix
    only; the PR matrix stays a curated stable set.

    Prefer this over an `include` entry carrying `state: unstable`. `include`
    applies to both matrices, and in the PR matrix a key like `click-version`
    is not a base axis (another `include` injects it), so GitHub's expansion
    would add the directive to every PR job and overwrite it. `unstable` only
    touches the full matrix, sidestepping that hijack.
    """

    variations: dict[str, list[str]] = field(default_factory=dict)
    """Extra matrix dimension values added to the full test matrix only.

    Each key is a dimension ID (e.g., `os`, `click-version`) and its value
    is a list of additional entries. For existing dimensions, values are merged
    with the upstream defaults. For new dimension IDs, a new axis is created.
    Only affects the full matrix; the PR matrix stays a curated reduced set.
    """


@dataclass
class VulnerableDepsConfig:
    """Nested schema for `[tool.repomatic.vulnerable-deps]`."""

    sources: list[str] = field(
        default_factory=lambda: ["uv-audit", "github-advisories"],
    )
    """Advisory databases to consult for known vulnerabilities.

    Recognized values:

    - `"uv-audit"`: PyPA Advisory Database via `uv audit` (works locally
      and in CI without a GitHub token).
    - `"github-advisories"`: GitHub Advisory Database via the
      repository's Dependabot alerts (CI-only, requires a token with
      `Dependabot alerts: Read-only`).

    Sources are unioned and deduplicated per package by advisory
    identity: entries sharing an `advisory_id` or a cross-referenced
    CVE/GHSA/PYSEC alias are merged. Repositories that distrust GHSA, or
    have no Dependabot alerts enabled, can opt out with
    `sources = ["uv-audit"]`.
    """

    sync: bool = True
    """Whether the `fix-vulnerable-deps` job is enabled for this project.

    Projects that manage their own vulnerability remediation flow can set
    this to `false` to skip the autofix job.
    """


@dataclass
class WorkflowConfig:
    """Nested schema for `[tool.repomatic.workflow]`."""

    source_paths: list[str] | None = None
    """Source code directory names for workflow trigger `paths:` filters.

    When set, thin-caller and header-only workflows include `paths:` filters
    using these directory names (as `name/**` globs) alongside universal paths
    like `pyproject.toml` and `uv.lock`.

    When `None` (default), source paths are auto-derived from
    `[project.name]` in `pyproject.toml` by replacing hyphens with
    underscores, the universal Python convention. For example,
    `name = "extra-platforms"` automatically uses `["extra_platforms"]`.
    """

    extra_paths: list[str] = field(default_factory=list)
    """Literal entries to append to every workflow's `paths:` filter.

    Applies to thin-caller and header-only sync. Useful for repo-specific
    files that should re-trigger CI but are not detected by the canonical
    `paths:` filter (e.g., `install.sh`, `dotfiles/**`).

    Per-workflow overrides in `paths` ignore this list: when an entry exists
    for a given filename, that entry is treated as the complete list.
    """

    ignore_paths: list[str] = field(default_factory=list)
    """Literal entries to strip from every workflow's `paths:` filter.

    Useful for canonical entries that don't exist downstream (e.g.,
    `tests/**`, `uv.lock` in repos with no Python tests or lockfile).
    Match is by exact string equality. Applies before `extra_paths`.

    Per-workflow overrides in `paths` ignore this list.
    """

    paths: dict[str, list[str]] = field(default_factory=dict)
    """Per-workflow override of the `paths:` filter, keyed by filename.

    When a workflow filename appears here, its `paths:` blocks (in `push`,
    `pull_request`, etc.) are replaced wholesale with the listed entries.
    `source_paths`, `extra_paths`, and `ignore_paths` do **not** apply when
    a per-workflow override is set: the list is treated as authoritative.

    Override only takes effect on triggers that already have a `paths:`
    filter in the canonical workflow. Workflows without `paths:` upstream
    keep their unrestricted trigger semantics.

    Example:

    ```toml
    [tool.repomatic.workflow.paths]
    "tests.yaml" = ["install.sh", "packages.toml", ".github/workflows/tests.yaml"]
    ```
    """

    sync: bool = True
    """Whether workflow sync is enabled for this project.

    Projects that manage their own workflow files and do not want the autofix job
    to sync thin callers or headers can set this to `false`.
    """


@dataclass
class Config:
    """Configuration schema for `[tool.repomatic]` in `pyproject.toml`.

    This dataclass defines the structure and default values for repomatic configuration.
    Each field has a docstring explaining its purpose.
    """

    abandoned_versions: list[str] = field(default_factory=list)
    """Versions documented in the changelog but never published.

    A version reached only its `[changelog] Release vX.Y.Z` freeze and was then
    skipped per `CLAUDE.md` § Skip and move forward (botched build, broken
    artifact, bad metadata) without rewriting history. List those versions here
    so `lint-changelog` reports them as skipped (an info log line) instead of
    flagging them every run as `⚠ X.Y.Z: not found on PyPI`. Applies to both
    PyPI lookups and the git-tag fallback.
    """

    action_pins_sync: bool = field(
        default=True,
        metadata={CONFIG_PATH_METADATA_KEY: "action-pins.sync"},
    )
    """Whether the `sync-action-pins` job is enabled for this project.

    Bumps SHA-pinned GitHub Actions (`uses: owner/repo@<sha> # vX.Y.Z`) to the
    latest release passing the `minimum-release-age` cooldown. Projects that
    pin actions by hand can set this to `false`.
    """

    awesome_template_sync: bool = field(
        default=True,
        metadata={CONFIG_PATH_METADATA_KEY: "awesome-template.sync"},
    )
    """Whether awesome-template sync is enabled for this project.

    Repositories whose name starts with `awesome-` get their boilerplate synced
    from files bundled in `repomatic`. Set to `false` to opt out.
    """

    binaries_sync: bool = field(
        default=True,
        metadata={CONFIG_PATH_METADATA_KEY: "binaries.sync"},
    )
    """Whether the release pipeline records released binaries into the repository.

    When enabled, the `scan-virustotal` release job regenerates the binaries
    catalog (`docs/binaries.md` and `docs/assets/binaries.csv`) and publishes
    it, along with the scan history (`docs/assets/virustotal-scans.csv`),
    through one long-lived pull request that each release appends to: the
    contract documented in
    [`docs/operation-contracts.md`](https://repomatic.net/operation-contracts#scan-job-contract).
    Set to `false` to keep the repository untouched: binaries are still
    scanned on VirusTotal (seeding AV vendor databases), but no catalog page,
    CSV, or scan record is published.
    """

    bumpversion_sync: bool = field(
        default=True,
        metadata={CONFIG_PATH_METADATA_KEY: "bumpversion.sync"},
    )
    """Whether bumpversion config sync is enabled for this project.

    Projects that manage their own `[tool.bumpversion]` section and do not want
    the autofix job to overwrite it can set this to `false`.
    """

    cache: CacheConfig = field(
        default_factory=CacheConfig,
        metadata={CONFIG_PATH_METADATA_KEY: "cache"},
    )
    """Binary cache configuration."""

    changelog_archive_location: str = field(
        default="",
        metadata={CONFIG_PATH_METADATA_KEY: "changelog.archive-location"},
    )
    """File path of the changelog archive, relative to the root of the repository.

    The archive holds older release sections split out of the live changelog to
    keep it small. Empty (the default) disables archive handling.

    When set, `lint-changelog` treats versions documented in the archive as
    present, so they are neither reported nor re-inserted as *orphans* (versions
    found on PyPI, GitHub, or git tags but missing from the changelog). The
    archive is frozen: its released entries are immutable and are not
    re-validated against their canonical release dates.
    """

    changelog_bullet_word_threshold: int = field(
        default=40,
        metadata={CONFIG_PATH_METADATA_KEY: "changelog.bullet-word-threshold"},
    )
    """Word count above which `lint-changelog` warns about a changelog bullet.

    A changelog entry is a release note, not a commit message: ideally one
    short sentence stating what changed (see `CLAUDE.md` § Changelog entry
    length). `lint-changelog` emits a non-fatal warning for every bullet in
    the *unreleased* section longer than this many words, nudging verbose,
    implementation-heavy entries back toward a user-facing summary. Released
    sections are immutable and never flagged. Set to `0` to disable the check.
    """

    changelog_location: str = field(
        default="./changelog.md",
        metadata={CONFIG_PATH_METADATA_KEY: "changelog.location"},
    )
    """File path of the changelog, relative to the root of the repository."""

    debug_sync: bool = field(
        default=False,
        metadata={CONFIG_PATH_METADATA_KEY: "debug.sync"},
    )
    """Whether the `debug.yaml` workflow is deployed to this project.

    Opt-in: the workflow dumps the GitHub contexts and the runner's host probes
    across every build target, which answers a question a maintainer asks while
    chasing a runner difference and nothing else reads. Left on by default it
    spends a monthly matrix of runners, the scarce macOS and Windows ones
    included, producing logs nobody opens.
    """

    dep_sources_sync: bool = field(
        default=True,
        metadata={CONFIG_PATH_METADATA_KEY: "dep-sources.sync"},
    )
    """Whether the `sync-dep-sources` updater is enabled for this project.

    Swaps a dependency tracked from a git branch back to its released version
    once the release named by its `.dev` version floor ships on PyPI (see
    {mod}`repomatic.deps.dep_sources` for the managed idiom). Projects that manage
    `[tool.uv.sources]` overrides by hand can set this to `false`.
    """

    dependency_graph: DependencyGraphConfig = field(
        default_factory=DependencyGraphConfig,
        metadata={CONFIG_PATH_METADATA_KEY: "dependency-graph"},
    )
    """Dependency graph generation configuration."""

    dev_release_sync: bool = field(
        default=True,
        metadata={CONFIG_PATH_METADATA_KEY: "dev-release.sync"},
    )
    """Whether dev pre-release sync is enabled for this project.

    Projects that do not want a rolling draft pre-release maintained on
    GitHub can set this to `false`.
    """

    docs: DocsConfig = field(
        default_factory=DocsConfig,
        metadata={CONFIG_PATH_METADATA_KEY: "docs"},
    )
    """Sphinx documentation generation configuration."""

    exclude: list[str] = field(default_factory=list)
    """Additional components and files to exclude from repomatic operations.

    Additive to the default exclusions (`agents`, `labels`, `skills`). Bare
    names exclude an entire component (e.g., `"workflows"`). Qualified
    `component/identifier` entries exclude a specific file within a component
    (e.g., `"workflows/autolock.yaml"`, `"skills/repomatic-audit"`,
    `"labels/labels.toml"`).

    Affects `repomatic init`, `workflow sync`, and `workflow create`.
    Explicit CLI positional arguments override this list.
    """

    flavor: FlavorConfig = field(
        default_factory=FlavorConfig,
        metadata={CONFIG_PATH_METADATA_KEY: "flavor"},
    )
    """Which agent and CI ecosystem this repository targets."""

    gitignore: GitignoreConfig = field(
        default_factory=GitignoreConfig,
        metadata={CONFIG_PATH_METADATA_KEY: "gitignore"},
    )
    """`.gitignore` sync configuration."""

    include: list[str] = field(default_factory=list)
    """Components and files to force-include, overriding default exclusions.

    Use this to opt into components that are excluded by default (`agents`,
    `labels`, `skills`). Each entry is subtracted from the effective exclude
    set (defaults + user `exclude`) and bypasses `RepoScope` filtering, so
    scope-restricted components (like awesome-only skills or Python-only
    `publish-pypi-action`) are included regardless of repository type.
    Qualified entries (`component/file`) implicitly select the parent
    component. Same syntax as `exclude`.
    """

    labels: LabelsConfig = field(
        default_factory=LabelsConfig,
        metadata={CONFIG_PATH_METADATA_KEY: "labels"},
    )
    """Repository label sync configuration."""

    lint_deps: LintDepsConfig = field(
        default_factory=LintDepsConfig,
        metadata={CONFIG_PATH_METADATA_KEY: "lint-deps"},
    )
    """Dependency shippability gate configuration."""

    mailmap_sync: bool = field(
        default=True,
        metadata={CONFIG_PATH_METADATA_KEY: "mailmap.sync"},
    )
    """Whether `.mailmap` sync is enabled for this project.

    Projects that manage their own `.mailmap` and do not want the autofix job
    to overwrite it can set this to `false`.
    """

    manpages_asset_name: str = field(
        default="",
        metadata={CONFIG_PATH_METADATA_KEY: "manpages.asset-name"},
    )
    """Filename stem (without the `.tar.gz` extension) for the man-page tarball
    uploaded to the GitHub release.

    Defaults to `<package-name>-manpages` when left empty and `manpages.script`
    is set. Has no effect when `manpages.script` is empty.
    """

    manpages_script: str = field(
        default="",
        metadata={CONFIG_PATH_METADATA_KEY: "manpages.script"},
    )
    """Click command target whose tree gets rendered as roff `.1` files and
    attached as a tarball asset on every GitHub release.

    Same shape the `click-extra wrap` CLI accepts: a `module:function` path
    (preferred for projects whose console-script entry point dispatches through
    a wrapper), an entry-point name, a `.py` file path, or a plain importable
    module name. Leave empty to disable release-attached man pages.

    The release job renders the tree with `click-extra wrap --help-format man`,
    so the project needs `click-extra >= 9` in its own environment.
    """

    metrics: MetricsConfig = field(
        default_factory=MetricsConfig,
        metadata={CONFIG_PATH_METADATA_KEY: "metrics"},
    )
    """What forges say about the repositories this project tracks, over time."""

    minimum_release_age: str = field(
        default="1 week",
        metadata={CONFIG_PATH_METADATA_KEY: "minimum-release-age"},
    )
    """Stabilization window before a new upstream release is adopted.

    Shared cooldown for the `sync-tool-versions`, `sync-action-pins`, and
    `sync-workflow-pins` jobs: a release is only proposed once it has been
    public for at least this long, giving upstream time to yank a bad cut. It
    also gates `repomatic run`'s ad-hoc installs at run time, so their
    transitive trees honor the same window: `uvx` tools via uv's
    `--exclude-newer`, npm tools via npm's `min-release-age`. `repomatic init`
    honors it too: the derived upstream workflow pin steps back to the newest
    release past the window (override with `--no-cooldown`). The
    GitHub/PyPI/npm counterpart to uv's `exclude-newer` (which guards
    `sync-uv-lock`). Accepts the same friendly durations (`8 days`, `2 weeks`,
    `36 hours`). Set to `0 days` to adopt releases immediately.
    """

    notification_unsubscribe: bool = field(
        default=False,
        metadata={CONFIG_PATH_METADATA_KEY: "notification.unsubscribe"},
    )
    """Whether the unsubscribe-threads workflow is enabled.

    Notifications are per-user across all repos. Enable on the single repo where
    you want scheduled cleanup of closed notification threads. Requires a classic
    PAT with `notifications` scope stored as `REPOMATIC_NOTIFICATIONS_PAT`.
    """

    nuitka_dev_targets: list[str] = field(
        default_factory=lambda: ["linux-arm64"],
        metadata={CONFIG_PATH_METADATA_KEY: "nuitka.dev-targets"},
    )
    """Nuitka build targets compiled on ordinary pushes, as a canary.

    An ordinary push to the default branch rebuilds binaries only for these
    targets: enough to catch a compilation break early, while freeing runner
    slots the full fleet would occupy on every code push just to refresh the
    rolling dev pre-release (a draft). The full target roster still builds on
    release commits, on the weekly `schedule` trigger, and on
    `workflow_dispatch`. Defaults to `["linux-arm64"]`, the fastest and
    cheapest builder. Set to `[]` to skip dev builds entirely.
    """

    nuitka_enabled: bool = field(
        default=True,
        metadata={CONFIG_PATH_METADATA_KEY: "nuitka.enabled"},
    )
    """Whether Nuitka binary compilation is enabled for this project.

    Projects with `[project.scripts]` entries that are not intended to produce
    standalone binaries (e.g., libraries with convenience CLI wrappers) can set this
    to `false` to opt out of Nuitka compilation.
    """

    nuitka_entry_points: list[str] = field(
        default_factory=list,
        metadata={CONFIG_PATH_METADATA_KEY: "nuitka.entry-points"},
    )
    """Which `[project.scripts]` entry points produce Nuitka binaries.

    List of CLI IDs (e.g., `["mpm"]`) to compile. When empty (the default),
    deduplicates by callable target: keeps the first entry point for each
    unique `module:callable` pair. This avoids building duplicate binaries
    when a project declares alias entry points (like both `mpm` and
    `meta-package-manager` pointing to the same function).
    """

    nuitka_extras: list[str] = field(
        default_factory=list,
        metadata={CONFIG_PATH_METADATA_KEY: "nuitka.extras"},
    )
    """`[project.optional-dependencies]` extras to install before the Nuitka build.

    List of extra names (like `["sbom"]`) to sync into the build venv before
    invoking Nuitka. By default the binary build only sees the project's base
    dependencies, which matches a bare `pip install <package>` and excludes
    optional features. Listing an extra here calls `uv sync --frozen --extra
    <name>` before the Nuitka build so the binary can bundle the optional
    feature's third-party packages (paired with `--include-package` in
    `[tool.nuitka]` for imports guarded behind `try/except`).
    """

    nuitka_nofollow_imports: list[str] = field(
        default_factory=lambda: ["tkinter"],
        metadata={CONFIG_PATH_METADATA_KEY: "nuitka.nofollow-imports"},
    )
    """Module names Nuitka must not follow into the compiled binary.

    Each name is forwarded as a `--nofollow-import-to` flag by `repomatic run
    nuitka`. Defaults to `["tkinter"]`: `boltons.ecoutils` (in the dependency
    tree of every click-extra CLI) probes tkinter inside a guarded `try`/
    `except` import, which otherwise drags the whole Tcl/Tk stack into every
    binary. Excluded modules raise `ImportError` when imported at run time,
    which guarded imports absorb. GUI projects that really ship tkinter can
    set this to `[]`.
    """

    nuitka_unstable_targets: list[str] = field(
        default_factory=list,
        metadata={CONFIG_PATH_METADATA_KEY: "nuitka.unstable-targets"},
    )
    """Nuitka build targets allowed to fail without blocking the release.

    List of target names (e.g., `["linux-arm64", "windows-x64"]`) that are marked as
    unstable. Jobs for these targets will be allowed to fail without preventing the
    release workflow from succeeding.
    """

    pypi_package_history: list[str] = field(default_factory=list)
    """Former PyPI package names for projects that were renamed.

    When a project changes its PyPI name, older versions remain published under
    the previous name. List former names here so `lint-changelog` can fetch
    release metadata from all names and generate correct PyPI URLs.
    """

    release_assets: list[str] = field(
        default_factory=list,
        metadata={CONFIG_PATH_METADATA_KEY: "release-assets"},
    )
    """Extra asset filenames attached to every GitHub release.

    Each listed file must be produced by a job the consumer defines in its own
    release workflow (alongside the `build` lane the engine call already gates
    on) and uploaded as a run artifact named `release-asset-<filename>`. The
    engine's `extra-assets` job downloads the artifacts, attests them with the
    same provenance chain as the compiled binaries, and attaches them to the
    release draft before publication locks it (GitHub immutable releases).

    The build code stays in the downstream repository as regular workflow
    code, reviewed and linted there: the engine never executes
    consumer-supplied commands. Filenames must be space-free, as they travel
    through a space-separated job environment variable. Leave empty to
    disable, which keeps the job silent.
    """

    settings_location: str = field(
        default=AGENT_LAYOUTS[DEFAULT_AGENT].settings,
        metadata={CONFIG_PATH_METADATA_KEY: "settings.location"},
    )
    """Path to the agent's project settings file, relative to the repository root.

    Left unset, it follows `[tool.repomatic.flavor] agent`; setting it
    explicitly overrides that.

    Only the `plugin` component writes here, merging the marketplace and
    enablement keys it owns into whatever the file already holds.
    """

    setup_guide: bool = True
    """Whether the setup guide issue is enabled for this project.

    Projects that do not need `REPOMATIC_PAT` or manage their
    own PAT setup can set this to `false` to suppress the setup guide issue.
    """

    site_cloudflare_compatibility_date: str = field(
        default="",
        metadata={CONFIG_PATH_METADATA_KEY: "site.cloudflare-compatibility-date"},
    )
    """Workers runtime date the Cloudflare Pages project is pinned to.

    A `YYYY-MM-DD` date, compared and enforced by `repomatic cloudflare-pages`
    against the live project's `deployment_configs`, on both the production and
    preview environments. Inert while the project has no Pages Functions, which
    is exactly how it drifts unnoticed: the value only starts mattering the
    moment a Function is added, long after anyone last chose it. Empty (the
    default) leaves the live value unmanaged.

    This is server-side state, not the `wrangler.toml` key of the same name:
    Cloudflare honours the project's own configuration, and the file only
    matters to a build that a Direct Upload project never runs. `lint-repo`
    warns when a committed `wrangler.toml` disagrees, so the repository states
    one value rather than two.
    """

    site_cloudflare_placement: str = field(
        default="",
        metadata={CONFIG_PATH_METADATA_KEY: "site.cloudflare-placement"},
    )
    """Smart Placement mode declared for the Cloudflare Pages project.

    `smart` or `off`, compared and enforced by `repomatic cloudflare-pages` on
    both environments. For a static site it changes nothing measurable and
    costs nothing; declaring it means the dashboard toggle stops looking like
    an accident. Empty (the default) leaves the live value unmanaged.
    """

    site_cloudflare_project: str = field(
        default="",
        metadata={CONFIG_PATH_METADATA_KEY: "site.cloudflare-project"},
    )
    """Name of the Cloudflare Pages project the site deploys into.

    Empty (the default) names the project after the repository, which is what
    the deploy job falls back to. Set it when the project predates repomatic or
    otherwise cannot carry the repository's name: renaming a live Pages project
    would move the `<project>.pages.dev` hostname every custom domain CNAMEs
    through.
    """

    site_deploy: str = field(
        default="github-pages",
        metadata={CONFIG_PATH_METADATA_KEY: "site.deploy"},
    )
    """Where this repository's built site is published.

    `github-pages`, the default, has the Docs workflow upload the Sphinx tree
    as a Pages artifact and deploy it with the repository's own OIDC identity:
    no stored credential, and nothing to configure beyond enabling Pages.

    `cloudflare-pages` uploads it to a Cloudflare Pages project instead, named
    per `site.cloudflare-project`, through `wrangler pages deploy`. That path
    needs one repository secret, `CLOUDFLARE_API_TOKEN`, and it trades the
    OIDC deploy for a long-lived token: the Docs workflow's monthly run is
    what surfaces its expiry, since Cloudflare warns about neither an
    approaching lapse nor a passed one.

    A property of the site rather than of Sphinx. A repository whose site is
    built by its own workflow (a Pelican blog, a hand-rolled static tree)
    declares the target here too: that is what turns on the credential checks,
    the setup-guide step and the Cloudflare drift job for it, even though the
    Docs workflow's own Sphinx build never runs.

    Choose Cloudflare for what the edge can do rather than for speed. A custom
    domain on Cloudflare Pages carries its own certificate, so the zone's apex
    can be proxied, which is what a `_redirects` file, a real `404.html` and
    any edge rule on the apex all depend on.
    """

    skills_location: str = field(
        default=AGENT_LAYOUTS[DEFAULT_AGENT].skills,
        metadata={CONFIG_PATH_METADATA_KEY: "skills.location"},
    )
    """Directory prefix for skill folders, relative to the repository root.

    Left unset, it follows `[tool.repomatic.flavor] agent`; setting it
    explicitly overrides that.

    Skill files are written as `{skills_location}/{skill-id}/SKILL.md`.
    Useful for repositories where `.claude/` is not at the root (like
    dotfiles repos that store configs under a subdirectory).
    """

    sphinx_builder: str = field(
        default="html",
        metadata={CONFIG_PATH_METADATA_KEY: "sphinx.builder"},
    )
    """Sphinx builder producing the deployed documentation site.

    The default `html` writes `page.html`, so the site serves `/page.html`.
    Setting it to `dirhtml` writes `page/index.html` instead, so the same page
    serves at `/page/` and the published URLs carry no extension, which is the
    shape search engines and most static hosts expect.

    The one Sphinx setting a project cannot make in its own `conf.py`, hence a
    config key: the builder is chosen on the command line, and `docs.yaml` is
    what runs it. Switching an already-published site republishes every URL it
    has: the old paths stop existing, so the repository's own absolute
    self-links (readme, packaging specs) move in the same commit, and whatever
    fronts the site redirects the old ones.
    """

    subagents_location: str = field(
        default=AGENT_LAYOUTS[DEFAULT_AGENT].subagents,
        metadata={CONFIG_PATH_METADATA_KEY: "subagents.location"},
    )
    """Directory prefix for subagent definitions, relative to the repository root.

    Left unset, it follows `[tool.repomatic.flavor] agent`; setting it
    explicitly overrides that.

    Subagent files are written as `{subagents_location}/{agent-id}.md`.
    Useful for repositories where `.claude/` is not at the root (like
    dotfiles repos that store configs under a subdirectory).
    """

    sync_runner_images: SyncRunnerImagesConfig = field(
        default_factory=SyncRunnerImagesConfig,
        metadata={CONFIG_PATH_METADATA_KEY: "sync-runner-images"},
    )
    """Runner image pull request configuration."""

    test_matrix: TestMatrixConfig = field(
        default_factory=TestMatrixConfig,
        metadata={
            CONFIG_PATH_METADATA_KEY: "test-matrix",
            NORMALIZE_KEYS_METADATA_KEY: False,
        },
    )
    """Per-project customizations for the GitHub Actions CI test matrix.

    Keys inside this section are GitHub Actions matrix identifiers (e.g.,
    `os`, `python-version`) and must not be normalized to snake_case.
    """

    tool_versions_sync: bool = field(
        default=True,
        metadata={CONFIG_PATH_METADATA_KEY: "tool-versions.sync"},
    )
    """Whether the `sync-tool-versions` job is enabled for this project.

    Bumps every tool in the `repomatic run` registry to the latest release
    passing the `minimum-release-age` cooldown (GitHub releases for binary
    tools, PyPI for the rest), recomputing binary checksums in the same pass.
    Projects that pin tool versions by hand can set this to `false`.
    """

    uv_lock_sync: bool = field(
        default=True,
        metadata={CONFIG_PATH_METADATA_KEY: "uv-lock.sync"},
    )
    """Whether `uv.lock` sync is enabled for this project.

    Projects that manage their own lock file strategy and do not want the
    `sync-uv-lock` job to run `uv lock --upgrade` can set this to `false`.
    """

    vulnerable_deps: VulnerableDepsConfig = field(
        default_factory=VulnerableDepsConfig,
        metadata={CONFIG_PATH_METADATA_KEY: "vulnerable-deps"},
    )
    """Vulnerable dependency detection and remediation configuration."""

    workflow: WorkflowConfig = field(
        default_factory=WorkflowConfig,
        metadata={CONFIG_PATH_METADATA_KEY: "workflow"},
    )
    """Workflow sync configuration."""

    workflow_pins_sync: bool = field(
        default=True,
        metadata={CONFIG_PATH_METADATA_KEY: "workflow-pins.sync"},
    )
    """Whether the `sync-workflow-pins` job is enabled for this project.

    Bumps version literals embedded in workflow YAML (npm `pkg@x` installs and
    `uvx '<pkg>==x'` PyPI pins) to the latest release passing the
    `minimum-release-age` cooldown. Projects that pin these by hand can set this
    to `false`.
    """

    def __post_init__(self) -> None:
        """Point the asset locations at the selected agent's layout.

        Only a location still sitting at its default is derived, so an explicit
        `skills.location`, `subagents.location` or `settings.location` always
        wins over the flavor. Also rejects a
        `site.deploy` target nothing implements, which would otherwise read as
        a workflow that runs and publishes nowhere, and a
        `site.cloudflare-placement` value the Pages API would bounce far from
        the line that caused it.
        """
        default = AGENT_LAYOUTS[DEFAULT_AGENT]
        layout = self.flavor.layout
        if self.skills_location == default.skills:
            self.skills_location = layout.skills
        if self.subagents_location == default.subagents:
            self.subagents_location = layout.subagents
        if self.settings_location == default.settings:
            self.settings_location = layout.settings
        if self.site_deploy not in SITE_DEPLOY_TARGETS:
            targets = ", ".join(sorted(SITE_DEPLOY_TARGETS))
            msg = (
                f"Unsupported site.deploy {self.site_deploy!r}. Pick one of: {targets}."
            )
            raise ValueError(msg)
        if self.site_cloudflare_placement not in CLOUDFLARE_PLACEMENT_MODES:
            modes = ", ".join(
                sorted(mode for mode in CLOUDFLARE_PLACEMENT_MODES if mode)
            )
            msg = (
                f"Unsupported site.cloudflare-placement"
                f" {self.site_cloudflare_placement!r}. Pick one of: {modes}."
            )
            raise ValueError(msg)


SUBCOMMAND_CONFIG_FIELDS: Final[frozenset[str]] = frozenset((
    "abandoned_versions",
    "action_pins_sync",
    "awesome_template_sync",
    "bumpversion_sync",
    "cache",
    "changelog_archive_location",
    "changelog_bullet_word_threshold",
    "changelog_location",
    "debug_sync",
    "dep_sources_sync",
    "dependency_graph",
    "dev_release_sync",
    "docs",
    "exclude",
    "flavor",
    "gitignore",
    "include",
    "labels",
    "lint_deps",
    "mailmap_sync",
    "metrics",
    "minimum_release_age",
    "notification_unsubscribe",
    "nuitka_enabled",
    "nuitka_nofollow_imports",
    "pypi_package_history",
    "settings_location",
    "setup_guide",
    "site_cloudflare_compatibility_date",
    "site_cloudflare_placement",
    "skills_location",
    "subagents_location",
    "sync_runner_images",
    "test_matrix",
    "tool_versions_sync",
    "uv_lock_sync",
    "vulnerable_deps",
    "workflow",
    "workflow_pins_sync",
))
"""Config fields consumed directly by subcommands, not needed as metadata outputs.

These fields are read directly from `[tool.repomatic]` in `pyproject.toml` by
their respective subcommands (e.g. `dep-graph`), so they no longer need to be
passed through workflow metadata outputs.
"""


def _format_default(value: object) -> str:
    """Format a `Config` field default for the reference table."""
    if value is None:
        return "*(none)*"
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    if isinstance(value, int):
        return f"`{value}`"
    if isinstance(value, str):
        if "\n" in value:
            return "*(see example)*"
        return f'`"{value}"`'
    if isinstance(value, list):
        if not value:
            return "`[]`"
        return f"`{value!r}`"
    return str(value)


def _format_type(annotation: str) -> str:
    """Simplify a type annotation string for the reference table.

    Strips `| None` suffixes since the default column already shows whether
    `None` is the default.
    """
    return annotation.replace(" | None", "")


def escape_type_for_gfm_table(ftype: str) -> str:
    """Escape outer brackets of nested generics for raw GFM table cells.

    Nested generics like `list[dict[str, str]]` would otherwise be
    interpreted by mdformat as a markdown link reference and re-escaped on
    every reformat. Escaping the outermost brackets up front keeps the
    cell stable under mdformat. Simple generics like `list[str]` have no
    nested brackets and stay unescaped.

    Apply this only when the value lands directly in a raw GFM table cell
    (e.g. CLI `show-config` output). Do not apply when wrapping the value
    in inline code backticks: inside a code span, backslashes are literal
    characters in CommonMark and would render visibly as `\\[`.
    """
    if "[" in ftype:
        first = ftype.index("[")
        last = ftype.rindex("]")
        inner = ftype[first + 1 : last]
        if "[" in inner:
            return ftype[:first] + "\\[" + inner + "\\]" + ftype[last + 1 :]
    return ftype


CONFIG_REFERENCE_HEADER_DEFS: tuple[ColumnSpec, ...] = (
    ColumnSpec("option", "Option"),
    ColumnSpec("type", "Type", max_width=24),
    ColumnSpec("default", "Default", max_width=24),
    ColumnSpec("description", "Description", max_width=60),
)
"""Column definitions for the `[tool.repomatic]` configuration reference table."""


def config_reference() -> list[tuple[str, str, str, str]]:
    """Build the `[tool.repomatic]` configuration reference as table rows.

    Introspection comes from click-extra's `schema_field_infos()` (dotted
    kebab-case keys, type annotations, defaults, attribute-docstring
    summaries); this wrapper only applies the Markdown presentation of the
    `show-config` table. Returns a list of
    `(option, type, default, description)` tuples suitable for
    `click_extra.table.print_table`.
    """
    return [
        (
            f"`{info.key}`",
            _format_type(info.type_hint),
            _format_default(info.default),
            info.summary,
        )
        for info in schema_field_infos(Config)
    ]


_UNKNOWN_KEYS_WARNED: set[Path] = set()
"""Projects whose unknown `[tool.repomatic]` keys were already reported.

Every helper needing a setting (cache TTLs, tool cooldowns) re-loads the
config, so a single stale key would otherwise be re-warned on each call,
drowning one actionable line in a dozen duplicates per invocation.
"""

_CONFIG_CACHE: dict[int, tuple[dict[str, Any], Config]] = {}
"""Loaded `Config` instances, keyed by the identity of the parse they came from.

Rebuilding a `Config` walks the whole schema through click-extra's dataclass
instantiation (~0.6 ms), and the hot paths re-load it constantly: every HTTP
cache access resolves {func}`repomatic.cache.cache_dir` and every PyPI, npm and
GitHub lookup re-reads its TTL, so a dependency sweep pays for hundreds of
identical rebuilds. `read_pyproject_toml` returns the *same* dict object for an
unchanged file (its own cache is keyed on mtime and size), so that object's
identity is the file's identity: the entry keeps a strong reference to the
keyed dict, both to pin its `id` and to guard against reuse. An edited file
parses into a new dict and misses the cache naturally.

Safe to share unguarded for the same reason the parse cache is: no production
caller mutates a loaded `Config` (tests that flip flags build their own
`Config()` directly). Cleared wholesale past {data}`_CONFIG_CACHE_MAX` entries,
a bound only a test session churning through fixture projects ever reaches.
"""

_CONFIG_CACHE_MAX = 64
"""Entry count past which {data}`_CONFIG_CACHE` is reset."""


def load_repomatic_config(
    pyproject_data: dict[str, Any] | None = None,
) -> Config:
    """Load `[tool.repomatic]` config merged with `Config` defaults.

    Delegates to click-extra's schema-aware dataclass instantiation, which
    handles normalization, flattening, nested dataclasses, and opaque field
    extraction automatically based on field metadata and type hints.

    Loads are memoized per parsed document (see {data}`_CONFIG_CACHE`), so
    treat the returned instance as read-only.

    :param pyproject_data: Pre-parsed `pyproject.toml` dict. If `None`,
        reads and parses `pyproject.toml` from the current working directory.
    """
    warn_unknown = True
    if pyproject_data is None:
        pyproject_data = read_pyproject_toml()
        # Warn about unknown keys once per project and process: the cwd read
        # makes the resolved pyproject.toml path the project's identity.
        pyproject_path = Path("pyproject.toml").resolve()
        warn_unknown = pyproject_path not in _UNKNOWN_KEYS_WARNED
        _UNKNOWN_KEYS_WARNED.add(pyproject_path)

    cached = _CONFIG_CACHE.get(id(pyproject_data))
    if cached is not None and cached[0] is pyproject_data:
        return cached[1]

    tool_section = pyproject_data.get("tool", {})
    user_config: dict[str, Any] = tool_section.get("repomatic", {})

    # The [tool.repomatic] section is schema-only (the CLI group runs with
    # included_params=()), so warn_unknown flags any key the schema does not
    # know as a typo, nested tables included.
    schema_callable = make_schema_callable(
        Config, strict=False, warn_unknown=warn_unknown
    )
    assert schema_callable is not None
    config: Config = schema_callable(user_config)
    if len(_CONFIG_CACHE) >= _CONFIG_CACHE_MAX:
        _CONFIG_CACHE.clear()
    _CONFIG_CACHE[id(pyproject_data)] = (pyproject_data, config)
    return config
