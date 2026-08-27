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

"""Tests for the label rules in :mod:`repomatic.labels`.

A rule is one label mapped to a list of patterns: keywords or regexes over a
thread's text, globs over a pull request's changed paths. These tests pin the
resolution semantics (project entries overlay the bundled defaults per label),
the pattern compiler, and the two matchers.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
import tomlrt
from click.testing import CliRunner

from repomatic import labels as labels_module
from repomatic.cli.main import repomatic
from repomatic.config import Config, LabelsConfig, load_repomatic_config
from repomatic.github.actions import get_github_event
from repomatic.labels import (
    DEFAULT_CONTENT_RULES,
    DEFAULT_FILE_RULES,
    apply_labels,
    compile_content_pattern,
    match_content_rules,
    match_file_rules,
    resolve_content_rules,
    resolve_file_rules,
    serialize_inline_labels,
)
from repomatic.sync_ops import DEPENDENCY_LABEL
from repomatic.tooling.bundle import get_data_content


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


def test_stale_array_of_tables_degrades_to_defaults(caplog):
    """The pre-7.11.0 list shape is reported, then ignored.

    The config loader passes a mistyped value through rather than crashing, so
    a downstream repo that has not migrated keeps every repomatic command
    working and falls back to the bundled defaults.
    """
    stale = [{"label": "🥭 mango", "patterns": ["mango"]}]
    with caplog.at_level(logging.WARNING):
        rules = resolve_content_rules(_make_config(content_rules=stale))
    assert rules == DEFAULT_CONTENT_RULES
    assert any("array-of-tables" in record.message for record in caplog.records)


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
    the same check: its label once carried a stray plural that existed in no
    repository, and every sponsor-labelling attempt died on it.
    {data}`~repomatic.sync_ops.DEPENDENCY_LABEL` is in the population for the
    same reason, being applied to every dependency-update PR.
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
        param.default
        for param in repomatic.commands["sponsor-label"].params
        if param.name == "label"
    )
    assert sponsor_default in defined

    assert DEPENDENCY_LABEL in defined


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


def _invoke_apply_labels(tmp_path, event, gh_responses, *args):
    """Run `apply-labels` against a synthetic event, capturing `gh` calls."""
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event), encoding="UTF-8")
    # The event payload is cached per process, so a test that ran earlier in
    # the same worker with no event would otherwise pin an empty one here.
    get_github_event.cache_clear()
    calls: list[list[str]] = []

    def fake_gh(gh_args):
        calls.append(list(gh_args))
        return gh_responses.get(tuple(gh_args[:2]), "")

    runner = CliRunner()
    # Each consuming module bound `run_gh_command` at import, so every binding
    # is patched: `pr` (changed files), `issue` (label writes), and `gh`
    # itself, whose `gh_api_json` backs the manual-invocation subject fetch.
    try:
        with (
            patch("repomatic.github.gh.run_gh_command", side_effect=fake_gh),
            patch("repomatic.github.pr.run_gh_command", side_effect=fake_gh),
            patch("repomatic.github.issue.run_gh_command", side_effect=fake_gh),
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
    result, calls = _invoke_apply_labels(tmp_path, event, {})
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
    files = "changelog.md\nuv.lock\n"
    result, calls = _invoke_apply_labels(
        tmp_path,
        event,
        {("api", "repos/kevin/fruits/pulls/7/files"): files},
        "--live",
    )
    assert result.exit_code == 0, result.output
    assert "Labelled PR #7 with: 🆙 changelog, 🔗 dependencies" in result.output
    edit = next(call for call in calls if call[:2] == ["pr", "edit"])
    assert edit.count("--add-label") == 2


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


# --- apply_labels path handling ---


@pytest.fixture
def captured_labelmaker(monkeypatch):
    """Record every labelmaker invocation instead of running the binary."""
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(
        labels_module,
        "ensure_binary",
        lambda _name: Path("/fake/labelmaker"),
    )
    monkeypatch.setattr(
        labels_module,
        "_run_labelmaker",
        lambda _lm, *args: calls.append(args),
    )
    return calls


def test_apply_labels_reads_labels_toml_from_labels_dir(
    tmp_path, monkeypatch, captured_labelmaker
):
    """`labels_dir` decides where `labels.toml` is read from, not the CWD."""
    monkeypatch.chdir(tmp_path)
    export = tmp_path / "export"
    export.mkdir()

    apply_labels(Config(), "owner/repo", is_awesome=False, labels_dir=export)

    assert captured_labelmaker == [
        ("apply", str(export / "labels.toml"), "--profile", "default", "owner/repo")
    ]


def test_apply_labels_defaults_to_current_directory(
    tmp_path, monkeypatch, captured_labelmaker
):
    """Omitting `labels_dir` keeps the historical CWD-relative behaviour."""
    monkeypatch.chdir(tmp_path)

    apply_labels(Config(), "owner/repo", is_awesome=False)

    assert captured_labelmaker[0][1] == "labels.toml"


def test_apply_labels_applies_awesome_profile_from_labels_dir(
    tmp_path, monkeypatch, captured_labelmaker
):
    """The awesome profile reads the same relocated `labels.toml`."""
    monkeypatch.chdir(tmp_path)
    export = tmp_path / "export"
    export.mkdir()

    apply_labels(Config(), "owner/awesome-list", is_awesome=True, labels_dir=export)

    profiles = [call[3] for call in captured_labelmaker]
    assert profiles == ["default", "awesome"]
    assert {call[1] for call in captured_labelmaker} == {str(export / "labels.toml")}


def test_apply_labels_merges_extra_label_directories(
    tmp_path, monkeypatch, captured_labelmaker
):
    """Committed and downloaded `extra-labels/` files are both applied."""
    monkeypatch.chdir(tmp_path)
    committed = tmp_path / "extra-labels"
    committed.mkdir()
    (committed / "hand-written.toml").write_text("", encoding="UTF-8")

    export = tmp_path / "export"
    (export / "extra-labels").mkdir(parents=True)
    (export / "extra-labels" / "downloaded.toml").write_text("", encoding="UTF-8")

    apply_labels(Config(), "owner/repo", is_awesome=False, labels_dir=export)

    applied = [call[1] for call in captured_labelmaker[1:]]
    # The committed directory stays CWD-relative, the download absolute. Both
    # are built from `Path`, as the production code does, so the expectation
    # carries the native separator on every platform.
    assert applied == [
        str(export / "extra-labels" / "downloaded.toml"),
        str(Path("extra-labels") / "hand-written.toml"),
    ]


def test_apply_labels_download_shadows_committed_file_of_same_name(
    tmp_path, monkeypatch, captured_labelmaker
):
    """A download wins over a committed file of the same name, applied once."""
    monkeypatch.chdir(tmp_path)
    committed = tmp_path / "extra-labels"
    committed.mkdir()
    (committed / "shared.toml").write_text("", encoding="UTF-8")

    export = tmp_path / "export"
    (export / "extra-labels").mkdir(parents=True)
    (export / "extra-labels" / "shared.toml").write_text("", encoding="UTF-8")

    apply_labels(Config(), "owner/repo", is_awesome=False, labels_dir=export)

    applied = [call[1] for call in captured_labelmaker[1:]]
    assert applied == [str(export / "extra-labels" / "shared.toml")]
