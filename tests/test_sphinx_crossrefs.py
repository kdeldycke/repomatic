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
summary tables deep-link to the sections they describe, that each changelog
release is anchored on its version, and that intersphinx references to Click
resolve to the upstream site. This catches drift the moment a
``{click:config}`` or ``{click:tree}`` directive stops wiring its anchors, or
an ``intersphinx_mapping`` URL goes stale, neither of which a mock-based test
would notice. The same build also shows whether a pickled page carries the
whole build environment.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from repomatic.changelog import Changelog

# The tests run on Linux alone, but collection imports the module everywhere.
if sys.platform != "win32":
    import fcntl

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
    # Under `--dist=loadgroup`, this marker keeps the tests of the module on one
    # xdist worker, so no other worker waits on the build. pytest-xdist 3.8.0
    # ignores it under pytest 9.2.0, and `built_docs` does not depend on it.
    pytest.mark.xdist_group("sphinx"),
]

PROJECT_ROOT = Path(__file__).parent.parent

DOCTREES_TO_ENVIRONMENT_RATIO = 10
"""Ceiling on the pickled pages of a build, in sizes of its pickled environment.

A clean build writes its pages in about two such sizes. A page that carries the
build environment adds a copy of it, so a leak across every API page takes the
ratio past a hundred.
"""


@pytest.fixture(scope="session")
def built_docs(tmp_path_factory: pytest.TempPathFactory, worker_id: str) -> Path:
    """Build the HTML documentation once per session and return its directory.

    Builds into a throwaway directory (rather than ``docs/_build``) so the run
    is hermetic and never clobbers a developer's local build. Reads in parallel,
    like the `docs.yaml` build these tests stand in for.

    Every xdist worker of the session reads the same build, whichever worker
    the scheduler gives the tests of this module. The build goes into the
    directory the workers share, under an exclusive lock: the first worker to
    take the lock builds, and the others wait for it. Two builds into one
    directory at the same time crash Sphinx on `searchindex.js.tmp`
    ([sphinx-doc/sphinx#13702](https://github.com/sphinx-doc/sphinx/issues/13702)).
    """
    basetemp = tmp_path_factory.getbasetemp()
    # The base directory of each worker sits in the one its session shares.
    shared = basetemp if worker_id == "master" else basetemp.parent
    out_dir = shared / "sphinx-html"
    built = shared / "sphinx-html.built"
    with (shared / "sphinx-html.lock").open("w", encoding="UTF-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if not built.exists():
            subprocess.run(
                [
                    "uv",
                    "run",
                    "--group",
                    "docs",
                    "sphinx-build",
                    "--builder",
                    "html",
                    "--jobs",
                    "auto",
                    str(PROJECT_ROOT / "docs"),
                    str(out_dir),
                ],
                check=True,
                cwd=PROJECT_ROOT,
            )
            built.touch()
    return out_dir


def read_html(built_docs: Path, filename: str) -> str:
    """Read a built HTML page."""
    html_path = built_docs / filename
    assert html_path.exists(), f"HTML file not found: {html_path}"
    return html_path.read_text(encoding="UTF-8")


@pytest.mark.parametrize(
    ("source", "page"),
    (
        ("changelog.md", "changelog.html"),
        ("docs/changelog-archive.md", "changelog-archive.html"),
    ),
)
def test_changelog_releases_are_anchored_on_their_version(built_docs, source, page):
    """Each release of the changelog has an anchor made from its version.

    docutils reduces a release heading to nothing and numbers its section from
    a per-page counter, so `#id40` names another release after each new one.
    `click_extra_release_anchors` in `docs/conf.py` gives each section an anchor
    like `#v1-4-2` instead.
    """
    changelog = Changelog((PROJECT_ROOT / source).read_text(encoding="UTF-8"))
    html = read_html(built_docs, page)
    missing = []
    for version in sorted(changelog.extract_all_version_headings()):
        # The release in preparation is anchored on the version it ships as.
        anchor = "v" + version.partition(".dev")[0].replace(".", "-")
        if f'<section id="{anchor}">' not in html:
            missing.append(version)
    assert not missing, f"{page}: releases with no version anchor: {missing}"


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

    Read from the API reference rather than the CLI guide: a Click link is
    emitted by a rendered signature or docstring, and the generated API
    reference lives only on the `repomatic.*` pages. The guide keeps the
    `{click:tree}` output, which links within this site and would pass this
    assertion for the wrong reason. Any API page qualifies, because which
    module renders a Click type changes as the code moves.
    """
    pages = sorted(built_docs.glob("repomatic.*.html"))
    assert pages, "no API reference page was built"
    assert any(
        "https://click.palletsprojects.com" in page.read_text(encoding="UTF-8")
        for page in pages
    ), "no intersphinx link to Click found; the mapping may be broken"


def test_pickled_pages_carry_no_build_environment(built_docs):
    """The pickled pages stay small next to the build environment.

    A node that keeps a throwaway document as its own pickles that document's
    settings with the page, build environment included. See
    `click_extra/sphinx/doctrees.py` in click-extra.
    """
    doctrees = built_docs / ".doctrees"
    environment = (doctrees / "environment.pickle").stat().st_size
    pages = sum(path.stat().st_size for path in doctrees.glob("*.doctree"))
    assert pages < DOCTREES_TO_ENVIRONMENT_RATIO * environment, (
        f"pickled pages take {pages} bytes, {pages / environment:.1f} times the"
        f" {environment}-byte environment"
    )
