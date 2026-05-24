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

"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from repomatic.metadata import Metadata


@pytest.fixture(autouse=True)
def _reset_metadata():
    """Ensure each test gets a fresh Metadata singleton.

    Resets before and after every test so that ``@cached_property`` values
    computed with one test's monkeypatched env vars never leak into another.
    """
    Metadata.reset()
    yield
    Metadata.reset()


@pytest.fixture(autouse=True)
def _cleanup_git_config_lock():
    """Delete a stale .git/config.lock before each test.

    ``pydriller.Git(".")`` acquires ``.git/config.lock`` in its ``__init__``
    (to set ``blame.markUnblamableLines``), then immediately releases it.
    On macOS with Python 3.14's incremental GC, the ``pydriller.Git`` object
    created by a previous test can remain alive past teardown (held by the
    ``Conf._data["git"]`` back-reference cycle), and its ``__del__`` may
    re-enter the lock path during the next test's ``Git(".")`` init, leaving
    the lock file on disk when ``rmfile()`` races.  Deleting it here is safe:
    no test holds the lock between test boundaries.

    On Windows, a parallel xdist worker may hold the lock at the moment this
    fixture runs.  A ``PermissionError`` here means the file is actively held
    — not stale — so silently skip the deletion.

    Any test that calls ``pydriller.Git(".")`` directly or indirectly (via
    ``Metadata.git``, ``Metadata.skip_binary_build``, etc.) must carry
    ``@pytest.mark.xdist_group("git")`` so it runs on the same worker as the
    git-group tests and avoids cross-worker lock conflicts.  This requires
    ``--dist=loadgroup`` (the active mode in ``[tool.pytest].addopts``);
    ``--dist=loadfile`` ignores xdist_group markers entirely.
    """
    try:
        Path(".git/config.lock").unlink(missing_ok=True)
    except PermissionError:
        pass
    yield
