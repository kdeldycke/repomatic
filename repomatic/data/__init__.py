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

"""Bundled data files for repomatic.

This package contains label definitions, the per-tool configuration files
`repomatic run` hands to the tools it shells out to, workflow templates, Claude
Code agent and skill definitions, and awesome-template boilerplate files.

Configuration files are stored directly in this directory. Workflow templates
are symlinked from `.github/workflows/`, agent definitions from
`.claude/agents/` and skill definitions from `.claude/skills/`, to maintain a
single source of truth while still being bundled in the package. The
`awesome_template/` sub-package contains boilerplate files for downstream
`awesome-*` repositories.

```{note}
The labelling rules are not here. They were bundled as
`labeller-content-based.yaml` and `labeller-file-based.yaml` for the two
JavaScript labeller actions to read; `apply-labels` now matches in-process, so
they live as {data}`repomatic.labels.DEFAULT_CONTENT_RULES` and
{data}`~repomatic.labels.DEFAULT_FILE_RULES`, overlaid by
`[tool.repomatic.labels]`, with no file staged anywhere.
```

All files are accessible at runtime via `importlib.resources`.
"""
