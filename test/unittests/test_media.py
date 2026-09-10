# Copyright 2024 Jarbas AI
#
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
"""Unit tests for ovoscope.media (MockOCPBackend, OCPCaptureSession, OCPPlayerHarness)."""

import time

import pytest
from unittest.mock import MagicMock

from ovos_bus_client.message import Message
from ovos_utils.fakebus import FakeBus
from ovos_utils.ocp import MediaState

from ovoscope.media import MockOCPBackend, OCPCaptureSession, OCPPlayerHarness


def test_media_harness_reexported_from_package() -> None:
    """The ovos-media harness is reachable from the top-level package, like the
    ovos-audio harness. (media.py imports ovos-media lazily, so the export does
    not require the [media] extra to be installed.)"""
    import ovoscope
    assert ovoscope.OCPPlayerHarness is OCPPlayerHarness
    assert ovoscope.OCPCaptureSession is OCPCaptureSession
    assert ovoscope.MockOCPBackend is MockOCPBackend


# ---------------------------------------------------------------------------
# MockOCPBackend tests
# ---------------------------------------------------------------------------

class TestMockOCPBackendInit:
    """Constructor and initial state."""

    def test_initial_state(self) -> None:
        """Backend starts with clean state."""
        bus = FakeBus()
        backend = MockOCPBackend(config={}, bus=bus)
        assert backend.is_playing is False
        assert backend.is_paused is False
        assert backend.current_uri is None
        assert backend.played_uris == []

    def test_namespace_default(self) -> None:
        """Default namespace is 'audio'."""
        bus = FakeBus()
        backend = MockOCPBackend(config={}, bus=bus)
        assert backend.namespace == "audio"

    def test_namespace_custom(self) -> None:
        """Custom namespace is stored."""
        bus = FakeBus()
        backend = MockOCPBackend(config={}, bus=bus, namespace="video")
        assert backend.namespace == "video"


class TestMockOCPBackendStateTransitions:
    """State mutation methods."""

    def setup_method(self) -> None:
        self.bus = FakeBus()
        self.backend = MockOCPBackend(config={}, bus=self.bus)

    def test_play_sets_playing(self) -> None:
        self.backend.play()
        assert self.backend.is_playing is True
        assert self.backend.is_paused is False

    def test_pause_sets_paused(self) -> None:
        self.backend.play()
        self.backend.pause()
        assert self.backend.is_paused is True

    def test_resume_clears_paused(self) -> None:
        self.backend.play()
        self.backend.pause()
        self.backend.resume()
        assert self.backend.is_paused is False

    def test_stop_clears_state(self) -> None:
        self.backend.play()
        result = self.backend.stop()
        assert self.backend.is_playing is False
        assert self.backend.is_paused is False
        assert result is True

    def test_load_track_sets_uri(self) -> None:
        self.backend.load_track("http://example.com/song.mp3")
        assert self.backend.current_uri == "http://example.com/song.mp3"
        assert "http://example.com/song.mp3" in self.backend.played_uris

    def test_load_track_emits_state_event(self) -> None:
        received: list = []
        self.bus.on(f"ovos.audio.service.media.state", lambda m: received.append(m))
        self.backend.load_track("http://example.com/song.mp3")
        assert len(received) == 1

    def test_add_list_records_uris(self) -> None:
        self.backend.add_list(["track1.mp3", "track2.mp3"])
        assert "track1.mp3" in self.backend.played_uris
        assert self.backend.current_uri == "track1.mp3"

    def test_clear_list(self) -> None:
        self.backend.add_list(["track1.mp3"])
        self.backend.clear_list()
        assert self.backend.played_uris == []
        assert self.backend.current_uri is None

    def test_reset(self) -> None:
        self.backend.play()
        self.backend.add_list(["track1.mp3"])
        self.backend.reset()
        assert self.backend.is_playing is False
        assert self.backend.is_paused is False
        assert self.backend.current_uri is None
        assert self.backend.played_uris == []

    def test_supported_uris(self) -> None:
        uris = self.backend.supported_uris()
        assert "file" in uris
        assert "http" in uris
        assert "https" in uris

    def test_track_info(self) -> None:
        self.backend.current_uri = "http://example.com/song.mp3"
        info = self.backend.track_info()
        assert info["track"] == "http://example.com/song.mp3"

    def test_get_track_length_returns_zero(self) -> None:
        assert self.backend.get_track_length() == 0

    def test_get_track_position_returns_zero(self) -> None:
        assert self.backend.get_track_position() == 0

    def test_simulate_end_emits_event(self) -> None:
        received: list = []
        self.bus.on("ovos.common_play.media.state", lambda m: received.append(m))
        self.backend.simulate_end()
        assert len(received) == 1
        assert self.backend.is_playing is False

    def test_simulate_invalid_stream(self) -> None:
        received: list = []
        self.bus.on("ovos.common_play.media.state", lambda m: received.append(m))
        self.backend.simulate_invalid_stream()
        assert len(received) == 1
        assert self.backend.is_playing is False


# ---------------------------------------------------------------------------
# OCPCaptureSession tests
# ---------------------------------------------------------------------------

class TestOCPCaptureSessionMessageAccumulation:
    """Message capture and filtering."""

    def setup_method(self) -> None:
        self.bus = FakeBus()

    def test_captures_matching_prefix(self) -> None:
        session = OCPCaptureSession(bus=self.bus)
        session.start()
        self.bus.emit(Message("ovos.common_play.play"))
        session.stop()
        assert "ovos.common_play.play" in session.message_types

    def test_does_not_capture_non_matching(self) -> None:
        session = OCPCaptureSession(bus=self.bus)
        session.start()
        self.bus.emit(Message("some.other.message"))
        session.stop()
        assert "some.other.message" not in session.message_types

    def test_start_clears_previous(self) -> None:
        session = OCPCaptureSession(bus=self.bus)
        session.start()
        self.bus.emit(Message("ovos.common_play.play"))
        session.stop()
        session.start()
        session.stop()
        assert session.messages == []

    def test_context_manager(self) -> None:
        with OCPCaptureSession(bus=self.bus) as session:
            self.bus.emit(Message("ovos.common_play.pause"))
        assert "ovos.common_play.pause" in session.message_types

    def test_assert_sequence_passes(self) -> None:
        with OCPCaptureSession(bus=self.bus) as session:
            self.bus.emit(Message("ovos.common_play.play"))
            self.bus.emit(Message("ovos.common_play.pause"))
        session.assert_sequence("ovos.common_play.play", "ovos.common_play.pause")

    def test_assert_sequence_fails_on_missing(self) -> None:
        with OCPCaptureSession(bus=self.bus) as session:
            self.bus.emit(Message("ovos.common_play.play"))
        with pytest.raises(AssertionError):
            session.assert_sequence("ovos.common_play.stop")

    def test_custom_prefixes(self) -> None:
        session = OCPCaptureSession(bus=self.bus, track_prefixes=["custom.prefix."])
        session.start()
        self.bus.emit(Message("custom.prefix.event"))
        self.bus.emit(Message("ovos.common_play.play"))  # should not be captured
        session.stop()
        assert session.message_types == ["custom.prefix.event"]


try:
    import ovos_media  # noqa: F401
    _HAS_OVOS_MEDIA = True
except ImportError:
    _HAS_OVOS_MEDIA = False


if _HAS_OVOS_MEDIA:
    from ovos_plugin_manager.templates.media import AudioPlayerBackend

    class _RecordingBackend(AudioPlayerBackend):
        """A real OCP ``MediaBackend`` stand-in built by a factory.

        Subclasses the genuine ``AudioPlayerBackend`` (not ``MockOCPBackend``) so
        its ``load_track`` emits the real ``ovos.common_play.media.state``
        ``LOADED_MEDIA`` event the live ``AudioService`` routes on. Records the uri
        its ``play()`` is driven with — analogous to a Music Assistant backend
        calling ``client.play_media(uri)`` — so a test can assert the player's play
        path actually reached the injected backend.
        """

        def __init__(self, bus):
            super().__init__(config={}, bus=bus)
            self.play_calls = []
            self.is_playing = False

        def supported_uris(self):
            return ["library", "http", "https"]

        def play(self, repeat: bool = False):
            self.is_playing = True
            self.play_calls.append(self._now_playing)

        def stop(self):
            self.is_playing = False
            return True

        def pause(self):
            pass

        def resume(self):
            pass

        def lower_volume(self):
            pass

        def restore_volume(self):
            pass

        def get_track_length(self):
            return 0

        def get_track_position(self):
            return 0

        def set_track_position(self, milliseconds):
            pass


@pytest.mark.skipif(not _HAS_OVOS_MEDIA,
                    reason="requires the [media] extra (ovos-media)")
class TestOCPPlayerHarnessBackendInjection:
    """OCPPlayerHarness(backend_factory=...) drives a real injected backend."""

    def test_default_factory_is_mock_backend(self) -> None:
        with OCPPlayerHarness() as h:
            assert isinstance(h.backend, MockOCPBackend)
            assert type(h.backend) is MockOCPBackend

    def test_injected_backend_is_used(self) -> None:
        with OCPPlayerHarness(backend_factory=_RecordingBackend) as h:
            assert isinstance(h.backend, _RecordingBackend)
            # name is supplied by the harness when the backend lacks one
            assert getattr(h.backend, "name", None)

    def test_player_drives_injected_backend_play(self) -> None:
        from ovos_utils.ocp import MediaEntry, PlaybackType
        with OCPPlayerHarness(backend_factory=_RecordingBackend) as h:
            h.play(MediaEntry(uri="library://track/42",
                              playback=PlaybackType.AUDIO))
            assert h.backend.is_playing is True
            assert h.backend.play_calls == ["library://track/42"]


@pytest.mark.skipif(not _HAS_OVOS_MEDIA,
                    reason="requires the [media] extra (ovos-media)")
class TestOCPHarnessRealPlaylistIsinstance:
    """A genuine ovos_utils.ocp.Playlist must satisfy the isinstance checks
    inside ovos_media.player.set_now_playing when driven through the harness.

    A harness that swaps ``ovos_media.player.Playlist`` for a local subclass
    would make this fail: set_now_playing does `isinstance(track, Playlist)`
    against the *module-global* name, so a caller passing a real Playlist
    would be rejected as neither a MediaEntry nor a Playlist.
    """

    def test_real_playlist_passes_isinstance_in_set_now_playing(self) -> None:
        from ovos_utils.ocp import MediaEntry, Playlist, PlaybackType

        with OCPPlayerHarness() as h:
            entry = MediaEntry(uri="library://track/1", playback=PlaybackType.AUDIO)
            playlist = Playlist(entry)
            h.player.set_now_playing(playlist)
            assert h.player.now_playing.uri == "library://track/1"


# ---------------------------------------------------------------------------
# Namespace bridging
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_OVOS_MEDIA,
                    reason="requires the [media] extra (ovos-media)")
class TestOCPHarnessNamespaceBridging:
    """OCPMediaPlayer subscribes to the LEGACY duck/cork topics
    (``recognizer_loop:audio_output_start/end``, ``recognizer_loop:record_begin/end``)
    which OVOS is migrating to the ``ovos.*`` spec namespace
    (``ovos.audio.output.*`` / ``ovos.listener.record.*``). These tests pin that
    the harness FakeBus namespace bridging connects a SPEC-namespace producer to
    those legacy handlers, and that turning the bridge off isolates a single
    namespace.

    The observable behaviour is the *cork* path: while PLAYING, a record-start
    event pauses the player (``handle_cork_request``).
    """

    @staticmethod
    def _play_then(h):
        """Drive the player into PLAYING with an AUDIO MediaEntry."""
        from ovos_utils.ocp import MediaEntry, PlaybackType, PlayerState
        h.play(MediaEntry(uri="http://example.com/song.mp3",
                          playback=PlaybackType.AUDIO))
        h.assert_player_state(PlayerState.PLAYING)

    def test_cork_via_legacy_topic_natively(self) -> None:
        """The legacy ``recognizer_loop:record_begin`` corks (pauses) the player —
        the namespace the player subscribes on works natively."""
        from ovos_utils.ocp import PlayerState
        with OCPPlayerHarness() as h:  # bridging default on
            self._play_then(h)
            h.bus.emit(Message("recognizer_loop:record_begin"))
            time.sleep(0.05)
            h.assert_player_state(PlayerState.PAUSED)

    def test_cork_via_spec_topic_through_bridging(self) -> None:
        """A SPEC producer emitting ``ovos.listener.record.started`` reaches the
        legacy-subscribed ``handle_cork_request`` via emit_legacy bridging and
        corks (pauses) the player."""
        from ovos_spec_tools import SpecMessage
        from ovos_utils.ocp import PlayerState
        with OCPPlayerHarness() as h:  # bridging default on
            self._play_then(h)
            h.bus.emit(Message(str(SpecMessage.LISTENER_RECORD_STARTED)))
            time.sleep(0.05)
            h.assert_player_state(PlayerState.PAUSED)

    def test_no_bridging_isolates_spec_from_legacy(self) -> None:
        """With bridging OFF, a SPEC ``ovos.listener.record.started`` emit does
        NOT reach the legacy-subscribed cork handler — the player stays PLAYING,
        proving the harness can exercise a single namespace."""
        from ovos_spec_tools import SpecMessage
        from ovos_utils.ocp import PlayerState
        with OCPPlayerHarness(modernize=False, emit_legacy=False) as h:
            self._play_then(h)
            h.bus.emit(Message(str(SpecMessage.LISTENER_RECORD_STARTED)))
            time.sleep(0.2)  # give any (incorrect) bridge a chance to fire
            h.assert_player_state(PlayerState.PLAYING)


@pytest.mark.skipif(not _HAS_OVOS_MEDIA,
                    reason="requires the [media] extra (ovos-media)")
class TestOCPHarnessDuckUnduckEmitsSpecTopics:
    """``duck()``/``unduck()`` simulate what the real ``ovos-audio`` service
    emits on speech begin/end — the spec topics ``ovos.audio.output.started``/
    ``ended`` — not the legacy ``recognizer_loop:audio_output_*`` aliases.

    Bridging is turned off here (``modernize=False, emit_legacy=False``) so
    the assertion pins the topic the *producer* actually emits, rather than
    one FakeBus synthesizes from the other namespace."""

    def test_duck_emits_spec_topic(self) -> None:
        with OCPPlayerHarness(modernize=False, emit_legacy=False) as h:
            seen = []
            h.bus.on("ovos.audio.output.started", lambda m: seen.append(m))
            h.duck()
            assert seen, "duck() did not emit ovos.audio.output.started"

    def test_unduck_emits_spec_topic(self) -> None:
        with OCPPlayerHarness(modernize=False, emit_legacy=False) as h:
            seen = []
            h.bus.on("ovos.audio.output.ended", lambda m: seen.append(m))
            h.unduck()
            assert seen, "unduck() did not emit ovos.audio.output.ended"


@pytest.mark.skipif(not _HAS_OVOS_MEDIA,
                    reason="requires the [media] extra (ovos-media)")
class TestOCPHarnessWithoutGUIInterface:
    """Newer ``ovos-media`` builds have dropped in-core GUI integration
    entirely, so ``ovos_media.player`` no longer defines ``GUIInterface``.
    The harness must still start up against such a build instead of
    AttributeError-ing on an unconditional patch target."""

    def test_enter_succeeds_when_gui_interface_symbol_is_absent(self) -> None:
        import ovos_media.player as ocp_player_module

        had_symbol = hasattr(ocp_player_module, "GUIInterface")
        removed = None
        if had_symbol:
            removed = ocp_player_module.GUIInterface
            del ocp_player_module.GUIInterface
        try:
            assert not hasattr(ocp_player_module, "GUIInterface")
            with OCPPlayerHarness() as h:
                assert h.player is not None
        except NameError as e:
            # An installed ovos-media that still carries in-core GUI
            # integration references GUIInterface directly from
            # OCPMediaPlayer.__init__, independent of the harness's own
            # patch logic — deleting the symbol out from under it simulates
            # a state that build can never actually be in. This scenario
            # only exercises the harness against a build that has genuinely
            # dropped the symbol.
            if "GUIInterface" not in str(e):
                raise
            pytest.skip("installed ovos-media still uses GUIInterface "
                        "internally; this build cannot run without it")
        finally:
            if had_symbol:
                ocp_player_module.GUIInterface = removed


@pytest.mark.skipif(not _HAS_OVOS_MEDIA,
                    reason="requires the [media] extra (ovos-media)")
class TestOCPHarnessWithoutHandlePlay:
    """Newer ``ovos-media`` builds dropped the per-namespace
    ``ovos.{ns}.service.play`` bus surface entirely — ``AudioService`` (and
    its ``BaseMediaService`` parent) no longer define ``handle_play``;
    ``pause``/``resume``/``stop`` survive as plain methods called directly by
    ``OCPMediaPlayer``. The real-backend-factory path of the harness must
    still start up, and playback must still work end-to-end, against a
    build without ``handle_play``."""

    def test_enter_and_play_succeed_when_handle_play_is_absent(self) -> None:
        from ovos_media.media_backends.audio import AudioService
        from ovos_media.media_backends.base import BaseMediaService

        # handle_play is inherited from BaseMediaService — not AudioService's
        # own attribute — so it must be removed at its defining class.
        had_symbol = hasattr(BaseMediaService, "handle_play")
        removed = None
        if had_symbol:
            removed = BaseMediaService.handle_play
            del BaseMediaService.handle_play
        try:
            assert not hasattr(AudioService, "handle_play")
            from ovos_utils.ocp import MediaEntry, PlaybackType
            with OCPPlayerHarness(backend_factory=_RecordingBackend) as h:
                assert h.player is not None
                h.play(MediaEntry(uri="library://track/42",
                                  playback=PlaybackType.AUDIO))
                assert h.backend.is_playing is True
                assert h.backend.play_calls == ["library://track/42"]
        finally:
            if had_symbol:
                BaseMediaService.handle_play = removed
