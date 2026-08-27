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

```{todo}
Drop the py-walk dependency and parse `.gitignore` with wcmatch, already
imported here for globbing, once it reads gitignore files natively:
[facelessuser/wcmatch#226](https://github.com/facelessuser/wcmatch/issues/226).
```
"""

from __future__ import annotations

import logging
import os
from functools import cached_property
from pathlib import Path, PurePath

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
    globmatch,
    iglob,
)

_GLOB_FLAGS = NODIR | GLOBSTAR | DOTGLOB | GLOBTILDE | BRACE | FOLLOW | NEGATE
"""One flag set for the walk and the per-group matches.

The walk collects candidates with {func}`~wcmatch.glob.iglob` and each group
filters them with {func}`~wcmatch.glob.globmatch`; sharing the flags is what
keeps the two reading a pattern the same way.
"""

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

    def __init__(self) -> None:
        self._dir_ignored: dict[str, bool] = {}
        """Per-directory verdicts: the directory, or one of its ancestors, is
        gitignored.

        The py-walk match costs about half a millisecond per path, and the
        candidates a walk yields inside an ignored tree (a virtualenv, a Sphinx
        build directory) outnumber the kept files by orders of magnitude. Git
        never re-includes anything below an ignored directory, so a single
        `True` recorded for `.venv` answers for its entire subtree through
        dictionary lookups instead of one parser match per file.
        """

        self._file_ignored: dict[str, bool] = {}
        """Per-file gitignore verdicts, shared across the group lookups.

        Overlapping groups re-encounter the same files (every Markdown file is
        also a doc file), so each path is judged once per inventory rather
        than once per group.
        """

        self._zsh_shebangs: dict[Path, bool | None] = {}
        """Per-file shebang verdicts, shared by the two shell groups.

        {attr}`shfmt_files` and {attr}`zsh_files` probe the same `.sh` files
        from opposite directions, so each file is opened once per inventory
        rather than once per group.
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

    def _in_ignored_dir(self, file_path: str) -> bool:
        """Whether *file_path* sits below a gitignored directory.

        Walks the parent chain up to the nearest already-judged ancestor, then
        fills the verdicts back down (see `_dir_ignored`). A directory only
        pays a parser match when no ancestor already answered `True`.
        """
        parent = PurePath(file_path).parent
        verdicts = self._dir_ignored
        chain: list[str] = []
        node = parent
        while True:
            key = str(node)
            if key == "." or key in verdicts or node.parent == node:
                break
            chain.append(key)
            node = node.parent
        ignored = verdicts.get(str(node), False)
        parser = self.gitignore_parser
        for key in reversed(chain):
            if not ignored and parser is not None:
                ignored = bool(parser.match(key))
            verdicts[key] = ignored
        return verdicts.get(str(parent), ignored)

    def gitignore_match(self, file_path: Path | str) -> bool:
        if self.gitignore_parser is None:
            return False
        key = str(file_path)
        verdict = self._file_ignored.get(key)
        if verdict is None:
            verdict = self._in_ignored_dir(key) or bool(
                self.gitignore_parser.match(key)
            )
            self._file_ignored[key] = verdict
        return verdict

    @cached_property
    def _all_files(self) -> tuple[str, ...]:
        """Every non-ignored file under the working directory, walked once.

        The candidate pool every {meth}`glob_files` call filters. Each group
        lookup used to run its own full-tree traversal, re-descending the
        ignored trees (a virtualenv holds thousands of files a walk visits and
        the filter then discards) once per group: ten lookups, ten walks. One
        walk feeds them all, and the per-group patterns match in memory.

        `.git/` internals are dropped up front: they are git's bookkeeping,
        never repository content, and no `.gitignore` rule covers them, so
        each of those hundreds of object files would otherwise pay a full
        parser match just to be discarded by every group pattern.
        """
        git_dir = f".git{os.sep}"
        return tuple(
            file_path
            for file_path in iglob(["**/*"], flags=_GLOB_FLAGS)
            if not file_path.startswith(git_dir) and not self.gitignore_match(file_path)
        )

    def glob_files(self, *patterns: str) -> list[Path]:
        """Return all file path matching the `patterns`.

        Patterns are glob patterns supporting `**` for recursive search, and `!`
        for negation, resolved against the current working directory: they
        select from one shared walk of it (see {attr}`_all_files`), so an
        absolute pattern matches nothing.

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

        for file_path in self._all_files:
            if not globmatch(file_path, patterns, flags=_GLOB_FLAGS):
                continue

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

    def shebang_names_zsh(self, path: Path) -> bool | None:
        """Whether *path* opens with a shebang line naming zsh.

        The `.sh` extension is ambiguous: it says POSIX shell while the
        shebang picks the actual interpreter. Reading that first line is what
        keeps {attr}`shfmt_files` and {attr}`zsh_files` disjoint, so a bash
        script is never handed to the Zsh linter and a zsh script is never
        handed to `shfmt`. Verdicts are memoized per inventory (see
        `_zsh_shebangs`).

        :param path: File to probe.
        :return: `True` when the shebang names zsh, `False` when it does not,
            and `None` when the file cannot be read. Both callers drop an
            unreadable file rather than guess at its dialect.
        """
        if path in self._zsh_shebangs:
            return self._zsh_shebangs[path]
        verdict: bool | None
        try:
            with path.open("rb") as fh:
                first_line = fh.readline(256)
        except OSError:
            verdict = None
        else:
            verdict = first_line.startswith(b"#!") and b"zsh" in first_line
        self._zsh_shebangs[path] = verdict
        return verdict

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
        and `for ... { }` brace-delimited loops.

        Files are excluded by extension (`.zsh`, `.zshrc`, etc.) and by
        shebang (any `.sh` file whose first line references `zsh`).

        ```{todo}
        Stop excluding Zsh once `shfmt` formats those constructs:
        [mvdan/sh#1203](https://github.com/mvdan/sh/issues/1203).
        ```
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
