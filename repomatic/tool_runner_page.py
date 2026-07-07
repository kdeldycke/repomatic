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
"""Markdown renderers behind the `tool-runner.md` documentation page.

Each renderer turns `TOOL_REGISTRY` into a Markdown fragment consumed by a
`{python:render}` block in `docs/tool-runner.md`, so the page documents the
live registry on every Sphinx build (the sibling of `binaries_page`, which
renders `binaries.md` from release data). Nothing here is checked in: adding
a tool to the registry is all it takes for the page to cover it.
"""

from __future__ import annotations

import re

from click_extra import TableFormat, render_table

from .tool_runner import TOOL_REGISTRY, NativeFormat, ToolSpec

_PYPI_RELEASE_TOOLS = frozenset({"mdformat", "mypy"})
"""Tools that lack a usable GitHub "latest release" object.

mdformat has zero GitHub Releases; mypy only has pre-releases.
"""


def _github_repo(url: str | None) -> str | None:
    """Extract ``owner/repo`` from a GitHub URL."""
    m = re.match(r"https?://github\.com/([^/]+/[^/]+)", url or "")
    return m.group(1) if m else None


def _tool_badges(key: str, spec: ToolSpec) -> str:
    """Render the Stars and Last release badges of a tool's section.

    Stars need a GitHub repository; the release badge picks the tool's
    backend: GitHub release date when a repository exists, PyPI version for
    the {data}`_PYPI_RELEASE_TOOLS` exceptions, npm version for npm tools.
    Badges keep their default shields.io labels so they self-describe inline
    (in the retired comparison table, column headers played that role).
    """
    badges = []
    repo = _github_repo(spec.source_url)
    if repo:
        badges.append(
            f"![Stars](https://img.shields.io/github/stars/{repo}?style=flat-square)"
        )
    if key in _PYPI_RELEASE_TOOLS:
        badges.append(
            f"![Last release](https://img.shields.io/pypi/v/{spec.pypi_name}?style=flat-square)"
        )
    elif spec.npm is not None:
        badges.append(
            f"![Last release](https://img.shields.io/npm/v/{spec.package or spec.name}?style=flat-square)"
        )
    elif repo:
        badges.append(
            f"![Last release](https://img.shields.io/github/release-date/{repo}?style=flat-square)"
        )
    return " ".join(badges)


def tool_summary() -> str:
    """Render the summary table of all managed tools."""
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

    return render_table(
        rows,
        headers=["Tool", "Version", "Type", "Config discovery"],
        table_format=TableFormat.GITHUB,
        colalign=("left", "left", "left", "left"),
    )


def tool_reference() -> str:
    """Render the per-tool detail sections.

    The metadata of each section (version, install, config, flags, links) is
    generated from the registry; the trailing free-form prose comes from the
    spec's own `docs_notes` field, so hand-written examples and caveats live
    next to the spec they document.
    """
    lines: list[str] = []

    for key in sorted(TOOL_REGISTRY):
        spec = TOOL_REGISTRY[key]
        label = spec.display_name or spec.name
        name_link = f"[{label}]({spec.datasource_url})"

        lines.append(f"### {name_link}")
        lines.append("")

        badges = _tool_badges(key, spec)
        if badges:
            lines.append(badges)
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

        if spec.docs_notes:
            lines.append(spec.docs_notes)
            lines.append("")

    return "\n".join(lines)
