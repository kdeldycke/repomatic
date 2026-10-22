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

"""PEP 740 provenance of the PyPI releases a sync bumps to.

A project publishing through PyPI's trusted publishing uploads every release
from its own workflow, which attaches the provenance on its own. So a release
with no provenance, after one that carried it, was uploaded some other way:
by hand, or with a stolen API token, the shape most PyPI account takeovers
take. `sync-uv-lock` and `sync-tool-versions` hold such a bump back and report
it instead of adopting it unread.

The check fails open on a lookup PyPI does not answer: an unknown provenance
holds nothing, since a transient error is not evidence. A trusted publisher
that changes between two releases is flagged in the report and held neither:
a repository transfer or rename changes it legitimately.

The cooldown and the provenance check guard different things. The cooldown
waits for a compromise to be noticed and the release pulled. The provenance
check catches the upload that bypassed the project's pipeline, which a quiet
compromise can survive a cooldown with.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from click_extra import run_jobs

from ..pypi import ReleaseProvenance, release_provenance
from .report import ProvenanceHold

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True)
class PypiBump:
    """One version move of a PyPI package, as a sync is about to make it."""

    name: str
    """Name of the report row: a tool name, or the package name."""

    package: str
    """The PyPI project the versions belong to."""

    old: str
    """Version in place, or empty for a package the run adds."""

    new: str
    """Version the run moves to."""


@dataclass(frozen=True)
class ProvenanceReview:
    """The provenance of a run's PyPI bumps."""

    cells: dict[str, str]
    """Row name to the markdown of its "Provenance" cell."""

    holds: list[ProvenanceHold]
    """The bumps to withhold, in the order of the bumps reviewed."""

    @property
    def held(self) -> set[str]:
        """Row names of the withheld bumps."""
        return {hold.name for hold in self.holds}


def provenance_cell(new: ReleaseProvenance, old: ReleaseProvenance | None) -> str:
    """Render the "Provenance" cell of a bump to *new* from *old*.

    :param new: Provenance of the release the bump moves to.
    :param old: Provenance of the release it replaces, `None` for an added
        package.
    """
    if not new.known:
        return "_unknown_"
    if not new.attested:
        return "none"
    if not new.publisher:
        return "✅ attested"
    if old is not None and old.publisher and old.publisher != new.publisher:
        return f"⚠️ `{new.publisher}`, was `{old.publisher}`"
    return f"✅ `{new.publisher}`"


def is_regression(new: ReleaseProvenance, old: ReleaseProvenance | None) -> bool:
    """Whether a bump from *old* to *new* lost provenance.

    Both lookups must have answered: an unknown side proves nothing.
    """
    return (
        old is not None
        and old.known
        and old.attested
        and new.known
        and not new.attested
    )


def review_bumps(bumps: Sequence[PypiBump], *, jobs: int = 1) -> ProvenanceReview:
    """Look up the provenance of both ends of each bump, and pick those to hold.

    :param bumps: The PyPI version moves of the run.
    :param jobs: How many lookups run at once.
    :return: The cell of every bump, and the bumps that lost provenance.
    """
    lookups = [
        (bump.package, version)
        for bump in bumps
        for version in (bump.old, bump.new)
        if version
    ]
    answers = dict(
        zip(
            lookups,
            run_jobs(lambda item: release_provenance(*item), lookups, jobs=jobs),
        )
    )
    cells: dict[str, str] = {}
    holds: list[ProvenanceHold] = []
    for bump in bumps:
        new = answers[bump.package, bump.new]
        old = answers[bump.package, bump.old] if bump.old else None
        if is_regression(new, old):
            assert old is not None
            holds.append(ProvenanceHold(bump.name, bump.old, bump.new, old.publisher))
            logging.warning(
                f"Holding {bump.name} at {bump.old}: {bump.new} carries no"
                f" provenance, while {bump.old} was uploaded by trusted publishing"
                + (f" from {old.publisher}." if old.publisher else ".")
            )
            continue
        cells[bump.name] = provenance_cell(new, old)
    return ProvenanceReview(cells=cells, holds=holds)
