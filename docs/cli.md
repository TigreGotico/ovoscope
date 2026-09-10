# ovoscope CLI

The `ovoscope` command-line tool provides six subcommands for recording,
replaying, diffing, validating, and scanning E2E test fixtures.

## Installation

After installing the package (``pip install ovoscope``), the ``ovoscope``
command is available on your ``$PATH``.

```bash
ovoscope --help
```

---

## Subcommands

### `ovoscope record`: Record a fixture

**In-process recording** (default): loads the skill(s) inside the current
process using `MiniCroft` (`cli.py:cmd_record`).

```bash
ovoscope record \
    --skill-id ovos-skill-hello-world.openvoiceos \
    --utterance "hello" \
    --output fixture.json \
    --lang en-US \
    --timeout 20
```

**Live recording** from a running OVOS instance (`RemoteRecorder`, in
`remote_recorder.py:RemoteRecorder.record`):

```bash
ovoscope record --live \
    --bus-url ws://localhost:8181/core \
    --skill-id ovos-skill-date-time.openvoiceos \
    --utterance "what time is it" \
    --output datetime_fixture.json
```

| Flag | Default | Description |
|------|---------|-------------|
| `--skill-id` | none | OPM skill IDs to load (repeatable). |
| `--utterance` | **required** | User utterance text. |
| `--output` | **required** | Output fixture JSON path. |
| `--lang` | `en-US` | Language tag. |
| `--pipeline` | None | Comma-separated pipeline stage IDs. |
| `--timeout` | `20.0` | Capture timeout in seconds. |
| `--live` | False | Use live OVOS instance via `RemoteRecorder`. |
| `--bus-url` | `ws://localhost:8181/core` | MessageBus URL (only for `--live`). |

---

### `ovoscope run`: Replay a fixture

Replays a saved fixture file and exits with code 1 on failure, in
`cli.py:cmd_run`.

```bash
ovoscope run test/fixtures/hello.json
ovoscope run test/fixtures/hello.json --verbose --timeout 30
```

| Flag | Default | Description |
|------|---------|-------------|
| `fixture` | **required** | Path to fixture JSON file. |
| `--verbose` | False | Print failure details. |
| `--timeout` | `30.0` | Execution timeout in seconds. |

---

### `ovoscope diff`: Compare two fixtures

Compares two fixture files and prints a colored report, in
`diff.py:diff_fixtures` and `cli.py:cmd_diff`.

```bash
ovoscope diff expected.json actual.json
ovoscope diff expected.json actual.json --no-color
```

Exits 0 if identical, 1 if differences are found.

| Flag | Default | Description |
|------|---------|-------------|
| `expected` | **required** | Reference fixture path. |
| `actual` | **required** | Fixture to compare against reference. |
| `--no-color` | False | Disable ANSI color codes. |
| `--include-context` | False | Include `context` fields in the comparison. By default context is ignored because it contains ephemeral routing metadata (`source`, `destination`, `session`) that varies between runs. Pass `--include-context` when you specifically want to assert routing behaviour. |

---

### `ovoscope validate`: Schema-validate fixtures

Validates one or more fixture files against the expected schema, in
`cli.py:cmd_validate`.

```bash
ovoscope validate test/fixtures/*.json
```

Uses `pydantic_helpers.validate_fixture` when available (requires
`pip install ovoscope[pydantic]`); falls back to basic JSON structure
validation (checks required top-level keys and that `expected_messages`
is a list) when the `pydantic` extra is not installed.

---

### `ovoscope coverage`: Ecosystem coverage scan

Scans a workspace root for OVOS plugin repos and reports E2E test coverage, in
`coverage.py:scan_workspace` and `cli.py:cmd_coverage`.

```bash
ovoscope coverage "OpenVoiceOS Workspace/" --format table
ovoscope coverage "OpenVoiceOS Workspace/" --format json
```

| Flag | Default | Description |
|------|---------|-------------|
| `workspace` | **required** | Workspace root directory. |
| `--format` | `table` | Output format: `table` or `json`. |

---

### `ovoscope bus-coverage` — Bus handler/emitter coverage

Runs every fixture found under a directory (or a single fixture file), and
reports which bus message types each skill actually listens for and emits,
merged across all fixtures — `cli.py:cmd_bus_coverage`.

```bash
ovoscope bus-coverage test/fixtures/
ovoscope bus-coverage test/fixtures/hello.json --format json
ovoscope bus-coverage test/fixtures/ --skill-id ovos-skill-hello-world.openvoiceos --verbose
```

| Flag | Default | Description |
|------|---------|-------------|
| `test_dir` | **required** | Directory of fixture JSON files, or a single fixture file. |
| `--skill-id` | None | Only report on fixtures that include this skill_id. |
| `--format` | `table` | Output format: `table` or `json`. |
| `--verbose` / `-v` | False | Print per-message-type detail rows. |

Fixtures that fail to load or time out booting `MiniCroft` are skipped and
counted; the run still reports coverage for the fixtures that succeeded.

---

## `ovoscope-setup` — Install the skill into AI coding assistants

`ovoscope-setup` is a separate console script (`setup_skill.py`) that installs
the ovoscope Claude Code / Gemini CLI skill — `SKILL.md`, docs, and `FAQ.md` —
downloaded from GitHub at install time.

```bash
ovoscope-setup                     # auto-detect and install all
ovoscope-setup --claude            # Claude Code only
ovoscope-setup --gemini            # Gemini CLI only (project-level)
ovoscope-setup --gemini --path /my/workspace
ovoscope-setup --list              # show detected tools without installing
ovoscope-setup --no-docs           # skip docs download (offline / CI)
ovoscope-setup --uninstall --claude
```

| Flag | Default | Description |
|------|---------|-------------|
| `--claude` | False | Install for Claude Code (`~/.claude/skills/ovoscope/`). |
| `--gemini` | False | Install for Gemini CLI (`<path>/.gemini/skills/ovoscope/`). Project-level. |
| `--path` | current directory | Project root for the Gemini install. |
| `--list` | False | Show which tools are detected on `PATH` without installing anything. |
| `--no-docs` | False | Skip downloading documentation from GitHub (offline / CI). |
| `--uninstall` | False | Remove the skill instead of installing it. |

With no explicit `--claude`/`--gemini` flag, the tool auto-detects which of
`claude`/`gemini` are on `PATH` and installs for those.

---

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success / no differences / all valid |
| 1 | Failure / differences found / validation error |

---
[← Usage Guide](usage-guide.md) · [Home](../README.md) · [CI Integration →](ci-integration.md)
