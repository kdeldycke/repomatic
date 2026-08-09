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

"""Naming and packing of the sigstore bundles attached to a release.

`actions/attest` writes every bundle to the same `attestation.json` basename,
whatever it signed, so each release job has to rename its own before the files
land in one directory. Three of them did, three different ways: the compiled
binaries appended the suffix to the full filename, the man-page tarball dropped
its `.tar.gz` first, and the consumer-declared extra assets were named after the
*job* rather than any file. A release page therefore carried
`repomatic-manpages.attestation.json` next to `repomatic-manpages.tar.gz`, and
`repomatic-extra-assets.attestation.json` next to `repomatic-claude-plugin.zip`.

This module holds the one rule instead: a bundle is named after the artifact it
attests. The subject list is read back out of the bundle rather than passed in,
so the name is derived from what was actually signed and no caller can spell it
differently. See {func}`bundle_filename` for the multi-subject case.

```{note}
The signing itself stays in `actions/attest`: it needs the job's OIDC token, so
it cannot move here. This module runs immediately after it, in the same job.
```
"""

from __future__ import annotations

import base64
import binascii
import json
import shutil
from pathlib import Path

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Final

ATTESTATION_SUFFIX: Final[str] = ".attestation.json"
"""Extension carried by every attestation bundle attached to a release.

Not `.sigstore.json` (the ecosystem's own convention) because these files have
been published under this name since the first attested release, and a release
asset name is part of the surface users script against.
"""


def bundle_subjects(bundle_path: Path) -> tuple[str, ...]:
    """Filenames of the artifacts a sigstore bundle attests.

    A bundle wraps a DSSE envelope whose base64 payload is an in-toto
    Statement, and that statement's `subject` array names every file signed in
    the same call. One entry for a single `subject-path`, several when
    `actions/attest` was handed a glob: "If multiple subjects are being attested
    at the same time, a single attestation will be created with references to
    each of the supplied subjects."

    :param bundle_path: The bundle `actions/attest` wrote.
    :return: Subject filenames, in the order the statement lists them.
    :raises ValueError: If the file is not a bundle carrying a readable
        in-toto statement, or names a subject that is not a bare filename.
    """
    try:
        bundle = json.loads(bundle_path.read_text(encoding="UTF-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{bundle_path} is not valid JSON: {exc}") from exc

    payload = (bundle.get("dsseEnvelope") or {}).get("payload")
    if not payload:
        raise ValueError(
            f"{bundle_path} carries no DSSE envelope payload: not an "
            "attestation bundle."
        )
    # A payload that is not a base64 string lands in the same basket as one
    # that is unreadable, so callers only ever catch ValueError here.
    try:
        statement = json.loads(base64.b64decode(payload, validate=True))
    except (
        TypeError,
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise ValueError(f"{bundle_path} has an unreadable payload: {exc}") from exc

    subjects = statement.get("subject")
    if not isinstance(subjects, list) or not subjects:
        raise ValueError(f"{bundle_path} attests no subject.")

    names = []
    for subject in subjects:
        name = subject.get("name") if isinstance(subject, dict) else None
        if not isinstance(name, str) or not name:
            raise ValueError(f"{bundle_path} has a subject with no name.")
        # A subject name is a bare filename everywhere the release lane
        # produces one. Reject a path so the join in pack_attestation below
        # cannot be walked out of the asset directory.
        if Path(name).name != name:
            raise ValueError(
                f"{bundle_path} names subject {name!r}, which is not a bare filename."
            )
        names.append(name)
    return tuple(names)


def bundle_filename(subjects: Sequence[str], set_name: str | None = None) -> str:
    """Name a bundle after the artifact it attests.

    A single subject gives the bundle its own name, suffix appended to the
    whole filename so the two sort together on a release page listing assets
    alphabetically (`papaya.tar.gz`, then `papaya.tar.gz.attestation.json`).

    Several subjects have no such name to borrow, since one bundle covers them
    all, so *set_name* is required and should describe the set rather than any
    member of it. That case only arises when a job hands `actions/attest` a
    glob.

    :param subjects: Subject filenames, from {func}`bundle_subjects`.
    :param set_name: Stem to use when *subjects* holds more than one entry.
    :return: The bundle's filename.
    :raises ValueError: If *subjects* is empty, or holds several entries with
        no *set_name* to fall back on.
    """
    if not subjects:
        raise ValueError("Cannot name a bundle that attests no subject.")
    if len(subjects) == 1:
        return f"{subjects[0]}{ATTESTATION_SUFFIX}"
    if not set_name:
        raise ValueError(
            f"Bundle attests {len(subjects)} subjects "
            f"({', '.join(sorted(subjects))}), so it cannot be named after any "
            "one of them: pass --name to name the set."
        )
    return f"{set_name}{ATTESTATION_SUFFIX}"


def pack_attestation(
    bundle_path: Path,
    asset_dir: Path,
    set_name: str | None = None,
) -> list[Path]:
    """Name a bundle after its subject and print what to upload with it.

    Copies *bundle_path* into *asset_dir* under the name
    {func}`bundle_filename` derives, then returns that bundle alongside every
    artifact it attests, which is exactly the file list the release upload step
    has to attach for the provenance to be verifiable offline.

    Every subject must already sit in *asset_dir*: a bundle naming a file that
    is not there means the job attested a different tree than the one it is
    about to upload, which would publish an asset whose sidecar covers
    something else. Immutable releases make that unfixable after the fact, so
    it fails here instead.

    Idempotent: re-running copies the same bytes over the same name.

    :param bundle_path: The bundle `actions/attest` wrote.
    :param asset_dir: Directory holding the attested artifacts.
    :param set_name: Stem for the multi-subject case, see
        {func}`bundle_filename`.
    :return: Sorted paths to upload, the renamed bundle included.
    :raises ValueError: If a subject is missing from *asset_dir*.
    """
    subjects = bundle_subjects(bundle_path)
    assets = [asset_dir / name for name in subjects]

    missing = [path.name for path in assets if not path.is_file()]
    if missing:
        raise ValueError(
            f"{bundle_path} attests {', '.join(sorted(missing))}, missing from "
            f"{asset_dir}."
        )

    target = asset_dir / bundle_filename(subjects, set_name)
    shutil.copy2(bundle_path, target)
    return sorted({*assets, target})
