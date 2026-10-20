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

"""Serialization of the metadata a workflow reads.

{meth}`~repomatic.metadata.core.Metadata.dump` computes the values, and
{class}`Dialect` writes them in the shape a consumer parses: GitHub Actions step
outputs, or plain JSON.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path

from ..github.outputs import format_multiline_output
from ..matrix.model import Matrix

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any, Final


HEREDOC_FIELDS: Final[frozenset[str]] = frozenset((
    "release_notes",
    "release_notes_with_admonition",
))
"""Metadata fields that always use the heredoc format in GitHub Actions output.

These fields can hold characters that break the simple `key=value` format:
brackets, parentheses, emojis or newlines. So they use the heredoc delimiter
format even when they hold a single line.
"""


class JSONMetadata(json.JSONEncoder):
    """JSON encoder for metadata values: a matrix and a path serialize too."""

    def default(self, o: Any) -> Any:
        if isinstance(o, Matrix):
            return o.matrix()

        if isinstance(o, Path):
            return str(o)

        return super().default(o)


def format_github_value(value: Any) -> str:
    """Transform Python value to GitHub-friendly, JSON-like, console string.

    Renders:

    - `str` as-is
    - `None` into empty string
    - `bool` into lower-cased string
    - `Matrix` into JSON string
    - `Iterable` of mixed strings and `Path` into a serialized space-separated
      string, where `Path` items are double-quoted
    - other `Iterable` into a JSON string
    - `dict[str, str]` into a JSON object string

    A mapping serializes as a JSON object so a workflow can index it by key
    (`fromJSON(...).runner_arch[matrix.os]`), which an array of pairs cannot
    do in a GitHub expression. Only a flat string-to-string mapping is
    accepted: a nested one is almost always a `Matrix` or a config
    dataclass, and the assertion below keeps it from serializing into a
    shape no consumer expects.

    ```{todo}
    Widen the JSON branch beyond an iterable of `dict[str, str]` and a flat
    `dict[str, str]`, the two shapes it asserts on today, when a metadata
    key needs a richer one.
    ```
    """
    # Structured metadata to be rendered as JSON.
    if isinstance(value, Matrix):
        return str(value)

    # Convert non-strings.
    if not isinstance(value, str):
        if value is None:
            value = ""

        elif isinstance(value, bool):
            value = str(value).lower()

        elif isinstance(value, int):
            value = str(value)

        elif isinstance(value, dict):
            assert all(
                isinstance(k, str) and isinstance(v, str) for k, v in value.items()
            ), (
                f"Unsupported mapping value: {value!r}. Only a flat "
                "string-to-string mapping serializes as a JSON object; "
                "wrap a richer one in a Matrix, or leave its config field out "
                "of METADATA_CONFIG_FIELDS so its subcommand reads it instead."
            )
            value = json.dumps(value)

        elif isinstance(value, Iterable):
            # Cast all items to strings, wrapping Path items with double-quotes.
            if all(isinstance(i, (str, Path)) for i in value):
                items = ((f'"{i}"' if isinstance(i, Path) else str(i)) for i in value)
                value = " ".join(items)
            else:
                assert all(
                    isinstance(i, dict)
                    and all(
                        isinstance(k, str) and isinstance(v, str) for k, v in i.items()
                    )
                    for i in value
                ), f"Unsupported iterable value: {value!r}"
                value = json.dumps(value)

        else:
            raise NotImplementedError(
                f"GitHub formatting for {type(value).__name__}: {value!r}. "
                "A nested config dataclass stays out of METADATA_CONFIG_FIELDS, "
                "and out of the metadata output."
            )

    return str(value)


class Dialect(StrEnum):
    """Output dialect for metadata serialization."""

    github = "github"
    github_json = "github-json"
    json = "json"

    def serialize(self, metadata: dict[str, Any]) -> str:
        """Render *metadata* in this dialect.

        :param metadata: Raw key-to-value mapping from
            {meth}`~repomatic.metadata.core.Metadata.dump`.
        :return: The serialized payload.
        """
        if self is Dialect.github:
            lines = []
            for env_name, value in metadata.items():
                env_value = format_github_value(value)
                # A value spanning several lines, or a field that may hold
                # characters `key=value` cannot carry, goes in a heredoc. That
                # path also trims a value past the size a consuming step can
                # receive.
                if len(env_value.splitlines()) > 1 or env_name in HEREDOC_FIELDS:
                    lines.append(format_multiline_output(env_name, env_value))
                else:
                    lines.append(f"{env_name}={env_value}")
            return "".join(f"{line}\n" for line in lines)

        if self is Dialect.github_json:
            # Bundle all metadata into a single `metadata` output key as JSON.
            # Downstream jobs access values via
            # `fromJSON(needs.metadata.outputs.metadata).key`,
            # eliminating the need for per-key `outputs:` declarations.
            #
            # Pre-format list/tuple values via format_github_value(). GitHub
            # Actions stringifies JSON arrays as "Array" when interpolated in
            # ${{ }} expressions, so workflows that splice lists into `run:`
            # or `env:` contexts would receive the literal word "Array".
            # Matrix objects are excluded: they serialize to JSON objects via
            # JSONMetadata and are consumed directly in `strategy: matrix:`
            # blocks, which accept expression objects without string coercion.
            formatted = {
                key: format_github_value(value)
                if isinstance(value, (list, tuple))
                else value
                for key, value in metadata.items()
            }
            json_str = json.dumps(formatted, cls=JSONMetadata, separators=(",", ":"))
            return f"metadata={json_str}\n"

        assert self is Dialect.json
        return json.dumps(metadata, cls=JSONMetadata, indent=2)
