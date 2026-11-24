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

"""Tests for the broken links report: linkcheck parsing and the issue verdict."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from repomatic.broken_links import (
    BLOB_URL_PATTERN,
    FIX_LINK_LABEL,
    GITHUB_TOKEN_ENV,
    LYCHEE_INSTALL_FAILURE_EXIT,
    LYCHEE_REPORT_FORMAT,
    NO_BROKEN_LINKS,
    RAW_URL_TEMPLATE,
    RELEASE_DOWNLOAD_PATTERN,
    SPHINX_FIRST_LOCATION_NOTE,
    LinkcheckResult,
    LinkVerdict,
    LycheeRun,
    filter_broken,
    generate_markdown_report,
    get_label,
    lychee_github_auth,
    lychee_section,
    manage_combined_broken_links_issue,
    parse_output_json,
    run_lychee,
    sphinx_section,
)
from repomatic.cli.main import repomatic
from repomatic.markdown import sanitize_markdown_mentions
from repomatic.tooling.registry import TOOL_REGISTRY, ToolInstallError
from repomatic.tooling.runner import run_tool
from tests.conftest import skip_unless_tool_runs

# ---------------------------------------------------------------------------
# Label selection tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("repo", "expected"),
    (
        ("awesome-falsehood", "🩹 fix link"),
        # The prefix alone is enough, with nothing after it.
        ("awesome-", "🩹 fix link"),
        ("workflows", "📚 documentation"),
        # Containing "awesome" is not the same as starting with it.
        ("my-awesome-repo", "📚 documentation"),
    ),
)
def test_get_label(repo, expected):
    """Only a repo whose name starts with `awesome-` gets the fix-link label."""
    assert get_label(repo) == expected


# ---------------------------------------------------------------------------
# Sphinx linkcheck parsing tests
# ---------------------------------------------------------------------------


def test_parse_output_json_empty_file(tmp_path):
    """Empty file returns no results."""
    output = tmp_path / "output.json"
    output.write_text("", encoding="UTF-8")
    assert parse_output_json(output) == []


def test_parse_output_json_skips_blank_lines(tmp_path):
    """Blank lines in the file are skipped."""
    entry = {
        "filename": "index.rst",
        "lineno": 1,
        "status": "working",
        "code": 200,
        "uri": "https://example.com",
        "info": "",
    }
    content = "\n" + json.dumps(entry) + "\n\n"
    output = tmp_path / "output.json"
    output.write_text(content, encoding="UTF-8")
    results = parse_output_json(output)
    assert len(results) == 1
    assert results[0].filename == "index.rst"


def test_parse_output_json_single_entry(tmp_path):
    """Single entry is parsed correctly."""
    entry = {
        "filename": "api.rst",
        "lineno": 42,
        "status": "broken",
        "code": 404,
        "uri": "https://example.com/missing",
        "info": "404 Not Found",
    }
    output = tmp_path / "output.json"
    output.write_text(json.dumps(entry) + "\n", encoding="UTF-8")
    results = parse_output_json(output)
    assert len(results) == 1
    assert results[0] == LinkcheckResult(
        filename="api.rst",
        lineno=42,
        status="broken",
        uri="https://example.com/missing",
        info="404 Not Found",
    )


def test_parse_output_json_multiple_entries(tmp_path):
    """Multiple entries are parsed correctly."""
    entries = [
        {
            "filename": "index.rst",
            "lineno": 1,
            "status": "working",
            "code": 200,
            "uri": "https://example.com",
            "info": "",
        },
        {
            "filename": "api.rst",
            "lineno": 10,
            "status": "broken",
            "code": 404,
            "uri": "https://example.com/gone",
            "info": "404 Not Found",
        },
    ]
    content = "\n".join(json.dumps(e) for e in entries) + "\n"
    output = tmp_path / "output.json"
    output.write_text(content, encoding="UTF-8")
    results = parse_output_json(output)
    assert len(results) == 2


def test_parse_output_json_missing_info_field(tmp_path):
    """Missing info field defaults to empty string."""
    entry = {
        "filename": "index.rst",
        "lineno": 1,
        "status": "working",
        "code": 200,
        "uri": "https://example.com",
    }
    output = tmp_path / "output.json"
    output.write_text(json.dumps(entry) + "\n", encoding="UTF-8")
    results = parse_output_json(output)
    assert results[0].info == ""


# ---------------------------------------------------------------------------
# Sphinx linkcheck filtering tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected_count"),
    [
        ("broken", 1),
        ("timeout", 1),
        ("working", 0),
        ("redirected", 0),
        ("unchecked", 0),
    ],
)
def test_filter_by_status(status, expected_count):
    """Only broken and timeout statuses are kept."""
    results = [
        LinkcheckResult(
            filename="index.rst",
            lineno=1,
            status=status,
            uri="https://example.com",
            info="",
        ),
    ]
    assert len(filter_broken(results)) == expected_count


def test_filter_broken_mixed_statuses():
    """Only broken and timeout are kept from a mixed list."""
    results = [
        LinkcheckResult("a.rst", 1, "working", "https://a.com", ""),
        LinkcheckResult("b.rst", 2, "broken", "https://b.com", "Not Found"),
        LinkcheckResult("c.rst", 3, "redirected", "https://c.com", ""),
        LinkcheckResult("d.rst", 4, "timeout", "https://d.com", "Timed out"),
        LinkcheckResult("e.rst", 5, "unchecked", "https://e.com", ""),
    ]
    broken = filter_broken(results)
    assert len(broken) == 2
    assert broken[0].uri == "https://b.com"
    assert broken[1].uri == "https://d.com"


def test_filter_broken_empty_input():
    """Empty input returns empty list."""
    assert filter_broken([]) == []


# ---------------------------------------------------------------------------
# Sphinx linkcheck report generation tests
# ---------------------------------------------------------------------------


def test_report_empty_list():
    """Empty list returns empty string."""
    assert generate_markdown_report([]) == ""


def test_report_single_broken_link():
    """Single broken link generates a valid report."""
    broken = [
        LinkcheckResult(
            filename="index.rst",
            lineno=10,
            status="broken",
            uri="https://example.com/missing",
            info="404 Not Found",
        ),
    ]
    report = generate_markdown_report(broken)
    # Report is a section (no H1 heading), ready for embedding.
    assert "# Broken documentation links" not in report
    assert "## `index.rst`" in report
    assert "| Line " in report
    assert "| URI " in report
    assert "| Info " in report
    assert "10" in report
    assert "https://example.com/missing" in report
    assert "404 Not Found" in report
    # Status column should not be present.
    assert "Status" not in report


def test_report_groups_by_file_alphabetically():
    """Results are grouped by filename in alphabetical order."""
    broken = [
        LinkcheckResult("z_file.rst", 1, "broken", "https://z.com", ""),
        LinkcheckResult("a_file.rst", 5, "broken", "https://a.com", ""),
        LinkcheckResult("a_file.rst", 2, "broken", "https://a2.com", ""),
    ]
    report = generate_markdown_report(broken)
    a_pos = report.index("## `a_file.rst`")
    z_pos = report.index("## `z_file.rst`")
    assert a_pos < z_pos


def test_report_escapes_pipe_chars_in_info():
    """Pipe characters in info are escaped to avoid breaking tables."""
    broken = [
        LinkcheckResult(
            filename="index.rst",
            lineno=1,
            status="broken",
            uri="https://example.com",
            info="Error | Details",
        ),
    ]
    report = generate_markdown_report(broken)
    assert "Error \\| Details" in report


def test_report_source_url_links_filenames_and_lines():
    """When source_url is provided, filenames and line numbers are linked."""
    broken = [
        LinkcheckResult(
            filename="architectures.md",
            lineno=4,
            status="broken",
            uri="https://example.com/missing",
            info="404 Not Found",
        ),
    ]
    source_url = "https://github.com/owner/repo/blob/abc123/docs"
    report = generate_markdown_report(broken, source_url=source_url)

    # File header should be a link.
    assert (
        "## [`architectures.md`]"
        "(https://github.com/owner/repo/blob/abc123/docs/architectures.md)"
    ) in report

    # Line number should be a deep link.
    assert (
        "[4](https://github.com/owner/repo/blob/abc123/docs/architectures.md?plain=1#L4)"
    ) in report


def test_report_no_source_url_plain_text():
    """Without source_url, filenames and line numbers are plain text."""
    broken = [
        LinkcheckResult(
            filename="architectures.md",
            lineno=4,
            status="broken",
            uri="https://example.com/missing",
            info="404 Not Found",
        ),
    ]
    report = generate_markdown_report(broken)

    # File header should be plain text.
    assert "## `architectures.md`" in report
    assert "## [`architectures.md`](" not in report

    # Line number should be plain text, not a link.
    assert " 4 " in report
    assert "[4](" not in report


# ---------------------------------------------------------------------------
# Sanitization of external tool output
# ---------------------------------------------------------------------------

ZWS = "\u200b"


@pytest.mark.parametrize(
    "info",
    (
        pytest.param("403 Forbidden @dependabot", id="mention_in_info"),
        pytest.param(
            "Redirected to https://github.com/org/repo/issues/42",
            id="github_url_in_info",
        ),
        pytest.param("See #123 for details", id="issue_ref_in_info"),
    ),
)
def test_report_leaves_external_content_unsanitized(info):
    """The report renderer passes tool output through; the caller sanitizes it.

    Splitting the two is what lets the same report be written to a file, where
    the mention would be inert, or posted to an issue, where it would not.
    `test_lychee_output_sanitized` covers the caller's half.
    """
    broken = [
        LinkcheckResult(
            filename="index.rst",
            lineno=1,
            status="broken",
            uri="https://example.com",
            info=info,
        ),
    ]
    report = generate_markdown_report(broken)
    # Only the table-breaking pipe is escaped; nothing else is touched.
    assert info.replace("|", "\\|") in report


def test_lychee_output_sanitized(tmp_path):
    """Lychee markdown output is sanitized before embedding in issue body."""
    raw = "Broken: @admin mentioned in https://github.com/org/repo #99"
    sanitized = sanitize_markdown_mentions(raw)
    assert f"@{ZWS}admin" in sanitized
    assert "redirect.github.com/" in sanitized
    assert f"#{ZWS}99" in sanitized


# ---------------------------------------------------------------------------
# Verdicts and the issue lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exit_code", "has_report", "verdict", "needle"),
    (
        pytest.param(0, False, LinkVerdict.CLEAN, NO_BROKEN_LINKS, id="clean"),
        pytest.param(2, True, LinkVerdict.BROKEN, "Broken: mango.example", id="broken"),
        pytest.param(
            2, False, LinkVerdict.UNKNOWN, "wrote no report", id="broken-no-report"
        ),
        pytest.param(1, True, LinkVerdict.UNKNOWN, "did not complete", id="failure"),
        pytest.param(3, False, LinkVerdict.UNKNOWN, "exit code 3", id="config-error"),
    ),
)
def test_lychee_section_reads_the_exit_code(exit_code, has_report, verdict, needle):
    """Only exit 2 with a report reports broken links; 1 and 3 report nothing."""
    report = "Broken: mango.example" if has_report else None
    section, found = lychee_section(LycheeRun(exit_code, report))
    assert found is verdict
    assert section.startswith("## Lychee\n\n")
    assert needle in section


@pytest.mark.parametrize(
    ("status", "verdict"),
    (("working", LinkVerdict.CLEAN), ("broken", LinkVerdict.BROKEN)),
)
def test_sphinx_section_reads_the_statuses(tmp_path, status, verdict):
    """Any broken or timed-out link makes the Sphinx verdict broken."""
    output = tmp_path / "output.json"
    entry = {
        "filename": "index.rst",
        "lineno": 3,
        "status": status,
        "uri": "https://example.com/kiwi",
    }
    output.write_text(json.dumps(entry) + "\n", encoding="UTF-8")
    section, found = sphinx_section(output)
    assert found is verdict
    assert section.startswith("## Sphinx linkcheck\n\n")


@pytest.mark.parametrize(("status", "warns"), (("working", False), ("broken", True)))
def test_sphinx_section_warns_of_one_location_per_url(tmp_path, status, warns):
    """Only a section with broken links warns that it lists a URL at one location.

    Sphinx linkcheck checks a URL once, so a URL used in two files shows under
    one of them: the warning sends the reader to search for the other.
    """
    output = tmp_path / "output.json"
    entry = {
        "filename": "index.rst",
        "lineno": 3,
        "status": status,
        "uri": "https://example.com/kiwi",
    }
    output.write_text(json.dumps(entry) + "\n", encoding="UTF-8")
    section, _ = sphinx_section(output)
    warning = f"## Sphinx linkcheck\n\n{SPHINX_FIRST_LOCATION_NOTE}\n\n"
    assert section.startswith(warning) is warns


def _manage(tmp_path, monkeypatch, exit_code, sphinx_status=None):
    """Run the lifecycle with lychee's *exit_code*, capturing the issue call."""
    monkeypatch.chdir(tmp_path)
    output = None
    if sphinx_status:
        output = tmp_path / "output.json"
        entry = {
            "filename": "index.rst",
            "lineno": 3,
            "status": sphinx_status,
            "uri": "https://example.com/kiwi",
        }
        output.write_text(json.dumps(entry) + "\n", encoding="UTF-8")
    with patch("repomatic.broken_links.manage_issue_lifecycle") as lifecycle:
        verdict = manage_combined_broken_links_issue(
            repo_name="orchard",
            lychee=LycheeRun(exit_code, "Broken: mango.example"),
            sphinx_output_json=output,
        )
    return verdict, lifecycle


@pytest.mark.parametrize("exit_code", (1, 3))
def test_a_failed_lychee_run_leaves_the_issue_alone(tmp_path, monkeypatch, exit_code):
    """A run that did not complete never closes an open broken links issue.

    It says nothing about the links, so claiming "No more issues." would close
    a report of links that may still be broken.
    """
    verdict, lifecycle = _manage(tmp_path, monkeypatch, exit_code)
    assert verdict is LinkVerdict.UNKNOWN
    lifecycle.assert_not_called()


def test_a_failed_lychee_run_beside_broken_sphinx_links_files_both(
    tmp_path, monkeypatch
):
    """Broken links found anywhere are filed, with the incomplete run said so."""
    verdict, lifecycle = _manage(tmp_path, monkeypatch, 1, sphinx_status="broken")
    assert verdict is LinkVerdict.BROKEN
    kwargs = lifecycle.call_args.kwargs
    assert kwargs["has_issues"] is True
    assert "Lychee did not complete" in kwargs["body"]
    assert "https://example.com/kiwi" in kwargs["body"]


@pytest.mark.parametrize(
    ("exit_code", "has_issues"),
    (pytest.param(2, True, id="broken"), pytest.param(0, False, id="clean")),
)
def test_a_completed_run_files_or_closes_the_issue(
    tmp_path, monkeypatch, exit_code, has_issues
):
    """A completed run decides the issue, either way."""
    verdict, lifecycle = _manage(tmp_path, monkeypatch, exit_code)
    assert verdict is (LinkVerdict.BROKEN if has_issues else LinkVerdict.CLEAN)
    assert lifecycle.call_args.kwargs["has_issues"] is has_issues
    assert lifecycle.call_args.kwargs["labels"] == ["📚 documentation"]


def test_a_ci_run_detects_every_default(tmp_path, monkeypatch):
    """In CI, the name, the linkcheck output and its source URL are all detected."""
    sha = "a1b2c3d4" * 5
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_REPOSITORY", "orchard/awesome-papaya")
    monkeypatch.setenv("GITHUB_SHA", sha)
    output = tmp_path / "docs" / "_linkcheck" / "output.json"
    output.parent.mkdir(parents=True)
    entry = {
        "filename": "index.rst",
        "lineno": 3,
        "status": "broken",
        "uri": "https://example.com/kiwi",
    }
    output.write_text(json.dumps(entry) + "\n", encoding="UTF-8")

    with patch("repomatic.broken_links.manage_issue_lifecycle") as lifecycle:
        verdict = manage_combined_broken_links_issue()

    assert verdict is LinkVerdict.BROKEN
    kwargs = lifecycle.call_args.kwargs
    assert kwargs["labels"] == [FIX_LINK_LABEL]
    assert f"orchard/awesome-papaya/blob/{sha}/docs/index.rst" in kwargs["body"]
    assert "https://example.com/kiwi" in kwargs["body"]


def test_no_repository_name_to_detect_is_an_error(tmp_path, monkeypatch):
    """With neither `$GITHUB_REPOSITORY` nor a clone, no name selects the label."""
    monkeypatch.chdir(tmp_path)
    with (
        patch("repomatic.broken_links.manage_issue_lifecycle") as lifecycle,
        pytest.raises(ValueError, match="No repository name specified"),
    ):
        manage_combined_broken_links_issue()
    lifecycle.assert_not_called()


def test_no_checker_leaves_the_issue_alone(tmp_path, monkeypatch, caplog):
    """With no checker run, nothing is known about the links: the issue stays.

    No crawl and no Sphinx linkcheck output said a word about the links, so
    closing an open issue would announce fixes that nobody checked.
    """
    monkeypatch.chdir(tmp_path)
    with (
        patch("repomatic.broken_links.manage_issue_lifecycle") as lifecycle,
        caplog.at_level("WARNING"),
    ):
        verdict = manage_combined_broken_links_issue(repo_name="orchard")
    assert verdict is None
    lifecycle.assert_not_called()
    assert "No link checker ran" in caplog.text


def test_cli_with_no_checker_exits_clean(tmp_path, monkeypatch):
    """`--no-lychee` with no linkcheck output leaves the issue, and exits 0.

    The job is advisory: a run that checked nothing must not turn it red.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GH_TOKEN", "fake-token")
    with patch("repomatic.broken_links.manage_issue_lifecycle") as lifecycle:
        result = CliRunner().invoke(
            repomatic, ["broken-links", "--no-lychee", "--repo-name", "orchard"]
        )
    assert result.exit_code == 0, result.output
    lifecycle.assert_not_called()


def test_cli_without_a_repository_name_is_a_usage_error(tmp_path, monkeypatch):
    """No `--repo-name` and nothing to detect one from fails before any check."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GH_TOKEN", "fake-token")
    with (
        patch(
            "repomatic.broken_links.Metadata",
            return_value=SimpleNamespace(repo_name=None),
        ),
        patch("repomatic.broken_links.manage_issue_lifecycle") as lifecycle,
    ):
        result = CliRunner().invoke(repomatic, ["broken-links", "--no-lychee"])
    assert result.exit_code == 2
    assert (
        "No repository name specified. Set --repo-name or $GITHUB_REPOSITORY."
        in result.output
    )
    lifecycle.assert_not_called()


# ---------------------------------------------------------------------------
# The lychee crawl
# ---------------------------------------------------------------------------


def test_run_lychee_skips_a_repository_with_no_documentation(tmp_path, monkeypatch):
    """With no documentation file, lychee has nothing to check and never runs."""
    monkeypatch.chdir(tmp_path)
    with patch("repomatic.broken_links.run_tool") as run_tool:
        assert run_lychee() is None
    run_tool.assert_not_called()


def test_run_lychee_reads_back_the_report_it_asked_for(tmp_path, monkeypatch):
    """The crawl is the bare `run lychee` invocation, plus the report flags.

    The report reaches the issue through the run itself, with no path agreed on
    with a workflow step.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(GITHUB_TOKEN_ENV, raising=False)
    Path("readme.md").write_text("# Orchard\n", encoding="UTF-8")
    calls = []

    def crawl(name, extra_args):
        calls.append((name, extra_args))
        output = extra_args[extra_args.index("--output") + 1]
        Path(output).write_text("Broken: kiwi.example", encoding="UTF-8")
        return 2

    with patch("repomatic.broken_links.run_tool", side_effect=crawl):
        run = run_lychee()

    assert run == LycheeRun(2, "Broken: kiwi.example")
    ((name, args),) = calls
    assert name == "lychee"
    assert args[: len(LYCHEE_REPORT_FORMAT)] == list(LYCHEE_REPORT_FORMAT)
    assert args[-1] == "readme.md"
    for flag in TOOL_REGISTRY["lychee"].default_args:
        assert flag in args


def test_run_lychee_without_a_report_says_so(tmp_path, monkeypatch):
    """A crawl that wrote no report carries none, whatever its exit code."""
    monkeypatch.chdir(tmp_path)
    Path("readme.md").write_text("# Orchard\n", encoding="UTF-8")
    with patch("repomatic.broken_links.run_tool", return_value=1):
        assert run_lychee() == LycheeRun(1)


def test_run_lychee_that_cannot_install_did_not_complete(tmp_path, monkeypatch):
    """A lychee that cannot be installed checked nothing: the issue stays put."""
    monkeypatch.chdir(tmp_path)
    Path("readme.md").write_text("# Orchard\n", encoding="UTF-8")
    refused = ToolInstallError("no binary for this platform")
    with patch("repomatic.broken_links.run_tool", side_effect=refused):
        run = run_lychee()
    assert run == LycheeRun(LYCHEE_INSTALL_FAILURE_EXIT)
    _section, verdict = lychee_section(run)
    assert verdict is LinkVerdict.UNKNOWN


FIG_TEMPLATE = str(Path("templates", "fig.md"))
"""A documentation file that the configurations below exclude."""


@pytest.mark.parametrize(
    ("config", "checked"),
    (
        pytest.param(
            {"pyproject.toml": '[tool.lychee]\nexclude_path = ["fig"]\n'},
            {"readme.md"},
            id="pyproject-table",
        ),
        pytest.param(
            {"lychee.toml": 'exclude_path = ["^templates"]\n'},
            {"readme.md"},
            id="lychee-toml",
        ),
        pytest.param({}, {"readme.md", FIG_TEMPLATE}, id="no-config"),
    ),
)
def test_run_lychee_leaves_out_the_paths_lychee_excludes(
    tmp_path, monkeypatch, config, checked
):
    """lychee warns on each input that its `exclude_path` excludes: none gets there.

    lychee searches each expression in an input path as it was passed, which is
    the string the inventory hands over.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(GITHUB_TOKEN_ENV, raising=False)
    Path("readme.md").write_text("# Orchard\n", encoding="UTF-8")
    Path("templates").mkdir()
    Path(FIG_TEMPLATE).write_text("[Fig]($orchard_url)\n", encoding="UTF-8")
    for name, content in config.items():
        Path(name).write_text(content, encoding="UTF-8")
    seen = {}

    def crawl(name, extra_args):
        seen["args"] = extra_args
        return 0

    with patch("repomatic.broken_links.run_tool", side_effect=crawl):
        assert run_lychee() == LycheeRun(0)
    assert {arg for arg in seen["args"] if arg in {"readme.md", FIG_TEMPLATE}} == (
        checked
    )


def test_run_lychee_skips_when_lychee_excludes_every_file(tmp_path, monkeypatch):
    """With every documentation file excluded, lychee has nothing to check."""
    monkeypatch.chdir(tmp_path)
    Path("readme.md").write_text("# Orchard\n", encoding="UTF-8")
    Path("lychee.toml").write_text("exclude_path = ['[.]md$']\n", encoding="UTF-8")
    with patch("repomatic.broken_links.run_tool") as run_tool:
        assert run_lychee() is None
    run_tool.assert_not_called()


@pytest.mark.parametrize("token", ("", "  "))
def test_lychee_github_auth_needs_a_token(tmp_path, monkeypatch, token):
    """With no token, the crawl stays anonymous and no file is written."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(GITHUB_TOKEN_ENV, token)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    assert lychee_github_auth(scratch) == []
    assert not list(scratch.iterdir())


@pytest.mark.parametrize(
    ("files", "project_config"),
    (
        pytest.param({}, [], id="no-config"),
        pytest.param(
            {"lychee.toml": 'exclude = ["kiwi"]\n'},
            ["--config", "lychee.toml"],
            id="lychee-toml",
        ),
        pytest.param(
            {"pyproject.toml": '[tool.lychee]\nexclude = ["kiwi"]\n'},
            ["--config", "pyproject.toml"],
            id="pyproject-table",
        ),
        pytest.param(
            {"pyproject.toml": '[project]\nname = "orchard"\n'},
            [],
            id="pyproject-without-table",
        ),
        pytest.param(
            {
                "lychee.toml": 'exclude = ["kiwi"]\n',
                "pyproject.toml": '[tool.lychee]\nexclude = ["mango"]\n',
            },
            ["--config", "lychee.toml"],
            id="lychee-toml-first",
        ),
    ),
)
def test_lychee_github_auth_keeps_the_project_config(
    tmp_path, monkeypatch, files, project_config
):
    """Name the config lychee would find, then the file that signs github.com.

    Any `--config` stops lychee from finding the project's own config, so the
    arguments name that file first. The token is in the file, never in them.
    """
    monkeypatch.chdir(tmp_path)
    for name, content in files.items():
        Path(name).write_text(content, encoding="UTF-8")
    monkeypatch.setenv(GITHUB_TOKEN_ENV, "papaya-token")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    args = lychee_github_auth(scratch)
    auth = scratch / "github-auth.toml"
    assert args == [*project_config, "--config", str(auth)]
    assert not any("papaya-token" in arg for arg in args)
    signed = {"headers": {"Authorization": "Bearer papaya-token"}}
    assert tomllib.loads(auth.read_text(encoding="UTF-8")) == {
        "exclude": [RELEASE_DOWNLOAD_PATTERN],
        "remap": [f"{BLOB_URL_PATTERN} {RAW_URL_TEMPLATE}"],
        "hosts": {"github.com": signed, "raw.githubusercontent.com": signed},
    }


@pytest.mark.parametrize(
    ("url", "excluded"),
    (
        ("https://github.com/orchard/papaya/releases/download/v1.0/papaya.bin", True),
        ("https://github.com/orchard/papaya/releases/latest/download/papaya.bin", True),
        ("https://github.com/orchard/papaya/releases/tag/v1.0", False),
        ("https://github.com/orchard/papaya/blob/main/readme.md", False),
    ),
)
def test_release_download_pattern(url, excluded):
    """A signed crawl skips the release downloads only."""
    assert bool(re.search(RELEASE_DOWNLOAD_PATTERN, url)) is excluded


RAW = "https://raw.githubusercontent.com/orchard/papaya"


@pytest.mark.parametrize(
    ("url", "remapped"),
    (
        pytest.param(
            "https://github.com/orchard/papaya/blob/main/fig.py#L73",
            f"{RAW}/main/fig.py",
            id="line-anchor",
        ),
        pytest.param(
            "https://github.com/orchard/papaya/blob/main/docs/fig.md?plain=1#L5",
            f"{RAW}/main/docs/fig.md",
            id="plain-view",
        ),
        pytest.param(
            "https://github.com/orchard/papaya/blob/v1.0/changelog.md",
            f"{RAW}/v1.0/changelog.md",
            id="tag-ref",
        ),
        pytest.param(
            "https://github.com/orchard/papaya/blob/0123abc/a/b/c/fig.toml",
            f"{RAW}/0123abc/a/b/c/fig.toml",
            id="nested-path",
        ),
        pytest.param(
            "https://github.com/orchard/papaya/tree/main/docs", None, id="tree"
        ),
        pytest.param("https://github.com/orchard/papaya/issues/7", None, id="issue"),
        pytest.param(
            "https://github.com/orchard/papaya/releases/tag/v1.0", None, id="release"
        ),
    ),
)
def test_blob_url_remap(url, remapped):
    """A `/blob/` URL becomes the raw URL of its file, query and fragment dropped.

    Read with Python's `re`, translating the ``${N}`` references of the Rust
    `regex` crate: the pattern itself uses only syntax both engines share.
    """
    template = re.sub(r"\$\{(\d+)\}", r"\\g<\1>", RAW_URL_TEMPLATE)
    if remapped is None:
        assert not re.search(BLOB_URL_PATTERN, url)
    else:
        assert re.sub(BLOB_URL_PATTERN, template, url) == remapped


def test_run_lychee_signs_github_requests(tmp_path, monkeypatch):
    """With a token, the crawl reads it from a file that ends with the crawl."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(GITHUB_TOKEN_ENV, "papaya-token")
    Path("readme.md").write_text("# Orchard\n", encoding="UTF-8")
    seen = {}

    def crawl(name, extra_args):
        seen["args"] = extra_args
        seen["auth"] = Path(extra_args[extra_args.index("--config") + 1])
        seen["content"] = seen["auth"].read_text(encoding="UTF-8")
        return 0

    with patch("repomatic.broken_links.run_tool", side_effect=crawl):
        assert run_lychee() == LycheeRun(0)
    assert "Bearer papaya-token" in seen["content"]
    assert not any("papaya-token" in arg for arg in seen["args"])
    assert seen["args"][-1] == "readme.md"
    assert not seen["auth"].exists()


@pytest.mark.once
@pytest.mark.real_http
def test_signed_crawl_keeps_the_project_excludes(tmp_path, monkeypatch, capfd):
    """The pinned lychee applies the excludes of both config files, and the remap.

    Lists the links lychee would check: the `[tool.lychee]` exclude and the
    release download exclude both apply, and the `/blob/` link shows as the
    raw.githubusercontent.com URL lychee checks in its place. Marked `once`: it
    downloads the pinned lychee binary, so one runner suffices.
    """
    monkeypatch.chdir(tmp_path)
    # Installed before the token is set, which the GitHub API would refuse.
    skip_unless_tool_runs("lychee")
    # Lychee never checks an example domain, so the excluded link names a real one.
    Path("pyproject.toml").write_text(
        '[tool.lychee]\nexclude = ["kiwi\\\\.org"]\n', encoding="UTF-8"
    )
    Path("page.md").write_text(
        "[blob](https://github.com/orchard/papaya/blob/main/readme.md)\n"
        "[download](https://github.com/orchard/papaya/releases/download/v1.0/fig.bin)\n"
        "[kiwi](https://kiwi.org/)\n",
        encoding="UTF-8",
    )
    monkeypatch.setenv(GITHUB_TOKEN_ENV, "papaya-token")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    capfd.readouterr()
    args = ["--dump", *lychee_github_auth(scratch), "page.md"]
    assert run_tool("lychee", extra_args=args) == 0
    listed = capfd.readouterr().out.split()
    assert listed == [f"{RAW}/main/readme.md"]


@pytest.mark.parametrize(
    ("flag", "crawls"),
    (
        pytest.param((), True, id="default"),
        pytest.param(("--no-lychee",), False, id="no-lychee"),
    ),
)
def test_cli_crawls_unless_told_not_to(monkeypatch, flag, crawls):
    """`broken-links` runs lychee itself, and `--no-lychee` leaves it out."""
    monkeypatch.setenv("GH_TOKEN", "fake-token")
    crawl = LycheeRun(0)
    with (
        patch("repomatic.cli.github.run_lychee", return_value=crawl) as run,
        patch("repomatic.cli.github.manage_combined_broken_links_issue") as manage,
    ):
        result = CliRunner().invoke(
            repomatic, ["broken-links", "--repo-name", "orchard", *flag]
        )
    assert result.exit_code == 0, result.output
    assert run.called is crawls
    assert manage.call_args.kwargs["lychee"] == (crawl if crawls else None)
