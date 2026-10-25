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

"""Tests for the label rules in :mod:`repomatic.github.labels`.

A rule is one label mapped to a list of patterns: keywords or regexes over a
thread's text, globs over a pull request's changed paths. These tests pin the
resolution semantics (project entries overlay the bundled defaults per label),
the pattern compiler, and the two matchers.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import tomlrt
import yaml
from click.testing import CliRunner

from repomatic.broken_links import DOCUMENTATION_LABEL, FIX_LINK_LABEL
from repomatic.bundle import get_data_content
from repomatic.cli.github import sponsor_label
from repomatic.cli.main import repomatic
from repomatic.config import Config, LabelsConfig, load_repomatic_config
from repomatic.deps.sync import DEPENDENCY_LABEL
from repomatic.github import labels as labels_module
from repomatic.github.event import get_github_event
from repomatic.github.labels import (
    DEFAULT_CONTENT_RULES,
    DEFAULT_FILE_RULES,
    _run_labelmaker,
    apply_labels,
    compile_content_pattern,
    declared_label_names,
    label_sources,
    match_content_rules,
    match_file_rules,
    resolve_content_rules,
    resolve_file_rules,
    serialize_inline_labels,
)
from repomatic.github.threads import BOT_ISSUE_LABEL


def _make_config(**labels_kwargs):
    return Config(labels=LabelsConfig(**labels_kwargs))


# -- Resolving rules over the defaults ----------------------------------------


@pytest.mark.parametrize(
    "resolver",
    (resolve_content_rules, resolve_file_rules),
    ids=("content", "file"),
)
def test_defaults_resolve_without_a_config(resolver):
    """The bundled defaults stand alone: no project config needed to match."""
    rules = resolver()
    assert rules
    assert all(isinstance(patterns, tuple) and patterns for patterns in rules.values())


@pytest.mark.parametrize(
    ("resolver", "field", "defaults", "default_label"),
    (
        (resolve_content_rules, "content_rules", DEFAULT_CONTENT_RULES, "🐛 bug"),
        (resolve_file_rules, "file_rules", DEFAULT_FILE_RULES, "🔗 dependencies"),
    ),
    ids=("content", "file"),
)
def test_project_entries_overlay_the_defaults(resolver, field, defaults, default_label):
    """A new label adds; a default label's entry is replaced, not merged."""
    rules = resolver(
        _make_config(**{
            field: {
                "🥭 mango": ["mango", "papaya"],
                default_label: ["crash"],
            },
        })
    )
    assert rules["🥭 mango"] == ("mango", "papaya")
    # Replacement, so a project can correct a default, not just widen it.
    assert rules[default_label] == ("crash",)
    # Untouched defaults keep flowing from upstream.
    assert rules["🆙 changelog"] == defaults["🆙 changelog"]


def test_empty_list_disables_a_default_label():
    """`"🐛 bug" = []` is the off switch for a bundled rule."""
    rules = resolve_content_rules(_make_config(content_rules={"🐛 bug": []}))
    assert "🐛 bug" not in rules
    assert match_content_rules(rules, "A bug and a traceback") == set()


def test_bare_string_is_a_single_pattern():
    """`"🥭 mango" = "mango"` reads as the one-element list it means."""
    rules = resolve_content_rules(_make_config(content_rules={"🥭 mango": "mango"}))
    assert rules["🥭 mango"] == ("mango",)


def test_rules_that_are_not_a_table_degrade_to_defaults(caplog):
    """A list where a table belongs is reported, then ignored.

    The config loader passes a mistyped value through rather than crashing, so
    every repomatic command keeps working and falls back to the bundled
    defaults.
    """
    malformed = [{"label": "🥭 mango", "patterns": ["mango"]}]
    with caplog.at_level(logging.WARNING):
        rules = resolve_content_rules(_make_config(content_rules=malformed))
    assert rules == DEFAULT_CONTENT_RULES
    assert any("expected a table" in record.message for record in caplog.records)


@pytest.mark.parametrize(
    ("overrides", "warning"),
    (
        pytest.param({"  ": ["mango"]}, "blank label", id="blank-label"),
        pytest.param({"🥭 mango": 42}, "expected a list", id="scalar"),
        pytest.param({"🥭 mango": ["ok", 42]}, "expected a list", id="mixed-list"),
    ),
)
def test_unusable_entries_are_skipped_loudly(caplog, overrides, warning):
    """A malformed entry is dropped with a warning, never a crash."""
    with caplog.at_level(logging.WARNING):
        rules = resolve_content_rules(_make_config(content_rules=overrides))
    assert "🥭 mango" not in rules
    assert any(warning in record.message for record in caplog.records)


# -- Compiling content patterns -----------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "text", "expected"),
    (
        # Bare keyword: case-insensitive, word-anchored.
        pytest.param("bug", "a BUG report", True, id="keyword-ignorecase"),
        pytest.param("fix", "prefix handling", False, id="keyword-word-anchored"),
        pytest.param("fix", "Fix the crash", True, id="keyword-at-edge"),
        # A non-word edge takes no anchor: `.lock` matches inside `uv.lock`.
        pytest.param(".lock", "the uv.lock file", True, id="dot-edge-unanchored"),
        pytest.param(".lock", "unlock it", False, id="dot-is-literal"),
        pytest.param("pyproject.toml", "in pyproject.toml", True, id="escaped-dot"),
        pytest.param("pyproject.toml", "pyprojectXtoml", False, id="no-regex-dot"),
        pytest.param("alpine linux", "On Alpine Linux 3.20", True, id="spaced"),
        # Slashed form: a raw regex, flags opt-in.
        pytest.param("/mango/i", "MANGO crate", True, id="regex-ignorecase"),
        pytest.param("/mango/", "MANGO crate", False, id="regex-case-sensitive"),
        pytest.param("/^mango$/m", "pears\nmango\n", True, id="regex-multiline"),
        pytest.param("/mango.crate/s", "mango\ncrate", True, id="regex-dotall"),
        # JavaScript-only flags are tolerated: they cannot change the verdict.
        pytest.param("/mango/gu", "a mango", True, id="js-only-flags-ignored"),
    ),
)
def test_content_pattern_forms(pattern, text, expected):
    """Both the keyword and `/body/flags` spellings behave as documented."""
    assert bool(match_content_rules({"🥭 mango": (pattern,)}, text)) is expected


def test_malformed_pattern_is_skipped_not_fatal(caplog):
    """One bad regex must not take the whole labelling run with it."""
    with caplog.at_level(logging.WARNING):
        matched = match_content_rules(
            {"🥭 mango": ("/[unclosed/",), "🍐 pear": ("pear",)},
            "a pear and a mango",
        )
    assert matched == {"🍐 pear"}
    assert compile_content_pattern("/[unclosed/") is None
    assert any("malformed content pattern" in r.message for r in caplog.records)


def test_content_patterns_are_or_joined():
    """Any one pattern matching applies the label: a keyword list just works."""
    rules = {"🥭 mango": ("mango", "papaya", "guava")}
    assert match_content_rules(rules, "a crate of papaya") == {"🥭 mango"}
    assert match_content_rules(rules, "a crate of pears") == set()


# -- Matching file rules ------------------------------------------------------


@pytest.mark.parametrize(
    ("patterns", "files", "expected"),
    (
        pytest.param(("*.md",), ["a.md", "b.py"], True, id="any-file-hits"),
        pytest.param(("*.md",), ["a.py"], False, id="no-file-hits"),
        pytest.param(("*.md",), ["sub/a.md"], False, id="star-stops-at-slash"),
        pytest.param(("**/*.md",), ["sub/a.md"], True, id="globstar"),
        pytest.param((".github/**/*",), [".github/w/x.yaml"], True, id="dotglob"),
        pytest.param(("{a,b}/*.py",), ["b/x.py"], True, id="brace"),
        # A negated glob subtracts from the label's other globs, the way a
        # `.gitignore` line reads.
        pytest.param(
            ("docs/**", "!docs/generated/**"),
            ["docs/guide.md"],
            True,
            id="negation-keeps-the-rest",
        ),
        pytest.param(
            ("docs/**", "!docs/generated/**"),
            ["docs/generated/api.md"],
            False,
            id="negation-subtracts",
        ),
        # A lone negation implies "everything else", not "nothing".
        pytest.param(("!**/*.md",), ["cli.py"], True, id="lone-negation"),
        pytest.param(("!**/*.md",), ["readme.md"], False, id="lone-negation-miss"),
        pytest.param(("**",), [], False, id="empty-diff-never-matches"),
    ),
)
def test_file_globs(patterns, files, expected):
    """The glob dialect stays `minimatch`'s, evaluated as one set per label."""
    assert bool(match_file_rules({"🥭 mango": patterns}, files)) is expected


# -- Bundled defaults ---------------------------------------------------------


def test_default_rule_labels_exist_in_the_label_registry():
    """Every label the package hard-codes names one `labels.toml` defines.

    Applying an unknown label fails the `gh` call outright, so a rule keyed on
    a label the registry dropped or renamed is not a dead rule but a broken
    one. The sponsor-label command's default rides the same registry and gets
    the same check. So do {data}`~repomatic.deps.sync.DEPENDENCY_LABEL`,
    applied to every dependency-update PR, the labels of the broken links
    issue, and the label of every issue repomatic maintains.
    """
    registry = tomlrt.loads(get_data_content("labels.toml"))
    defined = {
        label["name"]
        for profile in registry["profiles"].values()
        for label in profile["labels"]
    }
    rule_labels = set(DEFAULT_CONTENT_RULES) | set(DEFAULT_FILE_RULES)
    assert rule_labels <= defined, rule_labels - defined

    sponsor_default = next(
        param.default for param in sponsor_label.params if param.name == "label"
    )
    assert sponsor_default in defined

    assert DEPENDENCY_LABEL in defined
    assert {DOCUMENTATION_LABEL, FIX_LINK_LABEL, BOT_ISSUE_LABEL} <= defined


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        pytest.param("Traceback on an empty crate", {"🐛 bug"}, id="bug"),
        pytest.param("Update the README", {"📚 documentation"}, id="docs"),
        pytest.param("A workflow needs coverage", {"🤖 ci"}, id="ci"),
        pytest.param("Bump the changelog", {"🆙 changelog"}, id="changelog"),
        pytest.param("uv.lock drifted", {"🔗 dependencies"}, id="deps"),
        # `fix` must not fire inside `prefix`: keywords are word-anchored.
        pytest.param("prefix handling is off", set(), id="no-substring-match"),
        pytest.param("Nothing relevant at all", set(), id="no-match"),
    ),
)
def test_default_content_rules_match_as_advertised(text, expected):
    """The shipped keyword lists fire on their keyword and nothing else."""
    assert match_content_rules(resolve_content_rules(), text) == expected


BUNDLED_ISSUE_FORMS = ("awesome_template/.github/ISSUE_TEMPLATE/new-link.yaml",)
"""Every issue form repomatic bundles, so a keyword in one pre-labels them all."""


def _rendered_issue_form_body(path: str) -> str:
    """The text GitHub renders into a body filed through a bundled issue form.

    Each form element's `label` becomes a heading and each checkbox option's
    `label` becomes a line, both verbatim, so a keyword inside either reaches
    every submission filed through the form. A dropdown's unselected options
    and a textarea's `placeholder` do not, and are left out.

    :param path: The form's path inside the bundled data directory.
    :return: The rendered body text.
    """
    form = yaml.safe_load(get_data_content(path))
    parts: list[str] = []
    for element in form["body"]:
        attributes = element.get("attributes", {})
        if "label" in attributes:
            parts.append(f"### {attributes['label']}")
        for option in attributes.get("options") or ():
            if isinstance(option, dict) and "label" in option:
                parts.append(f"- [ ] {option['label']}")
    return "\n".join(parts)


@pytest.mark.parametrize("form_path", BUNDLED_ISSUE_FORMS)
def test_a_bundled_issue_form_pre_labels_nothing(form_path):
    """A form's own boilerplate must not label every submission filed through it.

    `.github` among the `🤖 ci` content rules matched the `.github/...` URLs
    this form embeds in its self-check boxes, so all four awesome lists
    labelled every new-link issue as CI. Read from the bundle rather than a
    copied string, so a keyword a later form edit introduces fails here.
    """
    body = _rendered_issue_form_body(form_path)
    assert body, f"{form_path} rendered no body text"
    assert match_content_rules(resolve_content_rules(), body) == set()


@pytest.mark.parametrize(
    ("files", "expected"),
    (
        pytest.param(["changelog.md"], {"🆙 changelog"}, id="changelog"),
        pytest.param(["uv.lock"], {"🔗 dependencies"}, id="lockfile"),
        pytest.param(["docs/guide/index.md"], {"📚 documentation"}, id="docs-tree"),
        pytest.param([".github/funding.yml"], {"🤖 ci"}, id="funding"),
        pytest.param(["repomatic/cli/main.py"], set(), id="source-is-unlabelled"),
    ),
)
def test_default_file_rules_match_as_advertised(files, expected):
    """The shipped globs cover the paths they name, and no others."""
    assert match_file_rules(resolve_file_rules(), files) == expected


# -- End-to-end through the config loader and the CLI -------------------------


def test_label_keys_survive_config_loading():
    """Emoji, spaces, colons and hyphens in label keys reach the rules intact.

    The loader normalizes configuration keys (`content-rules` →
    `content_rules`), and a label like `📦 manager: apt-cyg` must not be
    dragged through the same rewrite: mapping-typed fields are opaque to the
    normalization, which this pins.
    """
    config = load_repomatic_config({
        "tool": {
            "repomatic": {
                "labels": {
                    "content-rules": {"📦 manager: apt-cyg": ["cygwin"]},
                    "file-rules": {"📦 manager: apt-cyg": ["managers/apt_cyg.*"]},
                },
            },
        },
    })
    assert match_content_rules(resolve_content_rules(config), "Broken on Cygwin") == {
        "📦 manager: apt-cyg"
    }
    assert match_file_rules(resolve_file_rules(config), ["managers/apt_cyg.py"]) == {
        "📦 manager: apt-cyg"
    }


def _invoke_apply_labels(tmp_path, event, changed_files=(), *args):
    """Run `apply-labels` against a synthetic event, capturing `gh` calls."""
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event), encoding="UTF-8")
    # The event payload is cached per process, so a test that ran earlier in
    # the same worker with no event would otherwise pin an empty one here.
    get_github_event.cache_clear()
    calls: list[list[str]] = []

    def fake_gh(gh_args):
        calls.append(list(gh_args))
        if gh_args[:3] == ["api", "--paginate", "--slurp"]:
            return json.dumps([[{"filename": name} for name in changed_files]])
        return ""

    runner = CliRunner()
    # The JSON reads parse through the `gh` module, and the label writes go
    # through the threads module: one fake serves both.
    try:
        with (
            patch("repomatic.github.gh.run_gh_command", side_effect=fake_gh),
            patch("repomatic.github.threads.run_gh_command", side_effect=fake_gh),
        ):
            result = runner.invoke(
                repomatic,
                ["apply-labels", "--repo", "kevin/fruits", *args],
                env={
                    "GH_TOKEN": "x",
                    "GITHUB_EVENT_PATH": str(event_path),
                    "GITHUB_REPOSITORY": "kevin/fruits",
                },
            )
    finally:
        # Drop the synthetic event so later tests in this worker start clean.
        get_github_event.cache_clear()
    return result, calls


def test_apply_labels_dry_run_on_an_issue(tmp_path):
    """An issue event matches content rules only, and dry-run writes nothing."""
    event = {
        "issue": {
            "number": 42,
            "title": "Traceback when the crate is empty",
            "body": "The README does not mention it.",
        },
    }
    result, calls = _invoke_apply_labels(tmp_path, event)
    assert result.exit_code == 0, result.output
    assert "Would label issue #42 with: 🐛 bug, 📚 documentation" in result.output
    assert calls == []


def test_apply_labels_live_on_a_pull_request(tmp_path):
    """A PR event adds the file-rule matches and writes the labels once."""
    event = {
        "pull_request": {
            "number": 7,
            "title": "Sync the orchard",
            "body": "",
        },
    }
    result, calls = _invoke_apply_labels(
        tmp_path, event, ("changelog.md", "uv.lock"), "--live"
    )
    assert result.exit_code == 0, result.output
    assert "Labelled PR #7 with: 🆙 changelog, 🔗 dependencies" in result.output
    listing = next(call for call in calls if call[:2] == ["api", "--paginate"])
    assert listing[3].startswith("repos/kevin/fruits/pulls/7/files?")
    edit = next(call for call in calls if call[:2] == ["pr", "edit"])
    assert edit.count("--add-label") == 2
    assert edit[edit.index("--repo") + 1] == "kevin/fruits"


def test_apply_labels_reads_another_thread_from_the_api(tmp_path):
    """A thread the event does not carry has its text read from the API."""
    event = {"issue": {"number": 1, "title": "Unrelated", "body": ""}}
    result, calls = _invoke_apply_labels(tmp_path, event, (), "--number", "9")
    assert result.exit_code == 0, result.output
    view = next(call for call in calls if call[1:2] == ["view"])
    assert view[:3] == ["issue", "view", "9"]
    assert view[view.index("--repo") + 1] == "kevin/fruits"


# ---------------------------------------------------------------------------
# Label registry sync: `serialize_inline_labels`, `apply_labels` and the
# `sync-labels` command driving labelmaker.
# ---------------------------------------------------------------------------


def test_serialize_inline_labels_empty():
    """An empty input yields an empty string so the caller can skip labelmaker."""
    assert serialize_inline_labels([]) == ""


def test_serialize_inline_labels_all_invalid_returns_empty(caplog):
    """If every entry is nameless, the result is empty and warnings are logged."""
    caplog.set_level(logging.WARNING)
    entries = [{"color": "bfdadc"}, {"description": "no name here"}]
    assert serialize_inline_labels(entries) == ""
    assert len(caplog.records) == 2
    for record in caplog.records:
        assert "Skipping inline label without a `name`" in record.message


def test_serialize_inline_labels_round_trip():
    """Output parses back to the expected labelmaker structure."""
    entries = [
        {"name": "bug", "color": "d73a4a", "description": "Something isn't working"},
        {"name": "📦 manager: apk", "color": "bfdadc", "description": "apk"},
    ]
    output = serialize_inline_labels(entries)
    parsed = tomlrt.loads(output)
    labels = parsed["profiles"]["default"]["labels"]
    assert len(labels) == 2
    assert labels[0]["name"] == "bug"
    assert labels[0]["color"] == "d73a4a"
    assert labels[0]["description"] == "Something isn't working"
    assert labels[1]["name"] == "📦 manager: apk"


def test_serialize_inline_labels_strips_leading_hash_on_color():
    """Hex colors with a leading `#` are normalized to bare hex."""
    output = serialize_inline_labels([
        {"name": "bug", "color": "#d73a4a", "description": "x"},
    ])
    parsed = tomlrt.loads(output)
    assert parsed["profiles"]["default"]["labels"][0]["color"] == "d73a4a"


def test_serialize_inline_labels_omits_missing_optional_fields():
    """Missing `color` and `description` are omitted, not emitted as empty strings."""
    output = serialize_inline_labels([{"name": "bug"}])
    parsed = tomlrt.loads(output)
    label = parsed["profiles"]["default"]["labels"][0]
    assert label["name"] == "bug"
    assert "color" not in label
    assert "description" not in label


def test_serialize_inline_labels_passes_through_labelmaker_fields():
    """Every per-label labelmaker field rides through, booleans included."""
    output = serialize_inline_labels([
        {
            "name": "🔌 plugin",
            "color": "fef2c0",
            "description": "Plugin code",
            "create": False,
            "update": True,
            "enforce-case": False,
            "rename-from": ["🔌 bar-plugin", "plugin"],
            "on-rename-clash": "error",
        },
    ])
    parsed = tomlrt.loads(output)
    label = parsed["profiles"]["default"]["labels"][0]
    assert label["create"] is False
    assert label["update"] is True
    assert label["enforce-case"] is False
    assert label["rename-from"] == ["🔌 bar-plugin", "plugin"]
    assert label["on-rename-clash"] == "error"


def test_serialize_inline_labels_strips_hash_on_multi_color():
    """Multi-color lists are normalized to bare hex like single colors."""
    output = serialize_inline_labels([
        {"name": "bug", "color": ["#d73a4a", "bfdadc"]},
    ])
    parsed = tomlrt.loads(output)
    assert parsed["profiles"]["default"]["labels"][0]["color"] == [
        "d73a4a",
        "bfdadc",
    ]


def test_serialize_inline_labels_omits_empty_values():
    """Empty strings and empty lists are omitted, not emitted as blanks."""
    output = serialize_inline_labels([
        {"name": "bug", "description": "", "rename-from": []},
    ])
    parsed = tomlrt.loads(output)
    label = parsed["profiles"]["default"]["labels"][0]
    assert "description" not in label
    assert "rename-from" not in label


def test_serialize_inline_labels_warns_on_unknown_fields(caplog):
    """Unknown fields are dropped with a warning instead of aborting the sync."""
    caplog.set_level(logging.WARNING)
    output = serialize_inline_labels([
        {"name": "bug", "color": "d73a4a", "renme-from": ["typo"], "bogus": 1},
    ])
    parsed = tomlrt.loads(output)
    label = parsed["profiles"]["default"]["labels"][0]
    assert label["name"] == "bug"
    assert "renme-from" not in label
    assert "bogus" not in label
    assert any(
        "Ignoring unknown fields 'bogus', 'renme-from' on inline label 'bug'"
        in r.message
        for r in caplog.records
    )


def test_serialize_inline_labels_skips_blank_name(caplog):
    """A whitespace-only name is treated as missing."""
    caplog.set_level(logging.WARNING)
    output = serialize_inline_labels([
        {"name": "   ", "color": "bfdadc"},
        {"name": "real", "color": "bfdadc"},
    ])
    parsed = tomlrt.loads(output)
    labels = parsed["profiles"]["default"]["labels"]
    assert len(labels) == 1
    assert labels[0]["name"] == "real"
    assert any(
        "Skipping inline label without a `name`" in r.message for r in caplog.records
    )


@pytest.mark.parametrize(
    ("name", "description"),
    [
        ('quoted "label"', "has \"quotes\" and 'apostrophes'"),
        ("path\\separator", "back\\slash"),
        ("newline\ncontent", "tab\there"),
    ],
)
def test_serialize_inline_labels_escapes_special_characters(name, description):
    """Special characters survive serialization and re-parsing."""
    output = serialize_inline_labels([
        {"name": name, "color": "bfdadc", "description": description},
    ])
    parsed = tomlrt.loads(output)
    label = parsed["profiles"]["default"]["labels"][0]
    assert label["name"] == name
    assert label["description"] == description


# --- Label sources ---


@pytest.fixture
def captured_labelmaker(monkeypatch):
    """Record every labelmaker invocation, with the staged file's content."""
    calls: list[tuple[str, ...]] = []
    staged: dict[str, str] = {}

    def run(_lm, *args):
        # The staging directory is gone once `apply_labels` returns.
        calls.append(args)
        path = Path(args[1])
        staged[path.name] = path.read_text(encoding="UTF-8")

    monkeypatch.setattr(
        labels_module,
        "ensure_binary",
        lambda _name: Path("/fake/labelmaker"),
    )
    monkeypatch.setattr(labels_module, "_run_labelmaker", run)
    return calls, staged


def _staged_name(call: tuple[str, ...]) -> str:
    """The staging-relative path of the file one labelmaker call applies."""
    path = Path(call[1])
    if path.parent.name == "extra-labels":
        return f"extra-labels/{path.name}"
    return path.name


def test_apply_labels_applies_the_bundled_default_profile(
    tmp_path, monkeypatch, captured_labelmaker
):
    """The bundled definitions go first, under the `default` profile alone."""
    calls, staged = captured_labelmaker
    monkeypatch.chdir(tmp_path)

    apply_labels(Config(), "owner/repo", is_awesome=False)

    assert [(_staged_name(call), *call[2:]) for call in calls] == [
        ("labels.toml", "--profile", "default", "owner/repo")
    ]
    assert staged["labels.toml"] == get_data_content("labels.toml")


def test_apply_labels_applies_the_awesome_profile(
    tmp_path, monkeypatch, captured_labelmaker
):
    """An awesome list gets the `awesome` profile on top of `default`."""
    calls, _staged = captured_labelmaker
    monkeypatch.chdir(tmp_path)

    apply_labels(Config(), "owner/awesome-list", is_awesome=True)

    assert [call[3] for call in calls] == ["default", "awesome"]
    assert {_staged_name(call) for call in calls} == {"labels.toml"}


def test_sync_labels_reads_an_awesome_list_from_its_name(
    tmp_path, monkeypatch, captured_labelmaker
):
    """`sync-labels` adds the `awesome` profile for an `awesome-*` repository."""
    calls, _staged = captured_labelmaker
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        repomatic, ["sync-labels", "--repo", "owner/awesome-list"]
    )

    assert result.exit_code == 0, result.output
    assert "Labels synced." in result.output
    assert [call[3:] for call in calls] == [
        ("default", "owner/awesome-list"),
        ("awesome", "owner/awesome-list"),
    ]


def test_sync_labels_fails_on_a_labelmaker_failure(tmp_path, monkeypatch):
    """A labelmaker run that fails ends the command with labelmaker's message."""
    monkeypatch.chdir(tmp_path)
    with (
        patch(
            "repomatic.github.labels.ensure_binary",
            return_value=Path("/fake/labelmaker"),
        ),
        patch(
            "repomatic.github.labels._run_labelmaker",
            side_effect=RuntimeError("labelmaker failed: rate limited"),
        ),
    ):
        result = CliRunner().invoke(repomatic, ["sync-labels", "--repo", "owner/repo"])

    assert result.exit_code == 1
    assert "labelmaker failed: rate limited" in result.output
    assert "Labels synced." not in result.output


@pytest.mark.parametrize(
    ("stdout", "logged"),
    (
        pytest.param("", [], id="quiet"),
        pytest.param("Applied 🥝 kiwi.", ["Applied 🥝 kiwi."], id="chatty"),
    ),
)
def test_run_labelmaker_passes_the_promoted_token(caplog, stdout, logged):
    """labelmaker runs with the `gh` environment, its output kept for the debug log."""
    binary = Path("/fake/labelmaker")
    finished = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")
    with (
        patch("repomatic.github.labels.gh_env", return_value={"GH_TOKEN": "kiwi"}),
        patch("repomatic.github.labels.subprocess.run", return_value=finished) as run,
        caplog.at_level(logging.DEBUG),
    ):
        _run_labelmaker(binary, "apply", "labels.toml", "owner/repo")

    assert run.call_args.args[0] == [str(binary), "apply", "labels.toml", "owner/repo"]
    assert run.call_args.kwargs["env"] == {"GH_TOKEN": "kiwi"}
    debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert debug == logged


def test_run_labelmaker_raises_with_its_error_output():
    """A failed run raises with what labelmaker printed on its error stream."""
    failed = subprocess.CompletedProcess([], 1, stdout="", stderr="rate limited")
    with (
        patch("repomatic.github.labels.gh_env", return_value=None),
        patch("repomatic.github.labels.subprocess.run", return_value=failed),
        pytest.raises(RuntimeError, match=r"^labelmaker failed: rate limited$"),
    ):
        _run_labelmaker(Path("/fake/labelmaker"), "apply", "labels.toml", "owner/repo")


def test_apply_labels_stages_outside_the_working_tree(
    tmp_path, monkeypatch, captured_labelmaker
):
    """Every file labelmaker reads sits in a scratch directory, never the tree."""
    calls, _staged = captured_labelmaker
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.chdir(checkout)

    apply_labels(Config(), "owner/repo", is_awesome=False)

    assert not Path(calls[0][1]).is_relative_to(checkout)
    assert list(checkout.iterdir()) == []


def test_apply_labels_applies_committed_extra_labels(
    tmp_path, monkeypatch, captured_labelmaker
):
    """A hand-written `extra-labels/` file is applied without a profile."""
    calls, staged = captured_labelmaker
    (tmp_path / "extra-labels").mkdir()
    (tmp_path / "extra-labels" / "hand-written.toml").write_text(
        "# Kiwi.\n", encoding="UTF-8"
    )

    apply_labels(Config(), "owner/repo", is_awesome=False, root=tmp_path)

    assert calls[1][2:] == ("owner/repo",)
    assert _staged_name(calls[1]) == "extra-labels/hand-written.toml"
    assert staged["hand-written.toml"] == "# Kiwi.\n"


def test_download_shadows_committed_file_of_same_name(tmp_path):
    """A `labels.extra-files` download wins over a committed file of that name."""
    (tmp_path / "extra-labels").mkdir()
    (tmp_path / "extra-labels" / "shared.toml").write_text(
        "# Committed.\n", encoding="UTF-8"
    )
    config = _make_config(extra_files=["https://example.com/fruits/shared.toml"])

    with patch("repomatic.github.labels.get_bytes", return_value=b"# Downloaded.\n"):
        sources = list(label_sources(config, is_awesome=False, root=tmp_path))

    extra = [source for source in sources if source.path.startswith("extra-labels/")]
    assert [(source.path, source.text) for source in extra] == [
        ("extra-labels/shared.toml", "# Downloaded.\n")
    ]


def test_inline_definitions_come_last(tmp_path):
    """The inline definitions are applied after every file."""
    config = _make_config(extra=[{"name": "🍋 citrus", "color": "fef2c0"}])

    sources = list(label_sources(config, is_awesome=False, root=tmp_path))

    assert [source.path for source in sources] == ["labels.toml", "inline.toml"]
    assert "🍋 citrus" in sources[-1].text


def test_declared_names_read_the_applied_sources(tmp_path):
    """The names a lint calls declared are the names the sync applies."""
    (tmp_path / "extra-labels").mkdir()
    (tmp_path / "extra-labels" / "kiwi.yaml").write_text(
        "profiles:\n  default:\n    labels:\n      - name: 🥝 kiwi\n",
        encoding="UTF-8",
    )
    config = _make_config(extra=[{"name": "🍋 citrus"}])

    names = declared_label_names(config, is_awesome=False, root=tmp_path)

    assert {"🥝 kiwi", "🍋 citrus"} <= names
    assert BOT_ISSUE_LABEL in names
