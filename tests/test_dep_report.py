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

"""Golden renders and date-formatting checks for the shared report module.

Every dependency updater's PR body is assembled from
{mod}`repomatic.deps.report`, so the exact markdown these functions emit is a
user-visible contract shared by `sync-uv-lock`, `sync-deps`,
`sync-dep-sources`, the three version-sync bumpers, and `audit --fix`. The
tables are asserted verbatim rather than probed for substrings: a stray
column, a lost separator row, or a changed link shape is exactly the kind of
drift a `in x` assertion sails past.
"""

from __future__ import annotations

import ast
import json
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest

import repomatic
from repomatic.deps.report import (
    BYPASS_COLUMNS,
    BYPASS_NEEDS_RELEASE,
    BYPASS_NOT_LOCKED,
    HELD_BACK_COLUMNS,
    BypassForecast,
    HeldBackPackage,
    build_comparison_urls,
    build_held_back,
    fetch_release_notes,
    format_bypass_section,
    format_diff_table,
    format_exclude_newer_note,
    format_held_back_table,
    format_release_notes,
    format_released,
    format_upload_date,
    pypi_name_urls,
)
from tests.conftest import FakeResponse

CHANGES = [
    ("apricot", "1.2.0", "1.3.0"),
    ("blueberry", "", "0.4.1"),
    ("cherry", "2.0.0", ""),
]
"""One change of each shape: an upgrade, an addition, and a removal."""


# ---------------------------------------------------------------------------
# Date parsing and formatting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("2026-06-24T10:00:00Z", "2026-06-24"),
        # Unparsable input passes through rather than raising: the date is
        # decoration on a report, never a value anything branches on.
        ("not-a-date", "not-a-date"),
        ("", ""),
    ),
)
def test_format_upload_date(value, expected):
    assert format_upload_date(value) == expected


@pytest.mark.parametrize(
    ("raw_upload", "reference", "expected"),
    (
        ("", date(2026, 6, 26), ""),
        ("2026-06-24T10:00:00Z", None, "2026-06-24"),
        # A day reference truncates the upload to match it. Measuring the
        # 10:00 instant against midnight would call these 38 hours "a day".
        ("2026-06-24T10:00:00Z", date(2026, 6, 26), "2026-06-24 (2 days ago)"),
        # An instant reference keeps the hours a day reference cannot carry.
        (
            "2026-06-24T10:00:00Z",
            datetime(2026, 6, 24, 16, tzinfo=UTC),
            "2026-06-24 (6 hours ago)",
        ),
        ("not-a-date", date(2026, 6, 26), "not-a-date"),
    ),
)
def test_format_released(raw_upload, reference, expected):
    assert format_released(raw_upload, reference) == expected


# ---------------------------------------------------------------------------
# Diff table
# ---------------------------------------------------------------------------


def test_format_diff_table_without_upload_times():
    assert format_diff_table(CHANGES) == (
        "## 🆙 Updated packages\n"
        "\n"
        "| Package | Change |\n"
        "| :-- | :-- |\n"
        "| apricot | `1.2.0` → `1.3.0` |\n"
        "| blueberry | 🆕 new: `0.4.1` |\n"
        "| cherry | 🗑️ removed: `2.0.0` |"
    )


def test_format_diff_table_with_every_decoration():
    """Released column, cooldown note, links, and a comparison URL together."""
    assert format_diff_table(
        CHANGES,
        upload_times={"apricot": "2026-06-24T10:00:00Z"},
        cooldown_note=format_exclude_newer_note("2026-06-20T00:00:00Z"),
        comparison_urls={"apricot": "https://example.com/compare/v1.2.0...v1.3.0"},
        reference_date=date(2026, 6, 26),
        name_urls=pypi_name_urls(name for name, _old, _new in CHANGES),
        heading="Updated tools",
        subject="Tool",
        released_overrides={"cherry": "lockstep pin"},
    ) == (
        "## 🆙 Updated tools\n"
        "\n"
        "Resolved with [`exclude-newer`]"
        "(https://docs.astral.sh/uv/reference/settings/#exclude-newer)"
        " cutoff: `2026-06-20`.\n"
        "\n"
        "| Tool | Change | Released |\n"
        "| :-- | :-- | :-- |\n"
        "| [apricot](https://pypi.org/project/apricot/) |"
        " [`1.2.0` → `1.3.0`](https://example.com/compare/v1.2.0...v1.3.0) |"
        " 2026-06-24 (2 days ago) |\n"
        # An added package has no upload time, so the cell renders empty.
        "| [blueberry](https://pypi.org/project/blueberry/) | 🆕 new: `0.4.1` |  |\n"
        "| [cherry](https://pypi.org/project/cherry/) | 🗑️ removed: `2.0.0` |"
        " lockstep pin |"
    )


def test_format_diff_table_empty_changes():
    assert format_diff_table([]) == ""


def test_format_diff_table_override_alone_forces_released_column():
    """An override on a changed name turns the column on without upload times."""
    rendered = format_diff_table(
        [("apricot", "1.2.0", "1.3.0")],
        released_overrides={"apricot": "lockstep pin"},
    )
    assert "| Package | Change | Released |" in rendered
    assert rendered.endswith("| apricot | `1.2.0` → `1.3.0` | lockstep pin |")


def test_format_diff_table_override_for_unchanged_name_is_ignored():
    rendered = format_diff_table(
        [("apricot", "1.2.0", "1.3.0")],
        released_overrides={"durian": "lockstep pin"},
    )
    assert "| Package | Change |" in rendered
    assert "lockstep pin" not in rendered


def test_format_diff_table_change_label_leads_the_comparison_link():
    """A label sits outside the comparison link, so both stay clickable."""
    rendered = format_diff_table(
        [("apricot", "1.3.0", "1.2.0")],
        comparison_urls={"apricot": "https://example.com/compare/v1.3.0...v1.2.0"},
        change_labels={"apricot": "⏪ stepped back"},
    )
    assert rendered.endswith(
        "| apricot | ⏪ stepped back:"
        " [`1.3.0` → `1.2.0`](https://example.com/compare/v1.3.0...v1.2.0) |"
    )


def test_format_exclude_newer_note_empty():
    assert format_exclude_newer_note("") == ""


# ---------------------------------------------------------------------------
# Held-back table
# ---------------------------------------------------------------------------


def test_format_held_back_table():
    held = [
        HeldBackPackage("apricot", "1.2.0", "1.3.0", "2026-06-24", "2026-07-01"),
        HeldBackPackage("blueberry", "0.4.0", "0.4.1", "", ""),
    ]
    assert format_held_back_table(
        held,
        note="Cooling off.",
        name_urls={"apricot": "https://example.com/apricot"},
        subject="Tool",
    ) == (
        "## ⏸️ Held back by cooldown\n"
        "\n"
        "Cooling off.\n"
        "\n"
        "| Tool | Locked | Available | Released | Eligible |\n"
        "| :-- | :-- | :-- | :-- | :-- |\n"
        "| [apricot](https://example.com/apricot) | `1.2.0` | `1.3.0` |"
        " 2026-06-24 | 2026-07-01 |\n"
        "| blueberry | `0.4.0` | `0.4.1` |  |  |"
    )


def test_format_held_back_table_empty():
    assert format_held_back_table([]) == ""


def test_terminal_rows_follow_the_shared_columns():
    """Each record fills its terminal table in the order the columns name."""
    held = HeldBackPackage("apricot", "1.2.0", "1.3.0", "2026-06-24", "2026-07-01")
    assert held.row == ("apricot", "1.2.0", "1.3.0", "2026-06-24", "2026-07-01")
    assert len(held.row) == 1 + len(HELD_BACK_COLUMNS)
    unlocked = BypassForecast("fig", "", "2026-06-02")
    assert unlocked.row == ("fig", BYPASS_NOT_LOCKED, "2026-06-02")
    assert len(unlocked.row) == len(BYPASS_COLUMNS)


def test_build_held_back_formats_both_dates():
    row = build_held_back(
        "apricot",
        pinned="1.2.0",
        available="1.3.0",
        available_date="2026-06-24",
        min_age=timedelta(days=7),
        today=date(2026, 6, 26),
    )
    assert row == HeldBackPackage(
        "apricot",
        "1.2.0",
        "1.3.0",
        "2026-06-24 (2 days ago)",
        "2026-07-01 (in 5 days)",
    )


def test_build_held_back_without_a_date():
    """A datasource that reports no date still yields a row, minus the dates."""
    row = build_held_back(
        "apricot",
        pinned="1.2.0",
        available="1.3.0",
        available_date="",
        min_age=timedelta(days=7),
        today=date(2026, 6, 26),
    )
    assert row.released == ""
    assert row.eligible == ""


# ---------------------------------------------------------------------------
# Cooldown-bypass section
# ---------------------------------------------------------------------------


def test_format_bypass_section_covers_every_lifecycle_state():
    """Active, frozen, cleared, unreleased and unlocked entries share one table."""
    assert format_bypass_section(
        forecasts=[
            BypassForecast("apricot", "1.3.0", "2026-07-08 (in 2 days)"),
            BypassForecast("blueberry", "0.4.1", "2026-07-09 (in 3 days)"),
            BypassForecast("durian", "0.1.0", BYPASS_NEEDS_RELEASE),
            BypassForecast("elderberry", "", "2026-07-10 (in 4 days)"),
        ],
        pruned=[
            BypassForecast("cherry", "2.0.0", "2026-06-01"),
            BypassForecast("fig", "", "2026-06-02"),
        ],
        frozen=["blueberry"],
        name_urls={"apricot": "https://example.com/apricot"},
    ) == (
        "## ❄️ Cooldown bypasses\n"
        "\n"
        "Packages pulled in ahead of the cooldown by an [`exclude-newer-package`]"
        "(https://docs.astral.sh/uv/reference/settings/#exclude-newer-package)"
        " freeze.\n"
        "\n"
        "| Package | Held at | Held until |\n"
        "| :-- | :-- | :-- |\n"
        "| [apricot](https://example.com/apricot) | `1.3.0` |"
        " 2026-07-08 (in 2 days) |\n"
        "| blueberry | 📌 frozen: `0.4.1` | 2026-07-09 (in 3 days) |\n"
        "| cherry | 🧹 cleared: `2.0.0` | 2026-06-01 |\n"
        "| durian | 🚧 unreleased: `0.1.0` | *needs release* |\n"
        "| elderberry | *not locked* | 2026-07-10 (in 4 days) |\n"
        "| fig | 🧹 cleared: *not locked* | 2026-06-02 |"
    )


def test_format_bypass_section_warns_about_a_stale_comment():
    """A cleared entry the table's comment still names gets a warning."""
    section = format_bypass_section(
        forecasts=[],
        pruned=[BypassForecast("cherry", "2.0.0", "2026-06-01")],
        stale_comments=["cherry"],
    )
    assert section.endswith(
        "| cherry | 🧹 cleared: `2.0.0` | 2026-06-01 |\n"
        "\n"
        "> [!WARNING]\n"
        "> The comment above `exclude-newer-package` in `pyproject.toml` still"
        " names `cherry`, which this run cleared. Update it by hand."
    )


def test_format_bypass_section_empty():
    assert format_bypass_section([]) == ""


# ---------------------------------------------------------------------------
# Comparison URLs
# ---------------------------------------------------------------------------


def test_build_comparison_urls_detects_the_tag_prefix():
    notes = {
        "apricot": ("https://github.com/acme/apricot", [("v1.3.0", "body")]),
        "blueberry": ("https://github.com/acme/blueberry", [("0.4.1", "body")]),
    }
    changes = [("apricot", "1.2.0", "1.3.0"), ("blueberry", "0.4.0", "0.4.1")]
    assert build_comparison_urls(changes, notes) == {
        "apricot": "https://github.com/acme/apricot/compare/v1.2.0...v1.3.0",
        "blueberry": "https://github.com/acme/blueberry/compare/0.4.0...0.4.1",
    }


def test_build_comparison_urls_skips_tagless_notes():
    """A changelog-link fallback proves the tags do not exist: emit no URL.

    `fetch_release_notes` falls back to a bare changelog link (an empty tag)
    only when it found no GitHub release for the range. Guessing a `v` prefix
    there put a 404 in the PR body.
    """
    notes = {
        "apricot": (
            "https://github.com/acme/apricot",
            [("", "[Changelog](https://example.com/changes)")],
        ),
    }
    assert build_comparison_urls([("apricot", "1.2.0", "1.3.0")], notes) == {}


@pytest.mark.parametrize(
    "changes",
    (
        pytest.param([("apricot", "", "1.3.0")], id="added"),
        pytest.param([("apricot", "1.2.0", "")], id="removed"),
    ),
)
def test_build_comparison_urls_needs_both_versions(changes):
    notes = {"apricot": ("https://github.com/acme/apricot", [("v1.3.0", "body")])}
    assert build_comparison_urls(changes, notes) == {}


def test_build_comparison_urls_skips_unknown_packages():
    assert build_comparison_urls([("apricot", "1.2.0", "1.3.0")], {}) == {}


def test_format_diff_table_call_sites_pass_reference_date():
    """Every PR-report call site opts into the humanized `Released` delta.

    `reference_date` drives the relative hint on the `Released` column; a call
    site omitting it renders bare dates, as `fix-vulnerable-deps` once did
    while the other dependency updaters showed the delta.
    """
    package_dir = Path(repomatic.__file__).parent
    calls = 0
    violations = []
    for py_file in sorted(package_dir.rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="UTF-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else (func.id if isinstance(func, ast.Name) else "")
            )
            if name != "format_diff_table":
                continue
            calls += 1
            if "reference_date" not in {kw.arg for kw in node.keywords}:
                violations.append(f"{py_file.name}:{node.lineno}")
    # Guard the scan itself: a wrong root finds no call and passes vacuously.
    assert calls >= 2
    assert not violations, (
        f"format_diff_table calls missing reference_date: {violations}"
    )


def test_format_release_notes_nests_headings():
    """The h3 section holds h4 version headings, with upstream bodies below.

    The section heading is an h3 so it nests under the PR body's h2 update
    table, and the embedded upstream body's own headings are demoted below
    the h4 version heading instead of colliding with the PR's h2 sections.
    """
    notes = {
        "papaya": (
            "https://github.com/orchard/papaya",
            [("v2.0.0", "## Flavor\n\nSweeter pulp.\n\n### Texture\n\nFirmer.")],
        ),
    }
    section = format_release_notes(notes)
    assert section.startswith("### Release notes\n")
    assert "<summary><code>papaya</code></summary>" in section
    assert (
        "#### [`v2.0.0`](https://github.com/orchard/papaya/releases/tag/v2.0.0)"
        in section
    )
    assert "##### Flavor" in section
    assert "###### Texture" in section
    assert "\n## Flavor" not in section
    assert format_release_notes({}) == ""


# ---------------------------------------------------------------------------
# Release notes
# ---------------------------------------------------------------------------

PAPAYA_RELEASES = {
    "1.0.0": False,
    "1.1.0": False,
    "1.2.0": True,
    "1.3.0rc1": False,
    "1.3.0": False,
}
"""Releases of the `papaya` fixture package, mapped to whether PyPI yanked them."""


def _papaya_urlopen(calls: Counter[str]):
    """A `urlopen` serving PyPI and GitHub for `papaya`, counting each host."""
    pypi = {
        "info": {"project_urls": {"Source": "https://github.com/orchard/papaya"}},
        "releases": {
            version: [{"upload_time": "2026-01-01T00:00:00", "yanked": yanked}]
            for version, yanked in PAPAYA_RELEASES.items()
        },
    }
    releases = [
        {
            "tag_name": f"v{version}",
            "published_at": "2026-01-01T00:00:00Z",
            "body": f"Notes of {version}.",
        }
        for version in PAPAYA_RELEASES
    ]

    def urlopen(request, timeout=None):
        host = urlsplit(request.full_url).hostname
        calls[host] += 1
        payload = pypi if host == "pypi.org" else releases
        return FakeResponse(json.dumps(payload).encode())

    return urlopen


def test_fetch_release_notes_lists_each_repository_once(cache_env):
    """One listing of the releases serves every version the bump covers.

    A yanked release and a pre-release inside the range are left out, the way
    the selection sweeps leave them out.
    """
    calls: Counter[str] = Counter()
    with patch("repomatic.http.urlopen", side_effect=_papaya_urlopen(calls)):
        notes = fetch_release_notes([("papaya", "1.0.0", "1.3.0")])

    assert notes == {
        "papaya": (
            "https://github.com/orchard/papaya",
            [("v1.1.0", "Notes of 1.1.0."), ("v1.3.0", "Notes of 1.3.0.")],
        )
    }
    assert calls == {"pypi.org": 1, "api.github.com": 1}
