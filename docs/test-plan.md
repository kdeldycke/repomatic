# {octicon}`beaker` Test plan

`repomatic test-plan` is a general-purpose CLI tester. Point it at any executable : a compiled binary, a script, or a command on the `PATH` : give it a YAML list of cases, and it runs each one and checks the result against your expectations.

It is not tied to `repomatic`'s own CLI. It works just as well as a standalone smoke-test runner for a self-contained binary : a Nuitka or PyInstaller build, a Go or Rust executable, anything that runs, exits, and prints. The only inputs are the command to run and a plan of what to expect, so the same tool covers "does my freshly built binary still start, accept its core flags, and fail cleanly on bad input?" without a language-specific test harness.

```{note}
The command-under-test is chosen with `--binary <path>` (a file to execute) or `--command "<command line>"` (resolved on the `PATH`, arguments allowed). The two flags are aliases for one option; pass exactly one.
```

## Quick start

Save a plan as `test-plan.yaml`:

```yaml
# Two smoke tests for a `weather` command.
  - cli_parameters: --version
    exit_code: 0
  - cli_parameters: forecast --city Lisbon
    stdout_contains: Lisbon
```

Run it against the command:

```shell-session
$ repomatic test-plan --command weather --plan-file test-plan.yaml
Test plan results - Total: 2, Skipped: 0, Failed: 0
```

Or against a built binary by path:

```shell-session
$ repomatic test-plan --binary ./dist/weather --plan-file test-plan.yaml
```

The command exits non-zero if any case fails, so it drops straight into a CI step or a release gate.

## Writing a test plan

A test plan is a YAML list. Each item is a **test case**: the arguments to run, plus zero or more expectations. The arguments in `cli_parameters` are appended to the command-under-test; every other key is an expectation checked against the result. A case with no expectation simply asserts the command ran.

| Directive                | Purpose                                                                                                |
| :----------------------- | :----------------------------------------------------------------------------------------------------- |
| `cli_parameters`         | Arguments and options appended to the command. A string is split into arguments; a list is used as-is. |
| `exit_code`              | Expected process exit code; the case fails on any other code.                                          |
| `stdout_contains`        | Substring(s) that must all appear in stdout.                                                           |
| `stderr_contains`        | Substring(s) that must all appear in stderr.                                                           |
| `stdout_regex_matches`   | Regex(es) that must each match somewhere in stdout.                                                    |
| `stderr_regex_matches`   | Regex(es) that must each match somewhere in stderr.                                                    |
| `stdout_regex_fullmatch` | Regex that must fully match stdout, line by line.                                                      |
| `stderr_regex_fullmatch` | Regex that must fully match stderr, line by line.                                                      |
| `timeout`                | Seconds before the command is killed and the case fails as a timeout.                                  |
| `strip_ansi`             | Strip ANSI escape sequences from the output before matching.                                           |
| `skip_platforms`         | Platforms (or platform groups) on which to skip the case.                                              |
| `only_platforms`         | Restrict the case to these platforms; skip it everywhere else.                                         |

Each `*_contains` and `*_regex_matches` directive accepts either a single value or a list; with a list, **all** entries must match. The full field reference, with types, lives in the [`repomatic.test_plan` API documentation](repomatic.md).

### Matching the output

The three families of matcher escalate in strictness:

- **`*_contains`** is plain substring containment : the cheapest check, and the right default for pinning a single word or line.
- **`*_regex_matches`** searches the stream for each pattern (`re.DOTALL`, so `.` spans newlines). Use it when the value varies : a version number, a timestamp, a generated path.
- **`*_regex_fullmatch`** requires the pattern to match the stream in full, line by line. Reserve it for short, fully-known output where any drift is a real change.

Set `strip_ansi: true` when the command emits colors but you only care about the text; the escape codes are removed before matching.

```{caution}
The `output_contains`, `output_regex_matches`, and `output_regex_fullmatch` directives (matching a single combined stdout/stderr stream) are reserved and not yet implemented: setting one fails the case at runtime. Match `stdout_*` and `stderr_*` separately instead.
```

### Gating by platform

`skip_platforms` and `only_platforms` accept [`extra_platforms`](https://kdeldycke.github.io/extra-platforms/) identifiers : individual platforms (`linux`, `macos`, `windows`) or group IDs, in any case. A case is skipped (not failed) when it does not apply, and skips are counted separately in the summary. Use `skip_platforms` for a case that is known to misbehave on one OS, and `only_platforms` for a case that is meaningful on a single OS only (a shell-completion script, a platform-specific path).

## Where the plan comes from

`repomatic test-plan` resolves its cases from the first source that provides them, in this order:

1. `--plan-file <path>` and `--plan-envvar <name>` (both repeatable, read in the order given). CLI sources win outright.
2. `[tool.repomatic]` config in `pyproject.toml`: `test-plan.inline` (the YAML inline) then `test-plan.file` (a path). See the [configuration reference](configuration.md).
3. A built-in default that just checks `--version`, `--help`, and a verbose `--version`.

`test-plan.timeout` in config supplies the default per-case timeout when a case does not set its own.

## Running cases in parallel

Cases are independent process invocations, so `test-plan` runs them in parallel by default, using [`--jobs`](cli.md#repomatic-test-plan) (one fewer than the host CPU count). Pass `--jobs 1` to run sequentially : the only mode in which `--exit-on-error` can stop on the first failure, since parallel cases are already in flight. A plan whose cases share mutable state (writing the same file, racing on a port) should pin `--jobs 1`.

## Exit status

By default every selected case runs and the command exits `1` if any failed, `0` otherwise. `--exit-on-error` stops at the first failure (sequential runs only). `--select-test N` (repeatable) runs only the listed case numbers; the rest are reported as skipped.

## Worked example: testing a standalone binary

A release pipeline builds a `weather` binary and wants to prove it still works before publishing. The plan exercises the headline behaviors and the failure paths:

```yaml
# weather-test-plan.yaml

# Version prints a semver string and exits cleanly.
  - cli_parameters: --version
    exit_code: 0
    stdout_regex_matches: weather, version [0-9]+\.[0-9]+\.[0-9]+

# A known city prints a forecast table.
  - cli_parameters: forecast --city Lisbon
    stdout_contains:
      - Lisbon
      - Temperature

# An unknown city fails with a clear message.
  - cli_parameters: forecast --city Atlantis
    exit_code: 1
    stderr_contains: Unknown city

# Colored output: assert the text, ignore the escape codes.
  - cli_parameters: --color forecast --city Oslo
    strip_ansi: true
    stdout_contains: Oslo

# The live network path can be slow: cap it.
  - cli_parameters: forecast --city Tokyo --live
    timeout: 30

# The completion script differs on Windows: skip it there.
  - cli_parameters: --completion
    skip_platforms: windows
    exit_code: 0
```

Run the plan against the freshly built binary:

```shell-session
$ repomatic test-plan --binary ./dist/weather --plan-file weather-test-plan.yaml
```

The same plan can travel with the project by moving it into `pyproject.toml` under `[tool.repomatic]` (`test-plan.file = "./weather-test-plan.yaml"`), after which a bare `repomatic test-plan --binary ./dist/weather` finds it automatically.

## See also

- [`repomatic test-plan` options](cli.md#repomatic-test-plan) : the full command-line reference.
- [Configuration](configuration.md) : the `test-plan.file`, `test-plan.inline`, and `test-plan.timeout` keys.
- [Test matrix](test-matrix.md) : choosing which platforms and dependency versions CI exercises.
