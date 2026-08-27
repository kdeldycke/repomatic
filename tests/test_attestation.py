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

"""Tests for the attestation bundle naming rule."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from repomatic.release.attestation import (
    ATTESTATION_SUFFIX,
    bundle_filename,
    bundle_subjects,
    pack_attestation,
)


def write_bundle(path: Path, *subjects: str) -> Path:
    """Write a sigstore bundle attesting *subjects*.

    Mirrors the shape `actions/attest` emits: a DSSE envelope whose base64
    payload is an in-toto statement listing one entry per attested file.
    """
    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "predicateType": "https://slsa.dev/provenance/v1",
        "subject": [
            {"name": name, "digest": {"sha256": f"{index:064x}"}}
            for index, name in enumerate(subjects)
        ],
    }
    payload = base64.b64encode(json.dumps(statement).encode("UTF-8")).decode("ascii")
    path.write_text(
        json.dumps({
            "mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
            "dsseEnvelope": {
                "payload": payload,
                "payloadType": "application/vnd.in-toto+json",
            },
        }),
        encoding="UTF-8",
    )
    return path


def test_bundle_subjects_reads_every_name(tmp_path):
    """Subjects come back in the order the statement lists them."""
    bundle = write_bundle(tmp_path / "b.json", "papaya.tar.gz", "mango.zip")
    assert bundle_subjects(bundle) == ("papaya.tar.gz", "mango.zip")


@pytest.mark.parametrize(
    ("content", "message"),
    (
        ("not json at all", "not valid JSON"),
        ('{"mediaType": "x"}', "carries no DSSE envelope payload"),
        ('{"dsseEnvelope": {"payload": "!!!"}}', "unreadable payload"),
    ),
)
def test_bundle_subjects_rejects_malformed(tmp_path, content, message):
    """A file that is not a readable bundle fails with a specific message."""
    bundle = tmp_path / "b.json"
    bundle.write_text(content, encoding="UTF-8")
    with pytest.raises(ValueError, match=message):
        bundle_subjects(bundle)


def test_bundle_subjects_rejects_empty_subject_list(tmp_path):
    """A bundle attesting nothing has no name to give."""
    bundle = write_bundle(tmp_path / "b.json")
    with pytest.raises(ValueError, match="attests no subject"):
        bundle_subjects(bundle)


@pytest.mark.parametrize("name", ("../papaya.zip", "nested/papaya.zip"))
def test_bundle_subjects_rejects_path_subject(tmp_path, name):
    """A subject naming a path could escape the asset directory."""
    bundle = write_bundle(tmp_path / "b.json", name)
    with pytest.raises(ValueError, match="not a bare filename"):
        bundle_subjects(bundle)


@pytest.mark.parametrize(
    ("subjects", "set_name", "expected"),
    (
        # A lone subject lends the bundle its whole filename, extension kept,
        # so the two sort together on an alphabetically listed release page.
        (("papaya.tar.gz",), None, "papaya.tar.gz.attestation.json"),
        (
            ("papaya-1.2.3-linux-arm64.bin",),
            None,
            "papaya-1.2.3-linux-arm64.bin.attestation.json",
        ),
        # A set name is ignored when there is a subject to name it after.
        (("papaya.zip",), "mango-set", "papaya.zip.attestation.json"),
        # Several subjects share one bundle, which takes the set name.
        (
            ("papaya.zip", "mango.zip"),
            "fruit-extra-assets",
            "fruit-extra-assets.attestation.json",
        ),
    ),
)
def test_bundle_filename(subjects, set_name, expected):
    """One subject names the bundle; several fall back to the set name."""
    assert bundle_filename(subjects, set_name) == expected


def test_bundle_filename_needs_a_set_name_for_several_subjects():
    """No single filename can claim a bundle covering several assets."""
    with pytest.raises(ValueError, match="pass --name"):
        bundle_filename(("papaya.zip", "mango.zip"))


def test_bundle_filename_rejects_no_subject():
    """Naming a bundle that attests nothing is a programming error."""
    with pytest.raises(ValueError, match="attests no subject"):
        bundle_filename(())


def test_pack_attestation_names_bundle_and_lists_uploads(tmp_path):
    """The bundle lands beside its subject, and both are listed for upload."""
    (tmp_path / "papaya.tar.gz").write_bytes(b"tarball")
    bundle = write_bundle(tmp_path / "raw.json", "papaya.tar.gz")

    uploads = pack_attestation(bundle, tmp_path)

    assert [path.name for path in uploads] == [
        "papaya.tar.gz",
        "papaya.tar.gz.attestation.json",
    ]
    named = tmp_path / f"papaya.tar.gz{ATTESTATION_SUFFIX}"
    assert named.read_bytes() == bundle.read_bytes()
    # Idempotent: a second run copies the same bytes over the same name.
    assert pack_attestation(bundle, tmp_path) == uploads


def test_pack_attestation_covers_a_whole_set(tmp_path):
    """Several subjects yield one set-named bundle and every asset."""
    (tmp_path / "papaya.zip").write_bytes(b"one")
    (tmp_path / "mango.zip").write_bytes(b"two")
    bundle = write_bundle(tmp_path / "raw.json", "papaya.zip", "mango.zip")

    uploads = pack_attestation(bundle, tmp_path, "fruit-extra-assets")

    assert [path.name for path in uploads] == [
        "fruit-extra-assets.attestation.json",
        "mango.zip",
        "papaya.zip",
    ]


def test_pack_attestation_rejects_a_missing_subject(tmp_path):
    """A bundle covering a file that is not there attested another tree."""
    (tmp_path / "papaya.zip").write_bytes(b"one")
    bundle = write_bundle(tmp_path / "raw.json", "papaya.zip", "mango.zip")

    with pytest.raises(ValueError, match="mango.zip, missing from"):
        pack_attestation(bundle, tmp_path, "fruit-extra-assets")
