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

"""MediaBackend v2 plugin harness for ovoscope.

Drives a single ``ovos_plugin_manager.templates.media.MediaBackend`` plugin
(the v2 contract: ``load_track``/``play``/``pause``/``resume``/``stop`` plus
``report``/``bind_event_reporter``/``report_track_end``) in isolation, without
a running ``OCPMediaPlayer`` or ``ovos-media`` daemon in front of it. This is
the plugin-author-facing counterpart to ``ovoscope.media.OCPPlayerHarness``
(which drives a backend *through* the daemon); use this one to test a backend
plugin on its own, and the OCP harness to test how it behaves once wired into
the full player state machine.

The v2 template this harness targets (``ovos_plugin_manager.templates.media``)
only exists on the unreleased ``feat/media-backend-v2`` branch of
``ovos-plugin-manager`` — a released ``ovos-plugin-manager`` does not have it
yet. TEMPORARY: once ``ovos-plugin-manager>=3.0.0a1`` releases with the v2
template, it becomes an explicit ovoscope dependency and the guard below can
be dropped. Until then, importing this *module* is always safe; constructing
:class:`MediaBackendHarness` against a released ``ovos-plugin-manager`` raises
a clear ``ImportError`` instead of failing at import time.

Classes:
    MediaBackendHarness -- context manager binding a spy reporter onto a
                           MediaBackend v2 plugin instance
"""

from typing import Any, Dict, List, Tuple

try:
    from ovos_plugin_manager.templates.media import MediaBackend, PlaybackEvent
    _IMPORT_ERROR: Exception = None
except ImportError as _e:  # pragma: no cover - exercised via released OPM only
    MediaBackend = object
    PlaybackEvent = None
    _IMPORT_ERROR = _e


class MediaBackendHarness:
    """Binds a capturing spy reporter onto a ``MediaBackend`` v2 plugin
    instance and drives it, so a test can assert exactly which
    ``PlaybackEvent``\\ s the plugin reported and with what data — without a
    daemon translating them into bus messages in between.

    Usage::

        backend = MyVlcBackend(config={}, bus=FakeBus())
        with MediaBackendHarness(backend) as h:
            assert h.load_track("file:///tmp/song.mp3") is True
            h.play()
            h.assert_events(PlaybackEvent.TRACK_START)

            h.backend._stop()  # simulate the player process exiting cleanly
            h.backend.report_track_end(uri="file:///tmp/song.mp3")
            h.assert_events(PlaybackEvent.TRACK_START, PlaybackEvent.END_OF_MEDIA)

    Args:
        backend: an already-constructed ``MediaBackend`` v2 plugin instance.
            The harness does not construct the backend itself — a plugin's
            constructor may need a real or mocked ``bus``/``config`` only the
            caller knows how to build.
    """

    def __init__(self, backend: MediaBackend) -> None:
        if _IMPORT_ERROR is not None:
            raise ImportError(
                "MediaBackendHarness requires the MediaBackend v2 template "
                "(ovos_plugin_manager.templates.media), not present in the "
                "installed ovos-plugin-manager. TEMPORARY: this needs the "
                "unreleased feat/media-backend-v2 branch until "
                "ovos-plugin-manager>=3.0.0a1 releases."
            ) from _IMPORT_ERROR
        self.backend = backend
        self.events: List[Tuple[PlaybackEvent, Dict[str, Any]]] = []

    def __enter__(self) -> "MediaBackendHarness":
        """Bind the spy reporter onto the backend.

        Returns:
            self
        """
        self.backend.bind_event_reporter(self._capture)
        return self

    def __exit__(self, *args) -> None:
        """Unbind the spy reporter, restoring the backend to unbound."""
        self.backend._event_reporter = None

    def _capture(self, event: PlaybackEvent, **data) -> None:
        """The spy reporter itself: records every reported event verbatim."""
        self.events.append((event, data))

    # ------------------------------------------------------------------
    # Driving helpers — one per MediaBackend v2 verb
    # ------------------------------------------------------------------

    def load_track(self, uri: str, metadata: dict = None) -> bool:
        """Call ``backend.load_track`` and return its bool result.

        Args:
            uri: uri to load.
            metadata: optional track metadata.

        Returns:
            bool: whatever the backend's ``load_track`` returned.
        """
        return self.backend.load_track(uri, metadata)

    def play(self) -> None:
        """Call ``backend.play()``."""
        self.backend.play()

    def pause(self) -> None:
        """Call ``backend.pause()``."""
        self.backend.pause()

    def resume(self) -> None:
        """Call ``backend.resume()``."""
        self.backend.resume()

    def stop(self) -> bool:
        """Call the concrete ``backend.stop()`` template method (sets the
        explicit-stop flag, then delegates to the plugin's ``_stop``).

        Returns:
            bool: whatever the backend's ``stop`` returned.
        """
        return self.backend.stop()

    # ------------------------------------------------------------------
    # Assertion helpers
    # ------------------------------------------------------------------

    def assert_load_track_succeeds(self, uri: str, metadata: dict = None) -> None:
        """Assert ``load_track(uri, metadata)`` returns True.

        Args:
            uri: uri to load.
            metadata: optional track metadata.
        """
        assert self.load_track(uri, metadata) is True, (
            f"Expected load_track({uri!r}) to succeed (return True)"
        )

    def assert_load_track_fails(self, uri: str, metadata: dict = None) -> None:
        """Assert ``load_track(uri, metadata)`` returns False.

        Args:
            uri: uri to load.
            metadata: optional track metadata.
        """
        assert self.load_track(uri, metadata) is False, (
            f"Expected load_track({uri!r}) to fail (return False)"
        )

    @property
    def event_types(self) -> List[PlaybackEvent]:
        """Return the list of captured event types, in report order.

        Returns:
            List of ``PlaybackEvent`` values.
        """
        return [e for e, _ in self.events]

    def assert_events(self, *expected: PlaybackEvent) -> None:
        """Assert that the captured events contain *expected*, in order, as
        a subsequence (other events may appear in between).

        Mirrors ``ovoscope.media.OCPCaptureSession.assert_sequence``.

        Args:
            *expected: expected ``PlaybackEvent`` values, in order.

        Raises:
            AssertionError: if any expected event is missing or out of order.
        """
        received = self.event_types
        pos = 0
        for expected_event in expected:
            found = False
            while pos < len(received):
                if received[pos] == expected_event:
                    pos += 1
                    found = True
                    break
                pos += 1
            assert found, (
                f"Expected event {expected_event} not found in sequence. "
                f"Full captured sequence: {received}"
            )

    def assert_event_data(self, event: PlaybackEvent, **expected_kwargs) -> None:
        """Assert the *first* captured occurrence of *event* carried
        *expected_kwargs* among its reported data.

        Args:
            event: the ``PlaybackEvent`` to look up.
            **expected_kwargs: kwarg/value pairs expected in that event's
                reported data (e.g. ``error="disk full"``,
                ``uri="file:///tmp/song.mp3"``).

        Raises:
            AssertionError: if *event* was never reported, or a kwarg is
                missing/mismatched.
        """
        for reported_event, data in self.events:
            if reported_event == event:
                for key, value in expected_kwargs.items():
                    assert data.get(key) == value, (
                        f"{event}: expected {key}={value!r}, got "
                        f"{data.get(key)!r} (full data: {data})"
                    )
                return
        raise AssertionError(
            f"{event} was never reported. Captured: {self.event_types}"
        )
