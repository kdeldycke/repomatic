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

"""Render tests for Sphinx cross-references in the built documentation.

Build the docs once and assert against the real HTML that the generated
summary tables deep-link to the sections they describe, and that intersphinx
references to Click resolve to the upstream site. This catches drift the moment
a ``{click:config}`` or ``{click:tree}`` directive stops wiring its anchors, or
an ``intersphinx_mapping`` URL goes stale, neither of which a mock-based test
would notice.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# The docs dependency group requires Python >= 3.14 (see pyproject.toml
# [tool.uv] dependency-groups.docs). Only build under the same conditions the
# docs workflow uses: Linux, that Python floor, and uv available to provision
# the docs environment.
pytestmark = [
    pytest.mark.skipif(
        not sys.platform.startswith("linux"),
        reason="docs are built on Linux in CI",
    ),
    pytest.mark.skipif(
        sys.version_info < (3, 14),
        reason="docs dependency group requires Python >= 3.14",
    ),
    pytest.mark.skipif(
        shutil.which("uv") is None,
        reason="needs uv to build the docs",
    ),
    # Sphinx crashes with a FileNotFoundError on searchindex.js.tmp when
    # concurrent builds share the same output directory (sphinx-doc/sphinx#13702).
    # Force all tests in this module onto a single xdist worker.
    pytest.mark.xdist_group("sphinx"),
]

PROJECT_ROOT = Path(__file__).parent.parent


@pytest.fixture(scope="module")
def built_docs(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the HTML documentation once and return its output directory.

    Builds into a throwaway directory (rather than ``docs/_build``) so the run
    is hermetic and never clobbers a developer's local build.
    """
    out_dir = tmp_path_factory.mktemp("sphinx-html")
    subprocess.run(
        [
            "uv",
            "run",
            "--group",
            "docs",
            "sphinx-build",
            "--builder",
            "html",
            str(PROJECT_ROOT / "docs"),
            str(out_dir),
        ],
        check=True,
        cwd=PROJECT_ROOT,
    )
    return out_dir


def read_html(built_docs: Path, filename: str) -> str:
    """Read a built HTML page."""
    html_path = built_docs / filename
    assert html_path.exists(), f"HTML file not found: {html_path}"
    return html_path.read_text(encoding="UTF-8")


@pytest.mark.parametrize(
    ("page", "anchor"),
    (
        # `{click:config}` options, on the configuration reference.
        ("configuration.html", "abandoned-versions"),
        ("configuration.html", "changelog-archive-location"),
        ("configuration.html", "exclude"),
        ("configuration.html", "nuitka-enabled"),
        ("configuration.html", "nuitka-nofollow-imports"),
        # `{click:tree}` commands, on the CLI reference.
        ("cli.html", "repomatic-audit"),
        ("cli.html", "repomatic-cache"),
        ("cli.html", "repomatic-changelog"),
    ),
)
def test_summary_table_links_to_its_sections(built_docs, page, anchor):
    """Each generated summary table deep-links every row to its own section.

    Both directives render a table of contents above the detail sections, so
    a row whose target never materialized leaves a dead link on the published
    page rather than failing the build.
    """
    html = read_html(built_docs, page)
    assert f'id="{anchor}"' in html, f"{page}: missing section anchor for {anchor}"
    assert f'href="#{anchor}"' in html, f"{page}: summary table has no link to {anchor}"


def test_intersphinx_click_resolves(built_docs):
    """Click cross-references resolve to the upstream documentation site.

    Read from the module's own page rather than the CLI guide: a Click link is
    emitted by a rendered signature or docstring, and the generated API
    reference lives only on the `repomatic.*` pages. The guide keeps the
    `{click:tree}` output, which links within this site and would pass this
    assertion for the wrong reason.

    `--separate` gives each module its own page, so this names
    `repomatic.cli.main` rather than the `repomatic.cli` package page, which
    documents only the package's `__init__` and carries no signature.
    """
    html = read_html(built_docs, "repomatic.cli.main.html")
    assert "https://click.palletsprojects.com" in html, (
        "no intersphinx link to Click found; the mapping may be broken"
    )
