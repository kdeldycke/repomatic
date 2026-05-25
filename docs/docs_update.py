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
import subprocess
from dataclasses import Field, fields
from pathlib import Path

import click
from click_extra import TableFormat, render_table

from repomatic.config import Config, config_full_descriptions, config_reference

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


def _option_slug(option: str) -> str:
    """Derive the auto-generated Sphinx heading ID for an option name.

    Mirrors docutils' ``make_id`` so the summary table can link to each
    section's natural anchor (e.g., ``"awesome-template.sync"`` →
    ``"awesome-template-sync"``).
    """
    slug = option.strip("`").lower()
    return re.sub(r"[^a-z0-9]+", "-", slug).strip("-")


def _toml_value(value: object) -> str | None:
    """Format a Python value as a TOML literal. Returns ``None`` when no
    sensible literal exists (``None`` defaults, opaque objects)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        if "\n" in value:
            return "'''\n" + value + "\n'''"
        return f'"{value}"'
    if isinstance(value, list):
        if not value:
            return "[]"
        items = [_toml_value(v) for v in value]
        if any(v is None for v in items):
            return None
        return "[" + ", ".join(items) + "]"  # type: ignore[arg-type]
    return None


def _toml_key(field_obj: Field) -> str:
    """Extract the TOML key from a dataclass field (honors ``config_path`` metadata)."""
    path = field_obj.metadata.get("click_extra.config_path")
    if path:
        return str(path)
    return field_obj.name.replace("_", "-")


def _config_examples() -> dict[str, str]:
    """Build a TOML example assignment for each ``[tool.repomatic]`` option."""
    schema = Config()
    examples: dict[str, str] = {}
    for f in fields(Config):
        sub = getattr(schema, f.name)
        if hasattr(sub, "__dataclass_fields__"):
            prefix = _toml_key(f)
            for sf in fields(type(sub)):
                key = f"{prefix}.{sf.name.replace('_', '-')}"
                value = _toml_value(getattr(sub, sf.name))
                if value is not None:
                    examples[key] = f"{key} = {value}"
        else:
            key = _toml_key(f)
            value = _toml_value(getattr(schema, f.name))
            if value is not None:
                examples[key] = f"{key} = {value}"
    return examples


def config_deflist() -> str:
    """Render the config reference as a summary table + per-option sections."""
    rows = config_reference()
    examples = _config_examples()
    full_descriptions = config_full_descriptions()
    lines: list[str] = []

    # Quick-reference table with deep links to each heading.
    table_rows = []
    for option, _ftype, default, description in rows:
        bare = option.strip("`")
        slug = _option_slug(option)
        # `description` is already the single-line summary; escape pipes
        # so they don't break GFM table rendering.
        short = description.replace("|", "\\|")
        table_rows.append([f"[`{bare}`](#{slug})", short, default])
    lines.append(
        render_table(
            table_rows,
            headers=["Option", "Description", "Default"],
            table_format=TableFormat.GITHUB,
        )
    )
    lines.append("")

    # Per-option heading sections: lead with the short description, then
    # type/default, then the rest of the docstring, and finally a TOML
    # example pinned to the field's default value.
    for option, ftype, default, description in rows:
        bare = option.strip("`")
        full = full_descriptions.get(bare, description)
        head, _, tail = full.partition("\n\n")
        short = " ".join(head.split())
        rest = tail.strip()

        lines.append(f"### {option}")
        lines.append("")
        if short:
            lines.append(short)
            lines.append("")
        lines.append(f"**Type:** `{ftype}` | **Default:** {default}")
        lines.append("")
        if rest:
            lines.append(rest)
            lines.append("")
        if bare in examples:
            lines.append("**Example:**")
            lines.append("")
            lines.append("```toml")
            lines.append("[tool.repomatic]")
            lines.append(examples[bare])
            lines.append("```")
            lines.append("")
    return "\n".join(lines)


def _command_anchor(cmd_path: list[str]) -> str:
    """Build a heading slug from a command path.

    ``["cache", "show"]`` becomes ``"repomatic-cache-show"``.
    """
    return "repomatic-" + "-".join(cmd_path)


def _click_run_block(args: list[str]) -> list[str]:
    """Render a ``{click:run}`` directive block invoking ``repomatic`` with ``args``.

    Sphinx executes the block at build time via ``click_extra.sphinx``, so the
    rendered help text is always live and ANSI-colored (``ansi-shell-session``
    Pygments lexer). The block assumes ``repomatic`` is already in the runner
    namespace — seed it once with a leading ``{click:source}`` ``:hide-source:``
    block (see :func:`cli_reference`).
    """
    args_repr = ", ".join(repr(a) for a in args)
    return [
        "```{click:run}",
        f"invoke(repomatic, args=[{args_repr}])",
        "```",
        "",
    ]


def cli_reference() -> str:
    """Generate CLI reference with a summary table and per-command sections."""
    from repomatic.cli import repomatic

    lines: list[str] = []

    # Collect all commands (top-level + subcommands of groups).
    entries: list[tuple[list[str], click.Command]] = []
    for name in sorted(repomatic.commands):
        cmd = repomatic.commands[name]
        entries.append(([name], cmd))
        if isinstance(cmd, click.Group):
            entries.extend(
                ([name, sub_name], cmd.commands[sub_name])
                for sub_name in sorted(cmd.commands)
            )

    # Summary table.
    table_rows = []
    for path, cmd in entries:
        anchor = _command_anchor(path)
        label = " ".join(path)
        desc = (cmd.get_short_help_str() or "").rstrip(".")
        table_rows.append([f"[`repomatic {label}`](#{anchor})", desc])
    lines.append(
        render_table(
            table_rows,
            headers=["Command", "Description"],
            table_format=TableFormat.GITHUB,
        )
    )
    lines.append("")

    # Seed the per-document runner namespace with `repomatic` so every
    # `{click:run}` block below can call `invoke(repomatic, ...)` without
    # re-importing. `{click:source}` persists top-level assignments to the
    # namespace; `{click:run}` does not (see `.claude/agents/sphinx-docs.md`
    # § Live-rendering over captured output).
    # No blank line between `:hide-source:` and the source line: mdformat-myst
    # rewrites the directive option as a YAML block, then the post-process in
    # `repomatic.tool_runner._fix_myst_directive_options` converts it back to
    # field-list syntax without a trailing blank. Emitting one here would
    # cause an `update-docs` ↔ `format-markdown` ping-pong on every CI run.
    lines.append("```{click:source}")
    lines.append(":hide-source:")
    lines.append("from repomatic.cli import repomatic")
    lines.append("```")
    lines.append("")

    lines.append("## Help screen")
    lines.append("")
    lines.extend(_click_run_block(["--help"]))

    # Per-command sections.
    for path, _cmd in entries:
        label = " ".join(path)
        depth = len(path)
        heading = "#" * (depth + 1)
        lines.append(f"{heading} `repomatic {label}`")
        lines.append("")
        lines.extend(_click_run_block([*path, "--help"]))

    return "\n".join(lines)


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
        name_link = f"[{label}]({spec.source_url})" if spec.source_url else label

        if spec.binary:
            install_type = "Binary"
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
        name_link = f"[{label}]({spec.source_url})" if spec.source_url else label

        lines.append(f"### {name_link}")
        lines.append("")

        lines.append(f"**Installed version:** `{spec.version}`")
        lines.append("")

        if spec.binary:
            install_type = "Binary (downloaded from GitHub Releases)"
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


def _tag_date(tag: str) -> str:
    """Return the ISO date (`YYYY-MM-DD`) of a git tag's commit."""
    proc = subprocess.run(
        ["git", "log", "-1", "--format=%as", tag],
        capture_output=True,
        encoding="utf-8",
        check=True,
        cwd=PROJECT_ROOT,
    )
    return proc.stdout.strip()


def _python_compat_groups() -> list[tuple[str, str, str, tuple[str, ...]]]:
    """Walk every `vX.Y.Z` git tag and group consecutive releases that
    declare the same set of `Programming Language :: Python :: 3.X`
    classifiers in `pyproject.toml`.

    :return: List of ``(first_tag, last_tag, first_date, python_versions)``
        per group, in chronological order. Tags without a `pyproject.toml`
        or without Python classifiers are skipped.
    """
    proc = subprocess.run(
        ["git", "tag", "--sort=version:refname"],
        capture_output=True,
        encoding="utf-8",
        check=True,
        cwd=PROJECT_ROOT,
    )
    tag_re = re.compile(r"^v\d+\.\d+\.\d+$")
    classifier_re = re.compile(r"Programming Language :: Python :: (3\.\d+)")

    def _sort_key(version: str) -> tuple[int, ...]:
        return tuple(int(p) for p in version.split("."))

    groups: list[list] = []
    for tag in proc.stdout.split():
        if not tag_re.match(tag):
            continue
        show = subprocess.run(
            ["git", "show", f"{tag}:pyproject.toml"],
            capture_output=True,
            encoding="utf-8",
            check=False,
            cwd=PROJECT_ROOT,
        )
        if show.returncode != 0:
            continue
        versions = tuple(sorted(set(classifier_re.findall(show.stdout)), key=_sort_key))
        if not versions:
            continue
        if groups and groups[-1][3] == versions:
            groups[-1][1] = tag
        else:
            groups.append([tag, tag, _tag_date(tag), versions])
    return [(first, last, date, vers) for first, last, date, vers in groups]


def python_compat_table() -> str:
    """Render the Python compatibility matrix as a GFM table.

    Newest releases on top so the supported-version streak progresses from
    the upper-left toward the lower-right corner over time.
    """
    groups = _python_compat_groups()
    if not groups:
        return ""

    def _sort_key(version: str) -> tuple[int, ...]:
        return tuple(int(p) for p in version.split("."))

    all_versions = sorted({v for _, _, _, vers in groups for v in vers}, key=_sort_key)

    def _range_label(first: str, last: str, *, is_latest: bool = False) -> str:
        """Render the version range label for a Python-compat group.

        For the most recent (open-ended) group, collapse the upper bound to
        the major-version wildcard (e.g., ``6.x``) so the label is stable
        across new minor releases that share the same Python compatibility.
        Closed historical groups keep the precise minor-version bounds.
        """
        first_minor = ".".join(first.lstrip("v").split(".")[:2])
        last_minor = ".".join(last.lstrip("v").split(".")[:2])
        if first_minor == last_minor:
            return f"`{first_minor}.x`"
        if is_latest:
            last_major = last.lstrip("v").split(".")[0]
            return f"`{first_minor}.x` → `{last_major}.x`"
        return f"`{first_minor}.x` → `{last_minor}.x`"

    rows = []
    for index, (first, last, date, vers) in enumerate(reversed(groups)):
        cells = ["✅" if v in vers else "❌" for v in all_versions]
        rows.append([_range_label(first, last, is_latest=index == 0), date, *cells])

    headers = ["`repomatic`", "Released", *(f"`{v}`" for v in all_versions)]
    colalign = ("left", "left", *("center",) * len(all_versions))
    return render_table(
        rows,
        headers=headers,
        table_format=TableFormat.GITHUB,
        colalign=colalign,
    )


def update_install() -> None:
    """Update ``install.md`` with the Python compatibility matrix."""
    install_md = PROJECT_ROOT / "docs" / "install.md"
    replace_content(
        install_md,
        python_compat_table(),
        "<!-- python-compat-start -->",
        "<!-- python-compat-end -->",
    )


def update_configuration() -> None:
    """Update ``configuration.md`` with the config reference list."""
    config_md = PROJECT_ROOT / "docs" / "configuration.md"
    replace_content(
        config_md,
        config_deflist(),
        "<!-- config-reference-start -->",
        "<!-- config-reference-end -->",
    )


def update_cli_parameters() -> None:
    """Update ``cli-parameters.md`` with the CLI reference."""
    cli_md = PROJECT_ROOT / "docs" / "cli.md"
    replace_content(
        cli_md,
        cli_reference(),
        "<!-- cli-reference-start -->",
        "<!-- cli-reference-end -->",
    )


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
    update_configuration()
    update_cli_parameters()
    update_install()
    update_tool_runner()
