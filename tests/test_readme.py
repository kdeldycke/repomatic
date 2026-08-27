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

"""Tests for documentation sync with code."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from repomatic.tooling.tool_registry import TOOL_REGISTRY, NativeFormat

REPO_ROOT = Path(__file__).parent.parent
CLI_MD = REPO_ROOT / "docs" / "cli.md"
CONFIGURATION_MD = REPO_ROOT / "docs" / "configuration.md"
README_MD = REPO_ROOT / "readme.md"
TOOL_RUNNER_MD = REPO_ROOT / "docs" / "tool-runner.md"

IMAGE_SRC_RE = re.compile(r"""<img[^>]*\ssrc=["']([^"']+)["']|!\[[^\]]*\]\(([^)\s]+)""")
"""Every image reference in a Markdown file, HTML tag or Markdown syntax."""


def _parse_tool_runner_table() -> dict[str, str]:
    """Parse the hand-written `[tool.X]` support table in docs/tool-runner.md.

    Returns a dict mapping display name to support column content, for rows
    that list either 'repomatic bridge' or 'Native' support. This table is
    hand-curated prose (unlike the registry-rendered `{python:render}`
    blocks), so it needs a conformance test against the registry.
    """
    tool_runner_text = TOOL_RUNNER_MD.read_text(encoding="UTF-8")
    result = {}
    for line in tool_runner_text.splitlines():
        if not line.startswith("| "):
            continue
        cols = [c.strip() for c in line.split("|")]
        if len(cols) < 5:
            continue
        tool_col = cols[1]
        support_col = cols[4]
        if "repomatic bridge" not in support_col and "Native" not in support_col:
            continue
        m = re.match(r"\[([^\]]+)\]", tool_col)
        if m:
            result[m.group(1)] = support_col
    return result


def test_readme_images_are_absolute() -> None:
    """Every image in `readme.md` must be an absolute URL.

    `pyproject.toml` sets `readme = "readme.md"`, so this file ships verbatim
    as the PyPI long description. GitHub resolves a relative path against the
    repository; PyPI has no such base and renders a broken image. The logo
    banner shipped relative and 404'd on every release page until `7.7.1`.

    Local paths stay correct on GitHub, which is what makes this invisible in
    review: the only place the breakage shows is the published project page.
    """
    offenders = [
        html_src or md_src
        for html_src, md_src in IMAGE_SRC_RE.findall(
            README_MD.read_text(encoding="UTF-8")
        )
        if not (html_src or md_src).startswith(("https://", "http://", "data:"))
    ]
    assert not offenders, (
        f"readme.md carries {len(offenders)} relative image reference(s): "
        f"{offenders}. readme.md is the PyPI long description, which cannot "
        "resolve repository-relative paths. Use the absolute "
        "https://raw.githubusercontent.com/… URL."
    )


def test_docs_cli_reference_uses_tree_directive() -> None:
    """docs/cli.md must render the CLI reference through ``{click:tree}``.

    The directive walks the live command tree at build time, so per-command
    coverage is guaranteed by construction. This canary only guards against
    the directive block being dropped from the page.
    """
    cli_text = CLI_MD.read_text(encoding="UTF-8")
    assert "```{click:tree} repomatic" in cli_text


def test_docs_config_reference_uses_config_directive() -> None:
    """docs/configuration.md must render the reference through ``{click:config}``.

    The directive documents the live `Config` schema at build time, from the
    same `schema_field_infos()` records `show-config` renders, so option
    coverage is guaranteed by construction. This canary only guards against
    the directive block being dropped from the page.
    """
    config_text = CONFIGURATION_MD.read_text(encoding="UTF-8")
    assert "```{click:config} repomatic" in config_text


def test_docs_bridge_table_covers_registry() -> None:
    """The tool-runner.md table must list every registry tool that supports translation.

    A tool supports ``[tool.X]`` translation when it does not natively read
    ``pyproject.toml`` (``reads_pyproject=False``) and can receive a translated
    config via either a ``config_flag``, a ``native_config_files`` target in a
    non-editorconfig format (editorconfig files are shared across tools and
    not suitable as single-tool bridge targets), or a ``FLAGS`` translation to
    CLI options (like Nuitka).
    """
    tool_table = _parse_tool_runner_table()
    documented = {
        name for name, support in tool_table.items() if "repomatic bridge" in support
    }

    bridgeable = {
        name
        for name, spec in TOOL_REGISTRY.items()
        if not spec.reads_pyproject
        and (
            spec.config_flag
            or spec.native_format is NativeFormat.FLAGS
            or (
                spec.native_config_files
                and spec.native_format is not NativeFormat.EDITORCONFIG
            )
        )
    }

    missing = bridgeable - documented
    assert not missing, (
        f"Tools with [tool.X] bridge support missing from tool-runner.md table: "
        f"{sorted(missing)}. Add them to the tool table in docs/tool-runner.md."
    )

    extra = documented - bridgeable
    assert not extra, (
        f"Tools listed in tool-runner.md bridge rows but not bridgeable in registry: "
        f"{sorted(extra)}. Remove them or update the registry."
    )


def test_docs_tip_table_covers_registry() -> None:
    """The tool-runner.md table must list every registry tool that natively reads pyproject.toml.

    Tools with ``reads_pyproject=True`` in the registry should appear in the
    tool table with 'Native' support. The table may also list non-registry tools
    (like coverage.py, pytest, uv) that the workflows use and that natively
    read ``[tool.*]`` sections.
    """
    tool_table = _parse_tool_runner_table()
    documented = {name for name, support in tool_table.items() if "Native" in support}

    native_readers = {
        name for name, spec in TOOL_REGISTRY.items() if spec.reads_pyproject
    }

    # Registry tool names may differ from display names (e.g., "bump-my-version"
    # is the package name while the registry key is also "bump-my-version"). Map
    # registry names to the display names used in the table.
    display_names = {
        TOOL_REGISTRY[name].package or TOOL_REGISTRY[name].name
        for name in native_readers
    }

    missing = display_names - documented
    assert not missing, (
        f"Tools with reads_pyproject=True missing from tool-runner.md table: "
        f"{sorted(missing)}. Add them to the tool table in docs/tool-runner.md."
    )


@pytest.mark.parametrize(
    "renderer",
    ["tool_summary", "tool_reference"],
)
def test_docs_tool_runner_uses_render_blocks(renderer: str) -> None:
    """docs/tool-runner.md must render its registry content live.

    Each `repomatic.tooling.tool_registry` renderer is embedded through a
    `{python:render}` block, so registry coverage is by construction. These
    canaries only guard against a block being dropped from the page.
    """
    tool_runner_text = TOOL_RUNNER_MD.read_text(encoding="UTF-8")
    assert f"from repomatic.tooling.tool_registry import {renderer}" in tool_runner_text


# Sphinx-apidoc module pages that carry importable modules. The data/, templates/,
# and awesome-template subpackages hold only bundled assets, so they have no
# module pages to keep in sync.
_MODULE_DOC_PAGES = {
    "repomatic.md": ("repomatic", REPO_ROOT / "repomatic"),
    "repomatic.github.md": ("repomatic.github", REPO_ROOT / "repomatic" / "github"),
    "tests.md": ("tests", REPO_ROOT / "tests"),
}

# sphinx-apidoc emits an automodule for every module except a package `__init__`
# (covered by the package page) and `__main__` (an entry-point shim it skips).
_APIDOC_SKIP = frozenset({"__init__", "__main__"})


@pytest.mark.parametrize("page", sorted(_MODULE_DOC_PAGES))
def test_every_module_has_a_docs_automodule(page: str) -> None:
    """Every module appears in its sphinx-apidoc doc page.

    These pages are write-once: ``click_extra.rst_to_myst`` preserves an
    existing ``.md``,
    so a module added after a page was last generated silently goes undocumented
    (Sphinx then warns "document isn't included in any toctree"). The
    ``update-docs`` drift test cannot catch this, since the generator never
    rewrites the page. To regenerate a page complete, delete it and run
    ``repomatic update-docs``.
    """
    prefix, root = _MODULE_DOC_PAGES[page]
    documented = set(
        re.findall(
            r"automodule:: ([\w.]+)",
            (REPO_ROOT / "docs" / page).read_text(encoding="UTF-8"),
        )
    )
    on_disk = {
        f"{prefix}.{p.stem}" for p in root.glob("*.py") if p.stem not in _APIDOC_SKIP
    }
    missing = sorted(on_disk - documented)
    assert not missing, (
        f"docs/{page} has no automodule entry for: {missing}. "
        f"Delete docs/{page} and run `repomatic update-docs` to regenerate it."
    )
