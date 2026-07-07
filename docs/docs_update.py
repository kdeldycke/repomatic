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
"""Dynamic documentation content generation.

Auto-detected and executed by the upstream ``docs.yaml`` reusable workflow
via ``repomatic update-docs``.
"""

from __future__ import annotations

import re
from pathlib import Path

from click_extra import TableFormat, render_table

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
TOOL_RUNNER_MD = PROJECT_ROOT / "docs" / "tool-runner.md"


def replace_content(
    filepath: Path,
    new_content: str,
    start_tag: str,
    end_tag: str | None = None,
) -> None:
    """Replace in a file the content between start and end tags.

    The ``new_content`` payload is wrapped with a blank line on both sides so
    the resulting region is format-stable through ``mdformat``. ``mdformat``
    treats the surrounding ``<!-- ... -->`` markers as block-level HTML and
    inserts blank lines around them on every pass: emitting them up front
    avoids a generator/formatter ping-pong on every CI run.
    """
    filepath = filepath.resolve()
    assert filepath.exists(), f"File {filepath} does not exist."
    assert filepath.is_file(), f"File {filepath} is not a file."

    orig_content = filepath.read_text()

    assert start_tag in orig_content, (
        f"Start tag {start_tag!r} not found in {filepath}."
    )
    pre_content, table_start = orig_content.split(start_tag, 1)

    if end_tag:
        _, post_content = table_start.split(end_tag, 1)
    else:
        end_tag = ""
        post_content = ""

    wrapped = f"\n\n{new_content.strip()}\n\n" if new_content.strip() else "\n\n"
    filepath.write_text(
        f"{pre_content}{start_tag}{wrapped}{end_tag}{post_content}",
    )


def _github_repo(url: str) -> str | None:
    """Extract ``owner/repo`` from a GitHub URL."""
    m = re.match(r"https?://github\.com/([^/]+/[^/]+)", url or "")
    return m.group(1) if m else None


# Tools that lack a usable GitHub "latest release" object.
# mdformat has zero GitHub Releases; mypy only has pre-releases.
_PYPI_RELEASE_TOOLS = frozenset({"mdformat", "mypy"})

_BADGE = "label=%20&style=flat-square"


def tool_summary() -> str:
    """Generate the summary table of all managed tools."""
    from repomatic.tool_runner import TOOL_REGISTRY, NativeFormat

    rows: list[list[str]] = []
    for key in sorted(TOOL_REGISTRY):
        spec = TOOL_REGISTRY[key]
        label = spec.display_name or spec.name
        name_link = f"[{label}]({spec.datasource_url})"

        if spec.binary:
            install_type = "Binary"
        elif spec.npm:
            install_type = "npm"
        elif spec.needs_venv:
            install_type = "PyPI (venv)"
        else:
            install_type = "PyPI"

        # Config discovery column.
        parts: list[str] = []
        if spec.native_config_files:
            parts.extend(f"`{f}`" for f in spec.native_config_files)
        if spec.reads_pyproject or spec.native_format is NativeFormat.FLAGS:
            parts.append(f"`[tool.{spec.name}]` in `pyproject.toml`")
        config_str = ", ".join(parts) if parts else "CLI flags only"

        rows.append([name_link, f"`{spec.version}`", install_type, config_str])

    # Trailing newline ensures a blank line before the closing marker.
    # Without it, mdformat-gfm reinserts one on every run (a GFM table
    # needs a blank line before the next HTML comment), causing an
    # `update-docs` ↔ `format-markdown` ping-pong on `main`.
    return (
        render_table(
            rows,
            headers=["Tool", "Version", "Type", "Config discovery"],
            table_format=TableFormat.GITHUB,
            colalign=("left", "left", "left", "left"),
        )
        + "\n"
    )


def _tool_manual_block(key: str) -> str:
    """Return the hand-maintained extra docs for a tool's section, if any.

    Each per-tool section in ``tool-runner.md`` ends with a
    ``<!-- {key}-manual-start -->`` / ``<!-- {key}-manual-end -->`` region whose
    body is written by hand. The generator owns the metadata above the markers;
    everything between them is merged back verbatim so ``update-docs`` never
    clobbers the prose.
    """
    if not TOOL_RUNNER_MD.exists():
        return ""
    match = re.search(
        rf"<!-- {re.escape(key)}-manual-start -->(.*?)<!-- {re.escape(key)}-manual-end -->",
        TOOL_RUNNER_MD.read_text(encoding="UTF-8"),
        re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def tool_reference() -> str:
    """Generate per-tool detail sections + comparison tables.

    The metadata of each section (version, install, config, flags, links) is
    generated from the registry. A trailing manual region per tool is preserved
    across runs by :func:`_tool_manual_block`, so hand-written examples and
    caveats survive regeneration.
    """
    from repomatic.tool_runner import TOOL_REGISTRY, NativeFormat

    lines: list[str] = []

    # --- Per-tool detail sections ---
    for key in sorted(TOOL_REGISTRY):
        spec = TOOL_REGISTRY[key]
        label = spec.display_name or spec.name
        name_link = f"[{label}]({spec.datasource_url})"

        lines.append(f"### {name_link}")
        lines.append("")

        lines.append(f"**Installed version:** `{spec.version}`")
        lines.append("")

        if spec.binary:
            install_type = "Binary (downloaded from GitHub Releases)"
        elif spec.npm:
            install_type = "npm registry, run via `node_modules/.bin`"
        elif spec.needs_venv:
            install_type = "PyPI, runs in project virtualenv via `uv run`"
        else:
            install_type = "PyPI, installed via `uvx`"
        lines.append(f"**Installation method:** {install_type}")
        lines.append("")

        if spec.native_config_files:
            files_str = ", ".join(f"`{f}`" for f in spec.native_config_files)
            if spec.reads_pyproject:
                files_str += f" and `[tool.{spec.name}]` in `pyproject.toml` (native)"
            lines.append(f"**Config files:** {files_str}")
            lines.append("")
        elif spec.reads_pyproject:
            lines.append(
                f"**Config:** `[tool.{spec.name}]` in `pyproject.toml` (native)"
            )
            lines.append("")
        elif spec.native_format is NativeFormat.FLAGS:
            lines.append(
                f"**Config:** `[tool.{spec.name}]` in `pyproject.toml`"
                " (translated to CLI flags)"
            )
            lines.append("")
        else:
            lines.append("**Config:** CLI flags only")
            lines.append("")

        if spec.config_flag and not spec.reads_pyproject:
            lines.append(
                f"**`[tool.{spec.name}]` bridge:** repomatic translates to"
                f" {spec.native_format.name} and passes via `{spec.config_flag}`."
            )
            lines.append("")

        if spec.default_flags:
            flags_str = " ".join(f"`{f}`" for f in spec.default_flags)
            lines.append(f"**Default flags:** {flags_str}")
            lines.append("")

        if spec.ci_flags:
            ci_str = " ".join(f"`{f}`" for f in spec.ci_flags)
            lines.append(f"**CI flags:** {ci_str}")
            lines.append("")

        if spec.default_config:
            data_url = (
                "https://github.com/kdeldycke/repomatic/blob/main/repomatic/data/"
                + spec.default_config
            )
            lines.append(f"**Bundled default:** [`{spec.default_config}`]({data_url})")
            lines.append("")

        if spec.with_packages:
            lines.append("**Plugins:**")
            lines.append("")
            for pkg in spec.with_packages:
                display = pkg.split("==")[0].split("@")[0].strip()
                lines.append(f"- `{display}`")
            lines.append("")

        doc_links = []
        if spec.source_url:
            doc_links.append(f"[Source]({spec.source_url})")
        if spec.config_docs_url:
            doc_links.append(f"[Config reference]({spec.config_docs_url})")
        if spec.cli_docs_url:
            doc_links.append(f"[CLI usage]({spec.cli_docs_url})")
        if doc_links:
            lines.append(" | ".join(doc_links))
            lines.append("")

        # Hand-maintained extra docs for this tool, merged back verbatim on
        # every run. Authors add examples and caveats between the markers; the
        # generator preserves them and never overwrites the prose.
        manual = _tool_manual_block(key)
        lines.append(f"<!-- {key}-manual-start -->")
        lines.append("")
        if manual:
            lines.append(manual)
            lines.append("")
        lines.append(f"<!-- {key}-manual-end -->")
        lines.append("")

    # --- Comparison table ---
    def _last_release(key, spec, repo):
        pkg = spec.package or spec.name
        if key in _PYPI_RELEASE_TOOLS:
            return f"![Last release](https://img.shields.io/pypi/v/{pkg}?{_BADGE})"
        return f"![Last release](https://img.shields.io/github/release-date/{repo}?{_BADGE})"

    lines.append("## Comparison")
    lines.append("")
    _badge_table(
        lines,
        TOOL_REGISTRY,
        [
            (
                "Stars",
                lambda _k, _s, r: (
                    f"![Stars](https://img.shields.io/github/stars/{r}?{_BADGE})"
                ),
                "right",
            ),
            ("Last release", _last_release),
            (
                "Last commit",
                lambda _k, _s, r: (
                    f"![Last commit](https://img.shields.io/github/last-commit/{r}?{_BADGE})"
                ),
            ),
            (
                "Commits",
                lambda _k, _s, r: (
                    f"![Commits](https://img.shields.io/github/commit-activity/m/{r}?{_BADGE})"
                ),
                "right",
            ),
            (
                "Dependencies",
                lambda _k, _s, r: (
                    f"![Dependencies](https://img.shields.io/librariesio/github/{r}?{_BADGE})"
                ),
            ),
            (
                "Language",
                lambda _k, _s, r: (
                    f"![Language](https://img.shields.io/github/languages/top/{r}?style=flat-square)"
                ),
            ),
            (
                "License",
                lambda _k, _s, r: (
                    f"![License](https://img.shields.io/github/license/{r}?{_BADGE})"
                ),
            ),
        ],
    )

    return "\n".join(lines)


def _badge_table(
    lines: list[str],
    registry: dict,
    columns: list[tuple],
) -> None:
    """Append a badge comparison table for all tools.

    Each column is ``(name, badge_fn)`` or ``(name, badge_fn, align)``
    where align is ``"left"``, ``"center"`` (default), or ``"right"``.
    """
    headers = ["Tool"] + [c[0] for c in columns]
    colalign = tuple(["left"] + [c[2] if len(c) > 2 else "center" for c in columns])

    table_rows: list[list[str]] = []
    for key in sorted(registry):
        spec = registry[key]
        repo = _github_repo(spec.source_url)
        if not repo:
            continue
        label = spec.display_name or spec.name
        cells = [f"[{label}](#{label.lower()})"]
        for col in columns:
            fn = col[1]
            cells.append(fn(key, spec, repo))
        table_rows.append(cells)

    lines.append(
        render_table(
            table_rows,
            headers=headers,
            table_format=TableFormat.GITHUB,
            colalign=colalign,
        )
    )
    lines.append("")


def update_tool_runner() -> None:
    """Update ``tool-runner.md`` with summary table and per-tool detail sections."""
    tr_md = PROJECT_ROOT / "docs" / "tool-runner.md"
    replace_content(
        tr_md,
        tool_summary(),
        "<!-- tool-summary-start -->",
        "<!-- tool-summary-end -->",
    )
    replace_content(
        tr_md,
        tool_reference(),
        "<!-- tool-reference-start -->",
        "<!-- tool-reference-end -->",
    )


if __name__ == "__main__":
    update_tool_runner()
