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

"""The bundled Claude Code skills, as `list-skills` and the launchers show them.

The `skills` component of {data}`~repomatic.components.COMPONENTS` declares
which skills ship and where they land. This module reads what each skill says
about itself, its frontmatter, for the two places a person meets the skills: the
`list-skills` table, and the launcher command a closing hint or a pull request
body hands over.
"""

from __future__ import annotations

import re

from click_extra import ColumnSpec

from .bundle import get_data_content
from .components import COMPONENTS_BY_NAME, SKILL_FILENAME
from .frontmatter import split_frontmatter

AUDIT_SKILL: str = "repomatic-audit"
"""The bundled skill auditing a repository against the release it pins."""

RECOMMENDED_MODEL_RE = re.compile(r"Recommended model: (?P<model>\w+)\.")
"""The model recommendation a skill's `compatibility` field carries.

The Agent Skills spec has no `model:` field, so the recommendation rides in
`compatibility` as prose, and this is the one shape every bundled skill
writes it in. `tests/test_skills.py` holds each skill to it.
"""

SKILL_LIST_HEADER_DEFS: tuple[ColumnSpec, ...] = (
    ColumnSpec("phase", "Phase"),
    ColumnSpec("skill", "Skill"),
    ColumnSpec("description", "Description", max_width=60),
)
"""Column definitions for the `repomatic list-skills` table.

Lives beside {func}`skill_catalog`, whose triples these columns render, so the
two cannot drift apart. The columns mirror the hand-maintained roster of
`docs/agent-skills.md`, which the page renders this command right above.
"""

SKILL_PHASE_ORDER: tuple[str, ...] = (
    "Setup",
    "Development",
    "Quality",
    "Maintenance",
    "Release",
)
"""Canonical display order for lifecycle phases in `list-skills` output."""

SKILL_PHASES: dict[str, str] = {
    entry.file_id: entry.phase for entry in COMPONENTS_BY_NAME["skills"].files
}
"""Maps skill names to lifecycle phases for display grouping."""

UPGRADE_SKILL: str = "repomatic-upgrade"
"""The bundled skill reviewing what a newer upstream release lets a repo adopt."""


def _skill_frontmatter(skill_id: str) -> dict[str, object]:
    """Read the frontmatter of the bundled skill *skill_id*.

    :raises KeyError: When the registry ships no skill of that name.
    """
    entry = COMPONENTS_BY_NAME["skills"].entry(skill_id)
    if entry is None:
        msg = f"No bundled skill is named {skill_id!r}."
        raise KeyError(msg)
    # Each skill is a bundled folder, so reach past it for the entry point.
    meta, _body = split_frontmatter(
        get_data_content(f"{entry.source}/{SKILL_FILENAME}")
    )
    return meta


def skill_catalog() -> list[tuple[str, str, str]]:
    """Read every bundled skill's display metadata off its frontmatter.

    :return: One `(phase, name, description)` tuple per bundled skill, in
        {data}`SKILL_PHASE_ORDER`, then in registry order within a phase. The
        description's trailing period is stripped for table display. Phases
        are keyed by the registry `file_id`, not the frontmatter name, so a
        skill renamed in frontmatter still lands in its phase.
    """
    skills = []
    for skill_id, phase in SKILL_PHASES.items():
        meta = _skill_frontmatter(skill_id)
        name = str(meta.get("name", skill_id))
        description = str(meta.get("description", "")).removesuffix(".")
        skills.append((phase, name, description))
    # A stable sort, so the registry order holds within each phase.
    return sorted(skills, key=lambda skill: SKILL_PHASE_ORDER.index(skill[0]))


def skill_launcher(skill_id: str, *arguments: str) -> str:
    """The shell command launching one bundled skill in a fresh Claude Code session.

    Rendered wherever the toolkit hands a maintainer a next step to run
    (`init`'s closing hint, a sync pull request body), so the invocation is
    spelled once. The model comes off the skill's own `compatibility` field,
    so a changed recommendation never leaves a stale `--model` behind.

    ```{note}
    Claude Code is the only agent the launcher targets today. The skill folder
    itself follows the Agent Skills spec and loads in any agent honouring it;
    only this one line is agent-specific, which keeps a second launcher a
    config key away rather than a rewrite.
    ```

    :param skill_id: The registry `file_id` of a bundled skill.
    :param arguments: Extra words passed to the skill as `$ARGUMENTS`.
    :return: The command line, ready to paste into a shell.
    :raises KeyError: When the registry ships no skill named *skill_id*.
    """
    meta = _skill_frontmatter(skill_id)
    match = RECOMMENDED_MODEL_RE.search(str(meta.get("compatibility", "")))
    model = f" --model {match['model'].lower()}" if match else ""
    prompt = " ".join((f"/{skill_id}", *arguments))
    return f"claude{model} '{prompt}'"
