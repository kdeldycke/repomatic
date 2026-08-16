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

"""Draw an accumulated metric history as a standalone, themeable SVG.

Written by hand rather than through a plotting library: the output is
committed, so a docs build never needs the dependency, and the file stays a few
kilobytes of readable vector.

An SVG rather than a client-side canvas: GitHub strips `<script>` and
`<canvas>` from rendered Markdown, so a scripted chart is invisible to every
reader of the repository, while the third-party embeds these replace were
images that rendered there. Committing it also drops the pinned CDN artifact
and its subresource-integrity digest, which is the point of moving off a
service that died without notice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from html import escape
from math import floor, log10
from pathlib import Path

from .metrics import CHARTABLE_METRICS, METRICS_BY_ID, PREDECESSOR_SUFFIX

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

CHART_MODES = ("absolute", "relative")
"""Horizontal axes a chart can measure against.

`absolute` shares one calendar across every curve, answering when a project
gathered its following. `relative` starts each curve at its own repository's
creation, which is the only origin they all share, so a project that took eight
years to reach a figure another hit in two is read at a glance.

Kept separate from {data}`CHART_SCALES`, which measures the vertical one: a
comparison chart routinely wants both, and folding them into a single setting
would make each pair of choices a new name.
"""

CHART_SCALES = ("linear", "logarithmic")
"""Vertical axes a chart can measure against.

`linear` reads a difference, and is right whenever the series are the same size.
`logarithmic` reads a *rate*, and is what puts a project of 57 stars on one
chart with a peer of 25,000 without flattening it onto the axis: equal slopes
mean equal growth in percentage terms, whatever the counts.

A count of zero has no logarithm, and every series carries one, since the day a
repository was created is the only date its count is known exactly. So the
bottom {data}`LOG_ZERO_BAND` of the plot is kept linear, spanning nothing but
the step from zero to one. The curve then leaves the axis where the first star
landed rather than beginning in mid-air or being silently dropped.
"""

LABEL_CHAR_WIDTH = 7.6
"""Pixels a direct label's average character occupies, for margin arithmetic.

Measured against the 13px semibold `system-ui` the labels are drawn in. An SVG
carries no text metrics and this generator loads no font, so the width of a
label can only be estimated: erring high costs a few pixels of plot, erring low
clips the name off the edge of the chart.
"""

LOG_ZERO_BAND = 0.06
"""Fraction of a logarithmic plot's height reserved for the zero-to-one step.

Small enough to read as a baseline rather than as a decade of its own, and
large enough that a curve sitting at zero for years is visibly on the floor
instead of indistinguishable from one at a count of one.
"""

MIN_LABEL_MARGIN = 168
"""Floor on the right margin, in pixels, whatever the labels measure.

Holds the plot's proportions steady across the charts a project draws: a
single-series chart would otherwise stretch nearly to the edge and read as a
different shape from the comparison beside it.
"""

SERIES_PALETTE: tuple[tuple[str, str], ...] = (
    ("#2a78d6", "#3987e5"),
    ("#eb6834", "#d95926"),
    ("#1baf7a", "#199e70"),
    ("#eda100", "#c98500"),
    ("#e87ba4", "#d55181"),
    ("#8250df", "#a371f7"),
    ("#0a7c8a", "#22b8cf"),
    ("#cf222e", "#ff7b72"),
    ("#5a7f10", "#8fc832"),
    ("#8a6240", "#c19a6b"),
    ("#57606a", "#9198a1"),
    ("#bf3989", "#e878b8"),
)
"""Light and dark hex pair per categorical slot, in fixed order.

Assigned positionally and never cycled: a chart declaring more series than
there are slots raises rather than reusing a hue, since a repeated colour on a
chart whose curves are told apart by colour is a defect the reader cannot see.
Override any of them by name through `[tool.repomatic.metrics] colors`.

A few light-mode steps sit below 3:1 against a white surface. The direct label
drawn at the end of every line is what answers that: identity is never colour
alone.
"""

_CSS_UNSAFE_RE = re.compile(r"[^a-z0-9]+")
"""Runs of characters a CSS class name cannot carry, folded to one dash."""


@dataclass(frozen=True)
class ChartSpec:
    """One chart a repository asked for."""

    output: Path
    """Where to write the rendered SVG."""

    metric: str = "stars"
    """Which accruing metric to plot, from {data}`~repomatic.metrics.METRICS`.

    Defaults to the one that motivated the whole collector. Only a metric the
    store accrues can be charted: an attribute holds a single current value,
    which is a table cell rather than a curve.
    """

    mode: str = "absolute"
    """Which of {data}`CHART_MODES` measures the horizontal axis."""

    only: tuple[str, ...] = ()
    """Series to plot, in draw order. Every declared subject when empty."""

    scale: str = "linear"
    """Which of {data}`CHART_SCALES` measures the vertical axis."""

    title: str = ""
    """Accessible name for the chart, describing what it shows."""

    @property
    def logarithmic(self) -> bool:
        """Whether the vertical axis measures by powers of ten."""
        return self.scale == "logarithmic"

    @property
    def relative(self) -> bool:
        """Whether the horizontal axis measures project age."""
        return self.mode == "relative"

    @classmethod
    def from_mapping(cls, entry: Mapping[str, object]) -> ChartSpec:
        """Build a spec from one `[[tool.repomatic.metrics.charts]]` entry.

        :param entry: The entry as configuration parsed it.
        :return: The corresponding spec.
        :raises ValueError: When `output` is missing, the mode is unknown, or
            the named metric has no history to chart.
        """
        output = entry.get("output")
        if not output or not isinstance(output, str):
            msg = (
                "A [[tool.repomatic.metrics.charts]] entry needs an output path: "
                f"{entry!r}"
            )
            raise ValueError(msg)
        metric = str(entry.get("metric") or "stars")
        if metric not in CHARTABLE_METRICS:
            chartable = ", ".join(CHARTABLE_METRICS)
            msg = (
                f"Chart {output} plots metric {metric!r}, which the store keeps no "
                f"history of. Pick one of: {chartable}."
            )
            raise ValueError(msg)
        declared = entry.get("only") or ()
        # A bare string is the natural single-series shorthand, and accepting it
        # costs one branch where rejecting it costs a confusing error about a
        # chart that plotted one letter per series.
        if isinstance(declared, str):
            only: tuple[str, ...] = (declared,)
        elif isinstance(declared, (list, tuple)):
            only = tuple(str(name) for name in declared)
        else:
            msg = f"A chart's `only` must be a list of series names: {declared!r}"
            # ValueError, not the TypeError ruff proposes: this reports a value
            # a human wrote in `pyproject.toml`, and every other configuration
            # fault here raises ValueError for one CLI handler to catch.
            raise ValueError(msg)  # noqa: TRY004
        return cls(
            output=Path(output),
            metric=metric,
            mode=str(entry.get("mode") or "absolute"),
            only=only,
            scale=str(entry.get("scale") or "linear"),
            title=str(entry.get("title") or ""),
        )

    def __post_init__(self) -> None:
        """Reject an axis nothing implements, which would silently draw the default.

        Both axes fail the same way and so are checked the same way: a
        misspelled `logarithmic` that fell through to linear would draw a chart
        that looks plausible, and misreads every series on it by orders of
        magnitude.
        """
        if self.mode not in CHART_MODES:
            modes = ", ".join(CHART_MODES)
            msg = (
                f"Unsupported chart mode {self.mode!r} for {self.output}: pick {modes}."
            )
            raise ValueError(msg)
        if self.scale not in CHART_SCALES:
            scales = ", ".join(CHART_SCALES)
            msg = (
                f"Unsupported chart scale {self.scale!r} for {self.output}: "
                f"pick {scales}."
            )
            raise ValueError(msg)


@dataclass
class ChartData:
    """A chart's plotted series, already grouped and ordered.

    Holds what {func}`render_chart` draws, so the renderer never touches the
    store and stays testable against synthetic points.
    """

    points: dict[str, list[tuple[date, int]]] = field(default_factory=dict)
    """One sorted list of `(day, value)` per series, in draw order."""

    colors: dict[str, tuple[str, str]] = field(default_factory=dict)
    """Light and dark hex pair per series, keyed like {attr}`points`."""


def css_class(name: str) -> str:
    """Fold a series name into a CSS class fragment.

    :param name: A series name as the repository declared it.
    :return: The lowercased name with every unsafe run replaced by a dash.
    """
    return _CSS_UNSAFE_RE.sub("-", name.lower()).strip("-") or "series"


def assign_colors(
    names: Sequence[str],
    overrides: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, tuple[str, str]]:
    """Give every series a light and dark hue.

    Positional from {data}`SERIES_PALETTE` in *names* order, so a chart's first
    curve is always the first slot, with *overrides* winning by name. A hue is
    a property of the series rather than of the chart, which is what keeps a
    repository plotted on two charts recognizable across both.

    :param names: Series to colour, in draw order.
    :param overrides: Per-name `[light, dark]` pairs from configuration.
    :return: The light and dark pair of each name.
    :raises ValueError: When more series need a slot than the palette holds,
        or when an override is not a light and dark pair.
    """
    overrides = overrides or {}
    unslotted = [name for name in names if name not in overrides]
    if len(unslotted) > len(SERIES_PALETTE):
        msg = (
            f"{len(unslotted)} series need a colour but the palette holds "
            f"{len(SERIES_PALETTE)}. Split the chart, or name the extra hues in "
            "[tool.repomatic.metrics] colors."
        )
        raise ValueError(msg)
    slots = iter(SERIES_PALETTE)
    colors: dict[str, tuple[str, str]] = {}
    for name in names:
        override = overrides.get(name)
        if override is None:
            colors[name] = next(slots)
            continue
        pair = tuple(override)
        if len(pair) != 2:
            msg = f"Colour override for {name!r} needs a [light, dark] pair."
            raise ValueError(msg)
        colors[name] = (str(pair[0]), str(pair[1]))
    return colors


def build_chart_data(
    grouped: Mapping[str, list[tuple[date, int]]],
    spec: ChartSpec,
    overrides: Mapping[str, Sequence[str]] | None = None,
) -> ChartData:
    """Select, order and colour the series one chart plots.

    A forerunner rides along with the series it precedes rather than being
    selected on its own, and borrows that series' hue instead of claiming a
    slot of its own.

    :param grouped: Every recorded series, as {func}`repomatic.metrics.series`
        returns them.
    :param spec: The chart to prepare.
    :param overrides: Per-name `[light, dark]` pairs from configuration.
    :return: The chart's points and colours.
    :raises ValueError: When the chart plots nothing, when two series fold onto
        one CSS class, or when the palette runs out.
    """
    wanted = (
        list(spec.only)
        if spec.only
        else [name for name in grouped if not name.endswith(PREDECESSOR_SUFFIX)]
    )
    points: dict[str, list[tuple[date, int]]] = {}
    for name in wanted:
        if name in grouped:
            points[name] = grouped[name]
        prior = name + PREDECESSOR_SUFFIX
        if prior in grouped:
            points[prior] = grouped[prior]
    if not points:
        plotted = ", ".join(wanted) or "nothing"
        msg = (
            f"No star history recorded for {spec.output.name} ({plotted}). "
            "Run a sample or a backfill first."
        )
        raise ValueError(msg)

    classes: dict[str, str] = {}
    for name in wanted:
        fragment = css_class(name)
        if fragment in classes:
            msg = (
                f"Series {name!r} and {classes[fragment]!r} both fold onto the CSS "
                f"class {fragment!r}: rename one of them."
            )
            raise ValueError(msg)
        classes[fragment] = name

    colors = assign_colors(wanted, overrides)
    for name in list(points):
        base = name.removesuffix(PREDECESSOR_SUFFIX)
        colors.setdefault(name, colors[base])
    return ChartData(points=points, colors=colors)


def render_chart(
    data: ChartData,
    *,
    relative: bool = False,
    logarithmic: bool = False,
    title: str = "",
    label: str = "Stars",
    stamp: str | None = None,
) -> str:
    """Draw the line chart as a standalone, themeable SVG.

    :param data: The series to plot and their hues.
    :param relative: Measure the horizontal axis from each repository's own
        first point rather than from the calendar.
    :param logarithmic: Measure the vertical axis by powers of ten, so series
        orders of magnitude apart stay legible on one chart. See
        {data}`CHART_SCALES` for how the zero every series carries is placed.
    :param title: Accessible name for the chart. Derived from the metric and
        the mode when empty.
    :param label: What the vertical axis counts, from the plotted metric's
        {attr}`~repomatic.metrics.Metric.label`.
    :param stamp: Sampling date shown in the caption, in `YYYY-MM-DD` form.
        Today (UTC) when `None`.
    :return: The complete SVG document.
    """
    if stamp is None:
        stamp = datetime.now(tz=timezone.utc).date().isoformat()
    grouped = data.points

    width, height = 960, 460
    # The right margin carries the direct labels, so it is measured from the
    # longest one rather than fixed. A constant wide enough for one project's
    # own chart clips `semantic-release · 23,977` on a comparison of ten, and an
    # SVG text node does not wrap: the overflow is simply cut by the viewBox.
    captions = [
        len(f"{name} · {points[-1][1]:,}")
        for name, points in grouped.items()
        if not name.endswith(PREDECESSOR_SUFFIX)
    ]
    left, top, bottom = 58, 28, 46
    right = max(MIN_LABEL_MARGIN, round(max(captions, default=0) * LABEL_CHAR_WIDTH) + 20)
    plot_w, plot_h = width - left - right, height - top - bottom

    all_points = [point for points in grouped.values() for point in points]
    first_day = min(day for day, _stars in all_points)
    last_day = max(day for day, _stars in all_points)
    peak = max(stars for _day, stars in all_points)

    # Both modes plot a day offset; only its origin differs. Absolute measures
    # from the earliest date on the chart, so the curves share a calendar.
    # Relative measures each series from its own first point, which slides
    # every project to a common birth and compares trajectories.
    offsets: dict[str, list[tuple[int, int]]] = {}
    for name, points in grouped.items():
        origin = points[0][0] if relative else first_day
        offsets[name] = [((day - origin).days, stars) for day, stars in points]
    span = max((max(x for x, _s in points) for points in offsets.values()), default=1)
    span = max(span, 1)

    # Round the axis up to a decade-ish step so the top gridline is a whole
    # number the eye can anchor on. Annotated because `int ** int` widens to
    # `Any` (a negative exponent would yield a float), which would otherwise
    # leak all the way into the plotted coordinates.
    step: int = 10 ** (len(str(peak)) - 1)
    step = step // 2 if peak / step < 2 else step
    ceiling: int = ((peak // step) + 1) * step

    # A logarithmic axis stops at the peak itself rather than at the next whole
    # decade above it: rounding a peak of 25,057 up to 100,000 would spend three
    # quarters of the height on emptiness. Its gridlines are the decades falling
    # below that, which stay nameable powers of ten wherever the top lands.
    log_span = max(log10(peak), 1.0) if peak >= 1 else 1.0

    def x_of(offset: int) -> float:
        return left + offset / span * plot_w

    def y_of(stars: int) -> float:
        if not logarithmic:
            return top + plot_h - (stars / ceiling) * plot_h
        if stars < 1:
            # The floor itself, which is what {data}`LOG_ZERO_BAND` reserves.
            return top + plot_h
        height = LOG_ZERO_BAND + (1 - LOG_ZERO_BAND) * log10(stars) / log_span
        return top + plot_h - height * plot_h

    parts: list[str] = []

    # Horizontal gridlines and their value labels: quarters of the ceiling on a
    # linear axis, every decade plus the zero baseline on a logarithmic one.
    gridlines: list[int] = (
        [0, *(10**power for power in range(floor(log_span) + 1))]
        if logarithmic
        else [round(ceiling * i / 4) for i in range(5)]
    )
    for value in gridlines:
        y = y_of(value)
        parts.append(
            f'<line class="grid" x1="{left}" y1="{y:.1f}" '
            f'x2="{left + plot_w}" y2="{y:.1f}"/>'
        )
        parts.append(
            f'<text class="tick" x="{left - 10}" y="{y + 4:.1f}" '
            f'text-anchor="end">{value:,}</text>'
        )

    # Vertical gridlines: calendar years when absolute, years of age when
    # relative. Thinned on a long relative span so the labels stay readable.
    if relative:
        stride = 1 + span // 365 // 8
        marks = [
            (year * 365, "1st year" if year == 1 else f"{year} years")
            for year in range(stride, span // 365 + 1, stride)
        ]
    else:
        marks = [
            ((date(year, 1, 1) - first_day).days, str(year))
            for year in range(first_day.year, last_day.year + 1)
            if first_day <= date(year, 1, 1) <= last_day
        ]
    for offset, caption in marks:
        x = x_of(offset)
        parts.append(
            f'<line class="grid" x1="{x:.1f}" y1="{top}" '
            f'x2="{x:.1f}" y2="{top + plot_h}"/>'
        )
        parts.append(
            f'<text class="tick" x="{x:.1f}" y="{top + plot_h + 20}" '
            f'text-anchor="middle">{caption}</text>'
        )

    # Series, drawn in the fixed order so a name keeps its hue.
    labels: list[tuple[float, str, str]] = []
    for key, shifted in offsets.items():
        # The separator, not the tail: `partition` returns what follows it,
        # which for a key ending in the suffix is always the empty string.
        name, prior, _rest = key.partition(PREDECESSOR_SUFFIX)
        slug = css_class(name)
        coords = " ".join(f"{x_of(x):.1f},{y_of(s):.1f}" for x, s in shifted)
        css = f"s-{slug} prior" if prior else f"s-{slug}"
        parts.append(f'<polyline class="{css}" points="{coords}"/>')
        end_offset, end_stars = shifted[-1]
        if prior:
            # Annotated where it stops rather than in the right margin: a
            # forerunner ends mid-chart, and a label parked at the edge would
            # read as its final position on a date it never reached.
            parts.append(
                f'<text class="lbl prior s-{slug}" '
                f'x="{x_of(end_offset) + 6:.1f}" '
                f'y="{y_of(end_stars) - 8:.1f}">retired · {end_stars:,}</text>'
            )
            continue
        labels.append((y_of(end_stars), name, f"{end_stars:,}"))

    # Direct labels at each line's end, the relief for the light-mode hues that
    # sit below 3:1. Nudged apart so close finishers stay legible.
    labels.sort()
    for index in range(1, len(labels)):
        if labels[index][0] - labels[index - 1][0] < 15:
            # Rebuilt field by field rather than with a starred unpack, which
            # widens the tuple to a homogeneous type mypy cannot match.
            _label_y, label_name, label_text = labels[index]
            labels[index] = (labels[index - 1][0] + 15, label_name, label_text)
    for label_y, label_name, label_text in labels:
        caption = escape(f"{label_name} · {label_text}")
        parts.append(
            f'<text class="lbl s-{css_class(label_name)}" x="{left + plot_w + 12}" '
            f'y="{label_y + 4:.1f}">{caption}</text>'
        )

    css_light = "\n".join(
        f"    .s-{css_class(name)}{{stroke:{light};color:{light}}}"
        for name, (light, _dark) in data.colors.items()
        if not name.endswith(PREDECESSOR_SUFFIX)
    )
    css_dark = "\n".join(
        f"      .s-{css_class(name)}{{stroke:{dark};color:{dark}}}"
        for name, (_light, dark) in data.colors.items()
        if not name.endswith(PREDECESSOR_SUFFIX)
    )

    # Named in the accessible description as well as the caption: a
    # logarithmic chart read as a linear one is not slightly misread, it is
    # misread by orders of magnitude, and a screen reader has only this to go on.
    scale_note = ", logarithmic scale" if logarithmic else ""
    if relative:
        axis = f"{label} by age of the project, aligned on each series' origin"
        described = title or f"{label} by project age{scale_note}"
    else:
        axis = f"{label}, {first_day} to {last_day}"
        described = title or f"{label} history{scale_note}"
    axis += scale_note
    body = "\n  ".join(parts)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}"
     width="{width}" height="{height}" font-family="system-ui, sans-serif"
     role="img" aria-label="{escape(described, quote=True)}">
  <style>
    text{{fill:#0b0b0b}}
    .tick{{font-size:12px;fill:#52514e}}
    .lbl{{font-size:13px;font-weight:600;fill:currentColor}}
    .grid{{stroke:#0b0b0b;stroke-opacity:.10;stroke-width:1}}
    .axis{{font-size:12px;fill:#52514e}}
    polyline{{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}}
    .prior{{stroke-dasharray:6 4;stroke-width:1.5;opacity:.65}}
    text.prior{{font-size:11px;font-weight:600;opacity:1}}
{css_light}
    @media (prefers-color-scheme: dark) {{
      text{{fill:#fff}}
      .tick,.axis{{fill:#c3c2b7}}
      .grid{{stroke:#fff;stroke-opacity:.14}}
{css_dark}
    }}
  </style>
  {body}
  <text class="axis" x="{left}" y="{height - 10}">{escape(axis)} · sampled on {stamp}</text>
</svg>
"""


def write_chart(
    grouped: Mapping[str, list[tuple[date, int]]],
    spec: ChartSpec,
    overrides: Mapping[str, Sequence[str]] | None = None,
    stamp: str | None = None,
) -> bool:
    """Render one chart and write it, leaving an unchanged file alone.

    :param grouped: Every recorded series, as {func}`repomatic.metrics.series`
        returns them.
    :param spec: The chart to draw.
    :param overrides: Per-name `[light, dark]` pairs from configuration.
    :param stamp: Sampling date shown in the caption. Today (UTC) when `None`.
    :return: `True` when the file content changed.
    :raises ValueError: When the chart cannot be built (see
        {func}`build_chart_data`).
    """
    data = build_chart_data(grouped, spec, overrides)
    content = render_chart(
        data,
        relative=spec.relative,
        logarithmic=spec.logarithmic,
        title=spec.title,
        label=METRICS_BY_ID[spec.metric].label,
        stamp=stamp,
    )
    if spec.output.exists() and spec.output.read_text(encoding="UTF-8") == content:
        return False
    spec.output.parent.mkdir(parents=True, exist_ok=True)
    spec.output.write_text(content, encoding="UTF-8")
    return True
