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

"""Tests for `.gitignore` generation from gitignore.io templates."""

from __future__ import annotations

from unittest.mock import patch
from urllib.error import URLError

import pytest
from click_extra.testing import CliRunner

from repomatic.cli.main import repomatic
from repomatic.config import Config, GitignoreConfig
from repomatic.gitignore import (
    GITIGNORE_BASE_CATEGORIES,
    GITIGNORE_IO_URL,
    build_gitignore,
    orphaned_rules,
    parse_rules,
)
from repomatic.http import FetchError
from tests.conftest import FakeResponse

GENERATED = "*.pyc\n*.egg\n"
"""What the stubbed gitignore.io fetch builds, in the `sync-gitignore` tests."""


def _config(extra_categories: tuple[str, ...] = (), extra_content: str = "") -> Config:
    """Build a Config carrying just the `gitignore` fields under test."""
    return Config(
        gitignore=GitignoreConfig(
            extra_categories=list(extra_categories),
            extra_content=extra_content,
        ),
    )


def test_build_gitignore_requests_base_categories():
    """The base categories are fetched and the response body is returned."""
    body = b"# managed by repomatic\n*.log\n"
    with patch(
        "repomatic.http.urlopen", return_value=FakeResponse(body)
    ) as mock_urlopen:
        content = build_gitignore(_config())

    assert content == body.decode("UTF-8")
    request = mock_urlopen.call_args.args[0]
    expected_url = f"{GITIGNORE_IO_URL}/{','.join(GITIGNORE_BASE_CATEGORIES)}"
    assert request.full_url == expected_url
    # The socket timeout is part of the contract: a stalled fetch must fail,
    # not hang the sync.
    assert mock_urlopen.call_args.kwargs["timeout"] == 10
    assert request.get_header("User-agent").startswith("repomatic/")


def test_build_gitignore_merges_and_dedupes_extra_categories():
    """Extra categories append in order, and duplicates of a base one drop."""
    # "python" is already a base category (deduplicated); "papaya" is new.
    config = _config(extra_categories=("python", "papaya"))
    with patch(
        "repomatic.http.urlopen", return_value=FakeResponse(b"content")
    ) as mock_urlopen:
        build_gitignore(config)

    request = mock_urlopen.call_args.args[0]
    expected_categories = [*GITIGNORE_BASE_CATEGORIES, "papaya"]
    assert request.full_url == f"{GITIGNORE_IO_URL}/{','.join(expected_categories)}"


def test_build_gitignore_appends_extra_content():
    """Configured extra content is appended after the fetched template."""
    config = _config(extra_content="# custom\n*.tmp")
    with patch("repomatic.http.urlopen", return_value=FakeResponse(b"base\n")):
        content = build_gitignore(config)

    assert content == "base\n\n# custom\n*.tmp\n"


def test_build_gitignore_propagates_fetch_error():
    """A failed gitignore.io fetch surfaces to the caller, not silently empty."""
    with (
        patch("repomatic.http.urlopen", side_effect=URLError("unreachable")),
        pytest.raises(FetchError),
    ):
        build_gitignore(_config())


@pytest.mark.parametrize(
    ("content", "expected"),
    (
        pytest.param("", [], id="empty"),
        pytest.param("\n  \n\n", [], id="blank-lines-only"),
        pytest.param("# just a comment\n", [], id="comment-only"),
        pytest.param("*.log\n", ["*.log"], id="single-rule"),
        pytest.param("  *.log  \n", ["*.log"], id="surrounding-whitespace"),
        pytest.param("*.log\n*.log\n", ["*.log"], id="duplicates-collapse"),
        pytest.param("b\na\n", ["b", "a"], id="order-preserved"),
        pytest.param("!*.svg\n", ["!*.svg"], id="negation-is-a-rule"),
        # git reads `#` as a comment opener only in first position, so a hash
        # further along the line belongs to the pattern.
        pytest.param("foo#bar\n", ["foo#bar"], id="hash-mid-line-kept"),
        pytest.param("\\#literal\n", ["\\#literal"], id="escaped-hash-kept"),
    ),
)
def test_parse_rules(content, expected):
    """Only the lines git matches paths against survive parsing."""
    assert parse_rules(content) == expected


def test_orphaned_rules_reports_what_the_sync_would_lose():
    """A rule on disk and absent from the rebuild is reported."""
    existing = "# Pelican\n*.pid\noutput\nseo_report.html\n"
    generated = "# base\n*.pid\n"
    assert orphaned_rules(existing, generated) == ["output", "seo_report.html"]


@pytest.mark.parametrize(
    ("existing", "generated"),
    (
        pytest.param("", "*.log\n", id="nothing-on-disk"),
        pytest.param("*.log\n", "*.log\n", id="identical"),
        pytest.param("*.log\n", "*.log\n*.tmp\n", id="generated-is-a-superset"),
        # Layout is not content: a new header and a reorder drop no rule.
        pytest.param(
            "# old header\nb\na\n", "# new header\n\na\nb\n", id="reordered-new-header"
        ),
    ),
)
def test_orphaned_rules_empty_when_nothing_is_lost(existing, generated):
    """Rewrites that keep every rule report no orphan, whatever the layout."""
    assert orphaned_rules(existing, generated) == []


@pytest.mark.parametrize(
    ("on_disk", "args"),
    (
        pytest.param(None, (), id="first-run-at-the-default-location"),
        pytest.param("*.pyc\n", ("--output", ".gitignore"), id="nothing-lost"),
        pytest.param(
            "*.pyc\npapaya.log\n", ("--drop-orphans",), id="hand-added-rule-dropped"
        ),
    ),
)
def test_sync_gitignore_writes_the_generated_file(tmp_path, monkeypatch, on_disk, args):
    """The sync writes the whole file when no rule on disk is lost, or when told."""
    gitignore = tmp_path / ".gitignore"
    if on_disk is not None:
        gitignore.write_text(on_disk, encoding="UTF-8")
    monkeypatch.chdir(tmp_path)

    with patch("repomatic.cli.repo.build_gitignore", return_value=GENERATED):
        result = CliRunner().invoke(repomatic, ["sync-gitignore", *args])

    assert result.exit_code == 0, result.output
    assert gitignore.read_text(encoding="UTF-8") == GENERATED


def test_sync_gitignore_refuses_to_drop_a_hand_added_rule(tmp_path, monkeypatch):
    """A rule on disk that the generated file lacks stops the write, by name."""
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("*.pyc\npapaya.log\n", encoding="UTF-8")
    monkeypatch.chdir(tmp_path)

    with patch("repomatic.cli.repo.build_gitignore", return_value=GENERATED):
        result = CliRunner().invoke(repomatic, ["sync-gitignore"])

    assert result.exit_code == 1, result.output
    assert "1 rule(s) on disk are absent from the generated file" in result.output
    assert "  papaya.log\n" in result.output
    assert gitignore.read_text(encoding="UTF-8") == "*.pyc\npapaya.log\n"
