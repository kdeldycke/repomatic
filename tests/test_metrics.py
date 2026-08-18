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

import gzip
import json
import re
import urllib.error
import zlib
from collections import Counter
from datetime import date, datetime, timezone
from email.message import Message
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
    GITHUB_EPOCH,
    METRIC_HEADERS,
    METRICS,
    METRICS_BY_ID,
    PREDECESSOR_SUFFIX,
    SOURCE_RANK,
    SOURCES,
    WAYBACK_STAR_PATTERNS,
    MetricRecord,
    Retention,
    backfill_wayback,
    collected_subjects,
    fetch,
    gunzip,
    import_star_history_csv,
    last_fetch_failure,
    load_metrics,
    parse_csv_day,
    read_star_counter,
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

APRICOT = "https://github.com/fruits/apricot"
PAPAYA = "https://github.com/fruits/papaya"
OLD_PAPAYA = "https://github.com/old-fruits/papaya"

SUBJECTS = {"apricot": "fruits/apricot", "papaya": "fruits/papaya"}
PREDECESSORS = {"papaya": "old-fruits/papaya"}

WAYBACK_MARKUPS = (
    '<span id="repo-stars-counter-star" title="4,412">4.4k</span>',
    '<span title="4,412" id="repo-stars-counter-star">4.4k</span>',
    '<a aria-label="4412 users starred this repository" href="/x/y/stargazers">',
    '<a href="/x/y/stargazers" class="social-count js-social-count">4,412</a>',
    '<a class="social-count" href="/x/y/stargazers">4,412</a>',
)
"""One archived markup per pattern, newest layout first.

Paired positionally with {data}`~repomatic.metrics.WAYBACK_STAR_PATTERNS`, so a
pattern dropped or reordered without its sample fails rather than silently
losing a decade of captures.
"""


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


@pytest.fixture(autouse=True)
def _reset_refusal_streak(monkeypatch):
    """Reset the wayback refusal streak, run-scoped state held across calls."""
    monkeypatch.setattr("repomatic.metrics._WAYBACK_REFUSAL_STREAK", 0)


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


def test_save_metrics_merges_what_is_already_on_disk(tmp_path):
    """Check a flush cannot drop rows another writer recorded meanwhile.

    A slow backfill flushes across a run lasting hours, holding a snapshot that
    goes stale the moment anything else records a reading.
    """
    store = tmp_path / "metrics.csv"
    first = MetricRecord(PAPAYA, "stars", "2026-08-01", "10", "sample")
    save_metrics(store, {first.key: first})
    second = MetricRecord(APRICOT, "stars", "2026-08-02", "20", "sample")
    save_metrics(store, {second.key: second})
    assert len(load_metrics(store)) == 2


def test_save_metrics_cannot_resurrect_a_pruned_attribute(tmp_path):
    """Check the disk merge does not undo an attribute's retention.

    The merge is additive, so a superseded row coming back from disk would
    leave two readings of a metric that keeps one.
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


def test_reconstruct_accumulates_one_row_per_day_that_moved(monkeypatch):
    """Check per-star timestamps collapse into a cumulative daily curve."""
    pages = [
        json.dumps([
            {"starred_at": "2021-12-09T10:00:00Z"},
            {"starred_at": "2021-12-09T11:00:00Z"},
            {"starred_at": "2022-03-01T09:00:00Z"},
        ]),
        json.dumps([]),
    ]
    monkeypatch.setattr("repomatic.metrics.run_gh_command", lambda args: pages.pop(0))
    records: dict[tuple[str, str, str], MetricRecord] = {}
    outcome = reconstruct_from_github(records, "papaya", PAPAYA)

    assert outcome.stars == 3
    assert records[(PAPAYA, "stars", "2021-12-09")].value == "2"
    assert records[(PAPAYA, "stars", "2022-03-01")].value == "3"
    assert all(record.source == "github" for record in records.values())


def test_reconstruct_skips_a_subject_off_github():
    """Check a GitLab subject is skipped with a reason, not failed.

    Per-star timestamps are a GitHub endpoint; every other forge simply has
    nothing to reconstruct from.
    """
    records: dict[tuple[str, str, str], MetricRecord] = {}
    outcome = reconstruct_from_github(
        records, "papaya", "https://gitlab.com/fruits/papaya"
    )
    assert "gitlab.com" in outcome.note
    assert not records


def test_reconstruct_reports_a_repository_the_token_cannot_administer(monkeypatch):
    """Check the restricted endpoint's 404 reads as a skip, not a failure."""

    def refuse(args):
        msg = "gh: Not Found (HTTP 404)"
        raise RuntimeError(msg)

    monkeypatch.setattr("repomatic.metrics.run_gh_command", refuse)
    records: dict[tuple[str, str, str], MetricRecord] = {}
    outcome = reconstruct_from_github(records, "papaya", PAPAYA)
    assert outcome.note == "not an admin, not readable"
    assert not records


def test_reconstruct_writes_nothing_when_pagination_breaks_midway(monkeypatch):
    """Check a failure mid-walk abandons the subject rather than truncating.

    A partial cumulative curve written over a correct one would look exactly as
    legitimate as the rest of the history, so the walk is all-or-nothing.
    """
    calls = {"count": 0}

    def flaky(args):
        calls["count"] += 1
        if calls["count"] == 1:
            return json.dumps([{"starred_at": "2021-12-09T10:00:00Z"}] * 100)
        msg = "gh: Bad gateway (HTTP 502)"
        raise RuntimeError(msg)

    monkeypatch.setattr("repomatic.metrics.run_gh_command", flaky)
    records: dict[tuple[str, str, str], MetricRecord] = {}
    outcome = reconstruct_from_github(records, "papaya", PAPAYA)
    assert "abandoned on page 2" in outcome.note
    assert not records


def test_backfill_wayback_skips_an_exactly_reconstructed_subject():
    """Check the archives are spent only where no better source exists."""
    record = MetricRecord(PAPAYA, "stars", "2021-01-01", "5", "github")
    outcome = backfill_wayback({record.key: record}, "papaya", PAPAYA)
    assert outcome.note == "already reconstructed exactly"
    assert outcome.rows == 0


def test_backfill_wayback_skips_a_subject_off_github():
    """Check only `github.com` pages are mined, since only they are parsed."""
    outcome = backfill_wayback({}, "papaya", "https://gitlab.com/fruits/papaya")
    assert "gitlab.com" in outcome.note


def test_backfill_wayback_narrates_its_progress(monkeypatch):
    """Check a watcher hears each page reached, and only the points that landed."""
    monkeypatch.setattr("repomatic.metrics.time.sleep", lambda seconds: None)
    monkeypatch.setattr(
        "repomatic.metrics.wayback_captures",
        lambda path: ["20220101000000", "20230101000000"],
    )
    # The first capture serves its counter and the second is refused, which is
    # the ratio the archive actually answers with: a refusal must still move the
    # status along, and must not reach `on_row`.
    pages = iter([WAYBACK_MARKUPS[0].encode(), None])
    monkeypatch.setattr("repomatic.metrics.fetch", lambda url, **kwargs: next(pages))

    statuses: list[str] = []
    rows: list[str] = []
    outcome = backfill_wayback(
        {}, "papaya", PAPAYA, on_status=statuses.append, on_row=rows.append
    )

    assert outcome.rows == 1
    assert rows == ["fruits/papaya 2022-01-01: 4,412 stars"]
    assert "capture index" in statuses[0]
    assert "(1/2, 0 recovered)" in statuses[1]
    assert "(2/2, 1 recovered)" in statuses[2]


def test_backfill_wayback_runs_unwatched(monkeypatch):
    """Check the callbacks stay optional, since only an interactive run draws."""
    monkeypatch.setattr("repomatic.metrics.time.sleep", lambda seconds: None)
    monkeypatch.setattr(
        "repomatic.metrics.wayback_captures", lambda path: ["20220101000000"]
    )
    monkeypatch.setattr(
        "repomatic.metrics.fetch", lambda url, **kwargs: WAYBACK_MARKUPS[0].encode()
    )
    assert backfill_wayback({}, "papaya", PAPAYA).rows == 1


def test_backfill_wayback_abandons_on_a_refusal_streak(monkeypatch):
    """Check a spent budget stops the run instead of paying every retry left."""
    monkeypatch.setattr("repomatic.metrics.time.sleep", lambda seconds: None)
    monkeypatch.setattr("repomatic.metrics.WAYBACK_REFUSAL_LIMIT", 2)
    monkeypatch.setattr(
        "repomatic.metrics.wayback_captures",
        lambda path: [f"2022010{index}000000" for index in range(1, 6)],
    )
    calls: list[str] = []

    def refused(url, **kwargs):
        calls.append(url)

    monkeypatch.setattr("repomatic.metrics.fetch", refused)
    outcome = backfill_wayback({}, "papaya", PAPAYA)
    assert outcome.rows == 0
    assert "refused in a row" in outcome.note
    assert "retry later" in outcome.note
    # The run stopped at the limit, sparing the remaining captures' retries.
    assert len(calls) == 2


def test_backfill_wayback_streak_resets_on_a_served_page(monkeypatch):
    """Check a served page proves the archive healthy and clears the streak."""
    monkeypatch.setattr("repomatic.metrics.time.sleep", lambda seconds: None)
    monkeypatch.setattr("repomatic.metrics.WAYBACK_REFUSAL_LIMIT", 2)
    monkeypatch.setattr(
        "repomatic.metrics.wayback_captures",
        lambda path: [f"2022010{index}000000" for index in range(1, 5)],
    )
    # Refused, served, refused, served: the streak never reaches the limit.
    pages = iter([
        None,
        WAYBACK_MARKUPS[0].encode(),
        None,
        WAYBACK_MARKUPS[0].encode(),
    ])
    monkeypatch.setattr("repomatic.metrics.fetch", lambda url, **kwargs: next(pages))
    outcome = backfill_wayback({}, "papaya", PAPAYA)
    assert outcome.rows == 2
    assert outcome.note == "4 captures"


def test_backfill_wayback_skips_subjects_once_the_budget_is_spent(monkeypatch):
    """Check the streak outlives its subject: the budget is per IP, not repo."""
    monkeypatch.setattr("repomatic.metrics._WAYBACK_REFUSAL_STREAK", 10)

    def unspent_index(path):
        raise AssertionError("the capture index must not be read")

    monkeypatch.setattr("repomatic.metrics.wayback_captures", unspent_index)
    outcome = backfill_wayback({}, "papaya", PAPAYA)
    assert outcome.rows == 0
    assert "skipped" in outcome.note


def test_backfill_wayback_reports_a_recovered_point_once(monkeypatch):
    """Check a watcher's line replaces the log's, not joins it."""
    monkeypatch.setattr("repomatic.metrics.time.sleep", lambda seconds: None)
    monkeypatch.setattr(
        "repomatic.metrics.wayback_captures", lambda path: ["20220101000000"]
    )
    monkeypatch.setattr(
        "repomatic.metrics.fetch", lambda url, **kwargs: WAYBACK_MARKUPS[0].encode()
    )
    logged: list[str] = []
    monkeypatch.setattr("repomatic.metrics.logging.info", logged.append)
    rows: list[str] = []
    backfill_wayback({}, "papaya", PAPAYA, on_row=rows.append)
    assert rows == ["fruits/papaya 2022-01-01: 4,412 stars"]
    assert logged == []


def test_fetch_names_why_it_gave_up(monkeypatch):
    """Check an exhausted retry budget reports what happened, not just failure."""
    monkeypatch.setattr("repomatic.metrics.time.sleep", lambda seconds: None)

    def always_503(request, timeout=0):
        raise urllib.error.HTTPError(request.full_url, 503, "nope", Message(), None)

    monkeypatch.setattr("repomatic.metrics.urllib.request.urlopen", always_503)
    assert fetch("https://web.archive.org/whatever", tries=3) is None
    assert last_fetch_failure() == "3x HTTP 503"


def test_gunzip_salvages_a_truncated_stream():
    """Check a body cut mid-stream keeps everything that did arrive."""
    payload = ("<html>" + "papaya " * 5000 + "</html>").encode()
    truncated = gzip.compress(payload)[:-40]
    with pytest.raises(EOFError):
        gzip.decompress(truncated)
    salvaged = gunzip(truncated)
    assert salvaged.startswith(b"<html>")
    assert len(salvaged) > len(payload) // 2


def test_gunzip_returns_nothing_for_an_undecodable_blob():
    """Check a body that is not gzip at all yields empty rather than raising."""
    assert gunzip(b"not gzip at all") == b""
    with pytest.raises(zlib.error):
        zlib.decompressobj(zlib.MAX_WBITS | 16).decompress(b"not gzip at all")


@pytest.mark.parametrize(
    ("markup", "pattern"), tuple(zip(WAYBACK_MARKUPS, WAYBACK_STAR_PATTERNS))
)
def test_each_wayback_pattern_matches_its_layout(markup, pattern):
    """Check every counter markup GitHub shipped is still recognized."""
    assert pattern.search(markup)
    assert read_star_counter(markup) == 4412


def test_read_star_counter_finds_nothing_in_an_unrelated_page():
    """Check a page carrying no counter reports its absence."""
    assert read_star_counter("<html><body>no counter here</body></html>") is None


# ---------------------------------------------------------------------------
# The star-history.com import.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stamp", "expected"),
    (
        # A late-evening reading in a positive zone falls on the previous UTC day.
        ("Tue Dec 07 2021 20:50:22 GMT+0400 (Gulf Standard Time)", date(2021, 12, 7)),
        ("Wed Jan 01 2020 01:30:00 GMT+0400 (Gulf Standard Time)", date(2019, 12, 31)),
        ("Sun Jun 30 2024 22:00:00 GMT-0500 (Central Daylight Time)", date(2024, 7, 1)),
        ("Thu Jan 01 1970 04:00:00 GMT+0400 (Gulf Standard Time)", date(1970, 1, 1)),
    ),
)
def test_parse_csv_day(stamp, expected):
    """Check a JavaScript date stamp yields the UTC calendar day."""
    assert parse_csv_day(stamp) == expected


@pytest.mark.parametrize("stamp", ("", "2021-12-07", "not a date at all"))
def test_parse_csv_day_refuses_what_it_cannot_read(stamp):
    """Check an unparsable stamp yields nothing rather than a wrong day."""
    assert parse_csv_day(stamp) is None


def test_import_star_history_csv(tmp_path):
    """Check a calendar export lands in the store under its own provenance."""
    export = tmp_path / "star-history.csv"
    export.write_text(
        "Repository,Date,Stars\n"
        "fruits/papaya,Tue Dec 07 2021 20:50:22 GMT+0400 (Gulf Standard Time),1\n"
        "fruits/papaya,Tue May 31 2022 09:50:45 GMT+0400 (Gulf Standard Time),7\n"
        "fruits/apricot,Thu Aug 13 2026 11:41:30 GMT+0400 (Gulf Standard Time),120\n",
        encoding="UTF-8",
    )
    records: dict[tuple[str, str, str], MetricRecord] = {}
    outcomes = import_star_history_csv(records, export)
    assert {outcome.subject for outcome in outcomes} == {APRICOT, PAPAYA}
    assert len(records) == 3
    assert all(record.source == "star-history" for record in records.values())
    assert records[(PAPAYA, "stars", "2021-12-07")].value == "1"


def test_import_star_history_csv_honors_the_wanted_repositories(tmp_path):
    """Check an export covering more repositories than are tracked is filtered."""
    export = tmp_path / "star-history.csv"
    export.write_text(
        "Repository,Date,Stars\n"
        "fruits/papaya,Tue Dec 07 2021 20:50:22 GMT+0400 (Gulf Standard Time),1\n"
        "veggies/carrot,Tue Dec 07 2021 20:50:22 GMT+0400 (Gulf Standard Time),9\n",
        encoding="UTF-8",
    )
    records: dict[tuple[str, str, str], MetricRecord] = {}
    import_star_history_csv(records, export, [PAPAYA])
    assert {key[0] for key in records} == {PAPAYA}


def test_import_star_history_csv_refuses_the_by_age_export(tmp_path):
    """Check the epoch-zero variant is refused rather than imported."""
    export = tmp_path / "star-history.csv"
    export.write_text(
        "Repository,Date,Stars\n"
        "fruits/papaya,Thu Jan 01 1970 04:00:00 GMT+0400 (Gulf Standard Time),1\n"
        "fruits/papaya,Wed Jun 24 1970 17:00:23 GMT+0400 (Gulf Standard Time),7\n",
        encoding="UTF-8",
    )
    records: dict[tuple[str, str, str], MetricRecord] = {}
    with pytest.raises(ValueError, match="by-age export"):
        import_star_history_csv(records, export)
    assert not records
    assert GITHUB_EPOCH.year == 2008


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
