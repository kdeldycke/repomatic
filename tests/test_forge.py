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

"""Tests for the multi-forge metrics reader."""

from __future__ import annotations

import pytest

from repomatic.forge import (
    FORGE_APIS,
    GITHUB_HOST,
    ForgeMetrics,
    canonical_url,
    forge_of,
    newest_dated,
    split_repo_url,
)
from repomatic.metrics import METRICS_BY_ID


@pytest.mark.parametrize(
    ("url", "expected"),
    (
        (
            "https://github.com/apricot-org/apricot",
            ("github.com", "apricot-org/apricot"),
        ),
        (
            "http://github.com/apricot-org/apricot",
            ("github.com", "apricot-org/apricot"),
        ),
        (
            "https://github.com/apricot-org/apricot/",
            ("github.com", "apricot-org/apricot"),
        ),
        (
            "https://github.com/apricot-org/apricot.git",
            ("github.com", "apricot-org/apricot"),
        ),
        (
            "https://gitlab.example.org/fruits/papaya",
            ("gitlab.example.org", "fruits/papaya"),
        ),
        (
            "https://gitlab.example.org/fruits/tropical/papaya",
            ("gitlab.example.org", "fruits/tropical/papaya"),
        ),
    ),
)
def test_split_repo_url(url, expected):
    """Check a repository URL splits into its host and its namespace path."""
    assert split_repo_url(url) == expected


@pytest.mark.parametrize(
    "url",
    (
        "https://github.com",
        "https://github.com/",
        "https://github.com/apricot-org",
        "not-a-url",
    ),
)
def test_split_repo_url_rejects_incomplete(url):
    """Check a URL without an owner and a name is refused rather than guessed."""
    with pytest.raises(ValueError, match="repository URL"):
        split_repo_url(url)


@pytest.mark.parametrize(
    ("subject", "expected"),
    (
        # A bare slug is GitHub, which is what a repository declaring peers writes.
        ("fruits/papaya", "https://github.com/fruits/papaya"),
        ("/fruits/papaya/", "https://github.com/fruits/papaya"),
        ("https://github.com/fruits/papaya", "https://github.com/fruits/papaya"),
        ("https://github.com/fruits/papaya.git", "https://github.com/fruits/papaya"),
        ("http://github.com/fruits/papaya", "https://github.com/fruits/papaya"),
        ("https://gitlab.com/fruits/papaya", "https://gitlab.com/fruits/papaya"),
        (
            "https://codeberg.org/fruits/papaya/",
            "https://codeberg.org/fruits/papaya",
        ),
    ),
)
def test_canonical_url(subject, expected):
    """Check every way of naming a subject lands on one spelling.

    The store keys on this, so two configurations naming the same repository
    differently must not accrue two disjoint histories.
    """
    assert canonical_url(subject) == expected
    # Idempotent, since the sampler canonicalizes what it may already have.
    assert canonical_url(expected) == expected


@pytest.mark.parametrize("subject", ("", "papaya", "https://github.com/"))
def test_canonical_url_rejects_what_it_cannot_resolve(subject):
    """Check a half-written subject raises rather than becoming a wrong URL."""
    with pytest.raises(ValueError, match="repository URL"):
        canonical_url(subject)


def test_forge_of_never_guesses_an_unknown_host():
    """Check an undeclared host raises instead of sampling nothing.

    A silently unsampled subject is worse than a loud one: it renders as a
    project nobody follows rather than as a project nobody could read.
    """
    with pytest.raises(ValueError, match="Unknown forge host"):
        forge_of("https://git.example.org/fruits/papaya")

    resolved = forge_of(
        "https://git.example.org/fruits/papaya", {"git.example.org": "gitlab"}
    )
    assert resolved == "gitlab"


def test_forge_of_rejects_an_unimplemented_forge():
    """Check a declared host naming software nothing reads is refused."""
    with pytest.raises(ValueError, match="Unsupported forge"):
        forge_of("https://git.example.org/fruits/papaya", {"git.example.org": "svn"})


def test_known_forges_are_implemented():
    """Check every bundled host names a forge the reader actually speaks."""
    assert set(FORGE_APIS.values()) <= {"forgejo", "github", "gitlab"}
    assert FORGE_APIS == dict(sorted(FORGE_APIS.items()))
    assert FORGE_APIS[GITHUB_HOST] == "github"


@pytest.mark.parametrize(
    ("release", "tag", "expected"),
    (
        # A tag newer than the latest release object is the case that matters:
        # always preferring the release would report a live project as idle.
        ("2020-01-01", "2021-01-01", ("2021-01-01", "tag")),
        ("2021-01-01", "2020-01-01", ("2021-01-01", "release")),
        ("2021-01-01", "2021-01-01", ("2021-01-01", "release")),
        ("2021-01-01", None, ("2021-01-01", "release")),
        (None, "2021-01-01", ("2021-01-01", "tag")),
        (None, None, (None, None)),
    ),
)
def test_newest_dated(release, tag, expected):
    """Check the more recent of a release and a tag wins, and says which it is."""
    assert newest_dated(release, tag) == expected


def test_readings_yield_only_registered_metrics():
    """Check the reader and the metric registry cannot drift apart.

    A reading yielded under an ID the registry does not know raises inside
    `upsert`, mid-collection, on the one code path nobody watches.
    """
    full = ForgeMetrics(
        stars=42,
        created="2020-01-01",
        release="2026-08-01",
        release_source="tag",
        commit="2026-08-15",
    )
    readings = dict(full.readings())
    assert set(readings) <= set(METRICS_BY_ID)
    assert readings == {
        "stars": "42",
        "commit": "2026-08-15",
        "release": "2026-08-01",
        "release_source": "tag",
    }


def test_readings_omit_what_a_forge_did_not_answer():
    """Check a project with no release yields no row rather than a blank one."""
    assert dict(ForgeMetrics(stars=0).readings()) == {"stars": "0"}


def test_readings_leave_the_creation_date_to_the_sampler():
    """Check `created` is not yielded as a metric of its own.

    It is not a reading but the origin of one, recorded by the sampler as a
    zero-star reading on that date.
    """
    metrics = ForgeMetrics(stars=1, created="2020-01-01")
    assert "created" not in dict(metrics.readings())
    assert "created" not in METRICS_BY_ID


def test_readings_default_a_missing_release_source():
    """Check a release with no stated kind still records one.

    The store's consumers gate release-only rendering on this value, so an
    empty cell beside a real date would read as a project that never released.
    """
    metrics = ForgeMetrics(stars=1, release="2026-08-01")
    assert dict(metrics.readings())["release_source"] == "release"
