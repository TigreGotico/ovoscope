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
"""Unit tests for the MiniVoiceLoop harness.

TestMiniHotwordContainer  — container update/found/get_ww/verify (no dinkum)
TestMiniVoiceLoop         — feed_chunks + helpers (needs ovos-dinkum-listener)
TestVerifierGate          — verifier suppression (needs the hotword-verifier gate)
TestVoiceLoopTest         — declarative helper
"""
import inspect
import io
import time
import unittest
import wave
from unittest.mock import Mock

from ovos_bus_client.message import Message
from ovos_spec_tools import SpecMessage

from ovoscope.voice_loop import (
    ListenerHarness,
    MiniHotwordContainer,
    MiniVoiceLoop,
    MockHotWordEngine,
    MockStreamingSTT,
    VoiceLoopTest,
    get_mini_voice_loop,
)

_SILENCE = b"\x00" * 512


def _wav(speech_seconds=0.6, sample_rate=16000, sample_width=2):
    """Build in-memory WAV bytes of non-zero 'speech'."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setframerate(sample_rate)
        w.setsampwidth(sample_width)
        w.setnchannels(1)
        w.writeframes(b"\x10\x20" * int(sample_rate * speech_seconds))
    return buf.getvalue()

# ovos-dinkum-listener is an optional test dependency — the harness can build a
# real DinkumVoiceLoop only when it is installed.
try:
    from ovos_dinkum_listener.voice_loop.voice_loop import DinkumVoiceLoop
    HAS_DINKUM = True
    # The verifier gate is only present in builds shipping the hotword-verifier
    # feature; suppression assertions require it.
    HAS_VERIFY_GATE = "self.hotwords.verify" in inspect.getsource(
        DinkumVoiceLoop._detect_ww
    )
except ImportError:
    HAS_DINKUM = False
    HAS_VERIFY_GATE = False


def _accepting():
    v = Mock()
    v.verify.return_value = True
    return v


def _rejecting():
    v = Mock()
    v.verify.return_value = False
    return v


def _raising():
    v = Mock()
    v.verify.side_effect = RuntimeError("verifier boom")
    return v


# ---------------------------------------------------------------------------
# MiniHotwordContainer (no dinkum required)
# ---------------------------------------------------------------------------

class TestMiniHotwordContainer(unittest.TestCase):
    """MiniHotwordContainer unit tests."""

    def test_found_after_threshold(self):
        """found() returns the ww name once the engine fires."""
        c = MiniHotwordContainer(
            {"hey_mycroft": MockHotWordEngine(trigger_after=2)}
        )
        c.update(_SILENCE)
        self.assertIsNone(c.found())
        c.update(_SILENCE)
        self.assertEqual(c.found(), "hey_mycroft")

    def test_found_none_when_not_triggered(self):
        """found() returns None before the threshold."""
        c = MiniHotwordContainer(
            {"hey_mycroft": MockHotWordEngine(trigger_after=10)}
        )
        c.update(_SILENCE)
        self.assertIsNone(c.found())

    def test_update_feeds_all_engines(self):
        """update() forwards chunks to every registered engine."""
        a = MockHotWordEngine("hey_mycroft", trigger_after=1)
        b = MockHotWordEngine("hey_jarbas", trigger_after=1)
        c = MiniHotwordContainer({"hey_mycroft": a, "hey_jarbas": b})
        c.update(_SILENCE)
        self.assertEqual(a.update_count, 1)
        self.assertEqual(b.update_count, 1)

    def test_get_ww_metadata(self):
        """get_ww() returns listen-word metadata for a known ww."""
        c = MiniHotwordContainer({"hey_mycroft": MockHotWordEngine()})
        meta = c.get_ww("hey_mycroft")
        self.assertEqual(meta["key_phrase"], "hey_mycroft")
        self.assertTrue(meta["listen"])
        self.assertIsNone(meta["sound"])

    def test_get_ww_unknown_raises(self):
        """get_ww() raises ValueError for an unregistered ww."""
        c = MiniHotwordContainer({"hey_mycroft": MockHotWordEngine()})
        with self.assertRaises(ValueError):
            c.get_ww("unknown")

    def test_verify_accept(self):
        """verify() returns True when all verifiers accept."""
        c = MiniHotwordContainer({}, verifiers=[_accepting(), _accepting()])
        self.assertTrue(c.verify(b"audio"))

    def test_verify_reject(self):
        """verify() returns False when any verifier rejects."""
        c = MiniHotwordContainer({}, verifiers=[_accepting(), _rejecting()])
        self.assertFalse(c.verify(b"audio"))

    def test_verify_fail_open(self):
        """verify() ignores a raising verifier (fail-open)."""
        c = MiniHotwordContainer({}, verifiers=[_raising(), _accepting()])
        self.assertTrue(c.verify(b"audio"))

    def test_verify_no_verifiers(self):
        """verify() returns True when no verifiers are configured."""
        c = MiniHotwordContainer({})
        self.assertTrue(c.verify(b"audio"))


# ---------------------------------------------------------------------------
# MiniVoiceLoop (requires ovos-dinkum-listener)
# ---------------------------------------------------------------------------

@unittest.skipUnless(HAS_DINKUM, "ovos-dinkum-listener not installed")
class TestMiniVoiceLoop(unittest.TestCase):
    """MiniVoiceLoop integration tests against a real DinkumVoiceLoop."""

    def _loop(self, trigger_after=3, verifiers=None):
        ww = MockHotWordEngine("hey_mycroft", trigger_after=trigger_after)
        return MiniVoiceLoop(
            ww_instances={"hey_mycroft": ww}, verifiers=verifiers
        )

    def test_accept_emits_record_begin(self):
        """WW detected + accepting verifier emits the record-begin sequence."""
        with self._loop(verifiers=[_accepting()]) as vl:
            msgs = vl.feed_chunks([_SILENCE] * 5)
            types = {m.msg_type for m in msgs}
            self.assertIn("recognizer_loop:wakeword", types)
            self.assertIn("recognizer_loop:record_begin", types)

    def test_no_wakeword_no_events(self):
        """No detection emits no recognizer_loop events."""
        with self._loop(trigger_after=100, verifiers=[_accepting()]) as vl:
            msgs = vl.feed_chunks([_SILENCE] * 5)
            self.assertFalse(
                any(m.msg_type.startswith("recognizer_loop:") for m in msgs)
            )

    def test_raising_verifier_fails_open(self):
        """A raising verifier does not suppress detection (fail-open)."""
        with self._loop(verifiers=[_raising()]) as vl:
            vl.assert_record_begin_emitted(vl.feed_chunks([_SILENCE] * 5))

    def test_default_ww_instances(self):
        """Omitting ww_instances uses a default hey_mycroft engine."""
        with MiniVoiceLoop() as vl:
            vl.assert_record_begin_emitted(vl.feed_chunks([_SILENCE] * 3))

    def test_assert_wakeword_detected_helper(self):
        """assert_wakeword_detected passes on a real detection."""
        with self._loop() as vl:
            vl.feed_chunks([_SILENCE] * 5)
            vl.assert_wakeword_detected()  # operates on last feed result

    def test_assert_wakeword_detected_fails_without_detection(self):
        """assert_wakeword_detected raises when nothing fired."""
        with self._loop(trigger_after=100) as vl:
            vl.feed_chunks([_SILENCE] * 5)
            with self.assertRaises(AssertionError):
                vl.assert_wakeword_detected()

    def test_factory(self):
        """get_mini_voice_loop wires a usable harness."""
        vl = get_mini_voice_loop(
            ww_instances={"hey_mycroft": MockHotWordEngine(trigger_after=2)},
        )
        try:
            vl.assert_record_begin_emitted(vl.feed_chunks([_SILENCE] * 3))
        finally:
            vl.shutdown()

    def test_feed_file_full_sequence(self):
        """feed_file drives the whole loop to a transcribed utterance."""
        ww = MockHotWordEngine("hey_mycroft", trigger_after=2)
        stt = MockStreamingSTT(transcript="what time is it")
        with MiniVoiceLoop(ww_instances={"hey_mycroft": ww}, stt_instance=stt) as vl:
            msgs = vl.feed_file(_wav(), silence_tail_chunks=30)
            types = [m.msg_type for m in msgs]
            self.assertIn("recognizer_loop:record_begin", types)
            self.assertIn("recognizer_loop:record_end", types)
            vl.assert_utterance_emitted("what time is it", msgs)

    def test_feed_file_empty_transcript_unknown(self):
        """feed_file with no transcript emits the recognition-unknown event."""
        ww = MockHotWordEngine("hey_mycroft", trigger_after=2)
        with MiniVoiceLoop(ww_instances={"hey_mycroft": ww},
                           stt_instance=MockStreamingSTT(transcript="")) as vl:
            msgs = vl.feed_file(_wav(), silence_tail_chunks=30)
            types = [m.msg_type for m in msgs]
            self.assertIn("recognizer_loop:speech.recognition.unknown", types)
            self.assertNotIn("recognizer_loop:utterance", types)


# ---------------------------------------------------------------------------
# Verifier suppression gate (requires the hotword-verifier feature)
# ---------------------------------------------------------------------------

@unittest.skipUnless(
    HAS_VERIFY_GATE, "ovos-dinkum-listener build lacks the hotword-verifier gate"
)
class TestVerifierGate(unittest.TestCase):
    """Tests that depend on DinkumVoiceLoop gating on hotwords.verify()."""

    def test_reject_suppresses(self):
        """A rejecting verifier suppresses the whole record sequence."""
        ww = MockHotWordEngine("hey_mycroft", trigger_after=3)
        with MiniVoiceLoop(
            ww_instances={"hey_mycroft": ww}, verifiers=[_rejecting()]
        ) as vl:
            msgs = vl.feed_chunks([_SILENCE] * 5)
            vl.assert_wakeword_suppressed(msgs)

    def test_voice_loop_test_expect_suppressed(self):
        """VoiceLoopTest(expect_record_begin=False) passes on rejection."""
        VoiceLoopTest(
            ww_instances={"hey_mycroft": MockHotWordEngine(trigger_after=3)},
            verifiers=[_rejecting()],
            audio_chunks=[_SILENCE] * 5,
            expect_record_begin=False,
        ).execute()


# ---------------------------------------------------------------------------
# VoiceLoopTest declarative helper (requires ovos-dinkum-listener)
# ---------------------------------------------------------------------------

@unittest.skipUnless(HAS_DINKUM, "ovos-dinkum-listener not installed")
class TestVoiceLoopTest(unittest.TestCase):
    """VoiceLoopTest declarative helper tests."""

    def test_expect_record_begin_passes(self):
        """Passes when record_begin is expected and emitted."""
        VoiceLoopTest(
            ww_instances={"hey_mycroft": MockHotWordEngine(trigger_after=3)},
            verifiers=[_accepting()],
            audio_chunks=[_SILENCE] * 5,
            expect_record_begin=True,
        ).execute()

    def test_expect_record_begin_fails_without_detection(self):
        """Raises AssertionError when record_begin expected but absent."""
        with self.assertRaises(AssertionError):
            VoiceLoopTest(
                ww_instances={"hey_mycroft": MockHotWordEngine(trigger_after=100)},
                audio_chunks=[_SILENCE] * 5,
                expect_record_begin=True,
            ).execute()

    def test_audio_file_with_expected_utterance(self):
        """audio_file path drives the full loop and asserts the utterance."""
        VoiceLoopTest(
            ww_instances={"hey_mycroft": MockHotWordEngine(trigger_after=2)},
            stt_instance=MockStreamingSTT(transcript="hello world"),
            audio_file=_wav(),
            expect_utterance="hello world",
        ).execute()


# ---------------------------------------------------------------------------
# TestMiniVoiceLoopNamespaceBridging
# ---------------------------------------------------------------------------

@unittest.skipUnless(HAS_DINKUM, "ovos-dinkum-listener not installed")
class TestMiniVoiceLoopNamespaceBridging(unittest.TestCase):
    """The MiniVoiceLoop callbacks emit the LEGACY ``recognizer_loop:*`` topics;
    ``record_begin``/``record_end``/``utterance`` are migrated to the ovos.*
    spec namespace.  These tests pin that the FakeBus namespace bridging
    connects the two namespaces, and that turning it off isolates one.
    """

    def test_full_loop_legacy_reaches_spec_via_bridging(self):
        """Default loop (bridging on): the legacy record-begin/record-end/
        utterance emitted while driving the full loop are also delivered to
        subscribers on the spec topics."""
        with MiniVoiceLoop(
            ww_instances={"hey_mycroft": MockHotWordEngine(trigger_after=2)},
            stt_instance=MockStreamingSTT(transcript="hello world"),
        ) as vl:
            spec = {"begin": [], "end": [], "utt": []}
            vl.bus.on(str(SpecMessage.LISTENER_RECORD_STARTED),
                      lambda m: spec["begin"].append(m))
            vl.bus.on(str(SpecMessage.LISTENER_RECORD_ENDED),
                      lambda m: spec["end"].append(m))
            vl.bus.on(str(SpecMessage.UTTERANCE),
                      lambda m: spec["utt"].append(m))

            msgs = vl.feed_file(_wav())
            time.sleep(0.05)
            legacy_types = [m.msg_type for m in msgs]
            self.assertIn("recognizer_loop:record_begin", legacy_types)
            self.assertTrue(spec["begin"],
                            "ovos.listener.record.started not seen via bridge")
            self.assertTrue(spec["end"],
                            "ovos.listener.record.ended not seen via bridge")
            self.assertTrue(spec["utt"],
                            "ovos.utterance.handle not seen via bridge")

    def test_record_begin_spec_native(self):
        """A SPEC producer reaches a spec subscriber natively."""
        with MiniVoiceLoop(
            ww_instances={"hey_mycroft": MockHotWordEngine(trigger_after=2)},
        ) as vl:
            spec_hits = []
            vl.bus.on(str(SpecMessage.LISTENER_RECORD_STARTED),
                      lambda m: spec_hits.append(m))
            vl.bus.emit(Message(str(SpecMessage.LISTENER_RECORD_STARTED)))
            time.sleep(0.05)
            self.assertTrue(spec_hits,
                            "ovos.listener.record.started not delivered natively")

    def test_no_bridging_isolates_legacy_from_spec(self):
        """With bridging OFF, a legacy record-begin does NOT reach a spec-only
        subscriber."""
        with MiniVoiceLoop(
            ww_instances={"hey_mycroft": MockHotWordEngine(trigger_after=2)},
            modernize=False, emit_legacy=False,
        ) as vl:
            legacy_hits, spec_hits = [], []
            vl.bus.on("recognizer_loop:record_begin",
                      lambda m: legacy_hits.append(m))
            vl.bus.on(str(SpecMessage.LISTENER_RECORD_STARTED),
                      lambda m: spec_hits.append(m))
            vl.bus.emit(Message("recognizer_loop:record_begin"))
            time.sleep(0.1)
            self.assertTrue(legacy_hits, "legacy record_begin should still fire")
            self.assertEqual(spec_hits, [],
                             "spec topic must not fire with bridging off")


class TestAssertionHelpersAcceptCanonicalTopics(unittest.TestCase):
    """The assert_*_emitted/suppressed helpers filter a catch-all-captured
    stream (same defect class as ovoscope.assert_spoke) — they must accept
    canonical spec topics, not just the legacy recognizer_loop:* spellings.
    """

    def test_assert_record_begin_emitted_accepts_canonical_only_stream(self):
        harness = ListenerHarness()
        captured = [Message("ovos.listener.record.started")]
        harness.assert_record_begin_emitted(captured)  # must not raise

    def test_assert_wakeword_detected_accepts_canonical_only_stream(self):
        harness = ListenerHarness()
        captured = [
            Message("recognizer_loop:wakeword"),  # not migrated — legacy only
            Message("ovos.listener.record.started"),
        ]
        harness.assert_wakeword_detected(captured)  # must not raise

    def test_assert_utterance_emitted_accepts_canonical_only_stream(self):
        harness = ListenerHarness()
        captured = [Message("ovos.utterance.handle", {"utterances": ["hi"]})]
        harness.assert_utterance_emitted("hi", captured)  # must not raise

    def test_assert_wakeword_suppressed_still_fails_on_canonical_record_begin(self):
        """Regression for the negative-assertion false-GREEN: a canonical-only
        captured stream that DOES contain a record-begin must still fail
        assert_wakeword_suppressed, not silently pass because the helper only
        recognised the legacy spelling.
        """
        harness = ListenerHarness()
        captured = [Message("ovos.listener.record.started")]
        with self.assertRaises(AssertionError):
            harness.assert_wakeword_suppressed(captured)

    def test_assert_wakeword_suppressed_passes_on_truly_empty_stream(self):
        harness = ListenerHarness()
        harness.assert_wakeword_suppressed([])  # must not raise


if __name__ == "__main__":
    unittest.main()
