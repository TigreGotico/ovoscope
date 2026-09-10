# OvoScope Documentation
**OvoScope** is an end-to-end testing framework for OVOS skills. It runs a lightweight in-process OVOS Core using a `FakeBus`, loads real skill plugins, and captures every bus message produced in response to a test utterance: then asserts against the captured sequence.
## Contents
| Document | Description |
|---|---|
| [usage-guide.md](usage-guide.md) | **Start here**: tutorial: from zero to your first end2end test |
| [cli.md](cli.md) | `ovoscope` command-line tool: record, run, diff, validate, coverage |
| [ci-integration.md](ci-integration.md) | Wiring ovoscope into GitHub Actions CI with gh-automations |
| [minicroft.md](minicroft.md) | `MiniCroft`: in-process skill runtime |
| [capture-session.md](capture-session.md) | `CaptureSession`: message capture during a test |
| [end2end-test.md](end2end-test.md) | `End2EndTest`: full test runner reference |
| [e2e-pipeline-harness.md](e2e-pipeline-harness.md) | `E2EPipelineHarness`, `wait_for_match`, `make_utterance_message`: testing a single pipeline plugin (Adapt/Padatious/...) against raw bus messages |
| [intent-cases.md](intent-cases.md) | `IntentCase`, `register_intent_case_tests`: file-based intent test cases (`.intent.test`) with per-pipeline-family generated tests |
| [multi-engine-golden.md](multi-engine-golden.md) | `run_golden_suite`, `build_engine_intents`, `assert_gates`: golden-utterance corpus scored against Padatious/Padacioso/Nebulento (gating) and Model2Vec's `m2v-prototype` (informational), with arena-shaped prediction rows |
| [pydantic-integration.md](pydantic-integration.md) | Using `ovos-pydantic-models` with OvoScope |
| [audio-testing.md](audio-testing.md) | `AudioServiceHarness`, `PlaybackServiceHarness`: testing audio services |
| [media-testing.md](media-testing.md) | `OCPPlayerHarness`, `OCPCaptureSession`, `MockOCPBackend`: testing the `ovos-media` OCP player (and driving a real OCP backend) |
| [media-provider-testing.md](media-provider-testing.md) | `MediaProviderHarness`: testing `opm.media.provider` catalog/search plugins |
| [ocp.md](ocp.md) | `OCPTest`: testing legacy OCP search skills (`@ocp_search`) |
| [phal.md](phal.md) | `MiniPHAL`, `PHALTest`: testing PHAL plugins without physical hardware |
| [listener.md](listener.md) | `MiniListener`, `get_mini_listener`, `ListenerTest`, `MockVADEngine`, `MockHotWordEngine`, `VADTest`, `WakeWordTest`: testing audio transformer plugins, STT pipeline, VAD, and wake-word |
| [voice-loop.md](voice-loop.md) | `MiniVoiceLoop` / `MiniSimpleListener` / `MiniClassicListener`: file-driven bus-sequence testing for the ovos-dinkum, ovos-simple, and mycroft-classic listener services (wake-word → record-begin → utterance), with verifier-chain gating |
| [gui-testing.md](gui-testing.md) | `GUICaptureSession`: asserting GUI page navigation and namespace values |
| [bus-coverage.md](bus-coverage.md) | `BusCoverageTracker`, `BusCoverageReport`: measuring handler and emitter coverage per skill |
## Conceptual Model
```
Test                         FakeBus
────                         ───────
source_message ──emit──►  [MiniCroft + loaded skills]
                                  │
                    ◄──capture────┤ all emitted messages
                                  │ until EOF message
                                  ▼
            assert against expected_messages[]
```
The key insight is that OVOS skill behaviour is fully observable through bus messages. OvoScope intercepts every message on the in-process `FakeBus`, so the entire skill interaction (intent matching, converse, fallback, speak, session changes), is captured and verifiable.
## Quick Start
```bash
pip install ovoscope
```
```python
from ovoscope import End2EndTest
from ovos_bus_client.message import Message
from ovos_bus_client.session import Session
session = Session("test-123")
utterance = Message(
    "recognizer_loop:utterance",
    {"utterances": ["hello world"], "lang": "en-us"},
    {"session": session.serialize(), "source": "A", "destination": "B"},
)
test = End2EndTest(
    skill_ids=["skill-hello-world.openvoiceos"],
    source_message=utterance,
    expected_messages=[
        utterance,
        Message("speak", {"utterance": "Hello!"}),
        Message("ovos.utterance.handled", {}),
    ],
)
test.execute()
```
## Recording Mode
Instead of writing expected messages by hand, record them from a live run:
```python
test = End2EndTest.from_message(
    message=utterance,
    skill_ids=["skill-hello-world.openvoiceos"],
)
test.save("tests/hello_world.json")
```
Then replay later:
```python
test = End2EndTest.from_path("tests/hello_world.json")
test.execute()
```
## Public API
All primary classes and the factory function are importable from `ovoscope` directly:
```python
from ovoscope import (
    MiniCroft,           # in-process skill runtime
    get_minicroft,       # factory: create + wait for READY
    CaptureSession,      # message recorder for a single interaction
    End2EndTest,         # declarative test runner
    GUICaptureSession,   # capture gui.* messages for GUI assertions
    MiniListener,        # in-process audio transformer / VAD / WakeWord pipeline
    get_mini_listener,   # factory: create MiniListener with plugins
    ListenerTest,        # declarative audio transformer test runner
)
# VAD / WakeWord helpers (from ovoscope.listener)
from ovoscope.listener import (
    MockVADEngine,       # silence = all-zero bytes; speech = any non-zero
    MockHotWordEngine,   # fires after trigger_after update() calls
    VADTest,             # declarative VAD test runner
    WakeWordTest,        # declarative WakeWord test runner
)
# Listener-service bus-sequence testing (dinkum / simple / classic)
from ovoscope import (
    MiniVoiceLoop,        # ovos-dinkum-listener: feed PCM chunks or an audio file
    MiniSimpleListener,   # ovos-simple-listener: drive the loop over an audio file
    MiniClassicListener,  # mycroft-classic-listener: best-effort file drive + bridge
    get_mini_voice_loop,  # factory: create MiniVoiceLoop
    VoiceLoopTest,        # declarative wake-word → bus-sequence test runner
    MiniHotwordContainer, # controllable hotword container with a verifier chain
    MockFileMicrophone,   # file-backed mic plugin shared across listener harnesses
    MockStreamingSTT,     # configurable transcript STT mock
)
```
Type aliases also exported:
```python
from ovoscope import SerializedMessage, SerializedTest
```
## Dependencies
| Package | Role |
|---|---|
| `ovos-core >= 2.0.4a2` | `SkillManager`, `IntentService`, `FakeBus`, `SessionManager` |
Python 3.10+ is required (uses `match`/structural typing in ovos-core).
## Listener Pipeline Testing

`MiniListener` extends ovoscope to cover **audio transformer plugins**: the
plugins that process raw audio before it reaches the intent engine.  It wraps
`AudioTransformersService` on a `FakeBus` so transformer behaviour is fully
observable through bus messages.

See [listener.md](listener.md) for full API reference and usage patterns.

```python
from ovoscope import get_mini_listener
from ovos_audio_transformer_plugin_ggwave import GGWavePlugin

plugin = GGWavePlugin(config={"start_enabled": True})
listener = get_mini_listener(
    plugin_instances={"ovos-audio-transformer-plugin-ggwave": plugin}
)
msgs = listener.feed_audio(b"\x00" * 1024)
listener.shutdown()
```

## Listener-Service Bus-Sequence Testing

OVOS has several listener **services**: ovos-dinkum-listener, ovos-simple-listener,
and mycroft-classic-listener: each emitting the same `recognizer_loop:*` bus
events.  `MiniVoiceLoop`, `MiniSimpleListener`, and `MiniClassicListener` each
wire their real service to a `FakeBus` with mock mic/VAD/STT/wake-word plugins,
drive it over an arbitrary audio file (or PCM frames), and capture the emitted
sequence: sharing one set of assertion helpers.  `MiniVoiceLoop` also exercises
the dinkum verifier-chain gate that decides whether a detection survives.

See [voice-loop.md](voice-loop.md) for full API reference and usage patterns.

```python
from unittest.mock import Mock
from ovoscope.voice_loop import MiniVoiceLoop, MockHotWordEngine

ww = MockHotWordEngine("hey_mycroft", trigger_after=3)
accepting = Mock(); accepting.verify.return_value = True

with MiniVoiceLoop(ww_instances={"hey_mycroft": ww},
                   verifiers=[accepting]) as vl:
    msgs = vl.feed_chunks([b"\x00" * 512] * 5)
    vl.assert_record_begin_emitted(msgs)
```

## What OvoScope Does NOT Do
- Does not start a real WebSocket MessageBus server: uses `FakeBus` (in-process pub/sub).
- `MiniCroft` / `End2EndTest`, the harness this page documents, does not load PHAL plugins or the
  audio service; it only loads skills and the intent pipeline. PHAL plugins and audio services have
  their own dedicated harnesses: [phal.md](phal.md) (`MiniPHAL`, `PHALTest`) and
  [audio-testing.md](audio-testing.md) (`AudioServiceHarness`, `PlaybackServiceHarness`).
- Does not test GUI rendering: GUI namespace messages are ignored by default (`ignore_gui=True`).
- Does not test TTS: operates at the `recognizer_loop:utterance` level (see [audio-testing.md](audio-testing.md) for TTS lifecycle testing).
- `MiniListener` covers `AudioTransformersService`, the STT pipeline, and mock VAD/WakeWord engines. `MiniVoiceLoop` / `MiniSimpleListener` / `MiniClassicListener` drive the dinkum, simple, and classic listener **services** from an audio file and capture the `recognizer_loop:*` bus sequence; the classic file drive is best-effort (energy-based pipeline).
## Quick Links
| Resource | Path |
|---|---|
| Common questions | [`../FAQ.md`](../FAQ.md) |
| Change log | [`../CHANGELOG.md`](../CHANGELOG.md) |
## Who Uses ovoscope
| Repo | Test location | Notes |
|---|---|---|
| `ovos-core` | `ovos-core/test/end2end/` | Adapt + Padatious pipeline tests, blacklist tests |
| `Skills/ovos-skill-hello-world` | `Skills/ovos-skill-hello-world/test/test_helloworld.py` | Canonical example: Adapt + Padatious match + no-match |
## Cross-References
- [ovos-core](https://github.com/OpenVoiceOS/ovos-core): `SkillManager`, `IntentService` (runtime dependency)
- [ovos-utils](https://github.com/OpenVoiceOS/ovos-utils): `FakeBus`, `ProcessState`
- [ovos-workshop](https://github.com/OpenVoiceOS/ovos-workshop): `OVOSSkill` base class
- [ovos-bus-client](https://github.com/OpenVoiceOS/ovos-bus-client): `Message`, `Session`, `SessionManager`
- [ovos-pydantic-models](https://github.com/OpenVoiceOS/ovos-pydantic-models): optional typed message models (see [pydantic-integration.md](pydantic-integration.md))
