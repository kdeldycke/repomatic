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

from pathlib import Path

import pytest
from click.testing import CliRunner

from repomatic import __version__
from repomatic.cli.main import repomatic
from repomatic.config import Config
from repomatic.github.pr_body import (
    GITHUB_BODY_MAX_CHARS,
    TemplateArgumentError,
    _utf16_len,
    build_pr_body,
    fit_github_body,
    generate_pr_metadata_block,
    generate_refresh_tip,
    render_pr,
)
from repomatic.metadata.core import Metadata
from repomatic.templating import PrTemplate, get_template_names, render_template
from repomatic.versions import strip_dev_suffix

PR_TEMPLATES = tuple(
    name for name in get_template_names() if PrTemplate.load(name).meta.get("title")
)
"""The templates that front a pull request or an issue: the ones with a title."""

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


@pytest.fixture
def no_github_env(monkeypatch):
    """Clear the `GITHUB_*` environment, as on a developer machine."""
    for key in GITHUB_ENV_VARS:
        monkeypatch.delenv(key, raising=False)


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


def test_generate_metadata_block_minimal_vars(no_github_env):
    """Graceful degradation when most env vars are unset."""
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


def test_generate_refresh_tip_without_workflow_ref(no_github_env):
    """Tip is empty when workflow ref is unavailable."""
    assert generate_refresh_tip(Metadata()) == ""


# -- render_pr ------------------------------------------------------------------


@pytest.mark.parametrize("name", PR_TEMPLATES)
def test_render_pr_carries_one_footer(name, github_env):
    """Every pull request body ends on exactly one attribution footer.

    The metadata block carries it, so a template that rendered its own on top
    would show it twice.
    """
    template = PrTemplate.load(name)
    content = render_pr(
        Metadata(),
        Config(),
        template=template,
        extra_args=dict.fromkeys(template.args, "x"),
    )
    assert content.body.count("Generated with") == 1


def test_render_pr_without_a_template(no_github_env):
    """A prefix alone renders a body, and no title nor commit message."""
    content = render_pr(Metadata(), Config(), prefix="Ten crates of mango.")
    assert content.body.startswith("Ten crates of mango.")
    assert content.title == ""
    assert content.commit_message == ""
    assert "Documentation" not in content.body


def test_render_pr_links_the_template_documentation(no_github_env):
    """The metadata block names the template and links its job's section."""
    content = render_pr(Metadata(), Config(), template=PrTemplate.load("format-python"))
    assert content.title == "Format Python"
    assert content.commit_message == "Format Python"
    assert "[`format-python`](https://repomatic.net/workflows#" in content.body


def test_render_pr_extra_args_win_over_flags(no_github_env):
    """An explicit argument supersedes the dedicated flag of the same name."""
    content = render_pr(
        Metadata(),
        Config(),
        template=PrTemplate.load("bump-version"),
        extra_args={"version": "2.0.0"},
        version="1.0.0",
        part="minor",
    )
    assert content.title == "[changelog] Bump minor version to `v2.0.0`"


def test_render_pr_names_the_missing_argument(no_github_env):
    """An argument nothing supplies fails with the flag that would supply it."""
    with pytest.raises(TemplateArgumentError, match="--part is required"):
        render_pr(
            Metadata(),
            Config(),
            template=PrTemplate.load("bump-version"),
            version="1.2.0",
        )


def test_render_pr_reads_the_diff_table_from_its_file(
    no_github_env, tmp_path, monkeypatch
):
    """A report reaches the body through the file its `_FILE` variable names."""
    report = tmp_path / "diff-table.md"
    report.write_text(
        "| Fruit | Crates |\n| --- | --- |\n| fig | 3 |", encoding="UTF-8"
    )
    monkeypatch.setenv("REPOMATIC_DIFF_TABLE_FILE", str(report))
    monkeypatch.delenv("REPOMATIC_DIFF_TABLE", raising=False)
    content = render_pr(Metadata(), Config(), template=PrTemplate.load("sync-uv-lock"))
    assert "| fig | 3 |" in content.body


# -- Body size ------------------------------------------------------------------


FAKE_FOOTER = (
    "<details>metadata</details>\n\n"
    "***\n\n"
    "Generated with [repomatic](https://github.com/kdeldycke/repomatic)"
)
"""Simulates ``generate_pr_metadata_block(Metadata())`` output for unit tests."""


def test_build_pr_body_with_prefix(no_github_env):
    """Prefix is prepended with triple newline separator."""
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


def test_build_pr_body_empty_prefix(no_github_env):
    """Empty prefix without tip still includes footer and metadata."""
    result = build_pr_body("", FAKE_FOOTER)

    assert "Generated with [repomatic]" in result
    assert "<details>metadata</details>" in result


def test_build_pr_body_trims_oversized_prefix(github_env):
    """An oversized prefix is trimmed so the tail always survives.

    GitHub truncates oversized bodies from the end, which silently drops the
    refresh tip, metadata block, and attribution footer (the fate of huge
    `sync-uv-lock` tables). The prefix is trimmed instead, on line boundaries,
    with a caution admonition marking the cut.
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

    Oversized issue bodies make `gh issue create` and `gh issue edit` fail
    outright, so the guard rewrites the body to fit before it reaches the API.
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
    stable and {func}`click_extra.prep_path` does not call ``fileno()`` on
    the in-memory stream that Click's runner installs.
    """
    for key in GITHUB_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
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
    """`--prefix-file` carries a report too large to pass as an argument."""
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


def test_cli_missing_template_arg_is_a_usage_error(tmp_path, monkeypatch):
    """An argument nothing supplies is reported as a usage error."""
    result = _invoke_pr_body(
        ["--template", "bump-version", "--version", "1.2.0"],
        monkeypatch,
        tmp_path / "body.txt",
    )

    assert result.exit_code == 2
    assert "--part is required" in result.output


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
