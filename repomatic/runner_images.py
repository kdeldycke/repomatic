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

"""Watch GitHub's runner-image announcements and report them as an issue.

A `runs-on:` value is the one dependency in a workflow that nothing bumps:
Dependabot rewrites `uses:` references, `sync-workflow-pins` rewrites version
literals, and neither touches a runner image. So a new image ships and an old
one retires entirely outside this repository's view, on GitHub's schedule.

{func}`~repomatic.lint_repo.check_runner_images` covers the inward direction,
checking that every literal `runs-on:` names an image the curated axes in
{mod}`repomatic.matrix_axes` know about. This module covers the outward one:
what GitHub has announced about the images at stake.

Those two sets are not the same set, and the difference is the whole point.
{data}`~repomatic.lint_repo.KNOWN_RUNNERS` is what the project has *chosen*;
{func}`~repomatic.lint_repo.literal_runners` is what its workflows are *running*.
A job sitting on an image outside the axes is exactly the one nobody is
tracking, so watching the axes alone would warn about every image except the
neglected ones. The caller passes the union.

```{note} Why the announcement feed and not the README table
`actions/runner-images` publishes an `Announcement`-labelled issue for every
image that arrives, changes default tooling, or enters deprecation, and closes
it once the rollout lands. That makes the *open* set a self-limiting list of
what is currently in flight, with no state to keep on this side.

The alternative was scraping the "Available Images" table out of that
repository's `readme.md`. It carries the same facts, but only as a snapshot of
the current state: deriving *changes* from it means checking in a copy and
diffing, and the parse breaks whenever GitHub restyles the table. The
announcement feed is structured JSON reachable through `gh`, carries the dates
and the rationale a table cannot, and needs no checked-in snapshot.
```

```{caution}
Editing an issue body sends no GitHub notification: only a new issue or a new
comment does. So this reliably announces the *first* relevant announcement, and
thereafter keeps a current list that a maintainer has to look at. Treat it as a
standing dashboard rather than a pager.
```
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import tomlrt

from .github.gh import gh_api_json
from .github.issue import BOT_ISSUE_LABEL, manage_issue_lifecycle
from .github.pr_body import render_template, sanitize_markdown_mentions
from .runner_catalog import by_display_name, by_label, fetch_catalog, successor_for

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from typing import Any

    from .runner_catalog import RunnerImage

ANNOUNCEMENTS_REPO = "actions/runner-images"
"""Repository publishing the runner image announcements."""

ANNOUNCEMENT_LABEL = "Announcement"
"""Label GitHub puts on every runner-image announcement issue.

Capitalized upstream, and `gh` matches labels exactly, so the case matters.
"""

ISSUE_TITLE = "GitHub runner image announcements"
"""Title of the issue this module maintains.

{func}`~repomatic.github.issue.manage_issue_lifecycle` matches on the exact
title, so changing this string orphans the issue already open in every
repository rather than renaming it.
"""

DEPRECATION_MARKERS = (
    "deprecation",
    "deprecated",
    "deprecating",
    "retire",
    "unsupported",
)
"""Lowercased substrings marking an announcement as a retirement notice.

Each inflection is spelled out rather than folded into one truncated stem: the
stem was shorter, but a clipped word reads as a misspelling and `typos` fails
the build on it.

Matched against the **title only**. Bodies are long and discuss neighbouring
images, so scanning one misreads an arrival as a retirement: the announcement
introducing Ubuntu 26.04 uses the word "removal" once, deep in prose about
unrelated tooling. Titles state the event and nothing else ("is now available",
"will begin deprecation"), and classify every announcement open today
correctly.
"""

IMPACT_SECTION_RE = re.compile(
    r"^#+[ \t]*Possible impact[ \t]*$(?P<body>.*?)(?=^#+[ \t]|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)
"""The `### Possible impact` section of an announcement body.

The one section naming the labels an announcement *acts on*, as opposed to the
ones it merely mentions. Retirement notices spell it out there ("Workflows using
the ``macos-14``, ``macos-14-large``, ``macos-14-xlarge`` image labels will be
terminated with an error"), which is what makes precise matching possible.

Scoping to it is what keeps the match honest: the same body's migration advice
recommends the images to move *to* ("should be updated to ``ubuntu-24.04-arm``"),
so a whole-body scan reports a retirement as affecting the very image it tells
you to adopt. Should GitHub rename this heading the section stops matching and
nothing is flagged, which fails toward a missing warning rather than a wrong
one, the safer direction per this project's precision-first labelling rule.
"""

TARGET_DATE_SECTION_RE = re.compile(
    r"^#+[ \t]*Target date[ \t]*$(?P<body>.*?)(?=^#+[ \t]|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)
"""The `### Target date` section, carrying an announcement's deadline."""

TITLE_PLATFORM_RE = re.compile(r"^\[(?P<platform>[^\]]+)\]")
"""The `[Ubuntu]`/`[macOS]`/`[Windows]` prefix every announcement title carries."""

AFFECTED_SECTION_RE = re.compile(
    r"^#+[ \t]*Runner images affected[ \t]*$(?P<body>.*?)(?=^#+[ \t]|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)
"""The `### Runner images affected` checklist of an announcement body.

The one section an *arrival* fills in: a new-image notice ticks the image it
introduces here while naming no label anywhere in its prose. Scoped like
{data}`IMPACT_SECTION_RE`, so a checklist elsewhere in the body is never read.
"""

CHECKED_BOX_RE = re.compile(
    r"^[ \t]*[-*][ \t]+\[[xX]\][ \t]*(?P<name>.+?)[ \t]*$", re.MULTILINE
)
"""A ticked checklist entry, capturing the display name it names.

Ticked only. See {meth}`Announcement.checked_images` for why an unticked box is
not evidence of anything.
"""

BACKTICKED_TOKEN_RE = re.compile(r"`([A-Za-z][A-Za-z0-9.\-]*)`")
"""A backticked identifier in an announcement's markdown.

Runner labels appear verbatim and backticked, so pulling every backticked token
out of {data}`IMPACT_SECTION_RE` and intersecting with the images this project
has a stake in needs no fuzzy matching and cannot invent a label: anything the
caller did not name is discarded.
"""


@dataclass(frozen=True)
class Announcement:
    """One `Announcement`-labelled issue from `actions/runner-images`."""

    number: int
    """Issue number in the upstream repository."""

    title: str
    """Issue title, carrying a `[Ubuntu]`/`[macOS]`/`[Windows]` prefix."""

    url: str
    """Web URL of the upstream issue."""

    created_at: str
    """ISO 8601 creation timestamp, truncated to a date for display."""

    body: str
    """Issue body, searched for the runner labels an announcement affects."""

    @property
    def date(self) -> str:
        """The creation date, without the time component."""
        return self.created_at[:10]

    @property
    def target_date(self) -> str:
        """The `### Target date` section, collapsed to one line.

        Free text rather than a parsed date, and deliberately so: the section
        carries a single day for an arrival ("Thursday, June 11, 2026") and a
        pair for a retirement ("Deprecation: September 17th, 2026 / Retirement:
        April 17th, 2027"). Parsing either into a `date` would throw away the
        distinction that matters, and both are read by a human off a pull
        request body.
        """
        section = TARGET_DATE_SECTION_RE.search(self.body)
        if not section:
            return ""
        lines = [line.strip() for line in section.group("body").splitlines()]
        return " / ".join(line for line in lines if line)

    @property
    def platform(self) -> str:
        """Operating system family, from the title's `[Ubuntu]`-style prefix.

        The one identifier every announcement carries, whatever it is about and
        however its author filled the rest of the template. It is what makes a
        row scannable when the label columns come up empty.
        """
        match = TITLE_PLATFORM_RE.match(self.title)
        return match.group("platform") if match else "—"

    @property
    def is_deprecation(self) -> bool:
        """Whether this announces a retirement rather than an arrival.

        Read from the title alone, for the reason
        {data}`DEPRECATION_MARKERS` gives.
        """
        title = self.title.lower()
        return any(marker in title for marker in DEPRECATION_MARKERS)

    def affected_runners(self, known_runners: Iterable[str]) -> frozenset[str]:
        """Runner labels this announcement acts on that the project also uses.

        Drawn from the `Possible impact` section only, so an image named as a
        migration *destination* is never reported as affected. See
        {data}`IMPACT_SECTION_RE`.

        :param known_runners: The images this project tracks.
        :return: The intersection, empty when the announcement names no impact
            section or touches no image this project runs on.
        """
        return frozenset(self.concerned_labels() & set(known_runners))

    def checked_images(self) -> frozenset[str]:
        """Display names ticked under `Runner images affected`.

        Only *checked* boxes are read. An unchecked one carries no information:
        announcement 14225 announced a Windows image under a list holding six
        Ubuntu entries and no tick at all, the template left exactly as it
        shipped. Reading unchecked entries as "not affected" would take that
        untouched form as a statement.
        """
        section = AFFECTED_SECTION_RE.search(self.body)
        if not section:
            return frozenset()
        return frozenset(CHECKED_BOX_RE.findall(section.group("body")))

    def concerned_labels(
        self, catalog: Sequence[RunnerImage] | None = None
    ) -> set[str]:
        """Every runner label this announcement is about, whoever runs it.

        Two sources, unioned, because neither covers both announcement kinds:

        - **Backticked labels in `Possible impact`.** Precise, and what a
          retirement spells out ("Workflows using the `ubuntu-22.04`,
          `ubuntu-22.04-arm` image labels will be terminated").
        - **Ticked `Runner images affected` boxes**, resolved to labels through
          {mod}`repomatic.runner_catalog`. This is the only source an *arrival*
          has: a new-image notice names no label in its impact prose, so before
          this it produced nothing and a new image could never be reported.

        :param catalog: Parsed available-images table, fetched when omitted.
            Pass one when resolving several announcements, so the table is read
            once rather than per announcement.
        :return: Labels, empty when neither source yields any.
        """
        labels: set[str] = set()
        section = IMPACT_SECTION_RE.search(self.body)
        if section:
            labels.update(BACKTICKED_TOKEN_RE.findall(section.group("body")))

        ticked = self.checked_images()
        if ticked:
            images = by_display_name(
                fetch_catalog() if catalog is None else list(catalog)
            )
            for display_name in ticked:
                image = images.get(display_name)
                if image:
                    labels.update(image.labels)
                else:
                    # A name the table does not carry is a renamed row or a
                    # restyled table. Skipped rather than guessed at: the
                    # catalog docstring's fail-closed rule applies here too.
                    logging.debug(f"No catalog row for {display_name!r}.")
        return labels


def fetch_announcements(repo: str = ANNOUNCEMENTS_REPO) -> list[Announcement] | None:
    """Fetch the open runner-image announcements.

    :param repo: Repository to read announcements from.
    :return: The open announcements, newest first, or `None` when the API could
        not be read. A failure is deliberately indistinguishable from "could not
        run" so the caller reports a skip rather than closing the issue on a
        transient outage, which would otherwise read as "all clear".
    """
    payload = gh_api_json([
        "api",
        f"repos/{repo}/issues?labels={ANNOUNCEMENT_LABEL}&state=open&per_page=100",
    ])
    if payload is None:
        logging.warning(f"Could not read {repo} announcements.")
        return None
    if not isinstance(payload, list):
        logging.warning(f"Unexpected payload reading {repo} announcements.")
        return None

    announcements = []
    for item in payload:
        # The issues endpoint returns pull requests too; they are never
        # announcements.
        if not isinstance(item, dict) or "pull_request" in item:
            continue
        announcements.append(
            Announcement(
                number=item.get("number", 0),
                title=(item.get("title") or "").strip(),
                url=item.get("html_url") or "",
                created_at=item.get("created_at") or "",
                body=item.get("body") or "",
            )
        )
    announcements.sort(key=lambda a: a.created_at, reverse=True)
    return announcements


def render_announcement_rows(
    announcements: Sequence[Announcement],
    known_runners: Iterable[str],
    catalog: Sequence[RunnerImage] | None = None,
) -> str:
    """Render the announcements as a markdown table, most relevant first.

    Announcements naming an image this project runs on sort to the top and carry
    that image in the `Affects` column. The rest are kept rather than filtered:
    hiding an announcement because no label matched would silently drop the one
    whose wording changed, and the whole list is short enough to scan.

    :param announcements: The announcements to render.
    :param known_runners: The images this project tracks.
    :return: A GitHub-flavored markdown table.
    """
    known = frozenset(known_runners)
    # Read the table once for the whole batch rather than per announcement, and
    # take the caller's copy when it already has one: `manage_runner_images_issue`
    # needs the same catalog to decide whether to open the issue at all.
    if catalog is None:
        catalog = fetch_catalog()
    concerned = {item.number: item.concerned_labels(catalog) for item in announcements}

    # Newest first, then a stable re-sort floating the ones naming an image in
    # use to the top, so each group stays in date order.
    by_date = sorted(announcements, key=lambda a: a.created_at, reverse=True)
    ordered = sorted(by_date, key=lambda a: not (concerned[a.number] & known))

    rows = []
    for item in ordered:
        labels = concerned[item.number]
        affected = labels & known
        kind = "🔴 retirement" if item.is_deprecation else "🆕 new"
        cells = (
            item.date,
            item.platform,
            kind,
            ", ".join(f"`{label}`" for label in sorted(labels)) or "—",
            ", ".join(f"`{label}`" for label in sorted(affected)) or "—",
            f"[{sanitize_markdown_mentions(item.title)}]({item.url})",
        )
        rows.append(f"| {' | '.join(cells)} |")
    header = (
        "| Announced | Platform | Kind | Labels | Affects | Announcement |\n"
        "| :-------- | :------- | :--- | :----- | :------ | :----------- |"
    )
    return "\n".join([header, *rows])


def manage_runner_images_issue(known_runners: Iterable[str]) -> None:
    """Open, update, or close the runner image announcement issue.

    :param known_runners: The images this project has a stake in, normally the
        union of {data}`~repomatic.lint_repo.KNOWN_RUNNERS` and the labels
        {func}`~repomatic.lint_repo.literal_runners` finds in the workflows.
    """
    announcements = fetch_announcements()
    if announcements is None:
        # A failed read is not evidence that nothing is announced, so leave any
        # open issue exactly as it stands.
        logging.info("Skipping runner image issue: announcements unavailable.")
        return

    known = frozenset(known_runners)
    catalog = fetch_catalog()
    in_use = [a for a in announcements if a.concerned_labels(catalog) & known]
    logging.info(
        f"{len(announcements)} open announcements, "
        f"{len(in_use)} naming an image in use."
    )

    body = render_template(
        "runner-images-issue",
        announcement_table=render_announcement_rows(announcements, known, catalog),
        tracked_runners=", ".join(f"`{label}`" for label in sorted(known)),
    )

    # Gated on exposure, not on upstream activity. `actions/runner-images`
    # always has something in flight, so opening on `announcements` left every
    # repository holding a permanently-open issue about other people's images:
    # kdeldycke/plumage#542 listed six rows, every one of them irrelevant to it.
    # Gating here also turns the issue into the notification the module
    # docstring says it cannot be, because a *new* issue does notify where an
    # edited body does not: it now appears exactly when exposure is acquired.
    manage_issue_lifecycle(
        has_issues=bool(in_use),
        body=body,
        labels=[BOT_ISSUE_LABEL],
        title=ISSUE_TITLE,
        no_issues_comment="No open announcement names a runner image in use here.",
    )


@dataclass(frozen=True)
class RunnerChange:
    """One runner-image edit an announcement justifies.

    Carries the evidence as well as the edit, because the edit is the trivial
    half: what a reviewer needs is the deadline, the announcement, and which
    jobs move.
    """

    kind: str
    """`retirement` or `arrival`."""

    label: str
    """Label retiring, or arriving."""

    successor: str
    """Label to move onto. Empty for an arrival, which adds rather than moves."""

    locations: tuple[str, ...]
    """`file.yaml:job-id` entries naming {attr}`label`, for a retirement."""

    announcement_url: str
    """The announcement justifying this change."""

    announcement_title: str
    """The announcement's title, for a pull request body."""

    target_date: str
    """Deadline text, as {attr}`Announcement.target_date` collapses it."""

    @property
    def summary(self) -> str:
        """One line naming the change, for a commit subject or a table row."""
        if self.kind == "arrival":
            return f"probe `{self.label}`"
        return f"`{self.label}` → `{self.successor}`"


def plan_runner_changes(
    literal: Mapping[str, Sequence[str]],
    tracked: Iterable[str],
    announcements: Sequence[Announcement],
    catalog: Sequence[RunnerImage],
    ignore: Iterable[str] = (),
) -> list[RunnerChange]:
    """Work out which runner-image edits the open announcements justify.

    Two shapes, deliberately asymmetric:

    - **Retirement.** An image named in a literal `runs-on:` is retiring, so
      the jobs on it move to the successor
      {func}`~repomatic.runner_catalog.successor_for` picks. Only literals are
      reachable: a value built from an expression draws on a matrix axis, which
      is the axis owner's to move.
    - **Arrival.** A newly available image joins the test matrix as a
      `continue-on-error` probe rather than replacing anything. Nothing is
      migrated onto an image GitHub is still rolling out, but the suite starts
      exercising it immediately, which is what surfaces a dependency that
      breaks there while there is still runway to report it upstream.

    :param literal: Labels named outright in workflows, mapped to their
        locations, as {func}`~repomatic.lint_repo.literal_runners` reports them.
    :param tracked: Every image this repository has a stake in.
    :param announcements: Open announcements.
    :param catalog: Parsed available-images table.
    :param ignore: Labels the repository has declined. A `sync-*` job
        regenerates on every push, so without this a closed pull request
        reopens forever and the proposal becomes a nuisance rather than a
        service.
    :return: The changes to propose, retirements first.
    """
    if not catalog:
        # Fail closed: an unreadable table means no successor can be trusted.
        logging.info("Runner catalog unavailable: proposing no change.")
        return []

    declined = frozenset(ignore)
    known = frozenset(tracked)
    catalog_by_label = by_label(catalog)
    changes: list[RunnerChange] = []

    for item in announcements:
        labels = item.concerned_labels(catalog)
        for label in sorted(labels - declined):
            if item.is_deprecation and label in literal:
                successor = successor_for(label, catalog)
                if not successor:
                    # Nothing released to move onto. The issue reports it; a
                    # pull request has nothing to propose.
                    logging.info(f"No released successor for {label!r}.")
                    continue
                changes.append(
                    RunnerChange(
                        kind="retirement",
                        label=label,
                        successor=successor.preferred_label,
                        locations=tuple(literal[label]),
                        announcement_url=item.url,
                        announcement_title=item.title,
                        target_date=item.target_date,
                    )
                )
            elif not item.is_deprecation and label not in known:
                image = catalog_by_label.get(label)
                # Three conditions, each dropping a different false positive:
                #
                # - **Still in preview.** "Not deprecation" is not the same as
                #   "arrival": the Xcode-default-change notice concerns the GA
                #   macOS rows and would otherwise propose probing an image
                #   that has been generally available for months. A genuinely
                #   new image is the one GitHub is still rolling out.
                # - **The row's plain label.** A size variant is a paid larger
                #   runner, never something to adopt by default.
                # - **A family already in use.** A repository running no macOS
                #   job has no use for a macOS probe, and every probe cell costs
                #   runner minutes on a capped pool.
                if (
                    not image
                    or not image.preview
                    or image.preferred_label != label
                    or not _family_in_use(image, known, catalog_by_label)
                ):
                    continue
                changes.append(
                    RunnerChange(
                        kind="arrival",
                        label=label,
                        successor="",
                        locations=(),
                        announcement_url=item.url,
                        announcement_title=item.title,
                        target_date=item.target_date,
                    )
                )

    # Retirements first: one carries a deadline, the other an opportunity.
    changes.sort(key=lambda change: (change.kind != "retirement", change.label))
    return changes


def _family_in_use(
    image: RunnerImage,
    known: frozenset[str],
    catalog_by_label: Mapping[str, RunnerImage],
) -> bool:
    """Whether this repository already runs something in *image*'s family.

    Bounds the arrival case to platforms already carried. A repository running
    no macOS job has no use for a probe on a new macOS image, and every probe
    cell costs runner minutes on a capped pool.
    """
    return any(
        (other := catalog_by_label.get(label)) and other.family == image.family
        for label in known
    )


def render_change_table(changes: Sequence[RunnerChange]) -> str:
    """Render proposed changes as a Markdown table for a pull request body.

    Carries the evidence rather than just the edit: the diff already shows what
    moved, and what a reviewer cannot see there is the deadline, the
    announcement that set it, and the fact that an arrival is a probe rather
    than a migration.

    :param changes: Changes from {func}`plan_runner_changes`.
    :return: A GitHub-flavored Markdown table, newline-terminated.
    """
    rows = [
        "| Change | What | Deadline | Announcement |",
        "| :----- | :--- | :------- | :----------- |",
    ]
    for change in changes:
        kind = "🔴 retirement" if change.kind == "retirement" else "🆕 probe"
        where = (
            f"`{change.label}` → `{change.successor}` in "
            + ", ".join(f"`{location}`" for location in change.locations)
            if change.kind == "retirement"
            else f"`{change.label}` joins the matrix as `continue-on-error`"
        )
        title = sanitize_markdown_mentions(change.announcement_title)
        rows.append(
            f"| {kind} | {where} | {change.target_date or '—'} "
            f"| [{title}]({change.announcement_url}) |"
        )
    return "\n".join(rows) + "\n"


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
    # Seeded through `Table` rather than a bare dict: `setdefault` with a dict
    # writes an inline table, which `format-pyproject` then expands back into
    # sections. The two would rewrite each other on every push, which is the
    # generator/formatter ping-pong `claude.md` § Common maintenance pitfalls
    # warns about.
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
    if change.label not in list(axis):
        axis.append(change.label)
        changed = True
    if not any(dict(entry).get("os") == change.label for entry in unstable):
        unstable.append({"os": change.label})
        changed = True

    if changed:
        pyproject_path.write_text(tomlrt.dumps(doc), encoding="UTF-8")
    return changed
