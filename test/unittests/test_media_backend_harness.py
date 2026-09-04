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
"""Unit tests for ovoscope.media_backend.MediaBackendHarness.

Uses a small standalone MediaBackend v2 plugin stand-in (not MockOCPBackendV2)
so these tests exercise the harness against a plugin author's own class, the
way a real plugin repo would use it.
"""

import pytest

from ovos_utils.fakebus import FakeBus

from ovoscope.media_backend import MediaBackendHarness

try:
    from ovos_plugin_manager.templates.media import AudioPlayerBackend, PlaybackEvent
    _HAS_MEDIABACKEND_V2 = True
except ImportError:
    # The v2 template only exists on the unreleased feat/media-backend-v2
    # OPM branch — fall back to a plain placeholder so _FakePlugin below
    # still defines cleanly at collection time; every test in this module
    # is skipped via pytestmark when this is False.
    AudioPlayerBackend = object
    PlaybackEvent = None
    _HAS_MEDIABACKEND_V2 = False

pytestmark = pytest.mark.skipif(
    not _HAS_MEDIABACKEND_V2,
    reason="requires the MediaBackend v2 template (unreleased ovos-plugin-manager)")


class _FakePlugin(AudioPlayerBackend):
    """A minimal MediaBackend v2 plugin: no real audio, records calls."""

    def __init__(self, config=None, bus=None):
        super().__init__(config=config, bus=bus)
        self.loaded_uri = None

    def supported_uris(self):
        return ["file", "http", "https"]

    def load_track(self, uri, metadata=None):
        if "invalid" in uri:
            return False
        self.loaded_uri = uri
        return True

    def play(self):
        self.report(PlaybackEvent.TRACK_START, uri=self.loaded_uri)

    def _stop(self):
        return True

    def pause(self):
        self.report(PlaybackEvent.PAUSED, uri=self.loaded_uri)

    def resume(self):
        self.report(PlaybackEvent.RESUMED, uri=self.loaded_uri)

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


@pytest.fixture
def backend():
    return _FakePlugin(config={}, bus=FakeBus())


class TestMediaBackendHarnessHappyPath:
    """load_track -> play reports TRACK_START, captured verbatim."""

    def test_load_track_returns_true(self, backend) -> None:
        with MediaBackendHarness(backend) as h:
            assert h.load_track("file:///tmp/song.mp3") is True

    def test_assert_load_track_succeeds(self, backend) -> None:
        with MediaBackendHarness(backend) as h:
            h.assert_load_track_succeeds("file:///tmp/song.mp3")

    def test_play_reports_track_start(self, backend) -> None:
        with MediaBackendHarness(backend) as h:
            h.load_track("file:///tmp/song.mp3")
            h.play()
            h.assert_events(PlaybackEvent.TRACK_START)

    def test_event_data_carries_uri(self, backend) -> None:
        with MediaBackendHarness(backend) as h:
            h.load_track("file:///tmp/song.mp3")
            h.play()
            h.assert_event_data(PlaybackEvent.TRACK_START, uri="file:///tmp/song.mp3")


class TestMediaBackendHarnessPauseResume:
    """pause()/resume() report PAUSED/RESUMED in order."""

    def test_pause_then_resume_sequence(self, backend) -> None:
        with MediaBackendHarness(backend) as h:
            h.load_track("file:///tmp/song.mp3")
            h.play()
            h.pause()
            h.resume()
            h.assert_events(
                PlaybackEvent.TRACK_START,
                PlaybackEvent.PAUSED,
                PlaybackEvent.RESUMED,
            )


class TestMediaBackendHarnessStopVsNaturalEnd:
    """report_track_end() disambiguates an explicit stop() from a track
    finishing on its own, via the plugin's _stop_requested flag."""

    def test_natural_end_reports_end_of_media(self, backend) -> None:
        with MediaBackendHarness(backend) as h:
            h.load_track("file:///tmp/song.mp3")
            h.play()
            # a natural end: an exit/status callback fires without stop()
            # having been called first.
            backend.report_track_end(uri="file:///tmp/song.mp3")
            h.assert_events(PlaybackEvent.TRACK_START, PlaybackEvent.END_OF_MEDIA)

    def test_explicit_stop_reports_stopped_not_end_of_media(self, backend) -> None:
        with MediaBackendHarness(backend) as h:
            h.load_track("file:///tmp/song.mp3")
            h.play()
            h.stop()
            # the same exit/status callback now fires after an explicit stop().
            backend.report_track_end(uri="file:///tmp/song.mp3")
            h.assert_events(PlaybackEvent.TRACK_START, PlaybackEvent.STOPPED)
            assert PlaybackEvent.END_OF_MEDIA not in h.event_types

    def test_stop_requested_flag_clears_after_report(self, backend) -> None:
        """The flag is per-report, not sticky — a stop followed by two
        track-end callbacks must not tag the second one STOPPED too."""
        with MediaBackendHarness(backend) as h:
            h.load_track("file:///tmp/song.mp3")
            h.play()
            h.stop()
            backend.report_track_end(uri="file:///tmp/song.mp3")
            # a second, unrelated track now ends naturally
            backend.report_track_end(uri="file:///tmp/song2.mp3")
            h.assert_events(
                PlaybackEvent.TRACK_START,
                PlaybackEvent.STOPPED,
                PlaybackEvent.END_OF_MEDIA,
            )


class TestMediaBackendHarnessErrorPath:
    """report_track_end(error=...) reports ERROR with the error kwarg."""

    def test_error_reports_error_event_with_message(self, backend) -> None:
        with MediaBackendHarness(backend) as h:
            h.load_track("file:///tmp/song.mp3")
            h.play()
            backend.report_track_end(uri="file:///tmp/song.mp3",
                                     error="disk full")
            h.assert_events(PlaybackEvent.TRACK_START, PlaybackEvent.ERROR)
            h.assert_event_data(PlaybackEvent.ERROR, error="disk full")

    def test_error_is_coerced_to_str_from_an_exception(self, backend) -> None:
        """report_track_end's error=... coercion (str(error)) must actually
        happen — a plugin reporting a raw exception object must not leak it
        into the wire-facing data dict."""
        with MediaBackendHarness(backend) as h:
            h.load_track("file:///tmp/song.mp3")
            h.play()
            backend.report_track_end(uri="file:///tmp/song.mp3",
                                     error=RuntimeError("boom"))
            error_data = dict(h.events[-1][1])
            assert isinstance(error_data["error"], str)
            assert error_data["error"] == "boom"

    def test_error_is_coerced_to_str_from_a_non_string(self, backend) -> None:
        with MediaBackendHarness(backend) as h:
            h.load_track("file:///tmp/song.mp3")
            h.play()
            backend.report_track_end(uri="file:///tmp/song.mp3", error=1)
            error_data = dict(h.events[-1][1])
            assert isinstance(error_data["error"], str)
            assert error_data["error"] == "1"

    def test_error_takes_priority_over_stop_requested(self, backend) -> None:
        """error=... must win even if stop() was also called — a failure
        must never be reported as a clean STOPPED."""
        with MediaBackendHarness(backend) as h:
            h.load_track("file:///tmp/song.mp3")
            h.play()
            h.stop()
            backend.report_track_end(uri="file:///tmp/song.mp3", error="crashed")
            h.assert_events(PlaybackEvent.TRACK_START, PlaybackEvent.ERROR)
            assert PlaybackEvent.STOPPED not in h.event_types


class TestMediaBackendHarnessLoadFailure:
    """load_track returning False is the failed-load path."""

    def test_assert_load_track_fails(self, backend) -> None:
        with MediaBackendHarness(backend) as h:
            h.assert_load_track_fails("file:///tmp/invalid.mp3")

    def test_failed_load_reports_nothing(self, backend) -> None:
        with MediaBackendHarness(backend) as h:
            h.load_track("file:///tmp/invalid.mp3")
            assert h.events == []


class TestMediaBackendHarnessUnboundReporter:
    """report() no-ops (never raises) when nothing is bound yet."""

    def test_report_before_bind_is_a_silent_noop(self, backend) -> None:
        # bind_event_reporter has not been called — the harness context
        # manager is what binds it.
        backend.report(PlaybackEvent.TRACK_START, uri="file:///tmp/song.mp3")

    def test_report_after_unbind_is_a_silent_noop(self, backend) -> None:
        with MediaBackendHarness(backend) as h:
            h.load_track("file:///tmp/song.mp3")
            h.play()
        # harness has exited -> unbound
        backend.report(PlaybackEvent.PAUSED, uri="file:///tmp/song.mp3")
        assert h.event_types == [PlaybackEvent.TRACK_START]


class TestMediaBackendHarnessEventSubsequence:
    """assert_events is a subsequence match, mirroring
    OCPCaptureSession.assert_sequence — other events may appear in between."""

    def test_missing_event_raises(self, backend) -> None:
        with MediaBackendHarness(backend) as h:
            h.load_track("file:///tmp/song.mp3")
            h.play()
            with pytest.raises(AssertionError):
                h.assert_events(PlaybackEvent.TRACK_START, PlaybackEvent.STOPPED)

    def test_out_of_order_raises(self, backend) -> None:
        with MediaBackendHarness(backend) as h:
            h.load_track("file:///tmp/song.mp3")
            h.play()
            h.pause()
            with pytest.raises(AssertionError):
                h.assert_events(PlaybackEvent.PAUSED, PlaybackEvent.TRACK_START)

    def test_unmatched_event_data_raises(self, backend) -> None:
        with MediaBackendHarness(backend) as h:
            h.load_track("file:///tmp/song.mp3")
            h.play()
            with pytest.raises(AssertionError):
                h.assert_event_data(PlaybackEvent.TRACK_START, uri="wrong-uri")

    def test_assert_event_data_on_unreported_event_raises(self, backend) -> None:
        with MediaBackendHarness(backend) as h:
            with pytest.raises(AssertionError):
                h.assert_event_data(PlaybackEvent.TRACK_START, uri="anything")
