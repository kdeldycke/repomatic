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

{func}`run_lychee` runs the Lychee crawl itself, through the tool runner, and
reads back the report and the exit code. No workflow step has to relay either.

Each checker reports one {class}`LinkVerdict`. Only a run that completed can
clear the issue: a checker that did not finish says nothing about the links,
so it never closes an open issue.

Issue lifecycle management is delegated to {mod}`~repomatic.github.issue`.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from itertools import groupby
from operator import attrgetter
from pathlib import Path

from .github.issue import manage_issue_lifecycle
from .markdown import sanitize_markdown_mentions
from .metadata.core import Metadata
from .tabular import render_markdown_table
from .templating import render_template
from .tooling.registry import ToolInstallError, lookup_tool
from .tooling.runner import ConfigLevel, ConfigSource, resolve_default_args, run_tool
from .upstream import is_awesome_repo

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import Final


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

LYCHEE_REPORT_FORMAT: Final[tuple[str, ...]] = ("--format", "markdown")
"""Flags that make lychee write the markdown report the issue body embeds."""

LYCHEE_INSTALL_FAILURE_EXIT = 1
"""Exit code a lychee that could not be installed counts as.

The one lychee itself returns on an unexpected failure: like a crash, a missing
tool checks no link, so the issue is left as it is.
"""

GITHUB_TOKEN_ENV = "GITHUB_TOKEN"
"""Environment variable holding the token that signs lychee's github.com requests.

The `check-broken-links` job of `docs.yaml` sets it. See
{func}`lychee_github_auth`.
"""

RELEASE_DOWNLOAD_PATTERN = (
    r"^https://github\.com/[^/]+/[^/]+/releases/(?:latest/)?download/"
)
"""The lychee `exclude` pattern of a release download, which a signed request breaks.

See {func}`lychee_github_auth`.
"""

SPHINX_DEFAULT_OUTPUT = Path("./docs/_linkcheck/output.json")
"""Default Sphinx linkcheck output path produced by the `docs.yaml` workflow."""

NO_BROKEN_LINKS = "No broken links found."
"""The section body of a checker that completed and found nothing broken."""

SPHINX_FIRST_LOCATION_NOTE = (
    "Sphinx linkcheck checks each URL once and lists only its first location."
    " Search the repository for a URL before you fix it, to find its other"
    " locations."
)
"""Warning above the broken links of the Sphinx section.

Sphinx linkcheck collects each URI once: `HyperlinkCollector._add_uri` skips a
URI it already holds (Sphinx `9.2.0`), so `output.json` records only the first
place the URI appears. A URL used in a page and in a docstring shows under one
of them, and a fix made from the section alone misses the other.
"""


class LinkVerdict(StrEnum):
    """What one checker's run says about the links it covers."""

    BROKEN = "broken"
    """The run completed and found broken links."""

    CLEAN = "clean"
    """The run completed and found no broken link."""

    UNKNOWN = "unknown"
    """The run did not complete, or left no report: nothing can be claimed."""


@dataclass(frozen=True)
class LycheeRun:
    """What one lychee crawl of the documentation files returned."""

    exit_code: int
    """Exit code of lychee. See {data}`LYCHEE_BROKEN_LINKS_EXIT`."""

    report: str | None = None
    """The markdown report lychee wrote, `None` when it wrote none."""


def lychee_github_auth(scratch: Path) -> list[str]:
    """Return the lychee arguments that sign its github.com requests.

    A CI runner gets `503` from github.com for an anonymous request to a
    `/blob/` page, which passes from a workstation. Lychee `0.24.2` uses
    {data}`GITHUB_TOKEN_ENV` only in its GitHub API fallback, which reports a
    URL with a path after the repository as invalid. So the token signs the
    request itself, as `click_extra.sphinx` does for Sphinx linkcheck.

    Lychee reads the headers of one host only from the `[hosts."github.com"]`
    table of a config file: no flag sets them. So the table goes to a file in
    *scratch*, and the token stays out of the command line, which the tool
    runner logs.

    ```{caution}
    A `--config` flag stops lychee from finding the project's own config, in
    `lychee.toml` or in the `[tool.lychee]` table of `pyproject.toml`. That
    file comes first, in its own `--config`. Lychee merges the files: the last
    one wins a setting that two of them set, and their `exclude` lists add up.
    Lychee also finds a config in `Cargo.toml` or `package.json`, which
    {class}`~repomatic.tooling.runner.ConfigSource` does not know: such a
    config does not apply to a signed crawl.
    ```

    The file also excludes {data}`RELEASE_DOWNLOAD_PATTERN`. With an
    `Authorization` header, github.com redirects a release download to a host
    whose signed URL answers `401`, and a lychee config sets headers per host,
    not per URL. Sphinx linkcheck still checks the release downloads of its
    pages, without the token.

    :param scratch: Directory that keeps the file for the length of the crawl.
    :return: The `--config` arguments, or an empty list when no token is set.
    """
    token = os.environ.get(GITHUB_TOKEN_ENV, "").strip()
    if not token:
        return []
    spec = lookup_tool("lychee")
    assert spec.config_flag is not None
    # The levels lychee reads with no flag. The tool runner passes the others.
    source = ConfigSource.find(spec)
    project_config: list[str] = []
    if source.native_file is not None:
        project_config = [spec.config_flag, str(source.native_file)]
    elif source.level is ConfigLevel.PYPROJECT and spec.reads_pyproject:
        project_config = [spec.config_flag, "pyproject.toml"]
    auth = scratch / "github-auth.toml"
    auth.write_text(
        spec.native_format.serialize({
            "exclude": [RELEASE_DOWNLOAD_PATTERN],
            "hosts": {"github.com": {"headers": {"Authorization": f"Bearer {token}"}}},
        }),
        encoding="UTF-8",
    )
    logging.info(
        f"Lychee signs its github.com requests with ${GITHUB_TOKEN_ENV}, and skips"
        " release downloads."
    )
    return [*project_config, spec.config_flag, str(auth)]


def run_lychee() -> LycheeRun | None:
    """Crawl the documentation files with lychee, and keep its report.

    Runs the invocation of a bare `repomatic run lychee`: the default arguments
    of the registry, over the `doc_files` inventory. The markdown report goes
    to a scratch file this function reads back, so no workflow step has to
    agree on a path with it. With {data}`GITHUB_TOKEN_ENV` set, the crawl also
    signs its github.com requests, per {func}`lychee_github_auth`.

    A lychee that cannot be installed checked no link, so it counts as a run
    that did not complete: {data}`LYCHEE_INSTALL_FAILURE_EXIT`.

    :return: The run, or `None` when the repository holds no documentation
        file: lychee has nothing to check, and its section is left out.
    """
    batches = resolve_default_args(lookup_tool("lychee"))
    if batches is None:
        return None
    # lychee takes its targets in one invocation, never one per file.
    (crawl,) = batches
    with tempfile.TemporaryDirectory(prefix="repomatic-lychee-") as scratch:
        report_path = Path(scratch) / "report.md"
        args = [
            *LYCHEE_REPORT_FORMAT,
            "--output",
            str(report_path),
            *lychee_github_auth(Path(scratch)),
            *crawl,
        ]
        try:
            exit_code = run_tool("lychee", extra_args=args)
        except ToolInstallError as error:
            logging.error(f"Cannot run lychee: {error}")
            return LycheeRun(LYCHEE_INSTALL_FAILURE_EXIT)
        report = None
        if report_path.is_file():
            report = report_path.read_text(encoding="UTF-8")
    return LycheeRun(exit_code, report)


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


def lychee_section(run: LycheeRun) -> tuple[str, LinkVerdict]:
    """Render the Lychee section of the issue body, with its verdict.

    Only {data}`LYCHEE_BROKEN_LINKS_EXIT` reports on the links themselves, and
    only with a report to show: a broken-links verdict with nothing to show
    would file an issue whose body claims the links are fine. Every other
    non-zero code reports that the run failed.

    :param run: The lychee crawl.
    :return: The section markdown, and what it says about the links.
    """
    exit_code = run.exit_code
    if exit_code == 0:
        return f"## Lychee\n\n{NO_BROKEN_LINKS}", LinkVerdict.CLEAN
    if exit_code == LYCHEE_BROKEN_LINKS_EXIT and run.report is not None:
        report = sanitize_markdown_mentions(run.report)
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
    return (
        f"## Sphinx linkcheck\n\n{SPHINX_FIRST_LOCATION_NOTE}\n\n{report.strip()}",
        LinkVerdict.BROKEN,
    )


def manage_combined_broken_links_issue(
    repo_name: str | None = None,
    lychee: LycheeRun | None = None,
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
    links that run covers might still be broken. When no checker ran at all,
    the issue is left as it is too, with a warning: nothing was checked.

    When running in GitHub Actions, most parameters are auto-detected from
    {class}`~repomatic.metadata.core.Metadata` and well-known file paths:

    - `repo_name` defaults to {attr}`Metadata.repo_name
      <repomatic.metadata.env.EnvironmentMetadata.repo_name>`.
    - `sphinx_output_json` defaults to {data}`SPHINX_DEFAULT_OUTPUT` when the
      file exists.
    - `sphinx_source_url` is composed from {attr}`Metadata.repo_url
      <repomatic.metadata.env.EnvironmentMetadata.repo_url>` and
      {attr}`Metadata.sha <repomatic.metadata.env.EnvironmentMetadata.sha>`.

    :param repo_name: Repository name, for label selection.
    :param lychee: The lychee crawl from {func}`run_lychee`, `None` if lychee
        did not run.
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
    if lychee is not None:
        sections["lychee_section"], verdict = lychee_section(lychee)
        verdicts.append(verdict)
    if sphinx_output_json is not None:
        sections["sphinx_section"], verdict = sphinx_section(
            sphinx_output_json, sphinx_source_url
        )
        verdicts.append(verdict)

    if not verdicts:
        logging.warning(
            "No link checker ran: lychee did not crawl, and no Sphinx linkcheck"
            " output was found. The broken links issue is left as it is."
        )
        return None
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
    return overall
