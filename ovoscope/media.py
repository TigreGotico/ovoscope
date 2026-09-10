# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""OCP / Media testing harnesses for ovoscope.

Provides a mock OCP audio backend, a context-manager harness that wires a real
``OCPMediaPlayer`` onto a ``FakeBus`` with all heavy dependencies mocked out,
and a lightweight bus-message capture helper — enabling fast, dependency-free
end-to-end player state-machine tests.

Classes:
    MockOCPBackend    -- no-op AudioBackend that tracks state and can simulate
                         media events
    OCPPlayerHarness  -- context manager wrapping OCPMediaPlayer + MockOCPBackend
    OCPCaptureSession -- records OCP bus messages matching given prefixes
"""

import dataclasses
import time
from typing import Callable, List, Optional
from unittest.mock import MagicMock, patch

from ovos_bus_client.message import Message
from ovos_plugin_manager.templates.audio import AudioBackend
from ovos_utils.fakebus import FakeBus
from ovos_utils.ocp import MediaEntry, MediaState, PlayerState


# ---------------------------------------------------------------------------
# MockOCPBackend
# ---------------------------------------------------------------------------

class MockOCPBackend(AudioBackend):
    """A no-op AudioBackend that records state transitions and can emit media
    lifecycle events to simulate a real backend during testing.

    No actual audio is played.  Every mutating method updates simple Python
    attributes so tests can inspect them without mocks.

    Args:
        config: Backend configuration dict (may be empty).
        bus: FakeBus instance shared with the service under test.
        namespace: Backend namespace string, e.g. ``"audio"``.  Used when
            emitting ``ovos.{namespace}.service.media.state`` events.
    """

    def __init__(self, config: dict, bus: FakeBus,
                 namespace: str = "audio") -> None:
        """Initialise the backend with clean state.

        Args:
            config: Configuration dict forwarded to AudioBackend.__init__.
            bus: FakeBus used by the enclosing service.
            namespace: Namespace prefix for bus events.
        """
        super().__init__(config=config, bus=bus)
        self.namespace: str = namespace
        self.played_uris: List[str] = []
        self.is_playing: bool = False
        self.is_paused: bool = False
        self.current_uri: Optional[str] = None

    # ------------------------------------------------------------------
    # AudioBackend abstract interface
    # ------------------------------------------------------------------

    def supported_uris(self) -> List[str]:
        """Return URI schemes supported by this backend.

        Returns:
            List of scheme strings: ``["file", "http", "https"]``.
        """
        return ["file", "http", "https"]

    def add_list(self, playlist: List[str]) -> None:
        """Record tracks and set ``current_uri``.

        Args:
            playlist: List of track URIs to add.
        """
        self.played_uris.extend(playlist)
        if playlist:
            self.current_uri = playlist[0]

    def clear_list(self) -> None:
        """Clear the recorded playlist and current URI."""
        self.played_uris.clear()
        self.current_uri = None

    def load_track(self, uri: str) -> None:
        """Record *uri* and emit a ``LOADED_MEDIA`` state event.

        This triggers ``BaseMediaService.handle_media_state_change`` which
        calls ``self.current.play()`` to start real playback in production.
        In tests the ``play()`` call on this backend simply sets ``is_playing``.

        Args:
            uri: URI of the track to load.
        """
        self.current_uri = uri
        if uri not in self.played_uris:
            self.played_uris.append(uri)
        self.bus.emit(Message(
            f"ovos.{self.namespace}.service.media.state",
            {"state": MediaState.LOADED_MEDIA},
        ))

    def play(self, repeat: bool = False) -> None:
        """Mark the backend as playing.

        Args:
            repeat: Whether to loop (not implemented in mock).
        """
        self.is_playing = True
        self.is_paused = False

    def stop(self) -> bool:
        """Stop playback and clear state.

        Returns:
            True — ``BaseMediaService._perform_stop()`` gates on the return value.
        """
        self.is_playing = False
        self.is_paused = False
        return True

    def pause(self) -> None:
        """Pause playback."""
        self.is_paused = True

    def resume(self) -> None:
        """Resume paused playback."""
        self.is_paused = False

    def next(self) -> None:
        """Skip to next track (no-op)."""

    def previous(self) -> None:
        """Skip to previous track (no-op)."""

    def lower_volume(self) -> None:
        """Duck volume (no-op in mock)."""

    def restore_volume(self) -> None:
        """Restore ducked volume (no-op in mock)."""

    def track_info(self) -> dict:
        """Return minimal track info.

        Returns:
            Dict with ``"track"`` key containing ``current_uri``.
        """
        return {"track": self.current_uri}

    def shutdown(self) -> None:
        """Shut down the backend (no-op)."""

    def get_track_length(self) -> int:
        """Return track duration in ms.

        Returns:
            Always 0 — mock backend has no real audio.
        """
        return 0

    def get_track_position(self) -> int:
        """Return current playback position in ms.

        Returns:
            Always 0 — mock backend has no real audio.
        """
        return 0

    def set_track_position(self, milliseconds: int) -> None:
        """Seek to position (no-op).

        Args:
            milliseconds: Target position in ms.
        """

    def seek_forward(self, seconds: int = 1) -> None:
        """Seek forward (no-op).

        Args:
            seconds: Seconds to seek forward.
        """

    def seek_backward(self, seconds: int = 1) -> None:
        """Seek backward (no-op).

        Args:
            seconds: Seconds to seek backward.
        """

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    def simulate_end(self) -> None:
        """Emit an ``END_OF_MEDIA`` state event on the shared bus.

        Call this in tests to simulate the backend finishing a track without
        real audio hardware.
        """
        self.is_playing = False
        self.bus.emit(Message(
            "ovos.common_play.media.state",
            {"state": MediaState.END_OF_MEDIA},
        ))

    def simulate_invalid_stream(self) -> None:
        """Emit an ``INVALID_MEDIA`` state event on the shared bus.

        Call this in tests to simulate a broken or unplayable stream.
        """
        self.is_playing = False
        self.bus.emit(Message(
            "ovos.common_play.media.state",
            {"state": MediaState.INVALID_MEDIA},
        ))

    def reset(self) -> None:
        """Reset all recorded state back to initial values."""
        self.played_uris.clear()
        self.is_playing = False
        self.is_paused = False
        self.current_uri = None


# ---------------------------------------------------------------------------
# OCPPlayerHarness
# ---------------------------------------------------------------------------

class OCPPlayerHarness:
    """Context manager that runs ``OCPMediaPlayer`` on a ``FakeBus`` with all
    heavy dependencies mocked out and a ``MockOCPBackend`` injected as the
    sole audio backend.

    Usage::

        with OCPPlayerHarness() as h:
            entry = MediaEntry(uri="http://example.com/song.mp3",
                               playback=PlaybackType.AUDIO)
            h.play(entry)
            h.assert_player_state(PlayerState.PLAYING)
            assert h.backend.is_playing

    The harness patches:

    - ``ovos_media.player.AudioService``
    - ``ovos_media.player.VideoService``
    - ``ovos_media.player.WebService``
    - ``ovos_media.player.OcpMprisExporter``
    - ``ovos_media.player.OCPMediaCatalog``
    - ``ovos_media.player.Configuration``  (returns ``{"media": {}}``)

    Args:
        backend_namespace: Namespace for ``MockOCPBackend``; default ``"audio"``.
    """

    def __init__(self, backend_namespace: str = "audio",
                 backend_factory: Optional[Callable[[FakeBus], AudioBackend]] = None,
                 modernize: bool = True,
                 emit_legacy: bool = True) -> None:
        """Initialise harness parameters.

        Args:
            backend_namespace: Namespace prefix passed to ``MockOCPBackend``.
            backend_factory: Optional ``bus -> AudioBackend`` callable used to build
                the injected backend instead of the default :class:`MockOCPBackend`.
                The harness owns the ``FakeBus``, so a *factory* (not a pre-built
                instance) is taken: it is called with the harness bus inside
                ``__enter__``. Use it to drive a **real** OCP backend (e.g. a
                Music Assistant audio backend) through the real ``OCPMediaPlayer``;
                the factory is responsible for mocking any network client the real
                backend would otherwise reach. Note the mock-only assertion helpers
                (:meth:`assert_backend_paused`, ``backend.played_uris``) assume a
                :class:`MockOCPBackend` and may not apply to a real backend.
            modernize: FakeBus also emits the ovos.* spec topic when a legacy
                topic is emitted (legacy producer -> spec listener). OCPMediaPlayer
                subscribes to the LEGACY duck/cork topics
                (recognizer_loop:audio_output_start/end, record_begin/end); this
                bridge lets a spec-namespace producer's ovos.audio.output.* /
                ovos.listener.record.* reach those legacy handlers.
            emit_legacy: FakeBus also emits the legacy topic when an ovos.* spec
                topic is emitted (spec producer -> legacy listener). This is the
                bridge that connects a spec producer to a player build that
                binds only the legacy topics — including
                :meth:`OCPPlayerHarness.duck`/:meth:`~OCPPlayerHarness.unduck`,
                which emit ``ovos.audio.output.started/ended`` (matching what
                the real ``ovos-audio`` service emits) rather than the legacy
                aliases. Set both False to exercise a single namespace with no
                bridging.
        """
        self.backend_namespace: str = backend_namespace
        self.backend_factory = backend_factory
        self.modernize: bool = modernize
        self.emit_legacy: bool = emit_legacy
        self.bus: Optional[FakeBus] = None
        self.player = None  # OCPMediaPlayer instance
        self.backend: Optional[AudioBackend] = None
        self.gui: Optional[MagicMock] = None
        self._patches: list = []

    def __enter__(self) -> "OCPPlayerHarness":
        """Start ``OCPMediaPlayer`` with mocked deps and inject ``MockOCPBackend``.

        Returns:
            self
        """
        from ovos_media.player import OCPMediaPlayer

        # Re-entering a harness instance must not inherit the previous run's
        # patch list; __exit__ clears it, but a caller that reuses the object
        # after a failed enter would otherwise stop the same patches twice.
        self._patches = []
        self.bus = FakeBus(modernize=self.modernize,
                           emit_legacy=self.emit_legacy)
        if self.backend_factory is not None:
            self.backend = self.backend_factory(self.bus)
            # A config-loaded backend gets name/namespace from
            # BaseMediaService.load_services(), which the harness bypasses — supply
            # sane defaults so the service's bookkeeping (shutdown, routing) works.
            if not getattr(self.backend, "name", None):
                self.backend.name = "test-backend"
            if not getattr(self.backend, "namespace", None):
                self.backend.namespace = self.backend_namespace
        else:
            self.backend = MockOCPBackend(
                config={}, bus=self.bus, namespace=self.backend_namespace
            )

        # Build patch targets
        gui_mock = MagicMock()
        self.gui = gui_mock

        # Every mock.patch below is process-wide until stopped. If anything
        # after the first start() raises (a missing ovos_media attribute, a
        # backend constructor error), an unguarded exit would leave those
        # patches active for the rest of the process and silently corrupt
        # every later test. Unwind through __exit__ before propagating.
        try:
            simple_targets = [
                "ovos_media.player.AudioService",
                "ovos_media.player.VideoService",
                "ovos_media.player.WebService",
                "ovos_media.player.OcpMprisExporter",
                "ovos_media.player.OCPMediaCatalog",
            ]
            for target in simple_targets:
                p = patch(target)
                p.start()
                self._patches.append(p)

            p_cfg = patch("ovos_media.player.Configuration",
                          return_value={"media": {}})
            p_cfg.start()
            self._patches.append(p_cfg)

            # Instantiate the real player (all heavy deps are now mocked)
            self.player = OCPMediaPlayer(self.bus, config={})

            ns = self.backend_namespace
            if self.backend_factory is not None:
                # Real-backend mode: the mocked AudioService (a MagicMock) never routes
                # play()->load_track()->backend.play(), so swap in a *real* AudioService
                # with autoload off and the injected backend as its sole service. Now the
                # player's playback path actually drives the real backend (e.g. asserting
                # a Music Assistant client's play_media() call).
                from ovos_media.media_backends.audio import AudioService as _RealAudioService
                audio_svc = _RealAudioService(self.bus, config={"audio_players": {}},
                                              autoload=False)
                self.player.audio_service = audio_svc
                # Deferred uris (e.g. library://, {sei}//) are resolved by the OCP
                # pipeline's stream extractors *before* the player sees them; this
                # harness drives the backend directly, so bypass the player's
                # stream-extraction validation (no extractor plugins are loaded).
                self.player.validate_stream = lambda: True
                audio_svc.services = [self.backend]
                audio_svc.default = self.backend
                self.backend.set_track_start_callback(audio_svc.track_start)
                # load_services() (skipped with autoload=False) would register these.
                # handle_play routed the per-namespace ovos.{ns}.service.play bus
                # topic on builds that had it; newer ovos-media dropped that whole
                # per-namespace bus surface (pause/resume/stop survive as plain
                # methods) — playback goes exclusively through OCPMediaPlayer.play()
                # calling audio_service.play() directly, so there is nothing to
                # register in its absence.
                if hasattr(audio_svc, "handle_play"):
                    self.bus.on(f"ovos.{ns}.service.play", audio_svc.handle_play)
                self.bus.on(f"ovos.{ns}.service.pause", audio_svc.pause)
                self.bus.on(f"ovos.{ns}.service.resume", audio_svc.resume)
                self.bus.on(f"ovos.{ns}.service.stop", audio_svc.stop)
                # NB: BaseMediaService.__init__ already wired ovos.common_play.media.state
                # -> handle_media_state_change; re-registering it would fire backend.play()
                # twice, so it is deliberately omitted here.
                audio_svc._loaded.set()
            else:
                # Mock-backend mode: drive the player state-machine against the MagicMock
                # AudioService; the backend is exposed for manual simulate_*/state asserts.
                audio_svc = self.player.audio_service
                audio_svc.services = [self.backend]
                audio_svc.default = self.backend
                self.backend.set_track_start_callback(audio_svc.track_start)
                # Register the audio service bus handlers manually
                # (normally done inside BaseMediaService.load_services)
                if hasattr(audio_svc, "handle_play"):
                    self.bus.on(f"ovos.{ns}.service.play", audio_svc.handle_play)
                self.bus.on(f"ovos.{ns}.service.pause", audio_svc.pause)
                self.bus.on(f"ovos.{ns}.service.resume", audio_svc.resume)
                self.bus.on(f"ovos.{ns}.service.stop", audio_svc.stop)
                self.bus.on("ovos.common_play.media.state",
                            audio_svc.handle_media_state_change)
                audio_svc._loaded.set()

            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args) -> None:
        """Shut down the player, close the bus, and stop all patches."""
        if self.player:
            try:
                self.player.shutdown()
            except Exception:
                pass
        if self.bus:
            try:
                self.bus.close()
            except Exception:
                pass
        for p in reversed(self._patches):
            try:
                p.stop()
            except RuntimeError:
                pass
        self._patches = []

    # ------------------------------------------------------------------
    # Control methods — emit the correct bus message and yield briefly
    # ------------------------------------------------------------------

    def play(self, track: MediaEntry) -> None:
        """Emit ``ovos.common_play.play`` and wait for synchronous delivery.

        Args:
            track: ``MediaEntry`` to play.
        """
        self.bus.emit(Message("ovos.common_play.play", {
            "media": track.as_dict,
            "playlist": [track.as_dict],
        }))
        time.sleep(0.05)

    def pause(self) -> None:
        """Emit ``ovos.common_play.pause``."""
        self.bus.emit(Message("ovos.common_play.pause"))
        time.sleep(0.05)

    def resume(self) -> None:
        """Emit ``ovos.common_play.resume``."""
        self.bus.emit(Message("ovos.common_play.resume"))
        time.sleep(0.05)

    def stop(self) -> None:
        """Emit ``ovos.common_play.stop``."""
        self.bus.emit(Message("ovos.common_play.stop"))
        time.sleep(0.05)

    def next_track(self) -> None:
        """Emit ``ovos.common_play.next``."""
        self.bus.emit(Message("ovos.common_play.next"))
        time.sleep(0.05)

    def prev_track(self) -> None:
        """Emit ``ovos.common_play.previous``."""
        self.bus.emit(Message("ovos.common_play.previous"))
        time.sleep(0.05)

    def duck(self) -> None:
        """Lower the audio backend volume via ``ovos.audio.output.started``.

        Simulates the start of TTS playback the way the real ``ovos-audio``
        service does it: ``ovos_audio/playback.py`` emits the spec topic
        ``ovos.audio.output.started`` unconditionally on every speech begin
        (via ``SpecMessage``). The legacy ``recognizer_loop:audio_output_start``
        alias is not guaranteed to be emitted, so the harness targets the spec
        topic directly; the ``emit_legacy``/``modernize`` bridge on
        :class:`~ovos_utils.fakebus.FakeBus` still connects it to a
        legacy-only listener when needed.

        Ducking lowers volume while the voice assistant speaks.  The player
        **stays PLAYING** — only the backend volume is reduced.

        Equivalent OCP message: ``ovos.common_play.duck``.
        Handler: ``OCPMediaPlayer.handle_duck_request`` —
        ``ovos_media/player.py:1216``.
        """
        self.bus.emit(Message("ovos.audio.output.started"))
        time.sleep(0.05)

    def unduck(self) -> None:
        """Restore the audio backend volume via ``ovos.audio.output.ended``.

        Simulates the end of TTS playback the way the real ``ovos-audio``
        service does it: ``ovos_audio/playback.py`` emits the spec topic
        ``ovos.audio.output.ended`` unconditionally on every speech end (via
        ``SpecMessage``). See :meth:`duck` for why the harness targets the
        spec topic rather than the legacy ``recognizer_loop:audio_output_end``
        alias.

        Note: ``handle_unduck_request`` only restores volume when the player is
        PAUSED (``state == PlayerState.PAUSED``).  After a pure duck cycle the
        player remains PLAYING, so this call is a no-op in that case.

        Equivalent OCP message: ``ovos.common_play.unduck``.
        Handler: ``OCPMediaPlayer.handle_unduck_request`` —
        ``ovos_media/player.py:1228``.
        """
        self.bus.emit(Message("ovos.audio.output.ended"))
        time.sleep(0.05)

    def cork(self) -> None:
        """Pause the player via ``ovos.common_play.cork`` (microphone opens).

        Corking fully **pauses** the player and sets ``_paused_on_duck = True``
        so ``uncork()`` / ``record_end`` can resume it automatically.

        Equivalent legacy message: ``recognizer_loop:record_begin``.
        Handler: ``OCPMediaPlayer.handle_cork_request`` —
        ``ovos_media/player.py:1198``.
        """
        self.bus.emit(Message("ovos.common_play.cork"))
        time.sleep(0.05)

    def uncork(self) -> None:
        """Resume the player via ``ovos.common_play.uncork`` (microphone closes).

        Only resumes if the player is PAUSED **and** ``_paused_on_duck`` is True
        (i.e. the pause was caused by a cork, not a manual pause).

        Equivalent legacy message: ``recognizer_loop:record_end`` followed by
        8-second no-speak timeout.
        Handler: ``OCPMediaPlayer.handle_uncork_request`` —
        ``ovos_media/player.py:1207``.
        """
        self.bus.emit(Message("ovos.common_play.uncork"))
        time.sleep(0.05)

    def simulate_track_end(self) -> None:
        """Emit ``ovos.common_play.media.state`` ``END_OF_MEDIA`` via the backend.

        Triggers ``OCPMediaPlayer.handle_player_media_update`` →
        ``handle_playback_ended``, which auto-advances the queue when
        ``autoplay`` is enabled.
        """
        self.backend.simulate_end()
        time.sleep(0.05)

    def simulate_invalid_stream(self) -> None:
        """Emit ``ovos.common_play.media.state`` ``INVALID_MEDIA`` via the backend.

        Triggers ``OCPMediaPlayer.handle_player_media_update`` →
        ``handle_invalid_media``, then ``play_next()`` when ``autoplay`` is
        enabled.
        """
        self.backend.simulate_invalid_stream()
        time.sleep(0.05)

    # ------------------------------------------------------------------
    # Assertion helpers
    # ------------------------------------------------------------------

    def assert_player_state(self, state: PlayerState) -> None:
        """Assert the player is in the given ``PlayerState``.

        Args:
            state: Expected ``PlayerState``.
        """
        assert self.player.state == state, (
            f"Expected PlayerState.{state.name}, "
            f"got PlayerState.{self.player.state.name}"
        )

    def assert_media_state(self, state: MediaState) -> None:
        """Assert the player's media state matches *state*.

        Args:
            state: Expected ``MediaState``.
        """
        assert self.player.media_state == state, (
            f"Expected MediaState.{state.name}, "
            f"got MediaState.{self.player.media_state.name}"
        )

    def assert_backend_playing(self) -> None:
        """Assert the mock backend is currently playing."""
        assert self.backend.is_playing, "Expected backend to be playing"

    def assert_backend_paused(self) -> None:
        """Assert the mock backend is currently paused."""
        assert self.backend.is_paused, "Expected backend to be paused"

    def assert_backend_stopped(self) -> None:
        """Assert the mock backend is neither playing nor paused."""
        assert not self.backend.is_playing, \
            "Expected backend to be stopped (is_playing=True)"
        assert not self.backend.is_paused, \
            "Expected backend to be stopped (is_paused=True)"

    def assert_now_playing_uri(self, uri: str) -> None:
        """Assert the currently playing URI matches *uri*.

        Args:
            uri: Expected URI string.
        """
        actual = self.player.now_playing.uri if self.player.now_playing else None
        assert actual == uri, f"Expected now_playing.uri={uri!r}, got {actual!r}"


# ---------------------------------------------------------------------------
# OCPCaptureSession
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class OCPCaptureSession:
    """Records bus messages whose types match given prefixes.

    Designed as a lightweight companion to ``OCPPlayerHarness`` for asserting
    that specific OCP message sequences were emitted during a media interaction.

    Args:
        bus: The ``FakeBus`` to subscribe to.
        track_prefixes: Message-type prefix strings to capture.

    Attributes:
        messages: All captured ``Message`` objects in emission order.

    Example::

        with OCPPlayerHarness() as h:
            with OCPCaptureSession(h.bus) as session:
                h.play(entry)
            session.assert_sequence(
                "ovos.common_play.play",
                "ovos.common_play.player.state",
            )
    """

    bus: FakeBus
    # capture BOTH the legacy and the ovos.* spec topics of the duck/cork
    # messages the player consumes. The session observes the raw "message" wire
    # stream, which carries the producer's ORIGINAL topic only (FakeBus' namespace
    # bridging re-dispatches the counterpart as a typed event, not a second
    # "message" event). Listing both namespaces lets the session record the
    # sequence whether the producer emits legacy or spec.
    track_prefixes: List[str] = dataclasses.field(default_factory=lambda: [
        "ovos.common_play.",
        "ovos.audio.",
        "recognizer_loop:audio_output",
        "ovos.listener.record",
        "recognizer_loop:record",
    ])
    messages: List[Message] = dataclasses.field(default_factory=list)

    def _handle(self, msg: str) -> None:
        """Internal handler subscribed to the raw ``message`` event.

        Args:
            msg: Serialised message string from ``FakeBus``.
        """
        m = Message.deserialize(msg)
        for prefix in self.track_prefixes:
            if m.msg_type.startswith(prefix):
                self.messages.append(m)
                break

    def start(self) -> None:
        """Begin capturing messages."""
        self.messages.clear()
        self.bus.on("message", self._handle)

    def stop(self) -> None:
        """Stop capturing messages."""
        self.bus.remove("message", self._handle)

    def __enter__(self) -> "OCPCaptureSession":
        """Start capturing on context entry.

        Returns:
            self
        """
        self.start()
        return self

    def __exit__(self, *args) -> None:
        """Stop capturing on context exit."""
        self.stop()

    @property
    def message_types(self) -> List[str]:
        """Return the list of captured message type strings.

        Returns:
            List of ``msg_type`` strings in the order they were received.
        """
        return [m.msg_type for m in self.messages]

    def assert_sequence(self, *types: str) -> None:
        """Assert that captured messages contain all given types in order.

        Args:
            *types: Expected message type strings (subsequence check).

        Raises:
            AssertionError: If any type is missing or order is violated.
        """
        received = self.message_types
        pos = 0
        for t in types:
            found = False
            while pos < len(received):
                if received[pos] == t:
                    pos += 1
                    found = True
                    break
                pos += 1
            assert found, (
                f"Expected message '{t}' not found in sequence after position. "
                f"Full captured sequence: {received}"
            )
