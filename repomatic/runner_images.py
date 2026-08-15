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

"""Keep a repository's runner images current against what GitHub still offers.

A `runs-on:` value is the one dependency in a workflow that nothing bumps:
Dependabot rewrites `uses:` references, `sync-workflow-pins` rewrites version
literals, and neither touches a runner image. So an image retires on GitHub's
schedule, entirely outside this repository's view, and the first sign is a
failing build.

The source is the *Available Images* table
({mod}`repomatic.runner_catalog`), compared against the labels this repository
actually runs. Nothing else is read.

```{note} Why the table and not the announcement feed
This module previously polled the `Announcement`-labelled issues of
`actions/runner-images`, and the two questions turn out to be different ones.
The feed reports *what changed for anyone*; the table reports *what is true
for me*, and only the second decides anything. Polling produced an issue whose
every row was an image this repository either already ran or never would.

Two things are given up, both deliberately. GitHub badges an image
`deprecated` when deprecation *begins* rather than when it is announced, so a
retirement surfaces here months later than the feed would have shown it: for
Ubuntu 22.04, September rather than June. What remains is still ample, since
the badge lands well before the image stops working. And a change to the
*contents* of an image already in use, like a default toolchain moving, is
invisible in the table; the test suite is what catches those.
```

```{caution}
An unreadable or restyled table yields an empty catalog, and every caller here
reads that as "propose nothing" rather than "nothing exists". Failing closed
costs a cycle of not noticing; failing open would rewrite a `runs-on:` to an
image GitHub does not host, taking every job with it.
```
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import tomlrt

from .github.issue import close_issue, list_issues
from .runner_catalog import (
    by_label,
    newer_preview_than,
    newer_version_than,
    successor_for,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from typing import Any

    from .runner_catalog import RunnerImage

LEGACY_ISSUE_TITLE = "GitHub runner image announcements"
"""Title of the issue this module used to maintain, closed on sight.

Dropping the announcement feed stopped anything from managing that issue, and
an issue nothing manages never closes: every repository that ran the old
version would keep one open forever, listing announcements no longer read.
Closing it from here is the issue-shaped equivalent of a
{class}`~repomatic.registry.RemovedAsset` tombstone.
"""


@dataclass(frozen=True)
class RunnerChange:
    """One runner-image edit the available-images table justifies."""

    kind: str
    """`retirement` when the current image is going away, `upgrade` when a
    strictly newer version of it exists."""

    label: str
    """Label this repository runs today."""

    successor: str
    """Label to move onto, or to probe."""

    locations: tuple[str, ...]
    """`file.yaml:job-id` entries naming {attr}`label`, for a retirement."""

    reason: str
    """Why the table says this change is warranted."""

    alternative: str
    """A newer preview passed over in favour of a released successor.

    Reported, never taken. Whether a fresher preview beats a released image is
    a capacity judgement, and the pull request exists to host exactly that.
    """

    @property
    def summary(self) -> str:
        """One line naming the change, for a commit subject or a table row."""
        if self.kind == "upgrade":
            return f"probe `{self.successor}` alongside `{self.label}`"
        return f"`{self.label}` → `{self.successor}`"


def plan_runner_changes(
    literal: Mapping[str, Sequence[str]],
    tracked: Iterable[str],
    catalog: Sequence[RunnerImage],
    ignore: Iterable[str] = (),
) -> list[RunnerChange]:
    """Work out which runner-image edits the table justifies.

    Every label this repository runs is looked up in the table, and yields at
    most one change:

    - **Retirement.** The row is badged deprecated, or the label is absent from
      the table entirely, which means the image is already gone. Jobs naming it
      outright move to {func}`~repomatic.runner_catalog.successor_for`'s pick.
      Only literal `runs-on:` values are reachable: one built from an
      expression draws on a matrix axis, which is the axis owner's to move.
    - **Upgrade.** A strictly newer *version* exists. It joins the full matrix
      as a `continue-on-error` probe rather than replacing anything, so nothing
      is bet on it while the suite starts exercising it.

    Strictly newer by **version** is what separates an upgrade from a flavour.
    `Windows 11 Arm64 with Visual Studio 2026` sits at the same version as
    `Windows 11 Arm64`: a different toolchain, not a newer image, and proposing
    it as an upgrade would be wrong.

    :param literal: Labels named outright in workflows, mapped to their
        locations, as {func}`~repomatic.lint_repo.literal_runners` reports them.
    :param tracked: Every image this repository has a stake in.
    :param catalog: Parsed available-images table.
    :param ignore: Labels the repository has declined. A `sync-*` job
        regenerates on every run, so without this a closed pull request comes
        back and the proposal becomes a nuisance rather than a service.
    :return: The changes to propose, retirements first.
    """
    if not catalog:
        # Fail closed: an unreadable table means no successor can be trusted.
        logging.info("Runner catalog unavailable: proposing no change.")
        return []

    declined = frozenset(ignore)
    indexed = by_label(catalog)
    changes: list[RunnerChange] = []

    for label in sorted(frozenset(tracked) - declined):
        current = indexed.get(label)

        if current is None:
            # Absent from the table: withdrawn. Deliberately not reported here,
            # because `actionlint` already fails the Lint workflow on an unknown
            # label, and does it for a matrix axis as well as a literal
            # `runs-on:`. Saying it again would duplicate a stronger check with
            # a weaker one.
            continue

        if current.deprecated:
            successor = successor_for(label, catalog)
            if successor is None:
                # A dying image whose family offers nothing at all. Rare enough
                # to be worth a loud log rather than a silent skip.
                logging.warning(f"{label!r} is deprecated with no replacement.")
                continue
            preview_alt = newer_preview_than(successor, current, catalog)
            changes.append(
                RunnerChange(
                    kind="retirement",
                    label=label,
                    successor=successor.preferred_label,
                    locations=tuple(literal.get(label, ())),
                    reason=f"{current.display_name} is deprecated",
                    alternative=preview_alt.preferred_label if preview_alt else "",
                )
            )
            continue

        newer = newer_version_than(label, catalog)
        if newer and newer.preferred_label not in declined:
            changes.append(
                RunnerChange(
                    kind="upgrade",
                    label=label,
                    successor=newer.preferred_label,
                    locations=(),
                    reason=f"{newer.display_name} supersedes {current.display_name}",
                    alternative="",
                )
            )

    # Retirements first: one carries a deadline, the other an opportunity.
    changes.sort(key=lambda change: (change.kind != "retirement", change.label))
    return changes


def render_change_table(changes: Sequence[RunnerChange]) -> str:
    """Render proposed changes as a Markdown table for a pull request body.

    Carries the reasoning rather than just the edit: the diff shows what moved,
    and what a reviewer cannot see there is why the table says it had to, which
    jobs are affected, and what was passed over.

    :param changes: Changes from {func}`plan_runner_changes`.
    :return: A GitHub-flavored Markdown table, newline-terminated.
    """
    rows = [
        "| Change | What | Why | Passed over |",
        "| :----- | :--- | :-- | :---------- |",
    ]
    for change in changes:
        if change.kind == "retirement":
            kind = "🔴 retirement"
            where = f"`{change.label}` → `{change.successor}`"
            if change.locations:
                where += " in " + ", ".join(f"`{loc}`" for loc in change.locations)
        else:
            kind = "🆕 probe"
            where = (
                f"`{change.successor}` joins the matrix as `continue-on-error`, "
                f"beside `{change.label}`"
            )
        alternative = f"`{change.alternative}` (preview)" if change.alternative else "—"
        rows.append(f"| {kind} | {where} | {change.reason} | {alternative} |")
    return "\n".join(rows) + "\n"


def close_legacy_issue() -> None:
    """Close the announcement issue this module no longer maintains.

    Called on every run rather than once, because there is no "once" available:
    a downstream repository adopts a release whenever it adopts one, and the
    first run after that adoption is the only moment this can be noticed. The
    close is a no-op when no such issue is open.
    """
    comment = (
        "Runner images are now read from the [available-images table]"
        "(https://github.com/actions/runner-images#available-images) and"
        " compared against the labels this repository actually runs, so an"
        " announcement about an image it does not use no longer opens an issue."
        " `sync-runner-images` proposes what needs doing as a pull request"
        " instead."
    )
    for issue in list_issues(LEGACY_ISSUE_TITLE):
        if issue.get("state", "").upper() == "CLOSED":
            continue
        close_issue(issue["number"], comment)


RUNS_ON_RE_TEMPLATE = (
    r"(?P<prefix>^[ \t]*runs-on:[ \t]*)(?P<quote>['\"]?){label}(?P=quote)[ \t]*$"
)
"""A literal `runs-on:` naming one label, anchored to its own line.

Rewritten as raw text rather than through a YAML round-trip, for the reason
{func}`~repomatic.github.workflow_sync._extract_raw_job` gives: a round-trip
reformats the whole file, and a runner bump should read as a one-line diff.
The optional quote group is carried through so a quoted value stays quoted.
"""


def apply_retirement(change: RunnerChange, workflow_dir: Path) -> list[Path]:
    """Rewrite every literal `runs-on:` naming a retiring label.

    Idempotent: a file already on the successor matches nothing and is left
    untouched, so a re-run after a merge is a no-op rather than a second edit.

    :param change: A `retirement` change from {func}`plan_runner_changes`.
    :param workflow_dir: Directory holding the workflow files.
    :return: The files actually rewritten.
    """
    pattern = re.compile(
        RUNS_ON_RE_TEMPLATE.format(label=re.escape(change.label)), re.MULTILINE
    )
    touched = []
    for name in sorted({location.split(":")[0] for location in change.locations}):
        path = workflow_dir / name
        if not path.is_file():
            continue
        before = path.read_text(encoding="UTF-8")
        after = pattern.sub(
            lambda match: (
                f"{match.group('prefix')}{match.group('quote')}"
                f"{change.successor}{match.group('quote')}"
            ),
            before,
        )
        if after != before:
            path.write_text(after, encoding="UTF-8")
            touched.append(path)
    return touched


AXIS_LABEL_RE_TEMPLATE = r'(?P<quote>["\']){label}(?P=quote)'
"""A runner label as a quoted string literal in the curated axes.

Rewritten as text for the same reason a `runs-on:` is: the axes are a hand-kept
tuple carrying comments and an ordering that says which runner is the fast one,
and rebuilding the module from an AST would discard both.
"""


def apply_axes_retirement(change: RunnerChange, axes_path: Path) -> bool:
    """Move a retiring label forward in the curated test-matrix axes.

    Only meaningful inside `kdeldycke/repomatic`, where the axes live. A repo
    consuming repomatic inherits them through the pin, so its matrix moves when
    it adopts a release rather than when it edits anything.

    This is the highest-blast-radius edit the operation makes: every downstream
    repository picks these axes up at the next release. That is the argument
    for proposing it in a pull request whose own CI runs the full matrix on the
    new image, rather than for not proposing it.

    :param change: A `retirement` change from {func}`plan_runner_changes`.
    :param axes_path: Path to `matrix_axes.py`.
    :return: Whether the file was modified.
    """
    if not axes_path.is_file():
        return False
    pattern = re.compile(AXIS_LABEL_RE_TEMPLATE.format(label=re.escape(change.label)))
    before = axes_path.read_text(encoding="UTF-8")
    after = pattern.sub(
        lambda match: f"{match.group('quote')}{change.successor}{match.group('quote')}",
        before,
    )
    if after == before:
        return False
    axes_path.write_text(after, encoding="UTF-8")
    return True


def apply_arrival(change: RunnerChange, pyproject_path: Path) -> bool:
    """Add an arriving image to the full test matrix as a failing-allowed probe.

    Writes two keys under `[tool.repomatic.test-matrix]`: the label joins the
    `os` axis through `variations`, and an `unstable` entry marks every cell
    carrying it `continue-on-error`. Both are needed and neither alone is
    useful: the variation without the unstable entry gates the build on an
    image nobody has vetted, and the unstable entry without the variation
    matches nothing.

    Idempotent: an image already probed is detected in both keys and nothing is
    written.

    :param change: An `arrival` change from {func}`plan_runner_changes`.
    :param pyproject_path: The project file to edit.
    :return: Whether the file was modified.
    """
    if not pyproject_path.is_file():
        return False
    doc = tomlrt.loads(pyproject_path.read_text(encoding="UTF-8"))
    # Seeded through `Table` rather than a bare dict, which `setdefault` would
    # turn into an inline table.
    #
    # Either way `format-pyproject` normalizes what lands here into dotted keys
    # under `[tool.repomatic]` (`test-matrix.variations.os = [ … ]`), and tomlrt
    # cannot emit that form: assigning a dotted string produces a *quoted* key
    # holding dots, which is a different key. So the formatter gets the last
    # word on layout, and the invariant that matters is not the shape written
    # but that re-reading the formatter's shape finds the label already there.
    # It does, so the two converge instead of ping-ponging, per `claude.md`
    # § Common maintenance pitfalls. `tests/test_runner_sync.py` pins that.
    node: Any = doc
    for key in ("tool", "repomatic", "test-matrix"):
        if key not in node:
            node[key] = tomlrt.Table()
        node = node[key]
    matrix = node

    if "variations" not in matrix:
        matrix["variations"] = tomlrt.Table()
    variations = matrix["variations"]
    axis = variations.setdefault("os", [])
    unstable = matrix.setdefault("unstable", [])

    changed = False
    # The *successor* is what joins the matrix: `label` is the image already in
    # use, which the probe runs beside rather than replaces.
    probed = change.successor
    if probed not in list(axis):
        axis.append(probed)
        changed = True
    if not any(dict(entry).get("os") == probed for entry in unstable):
        unstable.append({"os": probed})
        changed = True

    if changed:
        pyproject_path.write_text(tomlrt.dumps(doc), encoding="UTF-8")
    return changed
