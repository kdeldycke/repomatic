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

"""Tests for the self-hosted version-sync foundation."""

from __future__ import annotations

import glob
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from repomatic import version_sync as vs
from repomatic.cli import repomatic
from repomatic.github.releases import GitHubRelease, GitHubReleasesUnavailable
from repomatic.pypi import PyPIRelease
from repomatic.tool_runner import TOOL_REGISTRY

TODAY = date(2026, 6, 27)


# ---------------------------------------------------------------------------
# Cooldown parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("8 days", timedelta(days=8)),
        ("1 day", timedelta(days=1)),
        ("2 weeks", timedelta(weeks=2)),
        ("36 hours", timedelta(hours=36)),
        ("0 days", timedelta(0)),
    ],
)
def test_parse_min_age(value, expected):
    assert vs.parse_min_age(value) == expected


@pytest.mark.parametrize("value", ["forever", "", "3 months", "garbage"])
def test_parse_min_age_unrecognized_is_no_cooldown(value):
    assert vs.parse_min_age(value) == timedelta(0)


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("new", "old", "expected"),
    [
        ("1.2.0", "1.1.0", True),
        ("1.1.0", "1.1.0", False),
        ("1.0.0", "1.1.0", False),
        ("garbage", "1.0.0", False),
    ],
)
def test_is_newer(new, old, expected):
    assert vs.is_newer(new, old) is expected


# ---------------------------------------------------------------------------
# Release selection
# ---------------------------------------------------------------------------


def test_select_latest_respects_cooldown():
    """The highest version that has cleared the cooldown wins."""
    candidates = [
        vs.Candidate("1.0.0", "2026-01-01", "v1.0.0"),
        vs.Candidate("1.2.0", "2026-06-26", "v1.2.0"),  # 1 day old: held back.
        vs.Candidate("1.1.0", "2026-06-01", "v1.1.0"),  # 26 days old: eligible.
    ]
    picked = vs.select_latest(candidates, timedelta(days=8), TODAY)
    assert picked is not None and picked.version == "1.1.0"


def test_select_latest_skips_prereleases_by_default():
    candidates = [
        vs.Candidate("1.1.0", "2026-01-01", "v1.1.0"),
        vs.Candidate("2.0.0rc1", "2026-01-01", "v2.0.0rc1"),
    ]
    picked = vs.select_latest(candidates, timedelta(days=8), TODAY)
    assert picked is not None and picked.version == "1.1.0"
    allowed = vs.select_latest(
        candidates, timedelta(days=8), TODAY, allow_prerelease=True
    )
    assert allowed is not None and allowed.version == "2.0.0rc1"


def test_select_latest_none_when_all_too_new():
    candidates = [vs.Candidate("1.0.0", "2026-06-26", "v1.0.0")]
    assert vs.select_latest(candidates, timedelta(days=8), TODAY) is None


def test_select_latest_skips_unparsable_versions_and_dates():
    candidates = [
        vs.Candidate("not-a-version", "2026-01-01", "x"),
        vs.Candidate("1.0.0", "not-a-date", "v1.0.0"),
        vs.Candidate("1.1.0", "2026-01-01", "v1.1.0"),
    ]
    picked = vs.select_latest(candidates, timedelta(days=8), TODAY)
    assert picked is not None and picked.version == "1.1.0"


def test_select_latest_empty():
    assert vs.select_latest([], timedelta(days=8), TODAY) is None


# ---------------------------------------------------------------------------
# Held-back selection and cooldown note
# ---------------------------------------------------------------------------


def test_select_held_back_returns_highest_in_cooldown_above_pinned():
    candidates = [
        vs.Candidate("1.1.0", "2026-06-01", "v1.1.0"),  # eligible, == pinned.
        vs.Candidate("1.2.0", "2026-06-24", "v1.2.0"),  # 3 days old: held back.
        vs.Candidate("1.3.0", "2026-06-26", "v1.3.0"),  # 1 day old: held back, higher.
    ]
    held = vs.select_held_back(candidates, "1.1.0", timedelta(days=8), TODAY)
    assert held is not None and held.version == "1.3.0"


def test_select_held_back_none_when_nothing_newer_in_cooldown():
    candidates = [
        vs.Candidate("1.0.0", "2026-01-01", "v1.0.0"),
        vs.Candidate("1.1.0", "2026-06-01", "v1.1.0"),  # eligible, not held back.
    ]
    assert vs.select_held_back(candidates, "1.1.0", timedelta(days=8), TODAY) is None


def test_select_held_back_ignores_versions_at_or_below_pinned():
    # A recent (in-cooldown) patch of an older line must not surface as held back.
    candidates = [vs.Candidate("1.0.1", "2026-06-26", "v1.0.1")]
    assert vs.select_held_back(candidates, "1.1.0", timedelta(days=8), TODAY) is None


def test_select_held_back_skips_prereleases_by_default():
    candidates = [
        vs.Candidate("2.0.0rc1", "2026-06-26", "v2.0.0rc1"),
        vs.Candidate("1.2.0", "2026-06-26", "v1.2.0"),
    ]
    picked = vs.select_held_back(candidates, "1.1.0", timedelta(days=8), TODAY)
    assert picked is not None and picked.version == "1.2.0"
    allowed = vs.select_held_back(
        candidates, "1.1.0", timedelta(days=8), TODAY, allow_prerelease=True
    )
    assert allowed is not None and allowed.version == "2.0.0rc1"


def test_select_held_back_skips_unparsable():
    candidates = [
        vs.Candidate("bad", "2026-06-26", "x"),
        vs.Candidate("1.2.0", "not-a-date", "v1.2.0"),
        vs.Candidate("1.3.0", "2026-06-26", "v1.3.0"),
    ]
    picked = vs.select_held_back(candidates, "1.1.0", timedelta(days=8), TODAY)
    assert picked is not None and picked.version == "1.3.0"


def test_format_cooldown_note():
    note = vs.format_cooldown_note("8 days", date(2026, 6, 20))
    assert "minimum-release-age" in note
    assert "`8 days`" in note
    assert "`2026-06-20`" in note


# ---------------------------------------------------------------------------
# Registry version rewriting (against the real source)
# ---------------------------------------------------------------------------


def test_set_tool_version_targets_one_tool():
    source = Path("repomatic/tool_runner.py").read_text(encoding="UTF-8")
    bumped = vs.set_tool_version(source, "gitleaks", "9.9.9")
    assert bumped.count('version="9.9.9"') == 1
    # A sibling tool is untouched.
    assert 'version="1.7.12"' in bumped
    # Re-applying the same version is a no-op.
    assert vs.set_tool_version(bumped, "gitleaks", "9.9.9") == bumped


# ---------------------------------------------------------------------------
# Action pin discovery and rewriting
# ---------------------------------------------------------------------------


def test_find_action_pins_skips_local_and_subpath_refs():
    content = (
        "      - uses: actions/checkout@" + "a" * 40 + " # v4.0.0\n"
        "      - uses: ./.github/actions/local\n"
        "      - uses: owner/repo/.github/workflows/x.yaml@" + "b" * 40 + " # v1\n"
    )
    pins = vs.find_action_pins(content)
    assert [p.slug for p in pins] == ["actions/checkout"]
    assert pins[0].ref == "v4.0.0"


def test_apply_action_pins_rewrites_sha_and_comment():
    content = "      - uses: actions/checkout@" + "a" * 40 + " # v4.0.0\n"
    new_content, changes = vs.apply_action_pins(
        content, {"actions/checkout": ("c" * 40, "v5.0.0")}
    )
    assert ("actions/checkout", "v4.0.0", "v5.0.0") in changes
    assert "@" + "c" * 40 + " # v5.0.0" in new_content


def test_apply_action_pins_skips_unchanged_sha():
    sha = "a" * 40
    content = "      - uses: actions/checkout@" + sha + " # v4.0.0\n"
    new_content, changes = vs.apply_action_pins(
        content, {"actions/checkout": (sha, "v4.0.0")}
    )
    assert changes == []
    assert new_content == content


# ---------------------------------------------------------------------------
# Workflow literal discovery and rewriting
# ---------------------------------------------------------------------------


def test_find_workflow_literals():
    content = (
        "          npm install awesome-lint@2.3.0\n"
        "          uvx --no-progress 'codecov-cli==11.2.8'\n"
        "          uvx --with 'extra-platforms[test]==13.0.1' run\n"
    )
    literals = {
        (lit.ecosystem, lit.package, lit.version)
        for lit in vs.find_workflow_literals(content)
    }
    assert ("npm", "awesome-lint", "2.3.0") in literals
    assert ("pypi", "codecov-cli", "11.2.8") in literals
    assert ("pypi", "extra-platforms", "13.0.1") in literals


def test_apply_workflow_literals():
    content = "          uvx --no-progress 'codecov-cli==11.2.8'\n"
    new_content, changes = vs.apply_workflow_literals(
        content, {("pypi", "codecov-cli"): "12.0.0"}
    )
    assert ("codecov-cli", "11.2.8", "12.0.0") in changes
    assert "codecov-cli==12.0.0" in new_content


# ---------------------------------------------------------------------------
# Datasource adapters (mocked: no network)
# ---------------------------------------------------------------------------


def test_github_candidates_extracts_raw_tags():
    fake = {
        "v1.0.0": GitHubRelease(date="2026-01-01", body=""),
        "v1.1.0": GitHubRelease(date="2026-02-01", body=""),
    }
    with patch("repomatic.version_sync.get_release_tags", return_value=fake):
        candidates = vs.github_candidates("https://github.com/owner/repo")
    assert {c.version for c in candidates} == {"1.0.0", "1.1.0"}
    # The raw tag is preserved for SHA resolution.
    assert {c.ref for c in candidates} == {"v1.0.0", "v1.1.0"}


def test_github_candidates_graceful_when_unavailable():
    with patch(
        "repomatic.version_sync.get_release_tags",
        side_effect=GitHubReleasesUnavailable("boom"),
    ):
        assert vs.github_candidates("https://github.com/owner/repo") == []


# ---------------------------------------------------------------------------
# End-to-end PR body wiring
# ---------------------------------------------------------------------------


def test_sync_action_pins_pr_body_has_cutoff_held_back_and_notes():
    """The sync-action-pins PR body carries the cutoff, held-back, and notes.

    Locks the gap PR 2827 surfaced: the three version-sync updaters must render
    the same cooldown cutoff line, `Held back by cooldown` section, and
    `Release notes` dropdown that sync-uv-lock already does. Fixture release
    dates are derived from the current date so the cooldown windows stay valid
    whenever the test runs.
    """
    today = datetime.now(timezone.utc).date()
    tags = {
        "v1.0.0": GitHubRelease(
            date=(today - timedelta(days=400)).isoformat(), body="one"
        ),
        # 30 days old: cleared the 8-day cooldown, so it is adopted.
        "v2.0.0": GitHubRelease(
            date=(today - timedelta(days=30)).isoformat(), body="release two notes"
        ),
        # 2 days old: still inside the cooldown, so it is held back.
        "v3.0.0": GitHubRelease(
            date=(today - timedelta(days=2)).isoformat(), body="release three notes"
        ),
    }
    runner = CliRunner()
    with runner.isolated_filesystem():
        workflow = Path(".github/workflows/ci.yaml")
        workflow.parent.mkdir(parents=True)
        workflow.write_text(
            "jobs:\n  build:\n    steps:\n"
            f"      - uses: owner/repo@{'a' * 40} # v1.0.0\n",
            encoding="UTF-8",
        )
        with (
            # github_candidates resolves get_release_tags in version_sync's
            # namespace; fetch_github_release_notes resolves it in releases'.
            patch("repomatic.version_sync.get_release_tags", return_value=tags),
            patch("repomatic.github.releases.get_release_tags", return_value=tags),
            patch("repomatic.sync_ops.resolve_tag_to_sha", return_value="b" * 40),
        ):
            result = runner.invoke(
                repomatic,
                [
                    "sync-action-pins",
                    "--release-notes",
                    "--output",
                    "out.md",
                    "--output-format",
                    "markdown",
                ],
            )
        assert result.exit_code == 0, result.output
        body = Path("out.md").read_text(encoding="UTF-8")

    # The action is bumped to the eligible v2.0.0.
    assert "### 🆙 Updated actions" in body
    assert "`v1.0.0` → `v2.0.0`" in body
    # The relative cooldown cutoff line (the uv exclude-newer counterpart).
    assert "minimum-release-age" in body
    # The held-back section surfaces the in-cooldown v3.0.0.
    assert "### 🔜 Held back by cooldown" in body
    assert "`3.0.0`" in body
    # Release notes for the adopted version only, not the held-back one.
    assert "### Release notes" in body
    assert "release two notes" in body
    assert "release three notes" not in body
    # Release notes sit between the update table and the held-back section.
    assert (
        body.index("### 🆙 Updated actions")
        < body.index("### Release notes")
        < body.index("### 🔜 Held back by cooldown")
    )


def test_pypi_candidates_skips_yanked():
    fake = {
        "1.0.0": PyPIRelease(date="2026-01-01", yanked=False, package="p"),
        "1.1.0": PyPIRelease(date="2026-02-01", yanked=True, package="p"),
    }
    with patch("repomatic.version_sync.pypi_release_dates", return_value=fake):
        candidates = vs.pypi_candidates("p")
    assert {c.version for c in candidates} == {"1.0.0"}


def test_npm_candidates():
    with patch(
        "repomatic.version_sync.npm_release_dates",
        return_value={"2.3.0": "2026-01-01"},
    ):
        candidates = vs.npm_candidates("awesome-lint")
    assert [(c.version, c.ref) for c in candidates] == [("2.3.0", "2.3.0")]


# ---------------------------------------------------------------------------
# Conformance: every managed surface is covered by a scanner / datasource
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(TOOL_REGISTRY))
def test_registry_tool_has_resolvable_datasource(name):
    """Every registry tool resolves a datasource so sync-tool-versions covers it."""
    spec = TOOL_REGISTRY[name]
    if spec.binary is not None:
        assert spec.source_url and spec.source_url.startswith("https://github.com/")
        owner_repo = spec.source_url.removeprefix("https://github.com/").split("/")
        assert len(owner_repo) == 2 and all(owner_repo)
    else:
        assert spec.package or spec.name


@pytest.mark.parametrize("name", sorted(TOOL_REGISTRY))
def test_registry_tag_pattern_is_valid(name):
    """A `tag_pattern`, when set, compiles and exposes a `version` group."""
    pattern = TOOL_REGISTRY[name].tag_pattern
    if pattern is not None:
        assert "version" in re.compile(pattern).groupindex


def _github_yaml_files() -> list[str]:
    return glob.glob(".github/workflows/*.yaml") + glob.glob(
        ".github/actions/**/*.yaml", recursive=True
    )


def test_every_simple_action_pin_is_discoverable():
    """Every simple `owner/repo@<sha>` pin in .github/ is found by the scanner.

    A pin in a shape `find_action_pins` misses would let `sync-action-pins`
    silently skip an action. Subpath refs (reusable workflows) and local `./`
    refs are excluded by design and asserted absent.
    """
    sha_uses = re.compile(r"uses:\s*(?P<ref>[\w./-]+)@(?P<sha>[0-9a-f]{40})")
    files = _github_yaml_files()
    assert files, "no workflow/action files found"
    for path in files:
        content = Path(path).read_text(encoding="UTF-8")
        found = {pin.slug for pin in vs.find_action_pins(content)}
        for match in sha_uses.finditer(content):
            ref = match.group("ref")
            if ref.count("/") == 1 and not ref.startswith("."):
                assert ref in found, (
                    f"{ref} in {path} not discovered by find_action_pins"
                )
