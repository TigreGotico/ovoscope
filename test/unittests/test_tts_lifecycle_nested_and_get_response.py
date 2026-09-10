"""Regression tests for OpenVoiceOS/ovos-skill-alerts#138 (Update 3):

ovoscope's synchronous FakeBus never completes the TTS handshake for
NESTED speak calls, and never resolves an unanswered get_response()/
ask_yesno() flow, so real skill handlers hang under the harness.

Both tests must complete quickly (well under the 15-20s upstream ceilings)
rather than hang.

Also covers the follow-up defect found in adversarial review of ovoscope
PR #130: the FIRST version of the get_response watchdog kept ONE flat list
of pending timers, so ANY skill's `.disable` cancelled EVERY other skill's
still-pending watchdog too. In a fleet-style MiniCroft running multiple
skills concurrently, skill A finishing its (answered) get_response() would
silently disarm skill B's watchdog, and if B's question was never answered
it would hang forever again — exactly the bug this file exists to prevent.
The fix scopes each watchdog by (skill_id, session_id), the same two-part
scope `ovos_workshop`'s own
`@killable_event("mycroft.skills.abort_question", check_skill_id=True)`
already uses to decide which stalled thread an abort is actually for.
"""
import threading
import time
import unittest

from ovos_bus_client.message import Message
from ovos_bus_client.session import Session
from ovos_workshop.skills.ovos import OVOSSkill
from ovos_utils.log import LOG

from ovoscope import get_minicroft, MiniCroft

NESTED_SKILL_ID = "ovoscope-unittest-nested-speak.test"
GETRESPONSE_SKILL_ID = "ovoscope-unittest-get-response.test"
CONCURRENT_SKILL_A = "ovoscope-unittest-get-response-a.test"
CONCURRENT_SKILL_B = "ovoscope-unittest-get-response-b.test"


class NestedSpeakSkill(OVOSSkill):
    """Mirrors ovos-skill-alerts' _get_response_cascade shape: a handler that
    calls speak_dialog(..., wait=True) from INSIDE another handler that itself
    already spoke, so the second wait_while_speaking() has to resolve against
    a speak emitted mid-handler on the same thread."""

    def initialize(self):
        self.add_event("unittest.nested_speak", self.handle_outer)

    def handle_inner(self, message: Message):
        # Nested call: this speak_dialog happens DURING handle_outer, after
        # handle_outer's own speak has already ducked/unducked once.
        self.speak("inner reply", wait=True)

    def handle_outer(self, message: Message):
        self.speak("outer reply", wait=True)
        self.handle_inner(message)
        self.bus.emit(message.forward("unittest.nested_speak.done"))


class GetResponseSkill(OVOSSkill):
    """Calls get_response() and never gets an answer from the test — mirrors
    ask_yesno()/get_response() call sites in ovos-skill-alerts that hang
    forever with the OVOSSkill default num_retries=-1."""

    def initialize(self):
        self.add_event("unittest.ask_something", self.handle_ask)

    def handle_ask(self, message: Message):
        ans = self.get_response("give me an answer")
        self.bus.emit(message.forward("unittest.ask_something.done",
                                      {"answer": ans}))


class TestNestedSpeakDialogWait(unittest.TestCase):
    """Reproduces ovos-skill-alerts#138 mechanism (a)."""

    def setUp(self):
        LOG.set_level("ERROR")

    def tearDown(self):
        LOG.set_level("CRITICAL")

    def test_nested_speak_dialog_wait_completes_quickly(self):
        mc = get_minicroft([NESTED_SKILL_ID],
                           extra_skills={NESTED_SKILL_ID: NestedSpeakSkill})
        try:
            done = []
            mc.bus.on("unittest.nested_speak.done", lambda m: done.append(m))

            start = time.time()
            mc.bus.emit(Message("unittest.nested_speak"))
            deadline = start + 10
            while not done and time.time() < deadline:
                time.sleep(0.05)
            elapsed = time.time() - start

            self.assertTrue(done, "nested speak_dialog(wait=True) never "
                                  "completed within 10s (would be a hang "
                                  "under the unfixed FakeBus, capped at "
                                  "2x15s=30s upstream)")
            # each wait_while_speaking should resolve off the mock-TTS's
            # ~0.1s unduck timer, not burn its full 15s default timeout.
            self.assertLess(elapsed, 2.0,
                            f"nested speak_dialog(wait=True) took {elapsed:.2f}s; "
                            f"expected < 2s if audio_output_end fires promptly "
                            f"for the nested speak too")
        finally:
            mc.stop()


class TestUnansweredGetResponse(unittest.TestCase):
    """Reproduces ovos-skill-alerts#138 mechanism (b)."""

    def setUp(self):
        LOG.set_level("ERROR")

    def tearDown(self):
        LOG.set_level("CRITICAL")

    def test_unanswered_get_response_resolves_promptly(self):
        mc = get_minicroft([GETRESPONSE_SKILL_ID],
                           extra_skills={GETRESPONSE_SKILL_ID: GetResponseSkill})
        try:
            done = []
            mc.bus.on("unittest.ask_something.done", lambda m: done.append(m))

            start = time.time()
            mc.bus.emit(Message("unittest.ask_something"))
            deadline = start + 10
            while not done and time.time() < deadline:
                time.sleep(0.05)
            elapsed = time.time() - start

            self.assertTrue(done, "get_response() with no injected answer "
                                  "never resolved within 10s - this is the "
                                  "unbounded OVOSSkill._wait_response() hang")
            self.assertIsNone(done[0].data.get("answer"),
                              "unanswered get_response() should resolve to "
                              "None (aborted), not a fabricated answer")
            self.assertLess(elapsed, 5.0,
                            f"unanswered get_response() took {elapsed:.2f}s; "
                            f"expected the watchdog abort well under 5s")
        finally:
            mc.stop()


class GetResponseSkillNamed(OVOSSkill):
    """Same as GetResponseSkill but the class doesn't hardcode skill_id, so
    two instances can be registered as two distinct skills in one MiniCroft
    (extra_skills maps skill_id -> class, and OVOSSkill picks up skill_id
    from the registration)."""

    def initialize(self):
        # Skill-scoped event name: "unittest.ask_something" (shared, unscoped)
        # would make BOTH skill instances react to a single emit, since
        # add_event registers on the bus's global topic namespace, not per
        # skill. Two skills firing off the SAME incoming message defeats the
        # point of the concurrency test (it stops being "two independent
        # requests," it becomes one request fanned out to both skills).
        self.add_event(f"{self.skill_id}.ask_something", self.handle_ask)

    def handle_ask(self, message: Message):
        ans = self.get_response("give me an answer")
        self.bus.emit(message.forward(f"{self.skill_id}.ask_something.done",
                                      {"answer": ans}))


class TestConcurrentGetResponseWatchdogScoping(unittest.TestCase):
    """Whitebox: directly drive the enable/disable protocol messages the way
    OVOSSkill.get_response() emits them, without waiting on real timers, to
    pin the exact defect found in review: a flat timer list lets one skill's
    `.disable` wipe out every other skill's still-armed watchdog."""

    def setUp(self):
        LOG.set_level("ERROR")

    def tearDown(self):
        LOG.set_level("CRITICAL")

    @staticmethod
    def _enable_message(skill_id: str, session_id: str) -> Message:
        sess = Session(session_id=session_id)
        return Message("skill.converse.get_response.enable",
                       {"skill_id": skill_id},
                       {"session": sess.serialize()})

    @staticmethod
    def _disable_message(skill_id: str, session_id: str) -> Message:
        sess = Session(session_id=session_id)
        return Message("skill.converse.get_response.disable",
                       {"skill_id": skill_id},
                       {"session": sess.serialize()})

    def test_disabling_one_skill_does_not_cancel_another_skills_watchdog(self):
        mc = get_minicroft([])
        try:
            mc.bus.emit(self._enable_message("skillA", "sessA"))
            mc.bus.emit(self._enable_message("skillB", "sessB"))
            # Both must be armed before either is disabled.
            self.assertEqual(2, len(mc._get_response_timers))

            mc.bus.emit(self._disable_message("skillA", "sessA"))

            # THE DEFECT (flat list): this drops to 0 - skillB's watchdog is
            # gone too. THE FIX (keyed by (skill_id, session_id)): only
            # skillA's entry is removed, skillB's stays armed.
            self.assertEqual(
                1, len(mc._get_response_timers),
                "disabling skillA's get_response must not cancel skillB's "
                "still-pending watchdog")
            remaining_key = next(iter(mc._get_response_timers))
            self.assertEqual(("skillB", "sessB"), remaining_key)
            self.assertTrue(mc._get_response_timers[remaining_key].is_alive())
        finally:
            mc.stop()

    def test_unanswered_skill_still_aborted_when_another_skill_answers_first(self):
        """End-to-end concurrent scenario: skill A gets answered quickly via
        a real injected utterance; skill B never gets an answer. B's own
        watchdog must still fire and abort it - A finishing first must not
        silently leave B hanging."""
        mc = get_minicroft(
            [CONCURRENT_SKILL_A, CONCURRENT_SKILL_B],
            extra_skills={CONCURRENT_SKILL_A: GetResponseSkillNamed,
                         CONCURRENT_SKILL_B: GetResponseSkillNamed})
        try:
            done_a, done_b = [], []
            mc.bus.on(f"{CONCURRENT_SKILL_A}.ask_something.done",
                     lambda m: done_a.append(m))
            mc.bus.on(f"{CONCURRENT_SKILL_B}.ask_something.done",
                     lambda m: done_b.append(m))

            sess_a = Session(session_id="concurrent-sess-a")
            sess_b = Session(session_id="concurrent-sess-b")

            # Kick off both get_response() calls concurrently on separate
            # threads, mirroring two independent skills/sessions in flight
            # in a real fleet-style MiniCroft at the same time.
            t_a = threading.Thread(
                target=lambda: mc.bus.emit(Message(
                    f"{CONCURRENT_SKILL_A}.ask_something", {},
                    {"session": sess_a.serialize(), "skill_id": CONCURRENT_SKILL_A})))
            t_b = threading.Thread(
                target=lambda: mc.bus.emit(Message(
                    f"{CONCURRENT_SKILL_B}.ask_something", {},
                    {"session": sess_b.serialize(), "skill_id": CONCURRENT_SKILL_B})))
            t_a.start()
            t_b.start()

            # Give both get_response() calls a moment to reach their
            # listening state, then answer ONLY A - B is deliberately left
            # unanswered so its own watchdog has to do the work.
            time.sleep(0.3)
            mc.bus.emit(Message(
                f"{CONCURRENT_SKILL_A}.converse.get_response",
                {"utterances": ["forty two"]},
                {"session": sess_a.serialize()}))

            t_a.join(timeout=10)
            t_b.join(timeout=10)

            deadline = time.time() + 10
            while (not done_a or not done_b) and time.time() < deadline:
                time.sleep(0.05)

            self.assertTrue(done_a, "skill A's answered get_response() "
                                    "never resolved")
            self.assertTrue(done_b, "skill B's unanswered get_response() "
                                    "never resolved - its watchdog was "
                                    "cancelled by skill A's .disable "
                                    "(the flat-list defect)")
            self.assertEqual("forty two", done_a[0].data.get("answer"),
                            "skill A's legitimate answer must survive "
                            "skill B's watchdog/abort handling untouched")
            self.assertIsNone(done_b[0].data.get("answer"),
                              "skill B was never answered, so it must "
                              "resolve to None via its OWN watchdog abort")
        finally:
            mc.stop()


if __name__ == "__main__":
    unittest.main()
