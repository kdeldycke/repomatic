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

"""Tests for :mod:`repomatic.templating` and the bundled templates."""

from __future__ import annotations

import inspect
import re
from importlib.resources import files
from pathlib import Path
from string import Template

import pytest
import yaml
from click_extra import Context

from repomatic import __version__
from repomatic.cli.main import repomatic
from repomatic.config import config_reference
from repomatic.deps.shippable import RELEASE_READY_SENTENCE
from repomatic.deps.sync import OPERATIONS_BY_NAME
from repomatic.frontmatter import split_frontmatter
from repomatic.release.protocol import (
    CHANGELOG_COMMIT_PREFIX,
    MANUAL_VERSION_BUMP_COMMIT_PREFIXES,
    VERSION_BUMP_COMMIT_PREFIXES,
)
from repomatic.templating import (
    FOOTER_TEMPLATE,
    PrTemplate,
    _parse_template,
    _unescape_dollars,
    get_template_names,
    render_template,
)
from tests.conftest import docs_heading_anchors

PROGRAMMATIC_TEMPLATES = frozenset({
    "available-admonition",
    "broken-links-issue",
    "development-warning",
    "generated-footer",
    "github-releases",
    "immutable-releases",
    "pr-metadata",
    "refresh-tip",
    "release-notes",
    "release-sync-report",
    "setup-guide",
    "setup-guide-branch-ruleset",
    "setup-guide-cloudflare-pages",
    "setup-guide-cloudflare-r2",
    "setup-guide-dependabot",
    "setup-guide-fork-pr-approval",
    "setup-guide-notifications-pat",
    "setup-guide-pages-source",
    "setup-guide-pypi-trusted-publisher",
    "setup-guide-sha-pinning-required",
    "setup-guide-token",
    "setup-guide-verify",
    "setup-guide-virustotal",
    "unavailable-admonition",
    "unsubscribe-report",
    "upgrade-invite",
    "yanked-admonition",
})
"""Templates rendered from Python code, not via the ``--template`` CLI flag."""

CLI_TEMPLATES = tuple(sorted(set(get_template_names()) - PROGRAMMATIC_TEMPLATES))
"""Templates that front a pull request or an issue `pr-sync` and `pr-body` open."""

REFERENCE_WORKFLOWS = (
    ".github/workflows/autofix.yaml",
    ".github/workflows/changelog.yaml",
    # The squashed-release command runs in the build lane, not the
    # release.yaml entry (which only calls the lanes and publishes).
    ".github/workflows/_release-build.yaml",
    # The scan-virustotal --template reference lives in the engine lane: that
    # job scans the assets a release publishes.
    ".github/workflows/_release-engine.yaml",
    # The sample-metrics --template reference lives in its own schedule-only
    # workflow, which `repomatic init` only materializes for a repository that
    # opted into an accumulating store.
    ".github/workflows/metrics.yaml",
    # Upstream-only bumpers, whose write domain exists only in this repository.
    ".github/workflows/self-maintenance.yaml",
)
"""Workflow files that reference PR body templates via ``--template``."""

COMMAND_TEMPLATES: dict[str, str] = {
    "bump-version": "bump-version",
    "squashed-release": "squashed-release",
}
"""Templates a command renders on its own, keyed by the command a workflow runs.

Each command opens its pull request or issue itself, so no `--template`
argument names the template in the workflow.
"""

WORKFLOWS_DOCS_URL = "https://repomatic.net/workflows"
"""Hosted workflows reference that template ``docs:`` fields deep-link into."""


# -- Parsing --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (r"no dollars here", "no dollars here"),
        (r"\$var", "$var"),
        (r"prefix \$x and \$y suffix", "prefix $x and $y suffix"),
        (r"already $fine", "already $fine"),
        (r"double \\$x", r"double \$x"),
    ],
)
def test_unescape_dollars(text, expected):
    r"""``\$`` sequences are converted back to ``$``."""
    assert _unescape_dollars(text) == expected


def test_parse_template_unescapes_body():
    r"""Body ``\$`` placeholders are unescaped at parse time."""
    raw = "---\ntitle: Test\n---\nHello \\$name"
    _meta, body = _parse_template(raw)
    assert "$name" in body
    assert r"\$" not in body


def test_parse_template_unescapes_no_frontmatter():
    r"""Templates without frontmatter also get ``\$`` unescaped."""
    raw = "Hello \\$world"
    _meta, body = _parse_template(raw)
    assert body == "Hello $world"


@pytest.mark.parametrize(
    ("declaration", "wants_footer"),
    [
        pytest.param("footer: false", False, id="yaml-boolean"),
        pytest.param('footer: "false"', False, id="quoted-string"),
        pytest.param("footer: true", True, id="explicit-opt-in"),
        pytest.param("title: Harvest", True, id="unset"),
    ],
)
def test_footer_opt_out_accepts_both_spellings(tmp_path, declaration, wants_footer):
    """`footer: false` opts out whether written as a YAML boolean or a string.

    The frontmatter is real YAML, so a bare `false` arrives as a boolean, but a
    downstream template may have quoted it. Both spell the same intent.
    """
    template = tmp_path / "harvest.md"
    template.write_text(f"---\n{declaration}\n---\nCrates packed.\n", encoding="UTF-8")
    assert PrTemplate.load(template).wants_footer is wants_footer
    rendered = render_template(template)
    assert ("Generated with" in rendered) is wants_footer


def test_get_template_names():
    """Every template file is discovered once, under its stem."""
    names = get_template_names()

    for expected in ("bump-version", "pr-metadata", "prepare-release", "fix-typos"):
        assert expected in names
    # Counted off the directory rather than written here: a literal turns
    # adding a template into a failure of a test that has nothing to say about
    # it, while still catching a name discovered twice or not at all.
    assert len(names) == len(set(names))
    assert set(names) == {
        item.name.removesuffix(".noformat").removesuffix(".md")
        for item in files("repomatic.templates").iterdir()
        if item.name.endswith((".md", ".md.noformat"))
    }


# -- Loading --------------------------------------------------------------------


def test_load_reads_the_frontmatter_once():
    """A bundled template loads once per process, as one shared instance."""
    assert PrTemplate.load("bump-version") is PrTemplate.load("bump-version")


def test_load_template_frontmatter():
    """Frontmatter is parsed correctly from a parameterized template."""
    template = PrTemplate.load("bump-version")

    assert template.name == "bump-version"
    assert template.args == ("version", "part")
    assert template.body.startswith("Merge this pull request")


def test_load_template_title_only_frontmatter():
    """Static templates have frontmatter with title but no args."""
    template = PrTemplate.load("fix-typos")

    assert "title" in template.meta
    assert template.args == ()
    assert template.body.startswith("> [!TIP]")


def test_load_template_external_file(tmp_path):
    """A {class}`~pathlib.Path` argument loads the template from disk."""
    template_path = tmp_path / "custom.md.noformat"
    template_path.write_text(
        "---\n"
        "args: [version]\n"
        'title: "Update package to v$version"\n'
        "---\n\n"
        "### Update\n\n"
        "Bumped to `$version`.\n",
        encoding="UTF-8",
    )

    template = PrTemplate.load(template_path)

    # The name is the file's stem, shorn of both extensions.
    assert template.name == "custom"
    assert template.args == ("version",)
    assert template.meta["title"] == "Update package to v$version"
    assert template.body.startswith("### Update")


def test_load_template_external_file_missing(tmp_path):
    """Pointing at a non-existent file raises FileNotFoundError."""
    missing = tmp_path / "does-not-exist.md"
    with pytest.raises(FileNotFoundError, match="does not exist"):
        PrTemplate.load(missing)


def test_load_unknown_bundled_template():
    """A name no bundled template carries raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError, match="papaya"):
        PrTemplate.load("papaya")


def test_render_template_external_file(tmp_path):
    """External templates render with substitution like packaged ones."""
    template_path = tmp_path / "external.md"
    template_path.write_text(
        "---\n"
        "args: [channel]\n"
        'title: "Update $channel package"\n'
        'commit_message: "Sync $channel"\n'
        "---\n\n"
        "### Update\n\nChannel: $channel.\n",
        encoding="UTF-8",
    )

    rendered = render_template(template_path, channel="Nix")
    assert "Channel: Nix." in rendered

    template = PrTemplate.load(template_path)
    assert template.title(channel="Nix") == "Update Nix package"
    assert template.commit_message(channel="Nix") == "Sync Nix"


def test_render_title_no_title_returns_empty(tmp_path):
    """A template without a `title` field renders to an empty string."""
    template_path = tmp_path / "bodyless.md"
    template_path.write_text("### Notice\n\nNothing to report.\n", encoding="UTF-8")

    template = PrTemplate.load(template_path)
    assert template.title() == ""
    # With neither `commit_message` nor `title`, the commit message is empty.
    assert template.commit_message() == ""


@pytest.mark.parametrize(
    ("labels", "expected"),
    (
        ("labels: [🍈 fruit, 🍋 citrus]", ("🍈 fruit", "🍋 citrus")),
        ("labels: 🍈 fruit", ("🍈 fruit",)),
        ("title: Harvest", ()),
    ),
)
def test_labels_accept_a_list_or_a_string(tmp_path, labels, expected):
    """The `labels:` key is a YAML list or a single string."""
    template_path = tmp_path / "harvest.md"
    template_path.write_text(f"---\n{labels}\n---\nCrates.\n", encoding="UTF-8")
    assert PrTemplate.load(template_path).labels == expected


@pytest.mark.parametrize(
    ("declaration", "expected"),
    (("draft: true", True), ('draft: "true"', True), ("title: Harvest", False)),
)
def test_draft_accepts_both_spellings(tmp_path, declaration, expected):
    """`draft: true` holds the pull request in draft, quoted or not."""
    template_path = tmp_path / "harvest.md"
    template_path.write_text(f"---\n{declaration}\n---\nCrates.\n", encoding="UTF-8")
    assert PrTemplate.load(template_path).draft is expected


def test_template_args_parameterized():
    """Parameterized templates report their required arguments."""
    assert PrTemplate.load("bump-version").args == ("version", "part")
    assert PrTemplate.load("prepare-release").args == (
        "changes_review",
        "dev_release_review",
        "release_readiness",
        "version",
    )


def test_template_args_static():
    """Static templates report no required arguments."""
    assert PrTemplate.load("fix-typos").args == ()
    assert PrTemplate.load("format-json").args == ()


def test_render_title_static():
    """Static templates have a literal title."""
    assert PrTemplate.load("fix-typos").title() == "Fix typos"
    assert PrTemplate.load("format-python").title() == "Format Python"


def test_render_title_parameterized():
    """Parameterized templates substitute variables in the title."""
    title = PrTemplate.load("bump-version").title(version="1.2.0", part="minor")
    assert title == "[changelog] Bump minor version to `v1.2.0`"

    title = PrTemplate.load("prepare-release").title(version="5.8.1")
    assert title == "Release `v5.8.1`"


def test_render_commit_message_falls_back_to_title():
    """Templates without explicit commit_message fall back to title."""
    assert PrTemplate.load("fix-typos").commit_message() == "Fix typos"
    assert PrTemplate.load("format-markdown").commit_message() == "Format Markdown"


def test_render_commit_message_explicit():
    """Templates without explicit commit_message fall back to title with backticks."""
    assert PrTemplate.load("format-pyproject").commit_message() == (
        "Format `pyproject.toml`"
    )
    assert PrTemplate.load("sync-gitignore").commit_message() == "Sync `.gitignore`"


def test_render_commit_message_parameterized():
    """Parameterized templates substitute variables in commit_message.

    The bump-version message must start with a MANUAL_VERSION_BUMP_COMMIT_PREFIXES
    member: that is the emission half of the invariant the workflow gates match
    against (see test_changelog_prefix_is_the_machinery_invariant).
    """
    msg = PrTemplate.load("bump-version").commit_message(version="1.2.0", part="minor")
    assert msg == "[changelog] Bump minor version to `v1.2.0`"
    assert any(msg.startswith(p) for p in MANUAL_VERSION_BUMP_COMMIT_PREFIXES)

    msg = PrTemplate.load("prepare-release").commit_message(version="5.8.1")
    assert msg == "Release `v5.8.1`"


# -- Footer ---------------------------------------------------------------------


def test_footer_template_opts_out_of_itself():
    """The footer renders alone: it never appends a second copy of itself."""
    footer = render_template(FOOTER_TEMPLATE, version="1.2.3")
    assert footer == (
        "🏭 Generated with [repomatic](https://github.com/kdeldycke/repomatic) `1.2.3`"
    )


def test_footer_appended_once_across_templates():
    """Several templates wanting the footer still share a single copy."""
    rendered = render_template(
        "broken-links-issue",
        "setup-guide-verify",
        lychee_section="",
        sphinx_section="",
        repo_url="https://example.com/orchard",
        repo_slug="kevin/orchard",
    )
    assert rendered.count("Generated with") == 1
    assert rendered.endswith(f"`{__version__}`\n")


@pytest.mark.parametrize("name", CLI_TEMPLATES)
def test_cli_templates_opt_out_of_the_footer(name):
    """A pull request template never carries the footer itself.

    The metadata block that closes every pull request body carries the
    attribution footer already, so a template rendering one too shows it
    twice.
    """
    assert PrTemplate.load(name).wants_footer is False


# -- Bundled template content ---------------------------------------------------


def test_render_bump_version():
    """Bump version template includes part, version, and merge instructions."""
    result = render_template("bump-version", version="1.2.0", part="minor")

    assert "bump the minor part" in result
    assert "## To bump version to `v1.2.0`" in result
    assert "Ready for review" in result
    assert "Rebase and merge" in result
    assert "bump-version" in result
    assert "changelog-yaml-jobs" in result


def test_render_prepare_release():
    """Prepare release template includes version, links, and caution admonition."""
    dev_review = (
        "1. Review the [`v5.8.1.dev0` GitHub release]"
        "(https://github.com/kdeldycke/repomatic/releases/tag/untagged-abc123)\n"
    )
    changes_review = (
        "1. Review the full changes: [`v5.8.0...main`]"
        "(https://github.com/kdeldycke/repomatic/compare/v5.8.0...main)\n"
    )
    result = render_template(
        "prepare-release",
        version="5.8.1",
        dev_release_review=dev_review,
        changes_review=changes_review,
        release_readiness=RELEASE_READY_SENTENCE,
    )

    assert "## How-to release `v5.8.1`" in result
    assert "`v5.8.1` tag on `main`" in result
    assert "`v5.8.1` release" in result
    # Both review steps render ahead of the merge instructions.
    assert "GitHub release]" in result
    assert "Review the full changes" in result
    assert result.index("GitHub release]") < result.index("Ready for review")
    assert result.index("Review the full changes") < result.index("Ready for review")
    # The template owns the squash-merge safeguard alone, at warning level;
    # `[!CAUTION]` is reserved for the readiness banner, which is absent here.
    assert "[!WARNING]" in result
    assert "[!CAUTION]" not in result
    assert "Squash and merge" in result
    assert "https://repomatic.net/workflows#freeze-and-unfreeze-commits" in result
    assert "PyPI" in result
    assert "prepare-release" in result
    assert "changelog-yaml-jobs" in result
    assert "release-yaml-jobs" in result


def test_render_prepare_release_without_review_steps():
    """Empty review fragments degrade the checklist to the merge instructions."""
    result = render_template(
        "prepare-release",
        version="5.8.1",
        dev_release_review="",
        changes_review="",
        release_readiness=RELEASE_READY_SENTENCE,
    )

    assert "## How-to release `v5.8.1`" in result
    assert "Ready for review" in result
    assert "Rebase and merge" in result
    # No dangling review links survive when the data is unavailable.
    assert "GitHub release]" not in result
    assert "Review the full changes" not in result


@pytest.mark.parametrize(
    ("template", "needles"),
    [
        # Templates whose job reads `[tool.repomatic]`: they list the keys
        # under a Configuration section. `test_template_config_options_are_real_keys`
        # separately proves each listed key exists and is anchored, so only the
        # roster is asserted here.
        pytest.param(
            "sync-bumpversion",
            ("## ⚙️ Configuration", "[tool.repomatic]", "bumpversion.sync"),
            id="sync-bumpversion",
        ),
        pytest.param(
            "sync-gitignore",
            (
                "## ⚙️ Configuration",
                "[tool.repomatic]",
                "gitignore.extra-categories",
                "gitignore.extra-content",
                "gitignore.location",
            ),
            id="sync-gitignore",
        ),
        pytest.param(
            "sync-mailmap",
            ("## ⚙️ Configuration", "[tool.repomatic]", "mailmap.sync"),
            id="sync-mailmap",
        ),
        pytest.param(
            "sync-repomatic-upgrade",
            (
                "## ⚙️ Configuration",
                "[tool.repomatic]",
                "minimum-release-age",
                "upstream-pin.sync",
            ),
            id="sync-repomatic-upgrade",
        ),
        pytest.param(
            "update-dep-graph",
            ("## ⚙️ Configuration", "[tool.repomatic]", "dependency-graph.output"),
            id="update-dep-graph",
        ),
        pytest.param(
            "update-docs",
            (
                "## ⚙️ Configuration",
                "[tool.repomatic]",
                "docs.apidoc-exclude",
                "docs.update-script",
            ),
            id="update-docs",
        ),
        # Templates whose job is driven by a third-party tool instead: they
        # point at that tool's own config table from a tip admonition.
        pytest.param("fix-typos", ("[!TIP]", "[tool.typos]"), id="fix-typos"),
        pytest.param("format-json", ("[!TIP]", "[tool.biome]"), id="format-json"),
        pytest.param(
            "format-markdown", ("[!TIP]", "[tool.mdformat]"), id="format-markdown"
        ),
        pytest.param(
            "format-pyproject",
            ("[!TIP]", "[tool.pyproject-fmt]"),
            id="format-pyproject",
        ),
        pytest.param(
            "format-python",
            ("[!TIP]", "[tool.ruff]", "[tool.autopep8]"),
            id="format-python",
        ),
    ],
)
def test_render_surfaces_the_config_surface(template: str, needles: tuple[str, ...]):
    """Each job's PR body points the reader at the knobs that job obeys."""
    result = render_template(template)
    for needle in needles:
        assert needle in result, f"{template} body does not mention {needle!r}"


def test_render_upgrade_title():
    """The upgrade pull request is titled after the release it adopts."""
    title = PrTemplate.load("sync-repomatic-upgrade").title(upgrade_version="7.15.0")
    assert title == "Upgrade `repomatic` to `v7.15.0`"


def test_render_upgrade_invite():
    """The invitation names both releases and hands over the launcher verbatim."""
    body = render_template(
        "upgrade-invite",
        previous="v7.14.0",
        adopted="v7.15.0",
        command="claude --model opus '/repomatic-upgrade v7.14.0 v7.15.0'",
    )
    assert "## 🧭 Upgrade review" in body
    assert "moves `repomatic` from `v7.14.0` to `v7.15.0`" in body
    assert (
        "```shell\nclaude --model opus '/repomatic-upgrade v7.14.0 v7.15.0'\n```"
        in body
    )
    assert "Generated with" not in body


def test_dependabot_step_names_the_downstream_updaters():
    """The setup step names the updaters a downstream repository runs.

    `sync-tool-versions` rewrites repomatic's own tool registry, so it runs
    upstream alone and replaces nothing Dependabot does downstream.
    """
    body = render_template(
        "setup-guide-dependabot",
        repo_url="https://example.com/orchard",
        repo_slug="kevin/orchard",
    )
    assert "`sync-workflow-pins`" in body
    assert "sync-tool-versions" not in body


# -- Markdown structure ---------------------------------------------------------


TABLE_ROW_PLACEHOLDER = re.compile(r"^\|.*\$\{?\w+", re.MULTILINE)
"""A placeholder inside a table row of a template body."""


@pytest.mark.parametrize("name", get_template_names())
def test_no_placeholder_splices_into_a_table_row(name):
    """A multi-line value spliced into one row's pipes breaks the table.

    Its first line takes the row's leading cell, and its last line gains the
    row's trailing ones. A table whose row count varies is rendered whole in
    Python, or its rows are spliced below the delimiter row, never inside one.
    """
    assert not TABLE_ROW_PLACEHOLDER.search(PrTemplate.load(name).body)


def test_release_sync_report_renders_a_well_formed_table():
    """Every row of the summary table has the cells its header declares."""
    rows = (
        "| \U0001f4e6 Total releases | 5 |\n"
        "| \u2705 In sync | 3 |\n"
        "| \U0001f504 Drifted | 2 |"
    )
    report = render_template(
        "release-sync-report", mode="dry-run", summary_rows=rows, details_section=""
    )
    table = [line for line in report.splitlines() if line.startswith("|")]
    assert len(table) == 5
    assert {line.count("|") for line in table} == {3}


# -- Template conformance -------------------------------------------------------


# Config-option references in PR body templates, written as
# ``- [`key`](…/configuration#anchor)`` bullets under "## ⚙️ Configuration".
CONFIG_OPTION_BULLET = re.compile(
    r"- \[`(?P<key>[^`]+)`\]"
    r"\(https://repomatic\.net/configuration#(?P<anchor>[^)]+)\)"
)

VALID_CONFIG_KEYS = frozenset(row[0].strip("`") for row in config_reference())
"""Every `[tool.repomatic]` key, dotted and nested-expanded, from the schema."""


@pytest.mark.parametrize("name", get_template_names())
def test_template_config_options_are_real_keys(name: str) -> None:
    """Every option a template lists is a real key, anchored and sorted.

    Each ``- [`key`](…/configuration#anchor)`` bullet must name a live
    `[tool.repomatic]` key, link to its matching anchor, and appear in
    alphabetical order, so a renamed or mistyped option cannot survive in a PR
    body template.
    """
    body = PrTemplate.load(name).body
    options = [(m["key"], m["anchor"]) for m in CONFIG_OPTION_BULLET.finditer(body)]

    for key, anchor in options:
        assert key in VALID_CONFIG_KEYS, (
            f"{name} lists unknown [tool.repomatic] option `{key}`"
        )
        assert anchor == key.replace(".", "-"), (
            f"{name} links `{key}` to #{anchor}, expected #{key.replace('.', '-')}"
        )

    keys = [key for key, _ in options]
    assert keys == sorted(keys), f"{name} config options are not sorted: {keys}"


SWITCH_KEY_RE = re.compile(r'exit_if_disabled\(ctx, "(?P<key>[\w.-]+)"\)')
"""The `[tool.repomatic]` key a command hands to `exit_if_disabled`."""


def test_templates_list_the_switch_of_their_command() -> None:
    """A pull request body that lists options names the one that stops it.

    A command gated by `exit_if_disabled` opens no pull request once its switch
    is off, so the option list of that pull request is where a maintainer looks
    for the switch. Only a template named after its command is read: `audit`
    and `sync-binaries` fill a template of another name. A template with no
    option list is left alone, like the `setup-guide` issue body.
    """
    templates = set(get_template_names())
    checked = 0
    missing = []
    ctx = Context(repomatic)
    for name in repomatic.list_commands(ctx):
        command = repomatic.get_command(ctx, name)
        if name not in templates or command is None or command.callback is None:
            continue
        switch = SWITCH_KEY_RE.search(inspect.getsource(command.callback))
        body = PrTemplate.load(name).body
        listed = {match["key"] for match in CONFIG_OPTION_BULLET.finditer(body)}
        if not switch or not listed:
            continue
        checked += 1
        if switch["key"] not in listed:
            missing.append(f"{name}: {switch['key']}")
    # An empty population would make the loop above prove nothing.
    assert checked
    assert not missing, (
        "These templates list the options of their command without the one that"
        f" turns it off: {missing}"
    )


def _collect_template_references() -> set[str]:
    """Scan reference workflows for the templates their steps render.

    A `--template <name>` argument names one, and so does a run of a command of
    {data}`COMMAND_TEMPLATES`. A `sync-deps --pr-sync` call renders the template
    of each updater it names, which shares its name.
    """
    repo_root = Path(__file__).resolve().parent.parent
    pattern = re.compile(r"--template\s+([\w-]+)")
    command = re.compile(r"repomatic\s+([\w-]+)")
    refs: set[str] = set()
    for rel_path in REFERENCE_WORKFLOWS:
        content = (repo_root / rel_path).read_text(encoding="UTF-8")
        refs.update(pattern.findall(content))
        refs.update(
            COMMAND_TEMPLATES[name]
            for name in command.findall(content)
            if name in COMMAND_TEMPLATES
        )
        for job in (yaml.safe_load(content).get("jobs") or {}).values():
            for step in job.get("steps") or ():
                run = step.get("run") or ""
                if "repomatic sync-deps" in run and "--pr-sync" in run:
                    refs.update(set(run.split()) & set(OPERATIONS_BY_NAME))
    return refs


def _template_package_items(
    *,
    exclude: frozenset[str] = frozenset(),
) -> list[tuple[str, str]]:
    """Return ``(filename, name)`` pairs for every file in the templates package.

    :param exclude: Template names to skip.
    """
    items = []
    for item in files("repomatic.templates").iterdir():
        # Only packaged template files count: skip directories and hidden or
        # dunder entries, since local tool droppings (like a `.DS_Store` file
        # or a `.claude/` directory) land in the same source directory.
        if not item.is_file():
            continue
        filename = getattr(item, "name", str(item))
        if filename.startswith((".", "__")):
            continue
        # .md.noformat files are renamed .md files hidden from mdformat.
        if filename.endswith(".md.noformat"):
            name = filename.removesuffix(".md.noformat")
        elif filename.endswith(".md"):
            name = filename.removesuffix(".md")
        else:
            name = filename
        if name in exclude:
            continue
        items.append((filename, name))
    return sorted(items)


@pytest.mark.parametrize(
    ("filename", "name"),
    _template_package_items(exclude=PROGRAMMATIC_TEMPLATES),
    ids=[pair[1] for pair in _template_package_items(exclude=PROGRAMMATIC_TEMPLATES)],
)
def test_template_file_policy(filename, name):
    """Each PR template file must be a valid ``.md`` file with correct frontmatter."""
    # Must be a markdown file. ``.md.noformat`` is a markdown template hidden
    # from mdformat (its ``string.Template`` placeholders confuse the list
    # parser); see :data:`repomatic.templating.TEMPLATE_SUFFIXES`.
    assert filename.endswith((".md", ".md.noformat")), (
        f"Template file {filename!r} is not a .md or .md.noformat file"
    )

    # Must parse without errors.
    template = PrTemplate.load(name)
    meta, body = template.meta, template.body

    # Frontmatter must have a non-empty 'title'.
    assert "title" in meta, f"Template {name!r} is missing 'title' in frontmatter"
    assert meta["title"], f"Template {name!r} has an empty 'title'"

    # If frontmatter has 'args', it must be a list. The placeholders it names
    # are matched against the text by test_placeholders_match_declared_args.
    if "args" in meta:
        assert isinstance(meta["args"], list), (
            f"Template {name!r} 'args' must be a list, got {type(meta['args'])}"
        )

    # PR body sections render as h2: no h3 heading may sneak in.
    assert "### " not in body, f"Template {name!r} body must use '##' section headings"

    # Every CLI template deep-links its job's section of the workflows
    # reference (surfaced as the metadata block's Documentation entry).
    # Validating the anchor against the in-tree headings keeps a docs reword
    # from orphaning it.
    docs = meta.get("docs")
    assert isinstance(docs, str) and docs.startswith(f"{WORKFLOWS_DOCS_URL}#"), (
        f"Template {name!r} must declare a 'docs' deep link into {WORKFLOWS_DOCS_URL}"
    )
    anchor = docs.partition("#")[2]
    assert anchor in docs_heading_anchors("workflows"), (
        f"Template {name!r} docs anchor {anchor!r} matches no docs/workflows.md heading"
    )


@pytest.mark.parametrize("name", get_template_names())
def test_placeholders_match_declared_args(name):
    """A template's `$placeholders` and its `args:` frontmatter name one set.

    `string.Template.get_identifiers()` lists the placeholders `substitute()`
    fills, bare or braced, so a placeholder the text uses but never declares
    fails here rather than as a `KeyError` in the job rendering it, and a
    declared arg no text references fails the same way. `is_valid()` rejects
    the stray `$` that `substitute()` would raise on.
    """
    template = PrTemplate.load(name)
    texts = [
        template.body,
        *(value for value in template.meta.values() if isinstance(value, str)),
    ]
    used: set[str] = set()
    for text in texts:
        parsed = Template(text)
        assert parsed.is_valid(), f"Template {name!r} holds an invalid placeholder"
        used.update(parsed.get_identifiers())
    assert used == set(template.args), (
        f"Template {name!r} declares {sorted(template.args)}"
        f" but its text renders {sorted(used)}"
    )


FRONTMATTER_KEY_ORDER = ["args", "title", "commit_message", "docs", "footer"]
"""Canonical ordering of keys within template frontmatter."""


@pytest.mark.parametrize(
    ("filename", "name"),
    _template_package_items(),
    ids=[pair[1] for pair in _template_package_items()],
)
def test_frontmatter_key_ordering(filename, name):
    """Frontmatter keys must follow the canonical order in `FRONTMATTER_KEY_ORDER`.

    `yaml.safe_load` preserves document order in the mapping it builds, so the
    parsed keys read back in the order the template spells them.
    """
    template_dir = files("repomatic.templates")
    raw = template_dir.joinpath(filename).read_text(encoding="UTF-8")
    meta, _body = split_frontmatter(raw)
    known = [k for k in meta if k in FRONTMATTER_KEY_ORDER]
    expected = [k for k in FRONTMATTER_KEY_ORDER if k in known]
    assert known == expected, (
        f"Template {name!r} frontmatter keys are {known}, expected order {expected}"
    )


@pytest.mark.parametrize(
    ("filename", "name"),
    _template_package_items(),
    ids=[pair[1] for pair in _template_package_items()],
)
def test_footer_opt_out_is_the_yaml_boolean(filename, name):
    """A bundled template spells the footer opt-out one way: `footer: false`."""
    template_dir = files("repomatic.templates")
    meta, _body = split_frontmatter(
        template_dir.joinpath(filename).read_text(encoding="UTF-8")
    )
    assert meta.get("footer", True) in (True, False), (
        f"Template {name!r} spells its footer opt-out {meta['footer']!r}"
    )


def test_templates_match_workflow_references():
    """Every CLI template must be referenced by ``--template`` in a workflow file."""
    template_names = set(CLI_TEMPLATES)
    workflow_refs = _collect_template_references()

    unreferenced = template_names - workflow_refs
    assert not unreferenced, "Templates not referenced in any workflow: " + ", ".join(
        sorted(unreferenced)
    )

    missing_templates = workflow_refs - template_names
    assert not missing_templates, (
        "Workflow --template references with no template file: "
        + ", ".join(sorted(missing_templates))
    )


# Commit-subject tokens that make GitHub Actions skip a workflow run. They
# match anywhere in the message, not just at the start, and a skipped required
# check sits in "Pending" forever rather than failing, blocking the merge. See
# docs/commit-messages.md.
CI_SKIP_TOKENS: frozenset[str] = frozenset({
    "[skip ci]",
    "[ci skip]",
    "[no ci]",
    "[skip actions]",
    "[actions skip]",
    "skip-checks:true",
    "skip-checks: true",
})


def _commit_message(name: str) -> str:
    """The commit message a template renders with every argument filled."""
    template = PrTemplate.load(name)
    return template.commit_message(**dict.fromkeys(template.args, "x"))


@pytest.mark.parametrize("name", get_template_names())
def test_template_commit_message_carries_no_ci_skip_token(name: str) -> None:
    """No generated commit message may silently skip CI.

    Every `sync-*`, `format-*` and `fix-*` job commits a message rendered
    from a template, in this repository and in every downstream one. A skip
    token anywhere in that message stops the run, and because the required
    check is then never reported, the pull request blocks on a "Pending"
    status instead of failing visibly.
    """
    lowered = _commit_message(name).lower()
    for token in CI_SKIP_TOKENS:
        assert token not in lowered, (
            f"template {name!r} renders a commit message containing {token!r}, "
            "which would skip CI for the job's own pull request"
        )


@pytest.mark.parametrize("name", get_template_names())
def test_template_commit_subject_claims_no_bracket_prefix(name: str) -> None:
    """A `[bracketed]` prefix is reserved for machine-parsed mechanisms.

    The one family templates may emit is
    {data}`repomatic.release.protocol.CHANGELOG_COMMIT_PREFIX`, which
    {data}`repomatic.release.protocol.VERSION_BUMP_COMMIT_PREFIXES` matches back
    (`bump-version` is the template that earns it). Any other bracket would
    either collide with that matcher or invent a label nothing reads. See
    docs/commit-messages.md.
    """
    message = _commit_message(name)
    subject = message.splitlines()[0] if message else ""
    assert not subject.startswith("[") or subject.startswith(CHANGELOG_COMMIT_PREFIX), (
        f"template {name!r} starts its commit subject with a bracket prefix "
        f"({subject!r}) other than {CHANGELOG_COMMIT_PREFIX!r}: brackets are "
        "reserved for parsed mechanisms"
    )


def test_only_the_changelog_prefix_is_bracketed() -> None:
    """The parsed-prefix set stays a single bracket family.

    `docs/commit-messages.md` tells readers that `[changelog] ` is the only
    bracket prefix any machine reads, carried by every version-machinery
    commit. This pins that claim to the code, so a second bracket namespace
    fails here and forces the documentation to follow.
    """
    for prefix in VERSION_BUMP_COMMIT_PREFIXES:
        assert prefix.startswith(CHANGELOG_COMMIT_PREFIX), (
            f"{prefix!r} opens a second bracket-prefixed commit contract: "
            "document it in docs/commit-messages.md before extending this "
            "assertion"
        )
