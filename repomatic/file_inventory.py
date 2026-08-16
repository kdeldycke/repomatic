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

"""What files this repository contains, honoring `.gitignore`.

One question, asked in one place: every "which files are the Python sources /
the workflows / the images" lookup routes through {class}`FileInventory`, whose
{meth}`~FileInventory.glob_files` resolves symlinks, drops broken ones and
filters out anything `.gitignore` excludes. The results are the lists CI jobs
gate on, so a job that formats Markdown and one that lints it see the same
files.

Split out of {class}`repomatic.metadata.Metadata`, which reaches CI context,
git history and `pyproject.toml`: none of that is needed to answer "what is on
disk here", and `Metadata` keeps the family reachable under its own names for
every existing caller.
"""

from __future__ import annotations

import logging
from functools import cached_property
from pathlib import Path

from py_walk import get_parser_from_file
from py_walk.models import Parser
from wcmatch.glob import (
    BRACE,
    DOTGLOB,
    FOLLOW,
    GLOBSTAR,
    GLOBTILDE,
    NEGATE,
    NODIR,
    iglob,
)

GITIGNORE_PATH = Path(".gitignore")
"""Path of the `.gitignore` file whose rules filter every inventory lookup.

Fixed at the repository root, unlike the configurable
`[tool.repomatic.gitignore] location` that `sync-gitignore` writes: the glob
filter has to match what git itself honors, and git only reads this path.
"""


class FileInventory:
    """The repository's files, grouped by what a job needs to act on them.

    Each group is a cached property, so a command asking for the Markdown
    files twice walks the tree once. Instantiate per working directory:
    the lookups resolve against the current directory at call time.
    """

    @cached_property
    def gitignore_exists(self) -> bool:
        return GITIGNORE_PATH.is_file()

    @cached_property
    def gitignore_parser(self) -> Parser | None:
        """Returns a parser for the `.gitignore` file, if it exists."""
        if self.gitignore_exists:
            logging.debug(f"Parse {GITIGNORE_PATH}")
            return get_parser_from_file(GITIGNORE_PATH)
        return None

    def gitignore_match(self, file_path: Path | str) -> bool:
        return bool(self.gitignore_parser and self.gitignore_parser.match(file_path))

    def glob_files(self, *patterns: str) -> list[Path]:
        """Return all file path matching the `patterns`.

        Patterns are glob patterns supporting `**` for recursive search, and `!`
        for negation.

        All directories are traversed, whether they are hidden (i.e. starting with a
        dot `.`) or not, including symlinks.

        Skips:

        - files which does not exists
        - directories
        - broken symlinks
        - files matching patterns specified by `.gitignore` file

        Returns both hidden and non-hidden files.

        All files are normalized to their absolute path, so that duplicates produced by
        symlinks are ignored.

        File path are returned as relative to the current working directory if
        possible, or as absolute path otherwise.

        The resulting list of file paths is sorted.
        """
        current_dir = Path.cwd()
        seen = set()

        for file_path in iglob(
            patterns,
            flags=NODIR | GLOBSTAR | DOTGLOB | GLOBTILDE | BRACE | FOLLOW | NEGATE,
        ):
            # Normalize the path to avoid duplicates.
            try:
                absolute_path = Path(file_path).resolve(strict=True)
            # Skip files that do not exists and broken symlinks.
            except OSError:
                logging.warning(f"Skip non-existing file / broken symlink: {file_path}")
                continue

            # Simplify the path by trying to make it relative to the current location.
            normalized_path = absolute_path
            try:
                normalized_path = absolute_path.relative_to(current_dir)
            except ValueError:
                # If the file is not relative to the current directory, keep its
                # absolute path.
                logging.debug(
                    f"{absolute_path} is not relative to {current_dir}. "
                    "Keeping the path absolute."
                )

            if normalized_path in seen:
                logging.debug(f"Skip duplicate file: {normalized_path}")
                continue

            # Skip files that are ignored by .gitignore.
            if self.gitignore_match(file_path):
                logging.debug(f"Skip file matching {GITIGNORE_PATH}: {file_path}")
                continue

            seen.add(normalized_path)
        return sorted(seen)

    @cached_property
    def python_files(self) -> list[Path]:
        """Returns a list of python files."""
        return self.glob_files("**/*.{py,pyi,pyw,pyx,ipynb}")

    @cached_property
    def json_files(self) -> list[Path]:
        """Returns a list of JSON files.

        ```{note}
        JSON5 files are excluded because Biome doesn't support them.
        ```
        """
        return self.glob_files(
            "**/*.{json,jsonc}",
            "**/.code-workspace",
            "!**/package-lock.json",
        )

    @cached_property
    def yaml_files(self) -> list[Path]:
        """Returns a list of YAML files."""
        return self.glob_files("**/*.{yaml,yml}")

    @cached_property
    def pyproject_files(self) -> list[Path]:
        """Returns a list of `pyproject.toml` files."""
        return self.glob_files("**/pyproject.toml")

    @cached_property
    def workflow_files(self) -> list[Path]:
        """Returns a list of GitHub workflow files."""
        return self.glob_files(".github/workflows/**/*.{yaml,yml}")

    @cached_property
    def doc_files(self) -> list[Path]:
        """Returns a list of doc files."""
        return self.glob_files(
            "**/*.{markdown,mdown,mkdn,mdwn,mkd,md,mdtxt,mdtext,mdx,rst,tex}"
        )

    @cached_property
    def markdown_files(self) -> list[Path]:
        """Returns a list of Markdown files."""
        return self.glob_files(
            "**/*.{markdown,mdown,mkdn,mdwn,mkd,md,mdtxt,mdtext,mdx}"
        )

    @cached_property
    def image_files(self) -> list[Path]:
        """Returns a list of image files.

        Covers the formats handled by `repomatic format-images`: JPEG, PNG,
        WebP, and AVIF. See {mod}`repomatic.images` for the optimization tools.
        """
        return self.glob_files("**/*.{jpeg,jpg,png,webp,avif}")

    @staticmethod
    def shebang_names_zsh(path: Path) -> bool | None:
        """Whether *path* opens with a shebang line naming zsh.

        The `.sh` extension is ambiguous: it says POSIX shell while the
        shebang picks the actual interpreter. Reading that first line is what
        keeps {attr}`shfmt_files` and {attr}`zsh_files` disjoint, so a bash
        script is never handed to the Zsh linter and a zsh script is never
        handed to `shfmt`.

        :param path: File to probe.
        :return: `True` when the shebang names zsh, `False` when it does not,
            and `None` when the file cannot be read. Both callers drop an
            unreadable file rather than guess at its dialect.
        """
        try:
            with path.open("rb") as fh:
                first_line = fh.readline(256)
        except OSError:
            return None
        return first_line.startswith(b"#!") and b"zsh" in first_line

    @cached_property
    def shfmt_files(self) -> list[Path]:
        """Returns a list of shell files that `shfmt` can reliably format.

        `shfmt` supports the following dialects (`-ln` flag):

        - **bash**: GNU Bourne Again Shell.
        - **posix**: POSIX Shell (`/bin/sh`).
        - **mksh**: MirBSD Korn Shell.
        - **bats**: Bash Automated Testing System.

        Zsh is excluded. `shfmt` added experimental Zsh support in v3.13.0
        but it fails on common constructs: `for var (list)` short-form loops
        and `for ... { }` brace-delimited loops. See [mvdan/sh#1203](https://github.com/mvdan/sh/issues/1203) for upstream tracking.

        Files are excluded by extension (`.zsh`, `.zshrc`, etc.) and by
        shebang (any `.sh` file whose first line references `zsh`).
        """
        candidates = self.glob_files(
            "**/*.{bash,bats,ksh,mksh,sh}",
            "**/.{bash_login,bash_logout,bash_profile,bashrc,profile}",
        )
        # Only a file that reads cleanly *and* is not zsh qualifies: an
        # unreadable file yields `None`, which drops it here too.
        return [path for path in candidates if self.shebang_names_zsh(path) is False]

    @cached_property
    def zsh_files(self) -> list[Path]:
        """Returns a list of Zsh files.

        The `.zsh` extension and the zsh dotfiles are unambiguous. A `.sh`
        file joins the list only when its shebang names zsh: matching the
        extension alone would claim every bash script in the repository, and
        the Zsh lint job would then run `zsh --no-exec` over scripts `shfmt`
        is formatting as bash. See {meth}`shebang_names_zsh`.
        """
        files = self.glob_files("**/*.zsh", "**/.{zshrc,zprofile,zshenv,zlogin}")
        files.extend(
            path for path in self.glob_files("**/*.sh") if self.shebang_names_zsh(path)
        )
        return sorted(files)
