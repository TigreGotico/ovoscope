# Media / OCP Testing with ovoscope

This document describes how to test `ovos-media`: the `OCPMediaPlayer` state
machine and the `MediaBackend` plugin contract it drives, using the harness
classes in `ovoscope.media` and `ovoscope.media_backend`.

> **Prerequisite:** the player-level harnesses require `ovos-media` to be
> installed (`pip install ovoscope ovos-media`). `MediaBackendHarness` depends
> only on `ovos-plugin-manager`, a base ovoscope dependency, and works without
> `ovos-media` installed at all.

## The MediaBackend v2 Contract

A `MediaBackend` plugin (`ovos_plugin_manager.templates.media`) is
single-track: playlists, queueing and auto-advance are OCP's job, not the
plugin's. A plugin implements `supported_uris`, `load_track`, `play`, `_stop`,
`pause`, `resume`, and the position/length/seek trio, and reports what
actually happens inside the player process — never a bus message — through
`report()`:

- `load_track(uri, metadata=None) -> bool` signals load success/failure by
  its return value alone. It reports nothing; the daemon owns the
  `LOADED_MEDIA`/`INVALID_MEDIA` transition on that value.
- `stop()` is a concrete template method: it records that a stop was
  explicitly requested, then calls the plugin's own `_stop()`. Plugins
  implement `_stop`, never `stop`.
- `report(event, **data)` sends one `PlaybackEvent` — `TRACK_START`,
  `PAUSED`, `RESUMED`, `STOPPED`, `END_OF_MEDIA`, or `ERROR` — to whatever
  reporter `bind_event_reporter()` last registered. It no-ops (does not
  raise) when nothing is bound, so a plugin is always safe to construct and
  drive standalone.
- `report_track_end(uri=None, error=None)` is the way to report a track
  ending from a callback that only knows "playback stopped" — a subprocess
  exit handler, an MPRIS `Stopped` transition — and cannot itself tell a
  requested stop from a natural end. It reports `ERROR` when `error` is
  given, `STOPPED` when `stop()` was called since the last report, and
  `END_OF_MEDIA` otherwise, then always clears the pending-stop flag.

The daemon (`ovos-media`'s `BaseMediaService`) is the only thing that turns
these physical events into `ovos.common_play.*` wire messages and drives the
player state machine. A backend that emits one of those messages itself is
buggy by definition.

## When to Use Which Class

| Scenario | Class |
|---|---|
| Testing a `MediaBackend` v2 plugin in isolation, no daemon involved | `MediaBackendHarness` |
| Testing `OCPMediaPlayer` play/pause/stop/next/prev state machine | `OCPPlayerHarness` |
| Testing duck/unduck (volume lowering) and cork/uncork (pause on listen) | `OCPPlayerHarness` |
| Capturing and asserting OCP bus message sequences | `OCPCaptureSession` |
| Simulating a broken or unplayable stream | `MockOCPBackendV2.simulate_invalid_stream()` |
| Simulating end-of-track to trigger auto-advance | `MockOCPBackendV2.simulate_end()` |

## MediaBackendHarness

`MediaBackendHarness` (`ovoscope/media_backend.py`) binds a capturing spy
reporter onto a single `MediaBackend` v2 plugin instance and drives it, so a
test can assert exactly which `PlaybackEvent`s the plugin reported and with
what data — without a running daemon in between. This is the harness a
plugin author reaches for to test their own backend class; use
`OCPPlayerHarness` to test how a backend behaves once wired into the full
player.

```python
from ovoscope.media_backend import MediaBackendHarness
from ovos_plugin_manager.templates.media import PlaybackEvent
from ovos_utils.fakebus import FakeBus

backend = MyVlcBackend(config={}, bus=FakeBus())
with MediaBackendHarness(backend) as h:
    assert h.load_track("file:///tmp/song.mp3") is True
    h.play()
    h.assert_events(PlaybackEvent.TRACK_START)
```

### Happy path

```python
with MediaBackendHarness(backend) as h:
    h.assert_load_track_succeeds("file:///tmp/song.mp3")
    h.play()
    h.assert_events(PlaybackEvent.TRACK_START)
    h.assert_event_data(PlaybackEvent.TRACK_START, uri="file:///tmp/song.mp3")
```

### Pause / resume

```python
with MediaBackendHarness(backend) as h:
    h.load_track("file:///tmp/song.mp3")
    h.play()
    h.pause()
    h.resume()
    h.assert_events(PlaybackEvent.TRACK_START, PlaybackEvent.PAUSED, PlaybackEvent.RESUMED)
```

A remote/side-channel backend (Cast, MPRIS, a network player) SHOULD report
`PAUSED`/`RESUMED` from its own status listener or poller rather than from
`pause()`/`resume()` themselves — those verbs are pure asks that don't
guarantee the remote player actually paused. Drive that shape by calling the
backend's poll/callback method directly and asserting the resulting event,
the same way `simulate_end()` below drives an exit callback rather than
calling `play()`/`_stop()` again.

### Stop vs. a natural end

```python
with MediaBackendHarness(backend) as h:
    h.load_track("file:///tmp/song.mp3")
    h.play()

    # a track that just finishes on its own
    backend.report_track_end(uri="file:///tmp/song.mp3")
    h.assert_events(PlaybackEvent.TRACK_START, PlaybackEvent.END_OF_MEDIA)
```

```python
with MediaBackendHarness(backend) as h:
    h.load_track("file:///tmp/song.mp3")
    h.play()
    h.stop()

    # the SAME exit callback, now firing because of the explicit stop()
    backend.report_track_end(uri="file:///tmp/song.mp3")
    h.assert_events(PlaybackEvent.TRACK_START, PlaybackEvent.STOPPED)
```

Both cases call the identical `report_track_end` — the flag `stop()` set is
what tells them apart, and it clears itself afterward.

### Error

```python
with MediaBackendHarness(backend) as h:
    h.load_track("file:///tmp/song.mp3")
    h.play()
    backend.report_track_end(uri="file:///tmp/song.mp3", error="disk full")
    h.assert_events(PlaybackEvent.TRACK_START, PlaybackEvent.ERROR)
    h.assert_event_data(PlaybackEvent.ERROR, error="disk full")
```

`error` always wins over a pending stop request: a plugin must never report a
failure as a clean `STOPPED`.

### Uri provenance

`END_OF_MEDIA` and `ERROR` SHOULD carry the `uri` of the track they belong
to, so the daemon can detect a late callback from a superseded track (a
watcher thread from track 1 firing after track 2 has already loaded) and drop
it as stale. Assert it with `assert_event_data`:

```python
h.assert_event_data(PlaybackEvent.END_OF_MEDIA, uri="file:///tmp/song.mp3")
```

### Capability trio

`can_seek`, `can_pause` and `is_remote` are plain class attributes
(`can_seek=False`, `can_pause=True`, `is_remote=False` by default) — assert
them directly on the backend instance, no harness call needed:

```python
assert backend.can_seek is False
assert backend.can_pause is True
assert backend.is_remote is False
```

A backend with `can_seek=False` inherits the concrete `-1`/no-op defaults for
`get_track_length`/`get_track_position`/`set_track_position`; a seekable
backend overrides all three and sets `can_seek=True`.

## OCPPlayerHarness

`OCPPlayerHarness` (`ovoscope/media.py`)

Wraps a real `OCPMediaPlayer` (`ovos_media.player`) with a mock audio backend
on a `FakeBus`. All heavy dependencies are patched out:

- `ovos_media.player.AudioService`: mocked. `MockOCPBackendV2` (or the
  `backend_factory` result — see below) is injected as the sole audio backend
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
    h.simulate_track_end()  # backend reports END_OF_MEDIA via report_track_end
    # Player auto-advances or stops depending on queue + autoplay config
```

### Driving a Real OCP Backend

By default `OCPPlayerHarness` injects a `MockOCPBackendV2` and mocks out
`AudioService`, so it exercises the **player state machine** but never the
real backend routing. To test a **real** OCP audio backend end-to-end — e.g.
assert that playing a uri makes a Music Assistant backend call its server —
pass a `backend_factory`: a `bus -> MediaBackend` callable. The harness then
wires a *real* `AudioService` (no autoload) with your backend as its sole
service, and binds its event reporter to that service's
`_handle_backend_event`, so the player's
`play -> load_track -> LOADED_MEDIA -> backend.play() -> report(TRACK_START)`
path actually reaches it, both ways.

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
  `MockOCPBackendV2`/`MockOCPBackend` and may not apply to a real backend: assert
  on the backend's own state/spies instead.
- Default (no `backend_factory`) mode has no real `AudioService` behind it —
  `audio_service` there is a `MagicMock`, so a v2 backend's event reporter is
  bound to a small shim (`OCPPlayerHarness._mock_mode_event_reporter`) that
  reproduces the same `TRACK_START`/`END_OF_MEDIA`/`ERROR` wire translation
  the real daemon performs, so `simulate_track_end()`/`simulate_invalid_stream()`
  stay meaningful without a `backend_factory`. `PAUSED`/`RESUMED`/`STOPPED` are
  not translated in default mode (no `on_external_event` callback exists
  without a real service) — use a `backend_factory` if a test needs those.

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

### MediaBackendHarness

`MediaBackendHarness` (`ovoscope/media_backend.py`)

**Constructor:** `MediaBackendHarness(backend)` — `backend` is an
already-constructed `MediaBackend` v2 plugin instance.

| Method | Description |
|---|---|
| `load_track(uri, metadata=None)` | Calls `backend.load_track`, returns its bool |
| `play()` / `pause()` / `resume()` | Call the matching backend verb |
| `stop()` | Calls the concrete `backend.stop()` template method |
| `assert_load_track_succeeds(uri, metadata=None)` | Raise unless `load_track` returns True |
| `assert_load_track_fails(uri, metadata=None)` | Raise unless `load_track` returns False |
| `assert_events(*expected)` | Raise unless the captured events contain `expected`, in order, as a subsequence |
| `assert_event_data(event, **kwargs)` | Raise unless the first captured `event` carried `kwargs` |
| `event_types` | List of captured `PlaybackEvent` values, in report order |
| `events` | List of `(PlaybackEvent, data_dict)` tuples, in report order |

### MockOCPBackendV2

`MockOCPBackendV2` (`ovoscope/media.py`)

The v2 (`ovos_plugin_manager.templates.media.AudioPlayerBackend`) counterpart
to `MockOCPBackend` below — this is what `OCPPlayerHarness` injects by
default.

| Attribute / Method | Type | Description |
|---|---|---|
| `played_uris` | `List[str]` | All URIs successfully passed to `load_track()` |
| `is_playing` | `bool` | True after `play()`, False after `_stop()`/`simulate_end()`/`simulate_invalid_stream()` |
| `is_paused` | `bool` | True after `pause()`, False after `resume()` |
| `current_uri` | `Optional[str]` | Most recently loaded URI |
| `load_track(uri, metadata=None)` | `bool` | Returns `False` for any uri containing `"invalid"`, else `True` |
| `simulate_end()` | `None` | `report_track_end()` for the current track: `END_OF_MEDIA` |
| `simulate_invalid_stream()` | `None` | `report_track_end(error=...)` for the current track: `ERROR` |
| `reset()` | `None` | Clear all recorded state |

### OCPPlayerHarness

`OCPPlayerHarness` (`ovoscope/media.py`)

**Constructor:** `OCPPlayerHarness(backend_namespace="audio", backend_factory=None)`.
`backend_factory` is an optional `bus -> MediaBackend` callable. When given, the
harness drives that real backend through a real `AudioService` (see
[Driving a Real OCP Backend](#driving-a-real-ocp-backend)) instead of the default
`MockOCPBackendV2`.

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
| `simulate_track_end()` | backend `report_track_end()` → `ovos.common_play.media.state` END_OF_MEDIA |
| `simulate_invalid_stream()` | backend `report_track_end(error=...)` → `ovos.common_play.media.state` INVALID_MEDIA |

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
| `backend` | `MockOCPBackendV2` | Injected mock audio backend (or the `backend_factory` result) |
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

## v1 (MediaBackend) — Unported Plugins

`MockOCPBackend` (`ovoscope/media.py`) is the v1 counterpart to
`MockOCPBackendV2`, built on `ovos_plugin_manager.templates.audio.AudioBackend`.
It stays importable and usable for plugin repos that have not yet ported to
the v2 contract: `load_track` returns nothing and emits a
`ovos.{namespace}.service.media.state` bus message itself instead of
returning a bool; `stop` is the concrete method a plugin overrides directly
(no `_stop` split); `simulate_end()`/`simulate_invalid_stream()` emit
`ovos.common_play.media.state` on the bus directly rather than going through
`report_track_end`.

`OCPPlayerHarness`'s default (no `backend_factory`) mock mode is
unconditionally v2: it always injects `MockOCPBackendV2` when the v2
template is installed, falling back to `MockOCPBackend` only when it is not
(the old-OPM fallback covered above). There is no API to hand the default
mode a v1 backend of your own — it always builds its own mock. To exercise a
*specific* v1 plugin class, use `backend_factory` instead (below), or drive
the plugin directly against `MockOCPBackend`'s own methods, unmediated by
`OCPPlayerHarness` at all (see `TestMockOCPBackendStateTransitions` in
ovoscope's own test suite for the pattern).

Passing `backend_factory=lambda bus: MyV1Backend(...)` to drive a v1 plugin
through the **real** `AudioService` (see
[Driving a Real OCP Backend](#driving-a-real-ocp-backend)) works, with a
caveat: the harness starts up without crashing (the `hasattr` guards prevent
the `AttributeError` a v1 backend used to hit against a v2 build), but a
real v2 `AudioService._play()` treats a v1 `load_track`'s `None` return as a
failed load by design — it logs "likely a MediaBackend v1 plugin ... upgrade
the plugin" and reports `INVALID_MEDIA`. A v1 backend genuinely does not
play through a v2-ported daemon; against a released (still v1-shaped)
`ovos-media`, the same `backend_factory` path plays it normally, the same
way it always has.

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

## Limitations

- **No real audio**: neither mock backend ever plays audio. Use
  `simulate_end()` to trigger end-of-track logic.
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

- `OCPMediaPlayer` (`ovos-media/ovos_media/player/__init__.py`)
- `BaseMediaService` (`ovos-media/ovos_media/media_backends/base.py`)
- `MediaBackend` v2 (base class) (`ovos_plugin_manager.templates.media.MediaBackend`)
- `AudioBackend` v1 (base class) (`ovos_plugin_manager.templates.audio.AudioBackend`)
- `MediaEntry`, `PlayerState`, `MediaState` (`ovos_utils.ocp`)
- `MockAudioBackend` / `AudioServiceHarness` (audio pattern): `ovoscope/audio.py`
- End-to-end tests: `ovos-media/test/end2end/test_ocp_player.py`

---
[← Audio Testing](audio-testing.md) · [Home](../README.md) · [Media Provider Testing →](media-provider-testing.md)
