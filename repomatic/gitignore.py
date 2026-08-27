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

"""Generate `.gitignore` content from gitignore.io templates.

Backs the `sync-gitignore` command: fetches the base template categories plus
any `[tool.repomatic] gitignore.extra-categories` from gitignore.io, then
appends `gitignore.extra-content`.
"""

from __future__ import annotations

import logging

from .http import get_text

TYPE_CHECKING = False
if TYPE_CHECKING:
    from .config import Config

GITIGNORE_BASE_CATEGORIES: tuple[str, ...] = (
    "certificates",
    "emacs",
    "git",
    "gpg",
    "linux",
    "macos",
    "node",
    "nohup",
    "python",
    "rust",
    "ssh",
    "vim",
    "virtualenv",
    "visualstudiocode",
    "windows",
)
"""Base gitignore.io template categories included in every generated `.gitignore`.

These cover common development environments, operating systems, and tools.
Downstream projects can add more via `gitignore.extra-categories` in
`[tool.repomatic]`.
"""

GITIGNORE_IO_URL = "https://www.toptal.com/developers/gitignore/api"
"""gitignore.io API endpoint for fetching `.gitignore` templates."""


def build_gitignore(config: Config) -> str:
    """Fetch and assemble the `.gitignore` content for *config*.

    Combines {data}`GITIGNORE_BASE_CATEGORIES` with the configured extra
    categories (order-preserving, deduplicated), fetches the merged template
    from gitignore.io, and appends the configured extra content.

    :param config: The resolved `[tool.repomatic]` configuration.
    :return: The full `.gitignore` text.
    :raises repomatic.http.FetchError: When the gitignore.io fetch fails.
    """
    all_categories = list(
        dict.fromkeys((*GITIGNORE_BASE_CATEGORIES, *config.gitignore.extra_categories))
    )

    url = f"{GITIGNORE_IO_URL}/{','.join(all_categories)}"
    logging.info(f"Fetching {url}")
    content = get_text(url)

    if config.gitignore.extra_content:
        content += "\n" + config.gitignore.extra_content + "\n"
    return content


def parse_rules(content: str) -> list[str]:
    """Extract the ignore rules from `.gitignore` *content*.

    Blank lines and comments are dropped, leaving only the lines git actually
    matches paths against. Order is preserved and duplicates are collapsed, so
    the result compares two files by what they ignore rather than by how they
    are laid out.

    Only a leading `#` opens a comment: git treats one anywhere else in the
    line as part of the pattern, so no inline-comment stripping happens here.

    :param content: Full text of a `.gitignore` file.
    :return: The rules, in first-seen order.
    """
    rules = (line.strip() for line in content.splitlines())
    return list(
        dict.fromkeys(rule for rule in rules if rule and not rule.startswith("#"))
    )


def orphaned_rules(existing: str, generated: str) -> list[str]:
    """Return the rules *generated* would drop from *existing*.

    `sync-gitignore` rebuilds the file from gitignore.io plus
    `[tool.repomatic.gitignore] extra-content` and never reads what is already
    on disk, so a rule added by hand survives exactly one edit: the next sync
    writes over it. Comparing the two rule sets before the write is what turns
    that silent loss into something the caller can refuse.

    :param existing: Current content of the `.gitignore` on disk.
    :param generated: Content {func}`build_gitignore` just produced.
    :return: Rules present in *existing* and absent from *generated*, in
        first-seen order. Empty when the sync drops nothing.
    """
    kept = set(parse_rules(generated))
    return [rule for rule in parse_rules(existing) if rule not in kept]
