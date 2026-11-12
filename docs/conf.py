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

from __future__ import annotations

import tomllib
from datetime import date
from pathlib import Path

from click_extra.sphinx.announcement import years_since
from extra_platforms import is_github_ci

project_path = Path(__file__).parent.parent.resolve()

# Fetch general information about the project from pyproject.toml.
toml_path = project_path / "pyproject.toml"
toml_config = tomllib.loads(toml_path.read_text(encoding="UTF-8"))

# Redistribute pyproject.toml config to Sphinx.
project_id = toml_config["project"]["name"]
version = release = toml_config["project"]["version"]
author = ", ".join(a["name"] for a in toml_config["project"]["authors"])

# Canonical origin of the published site, read from the one place that declares
# it. The trailing slash is mandatory: Sphinx joins page paths onto this verbatim
# to build each `<link rel="canonical">`, and sphinx-sitemap concatenates it with
# every page link (its own normalization line discards its result), so a
# slash-less value yields sitemap URLs with the host glued to the first path
# segment.
docs_site_url = toml_config["project"]["urls"]["Documentation"].rstrip("/") + "/"

# Title-case each word of the project ID.
project = " ".join(word.title() for word in project_id.split("-"))

# GitHub account owning the project. Factored out so a rename touches one line
# instead of every URL built from it below (repository, sponsors, social card).
github_user = "kdeldycke"

# Whole years since `0.0.1` (2021-12-11), the oldest entry of the changelog, for
# the announcement banner below.
maintained_years = years_since(date(2021, 12, 11))

# Addons.
extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosectionlabel",
    "sphinx.ext.intersphinx",
    "sphinx.ext.todo",
    "sphinx.ext.viewcode",
    # Adds a copy button to code blocks.
    "sphinx_copybutton",
    "sphinx_design",
    # Emits sitemap.xml from html_baseurl, so crawlers get the API reference as
    # a list instead of having to discover it by following links.
    "sphinx_sitemap",
    "sphinxext.opengraph",
    "myst_parser",
    # myst_docstrings hooks autodoc-process-docstring at priority 400 (vs default
    # 500) so it always runs before sphinx_autodoc_typehints. Listing it first
    # makes the intent explicit; the extension enforces this at load time.
    "click_extra.sphinx.myst_docstrings",
    "sphinx_autodoc_typehints",
    "click_extra.sphinx",
    "sphinxcontrib.mermaid",
    # jQuery must be listed explicitly: sphinx-datatables only activates it
    # from a html-page-context callback, too late for the jquery.js static
    # file to be registered and copied, leaving `$` undefined at runtime.
    "sphinxcontrib.jquery",
    "sphinx_datatables",
]

# https://myst-parser.readthedocs.io/en/latest/syntax/optional.html
myst_enable_extensions = [
    # Render GitHub-style alerts (`> [!NOTE]`, `> [!IMPORTANT]`, ...) as
    # admonitions. Native to myst-parser since 5.1.0; click-extra's own
    # converter defers to it from that version on (see click_extra.sphinx).
    "alert",
    "replacements",
    "smartquotes",
]
# Allow ```mermaid``` without curly braces (```{mermaid}```).
# See: https://github.com/mgaitan/sphinxcontrib-mermaid/issues/99#issuecomment-2339587001
myst_fence_as_directive = ["mermaid"]

# Register every heading as a resolvable cross-reference target, so the build
# resolves each authored `[text](#anchor)` and `[text](page.md#anchor)` link.
# The slug function is pinned to docutils' `make_id`, so an authored anchor is
# the same string as the section ID the page publishes (`cache-dir` for
# `cache.dir`). GitHub, `lychee` and myst-parser's own default slug function
# write `cachedir` instead: `[tool.lychee]` says why it skips these links.
# `make_id` leaves nothing of a changelog release heading:
# `click_extra_release_anchors` below gives each one an anchor made from its
# version.
myst_heading_anchors = 6
myst_heading_slug_func = "docutils.nodes.make_id"

# Fail the build on a fragment link that lands nowhere. myst-parser resolves
# each one against what the build produced: heading slugs, `(target)=` labels,
# and the sections directives like `{click:config}` generate. It logs a
# `myst.xref_missing` warning for a link it cannot place, and ships the link as
# written anyway. Also fail the build on a docstring `{todo}` that no page
# renders: the todolist page cannot list it, and click-extra logs a
# `click_extra.todo_unrendered` warning for it. Sphinx exits 0 on warnings. CI
# fails on every warning through `[tool.repomatic] sphinx.fail-on-warning`, so
# this list is what still fails a local build run without `--fail-on-warning`.
click_extra_fail_on_warnings = ["click_extra.todo_unrendered", "myst.xref_missing"]

# Anchor each changelog release on its version, like `#v1-4-2`, in place of a
# positional `#idN`. See click-extra's click_extra/sphinx/changelog.py.
click_extra_release_anchors = True

# click_extra.sphinx keeps the wheel scrolling the page over the `autoclasstree`
# diagrams that this zoom would capture. See click_extra/sphinx/mermaid.py.
mermaid_d3_zoom = True

# Applies to every table carrying the (default) `sphinx-datatable` class:
# currently only the binaries catalog. An empty `order` preserves the CSV's
# newest-first row order on load instead of DataTables' default first-column
# ascending sort; the page length accommodates one release's worth of
# binaries per page with room to spare. The render callback appends a
# relative hint ("9 days ago") to the Released column (index 2 in
# repomatic.release.binaries_page.CSV_HEADERS) at display time only, so sorting and
# searching keep operating on the raw ISO dates and the generated CSV stays
# free of hints that would go stale between releases. Passed as a raw JS
# string because a JSON dict cannot carry the function. Raw string: the JS
# regex's backslashes are not Python escapes.
datatables_options = r"""
{
    "order": [],
    "pageLength": 25,
    "columnDefs": [
        {
            "targets": 2,
            "render": function (data, type, row) {
                if (type !== "display" || !data) {
                    return data;
                }
                // Cells arrive as rendered HTML (<p>2026-07-02</p>), so
                // extract the date instead of parsing the markup.
                const match = /\d{4}-\d{2}-\d{2}/.exec(data);
                if (!match) {
                    return data;
                }
                const days = Math.floor(
                    (Date.now() - Date.parse(match[0])) / 86400000);
                if (!isFinite(days)) {
                    return data;
                }
                let hint;
                if (days <= 0) {
                    hint = "today";
                } else if (days === 1) {
                    hint = "a day ago";
                } else if (days < 30) {
                    hint = days + " days ago";
                } else if (days < 350) {
                    const months = Math.round(days / 30.44);
                    hint = months === 1 ? "a month ago" : months + " months ago";
                } else {
                    const years = Math.round(days / 365.25);
                    hint = years === 1 ? "a year ago" : years + " years ago";
                }
                // Inject inside the paragraph so the hint stays on the
                // same line as the date.
                const label = " (" + hint + ")";
                return data.includes("</p>")
                    ? data.replace("</p>", label + "</p>")
                    : data + label;
            }
        }
    ]
}
"""

# Enable the `{click:run}`/`{click:source}` and `{python:*}` execution directives
# the CLI reference and tool-runner pages rely on. Disabled by default upstream
# since click-extra v7.15.0 because they execute arbitrary Python at build time;
# without this flag every directive reference logs an "Unknown directive" warning
# and the live blocks render empty.
click_extra_enable_exec_directives = True

# Capture the output of the CLI blocks through `sys.stdout`, in place of the
# stdout file descriptor that the runner defaults to. The stream that `prep_path`
# opens for `--output -` sits on that descriptor and writes past the fd capture,
# so its output never reaches the page.
click_extra_run_capture = "sys"

# Emit roff man pages for repomatic's own CLI into <outdir>/man/ on every
# HTML build. See click-extra's click_extra/sphinx/manpages.py. The same
# script target is mirrored in `[tool.repomatic] manpages.script` so the
# release tarball and the HTML docs site stay in lockstep.
click_extra_manpages = [
    {
        "script": "repomatic.cli.main:repomatic",
        "prog_name": "repomatic",
    },
]

# Resolve `:manpage:`repomatic(1)`` (and every subcommand page) to the HTML
# siblings the hook above emits.
manpages_url = "man/{page}.{section}.html"

exclude_patterns = ["_build", "_linkcheck", "html", "Thumbs.db", ".DS_Store"]

suppress_warnings = [
    # Example: "Document headings start at H2, not H1"
    # Explanation: readme.md starts at H2 because GitHub supplies the H1 from the
    # repository name, and docs/index.md includes it as it is.
    "myst.header",
]

nitpicky = True
nitpick_ignore = [
    # py-walk publishes no inventory to link this annotation to.
    ("py:class", "py_walk.models.parser.Parser"),
]

# Concatenate class and __init__ docstrings.
autoclass_content = "both"
# Keep the same ordering as in original source code.
autodoc_member_order = "bysource"

autodoc_default_options = {
    "members": True,
    "undoc-members": True,
    "show-inheritance": True,
}

# If true, `todo` and `todoList` produce output.
todo_include_todos = True

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    # Resolves the fixture annotations of the docs/tests.md pages, under both
    # their public names and the `_pytest.*` spellings Sphinx evaluates them to.
    "pytest": ("https://docs.pytest.org/en/stable", None),
    "click": (
        "https://click.palletsprojects.com",
        None,
    ),
    "click-extra": (
        "https://kdeldycke.github.io/click-extra",
        None,
    ),
    # The API pages name types of these three in signatures and docstrings, like
    # `extra_platforms.Platform` and `packaging.version.Version`.
    "extra-platforms": ("https://kdeldycke.github.io/extra-platforms", None),
    "packaging": ("https://packaging.pypa.io/en/stable", None),
    "pyproject-metadata": ("https://pep621.readthedocs.io/en/latest", None),
}

# Prefix document path to section labels, to use:
# `path/to/file:heading` instead of just `heading`.
autosectionlabel_prefix_document = True

# Where these pages are published, which is what makes Sphinx emit a
# `<link rel="canonical">` on each one. Without it there is no canonical tag at
# all, and a site reachable from more than one hostname has nothing telling a
# crawler which is the real one: the old `github.io` address answers every
# request with a redirect here, but a page served from a preview deployment or
# mirrored elsewhere would otherwise compete with its own original.
#
# No trailing `.html` anywhere in the emitted URLs is not something to fix
# here: Cloudflare Pages strips the extension itself and redirects `page.html`
# to `page`, so `html_baseurl` names the origin and the host settles the shape.
html_baseurl = docs_site_url

# Theme config.
html_theme = "furo"
html_title = project
html_favicon = "assets/favicon.svg"
html_theme_options = {
    # Sidebar logo. Furo renders this pair as .only-light/.only-dark images
    # driven by its own theme state, so the logo follows the toggle. A single
    # html_logo cannot: assets/logo-square.svg is light-only by convention, and
    # its near-black wordmark drops to a 1.24:1 contrast ratio on the dark theme.
    # Both files are 400x480 exports of that SVG, so the swap holds geometry.
    # Furo resolves them against _static/, hence their html_static_path entries.
    "light_logo": "logo-square-light.png",
    "dark_logo": "logo-square-dark.png",
    "sidebar_hide_name": True,
    # Activates edit links.
    "source_repository": f"https://github.com/{github_user}/{project_id}",
    "source_branch": "main",
    "source_directory": "docs/",
    # Sits atop every page, so it stays a single line and leads with the track
    # record rather than pleading scarcity: how thin the maintenance is says
    # nothing to a reader who has not yet decided the project is worth funding.
    # The age is recomputed at build time, so the claim never goes stale. Each
    # call to action opens on its emoji, which gives the eye two markers to land
    # on in a line that is otherwise unbroken prose.
    "announcement": (
        f"{project} has been maintained for {maintained_years}+ years, and is "
        "free to use. You can help if you "
        "<strong><a class='reference external' "
        f"href='https://github.com/sponsors/{github_user}'>"
        "🤝 purchase business support</a></strong> or "
        "<strong><a class='reference external' "
        f"href='https://github.com/sponsors/{github_user}'>"
        "🫶 sponsor the project</a></strong>."
    ),
}

# Linkcheck configuration.
# GitHub renders issue comments, README tab anchors and blob line anchors with
# JavaScript, so the linkcheck builder cannot find them in the static HTML:
# skip anchor checks for the whole host instead of stacking per-anchor patterns.
linkcheck_anchors_ignore_for_url = [
    r"https://github\.com/",
]

# Some links time out the linkcheck bot intermittently (like biomejs.dev);
# retry before reporting them as broken.
linkcheck_retries = 3

# click_extra.sphinx signs the github.com requests with the `GITHUB_TOKEN` that
# docs.yaml exposes on the linkcheck step, release downloads excepted: see
# click-extra's click_extra/sphinx/linkcheck.py. The binaries page links one
# download per binary ever released, and checking that many anonymously stays
# affordable: the endpoint answers a CDN redirect, not a rendered page, and a
# burst of them draws no throttling.
linkcheck_ignore = [
    # These sites return 403 to bots but are valid.
    r"https://docutils\.sourceforge\.io",
    r"https://www\.bitdefender\.com/submit/",
    # npmjs.com returns 403 to bots.
    r"https://www\.npmjs\.com/package/",
    # githubstatus.com returns 405 to HEAD requests from bots.
    r"https://www\.githubstatus\.com",
]

# OpenGraph / social previews.
# Absolute base URL for OpenGraph. sphinxext.opengraph resolves og:image
# against it, so social crawlers can follow the image instead of a relative
# path they cannot resolve.
ogp_site_url = docs_site_url
# Social-card image (og:image), named as a path relative to the site root:
# sphinxext.opengraph joins it onto ogp_site_url, so every page emits the same
# absolute URL whatever its depth. The card is the site's own copy rather than a
# GitHub raw hotlink, which keeps one origin and refreshes it on every deploy.
# Its html_static_path entry below is what puts the file there.
ogp_image = "_static/banner-social-light.png"

# Footer content.
html_last_updated_fmt = "%Y-%m-%d"
copyright = f"{author} and contributors"
html_show_sphinx = False

# Do not publish a copy of every source document under `_sources/`. Nothing links
# to it: no page this theme renders carries a source link, so the copy is tree
# that only a hand-typed URL could reach. The sources themselves are not lost,
# they are the repository. Both settings are named because they gate different
# halves of the feature, `html_copy_source` the files and `html_show_sourcelink`
# the link, and a theme reading only the second would otherwise offer a link to
# files that are no longer there.
html_copy_source = False
html_show_sourcelink = False

# Individual files are copied to the root of _static/, which is where Furo looks
# for the light_logo/dark_logo pair. `banner-social-light.png` is read by no
# theme and shown on no page: it is the og:image social crawlers fetch, and it
# needs an entry here for the site to serve it at all. Listing docs/assets/
# wholesale would drag every other asset along.
html_static_path = [
    "_static",
    "assets/banner-social-light.png",
    "assets/logo-square-dark.png",
    "assets/logo-square-light.png",
]
html_css_files = ["custom.css"]

# Copied verbatim to the root of the build, which is the directory
# `wrangler pages deploy` uploads and the only place each of these means
# anything: `html_static_path` would bury them under `_static/`, where a crawler
# never looks for `robots.txt`, neither host looks for `404.html`, and Cloudflare
# never reads `_redirects`. An entry here also keeps its leading underscore,
# unlike html_static_path, whose contents Sphinx re-roots. `_redirects` is inert
# on GitHub Pages.
html_extra_path = ["404.html", "_redirects", "robots.txt"]
# sphinx-sitemap defaults to a `{lang}{version}{link}` layout meant for sites
# publishing several translations or versions side by side. This one publishes a
# single tree, so anything but the bare link yields sitemap entries that 404.
sitemap_url_scheme = "{link}"
# The generated navigation pages hold no content a crawler should index.
sitemap_excludes = ["genindex.html", "py-modindex.html", "search.html"]

# Copy the commands of a shell-session block alone: `.gp` is the Pygments class
# of the prompt, `.go` the one of the output lines. The default only skips line
# numbers, so every `$ ` and every output line landed in the clipboard.
copybutton_exclude = ".linenos, .gp, .go"


def prune_build_artifacts(app, exception):
    """Delete what Sphinx leaves in the output tree that is not content.

    `.buildinfo` records the configuration hash an incremental build compares
    against, and `.buildinfo.bak` is the copy Sphinx keeps when that comparison
    fails. Neither is content, and no setting suppresses them: the HTML builder
    writes `.buildinfo` from a finish task unconditionally, so the only way to
    keep them out of a published tree is to remove them once it has. The
    Cloudflare deploy of this site would carry them: `wrangler pages deploy`
    uploads dot files, and skips only `.DS_Store`, `.git` and `.wrangler`.

    `_sources/` is created unconditionally too, before `html_copy_source` is
    ever consulted, so switching that off empties the directory without
    removing it. It is deleted here only while empty, which keeps this from
    quietly discarding the sources of a build that does want to publish them.

    The markers go in a GitHub Actions build alone. That build starts from an
    empty output directory, and its tree is the one that gets published. A
    local incremental build needs `.buildinfo`: when the file is missing,
    Sphinx `9.2.0` skips the configuration comparison and compares file times
    only, so a changed html-only setting like `html_title` leaves every page as
    it was.
    """
    if exception:
        return
    outdir = Path(app.outdir)
    if is_github_ci():
        for marker in (".buildinfo", ".buildinfo.bak"):
            (outdir / marker).unlink(missing_ok=True)
    sources = outdir / "_sources"
    if sources.is_dir() and not any(sources.iterdir()):
        sources.rmdir()


def setup(app):
    """Sphinx extension entry point.

    Removes what the build leaves in the output tree that is not content, with
    :func:`prune_build_artifacts`.
    """
    app.connect("build-finished", prune_build_artifacts)
