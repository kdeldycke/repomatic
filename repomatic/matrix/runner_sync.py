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
({mod}`repomatic.matrix.runners`), compared against the labels this repository
actually runs. Nothing else is read.

```{note} Why the table and not the announcement feed
The `Announcement`-labelled issues of `actions/runner-images` answer a
different question. The feed reports *what changed for anyone*; the table
reports *what is true for this repository*, and only the second decides
anything. A feed reader opens reports about images this repository either
already runs or never will.

Two things are given up, both deliberately. GitHub badges an image
`deprecated` when deprecation *begins* rather than when it is announced, so a
retirement surfaces here months later than the feed shows it: for Ubuntu
22.04, September rather than June. What remains is still ample, since the
badge lands well before the image stops working. And a change to the
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
from enum import StrEnum
from pathlib import Path

import tomlrt

from ..pyproject import load_pyproject_doc
from ..tabular import render_markdown_table
from . import axes

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from typing import Any, Final

    from .runners import RunnerCatalog

AXES_SOURCE_PATH: Final = Path(*axes.__name__.split(".")).with_suffix(".py")
"""Where the curated test axes live, relative to the root of a repomatic checkout.

Derived from the module name, so the path follows the module if it moves. Only a
checkout of `kdeldycke/repomatic` itself holds this file: a repository consuming
repomatic inherits the axes through its pin.
"""

RUNS_ON_RE_TEMPLATE = (
    r"(?P<prefix>^[ \t]*runs-on:[ \t]*)(?P<quote>['\"]?){label}(?P=quote)[ \t]*$"
)
"""A literal `runs-on:` naming one label, anchored to its own line.

Rewritten as raw text rather than through a YAML round-trip, for the reason
`repomatic.github.workflow_sync._extract_raw_job` gives: a round-trip
reformats the whole file, and a runner bump should read as a one-line diff.
The optional quote group is carried through so a quoted value stays quoted.
"""

AXIS_LABEL_RE_TEMPLATE = r'(?P<quote>["\']){label}(?P=quote)'
"""A runner label as a quoted string literal in the curated axes.

Rewritten as text for the same reason a `runs-on:` is: the axes are a hand-kept
tuple carrying comments and an ordering that says which runner is the fast one,
and rebuilding the module from an AST would discard both.
"""


class ChangeKind(StrEnum):
    """Why the table justifies moving a label."""

    RETIREMENT = "retirement"
    """The image is badged deprecated: jobs on it carry an end date."""

    UPGRADE = "upgrade"
    """A strictly newer version of the image exists."""

    @property
    def label(self) -> str:
        """The kind as a pull request table cell shows it."""
        return "🔴 retirement" if self is ChangeKind.RETIREMENT else "🆕 upgrade"

    @property
    def rank(self) -> int:
        """Sort position: a retirement carries a deadline, so it comes first."""
        return 0 if self is ChangeKind.RETIREMENT else 1


@dataclass(frozen=True)
class RunnerChange:
    """One runner-image edit the available-images table justifies."""

    kind: ChangeKind
    """Whether the current image is going away, or has a newer version."""

    label: str
    """Label this repository runs today."""

    successor: str
    """Label to move onto, or to probe."""

    locations: tuple[str, ...]
    """`file.yaml:job-id` entries whose literal `runs-on:` names {attr}`label`.

    Rewritten onto {attr}`successor` for either kind. A retirement moves them
    because the image is going away; an upgrade because nothing else ever will,
    a literal being the one `runs-on:` no probe can reach.
    """

    reason: str
    """Why the table says this change is warranted."""

    alternative: str
    """A newer preview passed over in favour of a released successor.

    Reported, never taken. Whether a fresher preview beats a released image is
    a capacity judgement, and the pull request exists to host exactly that.
    """

    probe: bool = False
    """Whether the test matrix gains a `continue-on-error` cell on {attr}`successor`.

    An upgrade sets this only when the fleet does not run the successor yet.
    Probing one it already runs writes an `unstable` entry marking *every* cell
    on that image `continue-on-error`, so a single job left behind would stop
    the current image from gating the build.
    """

    @property
    def summary(self) -> str:
        """One line naming the change, for a commit subject or a table row."""
        if self.kind is ChangeKind.UPGRADE and not self.locations:
            return f"probe `{self.successor}` alongside `{self.label}`"
        return f"`{self.label}` → `{self.successor}`"

    def apply(
        self, workflow_dir: Path, pyproject_path: Path, axes_path: Path | None = None
    ) -> list[Path]:
        """Make this change in the working tree.

        Both kinds move the jobs naming the image outright. A retirement also
        moves the curated axes, when *axes_path* names them. An upgrade also adds
        its matrix probe.

        :param workflow_dir: Directory holding the workflow files.
        :param pyproject_path: The project file an upgrade probe lands in.
        :param axes_path: The curated axes module, set only inside a checkout of
            repomatic itself (see {data}`AXES_SOURCE_PATH`).
        :return: The files actually rewritten.
        """
        touched = self.rewrite_literals(workflow_dir)
        if self.kind is ChangeKind.RETIREMENT:
            if axes_path is not None and self.rewrite_axes(axes_path):
                touched.append(axes_path)
        elif self.add_probe(pyproject_path):
            touched.append(pyproject_path)
        return touched

    def rewrite_literals(self, workflow_dir: Path) -> list[Path]:
        """Rewrite every literal `runs-on:` naming the label being moved off.

        Serves both kinds. A retirement rewrites because the image is going
        away; an upgrade rewrites because a literal is the one `runs-on:` a
        matrix probe cannot reach, so without this it sits on its image until a
        retirement forces the move, at the moment there is least runway to
        handle a break.

        ```{caution}
        An upgrade rewrite is a bet rather than an experiment: a job has one
        image, so there is no `continue-on-error` half to fall back on. The pull
        request is where that bet is taken or declined, and its own CI run is
        the evidence only for a job the pull request actually runs. A workflow
        triggered by `schedule` or `workflow_dispatch` alone shows nothing on
        the proposal, so read the diff for those rather than the checkmark.
        ```

        Idempotent: a file already on the successor matches nothing and is left
        untouched, so a re-run after a merge is a no-op rather than a second
        edit.

        :param workflow_dir: Directory holding the workflow files.
        :return: The files actually rewritten.
        """
        pattern = re.compile(
            RUNS_ON_RE_TEMPLATE.format(label=re.escape(self.label)), re.MULTILINE
        )
        touched = []
        for name in sorted({location.split(":")[0] for location in self.locations}):
            path = workflow_dir / name
            if not path.is_file():
                continue
            before = path.read_text(encoding="UTF-8")
            after = pattern.sub(
                lambda match: (
                    f"{match.group('prefix')}{match.group('quote')}"
                    f"{self.successor}{match.group('quote')}"
                ),
                before,
            )
            if after != before:
                path.write_text(after, encoding="UTF-8")
                touched.append(path)
        return touched

    def rewrite_axes(self, axes_path: Path) -> bool:
        """Move a retiring label forward in the curated test-matrix axes.

        Only meaningful inside `kdeldycke/repomatic`, where the axes live. A
        repository consuming repomatic inherits them through the pin, so its
        matrix moves when it adopts a release rather than when it edits
        anything.

        This is the highest-blast-radius edit the operation makes: every
        downstream repository picks these axes up at the next release. That is
        the argument for proposing it in a pull request whose own CI runs the
        full matrix on the new image, rather than for not proposing it.

        :param axes_path: Path to the curated axes module.
        :return: Whether the file was modified.
        """
        if not axes_path.is_file():
            return False
        pattern = re.compile(AXIS_LABEL_RE_TEMPLATE.format(label=re.escape(self.label)))
        before = axes_path.read_text(encoding="UTF-8")
        after = pattern.sub(
            lambda match: (
                f"{match.group('quote')}{self.successor}{match.group('quote')}"
            ),
            before,
        )
        if after == before:
            return False
        axes_path.write_text(after, encoding="UTF-8")
        return True

    def add_probe(self, pyproject_path: Path) -> bool:
        """Add the successor to the full test matrix as a failing-allowed probe.

        Writes two keys under `[tool.repomatic.test-matrix]`: the label joins
        the `os` axis through `variations`, and an `unstable` entry marks every
        cell carrying it `continue-on-error`. Both are needed and neither alone
        is useful: the variation without the unstable entry gates the build on
        an image nobody has vetted, and the unstable entry without the
        variation matches nothing.

        Idempotent: an image already probed is detected in both keys and
        nothing is written.

        :param pyproject_path: The project file to edit.
        :return: Whether the file was modified. A change with {attr}`probe`
            unset writes nothing: its successor is already in the fleet, so the
            `unstable` entry would mark cells that gate today
            `continue-on-error`.
        """
        if not self.probe or not pyproject_path.is_file():
            return False
        doc = load_pyproject_doc(pyproject_path)
        # Seeded through `Table` rather than a bare dict, which `setdefault`
        # would turn into an inline table.
        #
        # Either way `format-pyproject` normalizes what lands here into dotted
        # keys under `[tool.repomatic]` (`test-matrix.variations.os = [ … ]`),
        # and tomlrt cannot emit that form: assigning a dotted string produces
        # a *quoted* key holding dots, which is a different key. So the
        # formatter gets the last word on layout, and the invariant that
        # matters is not the shape written but that re-reading the formatter's
        # shape finds the label already there. It does, so the two converge
        # instead of ping-ponging, and `tests/test_runner_sync.py` pins that.
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
        # The *successor* is what joins the matrix: `label` is the image already
        # in use, which the probe runs beside rather than replaces.
        probed = self.successor
        if probed not in list(axis):
            axis.append(probed)
            changed = True
        if not any(dict(entry).get("os") == probed for entry in unstable):
            unstable.append({"os": probed})
            changed = True

        if changed:
            pyproject_path.write_text(tomlrt.dumps(doc), encoding="UTF-8")
        return changed


def plan_runner_changes(
    literal: Mapping[str, Sequence[str]],
    tracked: Iterable[str],
    catalog: RunnerCatalog,
    ignore: Iterable[str] = (),
) -> list[RunnerChange]:
    """Work out which runner-image edits the table justifies.

    Every label this repository runs is looked up in the table, and yields at
    most one change:

    - **Retirement.** The row is badged deprecated. Jobs naming the image
      outright move to the pick of
      {meth}`~repomatic.matrix.runners.RunnerCatalog.successor_for`.
    - **Upgrade.** A strictly newer *version* exists. Jobs naming the old image
      outright move onto it, and the matrix gains it as a `continue-on-error`
      probe, so nothing is bet on it there while the suite starts exercising
      it. The probe half is dropped when the fleet already runs the successor,
      per {attr}`RunnerChange.probe`; the rewrite half is what moves a job no
      probe can reach.

    A label absent from the table is skipped, with no change: the image is
    already gone, and `actionlint` fails the Lint workflow on an unknown label.

    Only literal `runs-on:` values are rewritten, for either kind: one built
    from an expression draws on a matrix axis, which is the axis owner's to
    move.

    Strictly newer by **version** is what separates an upgrade from a flavour.
    `Windows 11 Arm64 with Visual Studio 2026` sits at the same version as
    `Windows 11 Arm64`: a different toolchain, not a newer image, and proposing
    it as an upgrade would be wrong.

    :param literal: Labels named outright in workflows, mapped to their
        locations, as {func}`~repomatic.github.workflow_files.literal_runners`
        reports them.
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
    running = frozenset(tracked)
    changes: list[RunnerChange] = []

    for label in sorted(running - declined):
        current = catalog.image(label)

        if current is None:
            # Absent from the table: withdrawn. Deliberately not reported here,
            # because `actionlint` already fails the Lint workflow on an unknown
            # label, and does it for a matrix axis as well as a literal
            # `runs-on:`. Saying it again would duplicate a stronger check with
            # a weaker one.
            continue

        if current.deprecated:
            successor = catalog.successor_for(current)
            if successor is None:
                # A dying image whose family offers nothing at all. Rare enough
                # to be worth a loud log rather than a silent skip.
                logging.warning(f"{label!r} is deprecated with no replacement.")
                continue
            preview_alt = catalog.newer_preview_than(successor, current)
            changes.append(
                RunnerChange(
                    kind=ChangeKind.RETIREMENT,
                    label=label,
                    successor=successor.preferred_label,
                    locations=tuple(literal.get(label, ())),
                    reason=f"{current.display_name} is deprecated",
                    alternative=preview_alt.preferred_label if preview_alt else "",
                )
            )
            continue

        newer = catalog.newer_version_than(current)
        if not newer or newer.preferred_label in declined:
            continue
        # Already in the fleet leaves nothing to start exercising, and probing
        # it anyway is worse than redundant: the `unstable` entry marks *every*
        # cell on that image `continue-on-error`, so one job left behind would
        # stop the image this repository runs today from gating the build.
        # The literal rewrite below still applies, which is how that job stops
        # being left behind.
        locations = tuple(literal.get(label, ()))
        probe = not running.intersection(newer.labels)
        if not locations and not probe:
            continue
        changes.append(
            RunnerChange(
                kind=ChangeKind.UPGRADE,
                label=label,
                successor=newer.preferred_label,
                locations=locations,
                reason=f"{newer.display_name} supersedes {current.display_name}",
                alternative="",
                probe=probe,
            )
        )

    changes.sort(key=lambda change: (change.kind.rank, change.label))
    return changes


def render_change_table(changes: Sequence[RunnerChange]) -> str:
    """Render proposed changes as a Markdown table for a pull request body.

    Carries the reasoning rather than just the edit: the diff shows what moved,
    and what a reviewer cannot see there is why the table says it had to, which
    jobs are affected, and what was passed over.

    :param changes: Changes from {func}`plan_runner_changes`.
    :return: A GitHub-flavored Markdown table, newline-terminated.
    """
    rows = []
    for change in changes:
        halves = []
        if change.locations:
            halves.append(
                f"`{change.label}` → `{change.successor}` in "
                + ", ".join(f"`{loc}`" for loc in change.locations)
            )
        if change.probe:
            halves.append(
                f"`{change.successor}` joins the matrix as `continue-on-error`, "
                f"beside `{change.label}`"
            )
        # A retirement with neither half is still worth a row: the image is
        # going away, and the label lives in a matrix axis this repository
        # inherits rather than in any `runs-on:` it could rewrite.
        where = ", and ".join(halves) or f"`{change.label}` → `{change.successor}`"
        alternative = f"`{change.alternative}` (preview)" if change.alternative else "—"
        rows.append((change.kind.label, where, change.reason, alternative))
    table = render_markdown_table(
        ("Change", "What", "Why", "Passed over"),
        rows,
        align=("left",) * 4,
    )
    return table + "\n"
