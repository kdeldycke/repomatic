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

"""Tests for runner image announcement parsing and reporting.

Every fixture below is trimmed from a real `actions/runner-images` announcement,
so the parsing stays pinned to the shapes GitHub actually publishes rather than
to invented ones.
"""

from __future__ import annotations

import pytest

from repomatic import runner_images
from repomatic.runner_images import (
    Announcement,
    fetch_announcements,
    render_announcement_rows,
)

# Trimmed from actions/runner-images#14254. The migration advice naming
# `ubuntu-24.04-arm` sits outside the impact section on purpose: it is the trap
# a whole-body scan falls into, reporting a retirement as hitting the very image
# it tells you to move to.
UBUNTU_22_RETIREMENT = """\
### Breaking changes

Deprecation will begin on September 17th, 2026.

### Possible impact

Workflows using the `ubuntu-22.04`, `ubuntu-22.04-arm` image labels will be
terminated with an error.

### Migration guide

Workflows using the `ubuntu-22.04` image label should be updated to
`ubuntu-24.04`, `ubuntu-26.04`, or `ubuntu-latest`.
Workflows using the `ubuntu-22.04-arm` image label should be updated to
`ubuntu-24.04-arm`, `ubuntu-26.04-arm`.
"""

# Trimmed from actions/runner-images#14226. "removal" appears in prose about
# unrelated tooling, which is what made a body-wide keyword scan misread this
# arrival as a retirement.
UBUNTU_26_ARRIVAL = """\
### Breaking changes

Ubuntu 26.04 x64 and Arm are now available for all GitHub Actions users.

```yaml
jobs:
  jobName:
    runs-on: ubuntu-26.04
```

Pre-cached versions are unavailable, pending removal of the old toolcache.
"""


def announcement(title: str, body: str = "", number: int = 1) -> Announcement:
    """Build an announcement with the fields the tests care about."""
    return Announcement(
        number=number,
        title=title,
        url=f"https://github.com/actions/runner-images/issues/{number}",
        created_at="2026-06-16T00:00:00Z",
        body=body,
    )


@pytest.mark.parametrize(
    ("title", "expected"),
    (
        # Both retirement notices open upstream today.
        ("[Ubuntu] The Ubuntu 22 based runner images will begin deprecation", True),
        ("[macOS] The macOS 14 Sonoma based images will be fully unsupported", True),
        ("[Windows] Windows 2019 images will be retired", True),
        # And every arrival, which must not be mistaken for one.
        ("[Ubuntu] Ubuntu 26.04 is now available as a public preview", False),
        ("[macOS] Xcode 27 is now available as a public preview", False),
        ("[macOS] Default Xcode on macOS 26 Tahoe will be set to Xcode 26.6", False),
    ),
)
def test_deprecation_is_classified_from_the_title(title: str, expected: bool) -> None:
    """The title alone decides, so body prose cannot flip the verdict."""
    assert announcement(title, body=UBUNTU_26_ARRIVAL).is_deprecation is expected


def test_arrival_body_mentioning_removal_is_not_a_retirement() -> None:
    """A body-wide keyword scan used to misread this arrival as a retirement."""
    item = announcement(
        "[Ubuntu] Ubuntu 26.04 and Ubuntu 26.04 Arm is now available",
        body=UBUNTU_26_ARRIVAL,
    )
    assert "removal" in item.body
    assert item.is_deprecation is False


def test_affected_runners_reads_only_the_impact_section() -> None:
    """A migration destination is never reported as affected.

    `ubuntu-24.04-arm` appears in the body, but as the image to move *to*, so a
    project running on it is not affected by this retirement.
    """
    item = announcement("Ubuntu 22 deprecation", body=UBUNTU_22_RETIREMENT)
    known = {"ubuntu-22.04", "ubuntu-22.04-arm", "ubuntu-24.04-arm", "ubuntu-26.04"}
    assert item.affected_runners(known) == frozenset({
        "ubuntu-22.04",
        "ubuntu-22.04-arm",
    })


def test_affected_runners_ignores_images_the_project_does_not_use() -> None:
    """Intersecting with the tracked set cannot invent a label."""
    item = announcement("Ubuntu 22 deprecation", body=UBUNTU_22_RETIREMENT)
    assert item.affected_runners({"macos-26", "windows-2025"}) == frozenset()


def test_affected_runners_without_an_impact_section_is_empty() -> None:
    """A renamed heading yields no flag rather than a wrong one."""
    item = announcement("Ubuntu 26.04 available", body=UBUNTU_26_ARRIVAL)
    assert item.affected_runners({"ubuntu-26.04"}) == frozenset()


def test_rows_put_affected_announcements_first_then_newest() -> None:
    """Announcements naming an image in use float above the rest."""
    stale_hit = Announcement(
        number=1,
        title="Ubuntu 22 deprecation",
        url="https://example.invalid/1",
        created_at="2026-01-01T00:00:00Z",
        body=UBUNTU_22_RETIREMENT,
    )
    recent_miss = Announcement(
        number=2,
        title="[macOS] Xcode 27 is now available",
        url="https://example.invalid/2",
        created_at="2026-07-16T00:00:00Z",
        body="",
    )
    older_miss = Announcement(
        number=3,
        title="[Windows] Windows 11 Arm is now available",
        url="https://example.invalid/3",
        created_at="2026-02-01T00:00:00Z",
        body="",
    )
    # An explicit empty catalog keeps the call offline. These fixtures name
    # their labels in the impact section, which resolves without a catalog:
    # only the checkbox source needs one.
    table = render_announcement_rows(
        (recent_miss, older_miss, stale_hit),
        {"ubuntu-22.04"},
        catalog=[],
    )
    rows = [line for line in table.splitlines() if line.startswith("| 20")]
    # The older announcement naming an image in use outranks both newer misses,
    # which then fall in date order.
    assert [row.split(" | ")[0] for row in rows] == [
        "| 2026-01-01",
        "| 2026-07-16",
        "| 2026-02-01",
    ]
    assert "`ubuntu-22.04`" in rows[0]
    assert "🔴 retirement" in rows[0]


def test_fetch_returns_none_when_the_api_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable API is not evidence that nothing is announced."""
    monkeypatch.setattr(runner_images, "gh_api_json", lambda args: None)
    assert fetch_announcements() is None


def test_fetch_skips_pull_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """The issues endpoint returns PRs too, and none of them is an announcement."""
    payload = [
        {
            "number": 1,
            "title": "[Ubuntu] Ubuntu 26.04 is now available",
            "html_url": "https://example.invalid/1",
            "created_at": "2026-06-11T00:00:00Z",
            "body": "",
        },
        {
            "number": 2,
            "title": "Bump some dependency",
            "html_url": "https://example.invalid/2",
            "created_at": "2026-06-12T00:00:00Z",
            "body": "",
            "pull_request": {"url": "https://example.invalid/pulls/2"},
        },
    ]
    monkeypatch.setattr(runner_images, "gh_api_json", lambda args: payload)
    fetched = fetch_announcements()
    assert fetched is not None
    assert [item.number for item in fetched] == [1]
