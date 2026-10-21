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

"""Group the contributor identities of the git history into a `.mailmap`.

Backs the `sync-mailmap` command: parses the existing mappings as the
reference for identities already grouped, reads every author and committer the
history holds, and appends whatever is not covered yet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .git_ops import list_contributor_identities

MAILMAP_PATH = Path(".mailmap")
"""Canonical path to the `.mailmap` file in the repository root."""


@dataclass
class Record:
    """A mailmap identity mapping entry."""

    canonical: str = ""
    """The identity every alias maps to, in `Name <email>` form."""

    aliases: set[str] = field(default_factory=set)
    """The other identities of the same contributor."""

    pre_comment: str = ""
    """The comment and blank lines written above the mapping."""

    def __post_init__(self) -> None:
        """Drop a pre-comment made of blank lines only."""
        if not self.pre_comment.strip():
            self.pre_comment = ""

    @property
    def identities(self) -> frozenset[str]:
        """Every identity of the record, lowercased.

        Identity matching is case insensitive, per the [mailmap
        syntax](https://git-scm.com/docs/gitmailmap#_syntax).
        """
        return frozenset(i.lower() for i in (self.canonical, *self.aliases) if i)

    @property
    def mapping(self) -> str:
        """The mapping line: the canonical identity, then the sorted aliases.

        The aliases sort without case sensitivity. The canonical identity
        keeps its place in front.
        """
        return " ".join((self.canonical, *sorted(self.aliases, key=str.casefold)))

    def __str__(self) -> str:
        """Render the pre-comment first, followed by the identity mapping."""
        lines = []
        if self.pre_comment:
            lines.append(self.pre_comment)
        if self.canonical:
            lines.append(self.mapping)
        return "\n".join(lines)


class Mailmap:
    """Helpers to manipulate `.mailmap` files.

    `.mailmap` [file format is documented on Git website](https://git-scm.com/docs/gitmailmap).
    """

    records: list[Record]

    def __init__(self) -> None:
        """Initialize the mailmap with an empty list of records."""
        self.records = []

    @staticmethod
    def split_identities(mapping: str) -> tuple[str, set[str]]:
        """Split a mapping of identities and normalize them."""
        identities = []
        for identity in map(str.strip, mapping.split(">")):
            # Skip blank strings produced by uneven spaces.
            if not identity:
                continue
            assert identity.count("<") == 1, f"Unexpected email format in {identity!r}"
            name, email = identity.split("<", maxsplit=1)
            identities.append(f"{name.strip()} <{email}>")

        assert len(identities), f"No identities found in {mapping!r}"

        identities = list(dict.fromkeys(identities))
        return identities[0], set(identities[1:])

    def parse(self, content: str) -> None:
        """Parse mailmap content and add it to the current list of records.

        Each non-empty, non-comment line is considered a mapping entry.

        The preceding lines of a mapping entry are kept attached to it as pre-comments,
        so the layout will be preserved on rendering, during which records are sorted.
        """
        logging.debug(f"Parsing:\n{content}")
        pre_lines = []
        for line in map(str.strip, content.splitlines()):
            # Comment lines are added as-is.
            if line.startswith("#") or not line:
                pre_lines.append(line)
            # Mapping entry, which mark the end of a block, so add it to the list
            # mailmap records.
            else:
                canonical, aliases = self.split_identities(line)
                record = Record(
                    pre_comment="\n".join(pre_lines),
                    canonical=canonical,
                    aliases=aliases,
                )
                logging.debug(record)
                pre_lines = []
                self.records.append(record)

    def update_from_git(self) -> None:
        """Add to internal records all missing contributors found in commit history.

        No normalization happens: all variations of authors and committers
        strings attached to all commits are considered. A contributor already
        registered, as a canonical identity or as an alias, is not added again.

        :raises RuntimeError: When git fails, carrying its stderr.
        """
        contributors = list_contributor_identities()
        logging.debug(
            "Authors and committers found in Git history:\n"
            + "\n".join(sorted(contributors, key=str.casefold))
        )
        known = {identity for record in self.records for identity in record.identities}
        # Sorted, so the first of two identities differing only in case is
        # the same on every run.
        for contributor in sorted(contributors, key=lambda c: (c.casefold(), c)):
            if contributor.lower() in known:
                logging.debug(f"Ignore existing identity {contributor}")
                continue
            record = Record(canonical=contributor)
            logging.info(f"Add new identity {record}")
            self.records.append(record)
            known |= record.identities

    def render(self) -> str:
        """Render internal records in Mailmap format.

        The records sort by canonical identity, without case sensitivity. The
        pre-comment of the first parsed record is the comment that opens the
        file, so it stays at the top, whichever record sorts first.
        """
        if not self.records:
            return ""
        first = self.records[0]
        lines = [first.pre_comment] if first.pre_comment else []
        for record in sorted(self.records, key=lambda r: r.canonical.casefold()):
            lines.append(record.mapping if record is first else str(record))
        return "\n".join(lines)
