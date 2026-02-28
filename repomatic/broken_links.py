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

Issue lifecycle management is delegated to :mod:`~repomatic.issue`.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from itertools import groupby
from operator import attrgetter
from pathlib import Path

from click_extra import TableFormat, render_table

from .github.issue import manage_issue_lifecycle
from .github.pr_body import render_template

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable


ISSUE_TITLE = "Broken links"
"""Issue title used for the combined broken links report."""

LYCHEE_DEFAULT_BODY = Path("./lychee/out.md")
"""Default output path used by the lychee-action GitHub Action."""

SPHINX_DEFAULT_OUTPUT = Path("./docs/linkcheck/output.json")
"""Default Sphinx linkcheck output path produced by the ``docs.yaml`` workflow."""


# ---------------------------------------------------------------------------
# Sphinx linkcheck parsing, filtering, and report generation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LinkcheckResult:
    """A single result entry from Sphinx linkcheck ``output.json``.

    Each line in the JSON-lines file corresponds to one checked URI.
    """

    filename: str
    lineno: int
    status: str
    code: int
    uri: str
    info: str


def parse_output_json(output_json: Path) -> list[LinkcheckResult]:
    """Parse the Sphinx linkcheck ``output.json`` file.

    The file uses JSON-lines format: one JSON object per line.
    Blank lines are skipped.

    :param output_json: Path to the ``output.json`` file.
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
                code=entry["code"],
                uri=entry["uri"],
                info=entry.get("info", ""),
            )
        )
    logging.info(f"Parsed {len(results)} linkcheck entries from {output_json}")
    return results


def filter_broken(results: Iterable[LinkcheckResult]) -> list[LinkcheckResult]:
    """Filter results to only broken and timed-out links.

    :param results: Iterable of linkcheck results.
    :return: List of results with ``status`` of ``"broken"`` or ``"timeout"``.
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
        group_list = list(group_iter)

        if source_url:
            file_url = f"{source_url}/{filename}"
            lines.append(f"## [`{filename}`]({file_url})\n")
        else:
            lines.append(f"## `{filename}`\n")

        table_rows = []
        for result in group_list:
            # Escape pipe characters in info to avoid breaking the table.
            escaped_info = result.info.replace("|", "\\|")
            if source_url:
                file_url = f"{source_url}/{result.filename}"
                line_cell = f"[{result.lineno}]({file_url}?plain=1#L{result.lineno})"
            else:
                line_cell = str(result.lineno)
            table_rows.append([line_cell, result.uri, escaped_info])
        lines.append(
            render_table(
                table_rows,
                headers=["Line", "URI", "Info"],
                table_format=TableFormat.GITHUB,
                colalign=("right", "left", "left"),
            )
        )
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Label selection
# ---------------------------------------------------------------------------


def get_label(repo_name: str) -> str:
    """Return the appropriate label based on repository name.

    :param repo_name: The repository name.
    :return: ``"🩹 fix link"`` for ``awesome-*`` repos, else ``"📚 documentation"``.
    """
    if repo_name.startswith("awesome-"):
        return "🩹 fix link"
    return "📚 documentation"


# ---------------------------------------------------------------------------
# Combined broken links issue (Lychee + Sphinx linkcheck)
# ---------------------------------------------------------------------------


def manage_combined_broken_links_issue(
    repo_name: str | None = None,
    lychee_exit_code: int | None = None,
    lychee_body_file: Path | None = None,
    sphinx_output_json: Path | None = None,
    sphinx_source_url: str | None = None,
) -> None:
    """Manage the combined broken links issue lifecycle.

    Combines results from Lychee and Sphinx linkcheck into a single "Broken
    links" issue. Each tool's results appear under its own heading. Tools that
    were not run are omitted from the report. Tools that found no broken links
    show a "No broken links found." message.

    When running in GitHub Actions, most parameters are auto-detected from
    environment variables and well-known file paths:

    - ``repo_name`` defaults to the name component of ``$GITHUB_REPOSITORY``.
    - ``lychee_body_file`` defaults to ``./lychee/out.md`` when
      ``lychee_exit_code`` is provided and the file exists.
    - ``sphinx_output_json`` defaults to ``./docs/linkcheck/output.json``
      when the file exists.
    - ``sphinx_source_url`` is composed from ``$GITHUB_SERVER_URL``,
      ``$GITHUB_REPOSITORY``, and ``$GITHUB_SHA`` when ``sphinx_output_json``
      is set.

    :param repo_name: Repository name (for label selection). Defaults to
        the name component of ``$GITHUB_REPOSITORY``.
    :param lychee_exit_code: Exit code from lychee (0=no broken links,
        2=broken links found). ``None`` if lychee was not run.
    :param lychee_body_file: Path to the lychee output file. Defaults to
        ``./lychee/out.md`` when ``lychee_exit_code`` is provided and the
        file exists.
    :param sphinx_output_json: Path to Sphinx linkcheck ``output.json``.
        Defaults to ``./docs/linkcheck/output.json`` when the file exists.
    :param sphinx_source_url: Base URL for linking filenames and line numbers
        in the Sphinx report. Auto-composed from ``$GITHUB_SERVER_URL``,
        ``$GITHUB_REPOSITORY``, and ``$GITHUB_SHA`` when not provided.
    :raises ValueError: If lychee exit code is not 0, 2, or ``None``.
    :raises ValueError: If ``repo_name`` cannot be determined.
    """
    # Auto-detect repo_name from $GITHUB_REPOSITORY.
    if repo_name is None:
        gh_repo = os.getenv("GITHUB_REPOSITORY", "")
        if gh_repo:
            repo_name = gh_repo.split("/")[-1]
            logging.info(
                f"Auto-detected repo_name={repo_name!r} from $GITHUB_REPOSITORY"
            )
    if not repo_name:
        msg = "No repository name specified. Set --repo-name or $GITHUB_REPOSITORY."
        raise ValueError(msg)

    # Auto-detect lychee body file when lychee was run.
    if lychee_exit_code is not None and lychee_body_file is None:
        if LYCHEE_DEFAULT_BODY.exists():
            lychee_body_file = LYCHEE_DEFAULT_BODY.resolve()
            logging.info(f"Auto-detected lychee body file: {lychee_body_file}")

    # Auto-detect Sphinx linkcheck output.
    if sphinx_output_json is None and SPHINX_DEFAULT_OUTPUT.exists():
        sphinx_output_json = SPHINX_DEFAULT_OUTPUT.resolve()
        logging.info(f"Auto-detected Sphinx output: {sphinx_output_json}")

    # Auto-compose Sphinx source URL from GitHub Actions environment.
    if sphinx_output_json is not None and sphinx_source_url is None:
        server_url = os.getenv("GITHUB_SERVER_URL", "")
        gh_repo = os.getenv("GITHUB_REPOSITORY", "")
        sha = os.getenv("GITHUB_SHA", "")
        if server_url and gh_repo and sha:
            sphinx_source_url = f"{server_url}/{gh_repo}/blob/{sha}/docs"
            logging.info(f"Auto-composed source URL: {sphinx_source_url}")

    # Interpret lychee exit code.
    # Exit codes: 0 = success, 1 = unexpected failure, 2 = broken links,
    # 3 = config error. Only treat as "broken links found" when lychee
    # produced an output file with actual content. A non-zero exit code
    # without output (e.g., config error, transient failure) should not
    # create an issue that misleadingly says "No broken links found."
    lychee_has_broken = False
    if lychee_exit_code is not None:
        if lychee_exit_code != 0 and lychee_body_file is not None:
            lychee_has_broken = True
        elif lychee_exit_code != 0:
            logging.warning(
                f"Lychee exit code {lychee_exit_code} but no output file found. "
                "Skipping broken links report."
            )
        logging.info(
            f"Lychee exit code {lychee_exit_code}: "
            f"{'broken links found' if lychee_has_broken else 'no broken links'}"
        )

    # Parse Sphinx linkcheck results.
    sphinx_has_broken = False
    sphinx_report = ""
    if sphinx_output_json is not None:
        results = parse_output_json(sphinx_output_json)
        broken = filter_broken(results)
        sphinx_has_broken = len(broken) > 0
        if sphinx_has_broken:
            sphinx_report = generate_markdown_report(
                broken,
                source_url=sphinx_source_url,
            )
        else:
            logging.info("No broken documentation links found.")

    # Build combined issue body.
    lychee_section = ""
    if lychee_exit_code is not None:
        if lychee_has_broken and lychee_body_file is not None:
            lychee_content = lychee_body_file.read_text(encoding="UTF-8").strip()
        else:
            lychee_content = "No broken links found."
        lychee_section = f"## Lychee\n\n{lychee_content}"

    sphinx_section = ""
    if sphinx_output_json is not None:
        sphinx_content = (
            sphinx_report.strip() if sphinx_has_broken else "No broken links found."
        )
        sphinx_section = f"## Sphinx linkcheck\n\n{sphinx_content}"

    has_broken_links = lychee_has_broken or sphinx_has_broken
    body = (
        render_template(
            "broken-links-issue",
            lychee_section=lychee_section,
            sphinx_section=sphinx_section,
        )
        + "\n"
    )

    # Write combined body to a temporary file.
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        delete=False,
        encoding="UTF-8",
    ) as tmp:
        tmp.write(body)
        body_file = Path(tmp.name)

    manage_issue_lifecycle(
        has_issues=has_broken_links,
        body_file=body_file,
        labels=[get_label(repo_name)],
        title=ISSUE_TITLE,
    )
