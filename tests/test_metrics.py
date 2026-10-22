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

"""Tests for the metric registry, its store, its collectors and its charts."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from click_extra.testing import CliRunner

from repomatic.cli.main import repomatic
from repomatic.config import Config, load_repomatic_config
from repomatic.github.gh import GhCommandError, GhOutputError
from repomatic.metrics.chart import (
    SERIES_PALETTE,
    ChartMode,
    ChartScale,
    ChartSpec,
    assign_colors,
    build_chart_data,
    css_class,
    render_chart,
    write_chart,
)
from repomatic.metrics.forge import ForgeMetrics
from repomatic.metrics.store import (
    CHARTABLE_METRICS,
    METRIC_HEADERS,
    METRICS,
    METRICS_BY_ID,
    PREDECESSOR_SUFFIX,
    STAR_HISTORY_MAX_PAGES,
    MetricRecord,
    MetricStore,
    Retention,
    SampleOutcome,
    SamplePhase,
    Source,
    chartable_metric,
    collected_subjects,
    is_predecessor,
    predecessor_key,
    reconstruct_from_github,
    sample_subject,
    series,
    subject_of,
)
from repomatic.pyproject import read_pyproject_toml
from repomatic.tabular import read_csv

REPO_ROOT = Path(__file__).parent.parent

STORE = REPO_ROOT / "docs" / "assets" / "metrics.csv"
"""This repository's own readings, accrued by the scheduled sampler."""

GITHUB_EPOCH = date(2008, 1, 1)
"""No star predates GitHub, so a reading dated earlier is a parsing fault.

Catches a store row holding a Unix timestamp where a calendar day belongs:
seconds read as a day land in the 1970s, decades before any repository.
"""

APRICOT = "https://github.com/fruits/apricot"
PAPAYA = "https://github.com/fruits/papaya"
OLD_PAPAYA = "https://github.com/old-fruits/papaya"

SUBJECTS = {"apricot": "fruits/apricot", "papaya": "fruits/papaya"}
PREDECESSORS = {"papaya": "old-fruits/papaya"}


def repo_config() -> Config:
    """Load this repository's own `[tool.repomatic]` section."""
    return load_repomatic_config(read_pyproject_toml(REPO_ROOT))


@pytest.fixture
def history():
    """Two subjects, one of them carrying a retired forerunner."""
    return MetricStore((
        MetricRecord(APRICOT, "stars", "2019-01-01", "0", Source.CREATED),
        MetricRecord(APRICOT, "stars", "2021-06-01", "120", Source.GITHUB),
        MetricRecord(APRICOT, "stars", "2026-08-16", "300", Source.SAMPLE),
        MetricRecord(APRICOT, "commit", "2026-08-16", "2026-08-15", Source.SAMPLE),
        MetricRecord(PAPAYA, "stars", "2022-01-01", "0", Source.CREATED),
        MetricRecord(PAPAYA, "stars", "2026-08-16", "90", Source.SAMPLE),
        MetricRecord(OLD_PAPAYA, "stars", "2015-01-01", "0", Source.CREATED),
        MetricRecord(OLD_PAPAYA, "stars", "2021-01-01", "40", Source.WAYBACK),
        # Past the handover, so the chart must clip it.
        MetricRecord(OLD_PAPAYA, "stars", "2026-08-16", "45", Source.SAMPLE),
    ))


# ---------------------------------------------------------------------------
# The registry.
# ---------------------------------------------------------------------------


def test_registry_is_coherent():
    """Check every metric is uniquely identified, sorted and documented."""
    ids = [metric.id for metric in METRICS]
    assert ids == sorted(ids), "metrics are not sorted by ID"
    assert len(ids) == len(set(ids)), "two metrics share an ID"
    assert set(METRICS_BY_ID) == set(ids)
    for metric in METRICS:
        assert metric.id == metric.id.lower()
        assert metric.label


def test_chartable_metrics_are_exactly_the_accruing_ones():
    """Check only a metric with a history is offered to a chart.

    An attribute holds one current value, so plotting it would draw a curve
    through a single point.
    """
    assert set(CHARTABLE_METRICS) == {m.id for m in METRICS if m.accrues}
    assert set(CHARTABLE_METRICS) == {
        m.id for m in METRICS if m.retention is Retention.HISTORY
    }
    assert "stars" in CHARTABLE_METRICS
    assert "commit" not in CHARTABLE_METRICS


def test_every_source_is_ranked():
    """Check every provenance has a rank, so `upsert` can compare any two.

    An unranked source fails inside `upsert`, mid-collection, on the one code
    path nobody watches.
    """
    for source in Source:
        assert isinstance(source.rank, int)
    # The anchor is the weakest claim, and a reconstruction the strongest.
    assert min(Source, key=lambda source: source.rank) is Source.CREATED
    assert max(Source, key=lambda source: source.rank) is Source.GITHUB


@pytest.mark.parametrize(
    ("metric", "chartable"),
    (("stars", True), ("commit", False), ("nonesuch", False)),
)
def test_chartable_metric_names_what_a_chart_can_plot(metric, chartable):
    """Check one lookup serves the chart spec and the series alike."""
    if chartable:
        assert chartable_metric(metric) is METRICS_BY_ID[metric]
    else:
        with pytest.raises(ValueError, match="Pick one of: stars"):
            chartable_metric(metric)


# ---------------------------------------------------------------------------
# The store.
# ---------------------------------------------------------------------------


def test_record_round_trips_through_a_csv_row():
    """Check a record survives the shape it is committed in."""
    record = MetricRecord(PAPAYA, "stars", "2026-08-16", "42", Source.SAMPLE)
    row = dict(zip(METRIC_HEADERS, record.as_row()))
    assert row["source"] == "sample"
    assert MetricRecord.from_row(row) == record
    assert record.count == 42


def test_record_refuses_an_unknown_source():
    """Check a provenance outside the vocabulary fails the load, loudly."""
    row = dict(zip(METRIC_HEADERS, (PAPAYA, "stars", "2026-08-16", "42", "rumor")))
    with pytest.raises(ValueError, match="rumor"):
        MetricRecord.from_row(row)


def test_upsert_is_idempotent_within_a_day():
    """Check re-running on the same day overwrites rather than appends."""
    store = MetricStore()
    assert store.upsert(
        MetricRecord(PAPAYA, "stars", "2026-08-16", "42", Source.SAMPLE)
    )
    assert not store.upsert(
        MetricRecord(PAPAYA, "stars", "2026-08-16", "42", Source.SAMPLE)
    )
    assert store.upsert(
        MetricRecord(PAPAYA, "stars", "2026-08-16", "43", Source.SAMPLE)
    )
    assert len(store) == 1


@pytest.mark.parametrize(
    ("stored", "incoming", "wins"),
    (
        # A weaker reading never degrades a stronger one already on file.
        ("github", "wayback", False),
        ("github", "star-history", False),
        ("sample", "wayback", False),
        ("wayback", "github", True),
        ("wayback", "sample", True),
        ("star-history", "wayback", True),
        ("sample", "github", True),
        # The creation origin assumes a count of zero, so any measurement of
        # that same day beats it, and it never overwrites one.
        ("created", "github", True),
        ("created", "wayback", True),
        ("github", "created", False),
        ("sample", "created", False),
    ),
)
def test_upsert_honors_source_precedence(stored, incoming, wins):
    """Check a weaker provenance cannot overwrite a stronger one for a day."""
    store = MetricStore()
    store.upsert(MetricRecord(PAPAYA, "stars", "2026-08-16", "10", Source(stored)))
    store.upsert(MetricRecord(PAPAYA, "stars", "2026-08-16", "99", Source(incoming)))
    stored_value = store.records[(PAPAYA, "stars", "2026-08-16")].value
    assert stored_value == ("99" if wins else "10")


def test_an_accruing_metric_keeps_every_day():
    """Check a counter's past readings are all retained."""
    store = MetricStore()
    for day, value in (("2026-08-01", "10"), ("2026-08-08", "20")):
        store.upsert(MetricRecord(PAPAYA, "stars", day, value, Source.SAMPLE))
    assert len(store) == 2


def test_an_attribute_keeps_one_row_and_dates_the_change():
    """Check an attribute holds a single row, restamped only when it moves.

    A quiet week must leave the file untouched rather than restamping every
    row, and the surviving date is when the value last *changed*, which is what
    dates a dead project's row honestly.
    """
    store = MetricStore()
    assert store.upsert(
        MetricRecord(PAPAYA, "commit", "2026-08-01", "2026-07-30", Source.SAMPLE)
    )
    # Same value a week later: nothing moves, and the old date stands.
    assert not store.upsert(
        MetricRecord(PAPAYA, "commit", "2026-08-08", "2026-07-30", Source.SAMPLE)
    )
    assert list(store.records) == [(PAPAYA, "commit", "2026-08-01")]
    # A moved value replaces the row and takes the new date.
    assert store.upsert(
        MetricRecord(PAPAYA, "commit", "2026-08-15", "2026-08-14", Source.SAMPLE)
    )
    assert list(store.records) == [(PAPAYA, "commit", "2026-08-15")]


def test_an_attribute_loaded_twice_collapses_to_the_newest_reading():
    """Check a file holding two rows of an attribute heals on the next change.

    The index built at load sees every row of the subject, so the moved value
    replaces all of them, not only the one it happened to find first.
    """
    store = MetricStore((
        MetricRecord(PAPAYA, "commit", "2026-08-01", "2026-07-30", Source.SAMPLE),
        MetricRecord(PAPAYA, "commit", "2026-08-08", "2026-08-07", Source.SAMPLE),
    ))
    assert not store.upsert(
        MetricRecord(PAPAYA, "commit", "2026-08-15", "2026-08-07", Source.SAMPLE)
    )
    assert store.upsert(
        MetricRecord(PAPAYA, "commit", "2026-08-22", "2026-08-21", Source.SAMPLE)
    )
    assert list(store.records) == [(PAPAYA, "commit", "2026-08-22")]


def test_upsert_rejects_an_unregistered_metric():
    """Check a typo in a metric ID fails loudly rather than storing a stray row."""
    with pytest.raises(KeyError):
        MetricStore().upsert(
            MetricRecord(PAPAYA, "starz", "2026-08-16", "1", Source.SAMPLE)
        )


def test_save_writes_a_sorted_csv(tmp_path, history):
    """Check the store is one row per reading, sorted for a readable diff."""
    store = tmp_path / "nested" / "metrics.csv"
    assert history.save(store) is True
    text = store.read_text(encoding="UTF-8")
    assert text.startswith("repo,metric,date,value,source\n")
    assert text.endswith("\n")
    # One line per record, plus the header: the whole point over JSON.
    assert len(text.splitlines()) == len(history) + 1

    rows = read_csv(store)
    keys = [(row["repo"], row["metric"], row["date"]) for row in rows]
    assert keys == sorted(keys)

    assert history.save(store) is False


def test_save_writes_exactly_what_the_store_holds(tmp_path):
    """Check the file becomes what the store holds, deletions included.

    A collector re-deriving a whole curve has to be able to retire a reading it
    no longer dates, so the write cannot merge the file back under itself.
    """
    store = tmp_path / "metrics.csv"
    first = MetricRecord(PAPAYA, "stars", "2026-08-01", "10", Source.SAMPLE)
    MetricStore((first,)).save(store)
    second = MetricRecord(APRICOT, "stars", "2026-08-02", "20", Source.SAMPLE)
    MetricStore((second,)).save(store)
    assert list(MetricStore.load(store).records) == [second.key]


def test_save_cannot_resurrect_a_pruned_attribute(tmp_path):
    """Check a round trip through the file leaves an attribute one reading.

    Loading, upserting and writing is what every collector does, and a
    superseded row surviving it would leave two readings of a metric that
    keeps one.
    """
    store = tmp_path / "metrics.csv"
    readings = MetricStore.load(store)
    readings.upsert(
        MetricRecord(PAPAYA, "commit", "2026-08-01", "2026-07-30", Source.SAMPLE)
    )
    readings.save(store)

    readings = MetricStore.load(store)
    readings.upsert(
        MetricRecord(PAPAYA, "commit", "2026-08-15", "2026-08-14", Source.SAMPLE)
    )
    readings.save(store)

    stored = MetricStore.load(store)
    assert list(stored.records) == [(PAPAYA, "commit", "2026-08-15")]


def test_load_is_loud_on_a_corrupt_store(tmp_path):
    """Check a malformed store raises instead of being silently clobbered."""
    store = tmp_path / "metrics.csv"
    store.write_text("repo,metric\nx,y\n", encoding="UTF-8")
    with pytest.raises(ValueError, match="Malformed metric store"):
        MetricStore.load(store)


def test_load_tolerates_a_missing_store(tmp_path):
    """Check a first run reads an empty store rather than failing."""
    assert len(MetricStore.load(tmp_path / "absent.csv")) == 0


# ---------------------------------------------------------------------------
# Subjects and series.
# ---------------------------------------------------------------------------


def test_collected_subjects_canonicalizes_and_marks_a_forerunner():
    """Check a slug and a URL land on one spelling, forerunners tagged."""
    assert collected_subjects(
        {"apricot": "fruits/apricot", "papaya": "https://gitlab.com/fruits/papaya"},
        {"papaya": "old-fruits/papaya"},
    ) == {
        "apricot": APRICOT,
        "papaya": "https://gitlab.com/fruits/papaya",
        "papaya" + PREDECESSOR_SUFFIX: OLD_PAPAYA,
    }


@pytest.mark.parametrize(
    ("key", "forerunner"),
    (
        ("papaya", False),
        ("papaya" + PREDECESSOR_SUFFIX, True),
        # The suffix marks a forerunner only at the end of the key.
        ("papaya" + PREDECESSOR_SUFFIX + "-2", False),
    ),
)
def test_predecessor_keys_name_their_subject(key, forerunner):
    """Check a series key says whether it is a forerunner's, and whose."""
    assert is_predecessor(key) is forerunner
    assert subject_of(key) == ("papaya" if forerunner else key)
    if forerunner:
        assert predecessor_key(subject_of(key)) == key


def test_series_clips_a_forerunner_at_the_handover(history):
    """Check a forerunner's line stops where its successor's begins.

    The archived repository keeps collecting the odd star, and drawing that
    tail would run it the whole width of the chart beside its successor,
    reading as two live projects rather than one handover.
    """
    grouped = series(history, SUBJECTS, "stars", PREDECESSORS)
    prior = grouped["papaya" + PREDECESSOR_SUFFIX]
    handover = grouped["papaya"][0][0]
    assert prior
    assert max(day for day, _value in prior) <= handover
    # The store keeps the discarded row: only the chart drops it.
    assert (OLD_PAPAYA, "stars", "2026-08-16") in history.records


def test_series_refuses_a_metric_with_no_history(history):
    """Check charting an attribute is refused, naming what can be plotted."""
    with pytest.raises(ValueError, match="no history to chart"):
        series(history, SUBJECTS, "commit")
    with pytest.raises(ValueError, match="no history to chart"):
        series(history, SUBJECTS, "nonesuch")


def test_series_skips_a_subject_with_no_reading():
    """Check an unsampled subject is absent rather than an empty curve."""
    assert series(MetricStore(), SUBJECTS) == {}


# ---------------------------------------------------------------------------
# Collectors.
# ---------------------------------------------------------------------------


def test_sample_subject_records_every_metric_and_the_origin(monkeypatch):
    """Check one call yields each metric the forge answered, plus the birth."""
    monkeypatch.setattr(
        "repomatic.metrics.store.repo_metrics",
        lambda url, extra=None: ForgeMetrics(
            stars=57,
            created="2021-12-09",
            release="2026-08-01",
            release_source="tag",
            commit="2026-08-15",
        ),
    )
    store = MetricStore()
    outcome = sample_subject(store, "papaya", PAPAYA, day="2026-08-16")

    assert outcome.stars == 57
    assert outcome.rows == 5
    assert outcome.phase is SamplePhase.FORWARD
    stored = {(key[1], record.value) for key, record in store.records.items()}
    assert stored == {
        ("stars", "57"),
        ("stars", "0"),
        ("commit", "2026-08-15"),
        ("release", "2026-08-01"),
        ("release_source", "tag"),
    }
    assert store.records[(PAPAYA, "stars", "2021-12-09")].source is Source.CREATED


def test_sample_subject_adds_no_row_for_what_a_forge_did_not_answer(monkeypatch):
    """Check a project with no release stores no blank release row."""
    monkeypatch.setattr(
        "repomatic.metrics.store.repo_metrics",
        lambda url, extra=None: ForgeMetrics(stars=3, created="2020-01-01"),
    )
    store = MetricStore()
    sample_subject(store, "papaya", PAPAYA, day="2026-08-16")
    assert {key[1] for key in store.records} == {"stars"}


def test_sample_subject_survives_an_unreadable_forge(monkeypatch):
    """Check one unreachable subject does not cost the others."""
    monkeypatch.setattr(
        "repomatic.metrics.store.repo_metrics", lambda url, extra=None: None
    )
    store = MetricStore()
    outcome = sample_subject(store, "papaya", PAPAYA, day="2026-08-16")
    assert outcome.note == "unreadable"
    assert not store


def star_week(sunday: str, days: list[int]) -> dict[str, object]:
    """Build one week of the star-history endpoint's payload.

    :param sunday: Calendar date labelling the week, as the endpoint reports it.
    :param days: Stars gained on each of the week's seven days.
    """
    start = datetime.fromisoformat(sunday).replace(tzinfo=UTC)
    return {"week": int(start.timestamp()), "total": sum(days), "days": days}


def test_reconstruct_accumulates_one_row_per_week_that_moved(monkeypatch):
    """Check the daily buckets collapse into a cumulative weekly curve."""
    pages = [
        # Newest first, as the endpoint serves them.
        [
            star_week("2022-02-27", [0, 1, 0, 0, 0, 0, 0]),
            star_week("2022-01-30", [0, 0, 0, 0, 0, 0, 0]),
            star_week("2021-12-05", [0, 0, 0, 0, 2, 0, 0]),
        ],
        [],
    ]
    monkeypatch.setattr("repomatic.metrics.store.gh_json", lambda args: pages.pop(0))
    store = MetricStore()
    outcome = reconstruct_from_github(store, "papaya", PAPAYA)

    assert outcome.stars == 3
    assert outcome.phase is SamplePhase.RECONSTRUCT
    # Dated on the last day of the week that gained a star, not the week start.
    assert store.records[(PAPAYA, "stars", "2021-12-09")].value == "2"
    assert store.records[(PAPAYA, "stars", "2022-02-28")].value == "3"
    # The quiet week in between earns no row at all.
    assert len(store) == 2
    assert all(record.source is Source.GITHUB for record in store.records.values())


def test_reconstruct_dates_a_day_without_converting_the_timezone(monkeypatch):
    """Check a day is the week's label plus its offset, left in GitHub's zone.

    The endpoint buckets days in `America/Los_Angeles`. Its `week` field is
    midnight UTC on the date labelling that local week, so the calendar date of
    that instant already carries the local day: converting it into a zone would
    shift every reading.
    """
    pages = [[star_week("2024-11-10", [0, 1, 0, 0, 0, 0, 0])], []]
    monkeypatch.setattr("repomatic.metrics.store.gh_json", lambda args: pages.pop(0))
    store = MetricStore()
    reconstruct_from_github(store, "papaya", PAPAYA)
    assert (PAPAYA, "stars", "2024-11-11") in store.records


def test_reconstruct_replaces_the_rows_it_wrote_before(monkeypatch):
    """Check a re-derived curve drops the days its previous run had claimed.

    The walk rebuilds the whole curve, so a day that no longer carries a
    reading must disappear rather than linger stating a total the rest of the
    curve contradicts. Rows from every other source are left alone.
    """
    stale = MetricRecord(PAPAYA, "stars", "2019-06-01", "99", Source.GITHUB)
    kept = MetricRecord(PAPAYA, "stars", "2019-06-02", "98", Source.WAYBACK)
    elsewhere = MetricRecord(APRICOT, "stars", "2019-06-01", "97", Source.GITHUB)
    store = MetricStore((stale, kept, elsewhere))

    pages = [[star_week("2021-12-05", [0, 0, 0, 0, 2, 0, 0])], []]
    monkeypatch.setattr("repomatic.metrics.store.gh_json", lambda args: pages.pop(0))
    outcome = reconstruct_from_github(store, "papaya", PAPAYA)

    assert stale.key not in store.records
    assert store.records[kept.key] == kept
    assert store.records[elsewhere.key] == elsewhere
    assert store.records[(PAPAYA, "stars", "2021-12-09")].value == "2"
    # One row written, one retired.
    assert outcome.rows == 2


def test_reconstruct_skips_a_subject_off_github():
    """Check a GitLab subject is skipped with a reason, not failed.

    The star history is a GitHub endpoint; every other forge simply has nothing
    to reconstruct from.
    """
    store = MetricStore()
    outcome = reconstruct_from_github(
        store, "papaya", "https://gitlab.com/fruits/papaya"
    )
    assert "gitlab.com" in outcome.note
    assert not store


def test_reconstruct_reports_a_repository_that_is_gone(monkeypatch):
    """Check a 404 on the first page reads as a skip, not a failure."""

    def refuse(args):
        msg = "gh: Not Found (HTTP 404)"
        raise GhCommandError(msg)

    monkeypatch.setattr("repomatic.metrics.store.gh_json", refuse)
    store = MetricStore()
    outcome = reconstruct_from_github(store, "papaya", PAPAYA)
    assert outcome.note == "no such repository"
    assert not store


def test_reconstruct_reports_a_repository_with_no_star(monkeypatch):
    """Check an unstarred repository is a note, not an empty curve."""
    monkeypatch.setattr("repomatic.metrics.store.gh_json", lambda args: [])
    store = MetricStore()
    outcome = reconstruct_from_github(store, "papaya", PAPAYA)
    assert outcome.note == "no star on record"
    assert not store


def test_reconstruct_writes_nothing_when_pagination_breaks_midway(monkeypatch):
    """Check a failure mid-walk abandons the subject rather than truncating.

    The walk runs newest first, so a truncated one holds only recent weeks:
    totalling those would date a fraction of the stars as the whole history,
    and every point would look as legitimate as the rest.
    """
    calls = {"count": 0}

    def flaky(args):
        calls["count"] += 1
        if calls["count"] == 1:
            return [star_week("2026-08-30", [1, 0, 0, 0, 0, 0, 0])]
        msg = "gh: Bad gateway (HTTP 502)"
        raise GhCommandError(msg)

    monkeypatch.setattr("repomatic.metrics.store.gh_json", flaky)
    store = MetricStore()
    outcome = reconstruct_from_github(store, "papaya", PAPAYA)
    assert outcome.note == "abandoned on page 2: gh: Bad gateway (HTTP 502)"
    assert not store


def test_reconstruct_gives_up_on_an_unparsable_page(monkeypatch):
    """Check a payload that is not JSON abandons the subject."""

    def garbled(args):
        msg = "gh api returned unparsable JSON"
        raise GhOutputError(msg)

    monkeypatch.setattr("repomatic.metrics.store.gh_json", garbled)
    store = MetricStore()
    outcome = reconstruct_from_github(store, "papaya", PAPAYA)
    assert outcome.note == "unparsable page 1"
    assert not store


def test_reconstruct_walk_is_bounded(monkeypatch):
    """Check a server that never runs out of pages cannot loop forever.

    GitHub refuses past {data}`~repomatic.metrics.store.STAR_HISTORY_MAX_PAGES`, so
    the walk stops there on its own rather than trusting the endpoint to end the
    sequence.
    """
    calls = {"count": 0}

    def endless(args):
        calls["count"] += 1
        return [star_week("2021-12-05", [1, 0, 0, 0, 0, 0, 0])]

    monkeypatch.setattr("repomatic.metrics.store.gh_json", endless)
    store = MetricStore()
    reconstruct_from_github(store, "papaya", PAPAYA)
    assert calls["count"] == STAR_HISTORY_MAX_PAGES


@pytest.mark.parametrize(
    ("outcome", "row"),
    (
        (
            SampleOutcome("papaya", PAPAYA, SamplePhase.FORWARD, stars=57, rows=5),
            ("papaya", "forward", PAPAYA, "57", "5", "—"),
        ),
        (
            SampleOutcome("papaya", PAPAYA, SamplePhase.RECONSTRUCT, note="gone"),
            ("papaya", "reconstruct", PAPAYA, "—", "0", "gone"),
        ),
    ),
)
def test_sample_outcome_row_fills_every_column(outcome, row):
    """Check the table row an outcome renders, one cell per column."""
    assert outcome.row() == row


# ---------------------------------------------------------------------------
# Charts.
# ---------------------------------------------------------------------------


def test_palette_slots_are_distinct_and_two_toned():
    """Check every categorical slot is its own hue in both themes."""
    assert len(SERIES_PALETTE) >= 12
    lights = [light for light, _dark in SERIES_PALETTE]
    darks = [dark for _light, dark in SERIES_PALETTE]
    assert len(set(lights)) == len(lights)
    assert len(set(darks)) == len(darks)
    for light, dark in SERIES_PALETTE:
        assert light != dark
        assert re.fullmatch(r"#[0-9a-f]{6}", light)
        assert re.fullmatch(r"#[0-9a-f]{6}", dark)


def test_assign_colors_is_positional_with_overrides_by_name():
    """Check a subject keeps its slot, and a pinned hue wins over the palette."""
    colors = assign_colors(["apricot", "papaya"], {"papaya": ["#111111", "#222222"]})
    assert colors["apricot"] == SERIES_PALETTE[0]
    assert colors["papaya"] == ("#111111", "#222222")


def test_assign_colors_refuses_to_cycle_the_palette():
    """Check a chart bigger than the palette raises rather than repeating a hue."""
    names = [f"series-{index}" for index in range(len(SERIES_PALETTE) + 1)]
    with pytest.raises(ValueError, match="palette holds"):
        assign_colors(names)


def test_assign_colors_rejects_a_malformed_override():
    """Check an override that is not a light and dark pair is refused."""
    with pytest.raises(ValueError, match=r"\[light, dark\] pair"):
        assign_colors(["papaya"], {"papaya": ["#111111"]})


@pytest.mark.parametrize(
    ("name", "expected"),
    (
        ("papaya", "papaya"),
        ("Papaya", "papaya"),
        ("meta-package-manager", "meta-package-manager"),
        ("fruits/papaya", "fruits-papaya"),
        ("papaya 2.0", "papaya-2-0"),
        ("...", "series"),
    ),
)
def test_css_class(name, expected):
    """Check a configured subject name folds into a usable CSS class."""
    assert css_class(name) == expected


def test_build_chart_data_rejects_colliding_css_classes():
    """Check two names folding onto one class are refused, not silently merged."""
    grouped = {"papaya": [(date(2020, 1, 1), 1)], "Papaya": [(date(2020, 1, 1), 2)]}
    spec = ChartSpec(output=Path("chart.svg"), only=("papaya", "Papaya"))
    with pytest.raises(ValueError, match="fold onto the CSS class"):
        build_chart_data(grouped, spec)


def test_build_chart_data_is_loud_when_a_chart_plots_nothing():
    """Check an unsampled chart names itself rather than drawing an empty box."""
    spec = ChartSpec(output=Path("chart.svg"), only=("papaya",))
    with pytest.raises(ValueError, match=r"chart\.svg has no stars history to plot"):
        build_chart_data({}, spec)


def test_chart_spec_rejects_an_unknown_mode():
    """Check a typo in `mode` raises instead of silently drawing a calendar."""
    with pytest.raises(
        ValueError, match=r"Unsupported chart mode 'sideways' for chart\.svg"
    ):
        ChartSpec.from_mapping({"output": "chart.svg", "mode": "sideways"})
    assert set(ChartMode) == {"absolute", "relative"}
    # The two axes are separate settings, so the vertical one is not reachable
    # by naming it here: a `mode` that silently fell back would draw a chart
    # misreading every series on it by orders of magnitude.
    with pytest.raises(ValueError, match="Unsupported chart mode"):
        ChartSpec.from_mapping({"output": "chart.svg", "mode": "logarithmic"})


def test_chart_spec_rejects_an_unknown_scale():
    """Check a typo in `scale` raises instead of silently drawing a linear axis."""
    with pytest.raises(ValueError, match="Unsupported chart scale"):
        ChartSpec.from_mapping({"output": "chart.svg", "scale": "log10"})
    assert set(ChartScale) == {"linear", "logarithmic"}


def test_chart_spec_axes_are_independent():
    """Check a comparison chart can slide the origins and compress the counts."""
    spec = ChartSpec(
        output=Path("chart.svg"),
        mode=ChartMode.RELATIVE,
        scale=ChartScale.LOGARITHMIC,
    )
    assert spec.relative
    assert spec.logarithmic
    assert not ChartSpec(output=Path("chart.svg")).logarithmic


def test_chart_spec_rejects_a_metric_with_no_history():
    """Check a chart pointed at an attribute is refused when it is declared."""
    with pytest.raises(
        ValueError, match=r"Chart c\.svg: Metric 'commit' has no history"
    ):
        ChartSpec.from_mapping({"output": "c.svg", "metric": "commit"})
    with pytest.raises(ValueError, match="has no history to chart"):
        ChartSpec(output=Path("c.svg"), metric="commit")


def test_chart_spec_from_mapping():
    """Check a configuration entry becomes a spec, shorthand included."""
    spec = ChartSpec.from_mapping({
        "output": "docs/assets/chart.svg",
        "metric": "stars",
        "mode": "relative",
        "only": ["papaya"],
        "scale": "logarithmic",
        "title": "Papaya stars",
    })
    assert spec == ChartSpec(
        output=Path("docs/assets/chart.svg"),
        metric="stars",
        mode=ChartMode.RELATIVE,
        only=("papaya",),
        scale=ChartScale.LOGARITHMIC,
        title="Papaya stars",
    )
    assert ChartSpec.from_mapping({"output": "c.svg"}).scale is ChartScale.LINEAR
    assert ChartSpec.from_mapping({"output": "c.svg", "only": "papaya"}).only == (
        "papaya",
    )
    assert ChartSpec.from_mapping({"output": "c.svg"}).mode is ChartMode.ABSOLUTE
    assert ChartSpec.from_mapping({"output": "c.svg"}).metric == "stars"


@pytest.mark.parametrize("entry", ({}, {"output": ""}, {"mode": "relative"}))
def test_chart_spec_from_mapping_needs_an_output(entry):
    """Check a chart with nowhere to write is refused."""
    with pytest.raises(ValueError, match="needs an output path"):
        ChartSpec.from_mapping(entry)


def test_chart_spec_from_mapping_rejects_a_scalar_only():
    """Check an `only` that is neither a string nor a list is refused."""
    with pytest.raises(ValueError, match="list of series names"):
        ChartSpec.from_mapping({"output": "c.svg", "only": 3})


def test_render_chart_labels_every_series(history):
    """Check the generator draws, labels and colours each plotted curve."""
    grouped = series(history, SUBJECTS, "stars", PREDECESSORS)
    data = build_chart_data(grouped, ChartSpec(output=Path("chart.svg")))
    svg = render_chart(data, label="Stars", stamp="2026-08-16")

    assert svg.startswith("<svg ")
    assert svg.rstrip().endswith("</svg>")
    # An accessible name, since the chart carries meaning no caption repeats.
    assert 'role="img"' in svg and "aria-label=" in svg
    # Identity is never colour alone: every series is directly labelled.
    for name in SUBJECTS:
        assert f'class="lbl s-{name}"' in svg
        light, dark = data.colors[name]
        assert light in svg
        assert dark in svg
    # The dark steps are selected, not an automatic flip of the light ones.
    assert "prefers-color-scheme: dark" in svg
    # A forerunner is drawn broken away from its successor, never joined to it.
    assert "stroke-dasharray" in svg
    assert 'class="lbl prior s-papaya"' in svg
    assert "sampled on 2026-08-16" in svg


def test_render_chart_caption_follows_the_plotted_metric(history):
    """Check the axis names the metric rather than hard-coding stars."""
    grouped = series(history, SUBJECTS, "stars", PREDECESSORS)
    data = build_chart_data(grouped, ChartSpec(output=Path("chart.svg")))
    svg = render_chart(data, label=METRICS_BY_ID["stars"].label, stamp="2026-08-16")
    assert "Stars, 2015-01-01 to 2026-08-16" in svg


def test_render_chart_honors_a_series_subset(history):
    """Check `only` drops the peers, their forerunners included."""
    grouped = series(history, SUBJECTS, "stars", PREDECESSORS)
    spec = ChartSpec(output=Path("chart.svg"), only=("apricot",))
    svg = render_chart(
        build_chart_data(grouped, spec), label="Stars", stamp="2026-08-16"
    )
    assert 'class="lbl s-apricot"' in svg
    assert "s-papaya" not in svg


def test_render_chart_relative_swaps_the_calendar_for_project_age(history):
    """Check the by-age axis carries no calendar year among its ticks."""
    grouped = series(history, SUBJECTS, "stars", PREDECESSORS)
    spec = ChartSpec(output=Path("chart.svg"), mode=ChartMode.RELATIVE)
    svg = render_chart(
        build_chart_data(grouped, spec),
        relative=True,
        label="Stars",
        stamp="2026-08-16",
    )
    assert "years</text>" in svg
    years = {str(year) for year in range(2000, 2100)}
    assert not years.intersection(re.findall(r">([^<>]+)</text>", svg))


# A pair three orders of magnitude apart, which is the gap the logarithmic
# scale exists for: on a linear axis the smaller curve is drawn onto the floor.
LOPSIDED = {
    "apricot": [(date(2020, 1, 1), 0), (date(2026, 1, 1), 57)],
    "papaya": [(date(2020, 1, 1), 0), (date(2026, 1, 1), 25057)],
}


def _final_y(svg: str, name: str) -> float:
    """Read the last plotted vertical coordinate of one series out of the SVG."""
    points = re.search(rf'<polyline class="s-{name}" points="([^"]+)"', svg)
    assert points, f"no polyline drawn for {name}"
    return float(points.group(1).split()[-1].split(",")[1])


def test_render_chart_logarithmic_axis_is_labelled_in_decades():
    """Check the gridlines are powers of ten, and the caption says so."""
    data = build_chart_data(LOPSIDED, ChartSpec(output=Path("chart.svg")))
    svg = render_chart(data, logarithmic=True, label="Stars", stamp="2026-08-16")
    ticks = re.findall(r'<text class="tick"[^>]*>([^<]+)</text>', svg)
    assert ["0", "1", "10", "100", "1,000", "10,000"] == [
        tick for tick in ticks if not tick.isalpha() and "20" not in tick
    ]
    # Named in the accessible description too, not just the visible caption.
    assert "logarithmic scale" in svg
    assert 'aria-label="Stars history, logarithmic scale"' in svg


def test_render_chart_logarithmic_lifts_a_series_off_the_axis():
    """Check the smaller curve stays readable beside one 440 times its size."""
    data = build_chart_data(LOPSIDED, ChartSpec(output=Path("chart.svg")))
    linear = _final_y(render_chart(data, label="Stars", stamp="2026-08-16"), "apricot")
    logarithmic = _final_y(
        render_chart(data, logarithmic=True, label="Stars", stamp="2026-08-16"),
        "apricot",
    )
    # The plot floor is 414 and its ceiling 28, so a smaller y sits higher.
    assert linear > 400, "a linear axis should pin the small series to the floor"
    assert logarithmic < 300, "a logarithmic axis should lift it clear"
    # The larger series still tops out at the ceiling, so the gap is compressed
    # rather than the whole chart being slid upwards.
    peak = _final_y(
        render_chart(data, logarithmic=True, label="Stars", stamp="2026-08-16"),
        "papaya",
    )
    assert peak == pytest.approx(28, abs=1)


def test_render_chart_logarithmic_keeps_a_zero_on_the_floor():
    """Check a series carrying the created origin draws it, not drops it.

    A count of zero has no logarithm, so it is placed on the floor the band at
    the bottom of the plot reserves. Dropping it instead would start each curve
    at its first star, which is a different and unstated claim. Not every
    series carries one: a repository starred on its creation day loses the
    anchor to that day's measurement, per
    {attr}`repomatic.metrics.store.Source.rank`.
    """
    data = build_chart_data(LOPSIDED, ChartSpec(output=Path("chart.svg")))
    svg = render_chart(data, logarithmic=True, label="Stars", stamp="2026-08-16")
    first = re.search(r'<polyline class="s-apricot" points="([^"]+)"', svg)
    assert first
    assert float(first.group(1).split()[0].split(",")[1]) == pytest.approx(414, abs=1)


def test_render_chart_escapes_a_series_name():
    """Check a name carrying markup cannot break out of the SVG text node."""
    grouped = {"a & b": [(date(2020, 1, 1), 1), (date(2021, 1, 1), 5)]}
    data = build_chart_data(grouped, ChartSpec(output=Path("chart.svg")))
    svg = render_chart(data, label="Stars", stamp="2026-08-16")
    assert "a &amp; b" in svg
    assert ">a & b" not in svg


@pytest.mark.parametrize("peak", (0, 1))
@pytest.mark.parametrize("logarithmic", (False, True))
def test_render_chart_survives_a_flat_or_single_star_history(peak, logarithmic):
    """Check a peak of 0 or 1 renders instead of dividing by zero.

    A brand-new repository's history is exactly this: the zero-star creation
    row alone, or that row plus its first star. Halving the axis step without
    a floor would bring it to 0, and the ceiling division would then crash on
    both scales.
    """
    grouped = {"papaya": [(date(2026, 1, 1), 0), (date(2026, 2, 1), peak)]}
    data = build_chart_data(grouped, ChartSpec(output=Path("chart.svg")))
    svg = render_chart(data, logarithmic=logarithmic, label="Stars", stamp="2026-08-16")
    assert '<polyline class="s-papaya"' in svg


def test_write_chart_is_convergent(tmp_path, history):
    """Check redrawing an unmoved history rewrites nothing."""
    grouped = series(history, SUBJECTS, "stars", PREDECESSORS)
    spec = ChartSpec(output=tmp_path / "nested" / "chart.svg")
    assert write_chart(grouped, spec) is True
    assert write_chart(grouped, spec) is False
    # Stamped with the newest reading, not with the day the chart was drawn.
    assert "sampled on 2026-08-16" in spec.output.read_text(encoding="UTF-8")


# ---------------------------------------------------------------------------
# The sample-metrics command.
# ---------------------------------------------------------------------------

SAMPLING_CONFIG = """\
[tool.repomatic]
metrics.store = "metrics.csv"
metrics.subjects.papaya = "fruits/papaya"
metrics.sync = true
"""
"""A project sampling one subject into a store at its root."""


@pytest.fixture
def sampling_project(tmp_path, monkeypatch):
    """A project sampling `papaya`, as the working directory.

    The forge answers 57 stars and a creation date. The star history is not
    reachable, as `gh` never runs in the suite.
    """
    (tmp_path / "pyproject.toml").write_text(SAMPLING_CONFIG, encoding="UTF-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "repomatic.metrics.store.repo_metrics",
        lambda url, extra=None: ForgeMetrics(stars=57, created="2021-12-09"),
    )
    return tmp_path


def test_sample_metrics_needs_a_subject(tmp_path, monkeypatch):
    """With no subject configured, the run reads no forge and writes no store."""
    (tmp_path / "pyproject.toml").write_text(
        "[tool.repomatic]\nmetrics.sync = true\n", encoding="UTF-8"
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(repomatic, ["sample-metrics"])

    assert result.exit_code == 0, result.output
    assert "No repository in [tool.repomatic.metrics] subjects" in result.output
    # The default store sits under docs/.
    assert not (tmp_path / "docs").exists()


def test_sample_metrics_records_each_reading_once(sampling_project):
    """A second run on the same day leaves the store as the first one wrote it."""
    first = CliRunner().invoke(repomatic, ["sample-metrics", "--no-render"])
    second = CliRunner().invoke(repomatic, ["sample-metrics", "--no-render"])

    assert first.exit_code == 0, first.output
    assert "Recorded 2 reading(s) in metrics.csv." in first.output
    readings = MetricStore.load(sampling_project / "metrics.csv")
    assert [record.value for record in readings.held(PAPAYA, "stars")] == ["0", "57"]
    assert second.exit_code == 0, second.output
    assert "metrics.csv already up to date." in second.output


def test_sample_metrics_redraws_each_configured_chart(sampling_project):
    """A chart is drawn from the readings, and left alone once current."""
    with (sampling_project / "pyproject.toml").open("a", encoding="UTF-8") as config:
        config.write('metrics.charts = [ { output = "stars.svg" } ]\n')

    first = CliRunner().invoke(repomatic, ["sample-metrics"])
    second = CliRunner().invoke(repomatic, ["sample-metrics"])

    assert first.exit_code == 0, first.output
    assert "Redrew stars.svg." in first.output
    assert (sampling_project / "stars.svg").is_file()
    assert second.exit_code == 0, second.output
    assert "Unchanged stars.svg." in second.output


def test_sample_metrics_draws_nothing_without_a_reading(sampling_project, monkeypatch):
    """A forge that answers nothing leaves no history to chart."""
    monkeypatch.setattr(
        "repomatic.metrics.store.repo_metrics", lambda url, extra=None: None
    )

    result = CliRunner().invoke(repomatic, ["sample-metrics"])

    assert result.exit_code == 0, result.output
    assert "No reading recorded yet, so no chart to draw." in result.output


def test_sample_metrics_refuses_a_malformed_store(sampling_project):
    """A store with foreign columns ends the run before any forge is read."""
    (sampling_project / "metrics.csv").write_text("kiwi,mango\n1,2\n", encoding="UTF-8")

    result = CliRunner().invoke(repomatic, ["sample-metrics", "--no-render"])

    assert result.exit_code == 1, result.output
    assert "Error:" in result.output


def test_sample_metrics_carries_the_store_from_its_branch(sampling_project):
    """`--carry-from` restores the store before the readings are appended."""
    with patch("repomatic.cli.repo.carry_pr_branch_paths") as carry:
        result = CliRunner().invoke(
            repomatic,
            ["sample-metrics", "--no-render", "--carry-from", "sample-metrics"],
        )

    assert result.exit_code == 0, result.output
    carry.assert_called_once_with("sample-metrics", (Path("metrics.csv"),))


# ---------------------------------------------------------------------------
# This repository's own committed store.
# ---------------------------------------------------------------------------


def test_committed_store_is_well_formed():
    """Check the readings this repository accrues keep their expected shape.

    Guards a file a scheduled job appends to unattended: a duplicated reading, a
    subject that left the configuration, an unknown provenance or an attribute
    holding two rows would all surface here rather than as a misdrawn chart.
    """
    if not STORE.exists():
        pytest.skip("no reading recorded yet")
    rows = read_csv(STORE)
    assert rows, "the store exists but holds nothing"

    config = repo_config()
    tracked = set(
        collected_subjects(
            config.metrics.subjects, config.metrics.predecessors
        ).values()
    )
    today = datetime.now(tz=UTC).date()
    seen: set[tuple[str, str, str]] = set()
    attributes: Counter[tuple[str, str]] = Counter()

    for row in rows:
        assert set(row) == set(METRIC_HEADERS)
        # A provenance outside the vocabulary fails the parse here.
        record = MetricRecord.from_row(row)
        assert record.repo in tracked, f"{record.repo} left the configuration"
        assert record.metric in METRICS_BY_ID
        # Dates are plain ISO days, never timestamps: a chart plots daily.
        assert GITHUB_EPOCH <= date.fromisoformat(record.day) <= today
        assert record.key not in seen, f"duplicate reading for {record.key}"
        seen.add(record.key)
        if METRICS_BY_ID[record.metric].accrues:
            assert record.count >= 0
        else:
            attributes[record.subject_key] += 1

    assert not [k for k, n in attributes.items() if n > 1], (
        "an attribute holds more than one row"
    )
    keys = [(row["repo"], row["metric"], row["date"]) for row in rows]
    assert keys == sorted(keys)


def test_committed_charts_match_the_committed_store(tmp_path):
    """Check every configured chart is what the renderer draws from the store.

    The scheduled job writes the store and the charts in one commit, so a
    difference here is a renderer change that skipped re-rendering them.
    """
    if not STORE.exists():
        pytest.skip("no reading recorded yet")
    config = repo_config()
    readings = MetricStore.load(STORE)
    assert config.metrics.charts, "no chart declared to draw"
    for entry in config.metrics.charts:
        spec = ChartSpec.from_mapping(entry)
        committed = REPO_ROOT / spec.output
        assert committed.exists(), f"{spec.output} was never written"
        grouped = series(
            readings, config.metrics.subjects, spec.metric, config.metrics.predecessors
        )
        drawn = replace(spec, output=tmp_path / spec.output.name)
        write_chart(grouped, drawn, config.metrics.colors)
        assert drawn.output.read_text(encoding="UTF-8") == committed.read_text(
            encoding="UTF-8"
        ), f"{spec.output} is stale: run `repomatic sample-metrics --no-forward`"


def test_tracked_subjects_are_reachable():
    """Check every subject this repository tracks can actually be read.

    An undeclared host raises mid-sample, one subject at a time, which is a red
    scheduled run rather than something a reviewer would notice.
    """
    config = repo_config()
    assert config.metrics.subjects, "no subject declared"
    collected = collected_subjects(config.metrics.subjects, config.metrics.predecessors)
    for name in config.metrics.subjects:
        assert name == name.lower(), f"{name} should be lowercase"
    for url in collected.values():
        assert url.startswith("https://")
    # A forerunner belongs to a subject that is actually tracked.
    assert set(config.metrics.predecessors) <= set(config.metrics.subjects)
