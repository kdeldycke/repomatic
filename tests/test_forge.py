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

"""Tests for the multi-forge metrics reader and its readings store."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from repomatic.config import Config, load_repomatic_config
from repomatic.forge import (
    FORGE_APIS,
    ForgeMetrics,
    ProjectRecord,
    forge_of,
    load_project_records,
    newest_dated,
    sample_projects,
    save_project_records,
    split_repo_url,
)
from repomatic.pyproject import read_pyproject_toml

REPO_ROOT = Path(__file__).parent.parent

PROJECT_STORE = REPO_ROOT / "docs" / "assets" / "project-metrics.json"
"""This repository's own readings, accrued by the scheduled sampler."""


def repo_config() -> Config:
    """Load this repository's own `[tool.repomatic]` section.

    Read from the repository root rather than from the working directory, so
    the assertions hold wherever pytest was invoked from.
    """
    return load_repomatic_config(read_pyproject_toml(REPO_ROOT))


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


def test_forge_of_never_guesses_an_unknown_host():
    """Check an undeclared host raises instead of sampling nothing.

    A silently unsampled project is worse than a loud one: it renders as a
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


def test_project_record_round_trips_through_json():
    """Check a record survives the shape it is committed in."""
    record = ProjectRecord(
        project_id="papaya",
        repo="https://github.com/fruits/papaya",
        sampled="2026-08-16",
        metrics=ForgeMetrics(
            stars=42, release="2026-08-01", release_source="tag", commit="2026-08-15"
        ),
    )
    assert ProjectRecord.from_dict(record.to_dict()) == record


def test_project_record_omits_what_a_project_does_not_have():
    """Check a project with no release adds no key rather than a null."""
    record = ProjectRecord(
        project_id="papaya",
        repo="https://github.com/fruits/papaya",
        sampled="2026-08-16",
        metrics=ForgeMetrics(stars=42),
    )
    assert record.to_dict() == {
        "date": "2026-08-16",
        "id": "papaya",
        "repo": "https://github.com/fruits/papaya",
        "stars": 42,
    }


def test_reading_ignores_the_date():
    """Check two readings of the same figures compare equal across dates.

    A week where nothing moved must leave the file untouched rather than
    restamping every unchanged row.
    """
    metrics = ForgeMetrics(stars=42)
    monday = ProjectRecord(
        "papaya", "https://github.com/fruits/papaya", "2026-08-10", metrics
    )
    sunday = ProjectRecord(
        "papaya", "https://github.com/fruits/papaya", "2026-08-16", metrics
    )
    assert monday.reading == sunday.reading
    assert monday != sunday


def test_save_project_records_is_biome_shaped(tmp_path):
    """Check the readings serialize the way `format-json` would leave them.

    Tab indentation and sorted keys, matching Biome's JSON style, so the
    autofix job never rewrites a file the sampler just committed.
    """
    store = tmp_path / "nested" / "project-metrics.json"
    records = {
        "papaya": ProjectRecord(
            "papaya", "https://github.com/fruits/papaya", "2026-08-16", ForgeMetrics(2)
        ),
        "apricot": ProjectRecord(
            "apricot",
            "https://github.com/fruits/apricot",
            "2026-08-16",
            ForgeMetrics(1),
        ),
    }
    assert save_project_records(store, records) is True
    text = store.read_text(encoding="UTF-8")
    assert "\t" in text
    assert "    " not in text
    assert text.endswith("\n")
    # Sorted by project ID, so a scheduled commit reads as a diff of the rows
    # that moved rather than a reshuffle.
    assert [entry["id"] for entry in json.loads(text)] == ["apricot", "papaya"]

    # Convergent: rewriting the same records touches nothing.
    assert save_project_records(store, records) is False


def test_load_project_records_is_loud_on_a_corrupt_file(tmp_path):
    """Check a malformed store raises instead of being silently clobbered."""
    store = tmp_path / "project-metrics.json"
    store.write_text("{not json", encoding="UTF-8")
    with pytest.raises(ValueError, match="Malformed project readings"):
        load_project_records(store)


def test_load_project_records_tolerates_a_missing_file(tmp_path):
    """Check a first run reads an empty store rather than failing."""
    assert load_project_records(tmp_path / "absent.json") == {}


def test_sample_projects_keeps_a_stale_reading_when_a_forge_fails(
    tmp_path, monkeypatch
):
    """Check one unreadable project costs its own row, not the whole column.

    A stale figure carrying its own date is more useful than a hole, and a
    flaky instance must not blank every project sampled beside it.
    """
    store = tmp_path / "project-metrics.json"
    previous = ProjectRecord(
        "papaya", "https://github.com/fruits/papaya", "2026-08-01", ForgeMetrics(7)
    )
    save_project_records(store, {"papaya": previous})

    def fake_metrics(url, extra_forges=None):
        if "papaya" in url:
            msg = "the forge answered nonsense"
            raise RuntimeError(msg)
        return ForgeMetrics(stars=99)

    monkeypatch.setattr("repomatic.forge.repo_metrics", fake_metrics)
    outcomes = sample_projects(
        store,
        {
            "apricot": "https://github.com/fruits/apricot",
            "papaya": "https://github.com/fruits/papaya",
        },
        sampled="2026-08-16",
    )

    by_id = {outcome.project_id: outcome for outcome in outcomes}
    assert by_id["apricot"].changed is True
    assert by_id["papaya"].error
    assert by_id["papaya"].metrics is None

    stored = load_project_records(store)
    assert stored["apricot"].metrics.stars == 99
    # The old reading survived, dated when it was actually taken.
    assert stored["papaya"] == previous


def test_sample_projects_drops_a_project_that_left(tmp_path, monkeypatch):
    """Check a project removed from the config takes its reading with it.

    Nothing downstream would read the row again, and a lingering ID would be
    sampled forever.
    """
    store = tmp_path / "project-metrics.json"
    save_project_records(
        store,
        {
            "papaya": ProjectRecord(
                "papaya",
                "https://github.com/fruits/papaya",
                "2026-08-01",
                ForgeMetrics(7),
            )
        },
    )
    monkeypatch.setattr(
        "repomatic.forge.repo_metrics", lambda url, extra_forges=None: ForgeMetrics(1)
    )
    outcomes = sample_projects(
        store, {"apricot": "https://github.com/fruits/apricot"}, sampled="2026-08-16"
    )

    assert {outcome.project_id for outcome in outcomes} == {"apricot", "papaya"}
    assert load_project_records(store).keys() == {"apricot"}


def test_sample_projects_leaves_an_unmoved_reading_alone(tmp_path, monkeypatch):
    """Check a quiet week rewrites nothing, date included."""
    store = tmp_path / "project-metrics.json"
    monkeypatch.setattr(
        "repomatic.forge.repo_metrics", lambda url, extra_forges=None: ForgeMetrics(7)
    )
    repos = {"papaya": "https://github.com/fruits/papaya"}
    sample_projects(store, repos, sampled="2026-08-01")
    before = store.read_text(encoding="UTF-8")

    outcomes = sample_projects(store, repos, sampled="2026-08-16")
    assert outcomes[0].changed is False
    assert store.read_text(encoding="UTF-8") == before


def test_committed_project_readings_are_well_formed():
    """Check the readings this repository accrues keep their expected shape.

    Guards a file a scheduled job rewrites unattended: a project that left the
    benchmark, a duplicated ID, or a date in the future would all surface here
    rather than as a misrendered page.
    """
    if not PROJECT_STORE.exists():
        pytest.skip("no project readings recorded yet")
    records = json.loads(PROJECT_STORE.read_text(encoding="UTF-8"))
    assert isinstance(records, list)
    assert records, "the readings file exists but holds nothing"

    config = repo_config()
    tracked = config.projects.repos
    today = datetime.now(tz=timezone.utc).date()
    optional = {"commit", "release", "release_source"}

    for record in records:
        assert set(record) <= {"date", "id", "repo", "stars"} | optional
        assert {"date", "id", "repo", "stars"} <= set(record)
        assert record["id"] in tracked
        assert record["repo"] == tracked[record["id"]]
        assert isinstance(record["stars"], int)
        assert record["stars"] >= 0
        for key in ("date", "commit", "release"):
            if key in record:
                assert date.fromisoformat(record[key]) <= today
        if "release" in record:
            assert record["release_source"] in {"release", "tag"}

    ids = [record["id"] for record in records]
    assert len(ids) == len(set(ids)), "one project was sampled twice"
    assert ids == sorted(ids), "records are not sorted by project ID"


def test_tracked_projects_sit_on_a_declared_forge():
    """Check every project this repository tracks can actually be read.

    An undeclared host raises mid-sample, one project at a time, which is a red
    scheduled run rather than something a reviewer would notice.
    """
    config = repo_config()
    assert not set(config.projects.repos) & set(config.projects.skip), (
        "a project cannot be both tracked and excused"
    )
    for project_id, url in config.projects.repos.items():
        assert url.startswith("https://"), f"{project_id} needs an https URL"
        forge_of(url, config.projects.forges)
    # Every excusal reads as a sentence, since it is the only record of why a
    # project shows nothing.
    for reason in config.projects.skip.values():
        assert reason.endswith("."), f"{reason!r} should read as a sentence"
