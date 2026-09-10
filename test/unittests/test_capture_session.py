"""Unit tests for CaptureSession.

CaptureSession only ever touches ``minicroft.bus``, so these tests drive a
``SimpleNamespace(bus=FakeBus())`` stub instead of booting a real MiniCroft.
A boot costs seconds and hundreds of MB of retained memory per test class,
and buys nothing here.

Race-condition coverage for the same class lives in
``test_audit_round1.py::TestCaptureSessionRaces`` and
``test_audit_round2.py::TestCaptureSessionArming``.
"""
import threading
import time
import unittest
from types import SimpleNamespace

from ovos_bus_client.message import Message
from ovos_utils.fakebus import FakeBus
from ovos_utils.log import LOG

from ovoscope import CaptureSession


class TestCaptureSession(unittest.TestCase):
    """CaptureSession is tested by emitting directly on a stub FakeBus."""

    def setUp(self):
        LOG.set_level("ERROR")
        self.mc = SimpleNamespace(bus=FakeBus())

    def tearDown(self):
        self.mc.bus.close()
        LOG.set_level("CRITICAL")

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _emit_after(self, delay_s: float, msg: Message):
        """Emit msg after delay_s seconds (from a background thread)."""
        def _run():
            import time
            time.sleep(delay_s)
            self.mc.bus.emit(msg)
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return t

    # ------------------------------------------------------------------
    # tests
    # ------------------------------------------------------------------
    def test_capture_receives_messages(self):
        """Messages emitted after capture() starts are stored in .responses.

        The EOF message itself IS included in responses — the generic "message"
        handler appends it before the specific EOF handler sets done.  This is
        consistent with how End2EndTest includes ovos.utterance.handled in its
        expected_messages list.
        """
        cs = CaptureSession(self.mc,
                            eof_msgs=["test.eof"],
                            ignore_messages=[])
        self._emit_after(0.05, Message("test.msg.A", data={"n": 1}))
        self._emit_after(0.10, Message("test.eof"))

        cs.capture(Message("test.trigger"), timeout=3)
        msgs = cs.finish()

        types = [m.msg_type for m in msgs]
        self.assertIn("test.trigger", types)
        self.assertIn("test.msg.A", types)
        # EOF is the final message in responses (captured before done is set)
        self.assertEqual(msgs[-1].msg_type, "test.eof")

    def test_capture_ignores_listed_messages(self):
        """Messages in ignore_messages must not appear in .responses."""
        cs = CaptureSession(self.mc,
                            eof_msgs=["test.eof"],
                            ignore_messages=["test.noise"])
        self._emit_after(0.05, Message("test.noise"))
        self._emit_after(0.10, Message("test.signal"))
        self._emit_after(0.15, Message("test.eof"))

        cs.capture(Message("test.trigger"), timeout=3)
        msgs = cs.finish()

        types = [m.msg_type for m in msgs]
        self.assertNotIn("test.noise", types)
        self.assertIn("test.signal", types)

    def test_capture_eof_stops_collection(self):
        """Messages emitted after the EOF must not be captured."""
        cs = CaptureSession(self.mc,
                            eof_msgs=["test.eof"],
                            ignore_messages=[])
        self._emit_after(0.05, Message("test.before"))
        self._emit_after(0.10, Message("test.eof"))
        # Give eof handler time to set done before test.after is emitted
        self._emit_after(0.25, Message("test.after"))   # must NOT appear

        cs.capture(Message("test.trigger"), timeout=3)
        msgs = cs.finish()

        types = [m.msg_type for m in msgs]
        self.assertIn("test.before", types)
        # The EOF itself is the last captured message; test.after should not appear
        self.assertNotIn("test.after", types,
                         "message after EOF must not be captured")

    def test_capture_async_messages_separated(self):
        """Messages listed in async_messages go to async_responses, not responses."""
        cs = CaptureSession(self.mc,
                            eof_msgs=["test.eof"],
                            ignore_messages=[],
                            async_messages=["test.async"])
        self._emit_after(0.05, Message("test.sync"))
        self._emit_after(0.08, Message("test.async"))
        self._emit_after(0.12, Message("test.eof"))

        cs.capture(Message("test.trigger"), timeout=3)
        msgs = cs.finish()

        sync_types = [m.msg_type for m in msgs]
        async_types = [m.msg_type for m in cs.async_responses]

        self.assertIn("test.sync", sync_types)
        self.assertNotIn("test.async", sync_types,
                         "async message must not be in main responses")
        self.assertIn("test.async", async_types,
                       "async message must be in async_responses")

    def test_finish_returns_responses_list(self):
        """finish() must return the same list as .responses."""
        cs = CaptureSession(self.mc, eof_msgs=["test.eof"], ignore_messages=[])
        self._emit_after(0.05, Message("test.x"))
        self._emit_after(0.10, Message("test.eof"))

        cs.capture(Message("test.trigger"), timeout=3)
        returned = cs.finish()

        self.assertIsInstance(returned, list)
        self.assertTrue(len(returned) >= 1,
                        "at least the trigger message must be in the list")

    def test_finish_stops_future_capture(self):
        """After finish(), further bus messages must not append to responses."""
        cs = CaptureSession(self.mc, eof_msgs=["test.eof"], ignore_messages=[])
        self._emit_after(0.05, Message("test.eof"))  # trigger finish quickly
        cs.capture(Message("test.trigger"), timeout=3)
        responses_before = cs.finish()
        count_before = len(responses_before)

        # emit another message after finish — should be ignored
        self.mc.bus.emit(Message("test.post.finish"))
        import time; time.sleep(0.05)

        self.assertEqual(len(cs.responses), count_before,
                         "no new messages should be captured after finish()")

    def test_multiple_eof_types(self):
        """Any message in the eof_msgs list should stop capture."""
        cs = CaptureSession(self.mc,
                            eof_msgs=["eof.alpha", "eof.beta"],
                            ignore_messages=[])
        self._emit_after(0.05, Message("test.x"))
        self._emit_after(0.10, Message("eof.beta"))   # second eof type
        self._emit_after(0.20, Message("test.late"))  # must not appear

        cs.capture(Message("test.trigger"), timeout=3)
        msgs = cs.finish()
        types = [m.msg_type for m in msgs]

        self.assertIn("test.x", types)
        self.assertNotIn("test.late", types)

    def test_eof_count_waits_for_n_occurrences(self):
        """With eof_count>1, capture continues until an eof topic is seen that
        many times — for scenarios with N concurrent lifecycles each terminating
        on the same eof topic."""
        cs = CaptureSession(self.mc,
                            eof_msgs=["test.eof"],
                            eof_count=2,
                            ignore_messages=[])
        self._emit_after(0.05, Message("test.eof"))      # 1st eof — must NOT stop
        self._emit_after(0.10, Message("test.between"))  # captured (after 1st eof)
        self._emit_after(0.15, Message("test.eof"))      # 2nd eof — stops capture
        self._emit_after(0.30, Message("test.after"))    # must NOT appear

        cs.capture(Message("test.trigger"), timeout=3)
        msgs = cs.finish()
        types = [m.msg_type for m in msgs]

        self.assertIn("test.between", types,
                      "a message between the 1st and 2nd eof must be captured")
        self.assertEqual(types.count("test.eof"), 2,
                         "both eof occurrences are captured")
        self.assertNotIn("test.after", types,
                         "message after the Nth eof must not be captured")

    def test_eof_count_resets_between_captures(self):
        """The eof counter resets per capture() call so eof_count applies fresh."""
        cs = CaptureSession(self.mc,
                            eof_msgs=["test.eof"],
                            eof_count=2,
                            ignore_messages=[])
        self._emit_after(0.05, Message("test.eof"))
        self._emit_after(0.10, Message("test.eof"))
        cs.capture(Message("test.trigger1"), timeout=3)
        cs.finish()
        # a second capture must again require 2 eofs, not be already-done
        cs2 = CaptureSession(self.mc, eof_msgs=["test.eof"], eof_count=2,
                             ignore_messages=[])
        self._emit_after(0.05, Message("test.eof"))
        self._emit_after(0.10, Message("test.mid"))
        self._emit_after(0.15, Message("test.eof"))
        cs2.capture(Message("test.trigger2"), timeout=3)
        types = [m.msg_type for m in cs2.finish()]
        self.assertIn("test.mid", types)

    def test_capture_timeout_returns_partial_results(self):
        """If the EOF never fires, capture() must return after the timeout
        and finish() must still return whatever was captured."""
        cs = CaptureSession(self.mc, eof_msgs=["never.fires"], ignore_messages=[])
        self._emit_after(0.05, Message("test.partial"))
        # timeout=0.2 — EOF never fires
        cs.capture(Message("test.trigger"), timeout=0.2)
        msgs = cs.finish()
        types = [m.msg_type for m in msgs]
        self.assertIn("test.partial", types)

    def test_unmatched_utterance_ends_on_terminal_signal_not_timeout(self):
        """A caller narrowing eof_msgs to a mid-pipeline topic (e.g. to dodge
        a get_response() deadlock on a *matched* handler, the exact pattern
        ovos-skill-alerts' multilang golden suite uses) must still end
        capture promptly for an UNMATCHED utterance, which never reaches
        that topic. capture() must fall back to the pipeline's own terminal
        signal (ovos.utterance.handled) instead of paying the full timeout
        on every unmatched row.
        """
        cs = CaptureSession(self.mc,
                            eof_msgs=["mycroft.skill.handler.start"],
                            ignore_messages=[])
        self._emit_after(0.05, Message("recognizer_loop:utterance"))
        self._emit_after(0.10, Message("ovos.intent.unmatched"))
        self._emit_after(0.12, Message("ovos.utterance.handled"))
        # "mycroft.skill.handler.start" never fires: only the timeout backstop
        # (5s) would return without the terminal-signal fallback.
        start = time.monotonic()
        completed = cs.capture(Message("test.trigger"), timeout=5)
        elapsed = time.monotonic() - start
        msgs = cs.finish()

        self.assertTrue(completed, "capture must end on a terminal signal, not time out")
        self.assertLess(elapsed, 2, "capture must not wait out the full timeout")
        types = [m.msg_type for m in msgs]
        # ovos.intent.unmatched fires first but is not a terminal signal —
        # capture keeps going until ovos.utterance.handled, the single true
        # end-marker, and includes both in the captured messages.
        self.assertIn("ovos.intent.unmatched", types)
        self.assertIn("ovos.utterance.handled", types)

    def test_terminal_signals_can_be_disabled(self):
        """terminal_signals=False restores the old eof_msgs-only behaviour."""
        cs = CaptureSession(self.mc,
                            eof_msgs=["mycroft.skill.handler.start"],
                            ignore_messages=[],
                            terminal_signals=False)
        self._emit_after(0.05, Message("ovos.intent.unmatched"))
        self._emit_after(0.08, Message("ovos.utterance.handled"))
        completed = cs.capture(Message("test.trigger"), timeout=0.3)
        cs.finish()
        self.assertFalse(completed, "terminal signals must be ignored when disabled")

    def test_terminal_signals_skipped_when_eof_count_above_one(self):
        """eof_count>1 counts occurrences of ovos.utterance.handled across
        concurrent lifecycles; the terminal-signal fallback must not
        short-circuit that count after only one occurrence."""
        cs = CaptureSession(self.mc,
                            eof_msgs=["ovos.utterance.handled"],
                            eof_count=2,
                            ignore_messages=[])
        self._emit_after(0.05, Message("ovos.intent.unmatched"))
        self._emit_after(0.08, Message("ovos.utterance.handled"))
        # only ONE ovos.utterance.handled fires — eof_count=2 must not be
        # satisfied early by the terminal-signal fallback.
        completed = cs.capture(Message("test.trigger"), timeout=0.3)
        cs.finish()
        self.assertFalse(completed, "eof_count must not be short-circuited by terminal signals")


if __name__ == "__main__":
    unittest.main()


class TestCaptureSessionTrainingNoise(unittest.TestCase):
    """A 'mycroft.skills.trained' event can legitimately interleave with a
    capture window (e.g. a pipeline plugin re-training for a secondary
    lang). It is filtered out via TRAINING_NOISE/DEFAULT_IGNORED so
    sequence comparisons stay exact on everything else — genuinely missing
    or duplicated real messages must still be caught, never masked as a
    subsequence match would."""

    def setUp(self):
        LOG.set_level("ERROR")
        self.mc = SimpleNamespace(bus=FakeBus())

    def tearDown(self):
        self.mc.bus.close()
        LOG.set_level("CRITICAL")

    def test_trained_event_interleaved_is_filtered_not_counted(self):
        from ovoscope import CaptureSession, DEFAULT_IGNORED, TRAINING_NOISE

        self.assertIn("mycroft.skills.trained", TRAINING_NOISE)
        self.assertIn("mycroft.skills.trained", DEFAULT_IGNORED)

        session = CaptureSession(minicroft=self.mc)
        session._armed = True
        self.mc.bus.emit(Message("speak", {"utterance": "hi"}))
        self.mc.bus.emit(Message("mycroft.skills.trained"))
        self.mc.bus.emit(Message("ovos.utterance.handled"))

        types = [m.msg_type for m in session.responses]
        self.assertEqual(types, ["speak", "ovos.utterance.handled"],
                         "'mycroft.skills.trained' must be filtered out of "
                         "the exact-comparison sequence, not counted as a "
                         "captured message")

    def test_genuinely_missing_message_still_fails_exact_comparison(self):
        """Filtering the training-noise topic must not become an excuse to
        subsequence-match the rest: a real message that never arrives has
        to still make an exact sequence comparison fail."""
        from ovoscope import CaptureSession

        session = CaptureSession(minicroft=self.mc)
        session._armed = True

        expected_types = ["speak", "mycroft.skill.handler.complete", "ovos.utterance.handled"]
        self.mc.bus.emit(Message("mycroft.skills.trained"))
        self.mc.bus.emit(Message("speak", {"utterance": "hi"}))
        # "mycroft.skill.handler.complete" never arrives.
        self.mc.bus.emit(Message("ovos.utterance.handled"))

        received_types = [m.msg_type for m in session.responses]
        self.assertNotEqual(received_types, expected_types)
        self.assertEqual(len(received_types), 2)
