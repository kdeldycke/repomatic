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

"""Tests for `repomatic.release.manpages`: the man-page release tarball."""

from __future__ import annotations

import tarfile

import pytest
from click_extra.testing import CliRunner

from repomatic.cli.main import repomatic
from repomatic.release.manpages import pack_manpages


@pytest.fixture
def man_dir(tmp_path):
    """A rendered `man/` tree: one page per command of a small CLI."""
    pages = tmp_path / "man"
    pages.mkdir()
    for name in ("papaya.1", "papaya-peel.1", "papaya-slice.1"):
        (pages / name).write_bytes(f".TH {name}\n".encode())
    return pages


def test_pack_manpages_keeps_the_directory_prefix(man_dir, tmp_path):
    """Members unpack into the `man/` tree the release job rendered."""
    archive = tmp_path / "papaya-manpages.tar.gz"

    members = pack_manpages(man_dir, archive)

    assert members == ["man/papaya-peel.1", "man/papaya-slice.1", "man/papaya.1"]
    with tarfile.open(archive) as tar:
        assert tar.getnames() == members
        for info in tar.getmembers():
            assert (info.mtime, info.uid, info.gid, info.mode) == (0, 0, 0, 0o644)
        page = tar.extractfile("man/papaya.1")
        assert page is not None
        assert page.read() == b".TH papaya.1\n"


def test_pack_manpages_is_byte_deterministic(man_dir, tmp_path):
    """Packing an unchanged tree twice gives the same bytes, and digest."""
    first, second = tmp_path / "first.tar.gz", tmp_path / "second.tar.gz"
    pack_manpages(man_dir, first)
    (man_dir / "papaya.1").touch()
    pack_manpages(man_dir, second)
    assert first.read_bytes() == second.read_bytes()


def test_pack_manpages_refuses_an_empty_directory(tmp_path):
    (tmp_path / "man").mkdir()
    with pytest.raises(ValueError, match="holds no man page"):
        pack_manpages(tmp_path / "man", tmp_path / "out.tar.gz")


def test_pack_manpages_cli_names_the_tarball_after_the_asset(
    man_dir, tmp_path, monkeypatch
):
    """The tarball takes the `manpages_asset_name` metadata key as its stem."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "papaya"\nversion = "1.0.0"\n', encoding="UTF-8"
    )

    result = CliRunner().invoke(repomatic, ["pack-manpages"])

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "papaya-manpages.tar.gz"
    with tarfile.open(tmp_path / "papaya-manpages.tar.gz") as tar:
        assert len(tar.getnames()) == 3


@pytest.mark.parametrize(
    ("args", "exit_code", "message"),
    (
        pytest.param(
            [],
            2,
            "No asset name: pass --output, or set manpages.asset-name",
            id="no-asset-name",
        ),
        pytest.param(
            ["--output", "papaya-manpages.tar.gz"],
            1,
            "holds no man page to pack.",
            id="no-page",
        ),
    ),
)
def test_pack_manpages_cli_writes_no_tarball_it_cannot_fill(
    tmp_path, monkeypatch, args, exit_code, message
):
    """A project with no package name, or a `man/` holding no page, packs nothing."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "man").mkdir()

    result = CliRunner().invoke(repomatic, ["pack-manpages", *args])

    assert result.exit_code == exit_code
    assert message in result.output
    assert list(tmp_path.glob("*.tar.gz")) == []
