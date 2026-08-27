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

"""Conformance of bundled skills to the Agent Skills specification.

Every bundled `SKILL.md` is checked against the [Agent Skills
spec](https://agentskills.io/specification), so a new or edited skill cannot
silently drift out of it.

```{note}
The spec allows six frontmatter fields, and `argument-hint` is the single
deliberate deviation (see {data}`CLAUDE_CODE_EXTENSIONS`). Anything Claude Code
could express through another of its own extensions belongs in a spec field
instead: the recommended model rides in `compatibility` rather than a `model:`
key, which is why {func}`test_skill_compatibility_carries_the_model_hint`
exists. Pinning the extension set to exactly one entry is what stops that
budget creeping back, and it catches a misspelled field name
(`user_invocable` for `user-invocable`) that would otherwise parse as valid
YAML and be silently ignored.
```
"""

from __future__ import annotations

import re
from importlib.resources import files
from pathlib import Path

import pytest

from repomatic.frontmatter import split_frontmatter
from repomatic.init_project import _copy_template_tree
from repomatic.registry import (
    COMPONENTS_BY_NAME,
    SKILL_FILENAME,
    _skill_dir,
    skill_catalog,
)
from repomatic.tooling.bundle import get_data_content

SKILLS_PAGE = Path(__file__).parent.parent / "docs" / "agent-skills.md"
"""The hand-maintained page whose table must mirror the skill registry."""

SKILL_TABLE_ROW_RE = re.compile(r"^\|[^|]*\|\s*\[`/([a-z0-9-]+)`\]", re.MULTILINE)
"""The skill name out of a table row, read from the linked `/name` cell."""

SPEC_FIELDS = frozenset({
    "allowed-tools",
    "compatibility",
    "description",
    "license",
    "metadata",
    "name",
})
"""The six frontmatter fields the Agent Skills spec defines."""

CLAUDE_CODE_EXTENSIONS = frozenset({"argument-hint"})
"""The only non-spec frontmatter field bundled skills are allowed to carry.

Kept because no spec field expresses an autocomplete hint, and it degrades to
a no-op wherever it is not understood. Do not grow this set: reach for a spec
field first, the way `compatibility` now carries what `model:` used to.
"""

MAX_COMPATIBILITY_LENGTH = 500
"""Spec ceiling on the `compatibility` field."""

MAX_DESCRIPTION_LENGTH = 1024
"""Spec ceiling on the `description` field."""

MAX_NAME_LENGTH = 64
"""Spec ceiling on the `name` field."""

MODEL_HINT_RE = re.compile(r"Recommended model: \w+\.")
"""Shape of the model recommendation carried by `compatibility`."""

SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
"""Spec charset for `name`: lowercase alphanumerics and single inner hyphens.

The grouping enforces the three separate prohibitions at once: no leading
hyphen, no trailing hyphen, and no consecutive hyphens.
"""

skill_entries = pytest.mark.parametrize(
    "entry",
    COMPONENTS_BY_NAME["skills"].files,
    ids=lambda entry: entry.file_id,
)


def frontmatter(entry):
    """Parse the frontmatter of a bundled skill registry entry."""
    meta, _body = split_frontmatter(
        get_data_content(f"{entry.source}/{SKILL_FILENAME}")
    )
    return meta


@pytest.fixture
def exported_skill(tmp_path):
    """A synthetic bundled skill, already exported once to its destination.

    Stands in for a downstream repo right after `repomatic init skills`: the
    destination holds a pristine copy of the bundle.

    :return: The `(source, dest)` folder pair.
    """
    source = tmp_path / "bundled" / "papaya-report"
    source.mkdir(parents=True)
    (source / SKILL_FILENAME).write_text(
        "---\nname: papaya-report\ndescription: Chart papaya harvests.\n---\n",
        encoding="UTF-8",
    )
    dest = tmp_path / "out" / "papaya-report"
    _copy_template_tree(source, dest)
    return source, dest


@skill_entries
def test_skill_is_a_folder_holding_a_skill_md(entry):
    """The spec's unit of distribution: a folder entered through `SKILL.md`.

    Registering the folder rather than the file is what lets a skill grow
    `scripts/`, `references/` or `assets/` without touching the registry.
    """
    assert entry.tree, f"{entry.source} is not registered as a folder"
    root = files("repomatic.data").joinpath(entry.source)
    assert root.is_dir(), f"{entry.source} is not a directory"
    names = {child.name for child in root.iterdir()}
    assert SKILL_FILENAME in names, (
        f"{entry.source} has no {SKILL_FILENAME}: {sorted(names)}"
    )


@skill_entries
def test_skill_name_matches_directory(entry):
    """`name` is required, spec-shaped, and equal to the parent directory."""
    name = frontmatter(entry).get("name")
    assert name, f"{entry.source} has no name field"
    assert isinstance(name, str), f"{entry.source} name is not a string"
    assert len(name) <= MAX_NAME_LENGTH, f"{entry.source} name is too long"
    assert SKILL_NAME_RE.match(name), f"{entry.source} name {name!r} is malformed"
    assert entry.target == _skill_dir(name), (
        f"{entry.source} name {name!r} does not match its target {entry.target}"
    )


@skill_entries
def test_skill_description_within_spec_limit(entry):
    """`description` is required and fits the spec ceiling."""
    description = frontmatter(entry).get("description")
    assert description, f"{entry.source} has no description field"
    assert isinstance(description, str), f"{entry.source} description is not a string"
    assert len(description) <= MAX_DESCRIPTION_LENGTH, (
        f"{entry.source} description is {len(description)} characters, "
        f"over the {MAX_DESCRIPTION_LENGTH} limit"
    )


@skill_entries
def test_skill_compatibility_carries_the_model_hint(entry):
    """Every skill names its recommended model in `compatibility`.

    `model:` is a Claude Code extension the spec does not define, so the hint
    rides in a spec field instead of a bespoke key or a line of body prose:
    structured, greppable, and portable to any client that reads the spec.
    """
    compatibility = frontmatter(entry).get("compatibility")
    assert compatibility, f"{entry.source} has no compatibility field"
    assert MODEL_HINT_RE.search(compatibility), (
        f"{entry.source} names no recommended model: {compatibility!r}"
    )


@skill_entries
def test_skill_optional_spec_fields_are_well_formed(entry):
    """`compatibility` and `metadata` hold what the spec says they hold.

    No bundled skill sets `metadata` today, so that half guards the shape a
    future one would have to honour rather than an invariant already in play.
    """
    meta = frontmatter(entry)
    compatibility = meta.get("compatibility")
    if compatibility is not None:
        assert isinstance(compatibility, str), (
            f"{entry.source} compatibility is not a string"
        )
        assert len(compatibility) <= MAX_COMPATIBILITY_LENGTH, (
            f"{entry.source} compatibility is {len(compatibility)} characters, "
            f"over the {MAX_COMPATIBILITY_LENGTH} limit"
        )
    metadata = meta.get("metadata")
    if metadata is not None:
        assert isinstance(metadata, dict), f"{entry.source} metadata is not a map"
        assert all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in metadata.items()
        ), f"{entry.source} metadata is not a string-to-string map"


@skill_entries
def test_skill_allowed_tools_are_space_separated(entry):
    """`allowed-tools` uses the spec's space-separated string.

    Claude Code also accepts a comma-separated string and a YAML list, so a
    comma here still works locally while failing the spec.
    """
    allowed_tools = frontmatter(entry).get("allowed-tools")
    if allowed_tools is None:
        pytest.skip("skill declares no allowed-tools")
    assert isinstance(allowed_tools, str), (
        f"{entry.source} allowed-tools is not a string"
    )
    assert "," not in allowed_tools, (
        f"{entry.source} allowed-tools is comma-separated: {allowed_tools!r}"
    )


@skill_entries
def test_skill_frontmatter_fields_are_known(entry):
    """No field beyond the spec's six and the extensions we opted into."""
    unknown = set(frontmatter(entry)) - SPEC_FIELDS - CLAUDE_CODE_EXTENSIONS
    assert not unknown, f"{entry.source} has unknown frontmatter fields: {unknown}"


def test_skill_resource_folders_are_copied_verbatim(tmp_path):
    """`scripts/`, `references/` and `assets/` travel with a skill.

    No bundled skill ships resources yet, so this exercises the contract on a
    synthetic folder: whatever sits beside `SKILL.md` is copied as-is, with no
    per-file registration, and re-running changes nothing.
    """
    source = tmp_path / "bundled" / "papaya-report"
    for subdir in ("assets", "references", "scripts"):
        (source / subdir).mkdir(parents=True)
    (source / SKILL_FILENAME).write_text(
        "---\nname: papaya-report\ndescription: Chart papaya harvests.\n---\n",
        encoding="UTF-8",
    )
    (source / "references/REFERENCE.md").write_text("Yields.\n", encoding="UTF-8")
    (source / "scripts/harvest.sh").write_text("echo papaya\n", encoding="UTF-8")
    (source / "assets/template.md").write_text("Template.\n", encoding="UTF-8")

    dest = tmp_path / "out" / "papaya-report"
    created, updated = _copy_template_tree(source, dest)

    assert sorted(
        p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()
    ) == [
        "SKILL.md",
        "assets/template.md",
        "references/REFERENCE.md",
        "scripts/harvest.sh",
    ]
    assert len(created) == 4
    assert updated == []
    assert _copy_template_tree(source, dest) == ([], [])


def test_drifted_skill_is_overwritten(exported_skill):
    """A downstream copy that fell behind the bundle is reconciled.

    Skills are copied verbatim, with no user-modified heuristic: content that
    differs from the bundle is rewritten, so a downstream repo picks up new
    frontmatter on its next `repomatic init`. Adding a leave-what-is-there
    guard here would strand every downstream repo on whichever revision it
    first received, which is why this is pinned rather than left to the
    idempotency check above (identical content exercises neither branch).
    """
    source, dest = exported_skill
    bundled = (source / SKILL_FILENAME).read_text(encoding="UTF-8")
    skill = dest / SKILL_FILENAME
    skill.write_text(
        "---\nname: papaya-report\ndescription: Old harvest charts.\n---\n",
        encoding="UTF-8",
    )

    created, updated = _copy_template_tree(source, dest)

    assert created == []
    assert updated == [skill]
    assert skill.read_text(encoding="UTF-8") == bundled


def test_local_files_beside_a_skill_survive(exported_skill):
    """Re-exporting creates and overwrites, but never prunes.

    Wiping whatever is not bundled would be an easy way to reconcile a drifted
    folder, and would take a downstream repo's own files with it. Retiring a
    bundled asset goes through a `REMOVED_ASSETS` tombstone instead.
    """
    source, dest = exported_skill
    local = dest / "harvest-notes.md"
    local.write_text("Papaya yields, local notes.\n", encoding="UTF-8")

    assert _copy_template_tree(source, dest) == ([], [])
    assert local.read_text(encoding="UTF-8") == "Papaya yields, local notes.\n"


def test_docs_table_lists_every_bundled_skill():
    """`docs/agent-skills.md` tabulates the registry, and nothing else.

    The page renders `list-skills` live right above the table, so a skill
    added to the registry shows up in the rendered output while the
    hand-maintained table below it keeps the old roster. Adding
    `repomatic-test-matrix` left exactly that gap, and only a reader
    comparing the two halves of one page would have caught it. Names only:
    the column padding belongs to `mdformat`.
    """
    documented = set(
        SKILL_TABLE_ROW_RE.findall(SKILLS_PAGE.read_text(encoding="UTF-8"))
    )
    bundled = {name for _phase, name, _description in skill_catalog()}
    assert documented == bundled, (
        f"docs/agent-skills.md table drifted from the registry. "
        f"Missing rows: {sorted(bundled - documented) or 'none'}. "
        f"Rows with no bundled skill: {sorted(documented - bundled) or 'none'}."
    )
