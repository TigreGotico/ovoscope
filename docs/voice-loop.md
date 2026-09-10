# Listener Service Bus-Sequence Testing

OVOS ships more than one listener **service**, and each emits the same
``recognizer_loop:*`` bus events as audio flows through it.  ovoscope provides an
in-process harness for each, sharing one capture bus and one set of assertion
helpers (:class:`ovoscope.voice_loop.ListenerHarness`):

| Service | Harness | Drive |
|---|---|---|
| ovos-dinkum-listener | `MiniVoiceLoop` | `feed_chunks` (wake-word / verifier gate) + `feed_file` (full `DinkumVoiceLoop.run()`) |
| ovos-simple-listener | `MiniSimpleListener` | `feed_file` (full `SimpleListener` loop) |
| mycroft-classic-listener | `MiniClassicListener` | `feed_file` (best-effort) + `bridge_recognizer_loop_to_bus` |

Each harness wires its real listener to a `FakeBus` with mock mic / VAD / STT /
wake-word plugins, runs it over an arbitrary audio file (or PCM frames), and
captures the emitted bus sequence.  The mocks live in
`ovoscope.voice_loop`: `MockFileMicrophone`, `MockStreamingSTT`,
`MockVADEngine`, `MockHotWordEngine`.

## Shared assertion helpers

Every harness inherits these (each takes an optional message list, defaulting to
the last feed result, and returns the checked list):

- `assert_record_begin_emitted()` (`recognizer_loop:record_begin`) present.
- `assert_wakeword_detected()`: both `recognizer_loop:wakeword` and `…:record_begin`.
- `assert_wakeword_suppressed()`: neither wake-word nor record-begin present.
- `assert_utterance_emitted(utterance=None)`: a `recognizer_loop:utterance` (optionally with the given text).

---

## ovos-dinkum-listener: `MiniVoiceLoop`

### Wake-word / verifier gate (`feed_chunks`)

Feeds PCM frames straight through `DinkumVoiceLoop._detect_ww` to assert the
wake-word detection and the verifier chain in isolation.

```python
from unittest.mock import Mock
from ovoscope.voice_loop import MiniVoiceLoop, MockHotWordEngine

SILENT = b"\x00" * 512

def loop(verifiers):
    ww = MockHotWordEngine("hey_mycroft", trigger_after=3)
    return MiniVoiceLoop(ww_instances={"hey_mycroft": ww}, verifiers=verifiers)

acc = Mock(); acc.verify.return_value = True
with loop([acc]) as vl:
    vl.assert_wakeword_detected(vl.feed_chunks([SILENT] * 5))

rej = Mock(); rej.verify.return_value = False
with loop([rej]) as vl:
    vl.assert_wakeword_suppressed(vl.feed_chunks([SILENT] * 5))

boom = Mock(); boom.verify.side_effect = RuntimeError("boom")
with loop([boom]) as vl:                       # fail-open
    vl.assert_record_begin_emitted(vl.feed_chunks([SILENT] * 5))
```

| Sequence | Expected bus events |
|---|---|
| WW detected + all verifiers accept | `recognizer_loop:wakeword` + `…:record_begin` |
| WW detected + a verifier rejects | suppressed: no `recognizer_loop:*` |
| WW detected + a verifier raises (fail-open) | `…:record_begin` emitted |
| No WW detected | no `recognizer_loop:*` |

The verifier gate lives inside `DinkumVoiceLoop._detect_ww` and is only present
in ovos-dinkum-listener builds that ship the hotword-verifier feature
(`HotwordContainer.verify`).  On a build without it the gate is absent and a
detection is never suppressed: assert accordingly for the version under test.

### Full loop from an audio file (`feed_file`)

Runs the whole `DinkumVoiceLoop.run()` state machine over an audio file through a
`MockFileMicrophone`, emitting the full record-begin → record-end → utterance
sequence.

```python
from ovoscope.voice_loop import MiniVoiceLoop, MockStreamingSTT

stt = MockStreamingSTT(transcript="what time is it")
with MiniVoiceLoop(stt_instance=stt) as vl:
    msgs = vl.feed_file("command.wav")
    vl.assert_record_begin_emitted(msgs)
    vl.assert_utterance_emitted("what time is it", msgs)
```

An empty `MockStreamingSTT` transcript yields
`recognizer_loop:speech.recognition.unknown` instead of an utterance.

`MiniVoiceLoop` builds a real `DinkumVoiceLoop`; it raises `RuntimeError` when
ovos-dinkum-listener is not installed.

---

## ovos-simple-listener: `MiniSimpleListener`

Drives the real `SimpleListener` thread with the canonical bus callbacks (a
per-instance mirror of `OVOSCallbacks`).

```python
from ovoscope.simple_listener import MiniSimpleListener
from ovoscope.voice_loop import MockHotWordEngine, MockStreamingSTT

with MiniSimpleListener(
    wakeword=MockHotWordEngine("hey_mycroft", trigger_after=2),
    stt_instance=MockStreamingSTT(transcript="turn on the lights"),
) as sl:
    msgs = sl.feed_file("command.wav")
    sl.assert_record_begin_emitted(msgs)
    sl.assert_utterance_emitted("turn on the lights", msgs)
```

The default timing is tightened (`min_speech_seconds=0`,
`max_silence_seconds=0.1`) so a file-driven command ends promptly; raise them to
mimic production. Raises `RuntimeError` when ovos-simple-listener is absent.

---

## mycroft-classic-listener: `MiniClassicListener`

The classic listener is a threaded, energy-based pipeline. Two entry points:

### Event bridge (always available)

`bridge_recognizer_loop_to_bus(loop, bus)` forwards a `RecognizerLoop`'s internal
EventEmitter events onto a `FakeBus`, exactly as the classic listener's
`service.py` does. Drive the loop with real audio and assert on the bus:

```python
from ovoscope.classic_listener import bridge_recognizer_loop_to_bus
from ovos_utils.fakebus import FakeBus

bus = FakeBus()
bridge_recognizer_loop_to_bus(loop, bus)   # loop = a RecognizerLoop
```

### Best-effort file drive

Injects a file-backed audio source and mock wake-word / STT into a fresh
`RecognizerLoop` and runs it to completion:

```python
from ovoscope.classic_listener import MiniClassicListener
from ovoscope.voice_loop import MockHotWordEngine, MockStreamingSTT

with MiniClassicListener(
    wakeword=MockHotWordEngine("hey_mycroft", trigger_after=1),
    stt_instance=MockStreamingSTT(transcript="hello world"),
) as cl:
    msgs = cl.feed_file("command.wav", tail_silence_seconds=3.0)
    cl.assert_record_begin_emitted(msgs)
    cl.assert_utterance_emitted("hello world", msgs)
```

The file drive depends on the energy-based recogniser, so assertions are
presence-based (a busy pipeline may emit more than one cycle before it is
stopped). Use `classic_listener_available()` to gate tests on the environment;
`MiniClassicListener(...)` (built mode) raises `RuntimeError` when the package is
absent.

---

## Declarative helper: `VoiceLoopTest`

For the dinkum backend, `VoiceLoopTest` runs a scenario and asserts in one call,
using `feed_chunks` by default, or `feed_file` when `audio_file` is set:

```python
from unittest.mock import Mock
from ovoscope.voice_loop import VoiceLoopTest, MockHotWordEngine, MockStreamingSTT

# verifier gate
accepting = Mock(); accepting.verify.return_value = True
VoiceLoopTest(
    ww_instances={"hey_mycroft": MockHotWordEngine(trigger_after=3)},
    verifiers=[accepting],
    audio_chunks=[b"\x00" * 512] * 5,
    expect_record_begin=True,
).execute()

# full loop from a file
VoiceLoopTest(
    audio_file="command.wav",
    stt_instance=MockStreamingSTT(transcript="what time is it"),
    expect_utterance="what time is it",
).execute()
```

## API surface

| Symbol | Description |
|---|---|
| `ListenerHarness` | Base: FakeBus capture + assertion helpers + file-mic. |
| `MiniVoiceLoop` / `get_mini_voice_loop` | ovos-dinkum-listener harness + factory. |
| `MiniHotwordContainer` | Controllable hotword container with a fail-open verifier chain. |
| `MiniSimpleListener` / `get_mini_simple_listener` | ovos-simple-listener harness + factory. |
| `MiniClassicListener` | mycroft-classic-listener harness (best-effort). |
| `bridge_recognizer_loop_to_bus` / `classic_listener_available` | Classic event-bridge + capability probe. |
| `MockFileMicrophone`, `MockStreamingSTT`, `MockVADEngine`, `MockHotWordEngine` | Mock plugins shared across backends. |
| `VoiceLoopTest` | Declarative dinkum scenario runner. |

---
[← Listener](listener.md) · [Home](../README.md) · [GUI Testing →](gui-testing.md)
