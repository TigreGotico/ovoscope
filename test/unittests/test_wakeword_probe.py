"""Tests for the per-clip WakeWordProbe (no real engine / no heavy deps)."""
import inspect
import unittest

import numpy as np

from ovoscope.wakeword_probe import (
    FRAME_SAMPLES,
    PRIME_SECONDS,
    SAMPLE_RATE,
    WakeWordDetection,
    WakeWordProbe,
)


class _FakeEngine:
    """Fires once it has seen ``trigger_after`` update() calls. Resets on read."""

    def __init__(self, trigger_after=None, takes_arg=False):
        self.trigger_after = trigger_after
        self._calls = 0
        self._fired = False
        self.reset_count = 0
        # Build found_wake_word with or without the vestigial frame arg, so the
        # probe's signature sniffing is exercised both ways.
        if takes_arg:
            def found_wake_word(frame_data=b""):
                return self._read()
        else:
            def found_wake_word():
                return self._read()
        self.found_wake_word = found_wake_word

    def update(self, chunk: bytes):
        self._calls += 1
        if self.trigger_after is not None and self._calls >= self.trigger_after:
            self._fired = True

    def _read(self):
        fired, self._fired = self._fired, False
        return fired

    def reset(self):
        self.reset_count += 1
        self._calls = 0
        self._fired = False


class TestPrimePad(unittest.TestCase):
    def test_leads_with_silence_and_pads_to_frames(self):
        probe = WakeWordProbe(_FakeEngine())
        clip = np.ones(1000, dtype="float32")
        out = probe.prime_pad(clip)
        # whole number of frames
        self.assertEqual(len(out) % FRAME_SAMPLES, 0)
        # at least PRIME_SECONDS of leading silence before any signal
        lead = int(SAMPLE_RATE * PRIME_SECONDS)
        self.assertTrue(np.all(out[:lead] == 0.0))
        # the clip survives inside the padded buffer
        self.assertGreaterEqual(len(out), lead + len(clip))

    def test_default_prime_is_a_few_seconds(self):
        self.assertGreaterEqual(PRIME_SECONDS, 2.5)


class TestDetect(unittest.TestCase):
    def test_detects_and_reports_frames_and_latency(self):
        engine = _FakeEngine(trigger_after=5)
        probe = WakeWordProbe(engine)
        result = probe.detect(np.zeros(SAMPLE_RATE, dtype="float32"))
        self.assertIsInstance(result, WakeWordDetection)
        self.assertTrue(result.detected)
        self.assertEqual(result.frames_to_detection, 5)
        self.assertGreaterEqual(result.latency_ms, 0.0)
        self.assertEqual(engine.reset_count, 1)  # reset before streaming

    def test_no_detection_returns_none_frames(self):
        probe = WakeWordProbe(_FakeEngine(trigger_after=None))
        result = probe.detect(np.zeros(SAMPLE_RATE, dtype="float32"))
        self.assertFalse(result.detected)
        self.assertIsNone(result.frames_to_detection)

    def test_handles_found_wake_word_with_frame_arg(self):
        engine = _FakeEngine(trigger_after=3, takes_arg=True)
        self.assertGreaterEqual(
            len(inspect.signature(engine.found_wake_word).parameters), 1)
        result = WakeWordProbe(engine).detect(
            np.zeros(SAMPLE_RATE, dtype="float32"))
        self.assertTrue(result.detected)


if __name__ == "__main__":
    unittest.main()
