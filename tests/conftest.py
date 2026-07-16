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

"""Shared pytest fixtures and cross-file test helpers."""

from __future__ import annotations

from io import BytesIO

import pytest

from repomatic.github.token import PatPermissionResults
from repomatic.metadata import Metadata

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing_extensions import Self


@pytest.fixture(autouse=True)
def _reset_metadata():
    """Ensure each test gets a fresh Metadata singleton.

    Resets before and after every test so that ``@cached_property`` values
    computed with one test's monkeypatched env vars never leak into another.
    """
    Metadata.reset()
    yield
    Metadata.reset()


class FakeResponse:
    """Minimal `urlopen` response double: a byte body behind a context manager.

    Stands in for the object `repomatic.http.get_json` reads, so network tests
    patch `repomatic.http.urlopen` with `return_value=FakeResponse(...)`.
    """

    def __init__(self, data: bytes) -> None:
        self._data = BytesIO(data)

    def read(self) -> bytes:
        return self._data.read()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass


def all_pass_pat_results() -> PatPermissionResults:
    """Build a PatPermissionResults where every check passes."""
    return PatPermissionResults(
        contents=(True, "Contents: token has access"),
        issues=(True, "Issues: token has access"),
        pull_requests=(True, "Pull requests: token has access"),
        vulnerability_alerts=(
            True,
            "Dependabot alerts: token has access, alerts enabled",
        ),
        workflows=(True, "Workflows: token has access"),
    )
