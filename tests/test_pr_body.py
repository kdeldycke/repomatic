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

from repomatic.config import config_reference
from repomatic.github.pr_body import (
    _parse_frontmatter,
    _unescape_dollars,
    build_pr_body,
    extract_workflow_filename,
    generate_pr_metadata_block,
    generate_refresh_tip,
    get_template_names,
    load_template,
    render_commit_message,
    render_template,
    render_title,
    template_args,
)

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


def test_parse_frontmatter_unescapes_body():
    r"""Body ``\$`` placeholders are unescaped at parse time."""
    raw = "---\ntitle: Test\n---\nHello \\$name"
    _meta, body = _parse_frontmatter(raw)
    assert "$name" in body
    assert r"\$" not in body


def test_parse_frontmatter_unescapes_no_frontmatter():
    r"""Templates without frontmatter also get ``\$`` unescaped."""
    raw = "Hello \\$world"
    _meta, body = _parse_frontmatter(raw)
    assert body == "Hello $world"


def test_generate_metadata_block_all_vars(monkeypatch):
    """Metadata block includes all expected fields when env vars are set."""
    for key, value in GITHUB_ENV_VARS.items():
        monkeypatch.setenv(key, value)

    block = generate_pr_metadata_block()

    assert "<details>" in block
    assert "<summary><code>Workflow metadata</code></summary>" in block
    assert "**Trigger**" in block
    assert "`push`" in block
    assert "**Actor**" in block
    assert "@dependabot[bot]" in block
    assert "**Ref**" in block
    assert "`main`" in block
    assert "**Commit**" in block
    assert "[`abc12345`]" in block
    assert "**Job**" in block
    assert "`autofix`" in block
    assert "**Workflow**" in block
    assert "[`autofix.yaml`]" in block
    assert "**Run**" in block
    assert "[#42.1]" in block
    assert "</details>" in block
    # Same actor, no re-run row.
    assert "Re-run by" not in block


def test_generate_metadata_block_rerun(monkeypatch):
    """Re-run by row appears when triggering actor differs from actor."""
    for key, value in GITHUB_ENV_VARS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GITHUB_TRIGGERING_ACTOR", "admin-user")

    block = generate_pr_metadata_block()

    assert "**Re-run by**" in block
    assert "@admin-user" in block


def test_generate_metadata_block_minimal_vars(monkeypatch):
    """Graceful degradation when most env vars are unset."""
    # Clear all GITHUB_ vars that might be set.
    for key in GITHUB_ENV_VARS:
        monkeypatch.delenv(key, raising=False)

    block = generate_pr_metadata_block()

    # Should still produce a valid details block without crashing.
    assert "<details>" in block
    assert "</details>" in block
    assert "**Trigger**" in block


def test_generate_refresh_tip_with_workflow_ref(monkeypatch):
    """Tip includes workflow dispatch URL when env vars are set."""
    for key, value in GITHUB_ENV_VARS.items():
        monkeypatch.setenv(key, value)

    tip = generate_refresh_tip()

    assert "> [!IMPORTANT]" in tip
    assert "Run workflow" in tip
    assert "https://github.com/owner/repo/actions/workflows/autofix.yaml" in tip


def test_generate_refresh_tip_without_workflow_ref(monkeypatch):
    """Tip is empty when workflow ref is unavailable."""
    for key in GITHUB_ENV_VARS:
        monkeypatch.delenv(key, raising=False)

    assert generate_refresh_tip() == ""


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
    assert "update-deps-graph" in names
    assert "update-docs" in names
    assert "sync-gitignore" in names
    assert "sync-mailmap" in names
    assert "sync-uv-lock" in names
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
    assert "renovate-migration" in names
    assert "unavailable-admonition" in names
    assert "unsubscribe-phase1" in names
    assert "unsubscribe-phase2" in names
    assert "setup-guide-fork-pr-approval" in names
    assert "setup-guide-virustotal" in names
    assert "yanked-admonition" in names
    assert "format-shell" in names
    assert "setup-guide-pypi-trusted-publisher" in names
    assert len(names) == 43


def test_load_template_frontmatter():
    """Frontmatter is parsed correctly from a parameterized template."""
    meta, body = load_template("bump-version")

    assert meta["args"] == ["version", "part"]
    assert body.startswith("### Description")


def test_load_template_title_only_frontmatter():
    """Static templates have frontmatter with title but no args."""
    meta, body = load_template("fix-typos")

    assert "title" in meta
    assert "args" not in meta
    assert body.startswith("### Description")


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
    assert template_args("prepare-release") == ["version"]


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
    assert title == "Bump minor version to `v1.2.0`"

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
    """Parameterized templates substitute variables in commit_message."""
    msg = render_commit_message("bump-version", version="1.2.0", part="minor")
    assert msg == "Bump minor version to `v1.2.0`"

    msg = render_commit_message("prepare-release", version="5.8.1")
    assert msg == "Release `v5.8.1`"


def test_render_bump_version():
    """Bump version template includes part, version, and merge instructions."""
    result = render_template("bump-version", version="1.2.0", part="minor")

    assert "bump the minor part" in result
    assert "### To bump version to `v1.2.0`" in result
    assert "Ready for review" in result
    assert "Rebase and merge" in result
    assert "bump-version" in result
    assert "changelog-yaml-jobs" in result


def test_render_prepare_release(monkeypatch):
    """Prepare release template includes version, links, and caution admonition."""
    for key, value in GITHUB_ENV_VARS.items():
        monkeypatch.setenv(key, value)

    result = render_template(
        "prepare-release",
        version="5.8.1",
    )

    assert "### How-to release `v5.8.1`" in result
    assert "`v5.8.1` tag on `main`" in result
    assert "`v5.8.1` release" in result
    assert "[!CAUTION]" in result
    assert "Squash and merge" in result
    assert "PyPI" in result
    assert "prepare-release" in result
    assert "changelog-yaml-jobs" in result
    assert "release-yaml-jobs" in result


def test_render_sync_gitignore():
    """Sync gitignore template includes description, config options, and docs link."""
    result = render_template("sync-gitignore")

    assert "### Description" in result
    assert "gitignore.io" in result
    assert "sync-gitignore" in result
    assert "### Configuration" in result
    assert "gitignore.extra-categories" in result
    assert "gitignore.extra-content" in result
    assert "gitignore.location" in result
    assert "[tool.repomatic]" in result


def test_render_fix_typos():
    """Fix typos template includes description and docs link."""
    result = render_template("fix-typos")

    assert "### Description" in result
    assert "typos" in result
    assert "autofix-yaml-jobs" in result


def test_render_format_json():
    """Format JSON template includes description and docs link."""
    result = render_template("format-json")

    assert "### Description" in result
    assert "Biome" in result
    assert "autofix-yaml-jobs" in result


def test_render_format_markdown():
    """Format Markdown template includes description and docs link."""
    result = render_template("format-markdown")

    assert "### Description" in result
    assert "mdformat" in result
    assert "autofix-yaml-jobs" in result


def test_render_format_pyproject():
    """Format pyproject template includes description and docs link."""
    result = render_template("format-pyproject")

    assert "### Description" in result
    assert "pyproject-fmt" in result
    assert "autofix-yaml-jobs" in result


def test_render_format_python():
    """Format Python template includes description, tools, and docs link."""
    result = render_template("format-python")

    assert "### Description" in result
    assert "autopep8" in result
    assert "Ruff" in result
    assert "[tool.ruff]" in result
    assert "autofix-yaml-jobs" in result


def test_render_sync_bumpversion():
    """Sync bumpversion template includes description and docs link."""
    result = render_template("sync-bumpversion")

    assert "### Description" in result
    assert "bumpversion" in result
    assert "autofix-yaml-jobs" in result


def test_render_update_deps_graph():
    """Update deps graph template includes description, config options, and docs link."""
    result = render_template("update-deps-graph")

    assert "### Description" in result
    assert "Mermaid" in result
    assert "autofix-yaml-jobs" in result
    assert "### Configuration" in result
    assert "dependency-graph.output" in result
    assert "[tool.repomatic]" in result


def test_render_update_docs():
    """Update docs template includes description, config options, and docs link."""
    result = render_template("update-docs")

    assert "### Description" in result
    assert "sphinx-apidoc" in result
    assert "docs_update.py" in result
    assert "autofix-yaml-jobs" in result
    assert "### Configuration" in result
    assert "docs.apidoc-exclude" in result
    assert "[tool.repomatic]" in result


def test_render_sync_mailmap():
    """Sync mailmap template includes description and docs link."""
    result = render_template("sync-mailmap")

    assert "### Description" in result
    assert ".mailmap" in result
    assert "autofix-yaml-jobs" in result


# Config-option references in PR body templates, written as
# ``- [`key`](…/configuration.html#anchor)`` bullets under "### Configuration".
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
"""Simulates ``generate_pr_metadata_block()`` output for unit tests."""


def test_build_pr_body_with_prefix(monkeypatch):
    """Prefix is prepended with triple newline separator."""
    for key in GITHUB_ENV_VARS:
        monkeypatch.delenv(key, raising=False)

    result = build_pr_body("Fix formatting issues.", FAKE_FOOTER)

    assert result.startswith("Fix formatting issues.")
    assert "Generated with [repomatic]" in result
    assert "\n\n\n<details>metadata</details>" in result


def test_build_pr_body_with_tip(monkeypatch):
    """Tip is inserted between prefix and footer when env vars are set."""
    for key, value in GITHUB_ENV_VARS.items():
        monkeypatch.setenv(key, value)

    result = build_pr_body("Description.", FAKE_FOOTER)

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


# ---------------------------------------------------------------------------
# Template file policy validation
# ---------------------------------------------------------------------------

REFERENCE_WORKFLOWS = (
    ".github/workflows/autofix.yaml",
    ".github/workflows/changelog.yaml",
    # The detect-squash-merge --template reference lives in the build lane, not
    # the release.yaml entry (which only calls the lanes and publishes).
    ".github/workflows/_release-build.yaml",
    ".github/workflows/renovate.yaml",
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
    "renovate-migration",
    "setup-guide",
    "setup-guide-branch-ruleset",
    "setup-guide-dependabot",
    "setup-guide-fork-pr-approval",
    "setup-guide-pages-source",
    "setup-guide-pypi-trusted-publisher",
    "setup-guide-token",
    "setup-guide-verify",
    "setup-guide-virustotal",
    "unavailable-admonition",
    "unsubscribe-phase1",
    "unsubscribe-phase2",
    "yanked-admonition",
})
"""Templates rendered from Python code, not via the ``--template`` CLI flag."""


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
        filename = getattr(item, "name", str(item))
        if filename.startswith("__"):
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
    # Must be a markdown file.
    assert filename.endswith(".md"), f"Template file {filename!r} is not a .md file"

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
            marker = f"${arg}"
            assert marker in body or marker in frontmatter_text, (
                f"Template {name!r} declares arg {arg!r}"
                f" but neither body nor frontmatter contains {marker}"
            )

    # Body must start with a markdown heading.
    assert body.startswith("###"), (
        f"Template {name!r} body must start with a '###' heading"
    )


FRONTMATTER_KEY_ORDER = ["args", "title", "commit_message", "footer"]
"""Canonical ordering of keys within template frontmatter."""


@pytest.mark.parametrize(
    ("filename", "name"),
    _template_package_items(),
    ids=[pair[1] for pair in _template_package_items()],
)
def test_frontmatter_key_ordering(filename, name):
    """Frontmatter keys must follow the canonical order: args, title, commit_message, footer."""
    template_dir = files("repomatic.templates")
    raw = template_dir.joinpath(filename).read_text(encoding="UTF-8")
    if not raw.startswith("---"):
        return
    end = raw.index("---", 3)
    yaml_block = raw[3:end].strip()
    keys = [
        line.partition(":")[0].strip()
        for line in yaml_block.splitlines()
        if ":" in line
    ]
    known = [k for k in keys if k in FRONTMATTER_KEY_ORDER]
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
    ],
)
def test_version_dev_suffix_stripping(version, expected):
    """The .dev suffix is stripped from auto-detected versions."""
    result = re.sub(r"\.dev\d*$", "", version)
    assert result == expected


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
    from click.testing import CliRunner

    from repomatic.cli import repomatic

    for key in GITHUB_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("GHA_PR_BODY_PREFIX", raising=False)
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
