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

"""Tests for PR body generation."""

from __future__ import annotations

import re
from importlib.resources import files
from pathlib import Path

import pytest
from click.testing import CliRunner

from repomatic import __version__
from repomatic.cli import repomatic
from repomatic.config import config_reference
from repomatic.dep_sources import RELEASE_READY_SENTENCE
from repomatic.frontmatter import split_frontmatter
from repomatic.git_ops import (
    CHANGELOG_COMMIT_PREFIX,
    MANUAL_VERSION_BUMP_COMMIT_PREFIXES,
    VERSION_BUMP_COMMIT_PREFIXES,
)
from repomatic.github.actions import extract_workflow_filename
from repomatic.github.pr_body import (
    GITHUB_BODY_MAX_CHARS,
    _parse_template,
    _unescape_dollars,
    _utf16_len,
    build_pr_body,
    build_release_review_steps,
    demote_markdown_headings,
    fit_github_body,
    generate_pr_metadata_block,
    generate_refresh_tip,
    get_template_names,
    load_template,
    render_commit_message,
    render_template,
    render_title,
    template_args,
)
from repomatic.metadata import Metadata
from repomatic.version_sync import strip_dev_suffix

# Full set of GITHUB_* environment variables for testing.
GITHUB_ENV_VARS = {
    "GITHUB_RUN_ID": "123456789",
    "GITHUB_RUN_NUMBER": "42",
    "GITHUB_RUN_ATTEMPT": "1",
    "GITHUB_SERVER_URL": "https://github.com",
    "GITHUB_REPOSITORY": "owner/repo",
    "GITHUB_JOB": "autofix",
    "GITHUB_SHA": "abc12345def67890",
    "GITHUB_WORKFLOW_REF": "owner/repo/.github/workflows/autofix.yaml@refs/heads/main",
    "GITHUB_EVENT_NAME": "push",
    "GITHUB_ACTOR": "dependabot[bot]",
    "GITHUB_TRIGGERING_ACTOR": "dependabot[bot]",
    "GITHUB_REF_NAME": "main",
}


@pytest.fixture
def github_env(monkeypatch):
    """Populate the full `GITHUB_*` environment a workflow run would provide.

    `Metadata` reads these lazily, so every test that renders a metadata block
    or a refresh tip needs the whole set in place before it builds one.
    """
    for key, value in GITHUB_ENV_VARS.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize(
    ("workflow_ref", "expected"),
    [
        (
            "owner/repo/.github/workflows/autofix.yaml@refs/heads/main",
            "autofix.yaml",
        ),
        (
            "owner/repo/.github/workflows/release.yaml@refs/tags/v1.0.0",
            "release.yaml",
        ),
        ("", ""),
        ("just-a-filename.yaml", "just-a-filename.yaml"),
    ],
)
def test_extract_workflow_filename(workflow_ref, expected):
    """Extract workflow filename from various reference formats."""
    assert extract_workflow_filename(workflow_ref) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # The shallowest heading lands on the floor; deeper ones keep their
        # relative depth.
        ("## Harvest\n\n### Apples", "##### Harvest\n\n###### Apples"),
        # An h1-led body shifts further down to reach the same floor.
        ("# Orchard report", "##### Orchard report"),
        # Levels pushed past h6 (markdown's deepest) are clamped.
        ("## Harvest\n\n#### Apples", "##### Harvest\n\n###### Apples"),
        # Headings already at or below the floor are never promoted.
        ("##### Apples\n\n###### Pears", "##### Apples\n\n###### Pears"),
        # Up to 3 leading spaces still form a heading and are preserved.
        ("  ## Cellar", "  ##### Cellar"),
        # Fenced code blocks pass through untouched.
        (
            "## Recipe\n\n```bash\n# a comment\n```",
            "##### Recipe\n\n```bash\n# a comment\n```",
        ),
        # A 4-space indent is a code block; `#word` and `#123` are prose.
        ("    # indented code", "    # indented code"),
        ("#hashtag", "#hashtag"),
        # No headings at all.
        ("plain prose", "plain prose"),
        ("", ""),
    ],
)
def test_demote_markdown_headings(text, expected):
    """Headings are uniformly demoted so the shallowest lands on the floor."""
    assert demote_markdown_headings(text, floor=5) == expected


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
    rendered = render_template(template)
    assert ("Generated with" in rendered) is wants_footer


def test_generate_metadata_block_all_vars(github_env):
    """Metadata block includes all expected fields when env vars are set."""
    block = generate_pr_metadata_block(Metadata())

    assert "<details>" in block
    assert "<summary><code>Workflow metadata</code></summary>" in block
    assert "- **Trigger**: `push`" in block
    assert "- **Actor**: @dependabot[bot]" in block
    assert "- **Ref**: `main`" in block
    assert "**Commit**" in block
    assert "[`abc12345`]" in block
    assert "**Job**" in block
    assert "`autofix`" in block
    assert "**Workflow**" in block
    assert "[`autofix.yaml`]" in block
    assert "**Run**" in block
    assert "[#42.1]" in block
    assert "</details>" in block
    # The block is a list, not a table.
    assert "| Field | Value |" not in block
    # Same actor, no re-run entry; no docs URL, no documentation entry.
    assert "Re-run by" not in block
    assert "Documentation" not in block


def test_generate_metadata_block_rerun(github_env, monkeypatch):
    """Re-run by entry appears when triggering actor differs from actor."""
    monkeypatch.setenv("GITHUB_TRIGGERING_ACTOR", "admin-user")

    block = generate_pr_metadata_block(Metadata())

    assert "- **Re-run by**: @admin-user" in block


def test_generate_metadata_block_docs_entry(github_env):
    """The documentation deep link leads the list when a template provides one."""
    block = generate_pr_metadata_block(
        Metadata(),
        docs_url="https://example.test/workflows.html#pack-fruit",
        docs_name="pack-fruit",
    )

    assert (
        "- **Documentation**:"
        " [`pack-fruit`](https://example.test/workflows.html#pack-fruit)\n"
        "- **Trigger**:"
    ) in block
    # Without a label the raw URL renders as an autolink.
    unlabelled = generate_pr_metadata_block(
        Metadata(), docs_url="https://example.test/workflows.html#pack-fruit"
    )
    assert (
        "- **Documentation**: <https://example.test/workflows.html#pack-fruit>"
    ) in unlabelled


def test_generate_metadata_block_minimal_vars(monkeypatch):
    """Graceful degradation when most env vars are unset."""
    # Clear all GITHUB_ vars that might be set.
    for key in GITHUB_ENV_VARS:
        monkeypatch.delenv(key, raising=False)

    block = generate_pr_metadata_block(Metadata())

    # Should still produce a valid details block without crashing.
    assert "<details>" in block
    assert "</details>" in block
    assert "**Trigger**" in block


def test_generate_refresh_tip_with_workflow_ref(github_env):
    """Tip includes workflow dispatch URL when env vars are set."""
    tip = generate_refresh_tip(Metadata())

    assert "> [!IMPORTANT]" in tip
    assert "Run workflow" in tip
    assert "https://github.com/owner/repo/actions/workflows/autofix.yaml" in tip


def test_generate_refresh_tip_without_workflow_ref(monkeypatch):
    """Tip is empty when workflow ref is unavailable."""
    for key in GITHUB_ENV_VARS:
        monkeypatch.delenv(key, raising=False)

    assert generate_refresh_tip(Metadata()) == ""


def test_get_template_names():
    """All expected template names are discovered."""
    names = get_template_names()

    assert "bump-version" in names
    assert "prepare-release" in names
    assert "fix-typos" in names
    assert "format-json" in names
    assert "format-markdown" in names
    assert "format-pyproject" in names
    assert "format-python" in names
    assert "sync-bumpversion" in names
    assert "update-dep-graph" in names
    assert "update-docs" in names
    assert "sync-gitignore" in names
    assert "sync-mailmap" in names
    assert "sync-uv-lock" in names
    assert "sync-tool-versions" in names
    assert "sync-action-pins" in names
    assert "sync-workflow-pins" in names
    assert "sync-repomatic" in names
    assert "fix-changelog" in names
    assert "fix-vulnerable-deps" in names
    assert "pr-metadata" in names
    assert "refresh-tip" in names
    assert "setup-guide" in names
    assert "detect-squash-merge" in names
    assert "generated-footer" in names
    assert "github-releases" in names
    assert "immutable-releases" in names
    assert "release-notes" in names
    assert "available-admonition" in names
    assert "broken-links-issue" in names
    assert "development-warning" in names
    assert "release-sync-report" in names
    assert "unavailable-admonition" in names
    assert "unsubscribe-phase1" in names
    assert "unsubscribe-phase2" in names
    assert "setup-guide-fork-pr-approval" in names
    assert "setup-guide-sha-pinning-required" in names
    assert "setup-guide-notifications-pat" in names
    assert "setup-guide-virustotal" in names
    assert "yanked-admonition" in names
    assert "format-shell" in names
    assert "setup-guide-pypi-trusted-publisher" in names
    assert "sync-dep-sources" in names
    assert "sync-runner-images" in names
    # Counted off the directory rather than written here: a literal turns
    # adding a template into a failure of a test that has nothing to say about
    # it, while still catching a name discovered twice or not at all.
    assert len(names) == len(set(names))
    assert set(names) == {
        item.name.removesuffix(".noformat").removesuffix(".md")
        for item in files("repomatic.templates").iterdir()
        if item.name.endswith((".md", ".md.noformat"))
    }


def test_load_template_frontmatter():
    """Frontmatter is parsed correctly from a parameterized template."""
    meta, body = load_template("bump-version")

    assert meta["args"] == ["version", "part"]
    assert body.startswith("Ready to be merged")


def test_load_template_title_only_frontmatter():
    """Static templates have frontmatter with title but no args."""
    meta, body = load_template("fix-typos")

    assert "title" in meta
    assert "args" not in meta
    assert body.startswith("> [!TIP]")


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

    meta, body = load_template(template_path)

    assert meta["args"] == ["version"]
    assert meta["title"] == "Update package to v$version"
    assert body.startswith("### Update")


def test_load_template_external_file_missing(tmp_path):
    """Pointing at a non-existent file raises FileNotFoundError."""
    missing = tmp_path / "does-not-exist.md"
    with pytest.raises(FileNotFoundError, match="does not exist"):
        load_template(missing)


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

    assert render_title(template_path, channel="Nix") == "Update Nix package"
    assert render_commit_message(template_path, channel="Nix") == "Sync Nix"


def test_render_title_no_title_returns_empty(tmp_path):
    """A template without a `title` field renders to an empty string."""
    template_path = tmp_path / "bodyless.md"
    template_path.write_text("### Notice\n\nNothing to report.\n", encoding="UTF-8")

    assert render_title(template_path) == ""


def test_render_commit_message_no_title_returns_empty(tmp_path):
    """Templates with neither `commit_message` nor `title` render empty."""
    template_path = tmp_path / "bodyless.md"
    template_path.write_text("### Notice\n\nNothing to report.\n", encoding="UTF-8")

    assert render_commit_message(template_path) == ""


def test_template_args_parameterized():
    """Parameterized templates report their required arguments."""
    assert template_args("bump-version") == ["version", "part"]
    assert template_args("prepare-release") == [
        "changes_review",
        "dev_release_review",
        "release_readiness",
        "version",
    ]


def test_template_args_static():
    """Static templates report no required arguments."""
    assert template_args("fix-typos") == []
    assert template_args("format-json") == []


def test_render_title_static():
    """Static templates have a literal title."""
    assert render_title("fix-typos") == "Typo"
    assert render_title("format-python") == "Format Python"


def test_render_title_parameterized():
    """Parameterized templates substitute variables in the title."""
    title = render_title("bump-version", version="1.2.0", part="minor")
    assert title == "[changelog] Bump minor version to `v1.2.0`"

    title = render_title("prepare-release", version="5.8.1")
    assert title == "Release `v5.8.1`"


def test_render_commit_message_falls_back_to_title():
    """Templates without explicit commit_message fall back to title."""
    assert render_commit_message("fix-typos") == "Typo"
    assert render_commit_message("format-markdown") == "Format Markdown"


def test_render_commit_message_explicit():
    """Templates without explicit commit_message fall back to title with backticks."""
    msg = render_commit_message("format-pyproject")
    assert msg == "Format `pyproject.toml`"

    msg = render_commit_message("sync-gitignore")
    assert msg == "Sync `.gitignore`"


def test_render_commit_message_parameterized():
    """Parameterized templates substitute variables in commit_message.

    The bump-version message must start with a MANUAL_VERSION_BUMP_COMMIT_PREFIXES
    member: that is the emission half of the invariant the workflow gates match
    against (see test_changelog_prefix_is_the_machinery_invariant).
    """
    msg = render_commit_message("bump-version", version="1.2.0", part="minor")
    assert msg == "[changelog] Bump minor version to `v1.2.0`"
    assert any(msg.startswith(p) for p in MANUAL_VERSION_BUMP_COMMIT_PREFIXES)

    msg = render_commit_message("prepare-release", version="5.8.1")
    assert msg == "Release `v5.8.1`"


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
    assert "[!CAUTION]" in result
    assert "Squash and merge" in result
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


def test_build_release_review_steps(github_env, monkeypatch):
    """Review steps embed the draft dev URL and the previous-version compare link."""
    monkeypatch.setattr(
        "repomatic.github.pr_body.dev_release_url_and_previous_version",
        lambda repo_url, version: (
            "https://github.com/owner/repo/releases/tag/untagged-abc",
            "1.2.2",
        ),
    )

    dev_review, changes_review = build_release_review_steps(Metadata(), "1.2.3")

    assert dev_review == (
        "1. Review the [`v1.2.3.dev0` GitHub release]"
        "(https://github.com/owner/repo/releases/tag/untagged-abc)\n"
    )
    assert changes_review == (
        "1. Review the full changes: [`v1.2.2...main`]"
        "(https://github.com/owner/repo/compare/v1.2.2...main)\n"
    )


def test_build_release_review_steps_partial(github_env, monkeypatch):
    """Each step is omitted independently when its release data is missing."""
    # Dev draft absent, previous version present.
    monkeypatch.setattr(
        "repomatic.github.pr_body.dev_release_url_and_previous_version",
        lambda repo_url, version: (None, "1.2.2"),
    )

    dev_review, changes_review = build_release_review_steps(Metadata(), "1.2.3")

    assert dev_review == ""
    assert "v1.2.2...main" in changes_review


def test_build_release_review_steps_unavailable(github_env, monkeypatch):
    """Both steps collapse to empty strings when the lookup returns nothing."""
    monkeypatch.setattr(
        "repomatic.github.pr_body.dev_release_url_and_previous_version",
        lambda repo_url, version: (None, None),
    )

    assert build_release_review_steps(Metadata(), "1.2.3") == ("", "")


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


# Config-option references in PR body templates, written as
# ``- [`key`](…/configuration.html#anchor)`` bullets under "## ⚙️ Configuration".
CONFIG_OPTION_BULLET = re.compile(
    r"- \[`(?P<key>[^`]+)`\]"
    r"\(https://kdeldycke\.github\.io/repomatic/configuration\.html#(?P<anchor>[^)]+)\)"
)

VALID_CONFIG_KEYS = frozenset(row[0].strip("`") for row in config_reference())
"""Every `[tool.repomatic]` key, dotted and nested-expanded, from the schema."""


@pytest.mark.parametrize("name", get_template_names())
def test_template_config_options_are_real_keys(name: str) -> None:
    """Every option a template lists is a real key, anchored and sorted.

    Each ``- [`key`](…/configuration.html#anchor)`` bullet must name a live
    `[tool.repomatic]` key, link to its matching anchor, and appear in
    alphabetical order. Guards against a renamed or mistyped option silently
    surviving in a PR body template (the lone unenforced `workflow.sync` was
    one such drift).
    """
    _, body = load_template(name)
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


FAKE_FOOTER = (
    "<details>metadata</details>\n\n"
    "***\n\n"
    "Generated with [repomatic](https://github.com/kdeldycke/repomatic)"
)
"""Simulates ``generate_pr_metadata_block(Metadata())`` output for unit tests."""


def test_build_pr_body_with_prefix(monkeypatch):
    """Prefix is prepended with triple newline separator."""
    for key in GITHUB_ENV_VARS:
        monkeypatch.delenv(key, raising=False)

    result = build_pr_body("Fix formatting issues.", FAKE_FOOTER)

    assert result.startswith("Fix formatting issues.")
    assert "Generated with [repomatic]" in result
    assert "\n\n\n<details>metadata</details>" in result


def test_build_pr_body_with_tip(github_env):
    """Tip is inserted between prefix and footer when env vars are set."""
    result = build_pr_body(
        "Description.", FAKE_FOOTER, refresh_tip=generate_refresh_tip(Metadata())
    )

    assert result.startswith("Description.")
    assert "> [!IMPORTANT]" in result
    assert "Generated with [repomatic]" in result
    assert result.index("Description.") < result.index("[!IMPORTANT]")
    assert result.index("[!IMPORTANT]") < result.index("Generated with")
    assert result.index("<details>") < result.index("Generated with")


def test_build_pr_body_empty_prefix(monkeypatch):
    """Empty prefix without tip still includes footer and metadata."""
    for key in GITHUB_ENV_VARS:
        monkeypatch.delenv(key, raising=False)

    result = build_pr_body("", FAKE_FOOTER)

    assert "Generated with [repomatic]" in result
    assert "<details>metadata</details>" in result


def test_build_pr_body_trims_oversized_prefix(github_env):
    """An oversized prefix is trimmed so the tail always survives.

    GitHub and create-pull-request truncate oversized bodies from the end,
    which silently drops the refresh tip, metadata block, and attribution
    footer (the fate of huge `sync-uv-lock` tables). The prefix is trimmed
    instead, on line boundaries, with a caution admonition marking the cut.
    """
    # Astral-plane emoji make the UTF-16 length diverge from the code-point
    # length, matching real dependency tables (🆙 heading, 🆕/🗑️ labels).
    row = "| [papaya](https://pypi.org/project/papaya/) | `1.0` → `2.0` 🆙 |"
    prefix = "\n".join([row] * 3000)
    result = build_pr_body(
        prefix, FAKE_FOOTER, refresh_tip=generate_refresh_tip(Metadata())
    )

    assert _utf16_len(result) <= GITHUB_BODY_MAX_CHARS
    assert result.endswith(FAKE_FOOTER)
    assert "> Report truncated to fit GitHub's body size limit." in result
    # The refresh tip survives too.
    assert "> [!IMPORTANT]" in result
    # The cut runs on line boundaries: rows are dropped, never split, and the
    # notice replaces them right where the table ends.
    assert result.startswith(row)
    assert 0 < result.count(row) < 3000
    assert f"{row}\n\n\n> [!CAUTION]" in result


def test_build_pr_body_under_limit_untouched(github_env):
    """A body under the limit is returned without any truncation notice."""
    result = build_pr_body("Small report.", FAKE_FOOTER)

    assert result.startswith("Small report.")
    assert "> Report truncated" not in result


def test_fit_github_body_trims_keeping_footer():
    """An oversized issue body is trimmed above the footer, which survives.

    Unlike PR bodies (silently truncated by create-pull-request), oversized
    issue bodies make `gh issue create` and `gh issue edit` fail outright, so
    the guard rewrites the body to fit before it reaches the API.
    """
    line = "- [https://example.com/papaya](https://example.com/papaya) 🍈 404"
    body = render_template(
        "broken-links-issue",
        lychee_section="## Lychee\n\n" + "\n".join([line] * 2000),
        sphinx_section="## Sphinx linkcheck\n\nNo broken links found.",
    )
    assert _utf16_len(body) > GITHUB_BODY_MAX_CHARS

    fitted = fit_github_body(body)

    assert _utf16_len(fitted) <= GITHUB_BODY_MAX_CHARS
    # The attribution footer still closes the body.
    assert fitted.endswith(
        "Generated with [repomatic](https://github.com/kdeldycke/repomatic)"
        f" `{__version__}`\n"
    )
    # The notice marks the cut, between the kept rows and the footer.
    assert 0 < fitted.count(line) < 2000
    assert fitted.index(line) < fitted.index("> [!CAUTION]")
    assert fitted.index("> [!CAUTION]") < fitted.index("Generated with")


def test_fit_github_body_small_unchanged():
    """A body under the limit passes through byte-for-byte."""
    body = render_template(
        "broken-links-issue",
        lychee_section="## Lychee\n\nNo broken links found.",
        sphinx_section="",
    )
    assert fit_github_body(body) == body


def test_fit_github_body_without_footer_still_fits():
    """A footerless oversized body is trimmed too, without inventing a footer."""
    body = "\n".join(["| papaya | 🆙 |"] * 9000)

    fitted = fit_github_body(body)

    assert _utf16_len(fitted) <= GITHUB_BODY_MAX_CHARS
    assert "> Report truncated to fit GitHub's body size limit." in fitted
    assert "Generated with" not in fitted


# ---------------------------------------------------------------------------
# Template file policy validation
# ---------------------------------------------------------------------------

REFERENCE_WORKFLOWS = (
    ".github/workflows/autofix.yaml",
    ".github/workflows/changelog.yaml",
    # The detect-squash-merge --template reference lives in the build lane, not
    # the release.yaml entry (which only calls the lanes and publishes).
    ".github/workflows/_release-build.yaml",
    # The update-dep-graph --template reference lives in the engine lane: a
    # release push is that job's only firing moment, and autofix.yaml (its
    # former home) skips version-bump pushes wholesale.
    ".github/workflows/_release-engine.yaml",
    # Upstream-only bumpers, whose write domain exists only in this repository.
    ".github/workflows/self-maintenance.yaml",
)
"""Workflow files that reference PR body templates via ``--template``."""

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
    "unsubscribe-phase1",
    "unsubscribe-phase2",
    "yanked-admonition",
})
"""Templates rendered from Python code, not via the ``--template`` CLI flag."""


WORKFLOWS_DOCS_URL = "https://kdeldycke.github.io/repomatic/workflows.html"
"""Hosted workflows reference that template ``docs:`` fields deep-link into."""


def _workflows_heading_anchors() -> set[str]:
    """Anchors available in ``docs/workflows.md``: heading slugs and explicit targets.

    Heading slugs follow the docutils section-id algorithm (lowercase, every
    non-alphanumeric run collapsed to one hyphen, trimmed), which is what the
    published Sphinx page exposes as ``id=`` attributes.
    """
    repo_root = Path(__file__).resolve().parent.parent
    text = (repo_root / "docs" / "workflows.md").read_text(encoding="UTF-8")
    anchors = set()
    for line in text.splitlines():
        if re.match(r"#{1,6} ", line):
            title = line.lstrip("#").strip()
            # Keep the text of markdown links, drop their targets.
            title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", title)
            anchors.add(re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-"))
        else:
            explicit = re.fullmatch(r"\(([\w-]+)\)=", line.strip())
            if explicit:
                anchors.add(explicit.group(1))
    return anchors


def _collect_template_references() -> set[str]:
    """Scan reference workflows for all ``--template <name>`` arguments."""
    repo_root = Path(__file__).resolve().parent.parent
    pattern = re.compile(r"--template\s+([\w-]+)")
    refs: set[str] = set()
    for rel_path in REFERENCE_WORKFLOWS:
        content = (repo_root / rel_path).read_text(encoding="UTF-8")
        refs.update(pattern.findall(content))
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
    # parser); see :func:`load_template`.
    assert filename.endswith((".md", ".md.noformat")), (
        f"Template file {filename!r} is not a .md or .md.noformat file"
    )

    # Must parse without errors.
    meta, body = load_template(name)

    # Frontmatter must have a non-empty 'title'.
    assert "title" in meta, f"Template {name!r} is missing 'title' in frontmatter"
    assert meta["title"], f"Template {name!r} has an empty 'title'"

    # If frontmatter has 'args', it must be a list with matching $variables in body
    # or any other frontmatter field.
    if "args" in meta:
        assert isinstance(meta["args"], list), (
            f"Template {name!r} 'args' must be a list, got {type(meta['args'])}"
        )
        # Collect all string-valued frontmatter fields for variable reference checks.
        frontmatter_text = " ".join(
            v for k, v in meta.items() if k != "args" and isinstance(v, str)
        )
        for arg in meta["args"]:
            # Both the bare (`$arg`) and braced (`${arg}`) substitution forms
            # count as a reference; the braced form is needed when a
            # placeholder abuts an identifier character (e.g. `${step}1.`).
            markers = (f"${arg}", "${" + arg + "}")
            haystack = body + " " + frontmatter_text
            assert any(marker in haystack for marker in markers), (
                f"Template {name!r} declares arg {arg!r}"
                f" but neither body nor frontmatter references {markers[0]}"
            )

    # PR body sections render as h2: no h3 heading (like the retired
    # per-template "### Description" boilerplate) may sneak back in.
    assert "### " not in body, f"Template {name!r} body must use '##' section headings"

    # Every CLI template deep-links its job's section of the workflows
    # reference (surfaced as the metadata block's Documentation entry, in
    # place of the retired description section). Validating the anchor
    # against the in-tree headings keeps a docs reword from orphaning it.
    docs = meta.get("docs")
    assert isinstance(docs, str) and docs.startswith(f"{WORKFLOWS_DOCS_URL}#"), (
        f"Template {name!r} must declare a 'docs' deep link into {WORKFLOWS_DOCS_URL}"
    )
    anchor = docs.partition("#")[2]
    assert anchor in _workflows_heading_anchors(), (
        f"Template {name!r} docs anchor {anchor!r} matches no docs/workflows.md heading"
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
    ("version", "expected"),
    [
        ("1.2.3", "1.2.3"),
        ("1.2.3.dev0", "1.2.3"),
        ("5.9.2.dev0", "5.9.2"),
        ("0.1.0.dev42", "0.1.0"),
        # The match is end-anchored, so a PEP 440 local segment after the
        # `.devN` leaves the whole version untouched.
        ("1.2.3.dev0+abc123", "1.2.3.dev0+abc123"),
    ],
)
def test_version_dev_suffix_stripping(version, expected):
    """The `.devN` suffix is stripped from auto-detected versions.

    Exercises the shared helper both `pr-body --version auto` and
    `repomatic init`'s default pin route through, rather than restating its
    regex here: a test that spells the pattern itself passes no matter what
    the callers do.
    """
    assert strip_dev_suffix(version) == expected


def test_templates_match_workflow_references():
    """Every CLI template must be referenced by ``--template`` in a workflow file."""
    template_names = set(get_template_names()) - PROGRAMMATIC_TEMPLATES
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


# ---------------------------------------------------------------------------
# CLI integration tests for --template-file and --template-arg
# ---------------------------------------------------------------------------


def _invoke_pr_body(
    args: list[str],
    monkeypatch: pytest.MonkeyPatch,
    output_path: Path,
):
    """Invoke ``repomatic pr-body`` with all GITHUB_* env vars cleared.

    Writes to ``output_path`` instead of stdout so the assertion target is
    stable and {func}`~repomatic.cli.prep_path` does not call ``fileno()`` on
    the in-memory stream that Click's runner installs.
    """
    for key in GITHUB_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("GHA_PR_BODY_PREFIX", raising=False)
    monkeypatch.delenv("GHA_PR_BODY_PREFIX_FILE", raising=False)
    return CliRunner().invoke(
        repomatic,
        ["pr-body", "--output", str(output_path), *args],
    )


def test_cli_template_file(tmp_path, monkeypatch):
    """`--template-file` renders a markdown body from an external template."""
    template_path = tmp_path / "release-fruit.md"
    template_path.write_text(
        "---\n"
        "args: [fruit]\n"
        'title: "Pack $fruit"\n'
        "---\n\n"
        "### Pack $fruit\n\nReady for shipment.\n",
        encoding="UTF-8",
    )

    output_path = tmp_path / "body.txt"
    result = _invoke_pr_body(
        [
            "--template-file",
            str(template_path),
            "--template-arg",
            "fruit=mango",
            "--output-format",
            "github-actions",
        ],
        monkeypatch,
        output_path,
    )

    assert result.exit_code == 0, result.output
    rendered = output_path.read_text(encoding="UTF-8")
    assert "### Pack mango" in rendered
    assert "title=Pack mango" in rendered


def test_cli_prefix_file_reads_the_prefix_from_disk(tmp_path, monkeypatch):
    """`--prefix-file` carries a report too large to pass through the env."""
    prefix_path = tmp_path / "harvest.md"
    prefix_path.write_text("### Crates packed\n\nmango, kiwi.\n", encoding="UTF-8")

    output_path = tmp_path / "body.txt"
    result = _invoke_pr_body(
        ["--prefix-file", str(prefix_path)],
        monkeypatch,
        output_path,
    )

    assert result.exit_code == 0, result.output
    assert "### Crates packed" in output_path.read_text(encoding="UTF-8")


def test_cli_prefix_file_wins_over_prefix(tmp_path, monkeypatch):
    """With both given, the file is the one the workflow means."""
    prefix_path = tmp_path / "harvest.md"
    prefix_path.write_text("mango", encoding="UTF-8")

    output_path = tmp_path / "body.txt"
    result = _invoke_pr_body(
        ["--prefix", "kiwi", "--prefix-file", str(prefix_path)],
        monkeypatch,
        output_path,
    )

    assert result.exit_code == 0, result.output
    rendered = output_path.read_text(encoding="UTF-8")
    assert "mango" in rendered
    assert "kiwi" not in rendered


def test_cli_template_and_template_file_mutually_exclusive(tmp_path, monkeypatch):
    """Passing both `--template` and `--template-file` is rejected."""
    template_path = tmp_path / "external.md"
    template_path.write_text(
        '---\ntitle: "Empty"\n---\n\n### Empty\n',
        encoding="UTF-8",
    )

    result = _invoke_pr_body(
        ["--template", "fix-typos", "--template-file", str(template_path)],
        monkeypatch,
        tmp_path / "body.txt",
    )

    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_cli_template_arg_invalid_format(tmp_path, monkeypatch):
    """`--template-arg` without `=` is rejected with a clear error."""
    template_path = tmp_path / "fruit.md"
    template_path.write_text(
        '---\nargs: [fruit]\ntitle: "Pack $fruit"\n---\n\n### Pack $fruit\n',
        encoding="UTF-8",
    )

    result = _invoke_pr_body(
        ["--template-file", str(template_path), "--template-arg", "fruit"],
        monkeypatch,
        tmp_path / "body.txt",
    )

    assert result.exit_code != 0
    assert "KEY=VALUE" in result.output


def test_cli_template_arg_overrides_dedicated_flag(tmp_path, monkeypatch):
    """`--template-arg` values supersede the dedicated `--version` flag."""
    template_path = tmp_path / "release.md"
    template_path.write_text(
        "---\n"
        "args: [version]\n"
        'title: "Release $version"\n'
        "---\n\n"
        "### Release $version\n",
        encoding="UTF-8",
    )

    output_path = tmp_path / "body.txt"
    result = _invoke_pr_body(
        [
            "--template-file",
            str(template_path),
            "--version",
            "1.0.0",
            "--template-arg",
            "version=2.0.0",
            "--output-format",
            "github-actions",
        ],
        monkeypatch,
        output_path,
    )

    assert result.exit_code == 0, result.output
    rendered = output_path.read_text(encoding="UTF-8")
    assert "### Release 2.0.0" in rendered
    assert "title=Release 2.0.0" in rendered


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


@pytest.mark.parametrize("name", get_template_names())
def test_template_commit_message_carries_no_ci_skip_token(name: str) -> None:
    """No generated commit message may silently skip CI.

    Every `sync-*`, `format-*` and `fix-*` job commits a message rendered
    from a template, in this repository and in every downstream one. A skip
    token anywhere in that message stops the run, and because the required
    check is then never reported, the pull request blocks on a "Pending"
    status instead of failing visibly.
    """
    message = render_commit_message(name, **dict.fromkeys(template_args(name), "x"))
    lowered = message.lower()
    for token in CI_SKIP_TOKENS:
        assert token not in lowered, (
            f"template {name!r} renders a commit message containing {token!r}, "
            "which would skip CI for the job's own pull request"
        )


@pytest.mark.parametrize("name", get_template_names())
def test_template_commit_subject_claims_no_bracket_prefix(name: str) -> None:
    """A `[bracketed]` prefix is reserved for machine-parsed mechanisms.

    The one family templates may emit is
    {data}`repomatic.git_ops.CHANGELOG_COMMIT_PREFIX`, which
    {data}`repomatic.git_ops.VERSION_BUMP_COMMIT_PREFIXES` matches back
    (`bump-version` is the template that earns it). Any other bracket would
    either collide with that matcher or invent a label nothing reads. See
    docs/commit-messages.md.
    """
    message = render_commit_message(name, **dict.fromkeys(template_args(name), "x"))
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
