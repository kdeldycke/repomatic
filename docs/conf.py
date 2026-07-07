from __future__ import annotations

from pathlib import Path

import tomllib  # type: ignore[import-not-found]
from docutils.nodes import container
from sphinxcontrib.mermaid import MermaidClassDiagram

project_path = Path(__file__).parent.parent.resolve()

# Fetch general information about the project from pyproject.toml.
toml_path = project_path / "pyproject.toml"
toml_config = tomllib.loads(toml_path.read_text(encoding="utf-8"))

# Redistribute pyproject.toml config to Sphinx.
project_id = toml_config["project"]["name"]
version = release = toml_config["project"]["version"]
author = ", ".join(a["name"] for a in toml_config["project"]["authors"])

# Title-case each word of the project ID.
project = " ".join(word.title() for word in project_id.split("-"))

# Addons.
extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.todo",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    # Adds a copy button to code blocks.
    "sphinx_copybutton",
    "sphinx_design",
    "sphinxext.opengraph",
    "myst_parser",
    "sphinx.ext.autosectionlabel",
    # myst_docstrings hooks autodoc-process-docstring at priority 400 (vs default
    # 500) so it always runs before sphinx_autodoc_typehints. Listing it first
    # makes the intent explicit; the extension enforces this at load time.
    "repomatic.myst_docstrings",
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
    "attrs_block",
    "attrs_inline",
    "colon_fence",
    "deflist",
    "fieldlist",
    "replacements",
    "smartquotes",
    "strikethrough",
    "tasklist",
]
# Allow ```mermaid``` without curly braces (```{mermaid}```).
# See: https://github.com/mgaitan/sphinxcontrib-mermaid/issues/99#issuecomment-2339587001
myst_fence_as_directive = ["mermaid"]

# Register every heading as a resolvable cross-reference target so in-page
# `[text](#anchor)` links resolve (and broken ones warn) at build time, making
# Sphinx the authority for internal anchors. The slug function is pinned to
# docutils' `make_id` so MyST anchors match the section IDs docutils already
# emits (`cache.dir` → `cache-dir`), keeping existing anchor URLs stable. This
# is also why the Lychee config skips intra-`docs/` fragment links: its
# GitHub-style slugger strips dots (`cache.dir` → `cachedir`) and cannot see
# these anchors, so it would false-positive links that resolve fine here.
myst_heading_anchors = 6
myst_heading_slug_func = "docutils.nodes.make_id"

mermaid_d3_zoom = True


class NoZoomClassDiagram(MermaidClassDiagram):
    """``autoclasstree`` with its diagram opted out of inline d3 zoom.

    ``mermaid_d3_zoom`` is all-or-nothing: it attaches wheel and drag handlers
    to every diagram's ``<svg>``, and sphinxcontrib-mermaid has no per-diagram
    opt-out while its fullscreen feature is active. On the tall class
    inheritance trees of the API sections, the wheel handler hijacks page
    scrolling. So this subclass wraps each tree in a marked container that
    ``custom.css`` targets to disable pointer events on the inline SVG: events
    then never reach the ``<svg>``, d3's handlers stay quiet, and the page
    scrolls normally. The fullscreen viewer clones the diagram outside the
    container, so its button and zoom still work there. Registered in
    :func:`setup` as an override of the upstream directive.
    """

    def run(self):
        return [container("", *super().run(), classes=["autoclasstree"])]


# Applies to every table carrying the (default) `sphinx-datatable` class:
# currently only the binaries catalog. An empty `order` preserves the CSV's
# newest-first row order on load instead of DataTables' default first-column
# ascending sort; the page length accommodates one release's worth of
# binaries per page with room to spare. The render callback appends a
# relative hint ("9 days ago") to the Released column (index 2 in
# repomatic.binaries_page.CSV_HEADERS) at display time only, so sorting and
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

exclude_patterns = ["_build", "_linkcheck", "Thumbs.db", ".DS_Store"]

nitpicky = True

# Concatenate class and __init__ docstrings.
autoclass_content = "both"
# Keep the same ordering as in original source code.
autodoc_member_order = "bysource"
always_use_bars_union = True

autodoc_default_options = {
    "members": True,
    "undoc-members": True,
    "show-inheritance": True,
}

# If true, `todo` and `todoList` produce output.
todo_include_todos = True

github_user = "kdeldycke"

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "click": (
        "https://click.palletsprojects.com",
        None,
    ),
    "click-extra": (
        "https://kdeldycke.github.io/click-extra",
        None,
    ),
}

# Prefix document path to section labels, to use:
# `path/to/file:heading` instead of just `heading`.
autosectionlabel_prefix_document = True

# Theme config.
html_theme = "furo"
html_title = project
html_logo = "assets/logo-square.svg"
html_favicon = "assets/favicon.svg"
html_theme_options = {
    "sidebar_hide_name": True,
    # Activates edit links.
    "source_repository": f"https://github.com/{github_user}/{project_id}",
    "source_branch": "main",
    "source_directory": "docs/",
    "announcement": (
        f"{project} works fine, but is"
        " <em>maintained by only one person</em>"
        " 😶‍🌫️.<br/>You can help if"
        " you <strong>"
        "<a class='reference external'"
        f" href='https://github.com/sponsors/"
        f"{github_user}'>"
        "purchase business support"
        " 🤝</a></strong> or"
        " <strong>"
        "<a class='reference external'"
        f" href='https://github.com/sponsors/"
        f"{github_user}'>"
        "sponsor the project"
        " 🫶</a></strong>."
    ),
}

# Linkcheck configuration.
# GitHub renders issue comments, README tab anchors and
# blob line anchors with JavaScript, so the linkcheck
# builder cannot find them in the static HTML.
linkcheck_anchors_ignore = [
    r"issuecomment-\d+",
    r"readme",
    r"L\d+",
]

# GitHub README anchors are JS-rendered and invisible to linkcheck.
linkcheck_anchors_ignore_for_url = [
    r"https://github\.com/",
    # star-history.com builds its chart and anchor with JavaScript.
    r"https://star-history\.com/",
]

# Some links time out the linkcheck bot intermittently (like biomejs.dev);
# retry before reporting them as broken.
linkcheck_retries = 3

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
ogp_image = "assets/banner-social-light.png"

# Footer content.
html_last_updated_fmt = "%Y-%m-%d"
copyright = f"{author} and contributors"
html_show_sphinx = False

html_static_path = ["_static"]
html_css_files = ["custom.css"]


def setup(app):
    """Sphinx extension entry point.

    Swaps sphinxcontrib-mermaid's ``autoclasstree`` directive for
    :class:`NoZoomClassDiagram`: conf.py is loaded as the last extension,
    so this registration wins.
    """
    app.add_directive("autoclasstree", NoZoomClassDiagram, override=True)
