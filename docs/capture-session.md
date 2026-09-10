# CaptureSession
`CaptureSession` subscribes to all messages on the `FakeBus` and records them during a single test interaction. It handles synchronous responses (ordered, from the intent pipeline) and asynchronous responses (from external threads, unordered).
## Class: `CaptureSession` (`ovoscope/__init__.py`)
```python
from ovoscope import CaptureSession
```
A `dataclass` that wraps a `MiniCroft` and manages message collection for one test interaction.
`CaptureSession.finish` (`ovoscope/__init__.py`)

> **Idempotency:** `finish()` may be called multiple times safely: subsequent calls
> return the same message list without re-subscribing or clearing state.
### Fields
| Field | Type | Default | Description |
|---|---|---|---|
| `minicroft` | `MiniCroft` | required | The runtime to capture from |
| `responses` | `list[Message]` | `[]` | Ordered synchronous messages captured |
| `async_responses` | `list[Message]` | `[]` | Async messages (arrive from external threads, unordered) |
| `eof_msgs` | `list[str]` | `["ovos.utterance.handled"]` | Message types that signal end of interaction |
| `terminal_signals` | `bool` | `True` | Also end capture on the pipeline's own terminal markers (see below) |
| `ignore_messages` | `list[str]` | `DEFAULT_IGNORED` | Message types to discard |
| `async_messages` | `list[str]` | `[]` | Message types to route to `async_responses` instead |
| `done` | `threading.Event` |: | Set when an EOF message is received |
### Methods
#### `capture(source_message, timeout=20)`
Emits `source_message` on the bus and waits for an EOF message (or timeout). Subsequent calls on the same session accumulate into `responses`.
```python
capture = CaptureSession(croft, eof_msgs=["ovos.utterance.handled"])
capture.capture(utterance_msg, timeout=10)
```
#### `finish() -> list[Message]`
Signals end of capture, unsubscribes from the bus, and returns the collected `responses`.
---
## Message Routing
Messages are sorted into three buckets on arrival:
```
incoming message
        │
        ├─ msg_type in async_messages?  → async_responses (unordered)
        ├─ msg_type in ignore_messages? → discarded
        └─ otherwise                   → responses (ordered)
eof_msgs trigger done.set() → capture.wait() returns
```
### Terminal signals
An utterance's lifecycle is over the instant this lands on the bus, whether
it was matched or not:
```python
TERMINAL_SIGNALS = ["ovos.utterance.handled"]
```
`ovos.utterance.handled` fires exactly once per utterance, always last —
after any matched, unmatched, or fallback handling — which is what makes it
safe to use as an early-exit signal without dropping messages that follow
it. By default (`terminal_signals=True`) `capture()` merges this into
whatever `eof_msgs` was given, so a caller who narrows `eof_msgs` down to a
topic that only a *matched* utterance reaches (e.g. to dodge a
`get_response()` deadlock on a specific skill handler) still gets a prompt
return for an unmatched or misrouted utterance instead of paying the full
`timeout` on every such row. Set `terminal_signals=False` to opt back into
the exact `eof_msgs`-only behaviour. The merge is skipped whenever
`eof_count > 1`: that knob counts occurrences of one topic across several
concurrent lifecycles, and letting the terminal signal count toward it would
end capture after only one lifecycle finished instead of all of them.

### Default ignored messages
```python
DEFAULT_IGNORED = ["ovos.skills.settings_changed"] + TRAINING_NOISE
```
`TRAINING_NOISE` (`["mycroft.skills.trained"]`) is MiniCroft's own
boot/training orchestration noise, not something any scenario's message
sequence should assert on: a pipeline plugin can legitimately re-emit it
mid-capture (e.g. a `secondary_langs` re-training pass), and it is filtered
out before any exact sequence comparison runs. This is deliberately narrow —
only genuine orchestration noise goes in `TRAINING_NOISE`, never a general
subsequence-matching mode. Comparisons stay exact on everything else, so a
message that is missing or duplicated for real still fails the test.
### Default GUI ignored (when `ignore_gui=True` on `End2EndTest`)
```python
GUI_IGNORED = [
    "gui.clear.namespace",
    "gui.value.set",
    "mycroft.gui.screen.close",
    "gui.page.show",
]
```
These are excluded by default because GUI namespace updates are frequent and rarely the focus of skill logic tests.
---
## Direct Usage
`CaptureSession` can be used without `End2EndTest` for lower-level scenarios:
```python
from ovoscope import get_minicroft, CaptureSession
from ovos_bus_client.message import Message
from ovos_bus_client.session import Session
croft = get_minicroft(["skill-weather.openvoiceos"])
session = Session("test-123")
utterance = Message(
    "recognizer_loop:utterance",
    {"utterances": ["what is the weather?"], "lang": "en-us"},
    {"session": session.serialize(), "source": "A", "destination": "B"},
)
capture = CaptureSession(croft)
capture.capture(utterance, timeout=15)
messages = capture.finish()
for msg in messages:
    print(msg.msg_type, msg.data)
croft.stop()
```
---
## Multi-turn Capture
Emit multiple source messages into the same `CaptureSession` to simulate a multi-turn conversation. The session from the last received message is propagated into each subsequent source message:
```python
capture = CaptureSession(croft, eof_msgs=["ovos.utterance.handled"])
capture.capture(first_utterance, timeout=10)
# inject session from last received message into follow-up
follow_up.context["session"] = capture.responses[-1].context["session"]
capture.capture(follow_up, timeout=10)
all_messages = capture.finish()
```
`End2EndTest` does this automatically when `source_message` is a list.

---
[← MiniCroft](minicroft.md) · [Home](../README.md) · [End2EndTest →](end2end-test.md)
