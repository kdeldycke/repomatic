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
{mod}`~repomatic.plugin` module docstring), so validation alone proves nothing
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
from repomatic.plugin import (
    AGENTS_DIR,
    ARCHIVE_NAME,
    MANIFEST_PATH,
    MARKETPLACE_NAME,
    MARKETPLACE_PATH,
    MARKETPLACE_REPO,
    PLUGIN_NAME,
    SKILLS_DIR,
    merge_plugin_settings,
    pack_plugin,
    render_plugin_settings,
)
from repomatic.prepare_release import PrepareRelease
from repomatic.registry import COMPONENTS_BY_NAME

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


def _load(relative: str) -> dict[str, Any]:
    """Parse a checked-in JSON document at *relative*."""
    document: dict[str, Any] = json.loads(
        (PROJECT_ROOT / relative).read_text(encoding="UTF-8")
    )
    return document


@pytest.fixture
def manifest() -> dict[str, Any]:
    """The checked-in plugin manifest, parsed."""
    return _load(MANIFEST_PATH)


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


def test_manifest_declares_no_version(manifest) -> None:
    """The version is stamped at pack time, never committed.

    A committed value is one more thing to bump, and Claude Code treats it as the
    update signal: left stale it strands every user on the plugin they already
    have.
    """
    assert "version" not in manifest


def test_manifest_fields_are_recognized(manifest) -> None:
    """No manifest field is one Claude Code would silently ignore."""
    unknown = set(manifest) - MANIFEST_FIELDS
    assert not unknown, f"{MANIFEST_PATH} has unrecognized fields: {unknown}"


@pytest.mark.parametrize("field", ("agents", "commands", "skills"))
def test_manifest_declares_no_component_paths(manifest, field: str) -> None:
    """The manifest carries metadata only, never a component path.

    `pack_plugin` relocates every asset onto the spec's default directories
    precisely so these fields are unnecessary. Re-introducing one is a regression
    rather than a refactor: an `agents` path passes `claude plugin validate
    --strict` and then loads zero agents.
    """
    assert field not in manifest, (
        f"{MANIFEST_PATH} declares {field!r}. The archive places assets at the "
        "spec's default locations, so a component path is unnecessary here, and "
        "an `agents` path silently loads no agents at all."
    )


def test_marketplace_identity(marketplace) -> None:
    """The catalog names the marketplace and plugin the install command uses."""
    assert marketplace["name"] == MARKETPLACE_NAME
    assert [entry["name"] for entry in marketplace["plugins"]] == [PLUGIN_NAME]


def test_marketplace_declares_no_plugin_version(marketplace) -> None:
    """The entry carries no version, which the manifest's would mask anyway."""
    for entry in marketplace["plugins"]:
        assert "version" not in entry


ARCHIVE_URL_RE = re.compile(
    r"^https://github\.com/(?P<repo>[\w.-]+/[\w.-]+)"
    r"/releases/(?:latest/download|download/v\d+\.\d+\.\d+)"
    r"/(?P<asset>[\w.-]+)$"
)
"""The two shapes the marketplace archive URL is ever allowed to take.

`latest/download` is the never-yet-frozen state; `download/vX.Y.Z` is what
`PrepareRelease.freeze_marketplace_archive_url` ratchets it to on every release.
Anything else, notably a `vX.Y.Z.devN` tag that was never created, would leave
`/plugin install` broken.
"""


def test_marketplace_installs_from_the_release_archive(marketplace) -> None:
    """The single entry fetches the archive the release lane attaches.

    Ties the marketplace URL to the repository and pins it to a shape that always
    resolves, so the catalog cannot point at a `.devN` tag that was never created.
    """
    source = marketplace["plugins"][0]["source"]
    assert source["source"] == "archive"

    match = ARCHIVE_URL_RE.match(source["url"])
    assert match, (
        f"{source['url']!r} is not a release-asset URL that resolves. Expected "
        "either the /releases/latest/download/ form or a /releases/download/"
        "vX.Y.Z/ pin, never a .devN tag."
    )
    assert match["repo"] == MARKETPLACE_REPO


def test_marketplace_url_converges_on_the_current_archive_name(
    tmp_path: Path, marketplace_raw: str
) -> None:
    """The next release freeze lands the URL on {data}`ARCHIVE_NAME`.

    Asserted by running the freeze rather than by comparing against the
    checked-in filename, because the two legitimately differ for exactly one
    cycle after the asset is renamed: the URL pins the *last published* release,
    which still carries the old filename, and only the next freeze can move both
    together. Comparing the literal here would force a choice between a red test
    and a URL that 404s until the next release.
    """
    target = tmp_path / "marketplace.json"
    target.write_text(marketplace_raw, encoding="UTF-8")

    PrepareRelease(marketplace_path=target).freeze_marketplace_archive_url("9.9.9")

    match = ARCHIVE_URL_RE.match(
        json.loads(target.read_text(encoding="UTF-8"))["plugins"][0]["source"]["url"]
    )
    assert match
    assert match["asset"] == ARCHIVE_NAME


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
    (repo / ".claude-plugin").mkdir(parents=True)
    (repo / ".claude-plugin/plugin.json").write_text(
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
    with pytest.raises(FileNotFoundError, match=MANIFEST_PATH):
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
