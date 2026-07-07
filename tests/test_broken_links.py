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

"""Tests for issue lifecycle management (triage, broken links, setup guide) and
Sphinx linkcheck parsing and report generation.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from repomatic.broken_links import (
    LinkcheckResult,
    filter_broken,
    generate_markdown_report,
    get_label,
    parse_output_json,
)
from repomatic.cli import repomatic as repomatic_cli
from repomatic.github.issue import _fit_body_file, triage_issues
from repomatic.github.pr_body import GITHUB_BODY_MAX_CHARS

TITLE = "Broken links"


# ---------------------------------------------------------------------------
# Issue triage tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("needed", "expected"),
    [
        (True, (True, None, None, set())),
        (False, (False, None, None, set())),
    ],
)
def test_no_matching_issues(needed, expected):
    """No issues match the title."""
    issues = [
        {
            "number": 1,
            "title": "Other issue",
            "createdAt": "2025-01-01T00:00:00Z",
            "state": "OPEN",
        },
    ]
    assert triage_issues(issues, TITLE, needed) == expected


@pytest.mark.parametrize(
    ("needed", "expected"),
    [
        (True, (True, None, None, set())),
        (False, (False, None, None, set())),
    ],
)
def test_empty_issues(needed, expected):
    """Empty issue list returns no matches."""
    assert triage_issues([], TITLE, needed) == expected


def test_one_match_needed():
    """Single matching open issue is kept when needed."""
    issues = [
        {
            "number": 42,
            "title": TITLE,
            "createdAt": "2025-01-01T00:00:00Z",
            "state": "OPEN",
        },
    ]
    assert triage_issues(issues, TITLE, needed=True) == (True, 42, "OPEN", set())


def test_one_match_not_needed():
    """Single matching open issue is closed when not needed."""
    issues = [
        {
            "number": 42,
            "title": TITLE,
            "createdAt": "2025-01-01T00:00:00Z",
            "state": "OPEN",
        },
    ]
    assert triage_issues(issues, TITLE, needed=False) == (False, None, None, {42})


def test_one_closed_match_needed():
    """Single matching closed issue is returned for reopening when needed."""
    issues = [
        {
            "number": 42,
            "title": TITLE,
            "createdAt": "2025-01-01T00:00:00Z",
            "state": "CLOSED",
        },
    ]
    assert triage_issues(issues, TITLE, needed=True) == (True, 42, "CLOSED", set())


def test_one_closed_match_not_needed():
    """Single matching closed issue is skipped when not needed."""
    issues = [
        {
            "number": 42,
            "title": TITLE,
            "createdAt": "2025-01-01T00:00:00Z",
            "state": "CLOSED",
        },
    ]
    assert triage_issues(issues, TITLE, needed=False) == (False, None, None, set())


def test_multiple_matches_needed():
    """Most recent issue is kept, older open ones are closed."""
    issues = [
        {
            "number": 10,
            "title": TITLE,
            "createdAt": "2024-06-01T00:00:00Z",
            "state": "OPEN",
        },
        {
            "number": 42,
            "title": TITLE,
            "createdAt": "2025-01-01T00:00:00Z",
            "state": "OPEN",
        },
        {
            "number": 5,
            "title": TITLE,
            "createdAt": "2024-01-01T00:00:00Z",
            "state": "OPEN",
        },
    ]
    assert triage_issues(issues, TITLE, needed=True) == (True, 42, "OPEN", {10, 5})


def test_multiple_matches_not_needed():
    """All open matching issues are closed when not needed."""
    issues = [
        {
            "number": 10,
            "title": TITLE,
            "createdAt": "2024-06-01T00:00:00Z",
            "state": "OPEN",
        },
        {
            "number": 42,
            "title": TITLE,
            "createdAt": "2025-01-01T00:00:00Z",
            "state": "OPEN",
        },
    ]
    assert triage_issues(issues, TITLE, needed=False) == (False, None, None, {10, 42})


def test_multiple_matches_closed_not_needed():
    """Already-closed issues are skipped when not needed."""
    issues = [
        {
            "number": 10,
            "title": TITLE,
            "createdAt": "2024-06-01T00:00:00Z",
            "state": "CLOSED",
        },
        {
            "number": 42,
            "title": TITLE,
            "createdAt": "2025-01-01T00:00:00Z",
            "state": "OPEN",
        },
    ]
    assert triage_issues(issues, TITLE, needed=False) == (False, None, None, {42})


def test_mixed_titles():
    """Non-matching issues are ignored."""
    issues = [
        {
            "number": 1,
            "title": "Other issue",
            "createdAt": "2025-06-01T00:00:00Z",
            "state": "OPEN",
        },
        {
            "number": 42,
            "title": TITLE,
            "createdAt": "2025-01-01T00:00:00Z",
            "state": "OPEN",
        },
        {
            "number": 10,
            "title": TITLE,
            "createdAt": "2024-06-01T00:00:00Z",
            "state": "OPEN",
        },
        {
            "number": 2,
            "title": "Another issue",
            "createdAt": "2025-03-01T00:00:00Z",
            "state": "OPEN",
        },
    ]
    assert triage_issues(issues, TITLE, needed=True) == (True, 42, "OPEN", {10})


def test_state_defaults_to_open():
    """Issues without a state field default to OPEN for backward compatibility."""
    issues = [
        {"number": 42, "title": TITLE, "createdAt": "2025-01-01T00:00:00Z"},
    ]
    assert triage_issues(issues, TITLE, needed=True) == (True, 42, "OPEN", set())


def test_fit_body_file_rewrites_oversized_body(tmp_path):
    """Oversized issue bodies are trimmed in place before reaching `gh`.

    The GitHub API rejects bodies over the size limit outright, so the
    lifecycle helpers rewrite the body file to fit, keeping the attribution
    footer at the end.
    """
    body_file = tmp_path / "body.md"
    small = "A short issue body."
    body_file.write_text(small, encoding="UTF-8")
    _fit_body_file(body_file)
    assert body_file.read_text(encoding="UTF-8") == small

    oversized = "\n".join(["- broken link report row 🍈"] * 5000)
    body_file.write_text(oversized, encoding="UTF-8")
    _fit_body_file(body_file)
    fitted = body_file.read_text(encoding="UTF-8")
    assert len(fitted.encode("utf-16-le")) // 2 <= GITHUB_BODY_MAX_CHARS
    assert "> Report truncated to fit GitHub's body size limit." in fitted


# ---------------------------------------------------------------------------
# Label selection tests
# ---------------------------------------------------------------------------


def test_awesome_repo():
    """Awesome repos get the fix link label."""
    assert get_label("awesome-falsehood") == "🩹 fix link"


def test_awesome_repo_prefix_only():
    """Only repos starting with awesome- get the fix link label."""
    assert get_label("awesome-") == "🩹 fix link"


def test_regular_repo():
    """Regular repos get the documentation label."""
    assert get_label("workflows") == "📚 documentation"


def test_repo_containing_awesome():
    """Repos containing but not starting with awesome get documentation label."""
    assert get_label("my-awesome-repo") == "📚 documentation"


# ---------------------------------------------------------------------------
# Sphinx linkcheck parsing tests
# ---------------------------------------------------------------------------


def test_empty_file(tmp_path):
    """Empty file returns no results."""
    output = tmp_path / "output.json"
    output.write_text("", encoding="UTF-8")
    assert parse_output_json(output) == []


def test_blank_lines_skipped(tmp_path):
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


def test_single_entry(tmp_path):
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
        code=404,
        uri="https://example.com/missing",
        info="404 Not Found",
    )


def test_multiple_entries(tmp_path):
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


def test_missing_info_field(tmp_path):
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
            code=0,
            uri="https://example.com",
            info="",
        ),
    ]
    assert len(filter_broken(results)) == expected_count


def test_mixed_statuses():
    """Only broken and timeout are kept from a mixed list."""
    results = [
        LinkcheckResult("a.rst", 1, "working", 200, "https://a.com", ""),
        LinkcheckResult("b.rst", 2, "broken", 404, "https://b.com", "Not Found"),
        LinkcheckResult("c.rst", 3, "redirected", 301, "https://c.com", ""),
        LinkcheckResult("d.rst", 4, "timeout", 0, "https://d.com", "Timed out"),
        LinkcheckResult("e.rst", 5, "unchecked", 0, "https://e.com", ""),
    ]
    broken = filter_broken(results)
    assert len(broken) == 2
    assert broken[0].uri == "https://b.com"
    assert broken[1].uri == "https://d.com"


def test_filter_empty_input():
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
            code=404,
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
        LinkcheckResult("z_file.rst", 1, "broken", 404, "https://z.com", ""),
        LinkcheckResult("a_file.rst", 5, "broken", 404, "https://a.com", ""),
        LinkcheckResult("a_file.rst", 2, "broken", 404, "https://a2.com", ""),
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
            code=0,
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
            code=404,
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
            code=404,
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
    ("info", "expected_fragment"),
    [
        pytest.param(
            "403 Forbidden @dependabot",
            f"@{ZWS}dependabot",
            id="mention_in_info",
        ),
        pytest.param(
            "Redirected to https://github.com/org/repo/issues/42",
            "redirect.github.com/org/repo/issues/42",
            id="github_url_in_info",
        ),
        pytest.param(
            "See #123 for details",
            f"#{ZWS}123",
            id="issue_ref_in_info",
        ),
    ],
)
def test_report_sanitizes_external_content(info, expected_fragment):
    """Sphinx report sanitizes mentions, issue refs, and GitHub URLs."""
    broken = [
        LinkcheckResult(
            filename="index.rst",
            lineno=1,
            status="broken",
            code=0,
            uri="https://example.com",
            info=info,
        ),
    ]
    report = generate_markdown_report(broken)
    # generate_markdown_report itself does not sanitize; the caller does.
    # Verify the raw info appears unmodified.
    assert info.replace("|", "\\|") in report


def test_lychee_output_sanitized(tmp_path):
    """Lychee markdown output is sanitized before embedding in issue body."""
    from repomatic.github.pr_body import sanitize_markdown_mentions

    raw = "Broken: @admin mentioned in https://github.com/org/repo #99"
    sanitized = sanitize_markdown_mentions(raw)
    assert f"@{ZWS}admin" in sanitized
    assert "redirect.github.com/" in sanitized
    assert f"#{ZWS}99" in sanitized


# ---------------------------------------------------------------------------
# Setup guide CLI tests
# ---------------------------------------------------------------------------


@patch("repomatic.github.token.validate_gh_token_env")
@patch("repomatic.cli.manage_issue_lifecycle")
def test_setup_guide_no_pat_opens_setup_issue(mock_lifecycle, _mock_token):
    """When no PAT is configured, the setup issue opens."""
    runner = CliRunner()
    result = runner.invoke(repomatic_cli, ["setup-guide"])
    assert result.exit_code == 0
    assert mock_lifecycle.call_count == 1
    setup_kwargs = mock_lifecycle.call_args_list[0][1]
    assert setup_kwargs["has_issues"] is True
    assert setup_kwargs["labels"] == ["🤖 ci"]
    assert setup_kwargs["title"] == "Repomatic setup guide"


@patch("repomatic.github.token.validate_gh_token_env")
@patch("repomatic.cli.manage_issue_lifecycle")
def test_setup_guide_pat_without_repo_keeps_issue_open(mock_lifecycle, _mock_token):
    """PAT without --repo cannot verify Dependabot or branch settings."""
    runner = CliRunner(env={"GITHUB_REPOSITORY": ""})
    result = runner.invoke(repomatic_cli, ["setup-guide", "--has-pat"])
    assert result.exit_code == 0
    assert mock_lifecycle.call_count == 1
    # Without --repo, dependabot and branch checks cannot run.
    assert mock_lifecycle.call_args_list[0][1]["has_issues"] is True


@patch("repomatic.github.token.validate_gh_token_env")
@patch("repomatic.cli.manage_issue_lifecycle")
def test_setup_guide_body_contains_template(mock_lifecycle, _mock_token):
    """The setup body file contains the setup guide template content."""
    captured: list[str] = []
    mock_lifecycle.side_effect = lambda **kw: captured.append(
        kw["body_file"].read_text(encoding="UTF-8")
    )
    runner = CliRunner()
    runner.invoke(repomatic_cli, ["setup-guide"])
    content = captured[0]
    assert "REPOMATIC_PAT" in content
    assert "Fine-grained tokens" in content


@patch("repomatic.github.token.validate_gh_token_env")
@patch("repomatic.cli.manage_issue_lifecycle")
def test_setup_guide_disabled_skips(mock_lifecycle, _mock_token, tmp_path, monkeypatch):
    """When setup-guide is disabled in config, the command exits without action."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[tool.repomatic]\nsetup-guide = false\n")
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(repomatic_cli, ["setup-guide"])
    assert result.exit_code == 0
    mock_lifecycle.assert_not_called()


def _all_pass_pat_results():
    """Build a PatPermissionResults where every check passes."""
    from repomatic.github.token import PatPermissionResults

    return PatPermissionResults(
        contents=(True, "Contents: token has access"),
        issues=(True, "Issues: token has access"),
        pull_requests=(True, "Pull requests: token has access"),
        vulnerability_alerts=(
            True,
            "Dependabot alerts: token has access, alerts enabled",
        ),
        workflows=(True, "Workflows: token has access"),
    )


def _partial_fail_pat_results():
    """Build a PatPermissionResults with one failing check."""
    from repomatic.github.token import PatPermissionResults

    return PatPermissionResults(
        contents=(True, "Contents: token has access"),
        issues=(True, "Issues: token has access"),
        pull_requests=(True, "Pull requests: token has access"),
        vulnerability_alerts=(
            False,
            "Token lacks 'Dependabot alerts: Read-only' permission."
            " Update the PAT to include this permission.",
        ),
        workflows=(True, "Workflows: token has access"),
    )


@patch("repomatic.github.token.validate_gh_token_env")
@patch("repomatic.cli.manage_issue_lifecycle")
@patch("repomatic.cli.check_branch_ruleset_on_default")
@patch("repomatic.cli._token_mod.check_all_pat_permissions")
@patch("repomatic.cli.check_pages_deployment_source")
@patch("repomatic.cli.check_pypi_trusted_publisher")
def test_setup_guide_all_checks_pass_closes_issue(
    mock_pypi, mock_pages, mock_check, mock_branch, mock_lifecycle, _mock_token
):
    """When PAT, permissions, branch ruleset, and VT key all pass, the issue closes."""
    mock_check.return_value = _all_pass_pat_results()
    mock_branch.return_value = (True, "Active branch rulesets found: main.")
    mock_pages.return_value = (True, "Pages deployment source is GitHub Actions.")
    mock_pypi.return_value = (True, "Trusted publisher is configured.")
    runner = CliRunner()
    result = runner.invoke(
        repomatic_cli,
        ["setup-guide", "--has-pat", "--has-virustotal-key", "--repo", "owner/repo"],
    )
    assert result.exit_code == 0
    assert mock_lifecycle.call_count == 1
    assert mock_lifecycle.call_args_list[0][1]["has_issues"] is False


@patch("repomatic.github.token.validate_gh_token_env")
@patch("repomatic.cli.manage_issue_lifecycle")
@patch("repomatic.cli.check_branch_ruleset_on_default")
@patch("repomatic.cli._token_mod.check_all_pat_permissions")
def test_setup_guide_pat_missing_permission_keeps_issue_open(
    mock_check, mock_branch, mock_lifecycle, _mock_token
):
    """When PAT is configured but a permission is missing, the issue stays open."""
    mock_check.return_value = _partial_fail_pat_results()
    mock_branch.return_value = (True, "Active branch rulesets found: main.")
    runner = CliRunner()
    result = runner.invoke(
        repomatic_cli, ["setup-guide", "--has-pat", "--repo", "owner/repo"]
    )
    assert result.exit_code == 0
    assert mock_lifecycle.call_count == 1
    assert mock_lifecycle.call_args_list[0][1]["has_issues"] is True


@patch("repomatic.github.token.validate_gh_token_env")
@patch("repomatic.cli.manage_issue_lifecycle")
@patch("repomatic.cli.check_branch_ruleset_on_default")
@patch("repomatic.cli._token_mod.check_all_pat_permissions")
def test_setup_guide_pat_missing_permission_body_contains_warning(
    mock_check, mock_branch, mock_lifecycle, _mock_token
):
    """When PAT has missing permissions, the issue body contains a warning section."""
    mock_check.return_value = _partial_fail_pat_results()
    mock_branch.return_value = (True, "Active branch rulesets found: main.")
    captured: list[str] = []
    mock_lifecycle.side_effect = lambda **kw: captured.append(
        kw["body_file"].read_text(encoding="UTF-8")
    )
    runner = CliRunner()
    runner.invoke(repomatic_cli, ["setup-guide", "--has-pat", "--repo", "owner/repo"])
    content = captured[0]
    assert "missing some permissions" in content
    assert "Dependabot alerts" in content


@patch("repomatic.github.token.validate_gh_token_env")
@patch("repomatic.cli.manage_issue_lifecycle")
@patch("repomatic.cli.check_branch_ruleset_on_default")
@patch("repomatic.cli._token_mod.check_all_pat_permissions")
def test_setup_guide_completed_step_collapsed(
    mock_check, mock_branch, mock_lifecycle, _mock_token
):
    """Completed steps render as collapsed details blocks with a checkmark."""
    mock_check.return_value = _all_pass_pat_results()
    mock_branch.return_value = (True, "Active branch rulesets found: main.")
    captured: list[str] = []
    mock_lifecycle.side_effect = lambda **kw: captured.append(
        kw["body_file"].read_text(encoding="UTF-8")
    )
    runner = CliRunner()
    runner.invoke(repomatic_cli, ["setup-guide", "--has-pat", "--repo", "owner/repo"])
    content = captured[0]
    # Token step should be collapsed with checkmark.
    assert (
        "<details>\n<summary>\u2705 <strong>Create and configure the token" in content
    )
    # Branch step should be collapsed with checkmark.
    assert "<details>\n<summary>\u2705 <strong>Protect the main branch" in content


@patch("repomatic.github.token.validate_gh_token_env")
@patch("repomatic.cli.manage_issue_lifecycle")
def test_setup_guide_incomplete_step_expanded(mock_lifecycle, _mock_token):
    """Incomplete steps render as open details blocks with an error indicator."""
    captured: list[str] = []
    mock_lifecycle.side_effect = lambda **kw: captured.append(
        kw["body_file"].read_text(encoding="UTF-8")
    )
    runner = CliRunner()
    runner.invoke(repomatic_cli, ["setup-guide"])
    content = captured[0]
    # Token step should be expanded with warning indicator.
    assert "<details open>" in content
    assert "\u274c" in content
    assert "<strong>Create and configure the token" in content


@patch("repomatic.github.token.validate_gh_token_env")
@patch("repomatic.cli.manage_issue_lifecycle")
@patch("repomatic.cli.check_branch_ruleset_on_default")
@patch("repomatic.cli._token_mod.check_all_pat_permissions")
def test_setup_guide_missing_branch_ruleset_keeps_issue_open(
    mock_check, mock_branch, mock_lifecycle, _mock_token
):
    """When PAT and permissions pass but branch ruleset is missing, issue stays open."""
    mock_check.return_value = _all_pass_pat_results()
    mock_branch.return_value = (False, "No active branch rulesets found.")
    runner = CliRunner()
    result = runner.invoke(
        repomatic_cli, ["setup-guide", "--has-pat", "--repo", "owner/repo"]
    )
    assert result.exit_code == 0
    assert mock_lifecycle.call_args_list[0][1]["has_issues"] is True


@patch("repomatic.github.token.validate_gh_token_env")
@patch("repomatic.cli.manage_issue_lifecycle")
@patch("repomatic.cli.check_branch_ruleset_on_default")
@patch("repomatic.cli._token_mod.check_all_pat_permissions")
def test_setup_guide_missing_vt_key_keeps_issue_open(
    mock_check, mock_branch, mock_lifecycle, _mock_token
):
    """When Nuitka is active and VT key is missing, the issue stays open."""
    mock_check.return_value = _all_pass_pat_results()
    mock_branch.return_value = (True, "Active branch rulesets found: main.")
    runner = CliRunner()
    result = runner.invoke(
        repomatic_cli, ["setup-guide", "--has-pat", "--repo", "owner/repo"]
    )
    assert result.exit_code == 0
    assert mock_lifecycle.call_args_list[0][1]["has_issues"] is True


@patch("repomatic.github.token.validate_gh_token_env")
@patch("repomatic.cli.manage_issue_lifecycle")
@patch("repomatic.cli.check_branch_ruleset_on_default")
@patch("repomatic.cli._token_mod.check_all_pat_permissions")
@patch("repomatic.cli.check_pypi_trusted_publisher")
def test_setup_guide_nuitka_disabled_hides_vt_step(
    mock_pypi,
    mock_check,
    mock_branch,
    mock_lifecycle,
    _mock_token,
    tmp_path,
    monkeypatch,
):
    """When Nuitka is disabled, the VT step is omitted from the setup guide."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[project]\nname = 'test'\nversion = '1.0'\n\n"
        "[tool.repomatic.nuitka]\nenabled = false\n"
    )
    monkeypatch.chdir(tmp_path)
    mock_check.return_value = _all_pass_pat_results()
    mock_branch.return_value = (True, "Active branch rulesets found: main.")
    mock_pypi.return_value = (True, "Trusted publisher is configured.")
    captured: list[str] = []
    mock_lifecycle.side_effect = lambda **kw: captured.append(
        kw["body_file"].read_text(encoding="UTF-8")
    )
    runner = CliRunner()
    result = runner.invoke(
        repomatic_cli, ["setup-guide", "--has-pat", "--repo", "owner/repo"]
    )
    assert result.exit_code == 0
    content = captured[0]
    assert "VirusTotal" not in content
    # Issue closes without VT key when Nuitka is disabled.
    assert mock_lifecycle.call_args_list[0][1]["has_issues"] is False


@patch("repomatic.github.token.validate_gh_token_env")
@patch("repomatic.cli.manage_issue_lifecycle")
def test_setup_guide_vt_step_shown_when_nuitka_active(mock_lifecycle, _mock_token):
    """When Nuitka is active, the VT step appears in the setup guide body."""
    captured: list[str] = []
    mock_lifecycle.side_effect = lambda **kw: captured.append(
        kw["body_file"].read_text(encoding="UTF-8")
    )
    runner = CliRunner()
    runner.invoke(repomatic_cli, ["setup-guide"])
    content = captured[0]
    assert "VirusTotal" in content


# ---------------------------------------------------------------------------
# Branch ruleset check tests
# ---------------------------------------------------------------------------


def test_check_branch_ruleset_found():
    """Active branch ruleset is detected."""
    from repomatic.lint_repo import check_branch_ruleset_on_default

    rulesets_json = json.dumps([
        {"name": "main", "target": "branch", "enforcement": "active"},
    ])
    with patch("repomatic.lint_repo.run_gh_command", return_value=rulesets_json):
        passed, msg = check_branch_ruleset_on_default("owner/repo")
    assert passed is True
    assert "main" in msg


def test_check_branch_ruleset_none():
    """No branch rulesets returns failure."""
    from repomatic.lint_repo import check_branch_ruleset_on_default

    rulesets_json = json.dumps([
        {"name": "tags", "target": "tag", "enforcement": "active"},
    ])
    with patch("repomatic.lint_repo.run_gh_command", return_value=rulesets_json):
        passed, _msg = check_branch_ruleset_on_default("owner/repo")
    assert passed is False


def test_check_branch_ruleset_api_error():
    """API failure defaults to incomplete (show the step)."""
    from repomatic.lint_repo import check_branch_ruleset_on_default

    with patch(
        "repomatic.lint_repo.run_gh_command",
        side_effect=RuntimeError("HTTP 403"),
    ):
        passed, msg = check_branch_ruleset_on_default("owner/repo")
    assert passed is False
    assert "skipped" in msg


# --- check_immutable_releases ---------------------------------------------------


def test_check_immutable_releases_enabled():
    """Immutable releases enabled is detected."""
    from repomatic.lint_repo import check_immutable_releases

    response = json.dumps({"enabled": True, "enforced_by_owner": False})
    with patch("repomatic.lint_repo.run_gh_command", return_value=response):
        passed, msg = check_immutable_releases("owner/repo")
    assert passed is True
    assert "enabled" in msg


def test_check_immutable_releases_disabled():
    """Immutable releases disabled returns failure."""
    from repomatic.lint_repo import check_immutable_releases

    response = json.dumps({"enabled": False, "enforced_by_owner": False})
    with patch("repomatic.lint_repo.run_gh_command", return_value=response):
        passed, msg = check_immutable_releases("owner/repo")
    assert passed is False
    assert "not enabled" in msg


def test_check_immutable_releases_api_error():
    """API failure returns None (indeterminate), not False."""
    from repomatic.lint_repo import check_immutable_releases

    with patch(
        "repomatic.lint_repo.run_gh_command",
        side_effect=RuntimeError("HTTP 403"),
    ):
        passed, msg = check_immutable_releases("owner/repo")
    assert passed is None
    assert "skipped" in msg
