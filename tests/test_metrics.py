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

import json
import re
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from repomatic.config import Config, load_repomatic_config
from repomatic.forge import ForgeMetrics
from repomatic.metric_chart import (
    CHART_MODES,
    CHART_SCALES,
    SERIES_PALETTE,
    ChartSpec,
    assign_colors,
    build_chart_data,
    css_class,
    render_chart,
    write_chart,
)
from repomatic.metrics import (
    CHARTABLE_METRICS,
    METRIC_HEADERS,
    METRICS,
    METRICS_BY_ID,
    PREDECESSOR_SUFFIX,
    SOURCE_RANK,
    SOURCES,
    STAR_HISTORY_MAX_PAGES,
    MetricRecord,
    Retention,
    collected_subjects,
    load_metrics,
    reconstruct_from_github,
    sample_subject,
    save_metrics,
    series,
    upsert,
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
    records: dict[tuple[str, str, str], MetricRecord] = {}
    for record in (
        MetricRecord(APRICOT, "stars", "2019-01-01", "0", "created"),
        MetricRecord(APRICOT, "stars", "2021-06-01", "120", "github"),
        MetricRecord(APRICOT, "stars", "2026-08-16", "300", "sample"),
        MetricRecord(APRICOT, "commit", "2026-08-16", "2026-08-15", "sample"),
        MetricRecord(PAPAYA, "stars", "2022-01-01", "0", "created"),
        MetricRecord(PAPAYA, "stars", "2026-08-16", "90", "sample"),
        MetricRecord(OLD_PAPAYA, "stars", "2015-01-01", "0", "created"),
        MetricRecord(OLD_PAPAYA, "stars", "2021-01-01", "40", "wayback"),
        # Past the handover, so the chart must clip it.
        MetricRecord(OLD_PAPAYA, "stars", "2026-08-16", "45", "sample"),
    ):
        records[record.key] = record
    return records


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
        assert metric.description.endswith(".")


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
    """Check the provenance vocabulary and its precedence stay in step.

    An unranked source raises a `KeyError` inside `upsert`, mid-collection, on
    the one code path nobody watches.
    """
    assert set(SOURCES) == set(SOURCE_RANK)
    for description in SOURCES.values():
        assert description.endswith(".")


# ---------------------------------------------------------------------------
# The store.
# ---------------------------------------------------------------------------


def test_record_round_trips_through_a_csv_row():
    """Check a record survives the shape it is committed in."""
    record = MetricRecord(PAPAYA, "stars", "2026-08-16", "42", "sample")
    row = dict(zip(METRIC_HEADERS, record.as_row()))
    assert MetricRecord.from_row(row) == record
    assert record.count == 42


def test_upsert_is_idempotent_within_a_day():
    """Check re-running on the same day overwrites rather than appends."""
    records: dict[tuple[str, str, str], MetricRecord] = {}
    assert upsert(records, MetricRecord(PAPAYA, "stars", "2026-08-16", "42", "sample"))
    assert not upsert(
        records, MetricRecord(PAPAYA, "stars", "2026-08-16", "42", "sample")
    )
    assert upsert(records, MetricRecord(PAPAYA, "stars", "2026-08-16", "43", "sample"))
    assert len(records) == 1


@pytest.mark.parametrize(
    ("stored", "incoming", "wins"),
    (
        # A backfill never degrades a stronger reading already on file.
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
    records: dict[tuple[str, str, str], MetricRecord] = {}
    upsert(records, MetricRecord(PAPAYA, "stars", "2026-08-16", "10", stored))
    upsert(records, MetricRecord(PAPAYA, "stars", "2026-08-16", "99", incoming))
    assert records[(PAPAYA, "stars", "2026-08-16")].value == ("99" if wins else "10")


def test_an_accruing_metric_keeps_every_day():
    """Check a counter's past readings are all retained."""
    records: dict[tuple[str, str, str], MetricRecord] = {}
    for day, value in (("2026-08-01", "10"), ("2026-08-08", "20")):
        upsert(records, MetricRecord(PAPAYA, "stars", day, value, "sample"))
    assert len(records) == 2


def test_an_attribute_keeps_one_row_and_dates_the_change():
    """Check an attribute holds a single row, restamped only when it moves.

    A quiet week must leave the file untouched rather than restamping every
    row, and the surviving date is when the value last *changed*, which is what
    dates a dead project's row honestly.
    """
    records: dict[tuple[str, str, str], MetricRecord] = {}
    assert upsert(
        records, MetricRecord(PAPAYA, "commit", "2026-08-01", "2026-07-30", "sample")
    )
    # Same value a week later: nothing moves, and the old date stands.
    assert not upsert(
        records, MetricRecord(PAPAYA, "commit", "2026-08-08", "2026-07-30", "sample")
    )
    assert list(records) == [(PAPAYA, "commit", "2026-08-01")]
    # A moved value replaces the row and takes the new date.
    assert upsert(
        records, MetricRecord(PAPAYA, "commit", "2026-08-15", "2026-08-14", "sample")
    )
    assert list(records) == [(PAPAYA, "commit", "2026-08-15")]


def test_upsert_rejects_an_unregistered_metric():
    """Check a typo in a metric ID fails loudly rather than storing a stray row."""
    records: dict[tuple[str, str, str], MetricRecord] = {}
    with pytest.raises(KeyError):
        upsert(records, MetricRecord(PAPAYA, "starz", "2026-08-16", "1", "sample"))


def test_save_metrics_writes_a_sorted_csv(tmp_path, history):
    """Check the store is one row per reading, sorted for a readable diff."""
    store = tmp_path / "nested" / "metrics.csv"
    assert save_metrics(store, history) is True
    text = store.read_text(encoding="UTF-8")
    assert text.startswith("repo,metric,date,value,source\n")
    assert text.endswith("\n")
    # One line per record, plus the header: the whole point over JSON.
    assert len(text.splitlines()) == len(history) + 1

    rows = read_csv(store)
    keys = [(row["repo"], row["metric"], row["date"]) for row in rows]
    assert keys == sorted(keys)

    assert save_metrics(store, history) is False


def test_save_metrics_writes_exactly_what_it_is_given(tmp_path):
    """Check the file becomes the caller's records, deletions included.

    A collector re-deriving a whole curve has to be able to retire a reading it
    no longer dates, so the write cannot merge the file back under itself.
    """
    store = tmp_path / "metrics.csv"
    first = MetricRecord(PAPAYA, "stars", "2026-08-01", "10", "sample")
    save_metrics(store, {first.key: first})
    second = MetricRecord(APRICOT, "stars", "2026-08-02", "20", "sample")
    save_metrics(store, {second.key: second})
    assert list(load_metrics(store)) == [second.key]


def test_save_metrics_cannot_resurrect_a_pruned_attribute(tmp_path):
    """Check a round trip through the file leaves an attribute one reading.

    Loading, upserting and writing is what every collector does, and a
    superseded row surviving it would leave two readings of a metric that
    keeps one.
    """
    store = tmp_path / "metrics.csv"
    records = load_metrics(store)
    upsert(
        records, MetricRecord(PAPAYA, "commit", "2026-08-01", "2026-07-30", "sample")
    )
    save_metrics(store, records)

    records = load_metrics(store)
    upsert(
        records, MetricRecord(PAPAYA, "commit", "2026-08-15", "2026-08-14", "sample")
    )
    save_metrics(store, records)

    stored = load_metrics(store)
    assert list(stored) == [(PAPAYA, "commit", "2026-08-15")]


def test_load_metrics_is_loud_on_a_corrupt_store(tmp_path):
    """Check a malformed store raises instead of being silently clobbered."""
    store = tmp_path / "metrics.csv"
    store.write_text("repo,metric\nx,y\n", encoding="UTF-8")
    with pytest.raises(ValueError, match="Malformed metric store"):
        load_metrics(store)


def test_load_metrics_tolerates_a_missing_store(tmp_path):
    """Check a first run reads an empty store rather than failing."""
    assert load_metrics(tmp_path / "absent.csv") == {}


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
    assert (OLD_PAPAYA, "stars", "2026-08-16") in history


def test_series_refuses_a_metric_with_no_history(history):
    """Check charting an attribute is refused, naming what can be plotted."""
    with pytest.raises(ValueError, match="no history to chart"):
        series(history, SUBJECTS, "commit")
    with pytest.raises(ValueError, match="no history to chart"):
        series(history, SUBJECTS, "nonesuch")


def test_series_skips_a_subject_with_no_reading():
    """Check an unsampled subject is absent rather than an empty curve."""
    assert series({}, SUBJECTS) == {}


# ---------------------------------------------------------------------------
# Collectors.
# ---------------------------------------------------------------------------


def test_sample_subject_records_every_metric_and_the_origin(monkeypatch):
    """Check one call yields each metric the forge answered, plus the birth."""
    monkeypatch.setattr(
        "repomatic.metrics.repo_metrics",
        lambda url, extra=None: ForgeMetrics(
            stars=57,
            created="2021-12-09",
            release="2026-08-01",
            release_source="tag",
            commit="2026-08-15",
        ),
    )
    records: dict[tuple[str, str, str], MetricRecord] = {}
    outcome = sample_subject(records, "papaya", PAPAYA, day="2026-08-16")

    assert outcome.stars == 57
    assert outcome.rows == 5
    stored = {(key[1], record.value) for key, record in records.items()}
    assert stored == {
        ("stars", "57"),
        ("stars", "0"),
        ("commit", "2026-08-15"),
        ("release", "2026-08-01"),
        ("release_source", "tag"),
    }
    assert records[(PAPAYA, "stars", "2021-12-09")].source == "created"


def test_sample_subject_adds_no_row_for_what_a_forge_did_not_answer(monkeypatch):
    """Check a project with no release stores no blank release row."""
    monkeypatch.setattr(
        "repomatic.metrics.repo_metrics",
        lambda url, extra=None: ForgeMetrics(stars=3, created="2020-01-01"),
    )
    records: dict[tuple[str, str, str], MetricRecord] = {}
    sample_subject(records, "papaya", PAPAYA, day="2026-08-16")
    assert {key[1] for key in records} == {"stars"}


def test_sample_subject_survives_an_unreadable_forge(monkeypatch):
    """Check one unreachable subject does not cost the others."""
    monkeypatch.setattr("repomatic.metrics.repo_metrics", lambda url, extra=None: None)
    records: dict[tuple[str, str, str], MetricRecord] = {}
    outcome = sample_subject(records, "papaya", PAPAYA, day="2026-08-16")
    assert outcome.note == "unreadable"
    assert not records


def star_week(sunday: str, days: list[int]) -> dict[str, object]:
    """Build one week of the star-history endpoint's payload.

    :param sunday: Calendar date labelling the week, as the endpoint reports it.
    :param days: Stars gained on each of the week's seven days.
    """
    start = datetime.fromisoformat(sunday).replace(tzinfo=timezone.utc)
    return {"week": int(start.timestamp()), "total": sum(days), "days": days}


def test_reconstruct_accumulates_one_row_per_week_that_moved(monkeypatch):
    """Check the daily buckets collapse into a cumulative weekly curve."""
    pages = [
        # Newest first, as the endpoint serves them.
        json.dumps([
            star_week("2022-02-27", [0, 1, 0, 0, 0, 0, 0]),
            star_week("2022-01-30", [0, 0, 0, 0, 0, 0, 0]),
            star_week("2021-12-05", [0, 0, 0, 0, 2, 0, 0]),
        ]),
        json.dumps([]),
    ]
    monkeypatch.setattr("repomatic.metrics.run_gh_command", lambda args: pages.pop(0))
    records: dict[tuple[str, str, str], MetricRecord] = {}
    outcome = reconstruct_from_github(records, "papaya", PAPAYA)

    assert outcome.stars == 3
    # Dated on the last day of the week that gained a star, not the week start.
    assert records[(PAPAYA, "stars", "2021-12-09")].value == "2"
    assert records[(PAPAYA, "stars", "2022-02-28")].value == "3"
    # The quiet week in between earns no row at all.
    assert len(records) == 2
    assert all(record.source == "github" for record in records.values())


def test_reconstruct_dates_a_day_without_converting_the_timezone(monkeypatch):
    """Check a day is the week's label plus its offset, left in GitHub's zone.

    The endpoint buckets days in `America/Los_Angeles`. Its `week` field is
    midnight UTC on the date labelling that local week, so the calendar date of
    that instant already carries the local day: converting it into a zone would
    shift every reading.
    """
    pages = [
        json.dumps([star_week("2024-11-10", [0, 1, 0, 0, 0, 0, 0])]),
        json.dumps([]),
    ]
    monkeypatch.setattr("repomatic.metrics.run_gh_command", lambda args: pages.pop(0))
    records: dict[tuple[str, str, str], MetricRecord] = {}
    reconstruct_from_github(records, "papaya", PAPAYA)
    assert (PAPAYA, "stars", "2024-11-11") in records


def test_reconstruct_replaces_the_rows_it_wrote_before(monkeypatch):
    """Check a re-derived curve drops the days its previous run had claimed.

    The walk rebuilds the whole curve, so a day that no longer carries a
    reading must disappear rather than linger stating a total the rest of the
    curve contradicts. Rows from every other source are left alone.
    """
    stale = MetricRecord(PAPAYA, "stars", "2019-06-01", "99", "github")
    kept = MetricRecord(PAPAYA, "stars", "2019-06-02", "98", "wayback")
    elsewhere = MetricRecord(APRICOT, "stars", "2019-06-01", "97", "github")
    records = {r.key: r for r in (stale, kept, elsewhere)}

    pages = [
        json.dumps([star_week("2021-12-05", [0, 0, 0, 0, 2, 0, 0])]),
        json.dumps([]),
    ]
    monkeypatch.setattr("repomatic.metrics.run_gh_command", lambda args: pages.pop(0))
    reconstruct_from_github(records, "papaya", PAPAYA)

    assert stale.key not in records
    assert records[kept.key] == kept
    assert records[elsewhere.key] == elsewhere
    assert records[(PAPAYA, "stars", "2021-12-09")].value == "2"


def test_reconstruct_skips_a_subject_off_github():
    """Check a GitLab subject is skipped with a reason, not failed.

    The star history is a GitHub endpoint; every other forge simply has nothing
    to reconstruct from.
    """
    records: dict[tuple[str, str, str], MetricRecord] = {}
    outcome = reconstruct_from_github(
        records, "papaya", "https://gitlab.com/fruits/papaya"
    )
    assert "gitlab.com" in outcome.note
    assert not records


def test_reconstruct_reports_a_repository_that_is_gone(monkeypatch):
    """Check a 404 on the first page reads as a skip, not a failure."""

    def refuse(args):
        msg = "gh: Not Found (HTTP 404)"
        raise RuntimeError(msg)

    monkeypatch.setattr("repomatic.metrics.run_gh_command", refuse)
    records: dict[tuple[str, str, str], MetricRecord] = {}
    outcome = reconstruct_from_github(records, "papaya", PAPAYA)
    assert outcome.note == "no such repository"
    assert not records


def test_reconstruct_reports_a_repository_with_no_star(monkeypatch):
    """Check an unstarred repository is a note, not an empty curve."""
    monkeypatch.setattr("repomatic.metrics.run_gh_command", lambda args: json.dumps([]))
    records: dict[tuple[str, str, str], MetricRecord] = {}
    outcome = reconstruct_from_github(records, "papaya", PAPAYA)
    assert outcome.note == "no star on record"
    assert not records


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
            return json.dumps([star_week("2026-08-30", [1, 0, 0, 0, 0, 0, 0])])
        msg = "gh: Bad gateway (HTTP 502)"
        raise RuntimeError(msg)

    monkeypatch.setattr("repomatic.metrics.run_gh_command", flaky)
    records: dict[tuple[str, str, str], MetricRecord] = {}
    outcome = reconstruct_from_github(records, "papaya", PAPAYA)
    assert "abandoned on page 2" in outcome.note
    assert not records


def test_reconstruct_gives_up_on_an_unparsable_page(monkeypatch):
    """Check a payload that is not JSON abandons the subject."""
    monkeypatch.setattr("repomatic.metrics.run_gh_command", lambda args: "<html>")
    records: dict[tuple[str, str, str], MetricRecord] = {}
    outcome = reconstruct_from_github(records, "papaya", PAPAYA)
    assert outcome.note == "unparsable page 1"
    assert not records


def test_reconstruct_walk_is_bounded(monkeypatch):
    """Check a server that never runs out of pages cannot loop forever.

    GitHub refuses past {data}`STAR_HISTORY_MAX_PAGES`, so the walk stops
    there on its own rather than trusting the endpoint to end the sequence.
    """
    calls = {"count": 0}

    def endless(args):
        calls["count"] += 1
        return json.dumps([star_week("2021-12-05", [1, 0, 0, 0, 0, 0, 0])])

    monkeypatch.setattr("repomatic.metrics.run_gh_command", endless)
    records: dict[tuple[str, str, str], MetricRecord] = {}
    reconstruct_from_github(records, "papaya", PAPAYA)
    assert calls["count"] == STAR_HISTORY_MAX_PAGES


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
    with pytest.raises(ValueError, match="chart.svg has no stars history to plot"):
        build_chart_data({}, spec)


def test_chart_spec_rejects_an_unknown_mode():
    """Check a typo in `mode` raises instead of silently drawing a calendar."""
    with pytest.raises(ValueError, match="Unsupported chart mode"):
        ChartSpec(output=Path("chart.svg"), mode="sideways")
    assert set(CHART_MODES) == {"absolute", "relative"}
    # The two axes are separate settings, so the vertical one is not reachable
    # by naming it here: a `mode` that silently fell back would draw a chart
    # misreading every series on it by orders of magnitude.
    with pytest.raises(ValueError, match="Unsupported chart mode"):
        ChartSpec(output=Path("chart.svg"), mode="logarithmic")


def test_chart_spec_rejects_an_unknown_scale():
    """Check a typo in `scale` raises instead of silently drawing a linear axis."""
    with pytest.raises(ValueError, match="Unsupported chart scale"):
        ChartSpec(output=Path("chart.svg"), scale="log10")
    assert set(CHART_SCALES) == {"linear", "logarithmic"}


def test_chart_spec_axes_are_independent():
    """Check a comparison chart can slide the origins and compress the counts."""
    spec = ChartSpec(output=Path("chart.svg"), mode="relative", scale="logarithmic")
    assert spec.relative
    assert spec.logarithmic
    assert not ChartSpec(output=Path("chart.svg")).logarithmic


def test_chart_spec_rejects_a_metric_with_no_history():
    """Check a chart pointed at an attribute is refused at configuration time."""
    with pytest.raises(ValueError, match="no history of"):
        ChartSpec.from_mapping({"output": "c.svg", "metric": "commit"})


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
        mode="relative",
        only=("papaya",),
        scale="logarithmic",
        title="Papaya stars",
    )
    assert ChartSpec.from_mapping({"output": "c.svg"}).scale == "linear"
    assert ChartSpec.from_mapping({"output": "c.svg", "only": "papaya"}).only == (
        "papaya",
    )
    assert ChartSpec.from_mapping({"output": "c.svg"}).mode == "absolute"
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
    svg = render_chart(data, stamp="2026-08-16")

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
    svg = render_chart(build_chart_data(grouped, spec), stamp="2026-08-16")
    assert 'class="lbl s-apricot"' in svg
    assert "s-papaya" not in svg


def test_render_chart_relative_swaps_the_calendar_for_project_age(history):
    """Check the by-age axis carries no calendar year among its ticks."""
    grouped = series(history, SUBJECTS, "stars", PREDECESSORS)
    spec = ChartSpec(output=Path("chart.svg"), mode="relative")
    svg = render_chart(
        build_chart_data(grouped, spec), relative=True, stamp="2026-08-16"
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
    svg = render_chart(data, logarithmic=True, stamp="2026-08-16")
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
    linear = _final_y(render_chart(data, stamp="2026-08-16"), "apricot")
    logarithmic = _final_y(
        render_chart(data, logarithmic=True, stamp="2026-08-16"), "apricot"
    )
    # The plot floor is 414 and its ceiling 28, so a smaller y sits higher.
    assert linear > 400, "a linear axis should pin the small series to the floor"
    assert logarithmic < 300, "a logarithmic axis should lift it clear"
    # The larger series still tops out at the ceiling, so the gap is compressed
    # rather than the whole chart being slid upwards.
    peak = _final_y(render_chart(data, logarithmic=True, stamp="2026-08-16"), "papaya")
    assert peak == pytest.approx(28, abs=1)


def test_render_chart_logarithmic_keeps_a_zero_on_the_floor():
    """Check the created origin every series carries is drawn, not dropped.

    A count of zero has no logarithm, so it is placed on the floor the band at
    the bottom of the plot reserves. Dropping it instead would start each curve
    at its first star, which is a different and unstated claim.
    """
    data = build_chart_data(LOPSIDED, ChartSpec(output=Path("chart.svg")))
    svg = render_chart(data, logarithmic=True, stamp="2026-08-16")
    first = re.search(r'<polyline class="s-apricot" points="([^"]+)"', svg)
    assert first
    assert float(first.group(1).split()[0].split(",")[1]) == pytest.approx(414, abs=1)


def test_render_chart_escapes_a_series_name():
    """Check a name carrying markup cannot break out of the SVG text node."""
    grouped = {"a & b": [(date(2020, 1, 1), 1), (date(2021, 1, 1), 5)]}
    data = build_chart_data(grouped, ChartSpec(output=Path("chart.svg")))
    svg = render_chart(data, stamp="2026-08-16")
    assert "a &amp; b" in svg
    assert ">a & b" not in svg


@pytest.mark.parametrize("peak", (0, 1))
@pytest.mark.parametrize("logarithmic", (False, True))
def test_render_chart_survives_a_flat_or_single_star_history(peak, logarithmic):
    """Check a peak of 0 or 1 renders instead of dividing by zero.

    A brand-new repository's history is exactly this: the zero-star creation
    row alone, or that row plus its first star. Halving the axis step used to
    floor it to 0, and the ceiling division then crashed on both scales.
    """
    grouped = {"papaya": [(date(2026, 1, 1), 0), (date(2026, 2, 1), peak)]}
    data = build_chart_data(grouped, ChartSpec(output=Path("chart.svg")))
    svg = render_chart(data, logarithmic=logarithmic, stamp="2026-08-16")
    assert '<polyline class="s-papaya"' in svg


def test_write_chart_is_convergent(tmp_path, history):
    """Check redrawing an unmoved history rewrites nothing."""
    grouped = series(history, SUBJECTS, "stars", PREDECESSORS)
    spec = ChartSpec(output=tmp_path / "nested" / "chart.svg")
    assert write_chart(grouped, spec, stamp="2026-08-16") is True
    assert write_chart(grouped, spec, stamp="2026-08-16") is False


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
    today = datetime.now(tz=timezone.utc).date()
    seen: set[tuple[str, str, str]] = set()
    attributes: Counter[tuple[str, str]] = Counter()

    for row in rows:
        assert set(row) == set(METRIC_HEADERS)
        record = MetricRecord.from_row(row)
        assert record.repo in tracked, f"{record.repo} left the configuration"
        assert record.metric in METRICS_BY_ID
        assert record.source in SOURCES
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


def test_committed_charts_are_redrawable():
    """Check every configured chart still renders from the committed store."""
    if not STORE.exists():
        pytest.skip("no reading recorded yet")
    config = repo_config()
    records = load_metrics(STORE)
    assert config.metrics.charts, "no chart declared to draw"
    for entry in config.metrics.charts:
        spec = ChartSpec.from_mapping(entry)
        grouped = series(
            records, config.metrics.subjects, spec.metric, config.metrics.predecessors
        )
        data = build_chart_data(grouped, spec, config.metrics.colors)
        svg = render_chart(data, relative=spec.relative, title=spec.title)
        assert svg.startswith("<svg ")
        assert (REPO_ROOT / spec.output).exists(), f"{spec.output} was never written"


def test_tracked_subjects_are_reachable():
    """Check every subject this repository tracks can actually be read.

    An undeclared host raises mid-sample, one subject at a time, which is a red
    scheduled run rather than something a reviewer would notice.
    """
    config = repo_config()
    assert config.metrics.subjects, "no subject declared"
    assert not set(config.metrics.subjects) & set(config.metrics.skip), (
        "a subject cannot be both tracked and excused"
    )
    collected = collected_subjects(config.metrics.subjects, config.metrics.predecessors)
    for name in config.metrics.subjects:
        assert name == name.lower(), f"{name} should be lowercase"
    for url in collected.values():
        assert url.startswith("https://")
    # A forerunner belongs to a subject that is actually tracked.
    assert set(config.metrics.predecessors) <= set(config.metrics.subjects)
    # Every excusal reads as a sentence, since it is the only record of why a
    # subject shows nothing.
    for reason in config.metrics.skip.values():
        assert reason.endswith("."), f"{reason!r} should read as a sentence"
