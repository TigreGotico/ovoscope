"""Lightweight per-clip wake-word detection probe.

Drives a **real** OVOS :class:`HotWordEngine` over a single audio clip the way
the live listening loop does — a few seconds of leading silence to warm the
engine's streaming feature buffers, then the clip streamed frame by frame —
and returns a per-clip detection decision plus latency.

Unlike :class:`ovoscope.voice_loop.MiniVoiceLoop` (which runs the full
``DinkumVoiceLoop`` state machine and needs the ``[listener]`` extra), this is
self-contained: no bus, no VAD, no state machine — just ``engine.update()`` /
``engine.found_wake_word()`` over primed audio. Ideal for plugin test suites
and benchmarks that score detection on labelled fixtures.

Why the long lead matters
-------------------------
Streaming detectors (openWakeWord, microWakeWord, …) only emit a prediction
once their rolling mel/embedding window is full (~2.5 s of frames). A clip fed
with too little leading silence never fills that window: the activation is
missed (a false reject), and on the shortest clips the half-full buffer raises
a shape mismatch that drops the sample entirely. Priming with a few seconds of
leading silence fills the window *before* the keyword arrives, exactly as a
live microphone keeps the loop warm. :data:`PRIME_SECONDS` defaults to 3 s.

Audio contract: mono ``float32`` in ``[-1, 1]`` at the engine's sample rate
(16 kHz for every OVOS hotword engine). Resample upstream if your source
differs. Needs the ``[bench]`` extra (numpy).
"""
from __future__ import annotations

import inspect
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Optional

SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280   # 80 ms @ 16 kHz — the OVOS listener chunk size
PRIME_SECONDS = 3.0    # leading silence to warm the feature window (see module docstring)
TAIL_SECONDS = 0.5     # trailing silence so a late activation can settle


@dataclass
class WakeWordDetection:
    """Outcome of running one clip through a hotword engine."""

    detected: bool
    latency_ms: float
    frames_to_detection: Optional[int]  # frames streamed before the latch fired


@contextmanager
def hotword_compat():
    """Widen ``HotWordEngine.__init__`` for the duration of the block.

    Recent wake-word plugins call ``super().__init__(key_phrase, config, lang)``;
    older ``HotWordEngine`` bases accept only ``(key_phrase, config)``. Widen the
    base signature to ignore the extra argument, then put the original back.

    The patch is on a process-wide base class, so it MUST NOT outlive the
    engine construction it exists for: leaving it installed changes how every
    later hotword plugin in the process is constructed — including code under
    test that is supposed to see the real signature.

    A no-op when the installed base already accepts ``lang``.
    """
    from ovos_plugin_manager.templates import hotwords as hw

    base = hw.HotWordEngine
    if "lang" in inspect.signature(base.__init__).parameters:
        yield
        return
    _orig = base.__init__

    def _compat(self, key_phrase="hey_mycroft", config=None, lang=None,
                *args, **kwargs):
        _orig(self, key_phrase, config)

    base.__init__ = _compat
    try:
        yield
    finally:
        base.__init__ = _orig


def load_hotword_engine(plugin_id: str, key_phrase: str = "hey_mycroft",
                        config: Optional[Dict[str, Any]] = None,
                        lang: str = "en-us"):
    """Load and instantiate a real OVOS hotword engine by plugin id.

    Tolerates the ``HotWordEngine(lang)`` signature change and dash/underscore
    variation in plugin ids, mirroring how the listening loop resolves engines.
    """
    from ovos_plugin_manager.wakewords import load_wake_word_plugin

    with hotword_compat():
        clazz = load_wake_word_plugin(plugin_id)
        if clazz is None and "-" in plugin_id:
            clazz = load_wake_word_plugin(plugin_id.replace("-", "_"))
        if clazz is None:
            raise ValueError(f"no wake-word plugin {plugin_id!r}")
        return clazz(key_phrase, dict(config or {}), lang)


class WakeWordProbe:
    """Drive a real ``HotWordEngine`` over single clips with listener-style priming."""

    def __init__(self, engine, *, sample_rate: int = SAMPLE_RATE,
                 frame_samples: int = FRAME_SAMPLES,
                 prime_seconds: float = PRIME_SECONDS,
                 tail_seconds: float = TAIL_SECONDS):
        self.engine = engine
        self.sample_rate = sample_rate
        self.frame_samples = frame_samples
        self.prime_seconds = prime_seconds
        self.tail_seconds = tail_seconds

    @classmethod
    def from_plugin(cls, plugin_id: str, key_phrase: str = "hey_mycroft",
                    config: Optional[Dict[str, Any]] = None,
                    lang: str = "en-us", **kwargs) -> "WakeWordProbe":
        """Build a probe from a plugin id (loads + instantiates the engine)."""
        engine = load_hotword_engine(plugin_id, key_phrase, config, lang)
        return cls(engine, **kwargs)

    def prime_pad(self, array):
        """Wrap a clip in leading + trailing silence, padded to whole frames."""
        import numpy as np

        arr = np.asarray(array, dtype="float32")
        lead = np.zeros(int(self.sample_rate * self.prime_seconds), dtype="float32")
        tail = np.zeros(int(self.sample_rate * self.tail_seconds), dtype="float32")
        out = np.concatenate([lead, arr, tail])
        rem = len(out) % self.frame_samples
        if rem:
            out = np.concatenate(
                [out, np.zeros(self.frame_samples - rem, dtype="float32")])
        return out

    @staticmethod
    def to_pcm16(array) -> bytes:
        """Float32 ``[-1, 1]`` mono array → 16-bit little-endian PCM bytes."""
        import numpy as np

        arr = np.clip(np.asarray(array, dtype="float32"), -1.0, 1.0)
        return (arr * 32767.0).astype("<i2").tobytes()

    def detect(self, array) -> WakeWordDetection:
        """Stream one clip through the engine; return the detection decision.

        The OVOS contract is ``update(chunk_bytes)`` to feed audio then
        ``found_wake_word()`` to read the latch. Some plugins keep a vestigial
        ``found_wake_word(frame_data)`` argument they ignore — we pass the chunk
        through so the signature matches either way.
        """
        primed = self.prime_pad(array)
        if hasattr(self.engine, "reset"):
            try:
                self.engine.reset()
            except Exception:
                pass
        fww = self.engine.found_wake_word
        fww_takes_arg = len(inspect.signature(fww).parameters) >= 1
        has_update = hasattr(self.engine, "update")
        pcm = self.to_pcm16(primed)
        step = self.frame_samples * 2  # 2 bytes / sample (int16)
        start = time.perf_counter()
        for i, off in enumerate(range(0, len(pcm), step), 1):
            chunk = pcm[off:off + step]
            if has_update:
                self.engine.update(chunk)
            if (fww(chunk) if fww_takes_arg else fww()):
                latency = (time.perf_counter() - start) * 1000
                return WakeWordDetection(True, round(latency, 3), i)
        latency = (time.perf_counter() - start) * 1000
        return WakeWordDetection(False, round(latency, 3), None)
