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
from unittest.mock import patch

import pytest

from repomatic.broken_links import (
    NO_BROKEN_LINKS,
    LinkcheckResult,
    LinkVerdict,
    filter_broken,
    generate_markdown_report,
    get_label,
    lychee_section,
    manage_combined_broken_links_issue,
    parse_output_json,
    sphinx_section,
)
from repomatic.markdown import sanitize_markdown_mentions

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
def test_lychee_section_reads_the_exit_code(
    tmp_path, exit_code, has_report, verdict, needle
):
    """Only exit 2 with a report reports broken links; 1 and 3 report nothing."""
    report = tmp_path / "out.md"
    report.write_text("Broken: mango.example", encoding="UTF-8")
    section, found = lychee_section(exit_code, report if has_report else None)
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


def _manage(tmp_path, monkeypatch, exit_code, sphinx_status=None):
    """Run the lifecycle with lychee's *exit_code*, capturing the issue call."""
    monkeypatch.chdir(tmp_path)
    report = tmp_path / "out.md"
    report.write_text("Broken: mango.example", encoding="UTF-8")
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
            lychee_exit_code=exit_code,
            lychee_body_file=report,
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
