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

"""Bootstrap and sync a repository with `repomatic init`.

Every component `repomatic init` accepts is declared in
{data}`~repomatic.components.COMPONENTS`, which carries each one's description,
default scope and target paths; `repomatic init --help` lists them. That tuple
is the only roster: a list repeated here would silently fall behind it.

The package splits the run by concern:

- {mod}`~repomatic.init.run` selects the components and writes their files.
- {mod}`~repomatic.init.tool_config` merges the `[tool.*]` templates into
  `pyproject.toml`.
- {mod}`~repomatic.init.upgrade` picks the upstream pin, and moves it with
  `init --upgrade`.
- {mod}`~repomatic.init.removed_assets` finds the orphans of assets repomatic
  no longer ships.
"""
