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

"""Tests for the star-history store, its collectors and its chart renderer."""

from __future__ import annotations

import gzip
import json
import re
import urllib.error
import zlib
from datetime import date, datetime, timezone
from email.message import Message
from pathlib import Path

import pytest

from repomatic.config import Config, load_repomatic_config
from repomatic.pyproject import read_pyproject_toml
from repomatic.star_chart import (
    CHART_MODES,
    SERIES_PALETTE,
    ChartSpec,
    assign_colors,
    build_chart_data,
    css_class,
    render_chart,
    write_chart,
)
from repomatic.stars import (
    GITHUB_EPOCH,
    PREDECESSOR_SUFFIX,
    SOURCE_RANK,
    SOURCES,
    WAYBACK_STAR_PATTERNS,
    StarRecord,
    backfill_wayback,
    collected_repos,
    fetch,
    gunzip,
    import_star_history_csv,
    last_fetch_failure,
    load_star_records,
    parse_csv_day,
    read_star_counter,
    reconstruct_from_github,
    sample_current,
    save_star_records,
    series,
    upsert,
)

REPO_ROOT = Path(__file__).parent.parent

STAR_STORE = REPO_ROOT / "docs" / "assets" / "star-history.json"
"""This repository's own history, accrued by the scheduled sampler."""

WAYBACK_MARKUPS = (
    '<span id="repo-stars-counter-star" title="4,412">4.4k</span>',
    '<span title="4,412" id="repo-stars-counter-star">4.4k</span>',
    '<a aria-label="4412 users starred this repository" href="/x/y/stargazers">',
    '<a href="/x/y/stargazers" class="social-count js-social-count">4,412</a>',
    '<a class="social-count" href="/x/y/stargazers">4,412</a>',
)
"""One archived markup per pattern, newest layout first.

Paired positionally with {data}`~repomatic.stars.WAYBACK_STAR_PATTERNS`, so a
pattern dropped or reordered without its sample fails rather than silently
losing a decade of captures.
"""


def repo_config() -> Config:
    """Load this repository's own `[tool.repomatic]` section."""
    return load_repomatic_config(read_pyproject_toml(REPO_ROOT))


@pytest.fixture
def sample_history():
    """Two series, one of them carrying a retired forerunner."""
    records: dict[tuple[str, str], StarRecord] = {}
    for record in (
        StarRecord("fruits/apricot", "2019-01-01", 0, "created"),
        StarRecord("fruits/apricot", "2021-06-01", 120, "github"),
        StarRecord("fruits/apricot", "2026-08-16", 300, "sample"),
        StarRecord("fruits/papaya", "2022-01-01", 0, "created"),
        StarRecord("fruits/papaya", "2026-08-16", 90, "sample"),
        StarRecord("old-fruits/papaya", "2015-01-01", 0, "created"),
        StarRecord("old-fruits/papaya", "2021-01-01", 40, "wayback"),
        # Past the handover, so the chart must clip it.
        StarRecord("old-fruits/papaya", "2026-08-16", 45, "sample"),
    ):
        records[record.key] = record
    return records


SERIES_MAP = {"apricot": "fruits/apricot", "papaya": "fruits/papaya"}
PREDECESSORS = {"papaya": "old-fruits/papaya"}


def test_every_source_is_ranked():
    """Check the provenance vocabulary and its precedence stay in step.

    An unranked source raises a `KeyError` inside `upsert`, mid-collection, on
    the one code path nobody watches.
    """
    assert set(SOURCES) == set(SOURCE_RANK)
    for description in SOURCES.values():
        assert description.endswith(".")


def test_star_record_round_trips_through_json():
    """Check a record survives the shape it is committed in."""
    record = StarRecord("fruits/papaya", "2026-08-16", 42, "sample")
    assert StarRecord.from_dict(record.to_dict()) == record
    assert set(record.to_dict()) == {"date", "repo", "source", "stars"}


def test_upsert_is_idempotent_within_a_day():
    """Check re-running on the same day overwrites rather than appends."""
    records: dict[tuple[str, str], StarRecord] = {}
    assert upsert(records, StarRecord("fruits/papaya", "2026-08-16", 42, "sample"))
    assert not upsert(records, StarRecord("fruits/papaya", "2026-08-16", 42, "sample"))
    assert upsert(records, StarRecord("fruits/papaya", "2026-08-16", 43, "sample"))
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
    records: dict[tuple[str, str], StarRecord] = {}
    upsert(records, StarRecord("fruits/papaya", "2026-08-16", 10, stored))
    upsert(records, StarRecord("fruits/papaya", "2026-08-16", 99, incoming))
    assert records[("fruits/papaya", "2026-08-16")].stars == (99 if wins else 10)


def test_save_star_records_is_biome_shaped(tmp_path, sample_history):
    """Check the history serializes the way `format-json` would leave it."""
    store = tmp_path / "nested" / "star-history.json"
    assert save_star_records(store, sample_history) is True
    text = store.read_text(encoding="UTF-8")
    assert "\t" in text
    assert "    " not in text
    assert text.endswith("\n")
    # Sorted by repository then date, so a scheduled commit reads as an append.
    keys = [(entry["repo"], entry["date"]) for entry in json.loads(text)]
    assert keys == sorted(keys)

    assert save_star_records(store, sample_history) is False


def test_save_star_records_merges_what_is_already_on_disk(tmp_path):
    """Check a flush cannot drop points another writer recorded meanwhile.

    A slow backfill flushes across a run lasting hours, holding a snapshot that
    goes stale the moment anything else records a sample.
    """
    store = tmp_path / "star-history.json"
    save_star_records(
        store,
        {
            ("fruits/papaya", "2026-08-01"): StarRecord(
                "fruits/papaya", "2026-08-01", 10, "sample"
            )
        },
    )
    stale = {
        ("fruits/apricot", "2026-08-02"): StarRecord(
            "fruits/apricot", "2026-08-02", 20, "sample"
        )
    }
    save_star_records(store, stale)
    assert len(load_star_records(store)) == 2


def test_load_star_records_is_loud_on_a_corrupt_file(tmp_path):
    """Check a malformed history raises instead of being silently clobbered."""
    store = tmp_path / "star-history.json"
    store.write_text('[{"repo": "a/b"}]', encoding="UTF-8")
    with pytest.raises(ValueError, match="Malformed star history"):
        load_star_records(store)


def test_collected_repos_marks_a_forerunner():
    """Check a predecessor is addressable without a second lookup."""
    assert collected_repos(SERIES_MAP, PREDECESSORS) == {
        "apricot": "fruits/apricot",
        "papaya": "fruits/papaya",
        "papaya" + PREDECESSOR_SUFFIX: "old-fruits/papaya",
    }


def test_series_clips_a_forerunner_at_the_handover(sample_history):
    """Check a forerunner's line stops where its successor's begins.

    The archived repository keeps collecting the odd star, and drawing that
    tail would run it the whole width of the chart beside its successor,
    reading as two live projects rather than one handover.
    """
    grouped = series(sample_history, SERIES_MAP, PREDECESSORS)
    prior = grouped["papaya" + PREDECESSOR_SUFFIX]
    handover = grouped["papaya"][0][0]
    assert prior
    assert max(day for day, _stars in prior) <= handover
    # The store keeps the discarded point: only the chart drops it.
    assert ("old-fruits/papaya", "2026-08-16") in sample_history


def test_series_skips_a_repository_with_no_point():
    """Check an unsampled series is absent rather than an empty curve."""
    assert series({}, SERIES_MAP) == {}


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
    records: dict[tuple[str, str], StarRecord] = {}
    outcomes = import_star_history_csv(records, export)
    assert {outcome.name for outcome in outcomes} == {"fruits/apricot", "fruits/papaya"}
    assert len(records) == 3
    assert all(record.source == "star-history" for record in records.values())
    assert records[("fruits/papaya", "2021-12-07")].stars == 1


def test_import_star_history_csv_honors_the_wanted_slugs(tmp_path):
    """Check an export covering more repositories than are tracked is filtered."""
    export = tmp_path / "star-history.csv"
    export.write_text(
        "Repository,Date,Stars\n"
        "fruits/papaya,Tue Dec 07 2021 20:50:22 GMT+0400 (Gulf Standard Time),1\n"
        "veggies/carrot,Tue Dec 07 2021 20:50:22 GMT+0400 (Gulf Standard Time),9\n",
        encoding="UTF-8",
    )
    records: dict[tuple[str, str], StarRecord] = {}
    import_star_history_csv(records, export, ["fruits/papaya"])
    assert {repo for repo, _day in records} == {"fruits/papaya"}


def test_import_star_history_csv_refuses_the_by_age_export(tmp_path):
    """Check the epoch-zero variant is refused rather than imported.

    Its rows land in the 1970s, and importing them would plant readings four
    decades before the repository existed.
    """
    export = tmp_path / "star-history.csv"
    export.write_text(
        "Repository,Date,Stars\n"
        "fruits/papaya,Thu Jan 01 1970 04:00:00 GMT+0400 (Gulf Standard Time),1\n"
        "fruits/papaya,Wed Jun 24 1970 17:00:23 GMT+0400 (Gulf Standard Time),7\n",
        encoding="UTF-8",
    )
    records: dict[tuple[str, str], StarRecord] = {}
    with pytest.raises(ValueError, match="by-age export"):
        import_star_history_csv(records, export)
    assert not records
    assert GITHUB_EPOCH.year == 2008


def test_gunzip_salvages_a_truncated_stream():
    """Check a body cut mid-stream keeps everything that did arrive.

    A degraded archive backend routinely cuts the connection after sending most
    of the page, and the star counter sits in markup that arrives well before
    the end.
    """
    payload = ("<html>" + "papaya " * 5000 + "</html>").encode()
    truncated = gzip.compress(payload)[:-40]
    with pytest.raises(EOFError):
        gzip.decompress(truncated)
    salvaged = gunzip(truncated)
    assert salvaged
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
    """Check every counter markup GitHub shipped is still recognized.

    An archived page states the exact figure in an attribute rather than the
    abbreviated `4.4k` readers saw, so a capture yields an integer.
    """
    assert pattern.search(markup)
    assert read_star_counter(markup) == 4412


def test_read_star_counter_finds_nothing_in_an_unrelated_page():
    """Check a page carrying no counter reports its absence."""
    assert read_star_counter("<html><body>no counter here</body></html>") is None


def test_palette_slots_are_distinct_and_two_toned():
    """Check every categorical slot is its own hue in both themes."""
    assert len(SERIES_PALETTE) >= 12
    lights = [light for light, _dark in SERIES_PALETTE]
    darks = [dark for _light, dark in SERIES_PALETTE]
    assert len(set(lights)) == len(lights)
    assert len(set(darks)) == len(darks)
    for light, dark in SERIES_PALETTE:
        # The dark step is selected, not an automatic flip of the light one.
        assert light != dark
        assert re.fullmatch(r"#[0-9a-f]{6}", light)
        assert re.fullmatch(r"#[0-9a-f]{6}", dark)


def test_assign_colors_is_positional_with_overrides_by_name():
    """Check a series keeps its slot, and a pinned hue wins over the palette."""
    colors = assign_colors(["apricot", "papaya"], {"papaya": ["#111111", "#222222"]})
    assert colors["apricot"] == SERIES_PALETTE[0]
    assert colors["papaya"] == ("#111111", "#222222")


def test_assign_colors_refuses_to_cycle_the_palette():
    """Check a chart bigger than the palette raises rather than repeating a hue.

    A repeated colour on a chart whose curves are told apart by colour is a
    defect the reader cannot see.
    """
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
    """Check a configured series name folds into a usable CSS class."""
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
    with pytest.raises(ValueError, match="No star history recorded for chart.svg"):
        build_chart_data({}, spec)


def test_chart_spec_rejects_an_unknown_mode():
    """Check a typo in `mode` raises instead of silently drawing a calendar."""
    with pytest.raises(ValueError, match="Unsupported chart mode"):
        ChartSpec(output=Path("chart.svg"), mode="logarithmic")
    assert set(CHART_MODES) == {"absolute", "relative"}


def test_chart_spec_from_mapping():
    """Check a configuration entry becomes a spec, shorthand included."""
    spec = ChartSpec.from_mapping({
        "output": "docs/assets/chart.svg",
        "mode": "relative",
        "only": ["papaya"],
        "title": "Papaya stars",
    })
    assert spec == ChartSpec(
        output=Path("docs/assets/chart.svg"),
        mode="relative",
        only=("papaya",),
        title="Papaya stars",
    )
    assert ChartSpec.from_mapping({"output": "c.svg", "only": "papaya"}).only == (
        "papaya",
    )
    assert ChartSpec.from_mapping({"output": "c.svg"}).mode == "absolute"


@pytest.mark.parametrize("entry", ({}, {"output": ""}, {"mode": "relative"}))
def test_chart_spec_from_mapping_needs_an_output(entry):
    """Check a chart with nowhere to write is refused."""
    with pytest.raises(ValueError, match="needs an output path"):
        ChartSpec.from_mapping(entry)


def test_chart_spec_from_mapping_rejects_a_scalar_only():
    """Check an `only` that is neither a string nor a list is refused."""
    with pytest.raises(ValueError, match="list of series names"):
        ChartSpec.from_mapping({"output": "c.svg", "only": 3})


def test_render_chart_labels_every_series(sample_history):
    """Check the generator draws, labels and colours each plotted curve.

    The SVG is generated rather than hand-written, so this guards the generator
    against crashes and structural regressions.
    """
    grouped = series(sample_history, SERIES_MAP, PREDECESSORS)
    data = build_chart_data(grouped, ChartSpec(output=Path("chart.svg")))
    svg = render_chart(data, stamp="2026-08-16")

    assert svg.startswith("<svg ")
    assert svg.rstrip().endswith("</svg>")
    # An accessible name, since the chart carries meaning no caption repeats.
    assert 'role="img"' in svg and "aria-label=" in svg
    # Identity is never colour alone: every series is directly labelled.
    for name in SERIES_MAP:
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


def test_render_chart_honors_a_series_subset(sample_history):
    """Check `only` drops the peers, their forerunners included."""
    grouped = series(sample_history, SERIES_MAP, PREDECESSORS)
    spec = ChartSpec(output=Path("chart.svg"), only=("apricot",))
    svg = render_chart(build_chart_data(grouped, spec), stamp="2026-08-16")
    assert 'class="lbl s-apricot"' in svg
    # Keyed on the rendered label, not the hue: an absent series declares no
    # colour either, so its absence has to be checked on what is drawn.
    assert "s-papaya" not in svg


def test_render_chart_relative_swaps_the_calendar_for_project_age(sample_history):
    """Check the by-age axis carries no calendar year among its ticks."""
    grouped = series(sample_history, SERIES_MAP, PREDECESSORS)
    spec = ChartSpec(output=Path("chart.svg"), mode="relative")
    svg = render_chart(
        build_chart_data(grouped, spec), relative=True, stamp="2026-08-16"
    )
    assert "years</text>" in svg
    years = {str(year) for year in range(2000, 2100)}
    assert not years.intersection(re.findall(r">([^<>]+)</text>", svg))


def test_render_chart_escapes_a_series_name(sample_history):
    """Check a name carrying markup cannot break out of the SVG text node."""
    grouped = {"a & b": [(date(2020, 1, 1), 1), (date(2021, 1, 1), 5)]}
    data = build_chart_data(grouped, ChartSpec(output=Path("chart.svg")))
    svg = render_chart(data, stamp="2026-08-16")
    assert "a &amp; b" in svg
    assert ">a & b" not in svg


def test_write_chart_is_convergent(tmp_path, sample_history):
    """Check redrawing an unmoved history rewrites nothing.

    The caption is stamped with the newest reading rather than with today, so a
    scheduled pass that found no new star leaves the committed SVG alone.
    """
    grouped = series(sample_history, SERIES_MAP, PREDECESSORS)
    spec = ChartSpec(output=tmp_path / "nested" / "chart.svg")
    assert write_chart(grouped, spec, stamp="2026-08-16") is True
    assert write_chart(grouped, spec, stamp="2026-08-16") is False


def test_sample_current_records_the_count_and_the_origin(monkeypatch):
    """Check one sample yields today's figure plus the repository's birth.

    The creation date is immutable and rides on the same payload, so the origin
    is recorded without a second call.
    """
    monkeypatch.setattr(
        "repomatic.stars.gh_api_json",
        lambda args: {"stargazers_count": 57, "created_at": "2021-12-09T10:11:12Z"},
    )
    records: dict[tuple[str, str], StarRecord] = {}
    outcome = sample_current(records, "papaya", "fruits/papaya", day="2026-08-16")

    assert outcome.stars == 57
    assert outcome.points == 2
    assert records[("fruits/papaya", "2026-08-16")].source == "sample"
    assert records[("fruits/papaya", "2021-12-09")] == StarRecord(
        "fruits/papaya", "2021-12-09", 0, "created"
    )


def test_sample_current_survives_an_unreadable_repository(monkeypatch):
    """Check one unreachable repository does not cost the other samples."""
    monkeypatch.setattr("repomatic.stars.gh_api_json", lambda args: None)
    records: dict[tuple[str, str], StarRecord] = {}
    outcome = sample_current(records, "papaya", "fruits/papaya", day="2026-08-16")

    assert outcome.note == "unreadable"
    assert outcome.stars is None
    assert not records


def test_reconstruct_accumulates_one_point_per_day_that_moved(monkeypatch):
    """Check per-star timestamps collapse into a cumulative daily curve."""
    pages = [
        json.dumps([
            {"starred_at": "2021-12-09T10:00:00Z"},
            {"starred_at": "2021-12-09T11:00:00Z"},
            {"starred_at": "2022-03-01T09:00:00Z"},
        ]),
        json.dumps([]),
    ]
    monkeypatch.setattr("repomatic.stars.run_gh_command", lambda args: pages.pop(0))
    records: dict[tuple[str, str], StarRecord] = {}
    outcome = reconstruct_from_github(records, "papaya", "fruits/papaya")

    assert outcome.stars == 3
    assert records[("fruits/papaya", "2021-12-09")].stars == 2
    assert records[("fruits/papaya", "2022-03-01")].stars == 3
    assert all(record.source == "github" for record in records.values())


def test_reconstruct_reports_a_repository_the_token_cannot_administer(monkeypatch):
    """Check the restricted endpoint's 404 reads as a skip, not a failure.

    GitHub answers `404` rather than `403` for every repository the token does
    not administer, which is the common case and must stay quiet.
    """

    def refuse(args):
        msg = "gh: Not Found (HTTP 404)"
        raise RuntimeError(msg)

    monkeypatch.setattr("repomatic.stars.run_gh_command", refuse)
    records: dict[tuple[str, str], StarRecord] = {}
    outcome = reconstruct_from_github(records, "papaya", "fruits/papaya")

    assert outcome.note == "not an admin, not readable"
    assert not records


def test_reconstruct_writes_nothing_when_pagination_breaks_midway(monkeypatch):
    """Check a failure mid-walk abandons the repository rather than truncating.

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

    monkeypatch.setattr("repomatic.stars.run_gh_command", flaky)
    records: dict[tuple[str, str], StarRecord] = {}
    outcome = reconstruct_from_github(records, "papaya", "fruits/papaya")

    assert "abandoned on page 2" in outcome.note
    assert not records


def test_backfill_wayback_skips_an_exactly_reconstructed_repository():
    """Check the archives are spent only where no better source exists."""
    records = {
        ("fruits/papaya", "2021-01-01"): StarRecord(
            "fruits/papaya", "2021-01-01", 5, "github"
        )
    }
    outcome = backfill_wayback(records, "papaya", "fruits/papaya")
    assert outcome.note == "already reconstructed exactly"
    assert outcome.points == 0


def test_fetch_names_why_it_gave_up(monkeypatch):
    """Check an exhausted retry budget reports what happened, not just failure.

    A run reporting only "unreachable" cannot tell a service refusing every
    request from one this collector is asking wrongly.
    """
    monkeypatch.setattr("repomatic.stars.time.sleep", lambda seconds: None)

    def always_503(request, timeout=0):
        raise urllib.error.HTTPError(request.full_url, 503, "nope", Message(), None)

    monkeypatch.setattr("repomatic.stars.urllib.request.urlopen", always_503)
    assert fetch("https://web.archive.org/whatever", tries=3) is None
    assert last_fetch_failure() == "3x HTTP 503"


def test_committed_star_history_is_well_formed():
    """Check the history this repository accrues keeps its expected shape.

    Guards a file a scheduled job appends to unattended: a duplicated day, a
    repository that left the configuration, or an unknown provenance would all
    surface here rather than as a misdrawn chart.
    """
    if not STAR_STORE.exists():
        pytest.skip("no star history recorded yet")
    records = json.loads(STAR_STORE.read_text(encoding="UTF-8"))
    assert isinstance(records, list)
    assert records, "the history file exists but holds nothing"

    config = repo_config()
    # Covers the retired forerunners too, which are collected but hold no
    # series of their own.
    tracked = set(
        collected_repos(config.stars.series, config.stars.predecessors).values()
    )
    today = datetime.now(tz=timezone.utc).date()
    seen = set()

    for record in records:
        assert set(record) == {"date", "repo", "source", "stars"}
        assert record["repo"] in tracked
        assert record["source"] in SOURCES
        assert isinstance(record["stars"], int)
        assert record["stars"] >= 0
        # Dates are plain ISO days, never timestamps: the chart plots daily.
        assert GITHUB_EPOCH <= date.fromisoformat(record["date"]) <= today
        key = (record["repo"], record["date"])
        assert key not in seen, f"duplicate record for {key}"
        seen.add(key)

    keys = [(record["repo"], record["date"]) for record in records]
    assert keys == sorted(keys)


def test_committed_charts_are_redrawable():
    """Check every configured chart still renders from the committed history.

    The charts are generated and committed, so nothing else would notice a
    series renamed out from under a chart until the scheduled job went red.
    """
    if not STAR_STORE.exists():
        pytest.skip("no star history recorded yet")
    config = repo_config()
    records = load_star_records(STAR_STORE)
    grouped = series(records, config.stars.series, config.stars.predecessors)
    assert config.stars.charts, "no chart declared to draw"
    for entry in config.stars.charts:
        spec = ChartSpec.from_mapping(entry)
        data = build_chart_data(grouped, spec, config.stars.colors)
        svg = render_chart(data, relative=spec.relative, title=spec.title)
        assert svg.startswith("<svg ")
        assert (REPO_ROOT / spec.output).exists(), f"{spec.output} was never written"


def test_tracked_series_are_github_slugs():
    """Check every plotted series names a repository the sampler can read.

    The history rests on the aggregate count GitHub kept public, and no other
    forge is read here: a URL or a bare name would sample nothing.
    """
    config = repo_config()
    declared = {**config.stars.series, **config.stars.predecessors}
    assert declared, "no series declared"
    for name, slug in declared.items():
        assert name == name.lower(), f"{name} should be lowercase"
        assert "://" not in slug, f"{name} needs an owner/name slug, got {slug!r}"
        owner, _, repo = slug.partition("/")
        assert owner and repo and "/" not in repo, (
            f"{name} needs an owner/name slug, got {slug!r}"
        )
    # A forerunner belongs to a series that is actually plotted.
    assert set(config.stars.predecessors) <= set(config.stars.series)
