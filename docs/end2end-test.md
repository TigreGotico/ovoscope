# End2EndTest
`End2EndTest` is the primary API. It wires together `MiniCroft`, `CaptureSession`, and all assertion logic into a single declarative test object.
## Class: `End2EndTest`: `ovoscope/__init__.py`
```python
from ovoscope import End2EndTest
```
A `dataclass`. Configure once, call `.execute()` to run.
`End2EndTest.execute`: `ovoscope/__init__.py`
---
## Fields
### Core
| Field | Type | Default | Description |
|---|---|---|---|
| `skill_ids` | `list[str]` | required | Skill plugin IDs to load |
| `source_message` | `Message \| list[Message]` | required | Input message(s). Standardized to list on init. |
| `expected_messages` | `list[Message]` | required | Ordered expected response sequence |
| `expected_boot_sequence` | `list[Message]` | `[]` | Startup messages to validate before running |
### Message Filtering
| Field | Type | Default | Description |
|---|---|---|---|
| `eof_msgs` | `list[str]` | `["ovos.utterance.handled"]` | Message types that end capture |
| `ignore_messages` | `list[str]` | `["ovos.skills.settings_changed"]` | Message types to discard |
| `ignore_gui` | `bool` | `True` | Discard GUI namespace messages |
| `async_messages` | `list[str]` | `[]` | Message types arriving from external threads (collected separately, unordered) |
### Routing Tracking
| Field | Type | Default | Description |
|---|---|---|---|
| `flip_points` | `list[str]` | `[]` | After receiving this message type, swap expected source↔destination |
| `entry_points` | `list[str]` | `["recognizer_loop:utterance"]` | On this message type, extract new expected source/destination from the received message context (reversed) |
| `keep_original_src` | `list[str]` | `["ovos.skills.fallback.ping"]` | For these message types, always compare against the original source/destination |
### Active Skill Tracking
| Field | Type | Default | Description |
|---|---|---|---|
| `inject_active` | `list[str]` | `[]` | Pre-activate these skill IDs before the test runs (modifies session) |
| `disallow_extra_active_skills` | `bool` | `False` | Fail if any unexpected skill is active |
| `activation_points` | `list[str]` | `[]` | After this message type, `context.skill_id` must remain active |
| `deactivation_points` | `list[str]` | `["intent.service.skills.deactivate"]` | After this message type, `context.skill_id` must NOT be active |
| `final_session` | `Session \| None` | `None` | If set, compare last-message session against this |
### Sub-test Toggles
All default to `True`. Set to `False` to skip individual assertion categories:
| Flag | What it checks |
|---|---|
| `test_message_number` | `len(received) == len(expected)` |
| `test_async_messages` | All `async_messages` types were received |
| `test_async_message_number` | Async message count matches |
| `test_boot_sequence` | Boot messages match `expected_boot_sequence` |
| `test_msg_type` | Each `msg_type` matches |
| `test_msg_data` | Each expected data key/value is present in received |
| `test_msg_context` | Each expected context key/value is present in received |
| `test_active_skills` | Active skills in session match expectations |
| `test_routing` | `context.source` and `context.destination` match |
| `test_final_session` | Final session matches `final_session` |
### Internals
| Field | Default | Description |
|---|---|---|
| `verbose` | `True` | Print pass/fail for each assertion |
| `minicroft` | `None` | Provide an existing `MiniCroft` to reuse across tests |
| `managed` | `False` | Set automatically; if `True`, `execute()` stops the minicroft after running |
---
## `execute(timeout=30)`
Runs the test. Raises `AssertionError` on the first failing assertion.
If `minicroft` is `None`, creates one automatically (managed mode: stops it after the test). To run multiple tests against the same loaded skills, pass your own `MiniCroft`:
```python
from ovoscope import get_minicroft, End2EndTest
croft = get_minicroft(["skill-weather.openvoiceos"])
test1 = End2EndTest(skill_ids=[], source_message=msg1, expected_messages=[...], minicroft=croft)
test2 = End2EndTest(skill_ids=[], source_message=msg2, expected_messages=[...], minicroft=croft)
test1.execute()
test2.execute()
croft.stop()
```
---
## Assertion Logic Detail
### Message count
```
assert len(expected_messages) == len(received_messages)
```
On failure, prints the first differing message type for debugging.
### Per-message assertions
For each `(expected, received)` pair:
**Type check:**
```python
assert expected.msg_type == received.msg_type
```
**Data check**: subset match (expected keys must be present with matching values):
```python
for k, v in expected.data.items():
    assert received.data[k] == v
```
**Context check**: same subset pattern:
```python
for k, v in expected.context.items():
    assert received.context[k] == v
```
**Routing check**: tracks rolling expected source/destination:
- Starts from `source_message[0].context["source"]` and `["destination"]`
- On `entry_points` message: flips (`e_src, e_dst = r_dst, r_src`): the reply comes back the other way
- On `flip_points` message: updates expected from received, then swaps
- `keep_original_src` always uses the original, regardless of flips
### Active skill tracking
Session is read from each received message's context. For messages after an `activation_point`, `context.skill_id` is added to the expected active set. For messages after a `deactivation_point`, it's removed. The test then verifies all expected active skill IDs appear in the session.
### Final session check
Compares `active_skills`, `lang`, `pipeline`, `system_unit`, `date_format`, `time_format`, `site_id`, `session_id`, `blacklisted_skills`, `blacklisted_intents` from the session in the last received message against `final_session`.
---
## Recording Mode: `from_message()`
Runs a live capture against real skills and returns a ready-to-use `End2EndTest` with the captured messages as `expected_messages`.
```python
test = End2EndTest.from_message(
    message=utterance,          # Message or list[Message]
    skill_ids=["skill-weather.openvoiceos"],
    eof_msgs=None,              # use defaults
    flip_points=None,
    ignore_messages=None,
    async_messages=None,
    timeout=20,
)
test.save("tests/weather_test.json")
```
Use this to bootstrap test fixtures from real behavior, then commit the JSON and replay in CI.
---
## Serialization
### `serialize(anonymize=True) -> dict`
Returns a JSON-serializable dict. With `anonymize=True`, scrubs location data from sessions.
### `save(path, anonymize=True)`
Writes the serialized test to a JSON file.
### `End2EndTest.deserialize(data) -> End2EndTest`
Loads from a dict or JSON string.
### `End2EndTest.from_path(path) -> End2EndTest`
Loads from a JSON file path.
---
## Examples
### Testing complete intent failure (no skills)
```python
from ovoscope import End2EndTest
from ovos_bus_client.message import Message
from ovos_bus_client.session import Session
session = Session("test-123")
utterance = Message(
    "recognizer_loop:utterance",
    {"utterances": ["zorbax flibnork"], "lang": "en-us"},
    {"session": session.serialize(), "source": "A", "destination": "B"},
)
End2EndTest(
    skill_ids=[],
    source_message=utterance,
    expected_messages=[
        utterance,
        Message("mycroft.audio.play_sound", {"uri": "snd/error.mp3"}),
        Message("complete_intent_failure", {}),
        Message("ovos.utterance.handled", {}),
    ],
).execute()
```
### Testing a skill with pre-activated converse
```python
End2EndTest(
    skill_ids=["skill-timer.openvoiceos"],
    source_message=utterance,
    expected_messages=[...],
    inject_active=["skill-timer.openvoiceos"],  # timer already in converse
    activation_points=["speak"],               # stays active after speaking
    deactivation_points=["intent.service.skills.deactivate"],
).execute()
```
### Multi-turn test
```python
End2EndTest(
    skill_ids=["skill-weather.openvoiceos"],
    source_message=[first_utterance, follow_up],  # two turns
    expected_messages=[...all messages from both turns...],
    eof_msgs=["ovos.utterance.handled"],  # reset between turns
).execute()
```
### Reusing MiniCroft across tests
```python
from ovoscope import get_minicroft, End2EndTest
croft = get_minicroft(["skill-weather.openvoiceos"])
try:
    for utterance, expected in test_cases:
        End2EndTest(
            skill_ids=[],
            source_message=utterance,
            expected_messages=expected,
            minicroft=croft,
        ).execute()
finally:
    croft.stop()
```

---
[← Capture Session](capture-session.md) · [Home](../README.md) · [Pipeline →](pipeline.md)
