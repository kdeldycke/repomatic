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

"""Pick the upstream pin `repomatic init` writes, and move it with `--upgrade`.

A downstream repository runs the reusable workflows of the release its thin
callers pin. `init` decides that pin: the running release, held back by the
`[tool.repomatic] minimum-release-age` cooldown when the release is too fresh,
and never moved below the pin the repository already carries. `init --upgrade`
picks the newest release past the cooldown instead, runs `init` as that
release, and describes the move for the pull request that carries it.

This module is the only reader of the running version and of the commit SHA of
its tag, so a test stages a release in one place.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import date, timedelta

from .. import __git_tag_sha__, __version__
from ..deps.pins import (
    MIN_AGE_HELD_BACK_NOTE,
    Candidate,
    UpstreamRefPin,
    exclude_newer_cutoff,
    find_upstream_ref_pins,
    format_cooldown_note,
    github_candidates,
    parse_min_age,
    pypi_candidates,
    select_held_back,
    select_latest,
)
from ..deps.report import (
    build_held_back,
    format_diff_table,
    format_held_back_table,
    format_release_notes,
)
from ..deps.uv import uvx_cmd
from ..github.pr_body import render_template
from ..github.releases import fetch_github_release_notes, resolve_tag_to_sha
from ..github.urls import repo_name
from ..github.workflow_files import github_yaml_paths
from ..humanize import today_utc
from ..markdown import sanitize_markdown_mentions
from ..pypi import PYPI_PACKAGE_URL
from ..release.prepare_release import SELF_PIN_COOLDOWN_EXEMPTION
from ..skills import UPGRADE_SKILL, skill_launcher
from ..upstream import DEFAULT_REPO, is_source_repo
from ..versions import is_newer, parse_tag_version, safe_version, strip_dev_suffix

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from pathlib import Path

    from ..config import Config


BREAKING_BULLET_RE = re.compile(
    r"^[ \t]*[-*][ \t]+(?P<bullet>\*\*(?:Breaking|Deprecated):\*\*.*)$",
    re.MULTILINE,
)
"""A release-notes bullet announcing a breaking change or a deprecation.

The changelog convention opens these bullets with a bold `**Breaking:**` or
`**Deprecated:**` label, and each GitHub release body repeats its changelog
section verbatim. The upgrade report lifts them above the full notes, because
they are the entries a repository may have to act on.
"""

INIT_WARNING_RE = re.compile(r"^[ \t]*warning:.*$", re.IGNORECASE | re.MULTILINE)
"""A warning line in the output of `repomatic init`.

Matches the `warning:` lines the configuration loader logs, like an unknown
`[tool.repomatic]` key, and the `Warning:` lines of the closing summary.
"""


def _base_version() -> str:
    """The running repomatic version with any PEP 440 `.devN` suffix stripped.

    `"5.10.0.dev0"` becomes `"5.10.0"`. The bare (no `v`) form, used both for the
    default pin and to age-check the running version against a release
    datasource.
    """
    return strip_dev_suffix(__version__)


def default_version_pin() -> str:
    """Derive the default version pin from `__version__`.

    Strips any `.dev0` suffix and prefixes with `v`. For example,
    `"5.10.0.dev0"` becomes `"v5.10.0"`.
    """
    return f"v{_base_version()}"


def pins_running_release(version: str) -> bool:
    """Whether the pin *version* names the release the running build heads for.

    Compared on the release tuple, so a development build matches the release
    it is heading for: the pin drops the `.devN` suffix the running version
    carries, and string equality would never match on a dev checkout.

    :param version: A pin, in its `vX.Y.Z` tag spelling.
    """
    pinned = parse_tag_version(version)
    running = safe_version(__version__)
    return (
        pinned is not None and running is not None and pinned.release == running.release
    )


def github_yaml_texts(root: Path) -> dict[Path, str]:
    """Read every workflow and composite-action file under *root*, once.

    :param root: Root of the repository to scan.
    :return: The text of each file
        {func}`~repomatic.github.workflow_files.github_yaml_paths` lists, keyed
        by path. A file that cannot be read is skipped.
    """
    texts: dict[Path, str] = {}
    for path in github_yaml_paths(root):
        try:
            texts[path] = path.read_text(encoding="UTF-8")
        except OSError as error:
            logging.warning(f"Could not read {path}: {error}")
    return texts


def highest_upstream_pin(texts: Iterable[str], repo: str) -> UpstreamRefPin | None:
    """The highest upstream `uses:` pin the given workflow texts carry.

    Read off the files {func}`github_yaml_texts` returns, the same files
    `sync-workflow-pins` bumps. Stragglers left behind at an older pin
    therefore converge upward onto the repository-wide maximum, matching how
    `sync-action-pins` treats a slug pinned at more than one version.

    Only *repo* is matched, not every slug in
    {data}`~repomatic.upstream.UPSTREAM_REPO_SLUGS`: a caller still naming a
    pre-rename slug is a tombstone `init` rewrites, not a pin to preserve.

    :param texts: The contents of the workflow and composite-action files.
    :param repo: Upstream `owner/repo` whose refs are read.
    :return: The highest pin found, or `None` for a repository carrying none.
    """
    best: UpstreamRefPin | None = None
    for content in texts:
        for pin in find_upstream_ref_pins(content, repo):
            if best is None:
                best = pin
                continue
            # At equal versions a SHA-pinned ref beats a bare tag one, so the
            # floor never unpins a hardened ref.
            hardens = pin.version == best.version and best.sha is None
            if hardens or is_newer(pin.version, best.version):
                best = pin
    return best


def _select_cooldown_pin(
    candidates: list[Candidate],
    base: str,
    min_age: timedelta,
    today: date,
) -> Candidate | None:
    """Pick the release `init` should pin when the cooldown holds `base` back.

    `init` normally pins the running repomatic version (`base`). When that
    version is still inside the `minimum-release-age` window (a fresh release a
    `--refresh` just pulled), pinning it would adopt an unproven cut, bypassing
    the cooldown every other version adopter honors. This returns the newest
    release *older* than `base` that has cleared the window, so the downstream
    pin steps back to a proven release instead.

    The step-back is skipped (returns `None`, meaning "keep pinning `base`")
    whenever `base` is safe or nothing better exists:

    - `base` is absent from *candidates* (an unreleased `.dev` cut, or the
      datasource is unavailable): fail open to `base`.
    - `base` has already cleared the cooldown: the common case, no change.
    - No cooldown-cleared release is strictly older than `base`: nothing to step
      back to.

    :param candidates: Releases offered by the datasource.
    :param base: The running repomatic version (bare, no `v` prefix).
    :param min_age: The `minimum-release-age` stabilization window.
    :param today: Reference date for the cooldown computation.
    :return: The {class}`~repomatic.deps.pins.Candidate` to pin instead of
        `base`, or `None` to keep pinning `base`.
    """
    cutoff = today - min_age
    base_date = next((c.date for c in candidates if c.version == base), None)
    if base_date is None:
        return None
    try:
        released = date.fromisoformat(base_date)
    except ValueError:
        return None
    # `base` is old enough: the overwhelmingly common case, pin it unchanged.
    if released <= cutoff:
        return None
    latest = select_latest(candidates, min_age, today)
    if latest is None or not is_newer(base, latest.version):
        return None
    return latest


def _note_cooldown(warnings: list[str] | None, note: str) -> None:
    """Log a cooldown decision and surface it in the `init` summary."""
    if warnings is not None:
        warnings.append(note)
    logging.warning(note)


def resolve_default_pin(
    config: Config,
    *,
    repo: str = DEFAULT_REPO,
    today: date | None = None,
    warnings: list[str] | None = None,
    floor: UpstreamRefPin | None = None,
) -> tuple[str, str | None]:
    """Resolve the upstream pin, holding a fresh release back by cooldown.

    Returns the `(version, commit_sha)` `init` stamps into thin-caller `uses:`
    refs. In the common case, and on any datasource failure, this is the running
    repomatic version paired with its build-time SHA. Only when adopting a
    release still inside the `[tool.repomatic] minimum-release-age` window does
    the pin step back to the newest cooldown-cleared release (see
    `_select_cooldown_pin`), resolving that tag's SHA afresh.

    ```{important}
    The cooldown may hold back an *adoption*. It may never rewrite a pin the
    repository already carries, which is what *floor* records.

    `init` is the only writer of these refs: `sync-action-pins` skips every slug
    in {data}`~repomatic.upstream.UPSTREAM_REPO_SLUGS`, and
    {data}`~repomatic.deps.pins.ACTION_PIN_RE` does not even match a
    subpath-carrying reusable-workflow ref. So a downstream repository adopts a
    new repomatic release one way: a newer `init` runs. A maintainer runs one by
    hand, and the CI `sync-repomatic` job runs one through `init --upgrade`,
    which picks the release itself (see {func}`resolve_upgrade_target`) and
    bypasses this function. Two things follow.

    A pin equal to the running version is not a decision to gate. Without a
    newer release to adopt, the CI `sync-repomatic` job runs `init` at the
    pinned version itself, so `base` equals *floor*; re-judging it there
    downgrades the repository once a week after each hand-bump.

    A pin below the running version is a skew. `init` renders caller *content*
    from the running version, so a ref naming an older release ships that
    content against an older reusable-workflow surface, which GitHub rejects as
    soon as the two disagree (see
    `test_thin_caller_workflow_call_inputs_stay_minimal`). Returning such a pin
    is therefore only half a decision: {func}`resolve_workflow_pin` reads it
    back and, when the repository already carries workflows, tells `init` to
    skip regenerating them so the tree stays coherent at the pin it keeps. A
    first-time adoption has no tree to keep, so there the skew stands as the
    only alternative to writing no workflows at all.
    ```

    :param config: Repomatic config supplying the `minimum-release-age` window.
    :param repo: Upstream `owner/repo` whose releases gate the pin.
    :param today: Reference date for the cooldown; defaults to the current UTC
        date.
    :param warnings: When provided, a cooldown note is appended here (in
        addition to being logged), so `init` can surface it in its final
        summary rather than only mid-run.
    :param floor: The highest upstream pin already committed downstream, from
        {func}`highest_upstream_pin`. `None` for a repository carrying none,
        the one case the cooldown may step back freely.
    :return: `(version_pin, commit_sha)`. `commit_sha` is `None` when no SHA can
        be resolved, leaving a bare tag pin.
    """
    base = _base_version()
    build_sha = __git_tag_sha__ or None
    # Unreleased dev cuts never appear in a release datasource: pin as-is,
    # sparing the source repo's own development builds a pointless lookup.
    if base != __version__:
        return f"v{base}", build_sha
    if floor is not None:
        # Regeneration, not adoption: the repository already pins this exact
        # version. Answered without a datasource round-trip, which is what
        # every CI sync does. Falls back to the on-disk SHA so a build carrying
        # no tag SHA of its own never unpins a hardened ref.
        if base == floor.version:
            return f"v{base}", build_sha or floor.sha
        # A deliberate rollback (`uvx repomatic==<older> init`). Honor it: pin
        # and content stay coherent, and holding the newer floor here would
        # make the rollback silently fail.
        if is_newer(floor.version, base):
            return f"v{base}", build_sha
    min_age = parse_min_age(config.minimum_release_age)
    if not min_age:
        return f"v{base}", build_sha
    if today is None:
        today = today_utc()
    repo_url = f"https://github.com/{repo}"
    stepped_back = _select_cooldown_pin(
        github_candidates(repo_url), base, min_age, today
    )
    if stepped_back is None:
        return f"v{base}", build_sha
    # The cleared release is no better than what the repository already runs:
    # decline the adoption instead of regressing to it.
    if floor is not None and not is_newer(stepped_back.version, floor.version):
        _note_cooldown(
            warnings,
            f"repomatic {base} is inside the {config.minimum_release_age} "
            f"minimum-release-age window; keeping the release this repository "
            f"already pins, v{floor.version}. Pass --no-cooldown to adopt "
            f"{base} now.",
        )
        return f"v{floor.version}", floor.sha
    _note_cooldown(
        warnings,
        f"repomatic {base} is inside the {config.minimum_release_age} "
        f"minimum-release-age window; pinning the newest cleared release "
        f"v{stepped_back.version} instead. Pass --no-cooldown to pin {base}.",
    )
    # Fall back to a bare tag pin when the SHA lookup fails.
    return f"v{stepped_back.version}", resolve_tag_to_sha(repo_url, stepped_back.ref)


@dataclass(frozen=True)
class WorkflowPin:
    """The upstream pin `init` writes into the thin callers it generates."""

    version: str
    """The pin, in its `vX.Y.Z` tag spelling."""

    commit_sha: str | None
    """The commit SHA paired with {attr}`version`, or `None` for a bare tag pin
    the next `sync-action-pins` run re-hardens."""

    adopts_content: bool = True
    """Whether the workflows take the content of the running release.

    `False` when the cooldown holds the pin below the running release on a tree
    that already carries workflows. `init` then leaves the workflows as they
    are, at the pin they already have.
    """


def resolve_workflow_pin(
    config: Config,
    *,
    version: str | None,
    cooldown: bool,
    repo: str,
    floor: UpstreamRefPin | None,
    warnings: list[str],
) -> WorkflowPin:
    """Resolve the pin `init` writes, and whether the workflows move with it.

    An explicit *version* keeps the build-time SHA and bypasses the cooldown.
    Without one, the pin derives from the running release, held back by
    {func}`resolve_default_pin` when *cooldown* is set.

    A pin the cooldown held below the running version cannot carry this
    version's caller content. `init` renders bodies from the running wheel and
    substitutes only the ref, so writing them beside an older `uses:` pin ships
    half an adoption: the trigger blocks, `concurrency` groups and `env:` of the
    new release, against the reusable-workflow surface of the old one.
    Declining the pin has to decline the content with it, or the cooldown buys
    nothing while the caller half lands anyway, silently.

    Only a repository that already carries workflows (a *floor*) has somewhere
    to stand: leaving them untouched keeps a coherent tree at *floor*. A
    first-time adoption has no such fallback, since skipping would write no
    workflows at all, and is the one case {func}`resolve_default_pin` documents
    the skew as unavoidable.

    :param config: Repomatic config supplying the `minimum-release-age` window.
    :param version: An explicit pin, in its `vX.Y.Z` tag spelling, or `None`.
    :param cooldown: Hold a fresh running release back by the cooldown.
    :param repo: Upstream `owner/repo` whose releases gate the pin.
    :param floor: The highest upstream pin already committed downstream.
    :param warnings: Cooldown notes are appended here, for the `init` summary.
    """
    commit_sha = __git_tag_sha__ or None
    if version is not None:
        return WorkflowPin(version, commit_sha)
    if not cooldown:
        return WorkflowPin(default_version_pin(), commit_sha)
    version, commit_sha = resolve_default_pin(
        config, repo=repo, warnings=warnings, floor=floor
    )
    if floor is None or not is_newer(_base_version(), version.removeprefix("v")):
        return WorkflowPin(version, commit_sha)
    _note_cooldown(
        warnings,
        f"Leaving the workflows at {version}: their content belongs to repomatic "
        f"{_base_version()}, which the pin above declines. Pass --no-cooldown to "
        "adopt both together.",
    )
    return WorkflowPin(version, commit_sha, adopts_content=False)


@dataclass(frozen=True)
class UpgradeTarget:
    """The release `init --upgrade` runs as, chosen before any file is written."""

    version: str
    """Bare version to run `init` as."""

    released: str
    """PyPI upload date of {attr}`version`, as `YYYY-MM-DD`. Empty when the
    datasource does not list the release pinned on disk."""

    previous: str | None = None
    """Bare version of the highest upstream pin on disk before the run, or `None`
    for a repository carrying none."""

    held_back: Candidate | None = None
    """The newest release above {attr}`version` that the `minimum-release-age`
    window still holds back: the one a later run adopts."""

    @property
    def in_process(self) -> bool:
        """Whether the running release is the target, so no hand-off is needed."""
        return self.version == __version__


def resolve_upgrade_target(
    config: Config,
    output_dir: Path,
    *,
    repo: str = DEFAULT_REPO,
    today: date | None = None,
) -> UpgradeTarget | None:
    """Pick the release `init --upgrade` runs as.

    That is the newest final, non-yanked release on PyPI older than the
    `[tool.repomatic] minimum-release-age` window, and never one below the
    upstream pin the repository already carries: a release pinned by hand inside
    the window stays. PyPI is the datasource because `uvx` installs from it, so
    a version whose publication failed, with a tag but no wheel, is never
    picked.

    :param config: Repomatic config supplying the window and both opt-outs.
    :param output_dir: Root of the target repository, read for its current pin.
    :param repo: Upstream `owner/repo`; its name is the PyPI package.
    :param today: Reference date for the cooldown; defaults to the current UTC
        date.
    :return: The target, or `None` when the upgrade does not apply: turned off
        by `upstream-pin.sync` or `workflow.sync`, run from a build that is not
        a release, or no release has cleared the window.
    """
    if not config.upstream_pin_sync:
        logging.info(
            "[tool.repomatic] upstream-pin.sync is disabled. Skipping the upgrade."
        )
        return None
    if not config.workflow.sync:
        logging.info(
            "[tool.repomatic] workflow.sync is disabled, so no upstream pin can"
            " move. Skipping the upgrade."
        )
        return None
    if is_source_repo(output_dir):
        logging.info(
            "The source repository runs from its lockfile. Skipping the upgrade."
        )
        return None
    if _base_version() != __version__:
        logging.info(
            f"repomatic {__version__} is a development build. Skipping the upgrade."
        )
        return None
    if today is None:
        today = today_utc()
    package = repo_name(repo)
    min_age = parse_min_age(config.minimum_release_age)
    candidates = pypi_candidates(package)
    chosen = select_latest(candidates, min_age, today)
    if chosen is None:
        logging.warning(
            f"No {package} release is older than the {config.minimum_release_age}"
            " minimum-release-age window. Skipping the upgrade."
        )
        return None
    floor = highest_upstream_pin(github_yaml_texts(output_dir).values(), repo)
    if floor is not None and is_newer(floor.version, chosen.version):
        # A release pinned by hand inside the window. The upgrade never moves a
        # pin backwards, so it regenerates the files at that release instead.
        chosen = next(
            (c for c in candidates if c.version == floor.version),
            Candidate(floor.version, "", floor.version),
        )
    return UpgradeTarget(
        version=chosen.version,
        released=chosen.date,
        previous=floor.version if floor is not None else None,
        held_back=select_held_back(candidates, chosen.version, min_age, today),
    )


def upgrade_command(
    version: str,
    init_args: Sequence[str],
    *,
    config: Config,
    repo: str = DEFAULT_REPO,
    today: date | None = None,
) -> list[str]:
    """Build the command running `init` as another `repomatic` release.

    `--no-cooldown` hands the child the decision {func}`resolve_upgrade_target`
    already took. Left to judge the window again, the child reads GitHub release
    dates where the parent read PyPI ones, and a split verdict near the limit
    writes the new configs but leaves the callers at the old release.

    The window reaches the resolution as `--exclude-newer`, which gates the
    child's dependency tree like any other install. The package itself carries
    the self-pin exemption, because it already cleared this repository's window:
    with that window disabled, no cutoff is passed, and the workflow's own
    `UV_EXCLUDE_NEWER` would refuse the release.

    :param version: Bare version to run.
    :param init_args: Arguments for the child `init`, after `--no-cooldown`.
    :param config: Repomatic config supplying the `minimum-release-age` window.
    :param repo: Upstream `owner/repo`; its name is the package and the command.
    :param today: Reference date for the cutoff; defaults to the current UTC
        date.
    :return: The command line.
    """
    if today is None:
        today = today_utc()
    package = repo_name(repo)
    cutoff = exclude_newer_cutoff(config.minimum_release_age, today)
    return [
        *uvx_cmd(exclude_newer=cutoff),
        *SELF_PIN_COOLDOWN_EXEMPTION.split(),
        "--from",
        f"{package}=={version}",
        package,
        "init",
        "--no-cooldown",
        *init_args,
    ]


def run_upgrade(
    command: Sequence[str], output_dir: Path
) -> subprocess.CompletedProcess[str]:
    """Run the `init` hand-off built by {func}`upgrade_command`.

    Runs from *output_dir*, where the child finds `[tool.repomatic]` as the
    parent did. Both output streams are merged in the order they were written,
    then captured, because {func}`upgrade_report` quotes the warnings the child
    printed: a config key the new release no longer knows shows up nowhere else.

    :param command: The command line from {func}`upgrade_command`.
    :param output_dir: Root of the target repository.
    :return: The finished process, its merged output in `stdout`.
    """
    logging.info(f"Running: {' '.join(command)}")
    return subprocess.run(
        command,
        cwd=output_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="UTF-8",
        check=False,
    )


def hand_off_upgrade(
    target: UpgradeTarget,
    config: Config,
    *,
    repo: str,
    output_dir: Path,
    flags: Sequence[str] = (),
) -> subprocess.CompletedProcess[str]:
    """Run `init` as the release *target* names, in a child process.

    The child gets the upstream repository and the output directory of this
    run, then *flags*: the deletion flags this run was given, so the child
    prunes what this run would have.

    :param target: The target {func}`resolve_upgrade_target` picked.
    :param config: Repomatic config supplying the `minimum-release-age` window.
    :param repo: Upstream `owner/repo`; its name is the package and the command.
    :param output_dir: Root of the target repository.
    :param flags: Extra `init` options for the child, like
        {meth}`repomatic.init.run.DeletionPolicy.flags` returns.
    :return: The finished process, its merged output in `stdout`.
    """
    command = upgrade_command(
        target.version,
        ["--upstream-repo", repo, "--output-dir", str(output_dir), *flags],
        config=config,
        repo=repo,
    )
    return run_upgrade(command, output_dir)


def _breaking_section(
    notes: Mapping[str, tuple[str, list[tuple[str, str]]]],
    previous: str,
    adopted: str,
) -> str:
    """List the breaking and deprecation bullets of the fetched release notes.

    Empty when no notes were fetched. Silence there means the notes could not
    be read, which must not render as a claim that nothing breaks.
    """
    if not notes:
        return ""
    heading = "## 💥 Breaking changes and deprecations"
    bullets = [
        f"- [`{tag}`]({repo_url}/releases/tag/{tag}): "
        f"{sanitize_markdown_mentions(match['bullet'].rstrip())}"
        for repo_url, versions in notes.values()
        for tag, body in versions
        for match in BREAKING_BULLET_RE.finditer(body)
    ]
    if not bullets:
        return (
            f"{heading}\n\nNo release between `v{previous}` and `v{adopted}` has"
            " a `**Breaking:**` or `**Deprecated:**` entry."
        )
    return "\n".join((heading, "", *bullets))


def _first_sentence(line: str) -> str:
    """Cut a log line after its first sentence, like a list of valid options."""
    head, separator, _rest = line.partition(". ")
    return f"{head}." if separator else line


def _init_output_section(output: str, adopted: str) -> str:
    """Show the warnings of a hand-off run, then its whole output, collapsed."""
    if not output.strip():
        return ""
    warnings = list(
        dict.fromkeys(
            _first_sentence(match.group(0).strip())
            for match in INIT_WARNING_RE.finditer(output)
        )
    )
    lines = ["## 📋 `repomatic init` output", ""]
    if warnings:
        lines += ["The run printed these warnings:", "", "```text", *warnings, "```"]
        lines.append("")
    lines += [
        "<details>",
        (
            "<summary>Full output of <code>repomatic init</code> at"
            f" <code>v{adopted}</code></summary>"
        ),
        "",
        "```text",
        output.strip(),
        "```",
        "",
        "</details>",
    ]
    return "\n".join(lines)


def upgrade_invite(previous: str, adopted: str) -> str:
    """Render the invitation to review a move of the upstream pin.

    Shared by every pull request that moves the pin, the `init --upgrade`
    report and a `sync-workflow-pins` lockstep bump, so both hand over the same
    launcher.

    :param previous: Bare version the pin moved from.
    :param adopted: Bare version the pin moved to.
    :return: The `upgrade-invite` section, ending on the skill launcher.
    """
    return render_template(
        "upgrade-invite",
        previous=f"v{previous}",
        adopted=f"v{adopted}",
        command=skill_launcher(UPGRADE_SKILL, f"v{previous}", f"v{adopted}"),
    )


def upgrade_report(
    target: UpgradeTarget,
    output_dir: Path,
    config: Config,
    *,
    init_output: str = "",
    repo: str = DEFAULT_REPO,
    today: date | None = None,
) -> tuple[str, str] | None:
    """Describe the upgrade a run just applied, for its pull request body.

    Reads the pin back from disk rather than trusting *target*, since what the
    run wrote is what the pull request carries. The sections, in reading order:

    - The version move, with its compare link and release date.
    - Every `**Breaking:**` and `**Deprecated:**` bullet of the releases in
      between.
    - The warnings the run printed, then its whole output, collapsed.
    - The release notes, one collapsed block per release.
    - The invitation to run the `repomatic-upgrade` review.
    - The newer release the window still holds back, if any.

    :param target: The target {func}`resolve_upgrade_target` picked.
    :param output_dir: Root of the target repository, read for the new pin.
    :param config: Repomatic config supplying the `minimum-release-age` window.
    :param init_output: The output of a hand-off run, empty for an in-process one.
    :param repo: Upstream `owner/repo` whose releases are described.
    :param today: Reference date for relative dates; defaults to the current
        UTC date.
    :return: `(adopted, markdown)`, or `None` when the run moved no pin forward.
    """
    pin = highest_upstream_pin(github_yaml_texts(output_dir).values(), repo)
    previous = target.previous
    if previous is None or pin is None or not is_newer(pin.version, previous):
        return None
    adopted = pin.version
    if today is None:
        today = today_utc()
    package = repo_name(repo)
    repo_url = f"https://github.com/{repo}"
    package_url = PYPI_PACKAGE_URL.format(package=package)
    min_age = parse_min_age(config.minimum_release_age)
    released = target.released if adopted == target.version else ""
    diff_table = format_diff_table(
        [(package, previous, adopted)],
        upload_times={package: released} if released else None,
        cooldown_note=(
            format_cooldown_note(config.minimum_release_age, today - min_age)
            if min_age
            else ""
        ),
        comparison_urls={package: f"{repo_url}/compare/v{previous}...v{adopted}"},
        reference_date=today,
        name_urls={package: package_url},
        heading="Upgraded release",
    )
    notes = fetch_github_release_notes([(package, repo_url, previous, adopted, None)])
    held_back = ""
    if target.held_back is not None:
        held_back = format_held_back_table(
            [
                build_held_back(
                    package,
                    adopted,
                    target.held_back.version,
                    target.held_back.date,
                    min_age,
                    today,
                )
            ],
            MIN_AGE_HELD_BACK_NOTE,
            name_urls={package: package_url},
        )
    sections = (
        diff_table,
        _breaking_section(notes, previous, adopted),
        _init_output_section(init_output, adopted),
        format_release_notes(notes),
        upgrade_invite(previous, adopted),
        held_back,
    )
    return adopted, "\n\n".join(section for section in sections if section)
