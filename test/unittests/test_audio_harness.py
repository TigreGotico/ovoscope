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

"""Unit tests for ovoscope.audio harness classes.

TestMockAudioBackend  (9 tests) — state tracking, stop() return value, reset
TestAudioServiceHarness (13 tests) — context manager, play/pause/resume/stop,
                                      list_backends, track_info, ducking, session
TestMockTTS           (5 tests)  — WAV output, utterance recording, reset
TestPlaybackServiceHarness (7 tests) — context manager, speak, events, listen
"""

import os
import struct
import tempfile
import time
import threading
import unittest
import importlib.util
from unittest.mock import patch, MagicMock

AUDIO_AVAILABLE = importlib.util.find_spec("ovos_audio") is not None

from ovos_bus_client.message import Message
from ovos_utils.fakebus import FakeBus

if AUDIO_AVAILABLE:
    from ovos_plugin_manager.templates.tts import TTS
    from ovoscope.audio import (
        AudioCaptureSession,
        AudioServiceHarness,
        MockAudioBackend,
        MockTTS,
        PlaybackServiceHarness,
        SILENT_WAV,
    )
else:
    # Stubs for collection when audio extra is missing
    AudioCaptureSession = AudioServiceHarness = MockAudioBackend = \
        MockTTS = PlaybackServiceHarness = SILENT_WAV = object


# ---------------------------------------------------------------------------
# TestMockAudioBackend
# ---------------------------------------------------------------------------

@unittest.skipUnless(AUDIO_AVAILABLE, "ovos-audio (audio extra) not installed")
class TestMockAudioBackend(unittest.TestCase):
    """Unit tests for MockAudioBackend state tracking."""

    def setUp(self) -> None:
        """Create a fresh FakeBus and MockAudioBackend for each test."""
        self.bus = FakeBus()
        self.backend = MockAudioBackend(config={}, bus=self.bus, name="test-backend")

    def test_initial_state(self) -> None:
        """Backend starts in stopped, un-paused state with empty playlist."""
        self.assertFalse(self.backend.is_playing)
        self.assertFalse(self.backend.is_paused)
        self.assertIsNone(self.backend.current_track)
        self.assertEqual(self.backend.played_tracks, [])
        self.assertEqual(self.backend.lower_volume_calls, 0)
        self.assertEqual(self.backend.restore_volume_calls, 0)

    def test_play_sets_is_playing(self) -> None:
        """play() must set is_playing=True and is_paused=False."""
        self.backend.play()
        self.assertTrue(self.backend.is_playing)
        self.assertFalse(self.backend.is_paused)

    def test_stop_clears_playing(self) -> None:
        """stop() must clear is_playing and is_paused."""
        self.backend.play()
        self.backend.stop()
        self.assertFalse(self.backend.is_playing)
        self.assertFalse(self.backend.is_paused)

    def test_stop_returns_true(self) -> None:
        """stop() MUST return True so AudioService emits mycroft.stop.handled."""
        result = self.backend.stop()
        self.assertTrue(result)

    def test_pause_sets_is_paused(self) -> None:
        """pause() sets is_paused=True."""
        self.backend.play()
        self.backend.pause()
        self.assertTrue(self.backend.is_paused)

    def test_resume_clears_is_paused(self) -> None:
        """resume() clears is_paused."""
        self.backend.play()
        self.backend.pause()
        self.backend.resume()
        self.assertFalse(self.backend.is_paused)

    def test_add_list_records_tracks(self) -> None:
        """add_list() appends tracks and sets current_track to first."""
        self.backend.add_list(["http://a.mp3", "http://b.mp3"])
        self.assertEqual(self.backend.played_tracks, ["http://a.mp3", "http://b.mp3"])
        self.assertEqual(self.backend.current_track, "http://a.mp3")

    def test_lower_and_restore_volume_counters(self) -> None:
        """lower_volume/restore_volume increment their counters."""
        self.backend.lower_volume()
        self.backend.lower_volume()
        self.assertEqual(self.backend.lower_volume_calls, 2)
        self.backend.restore_volume()
        self.assertEqual(self.backend.restore_volume_calls, 1)

    def test_reset_clears_all_state(self) -> None:
        """reset() returns backend to initial state."""
        self.backend.add_list(["http://x.mp3"])
        self.backend.play()
        self.backend.lower_volume()
        self.backend.reset()
        self.assertFalse(self.backend.is_playing)
        self.assertFalse(self.backend.is_paused)
        self.assertIsNone(self.backend.current_track)
        self.assertEqual(self.backend.played_tracks, [])
        self.assertEqual(self.backend.lower_volume_calls, 0)
        self.assertEqual(self.backend.restore_volume_calls, 0)


# ---------------------------------------------------------------------------
# TestAudioServiceHarness
# ---------------------------------------------------------------------------

@unittest.skipUnless(AUDIO_AVAILABLE, "ovos-audio (audio extra) not installed")
class TestAudioServiceHarness(unittest.TestCase):
    """Integration tests for AudioServiceHarness."""

    def test_context_manager_enters_and_exits(self) -> None:
        """AudioServiceHarness must be usable as a context manager."""
        with AudioServiceHarness() as h:
            self.assertIsNotNone(h.bus)
            self.assertIsNotNone(h.service)
            self.assertIsNotNone(h.backend)

    def test_play_sets_backend_playing(self) -> None:
        """play() must cause backend.is_playing to become True."""
        with AudioServiceHarness() as h:
            h.play(["http://example.com/song.mp3"])
            h.assert_playing()

    def test_pause_sets_backend_paused(self) -> None:
        """pause() after play must cause backend.is_paused."""
        with AudioServiceHarness() as h:
            h.play(["http://example.com/song.mp3"])
            h.pause()
            h.assert_paused()

    def test_resume_clears_paused(self) -> None:
        """resume() must clear is_paused."""
        with AudioServiceHarness() as h:
            h.play(["http://example.com/song.mp3"])
            h.pause()
            h.assert_paused()
            h.resume()
            self.assertFalse(h.backend.is_paused)

    def test_stop_after_guard_clears_playing(self) -> None:
        """stop() clears playing state when called after the 1-second stop guard."""
        with AudioServiceHarness() as h:
            h.play(["http://example.com/song.mp3"])
            h.assert_playing()
            # AudioService._stop ignores stop if < 1 second since play_start_time.
            # Directly mutate play_start_time to bypass the real-time wait.
            h.service.play_start_time = time.monotonic() - 2.0
            h.stop()
            h.assert_stopped()

    def test_stop_emits_stop_handled(self) -> None:
        """stop() must cause mycroft.stop.handled to be emitted."""
        event = threading.Event()
        with AudioServiceHarness() as h:
            h.bus.on("mycroft.stop.handled", lambda m: event.set())
            h.play(["http://example.com/song.mp3"])
            h.service.play_start_time = time.monotonic() - 2.0
            h.stop()
            self.assertTrue(event.wait(timeout=2.0), "mycroft.stop.handled was not emitted")

    def test_list_backends_returns_mock(self) -> None:
        """list_backends() response must include the mock backend by name."""
        with AudioServiceHarness(backend_name="mymock") as h:
            data = h.list_backends()
        self.assertIsNotNone(data)
        self.assertIn("mymock", data)

    def test_track_info_returns_dict_with_track_key(self) -> None:
        """track_info() response must contain a 'track' key."""
        with AudioServiceHarness() as h:
            h.play(["http://example.com/song.mp3"])
            info = h.get_track_info()
        self.assertIsNotNone(info)
        self.assertIn("track", info)

    def test_queue_appends_to_played_tracks(self) -> None:
        """queue() must append extra tracks to backend.played_tracks."""
        with AudioServiceHarness() as h:
            h.play(["http://example.com/song1.mp3"])
            h.queue(["http://example.com/song2.mp3"])
            self.assertIn("http://example.com/song2.mp3", h.backend.played_tracks)

    def test_ducking_lowers_volume(self) -> None:
        """recognizer_loop:audio_output_start must invoke lower_volume."""
        with AudioServiceHarness() as h:
            h.play(["http://example.com/song.mp3"])
            h.bus.emit(Message("recognizer_loop:audio_output_start"))
            # Give service a moment to process the message
            start = time.monotonic()
            while h.backend.lower_volume_calls == 0 and time.monotonic() - start < 2.0:
                time.sleep(0.01)
            h.assert_volume_lowered()

    def test_ducking_sets_volume_is_speaking(self) -> None:
        """audio_output_start must set service.volume_is_speaking=True."""
        with AudioServiceHarness() as h:
            h.play(["http://example.com/song.mp3"])
            h.bus.emit(Message("recognizer_loop:audio_output_start"))
            start = time.monotonic()
            while not h.service.volume_is_speaking and time.monotonic() - start < 2.0:
                time.sleep(0.01)
            self.assertTrue(h.service.volume_is_speaking)

    def test_ducking_restores_volume_on_end(self) -> None:
        """audio_output_end must restore volume after ducking."""
        with AudioServiceHarness() as h:
            h.play(["http://example.com/song.mp3"])
            h.bus.emit(Message("recognizer_loop:audio_output_start"))
            h.bus.emit(Message("recognizer_loop:audio_output_end"))
            start = time.monotonic()
            while h.backend.restore_volume_calls == 0 and time.monotonic() - start < 2.0:
                time.sleep(0.01)
            h.assert_volume_restored()
            self.assertFalse(h.service.volume_is_speaking)

    def test_validate_source_blocks_non_default_session(self) -> None:
        """With validate_source=True, messages from non-default sessions must be ignored."""
        from ovos_bus_client.session import Session
        with AudioServiceHarness(validate_source=True) as h:
            # Create a non-default session
            custom_sess = Session("custom-session-xyz")
            msg = Message("mycroft.audio.service.play",
                          {"tracks": ["http://example.com/song.mp3"]},
                          {"session": custom_sess.serialize()})
            h.bus.emit(msg)
            # Give a moment for it to be processed (or correctly ignored)
            time.sleep(0.1)
            # Backend should NOT have been called (source validation rejects it)
            self.assertFalse(h.backend.is_playing)


# ---------------------------------------------------------------------------
# TestMockTTS
# ---------------------------------------------------------------------------

@unittest.skipUnless(AUDIO_AVAILABLE, "ovos-audio (audio extra) not installed")
class TestMockTTS(unittest.TestCase):
    """Unit tests for MockTTS."""

    def test_get_tts_writes_valid_wav(self) -> None:
        """get_tts() must write a 44-byte valid WAV to the specified path."""
        tts = MockTTS()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        try:
            tts.get_tts("hello world", path)
            with open(path, "rb") as fh:
                content = fh.read()
            self.assertEqual(content[:4], b"RIFF")
            self.assertEqual(content[8:12], b"WAVE")
            self.assertEqual(len(content), 44)
        finally:
            os.unlink(path)

    def test_get_tts_returns_path_and_none(self) -> None:
        """get_tts() must return (wav_file, None)."""
        tts = MockTTS()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        try:
            result = tts.get_tts("test sentence", path)
            self.assertEqual(result[0], path)
            self.assertIsNone(result[1])
        finally:
            os.unlink(path)

    def test_get_tts_records_utterance(self) -> None:
        """get_tts() must append the sentence to spoken_utterances."""
        tts = MockTTS()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        try:
            tts.get_tts("hello", path)
            self.assertIn("hello", tts.spoken_utterances)
        finally:
            os.unlink(path)

    def test_silent_wav_constant(self) -> None:
        """SILENT_WAV class attribute must be 44 bytes and start with RIFF."""
        self.assertEqual(len(MockTTS.SILENT_WAV), 44)
        self.assertEqual(MockTTS.SILENT_WAV[:4], b"RIFF")

    def test_reset_clears_spoken_utterances(self) -> None:
        """reset() must empty spoken_utterances."""
        tts = MockTTS()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        try:
            tts.get_tts("one", path)
            tts.get_tts("two", path)
        finally:
            os.unlink(path)
        tts.reset()
        self.assertEqual(tts.spoken_utterances, [])


# ---------------------------------------------------------------------------
# TestPlaybackServiceHarness
# ---------------------------------------------------------------------------

@unittest.skipUnless(AUDIO_AVAILABLE, "ovos-audio (audio extra) not installed")
class TestPlaybackServiceHarness(unittest.TestCase):
    """Integration tests for PlaybackServiceHarness."""

    def test_context_manager_enters_and_exits(self) -> None:
        """PlaybackServiceHarness must construct and tear down cleanly."""
        with PlaybackServiceHarness() as h:
            self.assertIsNotNone(h.bus)
            self.assertIsNotNone(h.svc)
            self.assertIsNotNone(h.mock_tts)

    def test_speak_records_utterance(self) -> None:
        """speak() must result in utterance appearing in mock_tts.spoken_utterances."""
        with PlaybackServiceHarness() as h:
            h.speak("hello world")
            h.assert_spoke("hello world")

    def test_speak_emits_audio_output_start(self) -> None:
        """speak() must trigger recognizer_loop:audio_output_start."""
        with PlaybackServiceHarness() as h:
            h.speak("test utterance")
            h.assert_audio_output_started()

    def test_speak_emits_audio_output_end(self) -> None:
        """speak() must trigger recognizer_loop:audio_output_end."""
        with PlaybackServiceHarness() as h:
            h.speak("test utterance")
            h.assert_audio_output_ended()

    def test_speak_expect_response_emits_mic_listen(self) -> None:
        """speak(expect_response=True) must trigger mycroft.mic.listen."""
        with PlaybackServiceHarness() as h:
            h.speak("are you there?", expect_response=True)
            h.assert_mic_listen()

    def test_multiple_speaks_each_recorded(self) -> None:
        """Sequential speak calls must each appear in spoken_utterances."""
        with PlaybackServiceHarness() as h:
            # Emit two speak messages. PlaybackThread may batch them,
            # so we check that both utterances were eventually synthesised.
            h.bus.emit(Message("speak", {"utterance": "first sentence"}))
            h.bus.emit(Message("speak", {"utterance": "second sentence"}))

            # Wait for both to be recorded by MockTTS
            start = time.monotonic()
            while len(h.mock_tts.spoken_utterances) < 2 and time.monotonic() - start < 5.0:
                time.sleep(0.1)

            self.assertIn("first sentence", h.mock_tts.spoken_utterances)
            self.assertIn("second sentence", h.mock_tts.spoken_utterances)

    def test_stop_tts_halts_playback(self) -> None:
        """mycroft.stop must not raise and leaves service intact."""
        with PlaybackServiceHarness() as h:
            h.speak("something to say")
            h.stop()
            # Service should still be usable after stop
            self.assertIsNotNone(h.svc)


# ---------------------------------------------------------------------------
# TestAudioCaptureSession
# ---------------------------------------------------------------------------

@unittest.skipUnless(AUDIO_AVAILABLE, "ovos-audio (audio extra) not installed")
class TestAudioCaptureSession(unittest.TestCase):
    """Unit tests for AudioCaptureSession."""

    def test_captures_matching_messages(self) -> None:
        """AudioCaptureSession must capture messages matching track_prefixes."""
        bus = FakeBus()
        with AudioCaptureSession(bus=bus) as cap:
            bus.emit(Message("mycroft.audio.playing_track", {"track": "x.mp3"}))
            # Wait for capture
            start = time.monotonic()
            while not cap.message_types and time.monotonic() - start < 2.0:
                time.sleep(0.01)
        self.assertIn("mycroft.audio.playing_track", cap.message_types)

    def test_does_not_capture_unmatched_messages(self) -> None:
        """AudioCaptureSession must not capture messages that don't match prefixes."""
        bus = FakeBus()
        with AudioCaptureSession(bus=bus) as cap:
            bus.emit(Message("speak", {"utterance": "hello"}))
            time.sleep(0.05)
        self.assertNotIn("speak", cap.message_types)

    def test_assert_sequence_passes_for_correct_order(self) -> None:
        """assert_sequence must pass when types appear in the captured sequence."""
        bus = FakeBus()
        with AudioCaptureSession(bus=bus) as cap:
            bus.emit(Message("recognizer_loop:audio_output_start"))
            bus.emit(Message("recognizer_loop:audio_output_end"))
            # Wait for capture
            start = time.monotonic()
            while len(cap.message_types) < 2 and time.monotonic() - start < 2.0:
                time.sleep(0.01)
        cap.assert_sequence(
            "recognizer_loop:audio_output_start",
            "recognizer_loop:audio_output_end",
        )

    def test_assert_sequence_fails_for_missing_type(self) -> None:
        """assert_sequence must raise AssertionError for a missing message type."""
        bus = FakeBus()
        with AudioCaptureSession(bus=bus) as cap:
            bus.emit(Message("recognizer_loop:audio_output_start"))
            time.sleep(0.05)
        with self.assertRaises(AssertionError):
            cap.assert_sequence("recognizer_loop:audio_output_end")


# ---------------------------------------------------------------------------
# TestAudioHarnessNamespaceBridging
# ---------------------------------------------------------------------------

@unittest.skipUnless(AUDIO_AVAILABLE, "ovos-audio (audio extra) not installed")
class TestAudioHarnessNamespaceBridging(unittest.TestCase):
    """The audio harness subscribes on the ovos.* SPEC topics while ovos-audio
    emits the LEGACY topics. These tests pin that the FakeBus namespace bridging
    is what connects them, and that turning it off isolates a single namespace.
    """

    def test_ducking_works_via_bridging_default(self) -> None:
        """Default harness (bridging on): ovos-audio's legacy
        recognizer_loop:audio_output_start reaches the spec-subscribed
        _lower_volume_on_speak via modernize bridging."""
        with AudioServiceHarness() as h:  # modernize/emit_legacy default on
            h.play(["http://example.com/song.mp3"])
            h.bus.emit(Message("recognizer_loop:audio_output_start"))
            start = time.monotonic()
            while h.backend.lower_volume_calls == 0 and time.monotonic() - start < 2.0:
                time.sleep(0.01)
            h.assert_volume_lowered()

    def test_ducking_via_spec_topic_directly(self) -> None:
        """A SPEC producer (ovos.audio.output.started) also reaches the
        spec-subscribed ducking handler — the harness exercises the new namespace
        natively too."""
        from ovos_spec_tools import SpecMessage
        with AudioServiceHarness() as h:
            h.play(["http://example.com/song.mp3"])
            h.bus.emit(Message(str(SpecMessage.AUDIO_OUTPUT_STARTED)))
            start = time.monotonic()
            while h.backend.lower_volume_calls == 0 and time.monotonic() - start < 2.0:
                time.sleep(0.01)
            h.assert_volume_lowered()

    def test_no_bridging_isolates_legacy_from_spec(self) -> None:
        """With bridging OFF, a legacy emit does NOT reach the spec-subscribed
        ducking handler — proving the harness can exercise a single namespace."""
        with AudioServiceHarness(modernize=False, emit_legacy=False) as h:
            h.play(["http://example.com/song.mp3"])
            h.bus.emit(Message("recognizer_loop:audio_output_start"))
            time.sleep(0.3)  # give any (incorrect) bridge a chance to fire
            self.assertEqual(h.backend.lower_volume_calls, 0)

    def test_speak_lifecycle_via_bridging(self) -> None:
        """PlaybackService emits legacy audio_output_start/end; the harness
        observes them on the spec topics via bridging (default on)."""
        with PlaybackServiceHarness() as h:
            h.speak("namespace test")
            h.assert_audio_output_started()
            h.assert_audio_output_ended()


@unittest.skipUnless(AUDIO_AVAILABLE, "ovos-audio (audio extra) not installed")
class TestPlaybackServiceHarnessIsolation(unittest.TestCase):
    """Repeated, independent harness instances must not interfere.

    Regression for the shared ``TTS.playback`` class-attribute hazard: a
    garbage-collected MockTTS from an earlier harness used to stop the
    PlaybackThread of a *later*, still-running harness (via the inherited
    ``TTS.__del__`` -> ``TTS.stop`` -> ``TTS.playback.stop()`` chain). The
    victim thread terminated mid-run, its queued speak never played, and the
    next ``speak()`` hung until timeout. Because GC timing is nondeterministic
    this manifested as a flaky ``TimeoutError`` only after several
    create/destroy cycles.
    """

    def test_many_sequential_harnesses_each_complete_speaks(self) -> None:
        """Boot and tear down many harnesses, forcing GC between them, and
        require every speak in every harness to complete deterministically."""
        import gc

        for i in range(12):
            with PlaybackServiceHarness() as h:
                for tag in ("a", "b", "c"):
                    # unique sentences so the persistent TTS cache never
                    # short-circuits synthesis — each must drive real playback
                    h.speak(f"iter {i} part {tag}", timeout=8.0)
                    self.assertIn(f"iter {i} part {tag}",
                                  h.mock_tts.spoken_utterances)
            # provoke collection of the just-exited MockTTS *now*, while a
            # fresh harness will shortly own TTS.playback. Pre-fix, this is
            # exactly what killed the next harness's playback thread.
            gc.collect()

    def test_stale_mock_destructor_does_not_kill_live_thread(self) -> None:
        """A finished harness's MockTTS destructor must not terminate the
        playback thread that a *later* harness now owns.

        Deterministic reproduction of the GC race: keep a reference to harness
        A's MockTTS so it outlives A, open harness B (which registers its own
        thread on the shared ``TTS.playback`` class attribute), then run A's
        destructor. Pre-fix, ``MockTTS.__del__`` chained into
        ``TTS.playback.stop()`` and terminated B's live thread; B's next speak
        would then hang. With the no-op destructor, B is unaffected.
        """
        # Harness A — produce a MockTTS that survives the context exit.
        with PlaybackServiceHarness() as ha:
            ha.speak("harness A warmup", timeout=8.0)
        stale_mock = ha.mock_tts

        # Harness B now owns the shared TTS.playback thread.
        with PlaybackServiceHarness() as hb:
            self.assertIs(TTS.playback, hb.svc.playback_thread)
            self.assertTrue(hb.svc.playback_thread.is_alive())

            # Fire harness A's destructor explicitly (what GC would do).
            stale_mock.__del__()

            # The precise invariant: A's destructor must not have flagged B's
            # thread for termination. ``_terminated`` is checked at the top of
            # the playback loop, so a single in-flight speak can still slip
            # through even when set — but the thread would then exit on its next
            # iteration, hanging a subsequent speak. Assert the flag directly.
            self.assertFalse(
                hb.svc.playback_thread._terminated,
                "stale MockTTS destructor terminated the live playback thread",
            )

            # And B must keep working across multiple speaks (the loop must not
            # have exited).
            for n in range(3):
                hb.speak(f"harness B speak {n}", timeout=8.0)
            self.assertTrue(hb.svc.playback_thread.is_alive())

    def test_repeated_sentence_is_synthesised_by_each_harness(self) -> None:
        """The same sentence spoken in two harnesses must be synthesised twice.

        ``TTSContext._caches`` is class-level and keyed by tts_id, so audio
        rendered by one harness used to satisfy the next harness's request for
        the same sentence: ``get_tts`` was never called and the second
        harness's ``spoken_utterances`` stayed empty, silently degrading any
        later test that asserts on what was actually synthesised.
        """
        with PlaybackServiceHarness() as first:
            first.speak("hello world", timeout=8.0)
            self.assertIn("hello world", first.mock_tts.spoken_utterances)

        second_tts = MockTTS()
        with PlaybackServiceHarness(tts=second_tts) as second:
            second.speak("hello world", timeout=8.0)
        self.assertIn("hello world", second_tts.spoken_utterances)


if __name__ == "__main__":
    unittest.main()
