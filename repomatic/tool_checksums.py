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

"""SHA-256 checksums and pinned versions for binary tool downloads.

Separated from `tool_runner.py` so Renovate's `postUpgradeTasks` can
rewrite checksums in a different file than the one its regex manager bumps
the version in. Writing the same file the manager touched makes Renovate
silently drop the change (renovatebot/renovate#42263); a separate file
lets the version bump and its matching checksums land in one branch before
the PR opens, so any merge carries both. Regenerated in-place by
`repomatic update-checksums --registry`.

`VERSIONS` records the version each checksum set was computed for. A test
asserts it equals the matching `tool_runner.py` `ToolSpec.version`, so a
stale entry (a bump whose checksums were not refreshed) fails CI offline.
"""

from __future__ import annotations

from extra_platforms import (
    AARCH64,
    LINUX,
    MACOS,
    WINDOWS,
    X86_64,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from .tool_runner import PlatformKey


CHECKSUMS: dict[str, dict[PlatformKey, str]] = {
    "actionlint": {
        (
            LINUX,
            AARCH64,
        ): "325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6",
        (
            LINUX,
            X86_64,
        ): "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8",
        (
            MACOS,
            AARCH64,
        ): "aba9ced2dee8d27fecca3dc7feb1a7f9a52caefa1eb46f3271ea66b6e0e6953f",
        (
            MACOS,
            X86_64,
        ): "5b44c3bc2255115c9b69e30efc0fecdf498fdb63c5d58e17084fd5f16324c644",
        (
            WINDOWS,
            AARCH64,
        ): "cadcf7ea4efe3a68728893813643cebe1185e5b1d4be5b96245f65c9a4d5ea41",
        (
            WINDOWS,
            X86_64,
        ): "6e7241b51e6817ea6a047693d8e6fed13b31819c9a0dd6c5a726e1592d22f6e9",
    },
    "biome": {
        (
            LINUX,
            AARCH64,
        ): "27c9bc5994dfb5711f5f09a4c3c35749ca9c4a898a063bb062e6b932dbc2571d",
        (
            LINUX,
            X86_64,
        ): "e7df298f0551dd90bea4425779369aa3130d9817f4acc4f663ef63c327206a19",
        (
            MACOS,
            AARCH64,
        ): "9b9e04f749db6b037b0ad38ba0c5cce63b185a7cc3b049e577dad3c18f4adb2c",
        (
            MACOS,
            X86_64,
        ): "0c7002cc808eebabe7852c8417b9deb1a5615342e6881c588b49367c6c56db8c",
        (
            WINDOWS,
            AARCH64,
        ): "d561b19067059dfffb1244f00313958f6fcd41c0eaa9542d7787e362118a915d",
        (
            WINDOWS,
            X86_64,
        ): "84d4e71fdbb4b15b1aa83c1b1cc033aae9856d48a7a857c1381b1bd499430f7c",
    },
    "gitleaks": {
        (
            LINUX,
            AARCH64,
        ): "e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080",
        (
            LINUX,
            X86_64,
        ): "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
        (
            MACOS,
            AARCH64,
        ): "b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5",
        (
            MACOS,
            X86_64,
        ): "dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709",
        (
            WINDOWS,
            AARCH64,
        ): "b95f5e4f5c425cedca7ee203d9afd29597e692c4924a12ed42f970537c72cc0f",
        (
            WINDOWS,
            X86_64,
        ): "d29144deff3a68aa93ced33dddf84b7fdc26070add4aa0f4513094c8332afc4e",
    },
    "labelmaker": {
        (
            LINUX,
            AARCH64,
        ): "4685e142da55150904d16624fe1052161de5dba1a859cddef19ab41833c37728",
        (
            LINUX,
            X86_64,
        ): "d76f8e64f9671884dac1758fe54a28a6680c5d9bf0ffd593a2c68ba558cc49a2",
        (
            MACOS,
            AARCH64,
        ): "a52a4e102f0760ce1632da5fdaee2b0debe0e6ddea577b88a94a60172fe85751",
        (
            MACOS,
            X86_64,
        ): "dc8374d6a9bec4ebf143fb42e3024aeffabe8585bb9bd6f134cfaf0693be7688",
        (
            WINDOWS,
            X86_64,
        ): "939195930f9d5fd2b15a5cf43497019a52083e6c6713807d3379de49395c2e10",
    },
    "lychee": {
        (
            LINUX,
            AARCH64,
        ): "91a7bd65685da41b90ccb9bc867a3d649a7818042dae04ff405e55a25bddee4c",
        (
            LINUX,
            X86_64,
        ): "1f4e0ef7f6554a6ed33dd7ac144fb2e1bbed98598e7af973042fc5cd43951c9a",
        (
            MACOS,
            AARCH64,
        ): "c9d3740ea2d891854d37116c9fba840f37b6e7c89d330e7db84ac333631c4977",
        (
            WINDOWS,
            X86_64,
        ): "32975d1493ee1a975d6bb41e4fb56fe419cb442ded628bb772ba2e614acfacad",
    },
    "shfmt": {
        (
            LINUX,
            AARCH64,
        ): "32d92acaa5cd8abb29fc49dac123dc412442d5713967819d8af2c29f1b3857c7",
        (
            LINUX,
            X86_64,
        ): "fb096c5d1ac6beabbdbaa2874d025badb03ee07929f0c9ff67563ce8c75398b1",
        (
            MACOS,
            AARCH64,
        ): "9680526be4a66ea1ffe988ed08af58e1400fe1e4f4aef5bd88b20bb9b3da33f8",
        (
            MACOS,
            X86_64,
        ): "6feedafc72915794163114f512348e2437d080d0047ef8b8fa2ec63b575f12af",
        (
            WINDOWS,
            X86_64,
        ): "60cd368533d0ad73fa86d93d5bbf95ef40587245ce684ed138c1b31557b5fe97",
    },
    "typos": {
        (
            LINUX,
            AARCH64,
        ): "596d5c6b9ecf34307f68bea649178c5b45a4398fe3a1fcef9598e85aa2ccb742",
        (
            LINUX,
            X86_64,
        ): "7aef58932fc123b4cf4b40d86468e89a3297d80169051d7cfd13a235e05fc426",
        (
            MACOS,
            AARCH64,
        ): "23ca24a9186b5cb395b5f6c8eea8cdb02911c8980833e016454b56e90c3bd474",
        (
            MACOS,
            X86_64,
        ): "469a2d9fc894b0cdcec6e4fa3719b4c4638e195feee6517d4845450f8e8985c6",
        (
            WINDOWS,
            X86_64,
        ): "f4a12400c48cc08e7f5435b64d0ecb08c54091b97c3ccabf6cea178d0969ca1f",
    },
}

"""Tool name to platform-keyed SHA-256 hex digest mapping."""

VERSIONS: dict[str, str] = {
    "actionlint": "1.7.12",
    "biome": "2.5.0",
    "gitleaks": "8.30.1",
    "labelmaker": "0.6.4",
    "lychee": "0.24.2",
    "shfmt": "3.13.1",
    "typos": "1.47.2",
}
"""Tool name to the version each checksum set was computed for."""
