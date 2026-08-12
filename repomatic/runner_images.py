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
what GitHub has announced about those images. The two share
{data}`~repomatic.lint_repo.KNOWN_RUNNERS` as their single notion of "an image
this project tracks".

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

from .github.gh import gh_api_json
from .github.issue import BOT_ISSUE_LABEL, manage_issue_lifecycle
from .github.pr_body import render_template, sanitize_markdown_mentions

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

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

BACKTICKED_TOKEN_RE = re.compile(r"`([A-Za-z][A-Za-z0-9.\-]*)`")
"""A backticked identifier in an announcement's markdown.

Runner labels appear verbatim and backticked, so pulling every backticked token
out of {data}`IMPACT_SECTION_RE` and intersecting with the images this project
uses needs no fuzzy matching and cannot invent a label: anything not already in
{data}`~repomatic.lint_repo.KNOWN_RUNNERS` is discarded.
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
        section = IMPACT_SECTION_RE.search(self.body)
        if not section:
            return frozenset()
        mentioned = set(BACKTICKED_TOKEN_RE.findall(section.group("body")))
        return frozenset(mentioned & set(known_runners))


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
    # Newest first, then a stable re-sort floating the ones naming an image in
    # use to the top, so each group stays in date order.
    by_date = sorted(announcements, key=lambda a: a.created_at, reverse=True)
    ordered = sorted(by_date, key=lambda a: not a.affected_runners(known))

    rows = []
    for item in ordered:
        affected = item.affected_runners(known)
        kind = "🔴 retirement" if item.is_deprecation else "🆕 new"
        affects = ", ".join(f"`{label}`" for label in sorted(affected)) or "—"
        title = sanitize_markdown_mentions(item.title)
        rows.append(f"| {item.date} | {kind} | {affects} | [{title}]({item.url}) |")
    header = (
        "| Announced | Kind | Affects | Announcement |\n"
        "| :-------- | :--- | :------ | :----------- |"
    )
    return "\n".join([header, *rows])


def manage_runner_images_issue(known_runners: Iterable[str]) -> None:
    """Open, update, or close the runner image announcement issue.

    :param known_runners: The images this project tracks, normally
        {data}`~repomatic.lint_repo.KNOWN_RUNNERS`.
    """
    announcements = fetch_announcements()
    if announcements is None:
        # A failed read is not evidence that nothing is announced, so leave any
        # open issue exactly as it stands.
        logging.info("Skipping runner image issue: announcements unavailable.")
        return

    known = frozenset(known_runners)
    in_use = [a for a in announcements if a.affected_runners(known)]
    logging.info(
        f"{len(announcements)} open announcements, "
        f"{len(in_use)} naming an image in use."
    )

    body = render_template(
        "runner-images-issue",
        announcement_table=render_announcement_rows(announcements, known),
        tracked_runners=", ".join(f"`{label}`" for label in sorted(known)),
    )

    manage_issue_lifecycle(
        has_issues=bool(announcements),
        body=body,
        labels=[BOT_ISSUE_LABEL],
        title=ISSUE_TITLE,
        no_issues_comment="No open runner image announcements.",
    )
