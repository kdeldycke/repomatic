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

"""Guard the test suite against parametrizing over an unordered collection.

`pytest-xdist` distributes work by test ID, so every worker must collect the same
IDs in the same order. A `@pytest.mark.parametrize` fed a `set` or `frozenset`
iterates in an order that depends on the process's string-hash seed, so each
worker derives different IDs and the whole run aborts with "Different tests were
collected between gw0 and gw1" before a single test executes. The failure names
the workers rather than the parametrization, and the suite still passes serially,
which is what makes it worth a conformance test rather than a comment.

Wrapping the argument in `sorted()` fixes it.

```{note}
A `dict` is **not** a hazard: mappings iterate in insertion order, which is a
language guarantee and identical in every process. Only the hash-ordered
containers qualify, so `SOME_DICT` and `SOME_DICT.items()` are both accepted.
```

Two shapes evade a naive scan, and both have already shipped a latent break:

- **An imported collection.** `from repomatic.registry import NON_REUSABLE_WORKFLOWS`
  puts a `frozenset` in scope under a name this module never sees assigned, so
  the binding is resolved by importing it rather than by reading the source.
- **A wrapper call.** `list(SOME_SET)` and `tuple(SOME_SET)` are as unordered as
  the set they copy, and read as deliberate, which is worse.
"""

from __future__ import annotations

import ast
import importlib
import re

import pytest

from tests.conftest import PROJECT_ROOT

TESTS_DIR = PROJECT_ROOT / "tests"
"""Root of the test suite."""

TEST_FILES = sorted(TESTS_DIR.rglob("*.py"))
"""Every Python module of the test suite, sorted because `pytest-xdist` needs
identical parametrize IDs in every worker."""

UNORDERED_CALLS = frozenset({"set", "frozenset"})
"""Builtins whose result has no stable iteration order across processes."""

ORDER_PRESERVING_WRAPPERS = frozenset({"list", "tuple"})
"""Builtins that copy an iterable without imposing an order of their own.

Applied to a set, they freeze whatever hash order that process happened to
produce, which is exactly the failure they look like they are preventing.
"""

FILE_ENCODING = "UTF-8"
"""The single spelling of the `encoding=` argument used across the suite.

Matching production's dominant spelling. Both spellings are equally correct to
Python, so this is purely about a reader never wondering whether the difference
carries meaning.
"""

ENCODING_ARGUMENT_RE = re.compile(r"""encoding=["'](?!UTF-8["'])([\w-]+)["']""")
"""Match an `encoding=` keyword whose value is not the canonical spelling."""


def _module_level_bindings(tree: ast.Module) -> dict[str, ast.expr]:
    """Map each module-level name to the expression it is bound to.

    Only module-level bindings are tracked: a parametrization can reference
    nothing narrower, since decorators are evaluated at import time.
    """
    bindings: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        # A bare annotation (`FRUITS: tuple[str, ...]`) binds no value.
        if node.value is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                bindings[target.id] = node.value
    return bindings


def _imported_origins(tree: ast.Module) -> dict[str, tuple[str, str]]:
    """Map each `from X import y` name to its `(module, attribute)` pair."""
    origins: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                origins[alias.asname or alias.name] = (node.module, alias.name)
    return origins


def _kind_of_expression(
    node: ast.expr,
    bindings: dict[str, ast.expr],
    origins: dict[str, tuple[str, str]],
) -> str:
    """Describe *node* when it evaluates to an unordered collection, else `""`.

    Resolves one level of indirection in each of three directions: a local
    module-level binding, an imported name, and an order-preserving wrapper
    call. One level is enough for every shape the suite writes, and stopping
    there keeps the scan from chasing an arbitrary expression graph.
    """
    if isinstance(node, ast.Set):
        return "set literal"
    if isinstance(node, ast.SetComp):
        return "set comprehension"

    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        name = node.func.id
        if name in UNORDERED_CALLS:
            return f"{name}() call"
        if name in ORDER_PRESERVING_WRAPPERS and node.args:
            inner = _kind_of_expression(node.args[0], bindings, origins)
            if inner:
                return f"{name}() wrapping {inner}"
        return ""

    if isinstance(node, ast.Name):
        if node.id in bindings:
            inner = _kind_of_expression(bindings[node.id], bindings, origins)
            return f"{node.id} is a {inner}" if inner else ""
        if node.id in origins:
            module_name, attribute = origins[node.id]
            try:
                value = getattr(importlib.import_module(module_name), attribute)
            except (ImportError, AttributeError):
                # Unresolvable at scan time: judged clean rather than guessed at.
                return ""
            if isinstance(value, (set, frozenset)):
                return (
                    f"{node.id} is a {type(value).__name__} imported from {module_name}"
                )
    return ""


def _unordered_parametrize(tree: ast.Module) -> list[str]:
    """Return one description per parametrization fed an unordered collection."""
    bindings = _module_level_bindings(tree)
    origins = _imported_origins(tree)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not ast.unparse(node.func).endswith("parametrize"):
            continue
        # parametrize(argnames, argvalues): only the second argument is iterated
        # into test IDs.
        if len(node.args) < 2:
            continue
        kind = _kind_of_expression(node.args[1], bindings, origins)
        if kind:
            violations.append(f"line {node.lineno}: {kind}")
    return violations


def test_test_suite_discovered() -> None:
    """The scan actually walks the suite, so a green result means something."""
    assert len(TEST_FILES) > 40
    assert TESTS_DIR / "conftest.py" in TEST_FILES


def test_detector_flags_unordered_parametrize() -> None:
    """The detector catches each unordered shape, and clears the ordered ones."""
    tree = ast.parse(
        "from repomatic.registry import NON_REUSABLE_WORKFLOWS\n"
        "FRUITS = frozenset({'papaya', 'mango'})\n"
        "CITIES = {'Lisbon', 'Oslo'}\n"
        "CRATES = {city for city in CITIES}\n"
        "WEIGHTS = {'papaya': 2, 'mango': 1}\n"
        "SORTED = sorted(FRUITS)\n"
        # Unordered, one way or another.
        "@pytest.mark.parametrize('fruit', FRUITS)\n"
        "@pytest.mark.parametrize('city', CITIES)\n"
        "@pytest.mark.parametrize('crate', CRATES)\n"
        "@pytest.mark.parametrize('fruit', {'papaya', 'mango'})\n"
        "@pytest.mark.parametrize('fruit', list(FRUITS))\n"
        "@pytest.mark.parametrize('fruit', tuple(FRUITS))\n"
        "@pytest.mark.parametrize('flow', NON_REUSABLE_WORKFLOWS)\n"
        "@pytest.mark.parametrize('flow', list(NON_REUSABLE_WORKFLOWS))\n"
        # Ordered: a mapping, a sort, and plain literals.
        "@pytest.mark.parametrize('fruit', WEIGHTS)\n"
        "@pytest.mark.parametrize('fruit', WEIGHTS.items())\n"
        "@pytest.mark.parametrize('fruit', SORTED)\n"
        "@pytest.mark.parametrize('fruit', sorted(FRUITS))\n"
        "@pytest.mark.parametrize('fruit', list(sorted(FRUITS)))\n"
        "@pytest.mark.parametrize('fruit', ['papaya', 'mango'])\n"
        "def test_harvest(fruit, city, crate, flow):\n"
        "    pass\n"
    )
    violations = _unordered_parametrize(tree)
    report = "\n".join(violations)
    # Eight unordered decorators; the six ordered ones below them stay silent.
    assert len(violations) == 8, report
    assert "FRUITS is a frozenset() call" in report
    assert "CITIES is a set literal" in report
    assert "CRATES is a set comprehension" in report
    # An inline literal is reported on its own, with no name to attribute it to.
    assert "line 10: set literal" in report
    assert "list() wrapping FRUITS is a frozenset() call" in report
    assert "tuple() wrapping FRUITS is a frozenset() call" in report
    # The imported frozenset is caught bare and through the wrapper alike.
    assert report.count("NON_REUSABLE_WORKFLOWS is a frozenset imported from") == 2, (
        report
    )


@pytest.mark.parametrize("test_file", TEST_FILES, ids=lambda p: p.name)
def test_no_unordered_parametrize(test_file) -> None:
    """No parametrization iterates a collection with an unstable order."""
    tree = ast.parse(test_file.read_text(encoding=FILE_ENCODING))
    violations = _unordered_parametrize(tree)
    assert not violations, (
        f"{test_file.relative_to(TESTS_DIR.parent)} parametrizes over an unordered"
        f" collection, which breaks xdist collection: {'; '.join(violations)}."
        " Wrap the argument in sorted()."
    )


@pytest.mark.parametrize("test_file", TEST_FILES, ids=lambda p: p.name)
def test_encoding_argument_spelling_is_uniform(test_file) -> None:
    """Every `encoding=` in the suite uses the one canonical spelling.

    Python normalizes the codec name, so a mixed suite is correct but reads as
    though the difference means something. Pinning it is what keeps a reviewer
    from having to decide.
    """
    offenders = sorted(
        set(ENCODING_ARGUMENT_RE.findall(test_file.read_text(encoding=FILE_ENCODING)))
    )
    assert not offenders, (
        f"{test_file.relative_to(TESTS_DIR.parent)} spells encoding as"
        f" {', '.join(repr(o) for o in offenders)}; the suite uses"
        f" encoding={FILE_ENCODING!r} everywhere."
    )


BARE_TEXT_IO_CALLS = frozenset({"read_text", "write_text", "open"})
"""Text-I/O calls that silently fall back to the platform encoding when bare."""


@pytest.mark.parametrize("test_file", TEST_FILES, ids=lambda p: p.name)
def test_text_io_always_names_its_encoding(test_file) -> None:
    """Every text-mode I/O call passes `encoding=` explicitly.

    Windows still defaults to cp1252, so a bare `read_text()` hides until the
    content grows a non-ASCII character and only fails in Windows CI. Ruff's
    `PLW1514` covers the receivers its inference can type; this sweep covers
    the unannotated `Path` locals it misses.
    """
    tree = ast.parse(test_file.read_text(encoding=FILE_ENCODING))
    violations = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in BARE_TEXT_IO_CALLS:
            continue
        keywords = {kw.arg for kw in node.keywords}
        if "encoding" in keywords:
            continue
        if node.func.attr == "open":
            # Archive modules open bytes, never text.
            receiver = node.func.value
            if isinstance(receiver, ast.Name) and receiver.id in {
                "bz2",
                "gzip",
                "lzma",
                "tarfile",
                "zipfile",
            }:
                continue
            # `.open()` in binary mode (`"rb"`, or tarfile's `"w:gz"` family)
            # needs no encoding; only flag text modes.
            modes = [
                arg.value
                for arg in (*node.args, *(kw.value for kw in node.keywords))
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            ]
            if any("b" in mode or ":" in mode for mode in modes):
                continue
        violations.append(f"line {node.lineno}: {node.func.attr}()")
    assert not violations, (
        f"{test_file.name} has text I/O without an explicit encoding:"
        f" {'; '.join(violations)}."
    )


@pytest.mark.parametrize("test_file", TEST_FILES, ids=lambda p: p.name)
def test_no_function_local_imports(test_file) -> None:
    """Imports live at module level, never inside test or helper bodies.

    Function-local imports hide dependencies and bypass ruff's import sorting
    (see `claude.md` § Imports); nothing in the suite needs one to break an
    import cycle.
    """
    tree = ast.parse(test_file.read_text(encoding=FILE_ENCODING))
    violations = [
        f"line {node.lineno}"
        for func in ast.walk(tree)
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef))
        for node in ast.walk(func)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert not violations, (
        f"{test_file.name} imports inside a function body: {'; '.join(violations)}."
        " Hoist to module level."
    )


INTEGRATION_TOOL_GUARD = "skip_unless_tool_runs"
"""Marks a test that deliberately runs a tool for real, downloads included.

The opt-out for {func}`test_run_tool_calls_mock_the_binary_install`: a test
calling this has declared that reaching the network is the point, and is
skipped wholesale on a runner where the tool cannot run.
"""

BINARY_INSTALL_MOCK = "_install_binary"
"""The `tool_runner` internal a hermetic `run_tool` test has to patch."""


def _installing_tools() -> frozenset[str]:
    """Tool names whose `run_tool` reaches {data}`BINARY_INSTALL_MOCK`.

    Two ways in: the tool *is* a downloaded binary, or it declares `path_tools`
    and so downloads its helpers before running.
    """
    registry = importlib.import_module("repomatic.tooling.tool_registry")
    return frozenset(
        name
        for name, spec in registry.TOOL_REGISTRY.items()
        if spec.backend is registry.ToolBackend.BINARY or spec.path_tools
    )


@pytest.mark.parametrize("test_file", TEST_FILES, ids=lambda p: p.name)
def test_run_tool_calls_mock_the_binary_install(test_file) -> None:
    """A `run_tool` test on a downloading tool must patch its install.

    Patching `subprocess.run` alone looks hermetic and is not: `run_tool`
    resolves the binary *before* it builds a command line, so the download runs
    for real and only the invocation is faked. Such a test passes in silence
    for as long as GitHub Releases is healthy, then fails the whole Tests job
    with an `HTTPError` naming a URL nothing in the test mentions. Three
    `mdformat` tests shipped this way and went red on a 503, mdformat being a
    `uvx` tool that still downloads `shfmt` to put on its `PATH`.

    A test that means to hit the network says so with
    {data}`INTEGRATION_TOOL_GUARD` and is exempt.
    """
    tree = ast.parse(test_file.read_text(encoding=FILE_ENCODING))
    installing = _installing_tools()
    violations = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        source = ast.unparse(func)
        if INTEGRATION_TOOL_GUARD in source or BINARY_INSTALL_MOCK in source:
            continue
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "run_tool"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value in installing
            ):
                violations.append(f"{func.name} runs {node.args[0].value!r}")
    assert not violations, (
        f"{test_file.name} runs a downloading tool without patching"
        f" `repomatic.tooling.tool_runner.{BINARY_INSTALL_MOCK}`: {'; '.join(violations)}."
        " Patch it, or gate the test on"
        f" `{INTEGRATION_TOOL_GUARD}` if the download is the point."
    )


@pytest.mark.parametrize("test_file", TEST_FILES, ids=lambda p: p.name)
def test_no_classes_for_grouping(test_file) -> None:
    """No `Test*` classes: tests are top-level functions, per `claude.md`.

    Non-test helper classes (doubles, matcher sentinels) are fine; only a
    class pytest would collect as a grouping container is flagged.
    """
    tree = ast.parse(test_file.read_text(encoding=FILE_ENCODING))
    offenders = [
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test")
    ]
    assert not offenders, (
        f"{test_file.name} groups tests in classes: {', '.join(offenders)}."
        " Write top-level test functions instead."
    )
