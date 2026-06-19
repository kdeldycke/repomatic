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

from __future__ import annotations

import logging
import os
import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass, field, fields
from pathlib import Path
from shutil import which
from subprocess import TimeoutExpired, run

import yaml
from boltons.iterutils import flatten
from boltons.strutils import strip_ansi
from click_extra.testing import (
    args_cleanup,
    regex_fullmatch_line_by_line,
    render_cli_run,
)
from extra_platforms import current_platform, extract_members, is_windows

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Generator

    from extra_platforms._types import _TNestedReferences


class SkippedTest(Exception):
    """Raised when a test case should be skipped."""


def _split_args(cli: str) -> list[str]:
    """Split a string or sequence of strings into a tuple of arguments.

    ```{todo}
    Evaluate better Windows CLI parsing with:
    [w32lex](https://github.com/maxpat78/w32lex).
    ```
    """
    if is_windows():
        return cli.split()
    # For Unix platforms, we have the dedicated shlex module.
    else:
        return shlex.split(cli)


@dataclass(order=True)
class CLITestCase:
    """A single CLI test case: how to invoke the command and what to expect.

    Each case runs the command-under-test once with `cli_parameters` appended,
    then checks the captured result against the expectation directives below. A
    case with no expectation only asserts the command ran (plus `exit_code`, if
    set).
    """

    cli_parameters: tuple[str, ...] | str = field(default_factory=tuple)
    """Arguments and options appended to the command-under-test.

    A plain string is split into arguments (on spaces on Windows, with `shlex`
    elsewhere); a list or tuple is used as-is.
    """

    skip_platforms: _TNestedReferences = field(default_factory=tuple)
    """Platforms (or platform-group IDs) on which to skip this case.

    Accepts `extra_platforms` identifiers such as `linux`, `macos`, `windows`,
    in any case, mixed freely with group IDs.
    """

    only_platforms: _TNestedReferences = field(default_factory=tuple)
    """Restrict this case to these platforms; skip it everywhere else.

    The mirror image of `skip_platforms`, using the same identifiers.
    """

    timeout: float | str | None = None
    """Seconds before the command is killed and the case fails as a timeout.

    Falls back to the command's `--timeout` default, then to no limit.
    """

    exit_code: int | str | None = None
    """Expected process exit code; the case fails on any other code."""

    strip_ansi: bool = False
    """Strip ANSI escape sequences from the captured output before matching."""

    output_contains: tuple[str, ...] | str = field(default_factory=tuple)
    """Reserved: combined stdout/stderr matching is not implemented yet.

    Setting any `output_*` directive raises at runtime; use the `stdout_*` and
    `stderr_*` variants instead.
    """

    stdout_contains: tuple[str, ...] | str = field(default_factory=tuple)
    """Substrings that must all be present in stdout."""

    stderr_contains: tuple[str, ...] | str = field(default_factory=tuple)
    """Substrings that must all be present in stderr."""

    output_regex_matches: tuple[re.Pattern | str, ...] | str = field(
        default_factory=tuple
    )
    """Reserved: see `output_contains`."""

    stdout_regex_matches: tuple[re.Pattern | str, ...] | str = field(
        default_factory=tuple
    )
    """Regexes that must each match somewhere in stdout (searched, `re.DOTALL`)."""

    stderr_regex_matches: tuple[re.Pattern | str, ...] | str = field(
        default_factory=tuple
    )
    """Regexes that must each match somewhere in stderr (searched, `re.DOTALL`)."""

    output_regex_fullmatch: re.Pattern | str | None = None
    """Reserved: see `output_contains`."""

    stdout_regex_fullmatch: re.Pattern | str | None = None
    """Regex that must fully match stdout, line by line."""

    stderr_regex_fullmatch: re.Pattern | str | None = None
    """Regex that must fully match stderr, line by line."""

    execution_trace: str | None = None
    """Rendering of the command execution and its output.

    Populated after the case runs, for inspection on failure; not a directive
    you set in a test plan.
    """

    def __post_init__(self) -> None:
        """Normalize all fields.

        ```{note}
        We iterate with `fields()` + `getattr()` instead of `asdict()`
        because `asdict()` deep-copies field values via `copy.deepcopy()`,
        which fails on Python < 3.13 for `MappingProxyType` objects (used
        internally by `extra_platforms`).
        ```
        """
        for f in fields(self):
            field_id = f.name
            field_data = getattr(self, field_id)
            # Validates and normalize integer properties.
            if field_id == "exit_code":
                if isinstance(field_data, str):
                    field_data = int(field_data)
                elif field_data is not None and not isinstance(field_data, int):
                    raise ValueError(f"exit_code is not an integer: {field_data}")

            # Validates and normalize float properties.
            elif field_id == "timeout":
                if isinstance(field_data, str):
                    field_data = float(field_data)
                elif field_data is not None and not isinstance(field_data, float):
                    raise ValueError(f"timeout is not a float: {field_data}")
                # Timeout can only be unset or positive.
                if field_data and field_data < 0:
                    raise ValueError(f"timeout is negative: {field_data}")

            # Validates and normalize boolean properties.
            elif field_id == "strip_ansi":
                if not isinstance(field_data, bool):
                    raise ValueError(f"strip_ansi is not a boolean: {field_data}")

            # Validates and normalize tuple of strings.
            else:
                if field_data:
                    # Wraps single string and other types into a tuple.
                    if isinstance(field_data, str) or not isinstance(
                        field_data, Sequence
                    ):
                        # CLI parameters provided as a long string needs to be split so
                        # that each argument is a separate item in the final tuple.
                        if field_id == "cli_parameters":
                            field_data = _split_args(field_data)
                        else:
                            field_data = (field_data,)

                    for item in field_data:
                        if not isinstance(item, str):
                            raise TypeError(f"Invalid string in {field_id}: {item}")
                    # Ignore blank value.
                    field_data = tuple(i for i in field_data if i.strip())

            # Normalize any mishmash of platform and group IDs into a set of platforms.
            if field_id.endswith("_platforms") and field_data:
                field_data = frozenset(extract_members(field_data))

            # Validates fields containing one or more regexes.
            if "_regex_" in field_id and field_data:
                # Compile all regexes.
                valid_regexes = []
                for regex in flatten((field_data,)):
                    try:
                        # Let dots in regex match newlines.
                        valid_regexes.append(re.compile(regex, re.DOTALL))
                    except re.error as ex:
                        raise ValueError(
                            f"Invalid regex in {field_id}: {regex}"
                        ) from ex
                # Normalize single regex to a single element.
                if field_id.endswith("_fullmatch"):
                    if valid_regexes:
                        field_data = valid_regexes.pop()
                    else:
                        field_data = None
                else:
                    field_data = tuple(valid_regexes)

            setattr(self, field_id, field_data)

    def run_cli_test(
        self,
        command: Path | str,
        additional_skip_platforms: _TNestedReferences | None,
        default_timeout: float | None,
    ):
        """Run a CLI command and check its output against the test case.

        The provided `command` can be either:

        - a path to a binary or script to execute;
        - a command name to be searched in the `PATH`,
        - a command line with arguments to be parsed and executed by the shell.

        ```{todo}
        Add support for environment variables.
        ```

        ```{todo}
        Add support for proper mixed <stdout>/<stderr> stream as a single,
        intertwined output.
        ```
        """
        if self.only_platforms and current_platform() not in self.only_platforms:  # type: ignore[operator]
            raise SkippedTest(f"Test case only runs on platform: {current_platform()}")

        if current_platform() in extract_members(
            self.skip_platforms, additional_skip_platforms
        ):
            raise SkippedTest(f"Skipping test case on platform: {current_platform()}")

        if self.timeout is None and default_timeout is not None:
            logging.info(f"Set default test case timeout to {default_timeout} seconds")
            self.timeout = default_timeout

        # Separate the command into binary file path and arguments.
        args = []
        if isinstance(command, str):
            args = _split_args(command)
            command = args[0]
            args = args[1:]
            # Ensure the command to execute is in PATH.
            if not which(command):
                raise FileNotFoundError(f"Command not found in PATH: {command!r}")
            # Resolve the command to an absolute path.
            command = which(command)  # type: ignore[assignment]
            assert command is not None

        # Check the binary exists and is executable.
        binary = Path(command).resolve()
        assert binary.exists()
        assert binary.is_file()
        assert os.access(binary, os.X_OK)

        clean_args = args_cleanup(binary, args, self.cli_parameters)
        logging.info(f"Run CLI command: {' '.join(clean_args)}")

        try:
            result = run(
                clean_args,
                capture_output=True,
                timeout=self.timeout,  # type: ignore[arg-type]
                check=False,
                # Force UTF-8 decoding of subprocess output. The encoding parameter
                # only affects parent-side decoding and does not change child process
                # behavior. Without this, Windows defaults to cp1252, causing
                # UnicodeDecodeError on non-ASCII output (e.g. contributor names).
                encoding="utf-8",
            )
        except TimeoutExpired:
            raise TimeoutError(
                f"CLI timed out after {self.timeout} seconds: {' '.join(clean_args)}"
            )

        # Execution has been completed, save the output for user's inspection.
        self.execution_trace = render_cli_run(clean_args, result)
        for line in self.execution_trace.splitlines():
            logging.info(line)

        for f in fields(self):
            field_id = f.name
            field_data = getattr(self, field_id)
            if field_id == "exit_code":
                if field_data is not None:
                    logging.info(f"Test exit code, expecting: {field_data}")
                    if result.returncode != field_data:
                        raise AssertionError(
                            f"CLI exited with code {result.returncode}, "
                            f"expected {field_data}"
                        )
                # The specific exit code matches, let's proceed to the next test.
                continue

            # Ignore non-output fields, and empty test cases.
            elif not (
                field_id.startswith(("output_", "stdout_", "stderr_")) and field_data
            ):
                continue

            # Prepare output and name for comparison.
            output = ""
            name = ""
            if field_id.startswith("output_"):
                raise NotImplementedError("<stdout>/<stderr> output mix")
                # output = result.output
                # name = "output"
            elif field_id.startswith("stdout_"):
                output = result.stdout
                name = "<stdout>"
            elif field_id.startswith("stderr_"):
                output = result.stderr
                name = "<stderr>"

            if self.strip_ansi:
                logging.info(f"Strip ANSI sequences from {name}")
                output = strip_ansi(output)

            if field_id.endswith("_contains"):
                for sub_string in field_data:
                    logging.info(f"Check if {name} contains {sub_string!r}")
                    if sub_string not in output:
                        raise AssertionError(
                            f"{name} does not contain {sub_string!r}\n"
                            f"  Actual {name}: {output!r}"
                        )

            elif field_id.endswith("_regex_matches"):
                for regex in field_data:
                    logging.info(f"Check if {name} matches {regex!r}")
                    if not regex.search(output):
                        raise AssertionError(
                            f"{name} does not match regex {regex}\n"
                            f"  Actual {name}: {output!r}"
                        )

            elif field_id.endswith("_regex_fullmatch"):
                regex_fullmatch_line_by_line(field_data, output)


DEFAULT_TEST_PLAN: list[CLITestCase] = [
    # Output the version of the CLI.
    CLITestCase(cli_parameters="--version"),
    # Test combination of version and verbosity.
    CLITestCase(cli_parameters=("--verbosity", "DEBUG", "--version")),
    # Test help output.
    CLITestCase(cli_parameters="--help"),
]


def parse_test_plan(plan_string: str | None) -> Generator[CLITestCase, None, None]:
    if not plan_string:
        raise ValueError("Empty test plan")

    plan = yaml.full_load(plan_string)

    # Validates test plan structure.
    if not plan:
        raise ValueError("Empty test plan")
    if not isinstance(plan, list):
        raise TypeError(f"Test plan is not a list: {plan}")

    directives = frozenset(CLITestCase.__dataclass_fields__.keys())

    for index, test_case in enumerate(plan):
        # Validates test case structure.
        if not isinstance(test_case, dict):
            raise TypeError(f"Test case #{index + 1} is not a dict: {test_case}")
        if not directives.issuperset(test_case):
            raise ValueError(
                f"Test case #{index + 1} contains invalid directives:"
                f"{set(test_case) - directives}"
            )

        yield CLITestCase(**test_case)
