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
The spec allows six frontmatter fields. Claude Code accepts those six plus its
own extensions, and this repository uses three of them (see
{data}`CLAUDE_CODE_EXTENSIONS`). That surplus is deliberate but not free: the
claude.ai upload path, the Skills API, and `package_skill.py` from
[anthropics/skills](https://github.com/anthropics/skills) reject an unknown key
with a hard error rather than ignoring it. Keeping the extension set small and
explicit is what makes that trade-off reviewable, and it catches a misspelled
field name (`user_invocable` for `user-invocable`) that would otherwise parse
as valid YAML and be silently ignored.
```
"""

from __future__ import annotations

import re

import pytest

from repomatic.cli import _parse_skill_frontmatter
from repomatic.init_project import export_content
from repomatic.registry import COMPONENTS_BY_NAME, _skill_target

SPEC_FIELDS = frozenset({
    "allowed-tools",
    "compatibility",
    "description",
    "license",
    "metadata",
    "name",
})
"""The six frontmatter fields the Agent Skills spec defines."""

CLAUDE_CODE_EXTENSIONS = frozenset({
    "argument-hint",
    "disable-model-invocation",
    "model",
})
"""Non-spec frontmatter fields this repository relies on.

Claude Code reads these at the top level, so they cannot move under the spec's
`metadata` escape hatch. Extend this set only when a new field earns its keep:
each one narrows where the skill can be distributed.
"""

MAX_DESCRIPTION_LENGTH = 1024
"""Spec ceiling on the `description` field."""

MAX_NAME_LENGTH = 64
"""Spec ceiling on the `name` field."""

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
    return _parse_skill_frontmatter(export_content(entry.source))


@skill_entries
def test_skill_name_matches_directory(entry):
    """`name` is required, spec-shaped, and equal to the parent directory."""
    name = frontmatter(entry).get("name")
    assert name, f"{entry.source} has no name field"
    assert len(name) <= MAX_NAME_LENGTH, f"{entry.source} name is too long"
    assert SKILL_NAME_RE.match(name), f"{entry.source} name {name!r} is malformed"
    assert entry.target == _skill_target(name), (
        f"{entry.source} name {name!r} does not match its target {entry.target}"
    )


@skill_entries
def test_skill_description_within_spec_limit(entry):
    """`description` is required and fits the spec ceiling."""
    description = frontmatter(entry).get("description")
    assert description, f"{entry.source} has no description field"
    assert len(description) <= MAX_DESCRIPTION_LENGTH, (
        f"{entry.source} description is {len(description)} characters, "
        f"over the {MAX_DESCRIPTION_LENGTH} limit"
    )


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
