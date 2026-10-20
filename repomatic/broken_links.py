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

"""Broken links detection and reporting.

Combines Lychee and Sphinx linkcheck results into a single "Broken links"
GitHub issue. Sphinx linkcheck parsing detects broken auto-generated links
(intersphinx, autodoc, type annotations) that Lychee cannot see because they
only exist in the rendered HTML output.

Each checker reports one {class}`LinkVerdict`. Only a run that completed can
clear the issue: a checker that did not finish says nothing about the links,
so it never closes an open issue.

Issue lifecycle management is delegated to {mod}`~repomatic.github.issue`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from itertools import groupby
from operator import attrgetter
from pathlib import Path

from .components import is_awesome_repo
from .github.issue import manage_issue_lifecycle
from .markdown import sanitize_markdown_mentions
from .metadata.core import Metadata
from .tabular import render_markdown_table
from .templating import render_template

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable


ISSUE_TITLE = "Broken links"
"""Issue title used for the combined broken links report."""

DOCUMENTATION_LABEL = "📚 documentation"
"""Label of the broken links issue, outside awesome lists."""

FIX_LINK_LABEL = "🩹 fix link"
"""Label of the broken links issue in an `awesome-*` repository."""

LYCHEE_BROKEN_LINKS_EXIT = 2
"""The one lychee exit code that reports on the links rather than on the run.

Lychee exits 0 on success, 1 on an unexpected failure, 2 when it found broken
links, and 3 on a config error. Only 2 is a verdict about the links; 1 and 3 say
the run itself did not complete, so neither "broken links found" nor "no broken
links" can be claimed from them.
"""

LYCHEE_DEFAULT_BODY = Path("./lychee/out.md")
"""Report path the lychee step of the `docs.yaml` workflow passes to `--output`."""

SPHINX_DEFAULT_OUTPUT = Path("./docs/_linkcheck/output.json")
"""Default Sphinx linkcheck output path produced by the `docs.yaml` workflow."""

NO_BROKEN_LINKS = "No broken links found."
"""The section body of a checker that completed and found nothing broken."""


class LinkVerdict(StrEnum):
    """What one checker's run says about the links it covers."""

    BROKEN = "broken"
    """The run completed and found broken links."""

    CLEAN = "clean"
    """The run completed and found no broken link."""

    UNKNOWN = "unknown"
    """The run did not complete, or left no report: nothing can be claimed."""


@dataclass(frozen=True)
class LinkcheckResult:
    """A single result entry from Sphinx linkcheck `output.json`.

    Each line in the JSON-lines file corresponds to one checked URI.
    """

    filename: str
    """Source file the link was found in, relative to the docs root."""

    lineno: int
    """Line of the link in that file."""

    status: str
    """Linkcheck status, like `working`, `broken` or `timeout`."""

    uri: str
    """The checked URI."""

    info: str
    """What linkcheck says about the result, like the HTTP error."""


def parse_output_json(output_json: Path) -> list[LinkcheckResult]:
    """Parse the Sphinx linkcheck `output.json` file.

    The file uses JSON-lines format: one JSON object per line.
    Blank lines are skipped.

    :param output_json: Path to the `output.json` file.
    :return: List of parsed linkcheck results.
    """
    results: list[LinkcheckResult] = []
    content = output_json.read_text(encoding="UTF-8")
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        entry = json.loads(stripped)
        results.append(
            LinkcheckResult(
                filename=entry["filename"],
                lineno=entry["lineno"],
                status=entry["status"],
                uri=entry["uri"],
                info=entry.get("info", ""),
            )
        )
    logging.info(f"Parsed {len(results)} linkcheck entries from {output_json}")
    return results


def filter_broken(results: Iterable[LinkcheckResult]) -> list[LinkcheckResult]:
    """Filter results to only broken and timed-out links.

    :param results: Iterable of linkcheck results.
    :return: List of results with `status` of `"broken"` or `"timeout"`.
    """
    broken = [r for r in results if r.status in ("broken", "timeout")]
    logging.info(f"Found {len(broken)} broken/timed-out links")
    return broken


def generate_markdown_report(
    broken: list[LinkcheckResult],
    source_url: str | None = None,
) -> str:
    """Generate a Markdown report of broken links grouped by source file.

    The report starts with H2 file headings, suitable for embedding as a
    section in the combined broken links issue body.

    :param broken: List of broken linkcheck results.
    :param source_url: Base URL for linking filenames and line numbers. When
        provided, file headers become clickable links and line numbers deep-link
        to the specific line.
    :return: Markdown-formatted report string.
    """
    if not broken:
        return ""

    lines: list[str] = []

    # Group by filename, sorted alphabetically.
    sorted_results = sorted(broken, key=attrgetter("filename", "lineno"))
    for filename, group_iter in groupby(sorted_results, key=attrgetter("filename")):
        if source_url:
            lines.append(f"## [`{filename}`]({source_url}/{filename})\n")
        else:
            lines.append(f"## `{filename}`\n")

        table_rows = []
        for result in group_iter:
            # Escape pipe characters in info to avoid breaking the table.
            escaped_info = result.info.replace("|", "\\|")
            if source_url:
                file_url = f"{source_url}/{result.filename}"
                line_cell = f"[{result.lineno}]({file_url}?plain=1#L{result.lineno})"
            else:
                line_cell = str(result.lineno)
            table_rows.append((line_cell, result.uri, escaped_info))
        lines.extend((
            render_markdown_table(
                ("Line", "URI", "Info"),
                table_rows,
                align=("right", "left", "left"),
            ),
            "",
        ))

    return "\n".join(lines)


def get_label(repo_name: str) -> str:
    """Return the label the broken links issue carries in a repository.

    :param repo_name: The repository name.
    :return: {data}`FIX_LINK_LABEL` for `awesome-*` repos, else
        {data}`DOCUMENTATION_LABEL`.
    """
    return FIX_LINK_LABEL if is_awesome_repo(repo_name) else DOCUMENTATION_LABEL


def lychee_section(exit_code: int, body_file: Path | None) -> tuple[str, LinkVerdict]:
    """Render the Lychee section of the issue body, with its verdict.

    Only {data}`LYCHEE_BROKEN_LINKS_EXIT` reports on the links themselves, and
    only with a report to show: a broken-links verdict with nothing to show
    would file an issue whose body claims the links are fine. Every other
    non-zero code reports that the run failed.

    :param exit_code: Exit code of the lychee run.
    :param body_file: The report lychee wrote, `None` when it wrote none.
    :return: The section markdown, and what it says about the links.
    """
    if exit_code == 0:
        return f"## Lychee\n\n{NO_BROKEN_LINKS}", LinkVerdict.CLEAN
    if exit_code == LYCHEE_BROKEN_LINKS_EXIT and body_file is not None:
        report = sanitize_markdown_mentions(body_file.read_text(encoding="UTF-8"))
        return f"## Lychee\n\n{report.strip()}", LinkVerdict.BROKEN
    if exit_code == LYCHEE_BROKEN_LINKS_EXIT:
        logging.warning(
            f"Lychee exit code {exit_code} but no output file found: the broken"
            " links it found cannot be reported."
        )
        content = "Lychee found broken links but wrote no report this run."
    else:
        logging.warning(
            f"Lychee failed with exit code {exit_code}, which reports a run"
            " failure rather than broken links."
        )
        content = (
            f"Lychee did not complete (exit code {exit_code}), so the links it"
            " covers were not checked this run."
        )
    return f"## Lychee\n\n{content}", LinkVerdict.UNKNOWN


def sphinx_section(
    output_json: Path, source_url: str | None = None
) -> tuple[str, LinkVerdict]:
    """Render the Sphinx linkcheck section of the issue body, with its verdict.

    :param output_json: Path to Sphinx linkcheck `output.json`.
    :param source_url: Base URL for linking filenames and line numbers.
    :return: The section markdown, and what it says about the links.
    """
    broken = filter_broken(parse_output_json(output_json))
    if not broken:
        logging.info("No broken documentation links found.")
        return f"## Sphinx linkcheck\n\n{NO_BROKEN_LINKS}", LinkVerdict.CLEAN
    report = sanitize_markdown_mentions(
        generate_markdown_report(broken, source_url=source_url)
    )
    return f"## Sphinx linkcheck\n\n{report.strip()}", LinkVerdict.BROKEN


def manage_combined_broken_links_issue(
    repo_name: str | None = None,
    lychee_exit_code: int | None = None,
    lychee_body_file: Path | None = None,
    sphinx_output_json: Path | None = None,
    sphinx_source_url: str | None = None,
) -> LinkVerdict | None:
    """Manage the combined broken links issue lifecycle.

    Combines results from Lychee and Sphinx linkcheck into a single "Broken
    links" issue. Each tool's results appear under its own heading. Tools that
    were not run are omitted from the report.

    The issue is filed or updated when any checker found broken links, and
    closed when every checker that ran completed clean. When no checker found
    broken links but one did not complete, the issue is left as it is: the
    links that run covers might still be broken.

    When running in GitHub Actions, most parameters are auto-detected from
    {class}`~repomatic.metadata.core.Metadata` and well-known file paths:

    - `repo_name` defaults to {attr}`Metadata.repo_name
      <repomatic.metadata.env.EnvironmentMetadata.repo_name>`.
    - `lychee_body_file` defaults to {data}`LYCHEE_DEFAULT_BODY` when
      `lychee_exit_code` is provided and the file exists.
    - `sphinx_output_json` defaults to {data}`SPHINX_DEFAULT_OUTPUT` when the
      file exists.
    - `sphinx_source_url` is composed from {attr}`Metadata.repo_url
      <repomatic.metadata.env.EnvironmentMetadata.repo_url>` and
      {attr}`Metadata.sha <repomatic.metadata.env.EnvironmentMetadata.sha>`.

    :param repo_name: Repository name, for label selection.
    :param lychee_exit_code: Exit code from lychee, `None` if lychee was not
        run. See {data}`LYCHEE_BROKEN_LINKS_EXIT`.
    :param lychee_body_file: Path to the lychee output file.
    :param sphinx_output_json: Path to Sphinx linkcheck `output.json`.
    :param sphinx_source_url: Base URL for linking filenames and line numbers
        in the Sphinx report.
    :return: The combined verdict, or `None` when no checker ran.
    :raises ValueError: If `repo_name` cannot be determined.
    """
    md = Metadata()

    if repo_name is None:
        repo_name = md.repo_name
        if repo_name:
            logging.info(f"Auto-detected repo_name={repo_name!r} from CI context.")
    if not repo_name:
        msg = "No repository name specified. Set --repo-name or $GITHUB_REPOSITORY."
        raise ValueError(msg)

    if (
        lychee_exit_code is not None
        and lychee_body_file is None
        and LYCHEE_DEFAULT_BODY.exists()
    ):
        lychee_body_file = LYCHEE_DEFAULT_BODY.resolve()
        logging.info(f"Auto-detected lychee body file: {lychee_body_file}")

    if sphinx_output_json is None and SPHINX_DEFAULT_OUTPUT.exists():
        sphinx_output_json = SPHINX_DEFAULT_OUTPUT.resolve()
        logging.info(f"Auto-detected Sphinx output: {sphinx_output_json}")

    if (
        sphinx_output_json is not None
        and sphinx_source_url is None
        and md.repo_url
        and md.sha
    ):
        sphinx_source_url = f"{md.repo_url}/blob/{md.sha}/docs"
        logging.info(f"Auto-composed source URL: {sphinx_source_url}")

    sections: dict[str, str] = {"lychee_section": "", "sphinx_section": ""}
    verdicts: list[LinkVerdict] = []
    if lychee_exit_code is not None:
        sections["lychee_section"], verdict = lychee_section(
            lychee_exit_code, lychee_body_file
        )
        verdicts.append(verdict)
    if sphinx_output_json is not None:
        sections["sphinx_section"], verdict = sphinx_section(
            sphinx_output_json, sphinx_source_url
        )
        verdicts.append(verdict)

    if LinkVerdict.BROKEN in verdicts:
        overall = LinkVerdict.BROKEN
    elif LinkVerdict.UNKNOWN in verdicts:
        logging.warning(
            "A link checker did not complete and none found broken links: the"
            " broken links issue is left as it is."
        )
        return LinkVerdict.UNKNOWN
    else:
        overall = LinkVerdict.CLEAN

    manage_issue_lifecycle(
        has_issues=overall is LinkVerdict.BROKEN,
        body=render_template("broken-links-issue", **sections),
        labels=[get_label(repo_name)],
        title=ISSUE_TITLE,
    )
    return overall if verdicts else None
