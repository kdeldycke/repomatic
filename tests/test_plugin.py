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

"""Conformance of the Claude Code plugin manifest, marketplace and archive.

Claude Code ignores manifest fields it does not recognize and resolves component
paths lazily, so a typo (`agent` for `agents`) or a path that stopped existing
loads a plugin that is simply missing half its content. Nothing surfaces that at
runtime, which is what these tests are for.

```{caution}
The sharpest edge is that `claude plugin validate --strict` passes a manifest
whose `agents` path loads **zero** agents (see the
{mod}`~repomatic.tooling.plugin` module docstring), so validation alone proves nothing
about the archive being usable. {func}`test_archive_uses_the_default_component_dirs`
and {func}`test_manifest_declares_no_component_paths` are what keep the layout on
the shape that was verified to actually load.
```
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any

import pytest

from repomatic import __version__
from repomatic.registry import COMPONENTS_BY_NAME
from repomatic.release.prepare_release import PrepareRelease
from repomatic.tooling.plugin import (
    AGENTS_DIR,
    ARCHIVE_NAME,
    BIOME_DEFAULT_INDENT,
    MANIFEST_PATH,
    MARKETPLACE_NAME,
    MARKETPLACE_PATH,
    MARKETPLACE_REPO,
    PLUGIN_NAME,
    PLUGIN_ROOT,
    REPO_MANIFEST_PATH,
    SKILLS_DIR,
    _biome_json_indent,
    merge_plugin_settings,
    pack_plugin,
    render_plugin_settings,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

MANIFEST_FIELDS = frozenset({
    "$schema",
    "agents",
    "author",
    "commands",
    "dependencies",
    "description",
    "displayName",
    "homepage",
    "hooks",
    "keywords",
    "license",
    "lspServers",
    "mcpServers",
    "monitors",
    "name",
    "outputStyles",
    "repository",
    "settings",
    "skills",
    "themes",
    "userConfig",
    "version",
})
"""Top-level fields the plugin manifest schema recognizes.

Mirrors [the published
schema](https://json.schemastore.org/claude-code-plugin-manifest.json). Claude
Code silently ignores anything else, so an unknown key here is almost always a
misspelling of a real one rather than a deliberate addition.
"""

ARCHIVE_VERSION = "1.2.3"
"""Stand-in version for the packing tests, distinct from the package's own
`__version__` so a stamped manifest cannot pass by coincidence."""

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
"""A released version, bare because it names a package rather than a tag.

Both the manifest and the catalog entry carry one, and both are written by the
release freeze, so neither may hold a `.devN` suffix.
"""


def _load(relative: str) -> dict[str, Any]:
    """Parse a checked-in JSON document at *relative*."""
    document: dict[str, Any] = json.loads(
        (PROJECT_ROOT / relative).read_text(encoding="UTF-8")
    )
    return document


@pytest.fixture
def manifest() -> dict[str, Any]:
    """The checked-in plugin manifest, parsed."""
    return _load(REPO_MANIFEST_PATH)


@pytest.fixture
def marketplace() -> dict[str, Any]:
    """The checked-in marketplace catalog, parsed."""
    return _load(MARKETPLACE_PATH)


@pytest.fixture
def marketplace_raw() -> str:
    """The checked-in marketplace catalog, as text the freeze can rewrite."""
    return (PROJECT_ROOT / MARKETPLACE_PATH).read_text(encoding="UTF-8")


def test_manifest_identity(manifest) -> None:
    """The manifest names the plugin the constants and the docs promise."""
    assert manifest["name"] == PLUGIN_NAME


def test_manifest_pins_the_last_released_version(manifest) -> None:
    """The manifest names a release, written by the freeze and never by hand.

    A `git-subdir` marketplace source publishes this file as it stands, and
    `claude plugin validate --strict` fails a manifest with no version at all.
    Claude Code treats the string as the update signal, so a hand-maintained one
    left stale would strand every user on the plugin they already have.
    """
    assert VERSION_RE.match(manifest["version"])


def test_manifest_fields_are_recognized(manifest) -> None:
    """No manifest field is one Claude Code would silently ignore."""
    unknown = set(manifest) - MANIFEST_FIELDS
    assert not unknown, f"{REPO_MANIFEST_PATH} has unrecognized fields: {unknown}"


@pytest.mark.parametrize("field", ("agents", "commands", "skills"))
def test_manifest_declares_no_component_paths(manifest, field: str) -> None:
    """The manifest carries metadata only, never a component path.

    Every asset already sits at the spec's default location, precisely so these
    fields are unnecessary. Re-introducing one is a regression rather than a
    refactor: an `agents` path passes `claude plugin validate --strict` and then
    loads zero agents.
    """
    assert field not in manifest, (
        f"{REPO_MANIFEST_PATH} declares {field!r}. Every asset already sits at "
        "the spec's default location, so a component path is unnecessary here, "
        "and an `agents` path silently loads no agents at all."
    )


def test_marketplace_identity(marketplace) -> None:
    """The catalog names the marketplace and plugin the install command uses."""
    assert marketplace["name"] == MARKETPLACE_NAME
    assert [entry["name"] for entry in marketplace["plugins"]] == [PLUGIN_NAME]


MARKETPLACE_REFS = ("main", re.compile(r"^v\d+\.\d+\.\d+$"))
"""The two shapes the entry's `ref` is ever allowed to take.

The default branch is the unfrozen state, which is what lets the app's
`Sync automatically` observe a change at all. A `v`-prefixed release tag is what
`PrepareRelease.freeze_marketplace_pin` writes for the release commit, and it is
the state this test sees when the suite runs from a released tree. Never a
`vX.Y.Z.devN` tag, which was never created and would resolve against nothing.
"""


def test_marketplace_installs_from_the_plugin_root(marketplace) -> None:
    """The single entry publishes {data}`PLUGIN_ROOT` from a pinned tag.

    An `archive` source was the previous shape here, and it syncs only in the
    CLI: the claude.ai ingester rejects the type outright, which is what made
    every Desktop and Cowork marketplace sync fail. `git-subdir` is one of the
    three types it does accept, and the narrowest of them.
    """
    source = marketplace["plugins"][0]["source"]
    assert source["source"] == "git-subdir"
    assert source["url"] == f"https://github.com/{MARKETPLACE_REPO}"
    assert source["path"] == PLUGIN_ROOT

    branch, tag_re = MARKETPLACE_REFS
    assert source["ref"] == branch or tag_re.match(source["ref"]), (
        f"{source['ref']!r} is neither the default branch nor a released tag."
    )


def test_marketplace_entry_carries_a_version(marketplace) -> None:
    """The entry pins a version, which is what update detection reads.

    Bumping `ref` alone leaves the app's Update button greyed out: detection
    compares this string, not the manifest the source resolves to. Hand-writing
    it would strand everyone on the plugin they already had, so the freeze writes
    it beside the `ref` it is already moving.
    """
    for entry in marketplace["plugins"]:
        assert VERSION_RE.match(entry["version"]), (
            f"{entry['version']!r} is not a bare X.Y.Z package version."
        )


def test_marketplace_pin_moves_on_the_next_freeze(
    tmp_path: Path, marketplace_raw: str
) -> None:
    """The next release freeze lands both halves of the pin on that release.

    Asserted by running the freeze rather than by comparing against the
    checked-in values, which legitimately name the *last published* release
    rather than the version under development.
    """
    target = tmp_path / "marketplace.json"
    target.write_text(marketplace_raw, encoding="UTF-8")

    assert PrepareRelease(marketplace_path=target).freeze_marketplace_pin("9.9.9")

    entry = json.loads(target.read_text(encoding="UTF-8"))["plugins"][0]
    assert entry["source"]["ref"] == "v9.9.9"
    assert entry["version"] == "9.9.9"


def test_plugin_root_is_installable_as_it_stands(marketplace) -> None:
    """The directory the entry names really is a plugin root.

    A `git-subdir` source publishes the tree verbatim, with no packing step to
    rearrange it, so the manifest and both component directories have to be
    exactly where the spec scans for them. Moving any of the three without
    moving the marketplace `path` would ship a catalog entry resolving to a
    directory Claude Code loads nothing from.
    """
    root = PROJECT_ROOT / marketplace["plugins"][0]["source"]["path"]
    assert (root / MANIFEST_PATH).is_file()
    assert (root / AGENTS_DIR).is_dir()
    assert (root / SKILLS_DIR).is_dir()


def test_pack_plugin_layout(tmp_path: Path) -> None:
    """The archive holds one top-level folder, with the manifest inside it."""
    archive_path = tmp_path / ARCHIVE_NAME
    names = pack_plugin(PROJECT_ROOT, archive_path, ARCHIVE_VERSION)

    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.namelist()
    assert entries == names
    assert {name.split("/")[0] for name in entries} == {PLUGIN_NAME}
    assert f"{PLUGIN_NAME}/{MANIFEST_PATH}" in entries


def test_pack_plugin_ships_every_registered_asset(tmp_path: Path) -> None:
    """Exactly the manifest plus every registered agent and skill, nothing else."""
    archive_path = tmp_path / ARCHIVE_NAME
    names = set(pack_plugin(PROJECT_ROOT, archive_path, ARCHIVE_VERSION))

    wanted = {f"{PLUGIN_NAME}/{MANIFEST_PATH}"}
    for entry in COMPONENTS_BY_NAME["subagents"].files:
        name = Path(entry.target).name
        wanted.add(f"{PLUGIN_NAME}/{AGENTS_DIR}/{name}")
    for entry in COMPONENTS_BY_NAME["skills"].files:
        skill_dir = PROJECT_ROOT / entry.target
        for path in skill_dir.rglob("*"):
            if path.is_file():
                tail = path.relative_to(skill_dir).as_posix()
                wanted.add(
                    f"{PLUGIN_NAME}/{SKILLS_DIR}/{Path(entry.target).name}/{tail}"
                )
    assert names == wanted


def test_archive_uses_the_default_component_dirs(tmp_path: Path) -> None:
    """Assets land at the spec's default locations, not their `.claude/` paths.

    This is the invariant that makes the plugin actually load: a manifest pointing
    at a custom agents path validates cleanly and contributes no agents. Verified
    against Claude Code 2.1.220 with `claude plugin details`, which reported
    `Agents (0)` for the custom-path layout and `Agents (3)` for this one.
    """
    archive_path = tmp_path / ARCHIVE_NAME
    names = pack_plugin(PROJECT_ROOT, archive_path, ARCHIVE_VERSION)

    assets = [name for name in names if name != f"{PLUGIN_NAME}/{MANIFEST_PATH}"]
    assert assets, "the archive shipped no assets at all"
    for name in assets:
        tail = name.removeprefix(f"{PLUGIN_NAME}/")
        assert tail.startswith((f"{AGENTS_DIR}/", f"{SKILLS_DIR}/")), (
            f"{name} is outside the spec's default component directories"
        )
    # And nothing leaked the source layout through.
    assert not [name for name in names if "/.claude/" in name]


def test_archive_skill_folders_keep_their_subdirectories(tmp_path: Path) -> None:
    """A skill's own `references/`, `scripts/` and `assets/` travel with it.

    No bundled skill has one yet, so this covers the relocation arithmetic with a
    synthetic tree rather than waiting for the first skill to grow one and finding
    out in a release.
    """
    repo = tmp_path / "repo"
    (repo / REPO_MANIFEST_PATH).parent.mkdir(parents=True)
    (repo / REPO_MANIFEST_PATH).write_text(
        json.dumps({"name": PLUGIN_NAME}), encoding="UTF-8"
    )
    for entry in COMPONENTS_BY_NAME["subagents"].files:
        target = repo / entry.target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("---\nname: x\n---\n", encoding="UTF-8")
    for index, entry in enumerate(COMPONENTS_BY_NAME["skills"].files):
        skill_dir = repo / entry.target
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: x\n---\n", encoding="UTF-8")
        # Give exactly one skill a resource folder.
        if index == 0:
            (skill_dir / "references").mkdir()
            (skill_dir / "references/notes.md").write_text(
                "# Notes\n", encoding="UTF-8"
            )

    names = pack_plugin(repo, tmp_path / ARCHIVE_NAME, ARCHIVE_VERSION)

    first = Path(COMPONENTS_BY_NAME["skills"].files[0].target).name
    assert f"{PLUGIN_NAME}/{SKILLS_DIR}/{first}/references/notes.md" in names


def test_pack_plugin_stamps_the_version(tmp_path: Path) -> None:
    """The packaged manifest carries the version it was packed with."""
    archive_path = tmp_path / ARCHIVE_NAME
    pack_plugin(PROJECT_ROOT, archive_path, ARCHIVE_VERSION)

    with zipfile.ZipFile(archive_path) as archive:
        packed = json.loads(archive.read(f"{PLUGIN_NAME}/{MANIFEST_PATH}"))
    assert packed["version"] == ARCHIVE_VERSION


def test_pack_plugin_defaults_to_the_running_version(tmp_path: Path) -> None:
    """Omitting the version stamps the one the package reports."""
    archive_path = tmp_path / ARCHIVE_NAME
    pack_plugin(PROJECT_ROOT, archive_path)

    with zipfile.ZipFile(archive_path) as archive:
        packed = json.loads(archive.read(f"{PLUGIN_NAME}/{MANIFEST_PATH}"))
    assert packed["version"] == __version__


def test_pack_plugin_is_byte_deterministic(tmp_path: Path) -> None:
    """Re-packing an unchanged tree produces an identical archive.

    With no `sha256` pin in the marketplace entry, the archive digest is what
    Claude Code falls back to, so a nondeterministic pack would advertise an
    update on every release whether or not anything changed.
    """
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    pack_plugin(PROJECT_ROOT, first, ARCHIVE_VERSION)
    pack_plugin(PROJECT_ROOT, second, ARCHIVE_VERSION)
    assert first.read_bytes() == second.read_bytes()


def test_pack_plugin_creates_missing_parents(tmp_path: Path) -> None:
    """A destination in a directory that does not exist yet still packs."""
    archive_path = tmp_path / "nested" / "deeper" / ARCHIVE_NAME
    pack_plugin(PROJECT_ROOT, archive_path, ARCHIVE_VERSION)
    assert archive_path.is_file()


def test_pack_plugin_without_a_manifest(tmp_path: Path) -> None:
    """A tree with no manifest fails loudly instead of packing a broken plugin."""
    with pytest.raises(FileNotFoundError, match=REPO_MANIFEST_PATH):
        pack_plugin(tmp_path, tmp_path / ARCHIVE_NAME)


def test_settings_wiring_registers_and_enables() -> None:
    """A fresh document gets both the marketplace and the enablement entry."""
    document = json.loads(render_plugin_settings())
    assert document["enabledPlugins"] == {f"{PLUGIN_NAME}@{MARKETPLACE_NAME}": True}
    assert document["extraKnownMarketplaces"][MARKETPLACE_NAME] == {
        "source": {"repo": MARKETPLACE_REPO, "source": "github"},
    }


def test_settings_wiring_is_format_json_shaped() -> None:
    """Serialized with the tab indent and sorted keys `format-json` imposes."""
    rendered = render_plugin_settings()
    assert rendered.endswith("\n")
    assert '\n\t"enabledPlugins"' in rendered
    document = json.loads(rendered)
    assert list(document) == sorted(document)


@pytest.mark.parametrize(
    ("files", "expected"),
    (
        pytest.param({}, "\t", id="nothing-declared"),
        pytest.param(
            {"pyproject.toml": '[tool.biome.formatter]\nindentStyle = "tab"\n'},
            "\t",
            id="pyproject-tab",
        ),
        pytest.param(
            {
                "pyproject.toml": (
                    '[tool.biome.formatter]\nindentStyle = "space"\nindentWidth = 2\n'
                )
            },
            "  ",
            id="pyproject-two-spaces",
        ),
        pytest.param(
            {"pyproject.toml": '[tool.biome.formatter]\nindentStyle = "space"\n'},
            "  ",
            id="pyproject-spaces-default-width",
        ),
        pytest.param(
            {
                "pyproject.toml": (
                    '[tool.biome.formatter]\nindentStyle = "tab"\n'
                    '[tool.biome.json.formatter]\nindentStyle = "space"\nindentWidth = 4\n'
                )
            },
            "    ",
            id="per-language-override-wins",
        ),
        pytest.param(
            {
                "biome.json": '{"formatter": {"indentStyle": "space", "indentWidth": 8}}',
                "pyproject.toml": '[tool.biome.formatter]\nindentStyle = "tab"\n',
            },
            "        ",
            id="native-file-replaces-pyproject",
        ),
        pytest.param(
            {"biome.json": "{ // a comment no JSON parser accepts\n}"},
            "\t",
            id="unparsable-falls-back",
        ),
        pytest.param(
            {
                "biome.jsonc": (
                    '{"formatter": {"indentStyle": "space", "indentWidth": 4}}'
                ),
                "pyproject.toml": '[tool.biome.formatter]\nindentStyle = "tab"\n',
            },
            "    ",
            id="jsonc-file-replaces-pyproject",
        ),
        pytest.param(
            {
                "biome.json": '{"formatter": {"indentStyle": "space", "indentWidth": 3}}',
                "biome.jsonc": (
                    '{"formatter": {"indentStyle": "space", "indentWidth": 6}}'
                ),
            },
            "   ",
            id="registry-filename-order-decides",
        ),
        pytest.param(
            {
                "pyproject.toml": (
                    '[tool.biome.formatter]\nindentStyle = "space"\nindentWidth = 0\n'
                )
            },
            "  ",
            id="nonsense-width-falls-back",
        ),
    ),
)
def test_biome_json_indent_follows_the_declared_formatter(
    tmp_path: Path, files: dict[str, str], expected: str
) -> None:
    """The indent is read from wherever `repomatic run biome` would read it."""
    for name, content in files.items():
        (tmp_path / name).write_text(content, encoding="UTF-8")
    assert _biome_json_indent(tmp_path) == expected


@pytest.mark.parametrize(
    ("declared", "indent"),
    (
        pytest.param("", BIOME_DEFAULT_INDENT, id="biome-default"),
        pytest.param(
            '[tool.biome.formatter]\nindentStyle = "space"\nindentWidth = 2\n',
            "  ",
            id="two-spaces",
        ),
    ),
)
def test_merge_plugin_settings_converges_under_the_declared_style(
    tmp_path: Path, declared: str, indent: str
) -> None:
    """The write agrees with `format-json`, so the two cannot undo each other.

    A repository declaring spaces used to get a tab-indented document back from
    every run, which `format-json` reindented right back, leaving `sync-repomatic`
    and the autofix lane opening pull requests against each other forever.
    """
    if declared:
        (tmp_path / "pyproject.toml").write_text(declared, encoding="UTF-8")
    target = tmp_path / "dotfiles" / ".claude" / "settings.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps({"permissions": {"allow": []}}, indent=indent, sort_keys=True)
        + "\n",
        encoding="UTF-8",
    )

    assert merge_plugin_settings(target, tmp_path) is True
    written = target.read_text(encoding="UTF-8")
    assert f'\n{indent}"enabledPlugins"' in written
    # Re-running changes nothing, which is what keeps the two jobs from fighting.
    assert merge_plugin_settings(target, tmp_path) is False
    assert target.read_text(encoding="UTF-8") == written


def test_merge_plugin_settings_leaves_a_biome_formatted_document_alone(
    tmp_path: Path,
) -> None:
    """A collapsed short array is not drift, so the wiring rewrites nothing.

    `json.dumps` expands every array one item per line, while Biome collapses one
    that fits its line width. Comparing the rendered text reported a difference
    on a document `format-json` had already settled, so the two rewrote each
    other on every run. Matching the indent alone never closed that gap.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[tool.biome.formatter]\nindentStyle = "space"\nindentWidth = 2\n',
        encoding="UTF-8",
    )
    target = tmp_path / "dotfiles" / ".claude" / "settings.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {"permissions": {"allow": ["Bash(git status)"]}},
            indent="  ",
            sort_keys=True,
        )
        + "\n",
        encoding="UTF-8",
    )
    assert merge_plugin_settings(target, tmp_path) is True

    # Stand in for `format-json`, which collapses an array that fits the line.
    expanded = target.read_text(encoding="UTF-8")
    settled = re.sub(r'\[\s+("Bash\(git status\)")\s+\]', r"[\1]", expanded)
    assert settled != expanded, "fixture must differ from the rendered form"
    target.write_text(settled, encoding="UTF-8")

    assert merge_plugin_settings(target, tmp_path) is False
    assert target.read_text(encoding="UTF-8") == settled


@pytest.mark.parametrize(
    ("prior", "survivor"),
    (
        pytest.param(
            {"permissions": {"allow": ["Bash(git status)"]}},
            ("permissions",),
            id="unrelated-key",
        ),
        pytest.param(
            {"enabledPlugins": {"other@elsewhere": True}},
            ("enabledPlugins", "other@elsewhere"),
            id="another-plugin",
        ),
        pytest.param(
            {
                "extraKnownMarketplaces": {
                    "acme": {"source": {"source": "github", "repo": "acme/plugins"}}
                }
            },
            ("extraKnownMarketplaces", "acme"),
            id="another-marketplace",
        ),
    ),
)
def test_settings_wiring_preserves_existing_content(prior, survivor) -> None:
    """Merging never drops what a repository already declared."""
    existing = json.dumps(prior, indent="\t", sort_keys=True) + "\n"
    document = json.loads(render_plugin_settings(existing))

    node = document
    for key in survivor:
        assert key in node, f"{survivor} was dropped from the merged document"
        node = node[key]
    # The wiring still landed alongside it.
    assert document["enabledPlugins"][f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"] is True


def test_settings_wiring_replaces_a_non_object_section() -> None:
    """A malformed section is replaced rather than raising, so init converges."""
    existing = json.dumps({"enabledPlugins": "nonsense"}) + "\n"
    document = json.loads(render_plugin_settings(existing))
    assert document["enabledPlugins"] == {f"{PLUGIN_NAME}@{MARKETPLACE_NAME}": True}


def test_settings_wiring_rejects_a_non_object_document() -> None:
    """A settings file that is not an object is a mistake worth reporting."""
    with pytest.raises(TypeError, match="must be a JSON object"):
        render_plugin_settings("[]")


def test_merge_plugin_settings_creates_then_converges(tmp_path: Path) -> None:
    """First call writes the file, the second is a no-op."""
    target = tmp_path / ".claude" / "settings.json"
    assert merge_plugin_settings(target) is True
    written = target.read_text(encoding="UTF-8")
    assert merge_plugin_settings(target) is False
    assert target.read_text(encoding="UTF-8") == written
