# MiniListener: Listener Pipeline Testing

`MiniListener` extends ovoscope's testing capability beyond the skill pipeline
to cover **audio transformer plugins**: the plugins that process raw audio
chunks before speech reaches the intent engine.

## Conceptual Model

Two pipeline modes are supported:

**Audio transformer testing** (e.g. ggwave):
```
Test
────
audio_bytes ──feed_audio──►  [AudioTransformersService + loaded plugins]
                                        │ (FakeBus in-process)
                         ◄──captured───┤ all emitted Messages
                                        ▼
                   assert against expected_types[]
```

**Full pipeline testing** (audio transformers → STT):
```
Test
────
WAV file / bytes
    │
    ▼ AudioTransformersService.transform()
    │
    ▼ stt_instance.execute(AudioData, language)
    │
    ▼ bus.emit("recognizer_loop:utterance")   [if non-empty]
    │
    ▼ captured Messages
```

Rather than injecting a `recognizer_loop:utterance` (as `MiniCroft` does),
`MiniListener` feeds **raw audio bytes** into `AudioTransformersService` —
`ovos_dinkum_listener/transformers.py`: which dispatches them to each
loaded plugin's `feed_audio_chunk()` / `feed_speech_chunk()` / `transform()`
methods.  All `Message` objects emitted on the internal `FakeBus` during that
call are captured and returned.

## Quick Start

**Audio transformer testing** (ggwave):

```python
import types, sys
from unittest.mock import MagicMock

# Stub native ggwave before importing the plugin
_stub = types.ModuleType("ggwave")
_stub.init = MagicMock(return_value=MagicMock())
_stub.free = MagicMock()
_stub.decode = MagicMock(return_value=b"UTT:turn on the lights")
sys.modules.setdefault("ggwave", _stub)

from ovos_audio_transformer_plugin_ggwave import GGWavePlugin
from ovoscope.listener import get_mini_listener

plugin = GGWavePlugin(config={"start_enabled": True})
listener = get_mini_listener(
    plugin_instances={"ovos-audio-transformer-plugin-ggwave": plugin}
)
msgs = listener.feed_audio(b"\x00" * 1024)
assert any(m.msg_type == "recognizer_loop:utterance" for m in msgs)
listener.shutdown()
```

**Streaming real ggwave audio**: the ggwave decoder only fires after it has
accumulated enough frames, so feed the whole waveform with `feed_audio_stream`,
which keeps every message emitted across the stream (unlike `feed_audio`, which
clears its buffer on each call):

```python
import ggwave, numpy as np
from ovos_audio_transformer_plugin_ggwave import GGWavePlugin
from ovoscope.listener import get_mini_listener

# real ggwave waveform (48kHz float32 → 16kHz int16, as the mic would deliver it)
wf = np.frombuffer(ggwave.encode("UTT:turn on the lights", protocolId=1, volume=20),
                   dtype=np.float32)
mic = np.interp(np.linspace(0, len(wf) - 1, int(len(wf) * 16000 / 48000)),
                np.arange(len(wf)), wf)
audio = (np.clip(mic, -1, 1) * 32767).astype(np.int16).tobytes()

plugin = GGWavePlugin(config={"start_enabled": True, "sample_rate": 16000})
listener = get_mini_listener(
    plugin_instances={"ovos-audio-transformer-plugin-ggwave": plugin}
)
msgs = listener.feed_audio_stream(audio, chunk_size=2048)
assert any(m.msg_type == "recognizer_loop:utterance" for m in msgs)
listener.shutdown()
```

**Full pipeline testing** (STT with real WAV):

```python
from unittest.mock import MagicMock
from ovoscope.listener import get_mini_listener

stt = MagicMock()
stt.execute.return_value = "ask not what your country can do for you"

listener = get_mini_listener()
msgs = listener.listen("path/to/jfk.wav", language="en-us", stt_instance=stt)
utt = next(m for m in msgs if m.msg_type == "recognizer_loop:utterance")
assert utt.data["lang"] == "en-us"
assert "ask not" in utt.data["utterances"][0]
listener.shutdown()
```

## API Reference

### `MiniListener`: `ovoscope/listener.py`

**Constructor parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `config` | `dict` | Full OVOS config with `listener.audio_transformers` key |
| `plugin_instances` | `dict[str, Any]` | Pre-instantiated transformer plugins. Bypasses OPM discovery |
| `stt_instance` | `Any` | Optional STT plugin to use in `listen()` |
| `vad_instance` | `Any` | Optional VAD engine (e.g. `MockVADEngine`): `ovoscope/listener.py` |
| `ww_instances` | `dict[str, Any]` | Optional wake-word engines keyed by name: `ovoscope/listener.py` |

**Audio transformer methods:**

| Method | Signature | Description |
|--------|-----------|-------------|
| `feed_audio(chunk)`: `ovoscope/listener.py` | `(bytes) → List[Message]` | Calls `AudioTransformersService.feed_audio()`. Requires `ovos-dinkum-listener`. |
| `feed_speech(chunk)`: `ovoscope/listener.py` | `(bytes) → List[Message]` | Calls `AudioTransformersService.feed_speech()`. Requires `ovos-dinkum-listener`. |
| `feed_audio_stream(chunks, feed, chunk_size)` | `(bytes\|list[bytes], str, int) → List[Message]` | Streams frames in order **without** clearing between them; aggregates all emitted messages. Use for decoders that fire after many frames (ggwave). |
| `transform(chunk)`: `ovoscope/listener.py` | `(bytes) → tuple[bytes, dict, List[Message]]` | Full transform pipeline; returns `(audio, ctx, messages)`. Requires `ovos-dinkum-listener`. |
| `listen(audio, ...)`: `ovoscope/listener.py` | `(audio, language, stt_instance, ...) → List[Message]` | Full pipeline: audio → transformers → STT → utterance message. Requires `ovos-dinkum-listener`. |

**VAD methods:**

| Method | Signature | Description |
|--------|-----------|-------------|
| `is_silence(chunk)`: `ovoscope/listener.py` | `(bytes) → bool` | Delegates to the injected VAD engine. Raises `RuntimeError` if no VAD engine set. |
| `extract_speech(audio)`: `ovoscope/listener.py` | `(bytes) → bytes` | Returns only speech frames from `audio`. Raises `RuntimeError` if no VAD engine set. |

**Wake-word methods:**

| Method | Signature | Description |
|--------|-----------|-------------|
| `detect_wakeword(chunk, ww_name=None)`: `ovoscope/listener.py` | `(bytes, str?) → bool` | Feed `chunk` to the named engine (or first engine if `ww_name=None`). Returns `True` if the engine fires. |
| `scan_for_wakeword(audio, frame_size=2048, ww_name=None)`: `ovoscope/listener.py` | `(bytes\|List[bytes], int, str?) → (bool, int?)` | Feed each frame sequentially; return `(True, frame_index)` on first detection, or `(False, None)` if threshold never reached. |

**Lifecycle:**

| Method | Description |
|--------|-------------|
| `shutdown()`: `ovoscope/listener.py` | Gracefully shuts down transformer plugins and all wake-word engines. |

#### `listen()`: `ovoscope/listener.py`

```
listen(
    audio: bytes | str | Path,
    language: str = "en-us",
    stt_instance: Any = None,
    sample_rate: int = 16000,
    sample_width: int = 2,
) → List[Message]
```

Runs the complete listener pipeline:

1. Reads WAV file (or accepts raw bytes)
2. Passes bytes through `AudioTransformersService.transform()`: all loaded transformer plugins run
3. Converts the (possibly modified) bytes to `AudioData` via `_wav_to_audio_data()`: `listener.py`
4. Calls `stt_instance.execute(audio_data, language)` if provided
5. Emits `recognizer_loop:utterance` on the FakeBus if the transcript is non-empty
6. Returns all captured messages (from transformers **and** the utterance step)

`_wav_to_audio_data(audio, sample_rate, sample_width)`: `listener.py`:

- File path → `AudioData.from_file(path)` (handles WAV/AIFF/FLAC headers)
- Raw bytes → parses the WAV header with the `wave` stdlib module, or falls back to raw PCM if not a valid WAV

**Constructor parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `config` | `dict` | Full OVOS config with `listener.audio_transformers` key |
| `plugin_instances` | `dict[str, Any]` | Pre-instantiated plugins. Bypasses OPM discovery |

### `get_mini_listener()`: `ovoscope/listener.py`

Factory function. Two usage modes:

**Mode A: OPM discovery** (plugin registered as entry point):
```python
listener = get_mini_listener(
    transformer_plugins=["ovos-audio-transformer-plugin-ggwave"]
)
```

**Mode B: direct injection** (bypass OPM, full control over plugin config):
```python
plugin = GGWavePlugin(config={"start_enabled": True})
listener = get_mini_listener(
    plugin_instances={"ovos-audio-transformer-plugin-ggwave": plugin}
)
```

**Mode C: VAD / WakeWord injection:**
```python
from ovoscope.listener import get_mini_listener, MockVADEngine, MockHotWordEngine

listener = get_mini_listener(
    vad_instance=MockVADEngine(),
    ww_instances={"hey_mycroft": MockHotWordEngine(trigger_after=3)},
)
```

`get_mini_listener` accepts these additional keyword arguments for VAD/WW:

| Parameter | Type | Description |
|-----------|------|-------------|
| `vad_plugin` | `str` | OPM VAD plugin name to load via `OVOSVADFactory` |
| `vad_instance` | `Any` | Pre-built VAD engine (e.g. `MockVADEngine()`) |
| `ww_plugin` | `str` | OPM WakeWord plugin name to load via `OVOSWakeWordFactory` |
| `ww_instances` | `dict[str, Any]` | Pre-built WakeWord engines keyed by phrase name |

### `ListenerTest`: `ovoscope/listener.py`

Declarative test runner, analogous to `End2EndTest`.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `plugin_instances` | `dict` | `{}` | Pre-instantiated plugins |
| `transformer_plugins` | `list[str]` | `[]` | OPM plugin names |
| `config` | `dict` | `{}` | Full config override |
| `audio_input` | `bytes` | `b"\x00" * 1024` | Audio to inject |
| `feed_method` | `str` | `"feed_audio"` | Which method to call |
| `expected_types` | `list[str]` | `[]` | Message types that must appear |
| `forbidden_types` | `list[str]` | `[]` | Message types that must NOT appear |

`execute()`: runs the test, raises `AssertionError` on failure, returns the
captured message list on success.

## Plugin Injection vs OPM Discovery

`AudioTransformersService.load_plugins()`: `transformers.py`: uses
`find_audio_transformer_plugins()` from `ovos-plugin-manager` to discover
plugins by entry point.  If a plugin is registered under a legacy group (e.g.
`neon.plugin.audio` instead of `opm.plugin.audio_transformer`), or is not
installed in the test environment, OPM discovery will not find it.

Use **Mode B** (`plugin_instances`) in these cases. The plugin's behaviour
through `AudioTransformersService`'s pipeline methods is identical regardless
of how the plugin was loaded.

## VAD and Wake-Word Testing

`MiniListener` supports **in-process VAD and WakeWord testing** without loading
real models or hardware.

### `MockVADEngine`: `ovoscope/listener.py`

A zero-dependency VAD stub:

- **Silence** = chunk is all `\x00` bytes
- **Speech** = any non-zero byte present
- Tracks the `chunks_processed` counter. `reset()` zeroes it.

```python
from ovoscope.listener import MockVADEngine, MiniListener

vad = MockVADEngine()
listener = MiniListener({"listener": {"audio_transformers": {}}}, vad_instance=vad)

print(listener.is_silence(b"\x00" * 512))   # True
print(listener.is_silence(b"\x01" * 512))   # False
print(listener.extract_speech(b"\x00" * 512 + b"\x01" * 512))  # → b"\x01" * 512
listener.shutdown()
```

### `MockHotWordEngine`: `ovoscope/listener.py`

A controllable WakeWord stub:

- Fires after exactly `trigger_after` calls to `update()`
- Auto-resets after detection (`found_wake_word()` returns `True` once then `False`)
- `reset()` zeroes `update_count` and clears pending detection

```python
from ovoscope.listener import MockHotWordEngine, MiniListener

ww = MockHotWordEngine(key_phrase="hey mycroft", trigger_after=3)
listener = MiniListener(
    {"listener": {"audio_transformers": {}}},
    ww_instances={"hey_mycroft": ww},
)

# Feed 5 frames. Detection fires on frame index 2 (0-indexed)
found, frame = listener.scan_for_wakeword([b"\x00" * 512] * 5)
assert found and frame == 2
listener.shutdown()
```

### `VADTest`: `ovoscope/listener.py`

Declarative VAD test helper:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `vad_instance` | `Any` | `None` | Pre-built VAD engine |
| `vad_plugin` | `str` | `None` | OPM VAD plugin name |
| `audio_input` | `bytes` | `b"\x00"*1024` | Audio to test |
| `expect_silence` | `bool` | `None` | If set, assert `is_silence()` returns this value |
| `expect_speech_bytes` | `bytes` | `None` | If set, assert `extract_speech()` returns this |

```python
from ovoscope.listener import MockVADEngine, VADTest

VADTest(
    vad_instance=MockVADEngine(),
    audio_input=b"\x01" * 512,
    expect_silence=False,
    expect_speech_bytes=b"\x01" * 512,
).execute()
```

### `WakeWordTest`: `ovoscope/listener.py`

Declarative WakeWord test helper:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `ww_instances` | `dict[str, Any]` | `None` | Pre-built engines |
| `ww_plugin` | `str` | `None` | OPM WakeWord plugin name |
| `audio_chunks` | `List[bytes]` | `[]` | Frames to feed sequentially |
| `expect_detected` | `bool` | `None` | If set, assert detection occurred |
| `expected_detection_frame` | `int` | `None` | If set, assert detection at this 0-indexed frame |

```python
from ovoscope.listener import MockHotWordEngine, WakeWordTest

WakeWordTest(
    ww_instances={"hey_mycroft": MockHotWordEngine(trigger_after=2)},
    audio_chunks=[b"\x00" * 512] * 4,
    expect_detected=True,
    expected_detection_frame=1,  # fires on 2nd frame (0-indexed)
).execute()
```

## What MiniListener Does NOT Cover

- Full `DinkumVoiceLoop` state machine: only `AudioTransformersService` and mock VAD/WW engines
- Real hardware audio: inject a WAV file path or raw bytes instead
- Real STT models: `listen()` accepts a mock or real STT plugin, but does not load one automatically

## Cross-References

- `AudioTransformersService`: `ovos-dinkum-listener/ovos_dinkum_listener/transformers.py`
- `AudioData`: `ovos-plugin-manager/ovos_plugin_manager/utils/audio.py`
- `MiniCroft` / `get_minicroft()`: `ovoscope/docs/minicroft.md` (skill pipeline equivalent)
- Audio transformer E2E test: `Transformer plugins/ovos-audio-transformer-plugin-ggwave/test/end2end/test_ggwave_transformer.py`
- STT pipeline E2E test: `STT plugins/ovos-stt-plugin-rover/test/end2end/test_rover_listener_e2e.py`

---
[← PHAL](phal.md) · [Home](../README.md) · [Voice Loop →](voice-loop.md)
