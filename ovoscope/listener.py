# Copyright 2024 OpenVoiceOS
# Licensed under the Apache License, Version 2.0
"""MiniListener — in-process listener pipeline for ovoscope.

Wraps ``AudioTransformersService`` (and optionally STT, VAD, and WakeWord
plugins) on a ``FakeBus`` so that the full listener pipeline can be tested
end-to-end without a real microphone or a running ``ovos-dinkum-listener``
process.

Usage patterns:

**1. Audio transformer testing** (e.g. ggwave)::

    from ovoscope.listener import get_mini_listener
    from ovos_audio_transformer_plugin_ggwave import GGWavePlugin
    from unittest.mock import MagicMock, patch

    plugin = GGWavePlugin(config={"start_enabled": True})
    listener = get_mini_listener(
        plugin_instances={"ovos-audio-transformer-plugin-ggwave": plugin}
    )
    msgs = listener.feed_audio(b"\\x00" * 1024)
    assert any(m.msg_type == "recognizer_loop:utterance" for m in msgs)
    listener.shutdown()

**2. Full pipeline testing** (audio transformers → STT)::

    from ovoscope.listener import get_mini_listener
    from unittest.mock import MagicMock

    stt = MagicMock()
    stt.execute.return_value = "ask not what your country can do for you"

    listener = get_mini_listener(stt_instance=stt)
    msgs = listener.listen("path/to/jfk.wav", language="en-us")
    assert any(m.msg_type == "recognizer_loop:utterance" for m in msgs)
    listener.shutdown()

**3. VAD testing** — detect silence vs. speech in audio chunks::

    from ovoscope.listener import get_mini_listener, MockVADEngine

    vad = MockVADEngine()
    listener = get_mini_listener(vad_instance=vad)

    assert listener.is_silence(b"\\x00" * 1024)           # silent chunk
    assert not listener.is_silence(b"\\x01\\x02" * 512)   # non-silent chunk
    speech = listener.extract_speech(b"\\x00" * 512 + b"\\x01" * 512)
    listener.shutdown()

**4. Wake-word testing** — detect a hotword in a stream of audio chunks::

    from ovoscope.listener import get_mini_listener, MockHotWordEngine

    ww = MockHotWordEngine(key_phrase="hey_mycroft", trigger_after=3)
    listener = get_mini_listener(ww_instances={"hey_mycroft": ww})

    found, frame = listener.scan_for_wakeword([b"\\x00" * 512] * 5)
    assert found
    assert frame == 2  # zero-indexed frame that triggered
    listener.shutdown()
"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from ovos_bus_client.message import Message
from ovos_utils.fakebus import FakeBus
from ovos_utils.log import LOG


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _wav_to_audio_data(audio: Union[bytes, str, Path],
                       sample_rate: int = 16000,
                       sample_width: int = 2) -> Any:
    """Convert WAV bytes or a WAV file path to an ``AudioData`` object.

    Uses ``AudioData.from_file()`` when a file path is given, which handles
    WAV/AIFF/FLAC automatically.  For raw bytes, parses the WAV header via
    the ``wave`` stdlib module to extract sample_rate and sample_width.

    Args:
        audio: Raw WAV bytes **or** a path to a WAV/AIFF/FLAC file.
        sample_rate: Fallback sample rate when the WAV header cannot be parsed.
        sample_width: Fallback sample width (bytes) when header cannot be parsed.

    Returns:
        ``AudioData(frame_data, sample_rate, sample_width)``
    """
    from ovos_plugin_manager.utils.audio import AudioData

    if isinstance(audio, (str, Path)):
        return AudioData.from_file(str(audio))

    try:
        with wave.open(io.BytesIO(audio)) as wf:
            sample_rate = wf.getframerate()
            sample_width = wf.getsampwidth()
            frame_data = wf.readframes(wf.getnframes())
    except Exception as exc:
        # Not a WAV container — treat the bytes as raw PCM at the caller's
        # sample rate. Say so: a truncated or corrupt WAV lands here too, and
        # silently reinterpreting its header bytes as audio produces garbage
        # that is very hard to trace back to this line.
        LOG.debug(f"ovoscope: could not parse audio as WAV ({exc}); "
                  f"treating the bytes as raw PCM")
        frame_data = audio

    return AudioData(frame_data, sample_rate, sample_width)


# ---------------------------------------------------------------------------
# Mock plugin implementations for testing without real hardware
# ---------------------------------------------------------------------------

class MockVADEngine:
    """No-op VAD engine for testing.

    Classifies a chunk as silence when all bytes are zero; non-zero bytes
    are treated as speech.  This matches the behaviour of real silence
    produced by ``SILENT_WAV`` and similar helpers throughout ovoscope.

    ``extract_speech`` returns only the non-silent portions of the input,
    split on zero-byte boundaries.

    Args:
        sample_rate: Audio sample rate in Hz (default 16 000).
        config: Optional plugin config dict forwarded to the base class.

    Attributes:
        chunks_processed: Running count of chunks passed to :meth:`is_silence`.

    Example::

        vad = MockVADEngine()
        assert vad.is_silence(b"\\x00" * 512)
        assert not vad.is_silence(b"\\x01\\x02" * 256)
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.sample_rate: int = sample_rate
        self.config: Dict[str, Any] = config or {}
        self.chunks_processed: int = 0

    def is_silence(self, chunk: bytes) -> bool:
        """Return ``True`` if every byte in *chunk* is zero.

        Args:
            chunk: Raw PCM audio bytes.

        Returns:
            ``True`` when the chunk is silent, ``False`` otherwise.
        """
        self.chunks_processed += 1
        return chunk == b"\x00" * len(chunk)

    def extract_speech(self, audio: bytes) -> bytes:
        """Return only the non-silent (non-zero) bytes from *audio*.

        Splits *audio* into ``frame_size``-byte chunks, discards those
        that :meth:`is_silence` classifies as silent, and concatenates
        the remaining bytes.

        Args:
            audio: Raw PCM audio bytes to process.

        Returns:
            Bytes containing only the speech portions of *audio*.
        """
        frame_size = 960  # 30 ms at 16 kHz, 16-bit
        speech = bytearray()
        for i in range(0, len(audio), frame_size):
            chunk = audio[i:i + frame_size]
            if not self.is_silence(chunk):
                speech.extend(chunk)
        return bytes(speech)

    def reset(self) -> None:
        """Reset internal state (chunks counter)."""
        self.chunks_processed = 0


class MockHotWordEngine:
    """No-op wake-word engine for testing.

    Fires after receiving *trigger_after* calls to :meth:`update`, then
    auto-resets so it can be used in loop-based scanning tests.

    Args:
        key_phrase: Wake-word name (default ``"hey_mycroft"``).
        trigger_after: Number of :meth:`update` calls before detection
            fires (default 1).
        config: Optional config dict.

    Attributes:
        update_count: Running count of :meth:`update` calls since last reset.

    Example::

        ww = MockHotWordEngine("hey_mycroft", trigger_after=3)
        for _ in range(3):
            ww.update(b"\\x00" * 512)
        assert ww.found_wake_word()
        assert not ww.found_wake_word()  # auto-reset
    """

    def __init__(
        self,
        key_phrase: str = "hey_mycroft",
        trigger_after: int = 1,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.key_phrase: str = key_phrase.lower().replace(" ", "_")
        self.trigger_after: int = trigger_after
        self.config: Dict[str, Any] = config or {}
        self.update_count: int = 0
        self._found: bool = False

    def update(self, chunk: bytes) -> None:
        """Feed an audio chunk to the engine.

        Args:
            chunk: Raw PCM audio bytes.
        """
        self.update_count += 1
        if self.update_count >= self.trigger_after:
            self._found = True

    def found_wake_word(self) -> bool:
        """Return ``True`` if the wake word was detected; auto-resets state.

        Returns:
            ``True`` on the first call after detection, ``False`` thereafter.
        """
        found = self._found
        self._found = False
        return found

    def reset(self) -> None:
        """Reset detection state and chunk counter."""
        self._found = False
        self.update_count = 0

    def stop(self) -> None:
        """Graceful shutdown (no-op for mock)."""

    def shutdown(self) -> None:
        """Alias for :meth:`stop`."""
        self.stop()


# ---------------------------------------------------------------------------
# MiniListener
# ---------------------------------------------------------------------------

class MiniListener:
    """In-process listener pipeline for integration testing.

    Wraps ``AudioTransformersService`` on a ``FakeBus`` so transformer
    plugins, STT, VAD, and wake-word engines can be exercised without real
    hardware or a running ``ovos-dinkum-listener`` process.

    All ``Message`` objects emitted on the bus during any feed / transform /
    listen call are captured and returned by the corresponding method.

    Args:
        config: Full OVOS config dict. Must contain at minimum::

            {"listener": {"audio_transformers": {}}}

        plugin_instances: Optional mapping of plugin name →
            already-instantiated audio transformer plugin object.
        stt_instance: Optional STT plugin object with an
            ``execute(audio_data, language) -> str`` method.
        vad_instance: Optional VAD engine object implementing
            ``is_silence(chunk) -> bool`` and
            ``extract_speech(audio) -> bytes``.
        ww_instances: Optional mapping of hotword name →
            :class:`HotWordEngine` (or mock) instance.  Multiple wake-word
            engines can be registered simultaneously.
        modernize: FakeBus also emits the ovos.* spec topic when a legacy
            topic is emitted (legacy producer -> spec listener). The listener
            pipeline emits legacy ``recognizer_loop:*`` topics; this bridge is
            what lets a spec-topic subscriber observe them.
        emit_legacy: FakeBus also emits the legacy topic when an ovos.* spec
            topic is emitted (spec producer -> legacy listener). Set both False
            to exercise a single namespace with no bridging.

    Example::

        from ovoscope.listener import MiniListener, MockVADEngine, MockHotWordEngine

        vad = MockVADEngine()
        ww = MockHotWordEngine("hey_mycroft", trigger_after=2)
        listener = MiniListener(
            config={"listener": {"audio_transformers": {}}},
            vad_instance=vad,
            ww_instances={"hey_mycroft": ww},
        )
        assert listener.is_silence(b"\\x00" * 512)
        found, frame = listener.scan_for_wakeword([b"\\x00" * 512] * 3)
        assert found
        listener.shutdown()
    """

    def __init__(
        self,
        config: Dict[str, Any],
        plugin_instances: Optional[Dict[str, Any]] = None,
        stt_instance: Optional[Any] = None,
        vad_instance: Optional[Any] = None,
        ww_instances: Optional[Dict[str, Any]] = None,
        modernize: bool = True,
        emit_legacy: bool = True,
    ) -> None:
        self.bus: FakeBus = FakeBus(modernize=modernize,
                                    emit_legacy=emit_legacy)
        self._messages: List[Message] = []
        self._stt_instance: Optional[Any] = stt_instance
        self._vad: Optional[Any] = vad_instance
        self._ww: Dict[str, Any] = dict(ww_instances) if ww_instances else {}

        def _capture(msg: Any) -> None:
            if isinstance(msg, str):
                try:
                    msg = Message.deserialize(msg)
                except Exception:
                    return
            self._messages.append(msg)

        # Kept on the instance so shutdown() can unsubscribe it — a capture
        # handler left on a shared bus keeps feeding a dead harness.
        self._capture = _capture
        self.bus.on("message", _capture)

        # Narrow the guard to the IMPORT: a constructor failure is a real bug
        # in AudioTransformersService (or in the config passed to it) and must
        # propagate, not masquerade as "ovos-dinkum-listener is not installed".
        try:
            from ovos_dinkum_listener.transformers import AudioTransformersService
        except ImportError:
            AudioTransformersService = None
        if AudioTransformersService is None:
            self.transformers: Optional[Any] = None
        else:
            self.transformers = AudioTransformersService(self.bus, config)

        if plugin_instances:
            if self.transformers is None:
                raise RuntimeError(
                    "ovos-dinkum-listener is required to use plugin_instances. "
                    "Install it with: pip install ovos-dinkum-listener"
                )
            for name, plugin in plugin_instances.items():
                plugin.bind(self.bus)
                self.transformers.loaded_plugins[name] = plugin

    # ------------------------------------------------------------------
    # Audio transformer feed methods (unchanged from original)
    # ------------------------------------------------------------------

    def feed_audio(self, chunk: bytes) -> List[Message]:
        """Feed non-speech audio to all loaded transformer plugins.

        Args:
            chunk: Raw PCM bytes.

        Returns:
            List of ``Message`` objects emitted on the bus during this call.
        """
        if self.transformers is None:
            raise RuntimeError(
                "ovos-dinkum-listener is required for feed_audio. "
                "Install it with: pip install ovos-dinkum-listener"
            )
        self._messages.clear()
        self.transformers.feed_audio(chunk)
        return list(self._messages)

    def feed_speech(self, chunk: bytes) -> List[Message]:
        """Feed speech audio to all loaded transformer plugins.

        Args:
            chunk: Raw PCM bytes.

        Returns:
            List of ``Message`` objects emitted on the bus during this call.
        """
        if self.transformers is None:
            raise RuntimeError(
                "ovos-dinkum-listener is required for feed_speech. "
                "Install it with: pip install ovos-dinkum-listener"
            )
        self._messages.clear()
        self.transformers.feed_speech(chunk)
        return list(self._messages)

    def transform(self, chunk: bytes) -> Tuple[bytes, dict, List[Message]]:
        """Run the full transform pipeline.

        Args:
            chunk: Raw PCM bytes to transform.

        Returns:
            Tuple of ``(transformed_audio, context_dict, emitted_messages)``.
        """
        if self.transformers is None:
            raise RuntimeError(
                "ovos-dinkum-listener is required for transform. "
                "Install it with: pip install ovos-dinkum-listener"
            )
        self._messages.clear()
        audio, ctx = self.transformers.transform(chunk)
        return audio, ctx, list(self._messages)

    def feed_audio_stream(
        self,
        chunks: Union[bytes, List[bytes]],
        feed: str = "feed_audio",
        chunk_size: int = 2048,
    ) -> List[Message]:
        """Stream a sequence of audio frames and aggregate emitted messages.

        Unlike :meth:`feed_audio` / :meth:`feed_speech`, which clear the
        capture buffer on every call, this feeds each frame in order and keeps
        every message emitted across the **whole** stream.  This is required
        for transformers whose decoder only fires after accumulating many
        frames of audio (e.g. ggwave data-over-sound).

        Args:
            chunks: Either a flat ``bytes`` object (split into *chunk_size*
                frames internally) or a pre-segmented ``List[bytes]`` of frames.
            feed: Which transformer feed to drive per frame —
                ``"feed_audio"`` (non-speech) or ``"feed_speech"``.
            chunk_size: Bytes per frame when *chunks* is a flat ``bytes``
                object (ignored when *chunks* is already a list).

        Returns:
            All ``Message`` objects emitted on the bus across every frame.

        Raises:
            RuntimeError: If ``ovos-dinkum-listener`` is not installed.
            ValueError: If *feed* is not a recognised feed method.
        """
        if self.transformers is None:
            raise RuntimeError(
                "ovos-dinkum-listener is required for feed_audio_stream. "
                "Install it with: pip install ovos-dinkum-listener"
            )
        if feed not in ("feed_audio", "feed_speech"):
            raise ValueError(
                f"feed must be 'feed_audio' or 'feed_speech', got {feed!r}"
            )

        if isinstance(chunks, bytes):
            frames = [
                chunks[i:i + chunk_size]
                for i in range(0, len(chunks), chunk_size)
            ]
        else:
            frames = list(chunks)

        feeder = getattr(self.transformers, feed)
        self._messages.clear()
        for frame in frames:
            feeder(frame)
        return list(self._messages)

    def listen(
        self,
        audio: Union[bytes, str, Path],
        language: str = "en-us",
        stt_instance: Optional[Any] = None,
        sample_rate: int = 16000,
        sample_width: int = 2,
    ) -> List[Message]:
        """Full pipeline: audio → transformers → STT → ``recognizer_loop:utterance``.

        Args:
            audio: Raw WAV/PCM bytes **or** a path to a ``.wav`` file.
            language: BCP-47 language code forwarded to the STT plugin.
            stt_instance: Optional STT plugin overriding the constructor's value.
            sample_rate: Fallback sample rate for raw PCM input.
            sample_width: Fallback sample width in bytes for raw PCM input.

        Returns:
            All ``Message`` objects emitted on the FakeBus during this call.
        """
        self._messages.clear()

        if isinstance(audio, (str, Path)):
            with open(audio, "rb") as fh:
                audio_bytes: bytes = fh.read()
        else:
            audio_bytes = audio

        if self.transformers is None:
            raise RuntimeError(
                "ovos-dinkum-listener is required for listen. "
                "Install it with: pip install ovos-dinkum-listener"
            )
        transformed, ctx = self.transformers.transform(audio_bytes)

        stt_instance = stt_instance or self._stt_instance
        if stt_instance is not None:
            audio_data = _wav_to_audio_data(
                transformed, sample_rate=sample_rate, sample_width=sample_width
            )
            raw = stt_instance.execute(audio_data, language)
            transcript = raw.strip() if isinstance(raw, str) else ""

            if transcript:
                self.bus.emit(Message(
                    "recognizer_loop:utterance",
                    {"utterances": [transcript], "lang": language},
                    {**ctx, "destination": ["skills"]},
                ))

        return list(self._messages)

    # ------------------------------------------------------------------
    # VAD methods
    # ------------------------------------------------------------------

    def is_silence(self, chunk: bytes) -> bool:
        """Check whether *chunk* is silence using the loaded VAD engine.

        Delegates to the VAD engine's ``is_silence()`` method.  Raises
        ``RuntimeError`` if no VAD engine was provided.

        Args:
            chunk: Raw PCM audio bytes (one frame worth).

        Returns:
            ``True`` if the chunk is classified as silent.

        Raises:
            RuntimeError: If no VAD engine is loaded.
        """
        if self._vad is None:
            raise RuntimeError(
                "No VAD engine loaded. Pass vad_instance= or vad_plugin= to "
                "get_mini_listener()."
            )
        return self._vad.is_silence(chunk)

    def extract_speech(self, audio: bytes) -> bytes:
        """Strip silence from *audio* and return only speech frames.

        Delegates to the VAD engine's ``extract_speech()`` method.  Raises
        ``RuntimeError`` if no VAD engine was provided.

        Args:
            audio: Raw PCM audio bytes (may span many frames).

        Returns:
            Bytes containing only the speech-classified portions of *audio*.

        Raises:
            RuntimeError: If no VAD engine is loaded.
        """
        if self._vad is None:
            raise RuntimeError(
                "No VAD engine loaded. Pass vad_instance= or vad_plugin= to "
                "get_mini_listener()."
            )
        return self._vad.extract_speech(audio)

    # ------------------------------------------------------------------
    # Wake-word methods
    # ------------------------------------------------------------------

    def detect_wakeword(
        self, chunk: bytes, ww_name: Optional[str] = None
    ) -> bool:
        """Feed *chunk* to wake-word engine(s) and return whether any fired.

        Updates all registered wake-word engines (or just the one named by
        *ww_name*) with the audio chunk, then checks for detection.

        Args:
            chunk: Raw PCM audio bytes (one frame worth).
            ww_name: If provided, only the engine with this name is updated
                and checked.  When ``None``, all registered engines are used.

        Returns:
            ``True`` if any (or the named) engine reports a detection.

        Raises:
            RuntimeError: If no wake-word engines are loaded.
            KeyError: If *ww_name* is provided but not registered.
        """
        if not self._ww:
            raise RuntimeError(
                "No wake-word engines loaded. Pass ww_instances= or ww_plugin= "
                "to get_mini_listener()."
            )

        engines: Dict[str, Any]
        if ww_name is not None:
            if ww_name not in self._ww:
                raise KeyError(
                    f"Wake-word engine {ww_name!r} not registered. "
                    f"Available: {list(self._ww)}"
                )
            engines = {ww_name: self._ww[ww_name]}
        else:
            engines = self._ww

        for engine in engines.values():
            engine.update(chunk)

        # Materialise every result BEFORE reducing: found_wake_word() is a
        # destructive read on most engines (it consumes the latch), so the
        # short-circuit in any() would leave later engines still latched and
        # make the NEXT call report a stale detection.
        results = [e.found_wake_word() for e in engines.values()]
        return any(results)

    def scan_for_wakeword(
        self,
        audio: Union[bytes, List[bytes]],
        frame_size: int = 2048,
        ww_name: Optional[str] = None,
    ) -> Tuple[bool, Optional[int]]:
        """Scan *audio* for a wake-word and return the detection result.

        Feeds audio to the engine(s) frame by frame.  Stops on first
        detection.

        Args:
            audio: Either a ``bytes`` object (split into *frame_size* chunks
                internally) or a pre-segmented ``List[bytes]`` of frames.
            frame_size: Bytes per frame when *audio* is a flat ``bytes``
                object (ignored when *audio* is already a list).
            ww_name: Optional name of a specific engine to use.

        Returns:
            ``(detected, frame_index)`` where *frame_index* is the
            zero-based index of the frame that triggered detection, or
            ``(False, None)`` when no wake word was found.

        Raises:
            RuntimeError: If no wake-word engines are loaded.
        """
        if not self._ww:
            raise RuntimeError(
                "No wake-word engines loaded. Pass ww_instances= or ww_plugin= "
                "to get_mini_listener()."
            )

        if isinstance(audio, bytes):
            frames = [
                audio[i:i + frame_size]
                for i in range(0, len(audio), frame_size)
            ]
        else:
            frames = list(audio)

        engines: Dict[str, Any]
        if ww_name is not None:
            if ww_name not in self._ww:
                raise KeyError(
                    f"Wake-word engine {ww_name!r} not registered. "
                    f"Available: {list(self._ww)}"
                )
            engines = {ww_name: self._ww[ww_name]}
        else:
            engines = self._ww

        for idx, frame in enumerate(frames):
            for engine in engines.values():
                engine.update(frame)
            # Read every engine's latch (destructive) before reducing — see
            # feed_chunk above.
            results = [e.found_wake_word() for e in engines.values()]
            if any(results):
                return True, idx

        return False, None

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Shut down all loaded transformer and wake-word plugins gracefully.

        Also detaches the wildcard ``"message"`` capture handler so a shared
        bus stops feeding this harness after it is gone.
        """
        try:
            if self.transformers is not None:
                self.transformers.shutdown()
            for engine in self._ww.values():
                try:
                    engine.shutdown()
                except Exception:
                    pass
        finally:
            capture = getattr(self, "_capture", None)
            if capture is not None:
                try:
                    self.bus.remove("message", capture)
                except Exception:
                    pass
                self._capture = None


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------

def get_mini_listener(
    transformer_plugins: Optional[List[str]] = None,
    config: Optional[Dict[str, Any]] = None,
    plugin_instances: Optional[Dict[str, Any]] = None,
    stt_instance: Optional[Any] = None,
    vad_plugin: Optional[str] = None,
    vad_instance: Optional[Any] = None,
    ww_plugin: Optional[str] = None,
    ww_instances: Optional[Dict[str, Any]] = None,
    modernize: bool = True,
    emit_legacy: bool = True,
) -> MiniListener:
    """Factory: create a ready-to-use :class:`MiniListener`.

    Supports four orthogonal capabilities — transformers, STT, VAD, and
    wake-words — each configurable via either an OPM plugin name (for
    discovery) or a pre-instantiated object (for testing with mocks or
    injected configs).

    Args:
        transformer_plugins: Plugin names to enable via OPM discovery.
            Ignored when *config* is provided.
        config: Full config dict.  When provided, *transformer_plugins* is
            ignored.  Must include the ``listener.audio_transformers`` key.
        plugin_instances: Pre-instantiated audio-transformer plugin objects
            keyed by plugin name.
        stt_instance: STT plugin object with an
            ``execute(audio_data, language) -> str`` method.
        vad_plugin: OPM VAD plugin name to load (e.g.
            ``"ovos-vad-plugin-silero"``).  Ignored when *vad_instance*
            is provided.
        vad_instance: Pre-instantiated VAD engine object (e.g.
            :class:`MockVADEngine`).  Takes precedence over *vad_plugin*.
        ww_plugin: OPM wake-word plugin name to load (e.g.
            ``"ovos-ww-plugin-openWakeWord"``).  Creates a single engine
            keyed as ``"hey_mycroft"``.  Ignored when *ww_instances* is
            provided.
        ww_instances: Mapping of hotword name → engine instance.  Supports
            multiple simultaneous wake-word engines.  Takes precedence over
            *ww_plugin*.
        modernize: FakeBus also emits the ovos.* spec topic when a legacy topic
            is emitted (legacy producer -> spec listener).
        emit_legacy: FakeBus also emits the legacy topic when an ovos.* spec
            topic is emitted (spec producer -> legacy listener). Set both False
            to exercise a single namespace with no bridging.

    Returns:
        A fully initialised :class:`MiniListener` ready to receive audio.

    Example — OPM discovery::

        listener = get_mini_listener(
            transformer_plugins=["ovos-audio-transformer-plugin-ggwave"],
            vad_plugin="ovos-vad-plugin-silero",
            ww_plugin="ovos-ww-plugin-openWakeWord",
        )

    Example — direct injection with mocks::

        from ovoscope.listener import MockVADEngine, MockHotWordEngine

        listener = get_mini_listener(
            vad_instance=MockVADEngine(),
            ww_instances={"hey_mycroft": MockHotWordEngine(trigger_after=2)},
        )
    """
    if config is None:
        config = {
            "listener": {
                "audio_transformers": {
                    name: {} for name in (transformer_plugins or [])
                }
            }
        }

    # --- VAD resolution ---
    resolved_vad: Optional[Any] = vad_instance
    if resolved_vad is None and vad_plugin is not None:
        from ovos_plugin_manager.vad import OVOSVADFactory
        vad_config = {"listener": {"VAD": {"module": vad_plugin}}}
        resolved_vad = OVOSVADFactory.create(vad_config)

    # --- WakeWord resolution ---
    resolved_ww: Optional[Dict[str, Any]] = ww_instances
    if resolved_ww is None and ww_plugin is not None:
        from ovos_plugin_manager.wakewords import OVOSWakeWordFactory
        engine = OVOSWakeWordFactory.create_hotword(
            "hey_mycroft",
            config={"hotwords": {"hey_mycroft": {"module": ww_plugin}}},
        )
        resolved_ww = {"hey_mycroft": engine}

    return MiniListener(
        config,
        plugin_instances=plugin_instances,
        stt_instance=stt_instance,
        vad_instance=resolved_vad,
        ww_instances=resolved_ww,
        modernize=modernize,
        emit_legacy=emit_legacy,
    )


# ---------------------------------------------------------------------------
# Declarative test helpers
# ---------------------------------------------------------------------------

@dataclass
class ListenerTest:
    """Declarative end-to-end test for audio transformer plugins.

    Mirrors :class:`ovoscope.End2EndTest` for the listener pipeline.

    Example::

        from ovoscope.listener import ListenerTest
        from ovos_audio_transformer_plugin_ggwave import GGWavePlugin
        from ovos_bus_client.message import Message

        plugin = GGWavePlugin(config={"start_enabled": True})
        test = ListenerTest(
            plugin_instances={"ovos-audio-transformer-plugin-ggwave": plugin},
            audio_input=b"\\x00" * 1024,
            expected_types=["recognizer_loop:utterance"],
        )
        test.execute()
    """

    plugin_instances: Dict[str, Any] = field(default_factory=dict)
    """Pre-instantiated plugin objects, keyed by plugin name."""

    transformer_plugins: List[str] = field(default_factory=list)
    """Plugin names to load via OPM discovery (alternative to plugin_instances)."""

    config: Dict[str, Any] = field(default_factory=dict)
    """Full OVOS config dict.  If empty, built from transformer_plugins."""

    audio_input: bytes = field(default=b"\x00" * 1024)
    """Raw audio bytes to inject into the pipeline."""

    feed_method: str = "feed_audio"
    """Which feed method to call: ``"feed_audio"``, ``"feed_speech"``,
    ``"feed_audio_stream"``, ``"transform"``, or ``"listen"``.

    Use ``"feed_audio_stream"`` for transformers that decode only after
    accumulating many frames (e.g. ggwave): *audio_input* is split into
    *chunk_size* frames, fed in order, and all emitted messages are
    aggregated across the whole stream."""

    chunk_size: int = 2048
    """Frame size (bytes) used to split *audio_input* when *feed_method* is
    ``"feed_audio_stream"``."""

    expected_types: List[str] = field(default_factory=list)
    """Message types that MUST appear in the captured output."""

    forbidden_types: List[str] = field(default_factory=list)
    """Message types that MUST NOT appear in the captured output."""

    stt_instance: Optional[Any] = None
    """Optional STT plugin object for full pipeline testing."""

    def execute(self) -> List[Message]:
        """Run the test and assert expected / forbidden messages.

        Returns:
            The list of captured :class:`ovos_bus_client.message.Message` objects.

        Raises:
            AssertionError: If any expected type is absent or any forbidden type
                is present.
        """
        listener = get_mini_listener(
            transformer_plugins=self.transformer_plugins or None,
            config=self.config or None,
            plugin_instances=self.plugin_instances or None,
            stt_instance=self.stt_instance,
        )
        try:
            if self.feed_method == "listen":
                messages = listener.listen(self.audio_input)
            elif self.feed_method == "feed_audio_stream":
                messages = listener.feed_audio_stream(
                    self.audio_input, chunk_size=self.chunk_size
                )
            else:
                result = getattr(listener, self.feed_method)(self.audio_input)
                if self.feed_method == "transform":
                    messages: List[Message] = result[2]
                else:
                    messages = result

            captured_types = {m.msg_type for m in messages}
            for expected in self.expected_types:
                assert expected in captured_types, (
                    f"Expected message type '{expected}' not found in captured "
                    f"messages: {captured_types}"
                )
            for forbidden in self.forbidden_types:
                assert forbidden not in captured_types, (
                    f"Forbidden message type '{forbidden}' was emitted: "
                    f"{captured_types}"
                )
            return messages
        finally:
            listener.shutdown()


@dataclass
class VADTest:
    """Declarative VAD plugin test.

    Feeds *audio_input* to a VAD engine and asserts silence classification
    and/or speech extraction results.

    Args:
        audio_input: Raw PCM bytes to test (default 1 024 zero bytes).
        vad_plugin: OPM VAD plugin name.  Ignored when *vad_instance* is set.
        vad_instance: Pre-instantiated VAD engine (takes precedence).
        expect_silence: When not ``None``, assert that
            ``vad.is_silence(audio_input)`` equals this value.
        expect_speech_bytes: When not ``None``, assert that
            ``vad.extract_speech(audio_input)`` equals this value.

    Example::

        from ovoscope.listener import VADTest, MockVADEngine

        VADTest(
            vad_instance=MockVADEngine(),
            audio_input=b"\\x00" * 512,
            expect_silence=True,
        ).execute()
    """

    audio_input: bytes = field(default=b"\x00" * 1024)
    """Raw PCM audio bytes to feed to the VAD engine."""

    vad_plugin: Optional[str] = None
    """OPM VAD plugin name (e.g. ``"ovos-vad-plugin-silero"``)."""

    vad_instance: Optional[Any] = None
    """Pre-instantiated VAD engine (takes precedence over *vad_plugin*)."""

    expect_silence: Optional[bool] = None
    """Expected result of ``is_silence(audio_input)``; ``None`` to skip."""

    expect_speech_bytes: Optional[bytes] = None
    """Expected result of ``extract_speech(audio_input)``; ``None`` to skip."""

    def execute(self) -> Tuple[Optional[bool], Optional[bytes]]:
        """Run the VAD test and assert configured expectations.

        Returns:
            ``(is_silent, speech_bytes)`` — each is ``None`` when the
            corresponding assertion was skipped.

        Raises:
            AssertionError: If any assertion fails.
            RuntimeError: If neither *vad_instance* nor *vad_plugin* is set.
        """
        if self.vad_instance is None and self.vad_plugin is None:
            raise RuntimeError(
                "VADTest requires either vad_instance or vad_plugin."
            )
        listener = get_mini_listener(
            vad_instance=self.vad_instance,
            vad_plugin=self.vad_plugin,
        )
        try:
            is_silent: Optional[bool] = None
            speech: Optional[bytes] = None

            if self.expect_silence is not None:
                is_silent = listener.is_silence(self.audio_input)
                assert is_silent == self.expect_silence, (
                    f"Expected is_silence={self.expect_silence}, "
                    f"got {is_silent}"
                )

            if self.expect_speech_bytes is not None:
                speech = listener.extract_speech(self.audio_input)
                assert speech == self.expect_speech_bytes, (
                    f"extract_speech result mismatch: "
                    f"expected {self.expect_speech_bytes!r}, got {speech!r}"
                )

            return is_silent, speech
        finally:
            listener.shutdown()


@dataclass
class WakeWordTest:
    """Declarative wake-word plugin test.

    Streams *audio_chunks* through a wake-word engine and asserts whether
    and at which frame detection occurs.

    Args:
        audio_chunks: Ordered list of PCM byte frames to feed (default:
            five 512-byte zero-byte frames).
        ww_plugin: OPM wake-word plugin name.  Ignored when *ww_instances*
            is set.
        ww_instances: Mapping of hotword name → engine instance.
        key_phrase: Wake-word name used when loading via *ww_plugin*.
        expect_detected: Assert that a detection occurred (``True``) or did
            not occur (``False``).
        expected_detection_frame: When not ``None``, assert that the first
            detection happened at this zero-based frame index.

    Example::

        from ovoscope.listener import WakeWordTest, MockHotWordEngine

        WakeWordTest(
            ww_instances={"hey_mycroft": MockHotWordEngine(trigger_after=3)},
            audio_chunks=[b"\\x00" * 512] * 5,
            expect_detected=True,
            expected_detection_frame=2,
        ).execute()
    """

    audio_chunks: List[bytes] = field(
        default_factory=lambda: [b"\x00" * 512] * 5
    )
    """Ordered list of PCM audio frames to feed."""

    ww_plugin: Optional[str] = None
    """OPM wake-word plugin name."""

    ww_instances: Optional[Dict[str, Any]] = None
    """Mapping of hotword name → engine instance (takes precedence)."""

    key_phrase: str = "hey_mycroft"
    """Wake-word name used when loading via *ww_plugin*."""

    expect_detected: bool = True
    """Assert that detection occurred (``True``) or did not (``False``)."""

    expected_detection_frame: Optional[int] = None
    """Expected zero-based index of the detection frame; ``None`` to skip."""

    def execute(self) -> Tuple[bool, Optional[int]]:
        """Run the wake-word test and assert configured expectations.

        Returns:
            ``(detected, frame_index)`` as returned by
            :meth:`MiniListener.scan_for_wakeword`.

        Raises:
            AssertionError: If any assertion fails.
            RuntimeError: If neither *ww_instances* nor *ww_plugin* is set.
        """
        if self.ww_instances is None and self.ww_plugin is None:
            raise RuntimeError(
                "WakeWordTest requires either ww_instances or ww_plugin."
            )
        listener = get_mini_listener(
            ww_instances=self.ww_instances,
            ww_plugin=self.ww_plugin,
        )
        try:
            detected, frame_idx = listener.scan_for_wakeword(self.audio_chunks)

            assert detected == self.expect_detected, (
                f"Expected detect={self.expect_detected}, got {detected} "
                f"(frame={frame_idx})"
            )
            if self.expected_detection_frame is not None:
                assert frame_idx == self.expected_detection_frame, (
                    f"Expected detection at frame {self.expected_detection_frame}, "
                    f"got {frame_idx}"
                )

            return detected, frame_idx
        finally:
            listener.shutdown()
