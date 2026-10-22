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

"""Conformance of the `repomatic.cli` package: its lazy table and shared plumbing.

The group imports a command module only when a run needs one of its commands,
so the table in {mod}`repomatic.cli.main` is the one place every command is
declared. These tests hold that table to the commands it names, and the help
sections to the rules that file each command.
"""

from __future__ import annotations

import ast
import inspect
import io
import json
import re
import subprocess
import sys
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import click
import pytest
from click_extra import (
    STDOUT_SENTINEL,
    ConfigOption,
    Context,
    TableFormatOption,
    VerbosityOption,
    VersionOption,
    echo,
)
from click_extra.testing import CliRunner

from repomatic.claude_plugin import ARCHIVE_NAME, pack_plugin
from repomatic.cli import main as cli_main, release as cli_release
from repomatic.cli.main import (
    COMMANDS,
    repo_option,
    repomatic,
    resolve_repo,
    subcommand,
    write_output,
)
from repomatic.config import config_key_paths
from repomatic.github import outputs
from repomatic.release.manpages import pack_manpages
from tests.conftest import PROJECT_ROOT

CLI_PACKAGE = Path(cli_main.__file__).parent

SECTION_TITLES = (
    "Project setup",
    "Linting & checks",
    "Sync",
    "Release & versioning",
    "GitHub issues & PRs",
    "CI & runners",
    "Site & docs",
    "Forge sampling",
    "Tools",
)
"""The help sections, in the order the help screen lists them."""

LOADING_PROBE = dedent("""\
    import importlib
    import json
    import sys

    from repomatic.cli.main import COMMANDS

    loaded = sorted(name for name in sys.modules if name.startswith("repomatic.cli."))
    names = {}
    for key, spec in COMMANDS.items():
        module, _, attribute = spec.import_path.rpartition(".")
        names[key] = getattr(importlib.import_module(module), attribute).name
    print(json.dumps({"loaded": loaded, "names": names}))
    """)
"""Read the lazy table from a fresh interpreter.

Loading a command through the group renames it after its table key, so only an
interpreter that has not loaded any command yet still sees the name Click
derived from the callback.
"""


def _command_modules() -> list[str]:
    """Every module the lazy table imports a command from."""
    return sorted({spec.import_path.rpartition(".")[0] for spec in COMMANDS.values()})


def _all_commands() -> list[tuple[str, click.Command]]:
    """Every command of the tree, with its path, loading each lazy one."""
    found: list[tuple[str, click.Command]] = []

    def walk(group: click.Group, prefix: str) -> None:
        ctx = Context(group)
        for name in group.list_commands(ctx):
            command = group.get_command(ctx, name)
            assert command is not None, f"{prefix}{name} does not load"
            found.append((f"{prefix}{name}", command))
            if isinstance(command, click.Group):
                walk(command, f"{prefix}{name} ")

    walk(repomatic, "")
    return found


@pytest.mark.once
def test_lazy_table_resolves_from_a_fresh_interpreter():
    """Each import path names a command whose own name is its table key.

    Importing the group loads no command module: a run imports the one module
    that holds the command it invokes, and the help screen alone imports them
    all.
    """
    result = subprocess.run(
        [sys.executable, "-c", LOADING_PROBE],
        capture_output=True,
        text=True,
        encoding="UTF-8",
        check=True,
    )
    probe = json.loads(result.stdout)
    assert probe["loaded"] == ["repomatic.cli.main"]
    mismatches = {key: name for key, name in probe["names"].items() if name != key}
    assert not mismatches, f"Command names differing from their key: {mismatches}"


@pytest.mark.parametrize("module", _command_modules())
def test_command_module_imports_no_sibling(module):
    """A command module imports `main` alone among the CLI modules.

    Importing a sibling would load its commands, and all their dependencies,
    on every run of this module's commands.
    """
    path = CLI_PACKAGE / f"{module.rpartition('.')[2]}.py"
    siblings: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="UTF-8"))):
        if not isinstance(node, ast.ImportFrom):
            continue
        # `from . import name` names modules, `from .name import x` one module.
        if node.level == 1 and node.module is None:
            siblings.update(alias.name for alias in node.names)
        elif node.level == 1 and node.module:
            siblings.add(node.module)
        elif node.module and node.module.startswith("repomatic.cli."):
            siblings.add(node.module.rpartition(".")[2])
    siblings.discard("main")
    assert not siblings, f"{module} imports sibling CLI modules: {sorted(siblings)}"


def test_every_command_is_filed_in_a_section():
    """No command of the table lands in the catch-all "Other commands" section.

    The group itself registers only the `help` command click-extra adds to
    every group: click-extra files that one in the default section.
    """
    unfiled = sorted(name for name, spec in COMMANDS.items() if spec.section is None)
    assert not unfiled, f"Commands without a help section: {unfiled}"
    assert set(repomatic.commands) - set(COMMANDS) == {"help"}


@pytest.mark.parametrize("name", sorted(COMMANDS))
def test_sync_prefix_decides_the_sync_section(name):
    """A `sync-*` command is filed under Sync, and Sync holds nothing else."""
    section = COMMANDS[name].section
    assert section is not None
    assert (section.title == "Sync") == name.startswith("sync-")


def test_help_lists_the_sections_in_declared_order():
    """The help screen lists every section, in the order the table declares."""
    # A section is falsy while none of its commands is loaded: cloup sizes it.
    declared = [
        spec.section.title for spec in COMMANDS.values() if spec.section is not None
    ]
    assert list(dict.fromkeys(declared)) == list(SECTION_TITLES)
    result = CliRunner().invoke(repomatic, ["--no-color", "--help"])
    assert result.exit_code == 0, result.output
    positions = [result.output.index(f"\n{title}:\n") for title in SECTION_TITLES]
    assert positions == sorted(positions)


def test_commands_carry_no_root_option():
    """A command takes the root options from the group, never declares its own.

    The `command` decorator of click-extra adds them to every command it
    builds, so a command declared without `subcommand` shows them all.
    """
    root_types = (ConfigOption, TableFormatOption, VerbosityOption, VersionOption)
    offenders = {
        path: [param.name for param in command.params if isinstance(param, root_types)]
        for path, command in _all_commands()
    }
    offenders = {path: names for path, names in offenders.items() if names}
    assert not offenders, f"Commands declaring root options: {offenders}"


def test_every_output_option_accepts_stdout():
    """Each `--output` takes `-` to print to stdout instead of writing a file."""
    offenders = [
        path
        for path, command in _all_commands()
        for param in command.params
        if "--output" in getattr(param, "opts", ())
        and not getattr(param.type, "allow_dash", False)
    ]
    assert not offenders, f"--output options refusing '-': {offenders}"


FEATURE_GATE_RE = re.compile(r'exit_if_disabled\(ctx, "(?P<key>[\w.-]+)"\)')
"""A command gating itself on a `[tool.repomatic]` flag, named as a literal."""

FEATURE_GATES = sorted(
    (path.name, match["key"])
    for path in CLI_PACKAGE.glob("*.py")
    for match in FEATURE_GATE_RE.finditer(path.read_text(encoding="UTF-8"))
)
"""Every `(module, key)` pair the commands gate on."""


def test_feature_gates_are_found():
    """The scan sees the gates, so the check over them proves something."""
    assert FEATURE_GATES


@pytest.mark.parametrize(("module", "key"), FEATURE_GATES)
def test_feature_gate_names_a_real_key(module: str, key: str):
    """Every key a command gates on is a `[tool.repomatic]` setting.

    `exit_if_disabled` reads the flag through `Config.is_enabled`, which raises
    on a key it does not know: a typo would break the command on its first run.
    """
    assert key in config_key_paths(), f"{module} gates on unknown key {key!r}"


def test_feature_gate_exits_cleanly_when_off(tmp_path, monkeypatch):
    """A flag switched off in `pyproject.toml` ends the command with success."""
    (tmp_path / "pyproject.toml").write_text(
        "[tool.repomatic]\ngitignore.sync = false\n", encoding="UTF-8"
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(repomatic, ["--verbosity", "INFO", "sync-gitignore"])

    assert result.exit_code == 0, result.output
    assert "gitignore.sync is disabled" in result.output
    assert not (tmp_path / ".gitignore").exists()


def test_feature_gate_precedes_repository_detection():
    """No gated command looks for its repository while Click parses options.

    A parameter callback runs before the command body. A `--repo` that detects
    the repository of the checkout would refuse to run without one, before the
    guard could exit `0` on a disabled flag.
    """
    gated = [
        (path, command)
        for path, command in _all_commands()
        if command.callback is not None
        and "exit_if_disabled(" in inspect.getsource(inspect.unwrap(command.callback))
    ]
    assert gated
    offenders = [
        path
        for path, command in gated
        if any(param.callback is cli_main._resolve_repo for param in command.params)
    ]
    assert not offenders, f"Gated commands detecting --repo first: {offenders}"


@pytest.mark.parametrize(
    ("command", "key"),
    (("sync-binaries", "binaries.sync"), ("sync-labels", "labels.sync")),
)
def test_disabled_sync_needs_no_repository(tmp_path, monkeypatch, command, key):
    """A disabled sync exits `0` with no repository to act on."""
    (tmp_path / "pyproject.toml").write_text(
        f"[tool.repomatic]\n{key} = false\n", encoding="UTF-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setattr(cli_main, "Metadata", lambda: SimpleNamespace(repo_slug=None))

    result = CliRunner().invoke(repomatic, ["--verbosity", "INFO", command])

    assert result.exit_code == 0, result.output
    assert f"{key} is disabled" in result.output


def test_write_output_closes_the_file_it_opens(tmp_path, monkeypatch):
    """The stream opened for an `--output` path is closed once written."""
    opened = []
    real_prep_path = outputs.prep_path

    def spy(path):
        stream = real_prep_path(path)
        opened.append(stream)
        return stream

    monkeypatch.setattr(outputs, "prep_path", spy)
    target = tmp_path / "harvest.md"

    write_output("harvest", "mango\nkiwi", target)

    assert target.read_text(encoding="UTF-8") == "mango\nkiwi\n"
    assert [stream.closed for stream in opened] == [True]


class _CapturedStdout(io.StringIO):
    """An in-memory stdout with no file descriptor, like a test runner's."""

    name = "<stdout>"


def test_write_output_leaves_a_captured_stdout_open(monkeypatch):
    """The live stdout `prep_path` hands back for `-` stays open for later writes."""
    capture = _CapturedStdout()
    monkeypatch.setattr(sys, "stdout", capture)

    write_output("harvest", "papaya", Path(STDOUT_SENTINEL))

    assert not capture.closed
    assert capture.getvalue() == "papaya\n"


@subcommand()
@repo_option()
def repo_probe(repo: str) -> None:
    """Print the repository `--repo` resolved to."""
    echo(repo)


@subcommand()
@repo_option(deferred=True)
def deferred_repo_probe(repo: str | None) -> None:
    """Print the repository the command body resolves `--repo` to."""
    echo(resolve_repo(repo))


REPO_PROBES = (
    pytest.param(repo_probe, id="parse"),
    pytest.param(deferred_repo_probe, id="deferred"),
)
"""A command resolving `--repo` while Click parses it, and one in its body."""


@pytest.mark.parametrize("probe", REPO_PROBES)
@pytest.mark.parametrize(
    ("args", "env", "detected", "expected"),
    (
        pytest.param(
            ["--repo", "kiwi/orchard"],
            {"GITHUB_REPOSITORY": "mango/grove"},
            "papaya/farm",
            "kiwi/orchard",
            id="option",
        ),
        pytest.param(
            [],
            {"GITHUB_REPOSITORY": "mango/grove"},
            "papaya/farm",
            "mango/grove",
            id="envvar",
        ),
        pytest.param([], {}, "papaya/farm", "papaya/farm", id="checkout"),
    ),
)
def test_repo_option_falls_back_in_order(
    monkeypatch, probe, args, env, detected, expected
):
    """`--repo`, then `$GITHUB_REPOSITORY`, then the repository of the checkout."""
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setattr(
        cli_main, "Metadata", lambda: SimpleNamespace(repo_slug=detected)
    )
    result = CliRunner().invoke(probe, args, env=env)
    assert result.exit_code == 0, result.output
    assert result.output.strip() == expected


@pytest.mark.parametrize("probe", REPO_PROBES)
def test_repo_option_refuses_to_run_without_a_repository(monkeypatch, probe):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setattr(cli_main, "Metadata", lambda: SimpleNamespace(repo_slug=None))
    result = CliRunner().invoke(probe, [])
    assert result.exit_code == 2
    assert "No repository found" in result.output


def test_upload_without_a_repository_names_the_flag(tmp_path, monkeypatch):
    """`--upload-to` with no repository fails before it packs anything."""
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "papaya-1.2.3-linux-arm64.bin").write_bytes(b"\x7fELF")
    result = CliRunner().invoke(
        repomatic,
        [
            "pack-binaries",
            "--version",
            "1.2.3",
            "--dir",
            str(dist),
            "--upload-to",
            "v1.2.3",
        ],
    )
    assert result.exit_code == 2, result.output
    assert "--upload-to needs --repo, or $GITHUB_REPOSITORY." in result.output
    assert [path.name for path in dist.iterdir()] == ["papaya-1.2.3-linux-arm64.bin"]


def test_pack_commands_print_the_archive_they_write(tmp_path, monkeypatch):
    """`--output -` prints the bytes each packer writes to a file."""
    man = tmp_path / "man"
    man.mkdir()
    (man / "papaya.1").write_bytes(b".TH papaya.1\n")
    manpages = tmp_path / "manpages.tar.gz"
    pack_manpages(man, manpages)
    plugin = tmp_path / ARCHIVE_NAME
    pack_plugin(PROJECT_ROOT, plugin)
    monkeypatch.chdir(PROJECT_ROOT)

    for args, expected in (
        (["pack-manpages", "--dir", str(man), "--output", "-"], manpages),
        (["pack-plugin", "--output", "-"], plugin),
    ):
        result = CliRunner().invoke(repomatic, args)
        assert result.exit_code == 0, result.output
        assert result.stdout_bytes == expected.read_bytes(), args[0]


@pytest.mark.parametrize(
    ("closed", "reported"),
    (([], "none"), ([12], "#12"), ([12, 15], "#12, #15")),
)
def test_refused_bump_names_the_pull_requests_it_closed(monkeypatch, closed, reported):
    """A bump the version already moved past reports each stale PR it closed."""
    monkeypatch.setattr(cli_release, "is_version_bump_allowed", lambda part: False)
    monkeypatch.setattr(cli_release, "close_stale_bump_prs", lambda part: closed)
    result = CliRunner().invoke(repomatic, ["bump-version", "--part", "minor"])
    assert result.exit_code == 0, result.output
    assert f"minor bump not allowed. Closed stale PR(s): {reported}." in result.output


@pytest.mark.parametrize(
    ("content", "warned"),
    (
        # CI creates `$GITHUB_OUTPUT` empty before each step runs.
        pytest.param("", False, id="empty"),
        pytest.param("harvest=mango\n", True, id="content"),
    ),
)
def test_show_metadata_warns_only_over_content(tmp_path, content, warned):
    target = tmp_path / "github-output"
    target.write_text(content, encoding="UTF-8")

    result = CliRunner().invoke(
        repomatic, ["show-metadata", "--output", str(target), "is_python_project"]
    )

    assert result.exit_code == 0, result.output
    assert ("Target file exists and will be overwritten." in result.output) is warned
    assert "is_python_project=" in target.read_text(encoding="UTF-8")


def test_group_publishes_the_config_it_resolved(tmp_path, monkeypatch):
    """Library helpers see the config `--config` names, not `./pyproject.toml`.

    The group callback publishes the resolved config for the duration of the
    command, then clears the slot.
    """
    custom = tmp_path / "elsewhere" / "pyproject.toml"
    custom.parent.mkdir()
    custom.write_text(
        '[tool.repomatic]\nminimum-release-age = "3 days"\n', encoding="UTF-8"
    )
    published: list = []
    monkeypatch.setattr(cli_main, "publish_config", published.append)

    result = CliRunner().invoke(repomatic, ["--config", str(custom), "cache", "path"])

    assert result.exit_code == 0, result.output
    assert published[0].minimum_release_age == "3 days"
    assert published[-1] is None


RELEASED_CHANGELOG = dedent("""\
    # Changelog

    ## [`1.0.0` (2026-03-04)](https://github.com/kiwi/orchard/compare/v0.9.0...v1.0.0)

    - Plant the first row of apple trees.
    """)
"""A changelog whose newest section is released, so `changelog` opens the next."""


@pytest.fixture
def relocated_changelog(tmp_path, monkeypatch):
    """A project keeping its changelog away from the default `changelog.md`."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.bumpversion]\ncurrent_version = "1.0.0"\n\n'
        '[tool.repomatic]\nchangelog.location = "./docs/changes.md"\n',
        encoding="UTF-8",
    )
    changelog = tmp_path / "docs" / "changes.md"
    changelog.parent.mkdir()
    changelog.write_text(RELEASED_CHANGELOG, encoding="UTF-8")
    monkeypatch.chdir(tmp_path)
    return changelog


def test_changelog_writes_in_place_at_the_configured_location(relocated_changelog):
    """The new section lands in `changelog.location`, not in `./changelog.md`."""
    result = CliRunner().invoke(repomatic, ["changelog"])

    assert result.exit_code == 0, result.output
    updated = relocated_changelog.read_text(encoding="UTF-8")
    assert "(unreleased)" in updated
    assert "compare/v1.0.0...main" in updated
    assert "Plant the first row of apple trees." in updated
    assert not (relocated_changelog.parents[1] / "changelog.md").exists()


def test_changelog_in_place_is_idempotent(relocated_changelog):
    CliRunner().invoke(repomatic, ["changelog"])
    once = relocated_changelog.read_text(encoding="UTF-8")

    result = CliRunner().invoke(repomatic, ["changelog"])

    assert result.exit_code == 0, result.output
    assert relocated_changelog.read_text(encoding="UTF-8") == once


def test_changelog_prints_on_dash_and_leaves_the_file(relocated_changelog):
    result = CliRunner().invoke(repomatic, ["changelog", "-"])

    assert result.exit_code == 0, result.output
    assert "(unreleased)" in result.output
    assert relocated_changelog.read_text(encoding="UTF-8") == RELEASED_CHANGELOG
