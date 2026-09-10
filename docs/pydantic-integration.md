# OvoScope + ovos-pydantic-models Integration
OvoScope currently operates on untyped `ovos_bus_client.message.Message` objects: dicts with string keys. `ovos-pydantic-models` provides typed Pydantic v2 models for every OVOS message type. This document describes how they can be used together and what a deeper integration could look like.
---
## The Problem Today
Writing test fixtures by hand is verbose and error-prone:
```python
# untyped: no validation, any typo silently passes
expected = Message("recognizer_loop:utterance", {"utterances": ["hello"], "lang": "en-us"}, {})
```
`Message` is a raw dict wrapper. There is no validation of field names, no type checking, and no autocomplete. A typo in a field name (`"utterance"` instead of `"utterances"`) silently produces a wrong test.
---
## Bridge: Converting Between Message and Pydantic
`ovos_bus_client.message.Message` and `OpenVoiceOSMessage` share the same three-field structure (`type`/`message_type`, `data`, `context`). A bridge needs only two functions:
```python
from ovos_bus_client.message import Message
from ovos_pydantic_models.message import OpenVoiceOSMessage
def to_bus_message(pydantic_msg: OpenVoiceOSMessage) -> Message:
    """Convert a pydantic model to an ovos-bus-client Message."""
    d = pydantic_msg.model_dump()
    return Message(
        d["message_type"],
        d["data"],
        d["context"],
    )
def from_bus_message(bus_msg: Message, model: type[OpenVoiceOSMessage]) -> OpenVoiceOSMessage:
    """Parse a received bus Message into a typed pydantic model."""
    return model.model_validate({
        "message_type": bus_msg.msg_type,
        "data": bus_msg.data,
        "context": bus_msg.context,
    })
```
These two functions are all that's needed to use typed models with OvoScope today, without any changes to OvoScope itself.
---
## Usage Pattern 1: Typed Source Messages
Use pydantic models to construct source messages, then convert:
```python
from ovoscope import End2EndTest
from ovos_bus_client.message import Message
from ovos_bus_client.session import Session
from ovos_pydantic_models import RecognizerLoopUtteranceMessage, RecognizerLoopUtteranceData
# typed construction: validated at instantiation
utterance_model = RecognizerLoopUtteranceMessage(
    data=RecognizerLoopUtteranceData(utterances=["what is the weather?"], lang="en-us"),
)
session = Session("test-123")
bus_msg = to_bus_message(utterance_model)
bus_msg.context["session"] = session.serialize()
bus_msg.context["source"] = "A"
bus_msg.context["destination"] = "B"
End2EndTest(
    skill_ids=["skill-weather.openvoiceos"],
    source_message=bus_msg,
    expected_messages=[...],
).execute()
```
Benefit: `RecognizerLoopUtteranceData` validates that `utterances` is a `list[str]` and `lang` is a string. A missing `utterances` field raises `ValidationError` at construction time, not a silent wrong test.
---
## Usage Pattern 2: Typed Expected Messages
Use pydantic models to build expected messages. This documents intent and catches field-name mistakes:
```python
from ovos_pydantic_models import SpeakMessage, SpeakData, CompleteIntentFailureMessage, CompleteIntentFailureData
expected = [
    to_bus_message(RecognizerLoopUtteranceMessage(
        data=RecognizerLoopUtteranceData(utterances=["what is the weather?"], lang="en-us")
    )),
    to_bus_message(SpeakMessage(
        data=SpeakData(utterance="It is 22 degrees in London.")
    )),
    to_bus_message(OvosUtteranceHandledMessage()),
]
End2EndTest(
    skill_ids=["skill-weather.openvoiceos"],
    source_message=bus_msg,
    expected_messages=expected,
).execute()
```
Because `End2EndTest` checks only the data keys you specify (subset match), you can omit optional fields in expected messages: this works the same as before, but field names are now validated at Python parse time.
---
## Usage Pattern 3: Typed Assertions on Received Messages
After a test captures messages, convert received `Message` objects to their typed counterparts for richer assertions:
```python
from ovoscope import get_minicroft, CaptureSession
from ovos_pydantic_models import SpeakMessage
croft = get_minicroft(["skill-weather.openvoiceos"])
capture = CaptureSession(croft)
capture.capture(bus_msg, timeout=10)
messages = capture.finish()
croft.stop()
# find the speak message and parse it
speak_msgs = [m for m in messages if m.msg_type == "speak"]
assert len(speak_msgs) == 1
typed_speak = from_bus_message(speak_msgs[0], SpeakMessage)
assert "london" in typed_speak.data.utterance.lower()
assert typed_speak.data.expect_response is False
```
This is cleaner than `msg.data["utterance"]`: you get IDE autocomplete and the field contract is explicit.
---
## Usage Pattern 4: Type-safe Test Helpers
Build helpers that combine the two:
```python
def assert_speak(received_msg: Message, expected_utterance: str | None = None):
    """Assert a received message is a valid speak message."""
    typed = from_bus_message(received_msg, SpeakMessage)  # raises if invalid
    if expected_utterance is not None:
        assert typed.data.utterance == expected_utterance
    return typed  # return for further inspection
def make_utterance(text: str, lang: str = "en-us", session: Session | None = None) -> Message:
    """Build a typed recognizer_loop:utterance message."""
    model = RecognizerLoopUtteranceMessage(
        data=RecognizerLoopUtteranceData(utterances=[text], lang=lang)
    )
    msg = to_bus_message(model)
    if session:
        msg.context["session"] = session.serialize()
    return msg
```
---
## Deeper Integration: What OvoScope Could Gain
The patterns above work today with no changes to OvoScope. A deeper integration would add native support for pydantic models as an alternative to `Message`:
### Idea 1: Accept pydantic models directly in `End2EndTest`
```python
# instead of requiring to_bus_message() manually:
End2EndTest(
    skill_ids=[...],
    source_message=RecognizerLoopUtteranceMessage(...),   # pydantic directly
    expected_messages=[SpeakMessage(...), OvosUtteranceHandledMessage()],
)
```
Implementation: `__post_init__` could detect `OpenVoiceOSMessage` instances and call `to_bus_message()` automatically.
### Idea 2: `assert_message_type()` helper on `End2EndTest`
```python
test.assert_message_type(index=1, model=SpeakMessage)
# verifies received[1] can be deserialized as SpeakMessage
```
### Idea 3: Typed capture result
After `execute()`, expose captured messages as typed models where possible:
```python
test.execute()
speak = test.received_as(index=1, model=SpeakMessage)
assert speak.data.expect_response is False
```
### Idea 4: JSON schema validation in assertions
Instead of only checking key/value subsets, optionally validate each received message against the pydantic schema for its type:
```python
End2EndTest(
    ...,
    validate_schemas=True,  # each received message must parse as its pydantic model
)
```
This would catch malformed messages from skills (e.g. a skill emitting `speak` with missing `utterance`).
---
## Dependency Consideration
`ovos-pydantic-models` is a pure Pydantic v2 package with no OVOS runtime dependencies. OvoScope depends on `ovos-core>=2.0.4a2`. The optional dependency is declared in `pyproject.toml`:
```toml
[project.optional-dependencies]
pydantic = ["ovos-pydantic-models>=0.1.0"]
```
Install with:
```bash
pip install ovoscope[pydantic]
```
The bridge functions (`to_bus_message`, `from_bus_message`, `validate_fixture`) live in
`ovoscope.pydantic_helpers` and guard their imports conditionally: the module can be imported
without `ovos-pydantic-models` installed, but calling any function raises a clear `ImportError`
pointing to the extras install command:
```python
# safe to import regardless of whether pydantic extras are installed
from ovoscope.pydantic_helpers import to_bus_message  # ImportError only on call, not import
```
---
## Summary
| Pattern | What you get | Status |
|---|---|---|
| Typed source messages via `to_bus_message()` | Validation at construction | Done, in `ovoscope.pydantic_helpers` |
| Typed expected messages via `to_bus_message()` | Field name validation | Done, in `ovoscope.pydantic_helpers` |
| Typed assertions via `from_bus_message()` | IDE autocomplete, field contracts | Done, in `ovoscope.pydantic_helpers` |
| Fixture validation via `validate_fixture()` | Clear errors on malformed JSON | Done, in `ovoscope.pydantic_helpers` |
| Native pydantic in `End2EndTest` | Direct API, no `to_bus_message` call | Future: `__post_init__` auto-conversion |
| Schema validation in assertions | Catch malformed skill messages | Future: `validate_schemas=True` flag |
Install the extras to use the implemented patterns: `pip install ovoscope[pydantic]`

---
[← Pipeline](pipeline.md) · [Home](../README.md) · [Audio Testing →](audio-testing.md)
