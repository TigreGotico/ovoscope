---
name: ovoscope
description: Generate test boilerplate, run ovoscope tests, record fixtures, and validate expectations for OpenVoiceOS skills. Use when testing OVOS skills or creating test templates.
trigger: Testing OVOS skills, creating end-to-end tests, scaffolding test files, recording test fixtures
---

# ovoscope — OVOS End-to-End Testing

**Use this skill when you are:**
- Creating or scaffolding end-to-end tests for an OVOS skill
- Debugging failing ovoscope tests
- Recording live test fixtures from running skills
- Validating test expectations against actual behavior
- Testing skill interaction patterns (Adapt, Padatious, multi-turn, fallback, etc.)
- Setting up CI/CD integration for skill E2E tests

## Installation

```bash
pip install ovoscope
```

Or in a project with `uv`:
```bash
uv add ovoscope
uv run pytest test/end2end/ -v --timeout=60
```

## Quick Reference

### Run E2E Tests
```bash
pytest test/end2end/ -v --timeout=60
```

### Scaffold a Test File (Python API)

```python
from ovoscope import End2EndTest, get_minicroft
from ovos_bus_client.message import Message
from ovos_bus_client.session import Session

SKILL_ID = "skill-id.author"

# Start MiniCroft with your skill
minicroft = get_minicroft([SKILL_ID])

session = Session("test-session")
utterance = Message(
    "recognizer_loop:utterance",
    {"utterances": ["hello world"], "lang": "en-US"},
    {"session": session.serialize(), "source": "A", "destination": "B"},
)

# Define and run a test. expected_messages must be Message instances (not
# bare topic strings) — use the canonical ovos.* spec topic (OVOS-PIPELINE-1)
# for the speak response.
test = End2EndTest(
    minicroft=minicroft,
    skill_ids=[SKILL_ID],
    source_message=utterance,
    expected_messages=[
        utterance,
        Message("ovos.utterance.speak", {"utterance": "hello"}),
    ],
)
test.execute()
```

### Record a Fixture (CLI)

```bash
ovoscope record <skill_id> "<utterance>" --output fixtures/hello.json
```

### Bus Coverage Tracking

Enable bus-level message coverage to ensure your tests trigger all expected event handlers and emissions.

#### CLI (via pytest)
```bash
# Enable bus coverage for the session
pytest test/end2end/ --ovoscope-bus-cov

# Enable verbose mode to see exact message types
pytest test/end2end/ --ovoscope-bus-cov --ovoscope-bus-cov-verbose

# Filter by skill_id or component name (regex)
pytest test/end2end/ --ovoscope-bus-cov --ovoscope-bus-cov-include="my-skill"
pytest test/end2end/ --ovoscope-bus-cov --ovoscope-bus-cov-exclude="^Thread-|^__core__$"

# Save the merged report to a JSON file (useful for CI)
pytest test/end2end/ --ovoscope-bus-cov --ovoscope-bus-cov-file=coverage/bus-coverage.json
```

#### Manual Opt-in (per test)
```python
def test_something(self, minicroft, bus_coverage_session):
    test = End2EndTest(..., track_bus_coverage=True)
    test.execute()
    # Add to the session-level collector
    bus_coverage_session.add(test.bus_coverage_report)
```

**What is tracked:**
- **Listeners:** Which message types the skill is listening for and if they were invoked.
- **Emitters:** Which message types the skill emitted and if they were asserted in the test.
- **Coverage %:** Per-skill and session-wide coverage statistics.

### Validate Expectations

```python
from ovoscope import CaptureSession

# Capture live interaction
with CaptureSession(minicroft) as session:
    minicroft.bus.emit("recognizer_loop:utterance", {"utterances": ["hello"]})

# Assert captured messages match expected pattern
assert any(msg["type"] == "speak" for msg in session.messages)
```

## Key Docs (in this repo)

- **`docs/index.md`** — Conceptual model, architecture, and public API overview
- **`docs/usage-guide.md`** — 8 test patterns (Adapt, Padatious, recording, replay, multi-turn, fallback, session state, etc.)
- **`docs/end2end-test.md`** — `End2EndTest` parameter reference and full API
- **`docs/minicroft.md`** — `MiniCroft` and `get_minicroft()` reference
- **`docs/cli.md`** — CLI tool reference (`ovoscope record`, etc.)
- **`docs/capture-session.md`** — `CaptureSession` API for recording and replaying messages
- **`docs/ci-integration.md`** — GitHub Actions setup and CI/CD patterns
- **`docs/listener.md`** — Testing listener/audio pipeline components
- **`docs/phal.md`** — Testing PHAL plugin interactions
- **`docs/gui-testing.md`** — Testing GUI interaction patterns
- **`docs/audio-testing.md`** — Audio playback and TTS testing
- **`docs/pipeline.md`** — Testing STT/TTS pipeline components
- **`docs/ocp.md`** — Open Consumer Protocol (OCP) testing
- **`docs/pydantic-integration.md`** — Using Pydantic models in test assertions

## What ovoscope Does

`ovoscope` is a **lightweight end-to-end testing framework** for OVOS skills. It:
- Runs a **MiniCroft** (in-process `SkillManager`) on a `FakeBus` (no network)
- Loads real skill plugins from entry points
- Fires `recognizer_loop:utterance` messages and captures the full message sequence
- Asserts expected behavior without audio, TTS, or GUI

**What ovoscope does NOT cover:** PHAL plugins (use unit tests), actual audio service, STT/TTS implementations, GUI rendering, skill lifecycle hooks, internal handler logic.

## Example Test File

```python
"""test/end2end/test_basic_interaction.py"""
from ovoscope import End2EndTest, get_minicroft

def test_hello_world():
    minicroft = get_minicroft(["ovos-skill-hello-world"])

    test = End2EndTest(
        utterance="hello",
        skill_id="ovos-skill-hello-world",
        expected_messages=["speak", "play_sound"],
        max_wait=5,
    )

    test.execute(minicroft)
```

## Canonical Examples

- **Hello World skill**: `ovos-skill-hello-world/test/end2end/test_*.py`
- **ovos-core**: `ovos-core/test/end2end/test_*.py`

## Common Patterns

See `docs/usage-guide.md` for detailed patterns:
1. **Intent matching** (Adapt, Padatious)
2. **Recording and replaying** fixtures
3. **Multi-turn conversations**
4. **Fallback handling**
5. **Session state** persistence
6. **Message filtering** and validation
7. **Timeout and wait** strategies
8. **Fixture replay** and regression testing

---

**For more details, see the full docs in this repository.**
