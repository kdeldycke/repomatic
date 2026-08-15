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

"""Distribution of the bundled skills and agents as a Claude Code plugin.

Two halves of the same story, kept together because they share the plugin's
identity constants:

- {func}`pack_plugin` assembles the zip the release engine attaches to every
  GitHub release, from the manifest and asset directories already in the tree.
- {func}`merge_plugin_settings` writes the marketplace and enablement wiring
  into a consumer's Claude Code settings, so a downstream repository can install
  the plugin instead of carrying copied skill files.

```{caution}
{func}`pack_plugin` **relocates** each asset into the spec's default `skills/`
and `agents/` directories, rather than mirroring the `.claude/` layout it reads
them from, and the manifest therefore declares no component paths at all.

That asymmetry is not a stylistic choice. A manifest naming individual agent
files (`"agents": ["./.claude/agents/qa-engineer.md", ...]`, the only form the
[published
schema](https://json.schemastore.org/claude-code-plugin-manifest.json) accepts,
since it constrains the field to paths ending in `.md`) passes `claude plugin
validate --strict` and then loads **zero** agents at runtime, silently. Naming
the directory instead fails validation outright. The default location is the only
shape that actually works, verified against Claude Code 2.1.220 by loading the
packed archive and counting components with `claude plugin details`. `skills`
does honor a custom directory, but there is no reason to keep one half on the
mechanism that misbehaves, so both travel to their defaults and the manifest
stays metadata-only.
```

```{note}
`.claude/skills/` and `.claude/agents/` remain the single source of truth: the
relocation happens only inside the archive, so there is no symlink anywhere and
no second copy of any skill in the tree. The trade-off is that the repository
root is not itself an installable plugin: test a change by packing it and
pointing `claude --plugin-dir` at the unpacked archive.
```

```{note}
The checked-in manifest carries no `version`: {func}`pack_plugin` injects the
running `__version__` into the copy it writes to the archive.
Claude Code compares that string against a user's installed copy to decide
whether an update is due, so a hand-maintained value that went stale would
silently strand everyone on the plugin they already had. Deriving it at pack time
makes it impossible to forget, and keeps the one repomatic-specific
`[[tool.bumpversion.files]]` entry out of a `[tool.bumpversion]` block that
`sync-bumpversion` regenerates from a bundled template shared with every
downstream repository.
```

```{note}
The marketplace entry is an `archive` source pointing at the release asset, and
its URL **ratchets forward**: {meth}`.PrepareRelease.freeze_marketplace_archive_url`
rewrites it to `/releases/download/v{X.Y.Z}/` on each release commit, and nothing
walks it back. So the default branch always names the newest published release,
and a catalog added at a tag installs that tag's plugin. The URL is never a
`latest` redirect except before the very first release, and never a `.devN` tag.
```

```{caution}
The entry still carries no `sha256`. The archive is byte-deterministic, so a
digest could in principle be committed alongside the pin, but only if the release
runner reproduces those bytes exactly: `ZIP_DEFLATED` output depends on the zlib
build behind CPython, and a one-byte difference would fail *every* install with
`Plugin archive integrity check failed` rather than degrading. Integrity comes
from the attestation the engine's `extra-assets` job generates instead. Switching
to `ZIP_STORED` would make a committed digest safe, at the cost of a larger asset.

Independently of that: a release that publishes without this asset breaks
`/plugin install` until the next one, which is why a failed `extra-assets` now
blocks `publish-release`.
```
"""

from __future__ import annotations

import json
import logging
import zipfile
from pathlib import Path

from . import __version__
from .registry import COMPONENTS_BY_NAME

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator

MANIFEST_PATH = ".claude-plugin/plugin.json"
"""Location of the plugin manifest.

The same path in both places it appears: relative to the repository root, where
{func}`pack_plugin` reads it, and relative to the plugin root inside the archive,
where Claude Code looks for it.
"""

MARKETPLACE_PATH = ".claude-plugin/marketplace.json"
"""Location of the marketplace catalog, relative to the repository root."""

PLUGIN_NAME = "repomatic"
"""The plugin's `name`, which namespaces every skill and agent it ships.

Users type it as `/plugin install repomatic@kdeldycke` and see it in the scoped
component names (`repomatic:qa-engineer`). Renaming it breaks every existing
install, so it lives here as a constant and is asserted against the manifest
rather than read from it.
"""

MARKETPLACE_NAME = "kdeldycke"
"""The marketplace's `name`, the catalog this plugin is published in.

Named after the owner rather than the project, so sibling repositories can be
listed in the same catalog later. Like {data}`PLUGIN_NAME`, renaming it breaks
every existing install.
"""

MARKETPLACE_REPO = "kdeldycke/repomatic"
"""Repository a consumer registers to reach {data}`MARKETPLACE_PATH`."""

ARCHIVE_NAME = "repomatic-claude-plugin.zip"
"""Filename of the release asset {func}`pack_plugin` produces.

Carries `claude` because a bare `repomatic-plugin.zip` reads backwards: packaging
names an extension after its host first (`pytest-cov`, `mdformat-gfm`), so that
filename announces a plugin *for* repomatic on a release page, which is also what
"plugin" means for the mdformat entries of
{mod}`~repomatic.tool_registry`. The name mirrors the spec's own
`.claude-plugin/` directory instead.

Also the default `--output` of `repomatic pack-plugin`, so the release job never
spells it. It still appears in `[tool.repomatic] release-assets` and in the
`release-asset-` run-artifact name the engine matches, which TOML and YAML cannot
read from here; `tests/test_workflows.py` holds all three equal.
{meth}`~repomatic.prepare_release.PrepareRelease.freeze_marketplace_archive_url`
rewrites the marketplace URL's trailing filename from here too, so a rename
reaches every consumer through one constant.
"""

ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
"""Fixed modification time stamped on every archive member.

The earliest timestamp the ZIP format can represent. Together with a sorted
member list and an explicit file mode, it makes {func}`pack_plugin`
byte-deterministic, so re-packing an unchanged tree yields an identical archive.
That matters more here than it usually would: with no `sha256` pin in the
marketplace entry, the archive's own digest is what Claude Code falls back to
for change detection.
"""

FILE_MODE = 0o644
"""Permission bits stamped on every archive member.

Stamped explicitly rather than copied from disk so the archive does not vary
with the packing runner's umask.
"""

AGENTS_DIR = "agents"
"""Directory the plugin spec scans for agent definitions, inside the plugin root."""

SKILLS_DIR = "skills"
"""Directory the plugin spec scans for skill folders, inside the plugin root."""


def _plugin_assets(repo_root: Path) -> Iterator[tuple[Path, Path]]:
    """Pair every asset the plugin ships with its path inside the archive.

    Walks the `subagents` and `skills` component registries rather than globbing
    `.claude/`, so a file the registry does not declare cannot ride along into a
    published archive. Skill entries are whole folders, which is what picks up a
    skill's optional `references/`, `scripts/` and `assets/` subdirectories once
    one grows them.

    Each asset is re-rooted from the `.claude/` layout it lives in onto the
    plugin spec's default {data}`AGENTS_DIR` and {data}`SKILLS_DIR`, for the
    reason the module docstring gives: a manifest pointing at a custom agents
    path loads nothing.

    :param repo_root: Repository root.
    :return: Iterator of `(source relative to repo_root, path inside the plugin)`.
    :raises FileNotFoundError: If a declared agent file or skill folder is absent.
    """
    for entry in COMPONENTS_BY_NAME["subagents"].files:
        source = Path(entry.target)
        if not (repo_root / source).is_file():
            msg = f"Subagent file {entry.target} is missing."
            raise FileNotFoundError(msg)
        yield source, Path(AGENTS_DIR) / source.name

    for entry in COMPONENTS_BY_NAME["skills"].files:
        skill_dir = Path(entry.target)
        if not (repo_root / skill_dir).is_dir():
            msg = f"Skill folder {entry.target} is missing."
            raise FileNotFoundError(msg)
        for path in sorted((repo_root / skill_dir).rglob("*")):
            if path.is_file():
                source = path.relative_to(repo_root)
                # `entry.target` is `<skills location>/<skill id>`, so keeping the
                # tail below it carries a skill's own subdirectories along.
                tail = source.relative_to(skill_dir)
                yield source, Path(SKILLS_DIR) / skill_dir.name / tail


def _stamped_manifest(repo_root: Path, version: str = __version__) -> bytes:
    """Read the manifest and stamp the packaged version into it.

    :param repo_root: Repository root, which is also the plugin root.
    :param version: Version string to stamp.
    :return: The manifest as UTF-8 JSON bytes, ready to write into the archive.
    :raises FileNotFoundError: If the manifest is absent.
    :raises TypeError: If the manifest is not a JSON object.
    """
    manifest_path = repo_root / MANIFEST_PATH
    if not manifest_path.is_file():
        msg = f"Plugin manifest {MANIFEST_PATH} is missing."
        raise FileNotFoundError(msg)

    manifest = json.loads(manifest_path.read_text(encoding="UTF-8"))
    if not isinstance(manifest, dict):
        msg = f"{MANIFEST_PATH} must be a JSON object."
        raise TypeError(msg)

    manifest["version"] = version
    # Sorted keys and a tab indent, so the packaged manifest reads exactly like
    # the checked-in one `format-json` maintains.
    return (json.dumps(manifest, indent="\t", sort_keys=True) + "\n").encode("UTF-8")


def pack_plugin(repo_root: Path, output: Path, version: str = __version__) -> list[str]:
    """Pack the manifest and its assets into an installable plugin archive.

    The archive holds a single top-level folder named after the plugin. That is
    one of the two layouts Claude Code accepts, and the one that makes `unzip &&
    claude --plugin-dir repomatic` work on the downloaded asset. Inside it, assets
    sit at the spec's default locations rather than the `.claude/` paths they are
    read from: see the module docstring for why.

    :param repo_root: Repository root the assets are read from.
    :param output: Destination `.zip` path. Parent directories are created.
    :param version: Version stamped into the packaged manifest.
    :return: Archive member names, sorted.
    :raises FileNotFoundError: If the manifest, an agent file or a skill folder
        is missing.
    :raises TypeError: If the manifest is not a JSON object.
    """
    payloads = {Path(MANIFEST_PATH): _stamped_manifest(repo_root, version)}
    for source, member in _plugin_assets(repo_root):
        payloads[member] = (repo_root / source).read_bytes()

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for member in sorted(payloads):
            info = zipfile.ZipInfo(f"{PLUGIN_NAME}/{member.as_posix()}")
            info.date_time = ZIP_TIMESTAMP
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = FILE_MODE << 16
            archive.writestr(info, payloads[member])

    names = [f"{PLUGIN_NAME}/{member.as_posix()}" for member in sorted(payloads)]
    logging.info(f"Packed {len(names)} files into {output}")
    return names


def _plugin_settings() -> dict[str, dict[str, object]]:
    """Build the settings fragment that enables the plugin for a repository.

    Two independent keys, both required: `extraKnownMarketplaces` registers the
    catalog, and `enabledPlugins` turns this plugin on within it. Declaring them
    in a project's `.claude/settings.json` prompts each collaborator to install
    the plugin once they trust the folder. Claude Code never installs it on their
    behalf, which is why the copied-file `skills` and `agents` components stay in
    place alongside this rather than being replaced by it.

    :return: The fragment to merge into a Claude Code settings document.
    """
    return {
        "enabledPlugins": {f"{PLUGIN_NAME}@{MARKETPLACE_NAME}": True},
        "extraKnownMarketplaces": {
            MARKETPLACE_NAME: {
                "source": {"repo": MARKETPLACE_REPO, "source": "github"},
            },
        },
    }


def render_plugin_settings(existing: str = "") -> str:
    """Merge the plugin wiring into an existing settings document.

    Only the two keys {func}`_plugin_settings` owns are touched, and within them
    only the entries this plugin and marketplace are named by: a repository's own
    permissions, hooks and any unrelated marketplace survive untouched.

    Serialized the way `format-json` wants it (tab indent, sorted keys), so
    writing the file never leaves drift for the formatter to raise a PR about.

    :param existing: Current file content, or an empty string when absent.
    :return: The merged document, newline-terminated.
    :raises TypeError: If *existing* is not a JSON object.
    """
    document: dict[str, object] = {}
    if existing.strip():
        loaded = json.loads(existing)
        if not isinstance(loaded, dict):
            msg = "Claude Code settings must be a JSON object."
            raise TypeError(msg)
        document = loaded

    for key, entries in _plugin_settings().items():
        section = document.get(key)
        # A non-object value is someone else's mistake, not something to merge
        # into: replace it rather than raising, so init still converges.
        merged = dict(section) if isinstance(section, dict) else {}
        merged.update(entries)
        document[key] = merged

    return json.dumps(document, indent="\t", sort_keys=True) + "\n"


def merge_plugin_settings(target: Path) -> bool:
    """Write the plugin wiring into *target*, creating the file if absent.

    Idempotent: re-running against an already-wired document rewrites nothing
    and returns `False`, so `repomatic init` reports it as unchanged.

    :param target: Path to the Claude Code settings file to update.
    :return: Whether the file was created or modified.
    """
    existing = target.read_text(encoding="UTF-8") if target.is_file() else ""
    merged = render_plugin_settings(existing)
    if merged == existing:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(merged, encoding="UTF-8")
    return True
