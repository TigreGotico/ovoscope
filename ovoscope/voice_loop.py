# Copyright 2024 OpenVoiceOS
# Licensed under the Apache License, Version 2.0
"""MiniVoiceLoop — drive ``DinkumVoiceLoop`` bus sequences for ovoscope.

Where :class:`ovoscope.listener.MiniListener` covers the
``AudioTransformersService``, STT, and mock VAD/WakeWord engines, it does **not**
exercise the full ``DinkumVoiceLoop`` state machine.  The bus events that matter
for wake-word handling (``recognizer_loop:wakeword``,
``recognizer_loop:record_begin``, ``recognizer_loop:record_end``,
``recognizer_loop:utterance``) are emitted as side-effects of the voice loop as
PCM chunks flow through it.

``MiniVoiceLoop`` wires a real ``DinkumVoiceLoop`` to a ``FakeBus`` with no-op
mic/STT/transformer plugins, a controllable hotword container, and an optional
verifier chain.  It supports two drive modes:

* :meth:`MiniVoiceLoop.feed_chunks` — feed PCM frames directly through
  ``_detect_ww`` to assert the wake-word detection / verifier gate in isolation.
* :meth:`MiniVoiceLoop.feed_file` — run the **whole** ``DinkumVoiceLoop.run()``
  state machine, reading an arbitrary audio file through a file-backed
  microphone plugin, so the full record-begin → wakeword → command → record-end
  → utterance sequence is captured.

Example — verifier gate (``_detect_ww`` only)::

    from unittest.mock import Mock
    from ovoscope.voice_loop import MiniVoiceLoop, MockHotWordEngine

    SILENT_CHUNK = b"\\x00" * 512
    ww = MockHotWordEngine(key_phrase="hey_mycroft", trigger_after=3)
    accepting = Mock(); accepting.verify.return_value = True

    with MiniVoiceLoop(ww_instances={"hey_mycroft": ww},
                       verifiers=[accepting]) as vl:
        msgs = vl.feed_chunks([SILENT_CHUNK] * 5)
        vl.assert_record_begin_emitted(msgs)

Example — full loop driven from an audio file::

    from ovoscope.voice_loop import MiniVoiceLoop, MockStreamingSTT

    stt = MockStreamingSTT(transcript="what time is it")
    with MiniVoiceLoop(stt_instance=stt) as vl:
        msgs = vl.feed_file("command.wav")
        assert any(m.msg_type == "recognizer_loop:utterance" for m in msgs)

The verifier gate lives inside ``DinkumVoiceLoop._detect_ww`` and is only present
in ovos-dinkum-listener builds that ship the hotword-verifier feature
(``HotwordContainer.verify``).  On a build without it the gate is absent and the
detection is never suppressed — assert accordingly for the version under test.
"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from ovos_bus_client.message import Message
from ovos_utils.fakebus import FakeBus

from ovoscope import _topic_matches

# Re-export the engine mocks so callers have a single import site for the
# voice-loop harness.
from ovoscope.listener import MockHotWordEngine, MockVADEngine  # noqa: F401


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _read_audio(
    audio: Union[bytes, str, Path],
    sample_rate: int,
    sample_width: int,
    sample_channels: int,
) -> Tuple[bytes, int, int, int]:
    """Read raw PCM from a WAV file/bytes (or raw PCM) for the file mic.

    Args:
        audio: Path to a ``.wav`` file, WAV bytes, or raw PCM bytes.
        sample_rate: Fallback sample rate when no WAV header is present.
        sample_width: Fallback sample width (bytes).
        sample_channels: Fallback channel count.

    Returns:
        ``(pcm_bytes, sample_rate, sample_width, sample_channels)``.
    """
    if isinstance(audio, (str, Path)):
        with open(audio, "rb") as fh:
            raw = fh.read()
    else:
        raw = audio

    try:
        with wave.open(io.BytesIO(raw)) as wf:
            sample_rate = wf.getframerate()
            sample_width = wf.getsampwidth()
            sample_channels = wf.getnchannels()
            pcm = wf.readframes(wf.getnframes())
            return pcm, sample_rate, sample_width, sample_channels
    except (wave.Error, EOFError):
        # not a WAV container — treat input as raw PCM
        return raw, sample_rate, sample_width, sample_channels


# ---------------------------------------------------------------------------
# Mock plugin stand-ins for the DinkumVoiceLoop dependencies
# ---------------------------------------------------------------------------

class _MockMicrophone:
    """Silent stand-in for the listener ``Microphone`` plugin.

    Used when no audio file is driven; ``_detect_ww`` does not read from the
    mic, but the dataclass requires one and a few derived values
    (``sample_rate``, ``seconds_per_chunk``) are referenced by adjacent loop
    stages.

    Args:
        sample_rate: Audio sample rate in Hz.
        sample_width: Sample width in bytes.
        sample_channels: Channel count.
        chunk_size: Bytes per read.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        sample_width: int = 2,
        sample_channels: int = 1,
        chunk_size: int = 2048,
    ) -> None:
        self.sample_rate: int = sample_rate
        self.sample_width: int = sample_width
        self.sample_channels: int = sample_channels
        self.chunk_size: int = chunk_size

    @property
    def seconds_per_chunk(self) -> float:
        """Duration in seconds of one ``chunk_size`` read."""
        frames = self.chunk_size / max(self.sample_width, 1)
        return frames / max(self.sample_rate, 1)

    def read_chunk(self) -> bytes:
        """Return a silent chunk (the harness feeds audio explicitly)."""
        return b"\x00" * self.chunk_size

    def start(self) -> None:
        """No-op start hook."""

    def stop(self) -> None:
        """No-op stop hook."""


class MockFileMicrophone:
    """File-backed ``Microphone`` plugin for the voice loop.

    Streams an arbitrary audio file (or raw PCM) into ``DinkumVoiceLoop.run()``
    one ``chunk_size`` frame at a time, then appends a tail of silent frames so
    a silence-based VAD can detect the end of the command.  When every frame has
    been read, :meth:`read_chunk` invokes :attr:`on_exhausted` (used by
    :class:`MiniVoiceLoop` to stop the loop) and returns ``None``.

    Args:
        audio: Path to a ``.wav`` file, WAV bytes, or raw PCM bytes.
        chunk_size: Bytes per :meth:`read_chunk`.
        sample_rate: Fallback sample rate when the input has no WAV header.
        sample_width: Fallback sample width (bytes).
        sample_channels: Fallback channel count.
        silence_chunks: Number of trailing silent frames appended after the
            file so the command can end and the loop can return to wake-word
            detection.

    Attributes:
        on_exhausted: Optional zero-arg callback invoked once the audio is fully
            consumed (set by :class:`MiniVoiceLoop` to stop the loop).
    """

    def __init__(
        self,
        audio: Union[bytes, str, Path],
        chunk_size: int = 2048,
        sample_rate: int = 16000,
        sample_width: int = 2,
        sample_channels: int = 1,
        silence_chunks: int = 25,
    ) -> None:
        pcm, sr, sw, ch = _read_audio(
            audio, sample_rate, sample_width, sample_channels
        )
        self.sample_rate: int = sr
        self.sample_width: int = sw
        self.sample_channels: int = ch
        self.chunk_size: int = chunk_size

        self._chunks: List[bytes] = [
            pcm[i:i + chunk_size] for i in range(0, len(pcm), chunk_size)
        ]
        self._chunks.extend(b"\x00" * chunk_size for _ in range(silence_chunks))
        self._idx: int = 0
        self.on_exhausted: Optional[Any] = None

    @property
    def seconds_per_chunk(self) -> float:
        """Duration in seconds of one ``chunk_size`` read."""
        frames = self.chunk_size / max(self.sample_width, 1)
        return frames / max(self.sample_rate, 1)

    def read_chunk(self) -> Optional[bytes]:
        """Return the next audio frame, or ``None`` when exhausted.

        On exhaustion the :attr:`on_exhausted` callback is invoked so the loop
        can stop.
        """
        if self._idx >= len(self._chunks):
            if self.on_exhausted is not None:
                self.on_exhausted()
            return None
        chunk = self._chunks[self._idx]
        self._idx += 1
        return chunk

    def start(self) -> None:
        """No-op start hook."""

    def stop(self) -> None:
        """No-op stop hook."""


class MockStreamingSTT:
    """Configurable streaming STT engine for the voice loop.

    Returns a fixed transcript when the recorded command is finalized.  An empty
    transcript yields ``recognizer_loop:speech.recognition.unknown`` (matching
    the real listener), which is useful for asserting the silent-recording path.

    Args:
        transcript: Text returned by :meth:`transcribe`.  Empty string means "no
            transcription".
        confidence: Confidence score paired with the transcript.
        lang: Language tag reported by the ``lang`` property.
    """

    can_stream: bool = False

    def __init__(
        self,
        transcript: str = "",
        confidence: float = 1.0,
        lang: str = "en-us",
    ) -> None:
        self.transcript: str = transcript
        self.confidence: float = confidence
        self._lang: str = lang
        self.fed_chunks: int = 0

    @property
    def lang(self) -> str:
        """Language tag for transcription."""
        return self._lang

    def stream_start(self, *args: Any, **kwargs: Any) -> None:
        """Begin a streaming transcription session (no-op)."""

    def stream_data(self, chunk: bytes) -> None:
        """Feed audio to the streaming session."""
        self.fed_chunks += 1

    def stream_stop(self) -> str:
        """End the session and return the transcript text."""
        return self.transcript

    def execute(self, audio: Any = None, language: Optional[str] = None) -> Optional[str]:
        """Non-streaming transcription (used by mycroft-classic-listener).

        Args:
            audio: Recorded audio clip (ignored).
            language: Language override (ignored).

        Returns:
            The configured transcript, or ``None`` when empty (so the classic
            consumer emits ``recognizer_loop:speech.recognition.unknown``).
        """
        return self.transcript or None

    def transcribe(
        self, audio: Any = None, lang: Optional[str] = None
    ) -> List[Tuple[str, float]]:
        """Return the configured transcript as ``[(text, confidence)]``.

        Always returns a single ``(text, confidence)`` pair — the text is the
        empty string when no transcript is configured.  This matches the
        ``transcribe(audio)[0][0]`` access pattern used by ovos-simple-listener
        while still letting the dinkum loop's confidence filter run; harness
        callbacks treat an empty transcript as "no utterance".

        Args:
            audio: Ignored (the loop streams audio via :meth:`stream_data`).
            lang: Ignored language override.

        Returns:
            ``[(transcript, confidence)]``.
        """
        return [(self.transcript, self.confidence)]

    def shutdown(self) -> None:
        """Graceful shutdown (no-op)."""


class _MockTransformers:
    """No-op ``AudioTransformersService`` stand-in.

    Args:
        bus: The :class:`FakeBus` the loop is wired to.
    """

    def __init__(self, bus: FakeBus) -> None:
        self.bus: FakeBus = bus
        self.hotword_chunks: List[bytes] = []

    def feed_hotword(self, chunk: bytes) -> None:
        """Record a chunk forwarded after wake-word detection."""
        self.hotword_chunks.append(chunk)

    def feed_audio(self, chunk: bytes) -> None:
        """Consume a non-speech chunk (no-op)."""

    def feed_speech(self, chunk: bytes) -> None:
        """Consume a speech chunk (no-op)."""

    def transform(self, chunk: bytes) -> Tuple[bytes, Dict[str, Any]]:
        """Return the chunk unchanged with empty context."""
        return chunk, {}

    def shutdown(self) -> None:
        """Graceful shutdown (no-op)."""


class MiniHotwordContainer:
    """Controllable hotword container for :class:`MiniVoiceLoop`.

    Implements the subset of ``ovos_dinkum_listener.voice_loop.HotwordContainer``
    that the voice loop relies on, without requiring the wrapped engines to be
    real ``HotWordEngine`` subclasses — so ovoscope's :class:`MockHotWordEngine`
    can drive the loop directly.

    Every registered engine is treated as a **listen word** (it triggers the STT
    stage).  :meth:`update` and :meth:`found` are state-aware so the loop's
    separate hotword-detection branch (``_detect_hot``) does not double-count or
    mis-fire the listen engines.

    The :meth:`verify` chain mirrors the real container's **fail-open**
    semantics: a verifier that returns ``False`` suppresses the detection; a
    verifier that raises is skipped (the detection survives).

    Args:
        ww_instances: Mapping of wake-word name → engine instance (each engine
            implements ``update(chunk)``, ``found_wake_word() -> bool`` and
            ``reset()``).
        verifiers: Optional list of verifier objects with a
            ``verify(ww_audio: bytes) -> bool`` method.

    Attributes:
        state: Listening state, assigned by the voice loop during detection.
        reload_on_failure: Always ``False`` (no engine reloading in tests).
    """

    def __init__(
        self,
        ww_instances: Dict[str, Any],
        verifiers: Optional[List[Any]] = None,
    ) -> None:
        self._engines: Dict[str, Any] = dict(ww_instances)
        self.verifiers: List[Any] = list(verifiers) if verifiers else []
        self.state: Any = None
        self.reload_on_failure: bool = False

    def _active_engines(self) -> Dict[str, Any]:
        """Return the engines relevant to the current listening state.

        All registered engines are listen words, so they are active in the
        ``LISTEN`` state (and when no state has been set yet, as used by direct
        ``_detect_ww`` feeding).  In every other state — ``HOTWORD``,
        ``WAKEUP``, ``RECORDING`` — no engines are active.
        """
        name = getattr(self.state, "name", None)
        if name in (None, "LISTEN"):
            return self._engines
        return {}

    def update(self, chunk: bytes) -> None:
        """Feed *chunk* to the engines active in the current state.

        Args:
            chunk: Raw PCM audio bytes.
        """
        for engine in self._active_engines().values():
            engine.update(chunk)

    def found(self) -> Optional[str]:
        """Return the name of the first active engine reporting a detection.

        A detected engine is reset so a stale ``update_count`` does not re-fire
        the wake word on subsequent chunks (e.g. during the silence tail of a
        file-driven run).

        Returns:
            The wake-word name, or ``None`` if no active engine fired.
        """
        for name, engine in self._active_engines().items():
            if engine.found_wake_word():
                engine.reset()
                return name
        return None

    def get_ww(self, ww: str) -> Dict[str, Any]:
        """Return metadata for wake word *ww*.

        Mirrors the keys ``DinkumVoiceLoop`` and the listener's hotword callback
        read.  The wake word is treated as a listen word (it triggers the STT
        stage), with no confirmation sound.

        Args:
            ww: Wake-word name.

        Returns:
            Metadata dict for the wake word.

        Raises:
            ValueError: If *ww* is not registered.
        """
        if ww not in self._engines:
            raise ValueError(f"Requested ww not defined: {ww}")
        engine = self._engines[ww]
        return {
            "key_phrase": ww,
            "module": getattr(engine, "key_phrase", ww),
            "engine": engine.__class__.__name__,
            "sound": None,
            "listen": True,
            "utterance": None,
            "stt_lang": "en-us",
            "bus_event": None,
            "wakeup": False,
            "stopword": False,
        }

    def verify(self, ww_audio: bytes) -> bool:
        """Run the verifier chain against the wake-word audio.

        Fail-open: only an explicit ``False`` return suppresses the detection;
        a verifier that raises is skipped.

        Args:
            ww_audio: Raw PCM bytes of the audio that triggered the engine.

        Returns:
            ``True`` if every verifier accepts (or none are configured),
            ``False`` if any verifier rejects the audio.
        """
        for verifier in self.verifiers:
            try:
                if not verifier.verify(ww_audio):
                    return False
            except Exception:
                # fail-open: a raising verifier does not discard the detection
                pass
        return True

    def reset(self) -> None:
        """Reset all wrapped engines (called by the loop after a command)."""
        for engine in self._engines.values():
            try:
                engine.reset()
            except Exception:
                pass

    def shutdown(self) -> None:
        """Shut down all wrapped engines gracefully."""
        for engine in self._engines.values():
            try:
                engine.shutdown()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Shared harness base
# ---------------------------------------------------------------------------

class ListenerHarness:
    """Base for in-process listener-service harnesses.

    Owns a :class:`FakeBus`, captures every ``Message`` emitted on it, and
    provides the common ``recognizer_loop:*`` assertion helpers.  Concrete
    backends (:class:`MiniVoiceLoop` for ovos-dinkum-listener,
    ``MiniSimpleListener`` for ovos-simple-listener, ``MiniClassicListener`` for
    mycroft-classic-listener) wire their specific listener to this bus and add a
    drive method (``feed_file`` / ``feed_chunks``).

    Args:
        bus: Optional :class:`FakeBus` to capture on.  Defaults to a fresh bus.
        modernize: When a fresh bus is created, also emit the ovos.* spec topic
            whenever a legacy topic is emitted (legacy producer -> spec
            listener). The listener callbacks emit legacy ``recognizer_loop:*``
            topics; this bridge is what lets a spec-topic subscriber observe
            them. Ignored when *bus* is supplied.
        emit_legacy: When a fresh bus is created, also emit the legacy topic
            whenever an ovos.* spec topic is emitted (spec producer -> legacy
            listener). Set both False to exercise a single namespace with no
            bridging. Ignored when *bus* is supplied.
    """

    def __init__(self, bus: Optional[FakeBus] = None,
                 modernize: bool = True, emit_legacy: bool = True) -> None:
        self.bus: FakeBus = bus if bus is not None else FakeBus(
            modernize=modernize, emit_legacy=emit_legacy)
        self._messages: List[Message] = []
        self._last_messages: List[Message] = []
        self.bus.on("message", self._capture)

    def _capture(self, msg: Any) -> None:
        if isinstance(msg, str):
            try:
                msg = Message.deserialize(msg)
            except Exception:
                return
        self._messages.append(msg)

    # ------------------------------------------------------------------
    # File microphone
    # ------------------------------------------------------------------

    @staticmethod
    def _build_file_mic(
        audio: Union[bytes, str, Path],
        silence_chunks: int,
        chunk_size: int,
    ) -> MockFileMicrophone:
        """Build a :class:`MockFileMicrophone` for *audio*."""
        return MockFileMicrophone(
            audio, chunk_size=chunk_size, silence_chunks=silence_chunks
        )

    # ------------------------------------------------------------------
    # Assertion helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _has(messages: List[Message], msg_type: str) -> bool:
        # Accepts either the legacy or canonical spelling of msg_type — see
        # ovoscope._topic_matches: these helpers filter execute()/feed_*()'s
        # catch-all-captured stream, which can carry either spelling
        # depending on producer vintage.
        return any(_topic_matches(m.msg_type, msg_type) for m in messages)

    def _resolve(self, messages: Optional[List[Message]]) -> List[Message]:
        return messages if messages is not None else self._last_messages

    def assert_record_begin_emitted(
        self, messages: Optional[List[Message]] = None
    ) -> List[Message]:
        """Assert ``recognizer_loop:record_begin`` was emitted.

        Args:
            messages: Messages to check; defaults to the last feed result.

        Returns:
            The checked message list.

        Raises:
            AssertionError: If no record-begin event is present.
        """
        msgs = self._resolve(messages)
        assert self._has(msgs, "recognizer_loop:record_begin"), (
            "Expected 'recognizer_loop:record_begin' but it was not emitted. "
            f"Captured: {[m.msg_type for m in msgs]}"
        )
        return msgs

    def assert_wakeword_detected(
        self, messages: Optional[List[Message]] = None
    ) -> List[Message]:
        """Assert a wake word was detected and recording began.

        Checks for both ``recognizer_loop:wakeword`` and
        ``recognizer_loop:record_begin``.

        Args:
            messages: Messages to check; defaults to the last feed result.

        Returns:
            The checked message list.

        Raises:
            AssertionError: If either expected event is missing.
        """
        msgs = self._resolve(messages)
        captured = [m.msg_type for m in msgs]
        assert self._has(msgs, "recognizer_loop:wakeword"), (
            "Expected 'recognizer_loop:wakeword' but it was not emitted. "
            f"Captured: {captured}"
        )
        assert self._has(msgs, "recognizer_loop:record_begin"), (
            "Expected 'recognizer_loop:record_begin' but it was not emitted. "
            f"Captured: {captured}"
        )
        return msgs

    def assert_wakeword_suppressed(
        self, messages: Optional[List[Message]] = None
    ) -> List[Message]:
        """Assert no wake-word recording was triggered.

        Verifies that neither ``recognizer_loop:wakeword`` nor
        ``recognizer_loop:record_begin`` was emitted.

        Args:
            messages: Messages to check; defaults to the last feed result.

        Returns:
            The checked message list.

        Raises:
            AssertionError: If a wake-word or record-begin event is present.
        """
        msgs = self._resolve(messages)
        captured = [m.msg_type for m in msgs]
        assert not self._has(msgs, "recognizer_loop:record_begin"), (
            "Expected wake word to be suppressed, but "
            f"'recognizer_loop:record_begin' was emitted. Captured: {captured}"
        )
        assert not self._has(msgs, "recognizer_loop:wakeword"), (
            "Expected wake word to be suppressed, but "
            f"'recognizer_loop:wakeword' was emitted. Captured: {captured}"
        )
        return msgs

    def assert_utterance_emitted(
        self,
        utterance: Optional[str] = None,
        messages: Optional[List[Message]] = None,
    ) -> List[Message]:
        """Assert a ``recognizer_loop:utterance`` was emitted.

        Args:
            utterance: When given, also assert this exact text is among the
                emitted utterances.
            messages: Messages to check; defaults to the last feed result.

        Returns:
            The checked message list.

        Raises:
            AssertionError: If no utterance (or the named one) was emitted.
        """
        msgs = self._resolve(messages)
        utts: List[str] = []
        for m in msgs:
            if _topic_matches(m.msg_type, "recognizer_loop:utterance"):
                utts.extend(m.data.get("utterances", []))
        assert utts, (
            "Expected 'recognizer_loop:utterance' but it was not emitted. "
            f"Captured: {[m.msg_type for m in msgs]}"
        )
        if utterance is not None:
            assert utterance in utts, (
                f"Expected utterance {utterance!r} but got: {utts}"
            )
        return msgs

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Release backend resources (overridden by subclasses)."""

    def __enter__(self) -> "ListenerHarness":
        return self

    def __exit__(self, *_: Any) -> None:
        try:
            self.shutdown()
        finally:
            self.detach_capture()

    def detach_capture(self) -> None:
        """Unsubscribe the wildcard ``"message"`` capture handler.

        The bus may be shared with another harness or with the caller. A
        capture handler left behind keeps appending to a dead harness's
        message list — and every later message shows up in ``_messages``.
        """
        bus = getattr(self, "bus", None)
        capture = getattr(self, "_capture", None)
        if bus is None or capture is None:
            return
        try:
            bus.remove("message", capture)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# MiniVoiceLoop
# ---------------------------------------------------------------------------

class MiniVoiceLoop(ListenerHarness):
    """In-process ``DinkumVoiceLoop`` harness for bus-sequence testing.

    Captures every ``Message`` emitted on the ``FakeBus`` as audio flows through
    the loop.  The voice-loop callbacks are wired to emit the same bus events as
    the real listener service (``recognizer_loop:wakeword``,
    ``recognizer_loop:record_begin``, ``recognizer_loop:record_end``,
    ``recognizer_loop:utterance`` / ``…:speech.recognition.unknown``).

    Args:
        voice_loop: A pre-built ``DinkumVoiceLoop`` instance.  When provided, the
            caller owns its plugins/callbacks, which should emit on this
            harness's *bus* to be captured.  When ``None``, a loop is built from
            the mock arguments below.
        ww_instances: Mapping of wake-word name → engine instance.  Defaults to a
            single :class:`MockHotWordEngine` keyed ``"hey_mycroft"``.
        verifiers: Optional verifier objects (``verify(audio) -> bool``) gating
            detection.
        vad_instance: Optional VAD engine (defaults to :class:`MockVADEngine`).
        stt_instance: Optional streaming STT engine (defaults to a
            :class:`MockStreamingSTT` returning no transcript).  Used by
            :meth:`feed_file`.
        bus: Optional :class:`FakeBus` to capture on.  Defaults to a fresh bus.
        modernize: When a fresh bus is created, also emit the ovos.* spec topic
            whenever a legacy topic is emitted (legacy producer -> spec
            listener). Ignored when *bus* is supplied.
        emit_legacy: When a fresh bus is created, also emit the legacy topic
            whenever an ovos.* spec topic is emitted. Set both False to exercise
            a single namespace with no bridging. Ignored when *bus* is supplied.

    Raises:
        RuntimeError: If *voice_loop* is ``None`` and ovos-dinkum-listener is not
            installed.

    Example::

        from ovoscope.voice_loop import MiniVoiceLoop, MockHotWordEngine

        ww = MockHotWordEngine("hey_mycroft", trigger_after=3)
        with MiniVoiceLoop(ww_instances={"hey_mycroft": ww}) as vl:
            msgs = vl.feed_chunks([b"\\x00" * 512] * 5)
            vl.assert_record_begin_emitted(msgs)
    """

    def __init__(
        self,
        voice_loop: Optional[Any] = None,
        *,
        ww_instances: Optional[Dict[str, Any]] = None,
        verifiers: Optional[List[Any]] = None,
        vad_instance: Optional[Any] = None,
        stt_instance: Optional[Any] = None,
        bus: Optional[FakeBus] = None,
        modernize: bool = True,
        emit_legacy: bool = True,
    ) -> None:
        super().__init__(bus, modernize=modernize, emit_legacy=emit_legacy)

        self.hotwords: Optional[MiniHotwordContainer] = None
        if voice_loop is not None:
            self.voice_loop: Any = voice_loop
            hw = getattr(voice_loop, "hotwords", None)
            if isinstance(hw, MiniHotwordContainer):
                self.hotwords = hw
        else:
            self.voice_loop = self._build_voice_loop(
                ww_instances=ww_instances,
                verifiers=verifiers,
                vad_instance=vad_instance,
                stt_instance=stt_instance,
            )

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_voice_loop(
        self,
        ww_instances: Optional[Dict[str, Any]],
        verifiers: Optional[List[Any]],
        vad_instance: Optional[Any],
        stt_instance: Optional[Any],
    ) -> Any:
        """Build a ``DinkumVoiceLoop`` wired to this harness's bus.

        Returns:
            A ready-to-feed ``DinkumVoiceLoop`` instance.

        Raises:
            RuntimeError: If ovos-dinkum-listener is not installed.
        """
        try:
            from ovos_dinkum_listener.voice_loop.voice_loop import DinkumVoiceLoop
        except ImportError as e:
            raise RuntimeError(
                "ovos-dinkum-listener is required to build a MiniVoiceLoop. "
                "Install it with: pip install ovos-dinkum-listener"
            ) from e

        if ww_instances is None:
            ww_instances = {"hey_mycroft": MockHotWordEngine("hey_mycroft")}

        self.hotwords = MiniHotwordContainer(ww_instances, verifiers=verifiers)

        return DinkumVoiceLoop(
            mic=_MockMicrophone(),
            hotwords=self.hotwords,
            stt=stt_instance if stt_instance is not None else MockStreamingSTT(),
            fallback_stt=None,
            vad=vad_instance if vad_instance is not None else MockVADEngine(),
            transformers=_MockTransformers(self.bus),
            wake_callback=self._emit_record_begin,
            record_end_callback=self._emit_record_end,
            text_callback=self._emit_stt_text,
            listenword_audio_callback=self._emit_wakeword,
            hotword_audio_callback=self._emit_wakeword,
        )

    # ------------------------------------------------------------------
    # Bus-emitting callbacks (mirror ovos-dinkum-listener service.py)
    # ------------------------------------------------------------------

    def _emit_record_begin(self) -> None:
        """Emit ``recognizer_loop:record_begin`` (the real wake callback)."""
        self.bus.emit(Message("recognizer_loop:record_begin"))

    def _emit_record_end(self) -> None:
        """Emit ``recognizer_loop:record_end`` (the real record-end callback)."""
        self.bus.emit(Message("recognizer_loop:record_end"))

    def _emit_wakeword(self, audio_bytes: bytes, ww_context: Dict[str, Any]) -> None:
        """Emit ``recognizer_loop:wakeword`` for a detected listen word.

        Mirrors the listen-word branch of the listener's ``_hotword_audio``
        callback so the captured bus sequence matches the real service.

        Args:
            audio_bytes: Raw hotword audio collected by the loop.
            ww_context: Wake-word metadata from :meth:`MiniHotwordContainer.get_ww`.
        """
        payload = dict(ww_context)
        key_phrase = ww_context.get("key_phrase", "")
        payload["utterance"] = key_phrase.replace("_", " ").replace("-", " ")
        context = {
            "client_name": "ovos_dinkum_listener",
            "source": "audio",
            "destination": ["skills"],
        }
        self.bus.emit(Message("recognizer_loop:wakeword", payload, context))

    def _emit_stt_text(
        self, transcripts: List[Tuple[str, float]], stt_context: Dict[str, Any]
    ) -> None:
        """Emit the STT result (mirrors the listener's ``_stt_text`` callback).

        Emits ``recognizer_loop:utterance`` for a non-empty transcript, or
        ``recognizer_loop:speech.recognition.unknown`` when transcription is
        empty.

        Args:
            transcripts: List of ``(text, confidence)`` from the STT engine.
            stt_context: Context dict accumulated by the loop.
        """
        utts = [t[0] for t in transcripts if t[0]] if transcripts else []
        if utts:
            lang = stt_context.get("lang") or "en-us"
            self.bus.emit(Message(
                "recognizer_loop:utterance",
                {"utterances": utts, "lang": lang},
                stt_context,
            ))
        else:
            self.bus.emit(Message(
                "recognizer_loop:speech.recognition.unknown",
                context=stt_context,
            ))

    # ------------------------------------------------------------------
    # Feeding
    # ------------------------------------------------------------------

    def feed_chunks(self, chunks: List[bytes]) -> List[Message]:
        """Feed PCM *chunks* through the voice loop's wake-word detection.

        Each chunk is passed to ``DinkumVoiceLoop._detect_ww``; all bus messages
        emitted as side-effects are collected and returned.  This drives only
        the wake-word / verifier stage — use :meth:`feed_file` to run the full
        state machine.

        Args:
            chunks: Ordered list of raw PCM audio frames.

        Returns:
            The list of :class:`Message` objects emitted during this call.
        """
        self._messages.clear()
        for chunk in chunks:
            self.voice_loop._detect_ww(chunk)
        self._last_messages = list(self._messages)
        return list(self._messages)

    def feed_file(
        self,
        audio: Union[bytes, str, Path],
        *,
        silence_tail_chunks: int = 25,
        chunk_size: int = 2048,
    ) -> List[Message]:
        """Run the full ``DinkumVoiceLoop`` state machine over an audio file.

        Swaps in a :class:`MockFileMicrophone` that streams *audio* through the
        loop, drives ``start()`` + ``run()`` to completion, and returns every
        bus message emitted along the way.  A tail of silent frames is appended
        so a silence-based VAD can end the command and the loop can finalize the
        utterance.

        Args:
            audio: Path to a ``.wav`` file, WAV bytes, or raw PCM bytes.
            silence_tail_chunks: Trailing silent frames appended after the audio
                (gives the VAD a chance to detect end-of-command).
            chunk_size: Bytes per microphone read.

        Returns:
            The list of :class:`Message` objects emitted during the run.
        """
        mic = MockFileMicrophone(
            audio,
            chunk_size=chunk_size,
            silence_chunks=silence_tail_chunks,
        )
        mic.on_exhausted = self.voice_loop.stop
        self.voice_loop.mic = mic

        self._messages.clear()
        self.voice_loop.start()
        self.voice_loop.run()
        self._last_messages = list(self._messages)
        return list(self._messages)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Shut down the hotword container and detach the bus capture handler.

        Callers that use the harness without the context manager only ever
        call shutdown(); if that does not detach the capture handler, the dead
        harness keeps collecting every message on a shared bus.
        """
        try:
            if self.hotwords is not None:
                self.hotwords.shutdown()
        finally:
            self.detach_capture()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_mini_voice_loop(
    ww_instances: Optional[Dict[str, Any]] = None,
    verifiers: Optional[List[Any]] = None,
    vad_instance: Optional[Any] = None,
    stt_instance: Optional[Any] = None,
    bus: Optional[FakeBus] = None,
    modernize: bool = True,
    emit_legacy: bool = True,
) -> MiniVoiceLoop:
    """Factory: create a ready-to-feed :class:`MiniVoiceLoop`.

    Args:
        ww_instances: Mapping of wake-word name → engine instance.  Defaults to a
            single :class:`MockHotWordEngine` keyed ``"hey_mycroft"``.
        verifiers: Optional verifier objects gating detection.
        vad_instance: Optional VAD engine (defaults to :class:`MockVADEngine`).
        stt_instance: Optional streaming STT engine (defaults to
            :class:`MockStreamingSTT`).
        bus: Optional :class:`FakeBus` to capture on.
        modernize: When a fresh bus is created, also emit the ovos.* spec topic
            whenever a legacy topic is emitted (legacy producer -> spec
            listener). Ignored when *bus* is supplied.
        emit_legacy: When a fresh bus is created, also emit the legacy topic
            whenever an ovos.* spec topic is emitted. Set both False to exercise
            a single namespace with no bridging. Ignored when *bus* is supplied.

    Returns:
        A fully initialised :class:`MiniVoiceLoop`.

    Example::

        from ovoscope.voice_loop import get_mini_voice_loop, MockHotWordEngine

        vl = get_mini_voice_loop(
            ww_instances={"hey_mycroft": MockHotWordEngine(trigger_after=3)},
        )
        try:
            vl.assert_record_begin_emitted(vl.feed_chunks([b"\\x00" * 512] * 3))
        finally:
            vl.shutdown()
    """
    return MiniVoiceLoop(
        ww_instances=ww_instances,
        verifiers=verifiers,
        vad_instance=vad_instance,
        stt_instance=stt_instance,
        bus=bus,
        modernize=modernize,
        emit_legacy=emit_legacy,
    )


# ---------------------------------------------------------------------------
# Declarative test helper
# ---------------------------------------------------------------------------

@dataclass
class VoiceLoopTest:
    """Declarative wake-word → bus-sequence test for the voice loop.

    Drives a :class:`MiniVoiceLoop` and asserts whether the wake-word recording
    sequence was triggered.  Feeds PCM frames via ``feed_chunks`` by default, or
    an audio file via ``feed_file`` when *audio_file* is set.

    Example — verifier gate::

        from unittest.mock import Mock
        from ovoscope.voice_loop import VoiceLoopTest, MockHotWordEngine

        accepting = Mock(); accepting.verify.return_value = True
        VoiceLoopTest(
            ww_instances={"hey_mycroft": MockHotWordEngine(trigger_after=3)},
            verifiers=[accepting],
            audio_chunks=[b"\\x00" * 512] * 5,
            expect_record_begin=True,
        ).execute()

    Example — full loop from a file::

        from ovoscope.voice_loop import VoiceLoopTest, MockStreamingSTT

        VoiceLoopTest(
            audio_file="command.wav",
            stt_instance=MockStreamingSTT(transcript="what time is it"),
            expect_utterance="what time is it",
        ).execute()
    """

    ww_instances: Optional[Dict[str, Any]] = None
    """Mapping of wake-word name → engine instance."""

    verifiers: Optional[List[Any]] = None
    """Verifier objects (``verify(audio) -> bool``) gating detection."""

    vad_instance: Optional[Any] = None
    """Optional VAD engine (defaults to :class:`MockVADEngine`)."""

    stt_instance: Optional[Any] = None
    """Optional streaming STT engine (defaults to :class:`MockStreamingSTT`)."""

    audio_chunks: List[bytes] = field(
        default_factory=lambda: [b"\x00" * 512] * 5
    )
    """PCM frames fed via ``feed_chunks`` (ignored when *audio_file* is set)."""

    audio_file: Optional[Union[bytes, str, Path]] = None
    """When set, run the full loop over this audio via ``feed_file``."""

    expect_record_begin: bool = True
    """Assert ``recognizer_loop:record_begin`` was emitted (``True``) or
    suppressed (``False``)."""

    expect_utterance: Optional[str] = None
    """When set, assert this utterance text was emitted (implies full-loop)."""

    def execute(self) -> List[Message]:
        """Run the test and assert the configured expectations.

        Returns:
            The captured :class:`Message` list.

        Raises:
            AssertionError: If an expectation is not met.
        """
        vl = MiniVoiceLoop(
            ww_instances=self.ww_instances,
            verifiers=self.verifiers,
            vad_instance=self.vad_instance,
            stt_instance=self.stt_instance,
        )
        try:
            if self.audio_file is not None:
                messages = vl.feed_file(self.audio_file)
            else:
                messages = vl.feed_chunks(self.audio_chunks)

            if self.expect_record_begin:
                vl.assert_record_begin_emitted(messages)
            else:
                vl.assert_wakeword_suppressed(messages)

            if self.expect_utterance is not None:
                vl.assert_utterance_emitted(self.expect_utterance, messages)
            return messages
        finally:
            vl.shutdown()
