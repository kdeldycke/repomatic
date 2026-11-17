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

A project publishing through PyPI's trusted publishing uploads every file of a
release from its own workflow, which attaches the provenance on its own. So a
file with no provenance, after a release that carried it, was uploaded some
other way: by hand, or with a stolen API token, the shape most PyPI account
takeovers take. Such a token can upload a whole release, or add one file to a
release the workflow uploaded, so the check reads every file of the release.
`sync-uv-lock` and `sync-tool-versions` hold such a bump back and report it
instead of adopting it unread.

The check fails open on a lookup PyPI does not answer: an unknown provenance
holds nothing, since a transient error is not evidence. A trusted publisher
whose repository or workflow changes between two releases is flagged in the
report and held neither: a repository transfer or a workflow rename changes it
legitimately. A release where only some files carry provenance is flagged the
same way, unless the release it replaces carried provenance on every file:
that bump is held.

The cooldown and the provenance check guard different things. The cooldown
waits for a compromise to be noticed and the release pulled. The provenance
check catches the upload that bypassed the project's pipeline, which a quiet
compromise can survive a cooldown with.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from click_extra import run_jobs

from ..pypi import ReleaseProvenance, file_provenance, release_provenance
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

    A release where only some files carry provenance is flagged with its
    count, like PyPI's Security tab does. So is a trusted publisher whose
    repository or workflow differs from the one of *old*.

    :param new: Provenance of the release the bump moves to.
    :param old: Provenance of the release it replaces, `None` for an added
        package.
    """
    if not new.known:
        return "_unknown_"
    if not new.attested:
        return "none"
    if not new.fully_attested:
        return f"⚠️ {new.attested_files} of {new.files} files attested"
    if not new.publisher:
        return "✅ attested"
    if old is not None and old.publisher:
        if old.publisher != new.publisher:
            return f"⚠️ `{new.publisher}`, was `{old.publisher}`"
        if old.workflow != new.workflow:
            return f"⚠️ `{new.publisher}` via `{new.workflow}`, was `{old.workflow}`"
    return f"✅ `{new.publisher}`"


def is_regression(new: ReleaseProvenance, old: ReleaseProvenance | None) -> bool:
    """Whether a bump from *old* to *new* lost provenance.

    Two bumps lose it: to a release with no attested file from one with some,
    and to a release with a file that carries none from one attested on every
    file. The second is what a stolen token leaves when it adds a file to a
    release that the trusted publisher uploaded. Both lookups must have
    answered: an unknown side proves nothing.
    """
    if old is None or not (old.known and old.attested and new.known):
        return False
    return not new.attested or (old.fully_attested and not new.fully_attested)


def review_bumps(bumps: Sequence[PypiBump], *, jobs: int = 1) -> ProvenanceReview:
    """Look up the provenance of both ends of each bump, and pick those to hold.

    The Simple API page of a package lists the files of all its releases, so
    each package reads it once, for both ends of its bump.

    :param bumps: The PyPI version moves of the run.
    :param jobs: How many lookups run at once.
    :return: The cell of every bump, and the bumps that lost provenance.
    """
    packages = list(dict.fromkeys(bump.package for bump in bumps))
    indexes = dict(zip(packages, run_jobs(file_provenance, packages, jobs=jobs)))
    lookups = [
        (bump.package, version)
        for bump in bumps
        for version in (bump.old, bump.new)
        if version
    ]
    answers = dict(
        zip(
            lookups,
            run_jobs(
                lambda item: release_provenance(
                    *item, provenance_urls=indexes[item[0]]
                ),
                lookups,
                jobs=jobs,
            ),
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
                f"Holding {bump.name} at {bump.old}: {bump.new} has no provenance"
                f" on {new.files - new.attested_files} of its {new.files} files,"
                f" while {bump.old} carried provenance"
                + (f" from {old.publisher}." if old.publisher else ".")
            )
            continue
        cells[bump.name] = provenance_cell(new, old)
    return ProvenanceReview(cells=cells, holds=holds)
