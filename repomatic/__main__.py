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
"""Allow the module to be run as a CLI. I.e.:

```{code-block} shell-session

$ python -m repomatic
```

The CI test suite (`tests.yaml`) verifies launchability via multiple
invocation paths to catch entry-point and import issues early:

- `uv run -m repomatic` (module invocation)
- `uv run -- repomatic` (from local project)
- `uvx -- repomatic` (installed from PyPI)
- `uvx --from git+https://...` (installed from git)
"""

from __future__ import annotations

from repomatic.cli.main import repomatic


def main():
    """Execute the CLI under its canonical name, whatever the entry point.

    Without `prog_name`, Click derives the displayed name from the invocation:

    ```{code-block} shell-session
    $ python -m repomatic --version
    python -m repomatic, version 4.0.0
    ```

    The `main()` indirection lets three invocation styles share one entry
    point rendering the same name:

    - the plain module call: `python -m repomatic`,
    - the `[project.scripts]` console script: `repomatic = "repomatic.__main__:main"`,
    - Nuitka's main-module compilation requirement, which takes the
      package directory rather than this file:
      `python -m nuitka (...) --python-flag=-m repomatic`.
    """
    repomatic(prog_name=repomatic.name)


if __name__ == "__main__":
    main()
