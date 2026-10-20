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

"""Checks of the version pins the workflows and the tool registry carry.

The inline upstream pins and their cooldown exemption, the `setup-uv` version
pin and the checksum that covers it, the registry tool pins still inside the
cooldown, and the click-extra version the man pages render with.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from packaging.utils import canonicalize_name
from packaging.version import Version

from ..checks import CheckResult
from ..components import DEFAULT_REPO
from ..deps.pins import (
    SETUP_UV_SLUG,
    cleared_cooldown,
    find_upstream_ref_versions,
    parse_min_age,
    pinned_release_date,
    self_pin_exemption_re,
    setup_uv_verified_versions,
    tool_candidates,
)
from ..deps.uv import LockFile, exemption_admits, freeze_cutoff_after
from ..github.urls import repo_name
from ..github.workflow_files import iter_steps
from ..humanize import format_countdown, utc_midnight
from ..release.prepare_release import SELF_PIN_COOLDOWN_EXEMPTION
from ..tooling.registry import TOOL_REGISTRY

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator

    from ..tooling.registry import ToolSpec
    from .context import LintContext


LOCK_FILE = Path("uv.lock")
"""The lockfile the `manpages` release job installs from."""

MANPAGES_RENDERER = canonicalize_name("click-extra")
"""The package whose `wrap` CLI the `manpages` release job renders through."""

MANPAGES_RENDERER_FLOOR = Version("9")
"""Lowest {data}`MANPAGES_RENDERER` the `manpages` release job can render with.

`9.0.0` moved the roff source from `wrap --man` to `wrap --help-format man`
and gave `--man` the paged manual instead, which writes no file. The job runs
in the consumer's own environment, so the floor is a property of that project's
lock rather than of repomatic's dependencies.
"""


def check_manpages_toolchain(ctx: LintContext) -> CheckResult:
    """Check that a project opting into man pages locks a click-extra that renders.

    The `manpages` job is release-only: it first runs on the commit that tags
    and publishes, where a failure costs the release its man-page asset for
    good, since a published release locks its asset list. Reading the floor off
    `uv.lock` on every ordinary push is what moves that answer forward of the
    one run nothing rehearses.

    `uv.lock` is the subject rather than the `pyproject.toml` floor because the
    job installs with `uv sync --frozen`: the lock is what it gets. A project
    with no lock, or one whose lock never mentions click-extra, is
    indeterminate rather than failing, the same way a PAT-gated check reports a
    scope it could not read.
    """
    script = ctx.config.manpages_script
    if not LOCK_FILE.is_file():
        return CheckResult(
            None,
            f"Man pages: `{LOCK_FILE}` is missing, so the click-extra version"
            f" the release job would render with is unknown.",
        )

    locked = {
        canonicalize_name(name): version
        for name, version in LockFile.load(LOCK_FILE).versions.items()
    }
    version = locked.get(MANPAGES_RENDERER)
    if version is None:
        return CheckResult(
            None,
            f"Man pages: `{LOCK_FILE}` locks no {MANPAGES_RENDERER}, so the"
            f" release job has nothing to render `{script}` with.",
        )

    if Version(version) < MANPAGES_RENDERER_FLOOR:
        return CheckResult(
            False,
            f"Man pages: {MANPAGES_RENDERER} `{version}` is locked, below"
            f" `{MANPAGES_RENDERER_FLOOR}`. The release job renders with"
            f" `wrap --help-format man`, which that release introduced, so it"
            f" fails on the commit that publishes. Re-lock {MANPAGES_RENDERER}.",
        )

    return CheckResult(
        True,
        f"Man pages: {MANPAGES_RENDERER} `{version}` renders `{script}`.",
    )


def check_inline_pins_match_upstream(ctx: LintContext) -> CheckResult:
    """Check inline upstream pins match the workflow `uses:` ref version.

    A workflow that pins the upstream toolkit in a `run:` shell command (like
    `uvx 'repomatic==1.2.3' show-metadata`) must keep that version in lockstep with
    the SHA-pinned `uses:` refs. `init` realigns the inline pin whenever it
    writes the refs, so the two drift apart only when a ref is edited by hand,
    until the next `sync-workflow-pins` run. When the stale version drops
    a symbol the newer refs rely on, the metadata job fails and a release can
    publish to PyPI yet never tag (the toolkit chicken-and-egg). Flag the
    drift so the lint fails before a release does.
    """
    package = repo_name(DEFAULT_REPO)
    if not ctx.workflow_texts:
        return CheckResult(None, f"Inline {package} pin check: skipped (no workflows).")

    pin_re = re.compile(rf"\b{re.escape(package)}==(?P<version>[0-9]+(?:\.[0-9]+)*)")

    upstream_versions: set[str] = set()
    pins_by_file: dict[str, set[str]] = {}
    for wf, content in ctx.workflow_texts.items():
        upstream_versions.update(find_upstream_ref_versions(content, DEFAULT_REPO))
        found = {m["version"] for m in pin_re.finditer(content)}
        if found:
            pins_by_file[wf.name] = found

    if not upstream_versions or not pins_by_file:
        return CheckResult(None, f"Inline {package} pin check: nothing to compare.")

    lagging = {
        name: versions
        for name, versions in pins_by_file.items()
        if not versions <= upstream_versions
    }
    expected = ", ".join(sorted(upstream_versions))
    if lagging:
        detail = "; ".join(
            f"{name} pins {', '.join(sorted(v))}" for name, v in sorted(lagging.items())
        )
        msg = f"Inline {package} pin lags the uses: ref version ({expected}): {detail}."
        return CheckResult(False, msg)
    return CheckResult(
        True, f"Inline {package} pins match the uses: refs ({expected})."
    )


def check_self_pin_cooldown_exemption(ctx: LintContext) -> CheckResult:
    """Check every inline upstream pin carries its cooldown exemption.

    A workflow pinning the upstream toolkit in a `run:` command
    (`uvx 'repomatic==1.2.3' show-metadata`) resolves under the workflow-wide
    `UV_EXCLUDE_NEWER`, and that pin moves in lockstep with the `uses:` refs, so
    it routinely names a release published hours ago. Without
    {data}`~repomatic.release.prepare_release.SELF_PIN_COOLDOWN_EXEMPTION` on the
    command line, `uvx` cannot resolve it at all. `uvx` reads no project
    configuration, so there is nowhere else the bypass could live.

    The failure is total rather than partial, which is why this is worth a
    dedicated check: the pin usually sits in the `metadata` job, every other job
    is `needs: metadata`, and the whole workflow reports failure while executing
    nothing. Downstream repos are the exposed ones. `tests/test_workflows.py`
    pins the canonical workflows, `sync-workflow-pins` splices a missing flag in
    on any run that also moves the version, and a repo already pinned at the
    newest release falls through both.

    Only flags a pin under a workflow that actually sets a cooldown: a repo
    without one has nothing to exempt.
    """
    package = repo_name(DEFAULT_REPO)
    if not ctx.workflow_texts:
        return CheckResult(
            None, f"{package} cooldown exemption: skipped (no workflows)."
        )

    exemption_re = self_pin_exemption_re(package)
    unexempt: list[str] = []
    pinned = 0
    for wf, content in ctx.workflow_texts.items():
        # A workflow with no cooldown resolves the pin fine as-is.
        if "UV_EXCLUDE_NEWER" not in content:
            continue
        for match in exemption_re.finditer(content):
            pinned += 1
            if SELF_PIN_COOLDOWN_EXEMPTION not in match.group("flags"):
                unexempt.append(wf.name)
                break

    if not pinned:
        return CheckResult(None, f"{package} cooldown exemption: no inline pin found.")
    if unexempt:
        files = ", ".join(sorted(set(unexempt)))
        return CheckResult(
            False,
            f"Inline {package} pin resolves under a cooldown without"
            f" `{SELF_PIN_COOLDOWN_EXEMPTION}`, so it cannot resolve once the pin"
            f" names a release younger than the window: {files}.",
        )
    return CheckResult(
        True, f"Inline {package} pins ({pinned}) all carry the cooldown exemption."
    )


def _setup_uv_steps(
    workflows: Mapping[Path, dict],
) -> Iterator[tuple[Path, str, str | None]]:
    """Yield `(path, ref, version)` for every `astral-sh/setup-uv` step.

    The one definition of what counts as a `setup-uv` step, shared by the two
    checks reading them so neither can vouch for a step the other skips.

    *ref* is whatever follows the `@` (a commit SHA in a repository whose pins
    `sync-action-pins` walks, a tag in one pinning by tag), and *version* is
    the `with: version:` input, `None` when the step declares none.

    :param workflows: Parsed workflow documents, keyed by path.
    """
    for path, _job_id, step in iter_steps(workflows):
        uses = str(step.get("uses", ""))
        if not uses.startswith(f"{SETUP_UV_SLUG}@"):
            continue
        inputs = step.get("with")
        version = inputs.get("version") if isinstance(inputs, dict) else None
        yield (
            path,
            uses.partition("@")[2],
            None if version is None else str(version),
        )


def check_setup_uv_version_pin(ctx: LintContext) -> CheckResult:
    """Check every `astral-sh/setup-uv` step pins the uv version it installs.

    `[tool.uv] required-version` is a floor for everyone; what a runner
    downloads is a separate question, and left to `setup-uv` the answer is "the
    newest release satisfying the floor", installed seconds after it lands.
    That makes the tool enforcing every cooldown the one tool without one, so
    each step carries `with: version: "X.Y.Z"` and `sync-workflow-pins` walks it
    forward once a uv release clears `minimum-release-age`.

    Steps naming two different versions in one repository are flagged too: the
    pin exists so every job resolves through the same uv, and a split fleet
    silently tests two.

    Reads the parsed workflow rather than its text: a `with:` input is a plain
    mapping, so the step a pin belongs to is a fact the parser already knows.
    Matching the raw text instead means bounding a step's block by hand, and a
    body running past its own step lets one pinned step vouch for every
    unpinned one above it.
    """
    if not ctx.workflows:
        return CheckResult(None, "setup-uv version pin: skipped (no workflows).")

    unpinned: list[str] = []
    versions: dict[str, set[str]] = {}
    steps = 0
    for path, _ref, version in _setup_uv_steps(ctx.workflows):
        steps += 1
        if version is None:
            unpinned.append(path.name)
            continue
        versions.setdefault(version, set()).add(path.name)

    if not steps:
        return CheckResult(None, "setup-uv version pin: no setup-uv step found.")
    if unpinned:
        files = ", ".join(sorted(set(unpinned)))
        return CheckResult(
            False,
            "astral-sh/setup-uv step installs whatever uv is newest, with no"
            ' `with: version: "X.Y.Z"` input, leaving the tool that enforces'
            f" every cooldown without one: {files}.",
        )
    if len(versions) > 1:
        detail = "; ".join(
            f"{version} in {', '.join(sorted(files))}"
            for version, files in sorted(versions.items())
        )
        return CheckResult(
            False, f"astral-sh/setup-uv steps pin more than one uv version: {detail}."
        )
    pinned_version = next(iter(versions))
    return CheckResult(
        True, f"astral-sh/setup-uv steps ({steps}) all pin uv {pinned_version}."
    )


def check_setup_uv_checksum_coverage(ctx: LintContext) -> CheckResult:
    """Check the pinned uv is one the pinned `setup-uv` can checksum-verify.

    {func}`check_setup_uv_version_pin` settles that CI resolves through a uv
    somebody chose. Whether the bytes it downloads are the bytes that version
    shipped is a second question, and `setup-uv` answers it with a pinned hash
    only for the versions listed in the checksum table its own release bundles.
    Anything else installs with no verification before `v10.1.0`, and against a
    hash fetched at run time from `v10.1.0` on (see
    {func}`~repomatic.deps.pins.setup_uv_verified_versions`). So a repository
    can hold two perfectly good pins that together pin no hash, which is what
    this reports.

    Advisory, and not fatal for the same reason the pin check is not: the
    download succeeds and the job runs, it just carries a cooldown where it
    could have carried a cooldown and a pinned hash. `sync-workflow-pins`
    repairs it by stepping the uv pin back onto the table, and a
    `sync-action-pins` bump lets the pin move forward again (see
    `repomatic.deps.sync._gate_uv_on_checksums`).

    :return: A `CheckResult`, indeterminate when the table cannot be read.
    """
    if not ctx.workflows:
        return CheckResult(None, "setup-uv checksum coverage: skipped (no workflows).")

    refs: set[str] = set()
    versions: set[str] = set()
    for _path, ref, version in _setup_uv_steps(ctx.workflows):
        refs.add(ref)
        if version is not None:
            versions.add(version)

    if not versions:
        return CheckResult(
            None, "setup-uv checksum coverage: skipped (no pinned uv version)."
        )

    verified = setup_uv_verified_versions(refs)
    if verified is None:
        return CheckResult(
            None, "setup-uv checksum coverage: skipped (checksum table unreadable)."
        )

    unverified = sorted(versions - verified)
    if unverified:
        return CheckResult(
            False,
            f"uv {', '.join(unverified)} carries no checksum in the pinned"
            f" {SETUP_UV_SLUG}, so no pinned hash verifies it in CI. Step uv"
            " back onto the table, as `sync-workflow-pins` does.",
        )
    return CheckResult(
        True,
        f"Pinned uv ({', '.join(sorted(versions))}) is checksum-verified by the"
        f" pinned {SETUP_UV_SLUG}.",
    )


def check_registry_pin_cooldown(
    ctx: LintContext,
    *,
    registry: Mapping[str, ToolSpec] | None = None,
    now: datetime | None = None,
) -> list[CheckResult]:
    """Check that a `repomatic run` tool pinned inside the cooldown can install.

    `sync-tool-versions` adopts a release only after it clears
    `minimum-release-age`, but a hand bump can pin a release still inside the
    window. The result depends on how `repomatic run` installs the tool, which
    {class}`~repomatic.tooling.registry.ToolBackend` records:

    - A virtualenv tool installs through uv commands that read the project's
      `[tool.uv] exclude-newer-package`. A dated entry there admits the pin.
      When no entry admits it, the warning names the entry to add. The entry
      covers one repository, so a downstream repository that adopts the release
      inside the window needs its own entry, and gets the same warning.
    - A `uvx` or npm tool resolves its pin under the cooldown and reads no
      entry. No exemption in the repository can admit the pin, so the warning
      gives the date the install starts to work.
    - A binary tool is not checked. Its download verifies the registry checksum
      and applies no cooldown, so a pin inside the window installs, and no entry
      can admit it or hold it back. Listing its releases would also cost GitHub
      API calls on every run.

    Advisory: the pins come with repomatic, and a downstream repository can act
    on the first case only. A tool whose datasource gives no date for its
    pinned version is reported as skipped, never as a failure.

    :param ctx: The context. Its `[tool.uv] exclude-newer-package` holds the
        entries that admit a pin, and its `minimum-release-age` the cooldown.
    :param registry: The tools to audit, keyed by name. The bundled tool
        registry when `None`.
    :param now: The current instant. Read from the clock when `None`.
    :return: One result per pin inside the window, one for the pins past it,
        and one that names the tools without a release date.
    """
    if registry is None:
        registry = TOOL_REGISTRY
    minimum_release_age = ctx.config.minimum_release_age
    min_age = parse_min_age(minimum_release_age)
    if not min_age:
        return [
            CheckResult(
                None,
                "Registry pin cooldown: skipped (minimum-release-age sets no"
                " cooldown).",
            )
        ]
    if now is None:
        now = datetime.now(UTC)
    today = now.date()

    uv_section = ctx.tool_table.get("uv")
    table = (
        uv_section.get("exclude-newer-package")
        if isinstance(uv_section, Mapping)
        else None
    )
    entries = (
        {canonicalize_name(name): value for name, value in table.items()}
        if isinstance(table, Mapping)
        else {}
    )

    results: list[CheckResult] = []
    cleared = 0
    undated: list[str] = []
    for name, spec in sorted(registry.items()):
        backend = spec.backend
        if not backend.enforces_cooldown:
            continue
        released = pinned_release_date(tool_candidates(spec), spec.version)
        if released is None:
            undated.append(name)
            continue
        if cleared_cooldown(released, today - min_age):
            cleared += 1
            continue
        clears = format_countdown(utc_midnight(released + min_age), utc_midnight(today))
        if not backend.reads_uv_exemptions:
            results.append(
                CheckResult(
                    False,
                    f"`repomatic run {name}` cannot install {spec.version},"
                    f" published {released}, until {clears}, when it clears the"
                    f" {minimum_release_age} cooldown: its install reads no"
                    " exclude-newer-package entry, so nothing admits it sooner.",
                )
            )
            continue
        entry = entries.get(canonicalize_name(spec.pypi_name))
        if entry is not None and exemption_admits(entry, released, now):
            results.append(
                CheckResult(
                    True,
                    f"{name} {spec.version}, published {released}, is inside the"
                    f" {minimum_release_age} cooldown until {clears}, and its"
                    " [tool.uv] exclude-newer-package entry admits it.",
                )
            )
        else:
            results.append(
                CheckResult(
                    False,
                    f"`repomatic run {name}` cannot install {spec.version},"
                    f" published {released}, until {clears}, when it clears the"
                    f" {minimum_release_age} cooldown: add"
                    f' `{spec.pypi_name} = "{freeze_cutoff_after(released)}"` to'
                    " [tool.uv] exclude-newer-package to admit it now.",
                )
            )

    if cleared:
        results.append(
            CheckResult(
                True,
                f"PyPI and npm tool pins ({cleared}) are past the"
                f" {minimum_release_age} cooldown.",
            )
        )
    if undated:
        results.append(
            CheckResult(
                None,
                f"Registry pin cooldown: skipped for {', '.join(undated)} (no"
                " release date for the pinned version).",
            )
        )
    return results
