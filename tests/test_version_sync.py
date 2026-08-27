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
from repomatic.tool_registry import TOOL_REGISTRY

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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("8 days", 8),
        ("1 day", 1),
        ("2 weeks", 14),
        # Sub-day remainders round up so the gate never collapses to 0.
        ("36 hours", 2),
        ("1 hour", 1),
        ("0 days", 0),
        # Unrecognized values yield no cooldown.
        ("garbage", 0),
    ],
)
def test_min_release_age_days(value, expected):
    assert vs.min_release_age_days(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("8 days", "2026-06-23"),
        ("2 weeks", "2026-06-17"),
        # Disabled cooldown and unrecognized values yield no flag.
        ("0 days", None),
        ("garbage", None),
    ],
)
def test_exclude_newer_cutoff(value, expected):
    assert vs.exclude_newer_cutoff(value, date(2026, 7, 1)) == expected


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------


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


def test_select_latest_excludes_the_cutoff_day_itself():
    """A release dated exactly on the cutoff is held back, not adopted.

    Datasources report a release date while uv enforces `--exclude-newer` at
    instant granularity, so a release published on the cutoff day may still be
    younger than `now - min_age`. Adopting it pins a version uv then refuses to
    resolve. This is the shape that broke every binary build: a run at `05:20`
    UTC took a release published at `17:07` on the cutoff date.
    """
    cutoff_day = (TODAY - timedelta(days=8)).isoformat()
    candidates = [
        vs.Candidate("1.0.0", "2026-01-01", "v1.0.0"),
        vs.Candidate("2.0.0", cutoff_day, "v2.0.0"),
    ]
    picked = vs.select_latest(candidates, timedelta(days=8), TODAY)
    assert picked is not None and picked.version == "1.0.0"

    # And it lands in the held-back set rather than vanishing between the two.
    held = vs.select_held_back(candidates, "1.0.0", timedelta(days=8), TODAY)
    assert held is not None and held.version == "2.0.0"


@pytest.mark.parametrize(
    ("offset_days", "adopted"),
    (
        (9, True),  # Comfortably past the window.
        (8, False),  # The cutoff day itself: same-day ambiguity, held back.
        (7, False),  # Inside the window.
    ),
)
def test_cooldown_boundary_partitions_cleanly(offset_days: int, adopted: bool):
    """`select_latest` and `select_held_back` never both take, nor both drop."""
    released = (TODAY - timedelta(days=offset_days)).isoformat()
    candidates = [vs.Candidate("2.0.0", released, "v2.0.0")]
    picked = vs.select_latest(candidates, timedelta(days=8), TODAY)
    held = vs.select_held_back(candidates, "1.0.0", timedelta(days=8), TODAY)
    assert (picked is not None) is adopted
    assert (held is not None) is not adopted


@pytest.mark.parametrize(
    ("offset_days", "flagged"),
    (
        (9, False),  # Cleared the window.
        (8, True),  # The cutoff day itself: same-day ambiguity.
        (2, True),  # Well inside.
    ),
)
def test_pin_inside_cooldown(offset_days: int, flagged: bool):
    """A pin already on disk is judged by the predicate that would write it."""
    released = (TODAY - timedelta(days=offset_days)).isoformat()
    candidates = [vs.Candidate("2.0.0", released, "v2.0.0")]
    stuck = vs.pin_inside_cooldown(candidates, "2.0.0", timedelta(days=8), TODAY)
    assert (stuck is not None) is flagged


def test_pin_inside_cooldown_stays_quiet_on_unknowns():
    """A pin the datasource does not offer, or dates badly, is never flagged."""
    candidates = [vs.Candidate("2.0.0", "2026-01-01", "v2.0.0")]
    assert vs.pin_inside_cooldown(candidates, "9.9.9", timedelta(days=8), TODAY) is None
    undated = [vs.Candidate("2.0.0", "not-a-date", "v2.0.0")]
    assert vs.pin_inside_cooldown(undated, "2.0.0", timedelta(days=8), TODAY) is None


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
    source = Path("repomatic/tool_registry.py").read_text(encoding="UTF-8")
    bumped = vs.set_tool_version(source, "gitleaks", "9.9.9")
    assert bumped.count('version="9.9.9"') == 1
    # A sibling tool is untouched.
    assert 'version="1.7.12"' in bumped
    # Re-applying the same version is a no-op.
    assert vs.set_tool_version(bumped, "gitleaks", "9.9.9") == bumped


def test_set_with_package_version_rewrites_every_occurrence():
    source = (
        "    with_packages=(\n"
        '        "papaya-plugin==1.0.0",\n'
        '        "kiwi-plugin==2.3.4",\n'
        "    ),\n"
        "    other=(\n"
        '        "papaya-plugin==1.0.0",\n'
        "    ),\n"
    )
    bumped = vs.set_with_package_version(source, "papaya-plugin", "1.1.0")
    # Both pins converge, so two tools can never drift apart.
    assert bumped.count('"papaya-plugin==1.1.0"') == 2
    assert '"papaya-plugin==1.0.0"' not in bumped
    # A sibling package is untouched.
    assert '"kiwi-plugin==2.3.4"' in bumped
    # Re-applying the same version is a no-op.
    assert vs.set_with_package_version(bumped, "papaya-plugin", "1.1.0") == bumped


def test_set_with_package_version_does_not_match_a_name_prefix():
    """`mdformat` must not swallow the `mdformat-gfm` pin sitting next to it."""
    source = '"papaya==1.0.0",\n"papaya-plugin==2.0.0",\n'
    bumped = vs.set_with_package_version(source, "papaya", "1.2.0")
    assert '"papaya==1.2.0"' in bumped
    assert '"papaya-plugin==2.0.0"' in bumped


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
        "          uvx --no-progress 'papaya-cli==11.2.8'\n"
        "          uvx --with 'extra-platforms[test]==13.0.1' run\n"
    )
    literals = {
        (lit.ecosystem, lit.package, lit.version)
        for lit in vs.find_workflow_literals(content)
    }
    assert ("npm", "awesome-lint", "2.3.0") in literals
    assert ("pypi", "papaya-cli", "11.2.8") in literals
    assert ("pypi", "extra-platforms", "13.0.1") in literals


@pytest.mark.parametrize(
    ("command", "package", "version"),
    (
        pytest.param(
            "npm install awesome-lint@2.3.0", "awesome-lint", "2.3.0", id="npm-install"
        ),
        pytest.param("npm i awesome-lint@2.3.0", "awesome-lint", "2.3.0", id="npm-i"),
        pytest.param("npm add @scope/pkg@1.2.3", "@scope/pkg", "1.2.3", id="npm-add"),
        # `npx` runs a tool without installing it, which is what a workflow
        # reaches for, so its pins need walking forward like any other.
        pytest.param(
            "npx html-validate@10.1.1 .", "html-validate", "10.1.1", id="npx-bare"
        ),
        pytest.param(
            "npx --yes html-validate@10.1.1 './out/**/*.html'",
            "html-validate",
            "10.1.1",
            id="npx-flag",
        ),
        pytest.param(
            "npx --yes @divriots/jampack@0.34.1 ./output",
            "@divriots/jampack",
            "0.34.1",
            id="npx-scoped",
        ),
    ),
)
def test_find_workflow_literals_npm_commands(command, package, version):
    """Both npm and npx pins are discovered, whatever flags sit in between."""
    literals = {
        (lit.ecosystem, lit.package, lit.version)
        for lit in vs.find_workflow_literals(f"          {command}\n")
    }
    assert ("npm", package, version) in literals


@pytest.mark.parametrize(
    "command",
    (
        pytest.param("npm install awesome-lint", id="npm-unpinned"),
        pytest.param("npx some-tool .", id="npx-unpinned"),
    ),
)
def test_find_workflow_literals_ignores_unpinned_npm(command):
    """A command naming no version has no literal to walk forward."""
    npm = [
        lit
        for lit in vs.find_workflow_literals(f"          {command}\n")
        if lit.ecosystem == "npm"
    ]
    assert npm == []


def test_apply_workflow_literals():
    content = "          uvx --no-progress 'papaya-cli==11.2.8'\n"
    new_content, changes = vs.apply_workflow_literals(
        content, {("pypi", "papaya-cli"): "12.0.0"}
    )
    assert ("papaya-cli", "11.2.8", "12.0.0") in changes
    assert "papaya-cli==12.0.0" in new_content


EXEMPTION = "--exclude-newer-package repomatic=P0D"


@pytest.mark.parametrize(
    ("content", "expected"),
    (
        # The bare downstream pin gains the exemption alongside the new
        # version, spelled exactly as the release freeze writes it so the
        # unfreeze pattern has one shape to recognize.
        (
            "          uvx --no-progress 'repomatic==7.4.1' metadata\n",
            f"          uvx --no-progress {EXEMPTION} 'repomatic==7.6.0' metadata\n",
        ),
        # Idempotent: a command already carrying it is left alone.
        (
            f"          uvx --no-progress {EXEMPTION} 'repomatic==7.4.1' metadata\n",
            f"          uvx --no-progress {EXEMPTION} 'repomatic==7.6.0' metadata\n",
        ),
        # A pin already at the target version still gets the flag backfilled.
        (
            "          uvx --no-progress 'repomatic==7.6.0' pr-body\n",
            f"          uvx --no-progress {EXEMPTION} 'repomatic==7.6.0' pr-body\n",
        ),
    ),
)
def test_apply_workflow_literals_self_pin_exemption(content, expected):
    new_content, _changes = vs.apply_workflow_literals(
        content,
        {("pypi", "repomatic"): "7.6.0"},
        self_pin=("repomatic", EXEMPTION),
    )
    assert new_content == expected


def test_apply_workflow_literals_leaves_other_packages_unexempted():
    """Only the self-pin bypasses the cooldown, so only it earns the flag."""
    content = "          uvx --no-progress 'papaya-cli==11.2.8' upload\n"
    new_content, _changes = vs.apply_workflow_literals(
        content,
        {("pypi", "papaya-cli"): "12.0.0"},
        self_pin=("repomatic", EXEMPTION),
    )
    assert EXEMPTION not in new_content
    assert "papaya-cli==12.0.0" in new_content


def test_find_upstream_ref_versions():
    content = (
        "    uses: kdeldycke/repomatic/.github/workflows/lint.yaml@"
        "36523e5a56f287e814210042ca7b852147a95498 # v7.0.0\n"
        "      - uses: kdeldycke/repomatic/.github/actions/publish-pypi@v6.31.0\n"
        "    uses: other/repo/.github/workflows/tests.yaml@deadbeef # v9.9.9\n"
        "          uvx --no-progress 'repomatic==6.30.0' metadata\n"
    )
    versions = vs.find_upstream_ref_versions(content, "kdeldycke/repomatic")
    # SHA-pinned and tag-pinned refs are both read; foreign repos and inline
    # `==` pins are not.
    assert versions == {"7.0.0", "6.31.0"}


def test_find_upstream_ref_pins_carries_the_sha():
    """A tag-only ref yields a `None` SHA, which `init`'s floor must not invent."""
    content = (
        "    uses: kdeldycke/repomatic/.github/workflows/lint.yaml@"
        "36523e5a56f287e814210042ca7b852147a95498 # v7.0.0\n"
        "      - uses: kdeldycke/repomatic/.github/actions/publish-pypi@v6.31.0\n"
        "    uses: other/repo/.github/workflows/tests.yaml@deadbeef # v9.9.9\n"
    )
    assert vs.find_upstream_ref_pins(content, "kdeldycke/repomatic") == [
        vs.UpstreamRefPin("7.0.0", "36523e5a56f287e814210042ca7b852147a95498"),
        vs.UpstreamRefPin("6.31.0", None),
    ]


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
    assert "## 🆙 Updated actions" in body
    assert "`v1.0.0` → `v2.0.0`" in body
    # The relative cooldown cutoff line (the uv exclude-newer counterpart).
    assert "minimum-release-age" in body
    # The held-back section surfaces the in-cooldown v3.0.0.
    assert "## ⏸️ Held back by cooldown" in body
    assert "`3.0.0`" in body
    # Release notes for the adopted version only, not the held-back one.
    assert "### Release notes" in body
    assert "release two notes" in body
    assert "release three notes" not in body
    # Release notes nest under the update table, above the held-back section.
    assert (
        body.index("## 🆙 Updated actions")
        < body.index("### Release notes")
        < body.index("## ⏸️ Held back by cooldown")
    )


def test_sync_action_pins_converges_mixed_pins_without_eligible_upgrade():
    """Stragglers converge onto the highest pin when no upgrade qualifies.

    Locks the gap kdeldycke/extra-platforms#600 surfaced: with one action
    pinned at two versions, the repo-wide maximum masked the older pins, so
    the held-back table claimed the action was locked at the highest version
    while stale pins stayed on disk. The stragglers must be rewritten to the
    winning pin's SHA without any tag resolution, even when the only newer
    release is still inside the cooldown.
    """
    today = datetime.now(timezone.utc).date()
    tags = {
        # 100 days old: cleared the cooldown, and already the highest pin.
        "v2.0.0": GitHubRelease(
            date=(today - timedelta(days=100)).isoformat(), body="two"
        ),
        # 2 days old: still inside the cooldown, so no upgrade qualifies.
        "v3.0.0": GitHubRelease(
            date=(today - timedelta(days=2)).isoformat(), body="three"
        ),
    }
    runner = CliRunner()
    with runner.isolated_filesystem():
        workflow = Path(".github/workflows/ci.yaml")
        workflow.parent.mkdir(parents=True)
        workflow.write_text(
            "jobs:\n  build:\n    steps:\n"
            f"      - uses: owner/repo@{'a' * 40} # v1.0.0\n"
            f"      - uses: owner/repo@{'c' * 40} # v2.0.0\n",
            encoding="UTF-8",
        )
        with (
            patch("repomatic.version_sync.get_release_tags", return_value=tags),
            patch(
                "repomatic.sync_ops.resolve_tag_to_sha",
                side_effect=AssertionError("convergence must not resolve tags"),
            ),
        ):
            result = runner.invoke(
                repomatic,
                [
                    "sync-action-pins",
                    "--output",
                    "out.md",
                    "--output-format",
                    "markdown",
                ],
            )
        assert result.exit_code == 0, result.output
        content = workflow.read_text(encoding="UTF-8")
        body = Path("out.md").read_text(encoding="UTF-8")

    # The straggler now pins the winning SHA and version comment.
    assert content.count(f"owner/repo@{'c' * 40} # v2.0.0") == 2
    assert "v1.0.0" not in content
    # The report shows the convergence, dated from the release candidates.
    assert "`v1.0.0` → `v2.0.0`" in body
    # The held-back table stays truthful: locked at 2.0.0, 3.0.0 withheld.
    assert "`2.0.0`" in body
    assert "`3.0.0`" in body


def test_sync_action_pins_merges_mixed_pins_onto_widest_range():
    """Mixed pins bumping to one release report a single widest-range row.

    With one action pinned at two versions and an eligible release above
    both, per-file reporting used to emit one table row per starting version,
    while the slug-keyed compare-URL and release-notes mappings kept a single
    arbitrary range (the last one, after sorting), so rows linked to compare
    URLs that contradicted their own text. The merged row must span from the
    lowest pin and its release notes must cover every release any pin skips.
    """
    today = datetime.now(timezone.utc).date()
    tags = {
        "v1.0.0": GitHubRelease(
            date=(today - timedelta(days=400)).isoformat(), body="release one notes"
        ),
        # 100 days old: an intermediate release the lowest pin jumps over.
        "v2.0.0": GitHubRelease(
            date=(today - timedelta(days=100)).isoformat(), body="release two notes"
        ),
        # 30 days old: cleared the cooldown, adopted by every pin.
        "v3.0.0": GitHubRelease(
            date=(today - timedelta(days=30)).isoformat(), body="release three notes"
        ),
    }
    runner = CliRunner()
    with runner.isolated_filesystem():
        workflow = Path(".github/workflows/ci.yaml")
        workflow.parent.mkdir(parents=True)
        workflow.write_text(
            "jobs:\n  build:\n    steps:\n"
            f"      - uses: owner/repo@{'a' * 40} # v1.0.0\n"
            f"      - uses: owner/repo@{'c' * 40} # v2.0.0\n",
            encoding="UTF-8",
        )
        with (
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
        content = workflow.read_text(encoding="UTF-8")
        body = Path("out.md").read_text(encoding="UTF-8")

    # Both pins converge onto the eligible v3.0.0.
    assert content.count(f"owner/repo@{'b' * 40} # v3.0.0") == 2
    # A single row spans from the lowest pin; the narrower range is subsumed.
    assert "`v1.0.0` → `v3.0.0`" in body
    assert "`v2.0.0` → `v3.0.0`" not in body
    # The compare URL matches the merged row, not an arbitrary range.
    assert "https://github.com/owner/repo/compare/v1.0.0...v3.0.0" in body
    assert "compare/v2.0.0...v3.0.0" not in body
    # One dropdown covering every release inside the widest range, half-open
    # on the old side: the lowest pin's own notes stay out.
    assert body.count("<summary><code>owner/repo</code></summary>") == 1
    assert "release two notes" in body
    assert "release three notes" in body
    assert "release one notes" not in body


def test_sync_workflow_pins_release_notes_cover_pypi_literals_only():
    """`sync-workflow-pins --release-notes` fetches notes for PyPI pins only.

    npm literals have no source-discovery path, so they are excluded from the
    `fetch_release_notes` call even when bumped in the same run, while the PyPI
    literal's notes still render.
    """
    today = datetime.now(timezone.utc).date()
    eligible = (today - timedelta(days=30)).isoformat()
    captured: dict[str, object] = {}

    def fake_fetch(changes):
        captured["changes"] = changes
        return {"mango": ("https://github.com/owner/mango", [("v2.0.0", "the notes")])}

    runner = CliRunner()
    with runner.isolated_filesystem():
        workflow = Path(".github/workflows/ci.yaml")
        workflow.parent.mkdir(parents=True)
        workflow.write_text(
            "jobs:\n  build:\n    steps:\n"
            "      - run: npm install grape@1.0.0\n"
            "      - run: uvx 'mango==1.0.0'\n",
            encoding="UTF-8",
        )
        with (
            patch(
                "repomatic.sync_ops.npm_candidates",
                return_value=[
                    vs.Candidate(version="9.0.0", date=eligible, ref="9.0.0")
                ],
            ),
            patch(
                "repomatic.sync_ops.pypi_candidates",
                return_value=[
                    vs.Candidate(version="2.0.0", date=eligible, ref="2.0.0")
                ],
            ),
            patch("repomatic.sync_ops.fetch_release_notes", side_effect=fake_fetch),
        ):
            result = runner.invoke(
                repomatic,
                [
                    "sync-workflow-pins",
                    "--release-notes",
                    "--output",
                    "out.md",
                    "--output-format",
                    "markdown",
                ],
            )
        assert result.exit_code == 0, result.output
        body = Path("out.md").read_text(encoding="UTF-8")

    # Both literals were bumped in the file.
    assert "grape" in body
    assert "mango" in body
    # Notes were fetched for the PyPI literal only, never the npm one.
    assert captured["changes"] == [("mango", "1.0.0", "2.0.0")]
    assert "### Release notes" in body
    assert "the notes" in body


def _write_workflow_at(path: str, content: str) -> Path:
    """Create a workflow file with parents inside the isolated filesystem."""
    workflow = Path(path)
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text(content, encoding="UTF-8")
    return workflow


def test_sync_workflow_pins_upstream_pin_aligns_to_refs_in_cooldown():
    """The upstream toolkit pin aligns to the `uses:` ref despite the cooldown.

    A fresh upstream release is normally withheld by `minimum-release-age`,
    which left the inline pin lagging the already-bumped refs and `lint-repo`
    failing until the cooldown expired. Regular literals in the same run stay
    cooldown-governed.
    """
    today = datetime.now(timezone.utc).date()
    fresh = today.isoformat()

    runner = CliRunner()
    with runner.isolated_filesystem():
        workflow = _write_workflow_at(
            ".github/workflows/ci.yaml",
            "jobs:\n"
            "  lint:\n"
            "    uses: kdeldycke/repomatic/.github/workflows/lint.yaml@"
            "36523e5a56f287e814210042ca7b852147a95498 # v7.0.0\n"
            "  build:\n"
            "    steps:\n"
            "      - run: uvx 'repomatic==6.31.0' metadata\n"
            "      - run: uvx 'mango==1.0.0'\n",
        )
        with patch(
            "repomatic.sync_ops.pypi_candidates",
            return_value=[vs.Candidate(version="9.0.0", date=fresh, ref="9.0.0")],
        ):
            result = runner.invoke(
                repomatic,
                ["sync-workflow-pins", "--output", "out.md"],
            )
        assert result.exit_code == 0, result.output
        content = workflow.read_text(encoding="UTF-8")
        body = Path("out.md").read_text(encoding="UTF-8")

    # The upstream pin aligned to the ref version, not to PyPI's fresh 9.0.0,
    # and carries the exemption that lets `uvx` resolve a version the workflow's
    # own `UV_EXCLUDE_NEWER` would otherwise withhold.
    assert (
        "uvx --exclude-newer-package repomatic=P0D 'repomatic==7.0.0' metadata"
        in content
    )
    # The regular literal stayed put: its only release is inside the cooldown,
    # and it is cooldown-governed, so it earns no exemption.
    assert "uvx 'mango==1.0.0'" in content
    # The refs themselves are never rewritten by this updater.
    assert "lint.yaml@36523e5a56f287e814210042ca7b852147a95498 # v7.0.0" in content
    # No PyPI release listing was consulted for the pin, so its "Released"
    # cell marks the exemption instead of showing an upload date.
    assert (
        "| [repomatic](https://pypi.org/project/repomatic/)"
        " | `6.31.0` → `7.0.0`"
        " | [⛓️ lockstep with `uses:` refs]"
        "(https://repomatic.net/workflows"
        "#sync-workflow-pins-updater) |"
    ) in body


def test_sync_workflow_pins_upstream_pin_cooldown_without_refs():
    """Without upstream `uses:` refs, the toolkit pin stays cooldown-governed."""
    today = datetime.now(timezone.utc).date()
    fresh = today.isoformat()

    runner = CliRunner()
    with runner.isolated_filesystem():
        workflow = _write_workflow_at(
            ".github/workflows/ci.yaml",
            "jobs:\n  build:\n    steps:\n"
            "      - run: uvx 'repomatic==6.31.0' metadata\n",
        )
        with patch(
            "repomatic.sync_ops.pypi_candidates",
            return_value=[vs.Candidate(version="7.0.0", date=fresh, ref="7.0.0")],
        ):
            result = runner.invoke(
                repomatic,
                ["sync-workflow-pins", "--output", "out.md"],
            )
        assert result.exit_code == 0, result.output
        content = workflow.read_text(encoding="UTF-8")

    assert "repomatic==6.31.0" in content


def test_sync_workflow_pins_upstream_pin_already_aligned():
    """A pin equal to the `uses:` ref version ignores newer PyPI releases."""
    today = datetime.now(timezone.utc).date()
    eligible = (today - timedelta(days=30)).isoformat()

    runner = CliRunner()
    with runner.isolated_filesystem():
        workflow = _write_workflow_at(
            ".github/workflows/ci.yaml",
            "jobs:\n"
            "  lint:\n"
            "    uses: kdeldycke/repomatic/.github/workflows/lint.yaml@"
            "36523e5a56f287e814210042ca7b852147a95498 # v7.0.0\n"
            "  build:\n"
            "    steps:\n"
            "      - run: uvx 'repomatic==7.0.0' metadata\n",
        )
        with patch(
            "repomatic.sync_ops.pypi_candidates",
            return_value=[vs.Candidate(version="9.0.0", date=eligible, ref="9.0.0")],
        ):
            result = runner.invoke(
                repomatic,
                ["sync-workflow-pins", "--output", "out.md"],
            )
        assert result.exit_code == 0, result.output
        content = workflow.read_text(encoding="UTF-8")

    # The eligible 9.0.0 release does not override the ref-locked version.
    assert "repomatic==7.0.0" in content


def test_sync_workflow_pins_upstream_pin_realigns_stragglers_both_ways():
    """Every literal realigns to the refs, lagging or ahead, across files."""
    today = datetime.now(timezone.utc).date()
    fresh = today.isoformat()

    runner = CliRunner()
    with runner.isolated_filesystem():
        behind = _write_workflow_at(
            ".github/workflows/tests.yaml",
            "jobs:\n"
            "  lint:\n"
            "    uses: kdeldycke/repomatic/.github/workflows/lint.yaml@"
            "36523e5a56f287e814210042ca7b852147a95498 # v7.0.0\n"
            "  build:\n"
            "    steps:\n"
            "      - run: uvx 'repomatic==6.31.0' metadata\n",
        )
        ahead = _write_workflow_at(
            ".github/workflows/docs.yaml",
            "jobs:\n  build:\n    steps:\n"
            "      - run: uvx 'repomatic==7.1.0' changelog\n",
        )
        with patch(
            "repomatic.sync_ops.pypi_candidates",
            return_value=[vs.Candidate(version="9.0.0", date=fresh, ref="9.0.0")],
        ):
            result = runner.invoke(
                repomatic,
                ["sync-workflow-pins", "--output", "out.md"],
            )
        assert result.exit_code == 0, result.output
        behind_content = behind.read_text(encoding="UTF-8")
        ahead_content = ahead.read_text(encoding="UTF-8")

    assert "repomatic==7.0.0" in behind_content
    assert "repomatic==7.0.0" in ahead_content


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
    elif spec.npm is not None:
        assert spec.package or spec.name
    else:
        assert spec.pypi_name


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


# ---------------------------------------------------------------------------
# setup-uv checksum table
# ---------------------------------------------------------------------------

CHECKSUM_TABLE = """// AUTOGENERATED_DO_NOT_EDIT
export const KNOWN_CHECKSUMS: { [key: string]: string } = {
  "aarch64-apple-darwin-0.12.3":
    "1c99f3af80fdcd2e4b0e2c2c4ba5c3ff2ec0f27a24a2f6b0b8f38ba24bd1c0a1",
  "arm-unknown-linux-musleabihf-0.12.3":
    "2b88e2ae7fecbc1d3a0d1b1b3a94b2ee1db9e16913b1e5a0a7e27a913ac2b0b2",
  "x86_64-unknown-linux-gnu-0.11.30":
    "3a77d19d6edbab0c2909a0a0294a1dd00ca8d05802a0d4909d16909802b3a0c3",
};
"""
"""A checksum table in the upstream shape, holding two uv versions.

Both target triples and both segment counts are represented, since the version
is the only fixed part of a key: `arm-unknown-linux-musleabihf` carries one
more segment than `aarch64-apple-darwin`.
"""


@pytest.fixture
def uncached_table():
    """Clear the per-commit checksum-table cache around a test."""
    vs._checksum_table.cache_clear()
    yield
    vs._checksum_table.cache_clear()


def test_checksum_table_parses_every_version(uncached_table):
    """Each key yields its uv version, whatever the target triple's shape."""
    with patch("repomatic.version_sync.get_text", return_value=CHECKSUM_TABLE):
        assert vs._checksum_table("deadbeef") == frozenset({"0.12.3", "0.11.30"})


def test_checksum_table_unreadable_is_unknown(uncached_table):
    """A failed fetch leaves the gate open rather than blocking every bump."""
    with patch(
        "repomatic.version_sync.get_text", side_effect=vs.FetchError("boom")
    ) as fetch:
        assert vs._checksum_table("deadbeef") is None
    assert fetch.called


def test_checksum_table_without_keys_is_unknown(uncached_table):
    """A body parsing to nothing means the upstream format moved, not that
    the action verifies nothing."""
    with patch("repomatic.version_sync.get_text", return_value="export const X = {};"):
        assert vs._checksum_table("deadbeef") is None


def test_checksum_table_is_fetched_once_per_commit(uncached_table):
    """Content at a commit is immutable, so the two readers share one fetch."""
    with patch("repomatic.version_sync.get_text", return_value=CHECKSUM_TABLE) as fetch:
        vs._checksum_table("deadbeef")
        vs._checksum_table("deadbeef")
    assert fetch.call_count == 1


def test_setup_uv_verified_versions_intersects_every_pin(uncached_table):
    """Mid-bump, only a uv both pinned tables carry is verifiable fleet-wide."""
    older = CHECKSUM_TABLE.replace("0.12.3", "0.10.1")

    def fake_get_text(url, **kwargs):
        return CHECKSUM_TABLE if "newsha" in url else older

    with patch("repomatic.version_sync.get_text", side_effect=fake_get_text):
        verified = vs.setup_uv_verified_versions(["newsha", "oldsha"])
    assert verified == frozenset({"0.11.30"})


def test_setup_uv_verified_versions_without_pins_is_unknown(uncached_table):
    """A repository pinning no setup-uv commit has nothing to gate on."""
    assert vs.setup_uv_verified_versions([]) is None
