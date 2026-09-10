# OvoScope Usage Guide
This guide takes you from zero to writing and running your first end-to-end skill test. It
assumes familiarity with Python's `unittest` and the OVOS bus message model.
---
## Prerequisites
Install ovoscope and the skill under test in the same virtual environment:
```bash
# editable installs: recommended during development
uv pip install -e ovoscope/ -e Skills/ovos-skill-hello-world/
# or via PyPI
pip install ovoscope ovos-skill-hello-world
```
ovoscope requires:
- Python 3.10+
- `ovos-core >= 2.0.4a2` (pulled automatically as a dependency)
- The skill plugin must be discoverable via its `setup.py` / `pyproject.toml` entry point
Verify the skill is on the plugin path:
```bash
python -c "from ovos_plugin_manager.skills import find_skill_plugins; print(list(find_skill_plugins()))"
# should include: ovos-skill-hello-world.openvoiceos
```
---
## When to Use ovoscope vs FakeBus Unit Tests
| Scenario | Use |
|---|---|
| Test that a skill intent handler runs correct logic | FakeBus unit test |
| Test skill settings, decorators, or `initialize()` | FakeBus unit test |
| Test skill lifecycle (load / unload / reload) | FakeBus unit test |
| Test that an utterance matches a specific intent | **ovoscope** |
| Test the full message sequence a skill produces | **ovoscope** |
| Test message ordering and routing context | **ovoscope** |
| Test session state after an interaction | **ovoscope** |
| Test multi-turn dialogue (converse / fallback) | **ovoscope** |
| Test that a skill is blacklisted and does NOT match | **ovoscope** |
**Rule of thumb**: if you are asserting on *what gets emitted on the bus*: type, order, data, or
routing: use ovoscope. If you are testing the internal Python logic of a handler in isolation,
use FakeBus unit tests.
FakeBus reference:
```python
from ovos_utils.fakebus import FakeBus  # ovos-utils
```
---
## Quick Start: Hello World
The canonical example skill is `ovos-skill-hello-world.openvoiceos`. It has two intents:
- **HelloWorldIntent** (Adapt): triggered by "hello world"
- **Greetings.intent** (Padatious): triggered by greetings like "good morning"
```python
import unittest
from ovos_bus_client.message import Message
from ovos_bus_client.session import Session
from ovoscope import End2EndTest, get_minicroft
SKILL_ID = "ovos-skill-hello-world.openvoiceos"
class TestHelloWorldQuickStart(unittest.TestCase):
    def test_hello_world(self):
        session = Session("test-session-1")
        session.pipeline = ["ovos-adapt-pipeline-plugin-high"]
        utterance = Message(
            "recognizer_loop:utterance",
            {"utterances": ["hello world"], "lang": "en-US"},
            {"session": session.serialize(), "source": "A", "destination": "B"},
        )
        test = End2EndTest(
            skill_ids=[SKILL_ID],
            source_message=utterance,
            expected_messages=[
                utterance,
                Message(f"{SKILL_ID}.activate", data={}, context={"skill_id": SKILL_ID}),
                Message(f"{SKILL_ID}:HelloWorldIntent",
                        data={"utterance": "hello world", "lang": "en-US"},
                        context={"skill_id": SKILL_ID}),
                Message("mycroft.skill.handler.start",
                        data={"name": "HelloWorldSkill.handle_hello_world_intent"},
                        context={"skill_id": SKILL_ID}),
                Message("speak",
                        data={"utterance": "Hello world", "lang": "en-US",
                              "expect_response": False,
                              "meta": {"dialog": "hello.world", "data": {}, "skill": SKILL_ID}},
                        context={"skill_id": SKILL_ID}),
                Message("mycroft.skill.handler.complete",
                        data={"name": "HelloWorldSkill.handle_hello_world_intent"},
                        context={"skill_id": SKILL_ID}),
                Message("ovos.utterance.handled", data={}, context={"skill_id": SKILL_ID}),
            ],
        )
        test.execute(timeout=10)
```
`test.execute()` raises `AssertionError` on any mismatch. No return value is used: use pytest or
`unittest.TestCase` assertions normally.
---
## Pattern 1: Manual Assertion (Adapt Intent Match)
Write each expected `Message` explicitly. This is the most readable pattern and the easiest to
debug.
```python
from ovos_bus_client.message import Message
from ovos_bus_client.session import Session
from ovoscope import End2EndTest, get_minicroft
SKILL_ID = "ovos-skill-hello-world.openvoiceos"
# Build a session that restricts the pipeline to Adapt only
session = Session("test-adapt")
session.pipeline = ["ovos-adapt-pipeline-plugin-high"]
message = Message(
    "recognizer_loop:utterance",
    {"utterances": ["hello world"], "lang": "en-US"},
    {"session": session.serialize(), "source": "A", "destination": "B"},
)
test = End2EndTest(
    skill_ids=[SKILL_ID],
    source_message=message,
    expected_messages=[
        message,
        Message(f"{SKILL_ID}.activate", data={}, context={"skill_id": SKILL_ID}),
        Message(f"{SKILL_ID}:HelloWorldIntent",
                data={"utterance": "hello world", "lang": "en-US"},
                context={"skill_id": SKILL_ID}),
        Message("mycroft.skill.handler.start",
                data={"name": "HelloWorldSkill.handle_hello_world_intent"},
                context={"skill_id": SKILL_ID}),
        Message("speak",
                data={"utterance": "Hello world", "lang": "en-US",
                      "expect_response": False,
                      "meta": {"dialog": "hello.world", "data": {}, "skill": SKILL_ID}},
                context={"skill_id": SKILL_ID}),
        Message("mycroft.skill.handler.complete",
                data={"name": "HelloWorldSkill.handle_hello_world_intent"},
                context={"skill_id": SKILL_ID}),
        Message("ovos.utterance.handled", data={}, context={"skill_id": SKILL_ID}),
    ],
)
test.execute(timeout=10)
```
Only keys present in `expected.data` and `expected.context` are checked: extra keys in the
received message are ignored. This lets you assert on exactly the fields you care about.
---
## Pattern 2: Padatious Intent Match
Padatious uses `.intent` file names as the message type. Restrict the session pipeline to
Padatious only so Adapt doesn't shadow the match:
```python
SKILL_ID = "ovos-skill-hello-world.openvoiceos"
session = Session("test-padatious")
session.pipeline = ["ovos-padatious-pipeline-plugin-high"]
message = Message(
    "recognizer_loop:utterance",
    {"utterances": ["good morning"], "lang": "en-US"},
    {"session": session.serialize(), "source": "A", "destination": "B"},
)
test = End2EndTest(
    skill_ids=[SKILL_ID],
    source_message=message,
    expected_messages=[
        message,
        Message(f"{SKILL_ID}.activate", data={}, context={"skill_id": SKILL_ID}),
        Message(f"{SKILL_ID}:Greetings.intent",  # Padatious intent file name
                data={"utterance": "good morning", "lang": "en-US"},
                context={"skill_id": SKILL_ID}),
        Message("mycroft.skill.handler.start",
                data={"name": "HelloWorldSkill.handle_greetings"},
                context={"skill_id": SKILL_ID}),
        Message("speak",
                data={"lang": "en-US", "expect_response": False,
                      "meta": {"dialog": "hello", "data": {}, "skill": SKILL_ID}},
                context={"skill_id": SKILL_ID}),
        Message("mycroft.skill.handler.complete",
                data={"name": "HelloWorldSkill.handle_greetings"},
                context={"skill_id": SKILL_ID}),
        Message("ovos.utterance.handled", data={}, context={"skill_id": SKILL_ID}),
    ],
)
test.execute(timeout=10)
```
Note: for Padatious the `speak` message's `utterance` key may vary (depends on the dialog file
randomisation), so omit `"utterance"` from `expected.data` if it is non-deterministic: only
assert on `lang` and `meta`.
---
## Pattern 3: Recording Mode (Bootstrap Fixtures)
Don't know the exact message sequence yet? Let ovoscope record it for you:
```python
from ovoscope import End2EndTest
from ovos_bus_client.message import Message
from ovos_bus_client.session import Session
SKILL_ID = "ovos-skill-hello-world.openvoiceos"
session = Session("recorder-session")
session.pipeline = ["ovos-adapt-pipeline-plugin-high"]
message = Message(
    "recognizer_loop:utterance",
    {"utterances": ["hello world"], "lang": "en-US"},
    {"session": session.serialize(), "source": "A", "destination": "B"},
)
# Recording: runs the skill live, captures messages, returns a test object
test = End2EndTest.from_message(
    message=message,
    skill_ids=[SKILL_ID],
    timeout=20,
)
# Save to a JSON fixture for replay
test.save("tests/fixtures/hello_world_adapt.json", anonymize=True)
```
`anonymize=True` (default) strips real location / personal data from the session context before
saving: safe to commit.
Then in your test suite:
```python
test = End2EndTest.from_path("tests/fixtures/hello_world_adapt.json")
test.execute(timeout=10)
```
---
## Pattern 4: Replay from JSON Fixture
Committed JSON fixtures make tests fully self-contained: no network, no live skill discovery, no
non-determinism in expected messages.
```python
import unittest
from ovoscope import End2EndTest
class TestFromFixture(unittest.TestCase):
    def test_adapt_from_fixture(self):
        test = End2EndTest.from_path("tests/fixtures/hello_world_adapt.json")
        test.execute(timeout=10)
    def test_padatious_from_fixture(self):
        test = End2EndTest.from_path("tests/fixtures/hello_world_padatious.json")
        test.execute(timeout=10)
```
Note: skills still need to be installed (the JSON stores `skill_ids`, and `execute()` calls
`get_minicroft()` which loads the real plugin). The fixture stores the expected message sequence,
not the skill code.
---
## Pattern 5: Reusing MiniCroft Across Multiple Tests
Creating a `MiniCroft` is expensive (it trains intent models). Reuse it across tests in the same
class with `setUp` / `tearDown`:
```python
import unittest
from ovos_bus_client.message import Message
from ovos_bus_client.session import Session
from ovos_utils.log import LOG
from ovoscope import End2EndTest, get_minicroft
SKILL_ID = "ovos-skill-hello-world.openvoiceos"
class TestHelloWorldSharedRuntime(unittest.TestCase):
    def setUp(self):
        LOG.set_level("DEBUG")
        self.minicroft = get_minicroft([SKILL_ID])
    def tearDown(self):
        if self.minicroft:
            self.minicroft.stop()
        LOG.set_level("CRITICAL")
    def _make_test(self, utterance_text, pipeline, expected_messages):
        session = Session("shared-session")
        session.pipeline = pipeline
        message = Message(
            "recognizer_loop:utterance",
            {"utterances": [utterance_text], "lang": "en-US"},
            {"session": session.serialize(), "source": "A", "destination": "B"},
        )
        return End2EndTest(
            minicroft=self.minicroft,  # pass existing MiniCroft: not managed, not stopped
            skill_ids=[SKILL_ID],
            source_message=message,
            expected_messages=expected_messages,
        )
    def test_adapt_match(self):
        test = self._make_test(
            "hello world",
            ["ovos-adapt-pipeline-plugin-high"],
            expected_messages=[
                # ... (abbreviated for clarity)
            ],
        )
        test.execute(timeout=10)
    def test_padatious_no_match(self):
        # "hello world" does not match Padatious Greetings.intent → failure path
        session = Session("no-match-session")
        session.pipeline = ["ovos-padatious-pipeline-plugin-high"]
        message = Message(
            "recognizer_loop:utterance",
            {"utterances": ["hello world"], "lang": "en-US"},
            {"session": session.serialize(), "source": "A", "destination": "B"},
        )
        test = End2EndTest(
            minicroft=self.minicroft,
            skill_ids=[SKILL_ID],
            source_message=message,
            expected_messages=[
                message,
                Message("mycroft.audio.play_sound", {"uri": "snd/error.mp3"}),
                Message("complete_intent_failure", {}),
                Message("ovos.utterance.handled", {}),
            ],
        )
        test.execute(timeout=10)
```
When you pass `minicroft=self.minicroft` explicitly, `End2EndTest` sets `managed=False` and does
**not** call `minicroft.stop()` at the end of `execute()`. Your `tearDown` is responsible for
cleanup.
---
## Pattern 6: Multi-Turn Conversation
Pass a **list** of `Message` objects as `source_message` to test a dialogue sequence. ovoscope
emits them in order, propagating session state between turns:
```python
session = Session("multi-turn-session")
session.pipeline = ["ovos-adapt-pipeline-plugin-high"]
turn1 = Message(
    "recognizer_loop:utterance",
    {"utterances": ["hello world"], "lang": "en-US"},
    {"session": session.serialize(), "source": "A", "destination": "B"},
)
# For turn 2, session context is propagated automatically from the last received message
turn2 = Message(
    "recognizer_loop:utterance",
    {"utterances": ["good morning"], "lang": "en-US"},
    {"source": "A", "destination": "B"},  # no "session" key: will be filled by ovoscope
)
test = End2EndTest(
    skill_ids=[SKILL_ID],
    source_message=[turn1, turn2],  # list of turns
    expected_messages=[
        # All messages from both turns in sequence
        turn1,
        # ... turn 1 messages ...
        Message("ovos.utterance.handled", data={}, context={"skill_id": SKILL_ID}),
        turn2,
        # ... turn 2 messages ...
        Message("ovos.utterance.handled", data={}, context={"skill_id": SKILL_ID}),
    ],
)
test.execute(timeout=20)
```
Session propagation: if turn 2 has no `"session"` key in context, ovoscope copies the session
from the last received message: simulating how a real OVOS client propagates session updates.
---
## Pattern 7: Testing Fallback Skills
Fallback skills receive a `"ovos.skills.fallback.ping"` message to probe for a handler, and then
the main fallback message. The expected sequence is longer than a normal intent match:
```python
session = Session("fallback-session")
# use a pipeline that includes fallback
session.pipeline = ["ovos-fallback-skill-plugin-high"]
message = Message(
    "recognizer_loop:utterance",
    {"utterances": ["what is the meaning of life"], "lang": "en-US"},
    {"session": session.serialize(), "source": "A", "destination": "B"},
)
# For fallback testing, keep_original_src ensures the fallback ping routing is validated
test = End2EndTest(
    skill_ids=["my-fallback-skill.author"],
    source_message=message,
    expected_messages=[
        message,
        Message("ovos.skills.fallback.ping", {}),       # ovoscope validates source/destination for this
        # ... handler messages ...
        Message("ovos.utterance.handled", {}),
    ],
    # "ovos.skills.fallback.ping" is in DEFAULT_KEEP_SRC: its routing is checked against
    # the original source_message context, not the rolling flip-point tracker
)
test.execute(timeout=15)
```
See `DEFAULT_KEEP_SRC` in `ovoscope/__init__.py`: it pre-populates `keep_original_src` so
fallback ping routing is always validated against the original source message context.
---
## Pattern 8: Session State Validation
Use `final_session` and `inject_active` to assert on session state at the end of a test:
```python
from ovos_bus_client.session import Session
from ovoscope import End2EndTest
SKILL_ID = "ovos-skill-hello-world.openvoiceos"
# Pre-activate another skill before the test
expected_session = Session("state-check-session")
expected_session.pipeline = ["ovos-adapt-pipeline-plugin-high"]
# After the interaction, hello world skill must remain active
# Build what you expect the session to look like after the test
expected_session.activate_skill(SKILL_ID)
session = Session("state-check-session")
session.pipeline = ["ovos-adapt-pipeline-plugin-high"]
message = Message(
    "recognizer_loop:utterance",
    {"utterances": ["hello world"], "lang": "en-US"},
    {"session": session.serialize(), "source": "A", "destination": "B"},
)
test = End2EndTest(
    skill_ids=[SKILL_ID],
    source_message=message,
    expected_messages=[...],
    final_session=expected_session,        # checked after all messages are processed
    test_final_session=True,               # enabled by default
    test_active_skills=True,               # check active skill list per-message
    activation_points=[f"{SKILL_ID}.activate"],  # skill must be active after this message
    deactivation_points=["intent.service.skills.deactivate"],
)
test.execute(timeout=10)
```
Fields validated by `final_session`:
- `active_skills` (set comparison)
- `lang`, `pipeline`, `system_unit`, `date_format`, `time_format`
- `site_id`, `session_id`
- `blacklisted_skills`, `blacklisted_intents`
---
## Async Messages
Some messages arrive from external threads and may appear at any time during the interaction
(e.g., GUI updates that race with bus messages). Declare them in `async_messages` so they are
captured separately and not checked for ordering:
```python
test = End2EndTest(
    skill_ids=[SKILL_ID],
    source_message=message,
    expected_messages=[...],  # sync messages only
    async_messages=["gui.page.show"],  # collected separately, order not checked
    test_async_messages=True,          # assert that "gui.page.show" was received
    test_async_message_number=True,    # assert exactly 1 async message received
)
```
Async messages are collected in `CaptureSession.async_responses`: they are NOT in the main
`responses` list and are NOT included in `test_message_number` count.
---
## Disabling Assertions
Some assertion groups can be turned off individually when a message is noisy or non-deterministic:
| Parameter | Default | Effect |
|---|---|---|
| `test_message_number` | `True` | Assert exact message count |
| `test_msg_type` | `True` | Assert message type for each message |
| `test_msg_data` | `True` | Assert expected data keys exist and match |
| `test_msg_context` | `True` | Assert expected context keys exist and match |
| `test_routing` | `True` | Assert source/destination routing |
| `test_active_skills` | `True` | Assert skill activation state |
| `test_boot_sequence` | `True` | Assert boot messages (if `expected_boot_sequence` set) |
| `test_async_messages` | `True` | Assert async message types |
| `test_async_message_number` | `True` | Assert async message count |
| `test_final_session` | `True` | Assert final session state |
Example: disable data and routing checks for a noisy third-party message:
```python
test = End2EndTest(
    ...
    test_msg_data=False,    # don't assert on data keys
    test_routing=False,     # don't assert source/destination
)
```
---
## Troubleshooting
### Timeout: no messages received
- The skill plugin is not loaded. Verify `find_skill_plugins()` returns your skill ID.
- The session pipeline is empty or does not include the right plugin. Set
  `session.pipeline = [...]` explicitly.
- The EOF message (`ovos.utterance.handled`) never fires: check if the intent matched at all
  by setting `verbose=True` and inspecting stdout.
### Skill not loading
```
LOG.set_level("DEBUG")
minicroft = get_minicroft(["my-skill.author"])
# Watch for "Loaded skill: my-skill.author" in output
```
If it never prints, the entry point is wrong. Check your `setup.py` / `pyproject.toml`:
```python
# setup.py
entry_points={
    "ovos.plugin.skill": {
        "my-skill.author = my_skill:MySkill"
    }
}
```
### Intent not matching
- Confirm the utterance text matches an Adapt keyword or a Padatious training phrase.
- For Adapt: check that all required keywords are present in the utterance.
- For Padatious: training happens at `MiniCroft.run()` via `mycroft.skills.train`. If training
  fails silently, check the Padatious model files exist under `~/.local/share/`.
### Wrong message count
Enable `verbose=True` (default): ovoscope prints every received message with its index. Compare
against the expected list to find the first divergence.
### `get_minicroft()` hangs
`get_minicroft()` polls `croft.status.state` in a tight loop (0.1s sleep). If it hangs
indefinitely, a skill is raising an exception during `_startup`. Set `LOG.set_level("DEBUG")` and
watch for tracebacks.
---
## Constants Reference
### Test lifecycle constants
```python
from ovoscope import (
    DEFAULT_EOF,          # ["ovos.utterance.handled"]: end-of-test trigger
    DEFAULT_IGNORED,      # ["ovos.skills.settings_changed"]: filtered out
    GUI_IGNORED,          # GUI namespace messages ignored when ignore_gui=True
    DEFAULT_ENTRY_POINTS, # ["recognizer_loop:utterance"]: routing reset points
    DEFAULT_FLIP_POINTS,  # []: routing flip points
    DEFAULT_KEEP_SRC,     # ["ovos.skills.fallback.ping"]: always check vs original source
    DEFAULT_ACTIVATION,   # []: activation check points
    DEFAULT_DEACTIVATION, # ["intent.service.skills.deactivate"]
)
```
### Pipeline constants
ovoscope exposes composable pipeline stage lists so you can precisely control which pipeline
stages are active during a test:
```python
from ovoscope import (
    STOP_PIPELINE,         # ["ovos-stop-pipeline-plugin-high", ...medium, ...low]
    CONVERSE_PIPELINE,     # ["ovos-converse-pipeline-plugin"]
    ADAPT_PIPELINE,        # ["ovos-adapt-pipeline-plugin-high", ...medium, ...low]
    PADATIOUS_PIPELINE,    # ["ovos-padatious-pipeline-plugin-high", ...medium, ...low]
    FALLBACK_PIPELINE,     # ["ovos-fallback-pipeline-plugin-high", ...medium, ...low]
    COMMON_QUERY_PIPELINE, # ["ovos-common-query-pipeline-plugin"]
    PERSONA_PIPELINE,      # ["ovos-persona-pipeline-plugin-high", ...low]
    DEFAULT_TEST_PIPELINE, # all standard stages, no AI/persona/OCP: the default
)
```
`MiniCroft.default_pipeline` defaults to the narrower `LEAN_DEFAULT_PIPELINE`
(Stop, Converse, Adapt, Padatious, Padacioso, Fallback — high/medium tiers
only; see [minicroft.md](minicroft.md#lean-default-pipeline)) — use
`extra_pipelines=` to add a stage on top of it, or pass `DEFAULT_TEST_PIPELINE`
explicitly for the wider set including the `-low` tier and Common Query.
Both exclude persona, Ollama, OCP, and m2v stages, giving fully reproducible
results regardless of which AI plugins are installed.
**Composing custom pipelines:**
```python
# Adapt intent only: fastest, no fallback
mc = get_minicroft([SKILL_ID], default_pipeline=ADAPT_PIPELINE)
# Full intent chain with fallback: typical skill testing
mc = get_minicroft([SKILL_ID],
                   default_pipeline=CONVERSE_PIPELINE + ADAPT_PIPELINE + FALLBACK_PIPELINE)
# Include persona pipeline: when testing AI persona behaviour
mc = get_minicroft([SKILL_ID], default_pipeline=DEFAULT_TEST_PIPELINE + PERSONA_PIPELINE)
# No override: use whatever the system config says (includes OCP, m2v, etc.)
mc = get_minicroft([SKILL_ID], default_pipeline=None)
```
Sessions created without an explicit `session` in their message context inherit
`SessionManager.default_session.pipeline`, so the override covers all such utterances.
The original pipeline is restored when `mc.stop()` is called.
**When to use `PERSONA_PIPELINE`:** Only add persona stages when you are explicitly testing
persona behaviour.  Persona plugins make network calls to AI APIs and are
non-deterministic: they are intentionally excluded from `DEFAULT_TEST_PIPELINE`.
---
## See Also
- [end2end-test.md](end2end-test.md): full `End2EndTest` parameter reference
- [minicroft.md](minicroft.md): `MiniCroft` / `get_minicroft()` reference
- [capture-session.md](capture-session.md): `CaptureSession` internals
- [ci-integration.md](ci-integration.md): wiring ovoscope into GitHub Actions CI
- Canonical examples: `Skills/ovos-skill-hello-world/test/test_helloworld.py`
- Core examples: `ovos-core/test/end2end/`

---

## Pattern 9: Multi-Skill Interactions

When testing skill interactions where one skill hands off to another, load all
involved skills and emit a single utterance.  `CaptureSession` records messages
from all loaded skills simultaneously.

```python
from ovoscope import get_minicroft, CaptureSession
from ovos_utils.messagebus import Message

mc = get_minicroft([
    "ovos-skill-hello-world.openvoiceos",
    "ovos-skill-fallback-unknown.openvoiceos",
])
session = CaptureSession(mc)
session.capture(Message(
    "recognizer_loop:utterance",
    data={"utterances": ["something unknown"], "lang": "en-US"},
))
responses = session.finish()
mc.stop()
```

---

## Pattern 10: PHAL Plugin Testing

PHAL plugins communicate via the MessageBus and accept `bus` directly, so
`FakeBus` injection works without hardware.

```python
from ovos_utils.messagebus import Message
from ovoscope.phal import MiniPHAL, PHALTest

# Context-manager style
with MiniPHAL(plugin_ids=["ovos-PHAL-plugin-connectivity-events.openvoiceos"]) as phal:
    phal.emit(Message("network.connected"))
    phal.assert_emitted("mycroft.internet.connected", timeout=2.0)

# Declarative style
PHALTest(
    plugin_ids=["ovos-PHAL-plugin-system.openvoiceos"],
    trigger_message=Message("system.reboot"),
    expected_types=["system.reboot.confirmed"],
).execute()
```

See [phal.md](phal.md) for the full reference.

---

## Pattern 11: OCP / Common Play Testing

OCP skills respond to `ovos.common_play.query` with a media list.  `OCPTest`
drives the full flow with optional HTTP mocking.

```python
from ovoscope.ocp import OCPTest

OCPTest(
    skill_ids=["ovos-skill-youtube.openvoiceos"],
    utterance="play lofi hip hop",
    mock_responses={"youtube.com": {"items": [{"title": "Lofi Radio", "url": "..."}]}},
    expected_media=[{"title": "Lofi Radio"}],
).execute()
```

See [ocp.md](ocp.md) for the full reference.

---

## Pattern 12: GUI Message Assertion

`GUICaptureSession` captures `gui.*` messages so tests can assert page
navigation and namespace values without polluting the main message capture.

```python
from ovoscope import get_minicroft, GUICaptureSession
from ovos_utils.messagebus import Message
import time

mc = get_minicroft(["ovos-skill-hello-world.openvoiceos"])
with GUICaptureSession(mc.bus) as gui:
    mc.bus.emit(Message(
        "recognizer_loop:utterance",
        data={"utterances": ["hello"], "lang": "en-US"},
    ))
    time.sleep(2)
    gui.assert_page_shown("helloworldskill", "hello.qml")
mc.stop()
```

See [ovoscope/__init__.py](../ovoscope/__init__.py) for `GUICaptureSession` API.

---
[Home](../README.md) · [CLI →](cli.md)
