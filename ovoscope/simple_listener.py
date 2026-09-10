# Copyright 2024 OpenVoiceOS
# Licensed under the Apache License, Version 2.0
"""MiniSimpleListener — drive ovos-simple-listener for bus-sequence testing.

``ovos-simple-listener`` is an alternative OVOS listener service: a single
``SimpleListener`` thread that reads microphone chunks, detects a wake word (or
VAD activation), records the command, and dispatches a small set of callbacks.
Its canonical bus integration (``OVOSCallbacks`` in
``ovos_simple_listener.__main__``) emits ``recognizer_loop:wakeword`` /
``…:record_begin`` on activation, ``…:utterance`` /
``…:speech.recognition.unknown`` after STT, and ``…:record_end`` when the command
finishes.

:class:`MiniSimpleListener` wires a ``SimpleListener`` to a ``FakeBus`` with mock
wake-word / VAD / STT plugins and a :class:`MockFileMicrophone`, runs the loop
over an arbitrary audio file, and captures the emitted bus sequence — sharing the
assertion helpers of :class:`ovoscope.voice_loop.ListenerHarness`.

Example::

    from ovoscope.simple_listener import MiniSimpleListener
    from ovoscope.voice_loop import MockHotWordEngine, MockStreamingSTT

    with MiniSimpleListener(
        wakeword=MockHotWordEngine("hey_mycroft", trigger_after=2),
        stt_instance=MockStreamingSTT(transcript="turn on the lights"),
    ) as sl:
        msgs = sl.feed_file("command.wav")
        sl.assert_record_begin_emitted(msgs)
        sl.assert_utterance_emitted("turn on the lights", msgs)
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, List, Optional, Union

from ovos_bus_client.message import Message
from ovos_utils.fakebus import FakeBus
from ovos_utils.log import LOG

from ovoscope.voice_loop import (
    ListenerHarness,
    MockHotWordEngine,
    MockStreamingSTT,
    MockVADEngine,
)


class _SimpleBusCallbacks:
    """Per-instance ``ListenerCallbacks`` that emit on a ``FakeBus``.

    Mirrors the canonical ``OVOSCallbacks`` from
    ``ovos_simple_listener.__main__`` (minus the listen-sound playback), but
    binds the bus per instance instead of on the class so concurrent harnesses
    do not clobber one another.

    Args:
        bus: The :class:`FakeBus` to emit listener events on.
    """

    def __init__(self, bus: FakeBus) -> None:
        self.bus: FakeBus = bus

    def listen_callback(self) -> None:
        """Activation: emit wakeword + record-begin."""
        self.bus.emit(Message("recognizer_loop:wakeword"))
        self.bus.emit(Message("recognizer_loop:record_begin"))

    def end_listen_callback(self) -> None:
        """Command finished: emit record-end."""
        self.bus.emit(Message("recognizer_loop:record_end"))

    def audio_callback(self, audio: Any) -> None:
        """Recorded command audio is available (no-op)."""

    def error_callback(self, audio: Any) -> None:
        """Empty transcription: emit the recognition-unknown event."""
        self.bus.emit(Message("recognizer_loop:speech.recognition.unknown"))

    def text_callback(self, utterance: str, lang: str) -> None:
        """Transcription succeeded: emit the utterance."""
        self.bus.emit(Message(
            "recognizer_loop:utterance",
            {"utterances": [utterance], "lang": lang},
        ))


class MiniSimpleListener(ListenerHarness):
    """In-process ovos-simple-listener harness for bus-sequence testing.

    Args:
        wakeword: Wake-word engine (``update`` + ``found_wake_word``).  Defaults
            to a :class:`MockHotWordEngine` keyed ``"hey_mycroft"``.  Pass
            ``None`` to use VAD-only activation.
        vad_instance: VAD engine (defaults to :class:`MockVADEngine`).
        stt_instance: Streaming STT engine (defaults to
            :class:`MockStreamingSTT` returning no transcript).
        min_speech_seconds: Minimum command speech before a silence can end it.
        max_silence_seconds: Trailing silence that ends a command.
        max_speech_seconds: Hard cap on command length.
        bus: Optional :class:`FakeBus` to capture on.
        modernize: When a fresh bus is created, also emit the ovos.* spec topic
            whenever a legacy topic is emitted (legacy producer -> spec
            listener). The ``_SimpleBusCallbacks`` emit legacy
            ``recognizer_loop:*`` topics; this bridge lets a spec-topic
            subscriber observe them. Ignored when *bus* is supplied.
        emit_legacy: When a fresh bus is created, also emit the legacy topic
            whenever an ovos.* spec topic is emitted. Set both False to exercise
            a single namespace with no bridging. Ignored when *bus* is supplied.

    Raises:
        RuntimeError: If ovos-simple-listener is not installed.

    The default timing is tightened (``min_speech_seconds=0`` /
    ``max_silence_seconds=0.1``) so a file-driven command ends promptly and
    deterministically; raise them to mimic production behaviour.
    """

    def __init__(
        self,
        *,
        wakeword: Optional[Any] = "__default__",
        vad_instance: Optional[Any] = None,
        stt_instance: Optional[Any] = None,
        min_speech_seconds: float = 0.0,
        max_silence_seconds: float = 0.1,
        max_speech_seconds: float = 8.0,
        bus: Optional[FakeBus] = None,
        modernize: bool = True,
        emit_legacy: bool = True,
    ) -> None:
        super().__init__(bus, modernize=modernize, emit_legacy=emit_legacy)

        try:
            from ovos_simple_listener import SimpleListener
        except ImportError as e:
            raise RuntimeError(
                "ovos-simple-listener is required to use MiniSimpleListener. "
                "Install it with: pip install ovos-simple-listener"
            ) from e

        if wakeword == "__default__":
            wakeword = MockHotWordEngine("hey_mycroft", trigger_after=2)

        self.callbacks = _SimpleBusCallbacks(self.bus)
        self._listener_cls = SimpleListener
        # Kept so a wedged listener thread can be replaced with a fresh object
        # instead of being reused (see feed_file).
        self._listener_kwargs = dict(
            wakeword=wakeword,
            mic=None,  # supplied per-run by feed_file
            vad=vad_instance if vad_instance is not None else MockVADEngine(),
            stt=stt_instance if stt_instance is not None else MockStreamingSTT(),
            min_speech_seconds=min_speech_seconds,
            max_silence_seconds=max_silence_seconds,
            max_speech_seconds=max_speech_seconds,
            callbacks=self.callbacks,
        )
        self.listener = self._listener_cls(**self._listener_kwargs)

    def _new_listener(self) -> Any:
        """Build a fresh listener object from the original constructor args."""
        return self._listener_cls(**self._listener_kwargs)

    def feed_file(
        self,
        audio: Union[bytes, str, Path],
        *,
        silence_tail_chunks: int = 25,
        chunk_size: int = 2048,
        timeout: float = 10.0,
    ) -> List[Message]:
        """Run the simple listener over an audio file and capture bus events.

        Streams *audio* through a :class:`MockFileMicrophone`, runs the listener
        thread until the command completes (``recognizer_loop:record_end``) or
        *timeout* elapses, then stops it.

        Args:
            audio: Path to a ``.wav`` file, WAV bytes, or raw PCM bytes.
            silence_tail_chunks: Trailing silent frames appended after the audio
                so the command can end.
            chunk_size: Bytes per microphone read.
            timeout: Maximum seconds to wait for the command to finish.

        Returns:
            The list of :class:`Message` objects emitted during the run.
        """
        mic = self._build_file_mic(audio, silence_tail_chunks, chunk_size)
        self.listener.mic = mic

        self._messages.clear()
        self.listener.start()  # Thread.start → runs the loop in the background
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self._has(self._messages, "recognizer_loop:record_end"):
                    break
                time.sleep(0.02)
        finally:
            self.listener.stop()
            self.listener.join(timeout=2.0)
            if self.listener.is_alive():
                # The thread refused to die. Reusing it would let it keep
                # appending to `_messages` during the NEXT run. Abandon it
                # (it is a daemon of the test process) and start clean.
                LOG.warning(
                    "MiniSimpleListener: listener thread still alive 2s after "
                    "stop(); abandoning it and building a fresh listener for "
                    "the next run to avoid cross-run message pollution."
                )
                self.listener = self._new_listener()

        self._last_messages = list(self._messages)
        return list(self._messages)

    def shutdown(self) -> None:
        """Stop the listener thread if still running."""
        try:
            self.listener.stop()
            if self.listener.is_alive():
                self.listener.join(timeout=2.0)
        except Exception:
            pass


def get_mini_simple_listener(
    wakeword: Optional[Any] = "__default__",
    vad_instance: Optional[Any] = None,
    stt_instance: Optional[Any] = None,
    bus: Optional[FakeBus] = None,
    modernize: bool = True,
    emit_legacy: bool = True,
) -> MiniSimpleListener:
    """Factory: create a ready-to-feed :class:`MiniSimpleListener`.

    Args:
        wakeword: Wake-word engine (defaults to a :class:`MockHotWordEngine`).
        vad_instance: VAD engine (defaults to :class:`MockVADEngine`).
        stt_instance: Streaming STT engine (defaults to
            :class:`MockStreamingSTT`).
        bus: Optional :class:`FakeBus` to capture on.
        modernize: When a fresh bus is created, also emit the ovos.* spec topic
            whenever a legacy topic is emitted (legacy producer -> spec
            listener). Ignored when *bus* is supplied.
        emit_legacy: When a fresh bus is created, also emit the legacy topic
            whenever an ovos.* spec topic is emitted. Set both False to exercise
            a single namespace with no bridging. Ignored when *bus* is supplied.

    Returns:
        A fully initialised :class:`MiniSimpleListener`.
    """
    return MiniSimpleListener(
        wakeword=wakeword,
        vad_instance=vad_instance,
        stt_instance=stt_instance,
        bus=bus,
        modernize=modernize,
        emit_legacy=emit_legacy,
    )
