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
"""Live fixture recording from a running OVOS MessageBus instance.

:class:`RemoteRecorder` connects to a live ``ovos-messagebus`` server,
fires an utterance, and captures the resulting message sequence as an
:class:`~ovoscope.End2EndTest` fixture for later replay.

Requires ``ovos-bus-client`` (already a transitive dependency via
``ovos-core``).

Example::

    from ovoscope.remote_recorder import RemoteRecorder

    recorder = RemoteRecorder(bus_url="ws://localhost:8181/core")
    recorder.connect()
    test = recorder.record(
        utterance="what time is it",
        skill_id="ovos-skill-date-time.openvoiceos",
        timeout=15.0,
    )
    recorder.disconnect()
    test.save("fixture_datetime.json")
"""
from __future__ import annotations

import threading
import time
from typing import Any, List, Optional

from ovos_utils.messagebus import Message


class RemoteRecorder:
    """Record an :class:`~ovoscope.End2EndTest` fixture from a live OVOS instance.

    Connects to a running ``ovos-messagebus`` WebSocket server, emits
    a ``recognizer_loop:utterance`` message, and waits for the response
    sequence to complete.

    Args:
        bus_url: WebSocket URL of the running MessageBus
            (default ``"ws://localhost:8181/core"``).

    Example::

        recorder = RemoteRecorder()
        recorder.connect()
        test = recorder.record("hello", skill_id="ovos-skill-hello-world.openvoiceos")
        recorder.disconnect()
        test.save("/tmp/hello_fixture.json")
    """

    _EOF_TYPES = frozenset({
        "mycroft.mic.listen",
        "ovos.utterance.handled",
        "complete_intent_failure",
        "intent_failure",
        "mycroft.skill.handler.complete",
        "ovos.session.update_default",
    })

    def __init__(self, bus_url: str = "ws://localhost:8181/core") -> None:
        self.bus_url: str = bus_url
        self._client: Any = None
        self._captured: List[Message] = []
        self._done_event: threading.Event = threading.Event()

    def connect(self) -> None:
        """Connect to the running OVOS MessageBus.

        Raises:
            ImportError: If ``ovos-bus-client`` is not installed.
            ConnectionError: If the connection cannot be established.
        """
        try:
            from ovos_bus_client.client import MessageBusClient  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "ovos-bus-client is required for RemoteRecorder. "
                "Install it with: pip install ovos-bus-client"
            ) from exc

        host, port, path = self._parse_url(self.bus_url)
        self._client = MessageBusClient(host=host, port=port, route=path)
        self._client.run_in_thread()
        # Wait for connection
        deadline = time.monotonic() + 10.0
        while not self._client.connected_event.is_set():
            if time.monotonic() > deadline:
                # Close the client before giving up — MessageBusClient keeps a
                # reconnect thread alive forever otherwise.
                client, self._client = self._client, None
                try:
                    client.close()
                except Exception:
                    pass
                raise ConnectionError(f"Could not connect to {self.bus_url} within 10 seconds.")
            time.sleep(0.1)

    def disconnect(self) -> None:
        """Disconnect from the MessageBus."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def record(
        self,
        utterance: str,
        skill_id: Optional[str] = None,
        lang: str = "en-US",
        timeout: float = 15.0,
    ) -> Any:
        """Fire *utterance* and capture the response as a fixture.

        Args:
            utterance: The text utterance to send.
            skill_id: Optional skill ID to tag on the source message context.
            lang: Language tag (default ``"en-US"``).
            timeout: Maximum seconds to wait for the interaction to complete.

        Returns:
            An :class:`~ovoscope.End2EndTest` instance ready to save or replay.

        Raises:
            RuntimeError: If :meth:`connect` has not been called.
            TimeoutError: If the interaction does not complete within *timeout*.
        """
        if self._client is None:
            raise RuntimeError("Call connect() before record().")

        from ovoscope import End2EndTest  # avoid circular at module level

        self._captured.clear()
        self._done_event.clear()

        # Subscribe to all messages
        self._client.on("message", self._on_message)

        context: dict = {"lang": lang}
        if skill_id:
            context["skill_id"] = skill_id

        src = Message(
            "recognizer_loop:utterance",
            data={"utterances": [utterance], "lang": lang},
            context=context,
        )
        self._client.emit(src)

        completed = self._done_event.wait(timeout=timeout)
        self._client.remove("message", self._on_message)

        if not completed:
            raise TimeoutError(
                f"Interaction did not complete within {timeout}s for utterance {utterance!r}"
            )

        # Build End2EndTest from captured messages
        skill_ids = [skill_id] if skill_id else []
        test = End2EndTest(
            skill_ids=skill_ids,
            source_message=src,
            expected_messages=list(self._captured),
        )
        return test

    def _on_message(self, message: Any) -> None:
        """Internal handler: capture messages and detect end-of-interaction.

        Args:
            message: Raw message string or :class:`Message` object.
        """
        if isinstance(message, str):
            try:
                message = Message.deserialize(message)
            except Exception:
                return
        self._captured.append(message)
        if message.msg_type in self._EOF_TYPES:
            self._done_event.set()

    @staticmethod
    def _parse_url(url: str) -> tuple[str, int, str]:
        """Parse a WebSocket URL into (host, port, path) components.

        Args:
            url: WebSocket URL string (e.g. ``ws://localhost:8181/core``).

        Returns:
            Tuple of (host, port, path).
        """
        # Strip scheme
        raw = url.replace("ws://", "").replace("wss://", "").replace("http://", "")
        if "/" in raw:
            host_port, path = raw.split("/", 1)
            path = "/" + path
        else:
            host_port, path = raw, "/core"

        if ":" in host_port:
            host, port_str = host_port.rsplit(":", 1)
            port = int(port_str)
        else:
            host = host_port
            port = 8181

        return host, port, path
