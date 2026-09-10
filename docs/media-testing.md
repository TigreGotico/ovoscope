# Media / OCP Testing with ovoscope

This document describes how to test `ovos-media` services: specifically the
`OCPMediaPlayer` state machine: using the harness classes provided in
`ovoscope.media`.

> **Prerequisite:** Media testing harnesses require `ovos-media` to be installed.
> Install it with: `pip install ovoscope ovos-media`

## When to Use Which Class

| Scenario | Class |
|---|---|
| Testing `OCPMediaPlayer` play/pause/stop/next/prev state machine | `OCPPlayerHarness` |
| Testing duck/unduck (volume lowering) and cork/uncork (pause on listen) | `OCPPlayerHarness` |
| Capturing and asserting OCP bus message sequences | `OCPCaptureSession` |
| Simulating a broken or unplayable stream | `MockOCPBackend.simulate_invalid_stream()` |
| Simulating end-of-track to trigger auto-advance | `MockOCPBackend.simulate_end()` |

## OCPPlayerHarness

`OCPPlayerHarness` (`ovoscope/media.py`)

Wraps a real `OCPMediaPlayer` (`ovos_media.player`) with a `MockOCPBackend` on a
`FakeBus`. All heavy dependencies are patched out:

- `ovos_media.player.AudioService`: mocked. `MockOCPBackend` is injected as the
  sole audio backend
- `ovos_media.player.VideoService`: mocked
- `ovos_media.player.WebService`: mocked
- `ovos_media.player.OcpMprisExporter`: mocked (no D-Bus session required)
- `ovos_media.player.GUIInterface`: mocked (exposed as `harness.gui`), only
  on `ovos-media` builds that define it. Builds without in-core GUI
  integration skip this patch.
- `ovos_media.player.OCPMediaCatalog`: mocked
- `ovos_media.player.Configuration`: returns `{"media": {}}`

### Basic Usage

```python
from ovoscope.media import OCPPlayerHarness
from ovos_utils.ocp import MediaEntry, PlaybackType, PlayerState

with OCPPlayerHarness() as h:
    entry = MediaEntry(
        uri="http://example.com/song.mp3",
        playback=PlaybackType.AUDIO,
        title="Test Track",
    )
    h.play(entry)
    h.assert_player_state(PlayerState.PLAYING)
    assert h.backend.is_playing

    h.pause()
    h.assert_player_state(PlayerState.PAUSED)

    h.resume()
    h.assert_player_state(PlayerState.PLAYING)

    h.stop()
    h.assert_player_state(PlayerState.STOPPED)
```

### Queue Navigation

```python
from ovoscope.media import OCPPlayerHarness
from ovos_utils.ocp import MediaEntry, PlaybackType, PlayerState
from ovos_bus_client.message import Message

with OCPPlayerHarness() as h:
    track1 = MediaEntry(uri="http://example.com/1.mp3",
                        playback=PlaybackType.AUDIO)
    track2 = MediaEntry(uri="http://example.com/2.mp3",
                        playback=PlaybackType.AUDIO)

    # Emit play with a playlist so the queue has two tracks
    h.bus.emit(Message("ovos.common_play.play", {
        "media": track1.as_dict,
        "playlist": [track1.as_dict, track2.as_dict],
    }))
    import time
    time.sleep(0.05)

    h.assert_now_playing_uri("http://example.com/1.mp3")
    h.next_track()
    h.assert_now_playing_uri("http://example.com/2.mp3")
```

### Duck / Unduck vs Cork / Uncork

`OCPMediaPlayer` distinguishes two separate mechanisms for voice-assistant
interruptions.  Understanding the difference is essential for writing correct
tests.

#### Ducking: lower volume, keep playing

Ducking happens when the assistant **speaks** (TTS output).  The player stays
in ``PLAYING`` state. Only the audio backend volume is reduced.

| Bus message | Handler | Effect |
|---|---|---|
| `ovos.audio.output.started` / `ovos.common_play.duck` | `handle_duck_request` | Calls `audio_service.lower_volume()`, sets `_paused_on_duck=True` |
| `ovos.audio.output.ended` / `ovos.common_play.unduck` | `handle_unduck_request` | Calls `audio_service.restore_volume()` whenever `_paused_on_duck` is True, **regardless of player state** |

`OCPPlayerHarness.duck()`/`unduck()` emit the spec-namespace
`ovos.audio.output.started`/`ended` — matching what the real `ovos-audio`
service emits on every speech begin/end — rather than the legacy
`recognizer_loop:audio_output_*` alias. The `emit_legacy` bridge (on by
default) relays the spec emission to legacy-only subscribers, so the same
helper drives ovos-media builds that bind either namespace.

```python
from ovoscope.media import OCPPlayerHarness
from ovos_utils.ocp import MediaEntry, PlaybackType, PlayerState

with OCPPlayerHarness() as h:
    entry = MediaEntry(uri="http://example.com/song.mp3",
                       playback=PlaybackType.AUDIO)
    h.play(entry)
    h.duck()                         # lower_volume called, player stays PLAYING
    h.assert_player_state(PlayerState.PLAYING)
    assert h.player._paused_on_duck  # flag set
    h.unduck()                       # restore_volume called, _paused_on_duck cleared
    h.assert_player_state(PlayerState.PLAYING)
    assert not h.player._paused_on_duck
```

#### Corking: pause the player, resume after listening

Corking happens when the **microphone opens** (wake-word recognised, user
speaking).  The player is fully **paused** and resumes after the interaction.

| Bus message | Handler | Effect |
|---|---|---|
| `recognizer_loop:record_begin` / `ovos.common_play.cork` | `handle_cork_request` | Pauses player, sets `_paused_on_duck=True` |
| `ovos.common_play.uncork` | `handle_uncork_request` | Resumes player **only if PAUSED and `_paused_on_duck`** |
| `recognizer_loop:record_end` | `handle_record_end` | Waits up to 8 s for `speak`. If none, uncork |

```python
from ovoscope.media import OCPPlayerHarness
from ovos_utils.ocp import MediaEntry, PlaybackType, PlayerState

with OCPPlayerHarness() as h:
    entry = MediaEntry(uri="http://example.com/song.mp3",
                       playback=PlaybackType.AUDIO)
    h.play(entry)
    h.cork()                         # player → PAUSED
    h.assert_player_state(PlayerState.PAUSED)
    assert h.player._paused_on_duck

    h.uncork()                       # player → PLAYING
    h.assert_player_state(PlayerState.PLAYING)
    assert not h.player._paused_on_duck
```

#### Uncork-guard: manual pause is not overridden by uncork

``handle_uncork_request`` checks ``_paused_on_duck`` before resuming.  If the
user paused manually, ``_paused_on_duck`` is ``False`` and ``uncork()`` is a
no-op, preventing a spurious resume.

```python
with OCPPlayerHarness() as h:
    h.play(entry)
    h.pause()                 # manual pause: _paused_on_duck stays False
    h.uncork()                # no-op: _paused_on_duck is False
    h.assert_player_state(PlayerState.PAUSED)
```

#### record_end auto-uncork

When the mic closes without any TTS following (utterance not recognised),
``handle_record_end`` uncorks automatically after an 8-second timeout.  Tests
should patch ``bus.wait_for_message`` to avoid the real wait:

```python
from unittest.mock import patch

with OCPPlayerHarness() as h:
    h.play(entry)
    h.cork()
    with patch.object(h.bus, "wait_for_message", return_value=None):
        h.bus.emit(Message("recognizer_loop:record_end"))
        import time
    time.sleep(0.05)
    h.assert_player_state(PlayerState.PLAYING)
```

### Simulating Stream End

```python
from ovoscope.media import OCPPlayerHarness
from ovos_utils.ocp import MediaEntry, MediaState, PlaybackType

with OCPPlayerHarness() as h:
    entry = MediaEntry(uri="http://example.com/song.mp3",
                       playback=PlaybackType.AUDIO)
    h.play(entry)
    h.simulate_track_end()  # backend emits END_OF_MEDIA
    # Player auto-advances or stops depending on queue + autoplay config
```

### Driving a Real OCP Backend

By default `OCPPlayerHarness` injects a `MockOCPBackend` and mocks out
`AudioService`, so it exercises the **player state machine** but never the real
backend routing. To test a **real** OCP audio backend end-to-end: e.g. assert
that playing a uri makes a Music Assistant backend call its server: pass a
`backend_factory`: a `bus -> AudioBackend` callable. The harness then wires a
*real* `AudioService` (no autoload) with your backend as its sole service, so the
player's `play -> load_track -> LOADED_MEDIA -> backend.play()` path actually
reaches it.

```python
from ovoscope.media import OCPPlayerHarness
from ovos_utils.ocp import MediaEntry, PlaybackType

def make_backend(bus):
    backend = MAssOCPAudioService(config={"url": "http://mass.local:8095"}, bus=bus)
    backend.api = mock_client          # mock the network client the backend reaches
    backend.player_state = {"available": True}
    return backend

with OCPPlayerHarness(backend_factory=make_backend) as h:
    h.play(MediaEntry(uri="library://track/42", playback=PlaybackType.AUDIO))
    h.backend.api.play_media.assert_called_once_with(queue_id, "library://track/42")
```

Notes:

- The factory **owns mocking** any network client the real backend would reach.
- The OCP pipeline's stream extractors resolve deferred uris (`library://`, `{sei}//…`)
  *before* the player in production. The harness loads no
  extractor plugins, so it bypasses the player's stream validation when you use a
  backend factory.
- The harness supplies `name`/`namespace` if the backend lacks them
  (normally set by `BaseMediaService.load_services()`, which the harness bypasses).
- The mock-only helpers (`assert_backend_paused`, `backend.played_uris`) assume a
  `MockOCPBackend` and may not apply to a real backend: assert on the backend's
  own state/spies instead.

## OCPCaptureSession

`OCPCaptureSession` (`ovoscope/media.py`)

Captures all `ovos.common_play.*` and `ovos.audio.*` bus messages during a
block of code and lets you assert that specific message types appeared in order.

```python
from ovoscope.media import OCPPlayerHarness, OCPCaptureSession
from ovos_utils.ocp import MediaEntry, PlaybackType

with OCPPlayerHarness() as h:
    entry = MediaEntry(uri="http://example.com/song.mp3",
                       playback=PlaybackType.AUDIO)
    with OCPCaptureSession(h.bus) as session:
        h.play(entry)

    # Check that player.state was announced after play
    session.assert_sequence("ovos.common_play.player.state")
```

### Custom Prefixes

```python
from ovoscope.media import OCPCaptureSession

with OCPPlayerHarness() as h:
    with OCPCaptureSession(h.bus,
                           track_prefixes=["ovos.common_play.player."]) as s:
        h.play(entry)
    print(s.message_types)  # ['ovos.common_play.player.state']
```

## API Reference

### MockOCPBackend

`MockOCPBackend` (`ovoscope/media.py`)

| Attribute / Method | Type | Description |
|---|---|---|
| `played_uris` | `List[str]` | All URIs passed to `add_list()` or `load_track()` |
| `is_playing` | `bool` | True after `play()`, False after `stop()` |
| `is_paused` | `bool` | True after `pause()`, False after `resume()` |
| `current_uri` | `Optional[str]` | Most recently loaded URI |
| `namespace` | `str` | Backend namespace string (default `"audio"`) |
| `stop()` | `bool` | Always returns `True` (required by BaseMediaService) |
| `simulate_end()` | `None` | Emit `END_OF_MEDIA` on the bus |
| `simulate_invalid_stream()` | `None` | Emit `INVALID_MEDIA` on the bus |
| `reset()` | `None` | Clear all recorded state |

### OCPPlayerHarness

`OCPPlayerHarness` (`ovoscope/media.py`)

**Constructor:** `OCPPlayerHarness(backend_namespace="audio", backend_factory=None)`.
`backend_factory` is an optional `bus -> AudioBackend` callable. When given, the
harness drives that real backend through a real `AudioService` (see
[Driving a Real OCP Backend](#driving-a-real-ocp-backend)) instead of the default
`MockOCPBackend`.

**Control methods** (each emits the corresponding bus message + `time.sleep(0.05)`):

| Method | Bus message emitted |
|---|---|
| `play(track: MediaEntry)` | `ovos.common_play.play` |
| `pause()` | `ovos.common_play.pause` |
| `resume()` | `ovos.common_play.resume` |
| `stop()` | `ovos.common_play.stop` |
| `next_track()` | `ovos.common_play.next` |
| `prev_track()` | `ovos.common_play.previous` |
| `duck()` | `ovos.audio.output.started`: lower volume, player stays PLAYING |
| `unduck()` | `ovos.audio.output.ended`: restore volume whenever `_paused_on_duck` is True (duck or cork path) |
| `cork()` | `ovos.common_play.cork`: pause player, set `_paused_on_duck=True` |
| `uncork()` | `ovos.common_play.uncork`: resume player if PAUSED and `_paused_on_duck` |
| `simulate_track_end()` | `ovos.common_play.media.state` END_OF_MEDIA |
| `simulate_invalid_stream()` | `ovos.common_play.media.state` INVALID_MEDIA |

**Assertion helpers:**

| Method | Description |
|---|---|
| `assert_player_state(state)` | Raise if `player.state != state` |
| `assert_media_state(state)` | Raise if `player.media_state != state` |
| `assert_backend_playing()` | Raise if `backend.is_playing` is False |
| `assert_backend_paused()` | Raise if `backend.is_paused` is False |
| `assert_backend_stopped()` | Raise if `is_playing` or `is_paused` is True |
| `assert_now_playing_uri(uri)` | Raise if `now_playing.uri != uri` |

**Exposed attributes:**

| Attribute | Type | Description |
|---|---|---|
| `player` | `OCPMediaPlayer` | Real player instance |
| `bus` | `FakeBus` | Shared in-process bus |
| `backend` | `MockOCPBackend` | Injected mock audio backend |
| `gui` | `MagicMock` | Mocked GUIInterface (unused if the `ovos-media` build has no GUI integration) |

### OCPCaptureSession

`OCPCaptureSession` (`ovoscope/media.py`)

| Method / Property | Description |
|---|---|
| `start()` / `stop()` | Subscribe/unsubscribe from FakeBus |
| `__enter__` / `__exit__` | Context manager interface |
| `messages` | List of captured `Message` objects |
| `message_types` | List of captured `msg_type` strings |
| `assert_sequence(*types)` | Assert types appear in order as a subsequence |

Default `track_prefixes` captures: `"ovos.common_play."`, `"ovos.audio."`.

## Limitations

- **No real audio**: `MockOCPBackend` never plays audio. Use `simulate_end()` to
  trigger end-of-track logic.
- **No MPRIS**: `OcpMprisExporter` is mocked out: MPRIS D-Bus integration is
  not exercised.
- **No GUI rendering**: on `ovos-media` builds that still define
  `GUIInterface`, it is patched with a `MagicMock`; test GUI calls via
  `harness.gui.show_media_player.assert_called_with(...)`. Builds without
  in-core GUI integration have no `GUIInterface` to patch, and `harness.gui`
  is an unused `MagicMock`.
- **No VideoService / WebService**: Only audio playback (`PlaybackType.AUDIO`)
  is wired with a real mock backend.
- **FakeBus is synchronous**: Handlers run in the same thread that calls
  `bus.emit()`. The `time.sleep(0.05)` in control methods is sufficient for
  synchronous delivery. Async or threaded handlers may need explicit waits.

## Cross-References

- `OCPMediaPlayer` (`ovos-media/ovos_media/player.py`)
- `BaseMediaService` (`ovos-media/ovos_media/media_backends/base.py`)
- `AudioBackend` (base class) (`ovos_plugin_manager.templates.audio.AudioBackend`)
- `MediaEntry`, `PlayerState`, `MediaState` (`ovos_utils.ocp`)
- `MockAudioBackend` / `AudioServiceHarness` (audio pattern): `ovoscope/audio.py`
- End-to-end tests: `ovos-media/test/end2end/test_ocp_player.py`

---
[← Audio Testing](audio-testing.md) · [Home](../README.md) · [Media Provider Testing →](media-provider-testing.md)
