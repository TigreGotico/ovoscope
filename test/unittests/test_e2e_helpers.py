"""Fast unit tests for the engine-agnostic helpers in ``ovoscope.e2e``.

These do not spin up MiniCroft — they exercise the standalone helpers
against a plain ``FakeBus`` so they run in well under a second and can
catch regressions in the helper logic itself.
"""
import threading
import unittest
from unittest.mock import patch

from ovos_bus_client.message import Message
from ovos_utils.fakebus import FakeBus

from ovoscope.e2e import (
    detach_intent,
    detach_skill,
    make_session,
    make_utterance_message,
    register_adapt_vocab,
    register_padatious_entity,
    register_padatious_intent,
    wait_for_failure,
    wait_for_match,
)


class TestMakeSession(unittest.TestCase):
    def test_only_session_id(self):
        s = make_session("abc")
        d = s.serialize()
        self.assertEqual(d["session_id"], "abc")

    def test_pipeline_override(self):
        s = make_session("x", pipeline=["foo", "bar"])
        self.assertEqual(s.serialize()["pipeline"], ["foo", "bar"])

    def test_blacklists_round_trip(self):
        s = make_session(
            "y",
            blacklisted_intents=["sk:hi"],
            blacklisted_skills=["sk"],
        )
        data = s.serialize()
        self.assertEqual(data["blacklisted_intents"], ["sk:hi"])
        self.assertEqual(data["blacklisted_skills"], ["sk"])


class TestMakeUtteranceMessage(unittest.TestCase):
    def test_no_session(self):
        m = make_utterance_message("hello there")
        self.assertEqual(m.msg_type, "recognizer_loop:utterance")
        self.assertEqual(m.data["utterances"], ["hello there"])
        self.assertEqual(m.data["lang"], "en-US")
        self.assertNotIn("session", m.context)

    def test_with_session(self):
        s = make_session("sid", pipeline=["pipeline-x"])
        m = make_utterance_message("hi", session=s)
        self.assertIn("session", m.context)
        self.assertEqual(m.context["session"]["pipeline"], ["pipeline-x"])

    def test_lang_propagates(self):
        m = make_utterance_message("oi", lang="pt-PT")
        self.assertEqual(m.data["lang"], "pt-PT")


class TestRegistrationShims(unittest.TestCase):
    def setUp(self):
        self.bus = FakeBus()
        self.captured = []
        # Wildcard-ish: subscribe to each known type and append.
        for t in (
            "padatious:register_intent",
            "padatious:register_entity",
            "register_vocab",
            "detach_intent",
            "detach_skill",
        ):
            self.bus.on(t, self._append)

    def _append(self, msg):
        self.captured.append(msg)

    def _types(self):
        return [m.msg_type for m in self.captured]

    def test_register_padatious_intent_emits_event(self):
        register_padatious_intent(
            self.bus, "sk:hi", ["hi", "hello"], skill_id="sk", settle=0.0
        )
        self.assertEqual(self._types(), ["padatious:register_intent"])
        d = self.captured[0].data
        self.assertEqual(d["name"], "sk:hi")
        self.assertEqual(d["samples"], ["hi", "hello"])
        self.assertEqual(d["lang"], "en-US")

    def test_register_padatious_intent_stamps_context_skill_id(self):
        register_padatious_intent(
            self.bus, "sk:hi", ["hi"], skill_id="sk", settle=0.0
        )
        self.assertEqual(self.captured[0].context["skill_id"], "sk")

    def test_register_padatious_intent_requires_skill_id(self):
        # OVOS-INTENT-4 §3.1/§3.2: no derivation fallback — omitting the
        # keyword is a caller bug, not a message ovoscope silently sends
        # unattributed.
        with self.assertRaises(TypeError):
            register_padatious_intent(self.bus, "sk:hi", ["hi"], settle=0.0)

    def test_register_padatious_entity_emits_event(self):
        register_padatious_entity(
            self.bus, "item", ["milk", "bread"], skill_id="sk", settle=0.0
        )
        self.assertEqual(self._types(), ["padatious:register_entity"])
        self.assertEqual(self.captured[0].data["samples"], ["milk", "bread"])

    def test_register_padatious_entity_stamps_context_skill_id(self):
        register_padatious_entity(
            self.bus, "item", ["milk"], skill_id="sk", settle=0.0
        )
        self.assertEqual(self.captured[0].context["skill_id"], "sk")

    def test_register_padatious_entity_requires_skill_id(self):
        with self.assertRaises(TypeError):
            register_padatious_entity(self.bus, "item", ["milk"], settle=0.0)

    def test_register_adapt_vocab_emits_one_event_per_word(self):
        register_adapt_vocab(
            self.bus, "sk:Light", ["light", "lamp", "bulb"],
            skill_id="sk", settle=0.0,
        )
        self.assertEqual(self._types(), ["register_vocab"] * 3)
        values = [m.data["entity_value"] for m in self.captured]
        self.assertEqual(values, ["light", "lamp", "bulb"])
        for m in self.captured:
            self.assertEqual(m.data["entity_type"], "sk:Light")

    def test_register_adapt_vocab_stamps_context_skill_id(self):
        register_adapt_vocab(
            self.bus, "sk:Light", ["light"], skill_id="sk", settle=0.0
        )
        self.assertEqual(self.captured[0].context["skill_id"], "sk")

    def test_register_adapt_vocab_requires_skill_id(self):
        # Adapt vocab names are conventionally unscoped ("Fruit", "greeting")
        # so there is nothing to derive a skill_id from — the caller MUST
        # supply one or the vocab registers ownerless and detach_skill can
        # never remove it.
        with self.assertRaises(TypeError):
            register_adapt_vocab(self.bus, "Fruit", ["apple"], settle=0.0)

    def test_detach_helpers(self):
        detach_intent(self.bus, "sk:hi", skill_id="sk", settle=0.0)
        detach_skill(self.bus, "sk", settle=0.0)
        self.assertEqual(self._types(), ["detach_intent", "detach_skill"])
        self.assertEqual(self.captured[0].data["intent_name"], "sk:hi")
        self.assertEqual(self.captured[1].data["skill_id"], "sk")

    def test_detach_intent_stamps_context_skill_id(self):
        detach_intent(self.bus, "sk:hi", skill_id="sk", settle=0.0)
        self.assertEqual(self.captured[0].context["skill_id"], "sk")

    def test_detach_intent_requires_skill_id(self):
        with self.assertRaises(TypeError):
            detach_intent(self.bus, "sk:hi", settle=0.0)

    def test_detach_skill_stamps_context_skill_id(self):
        # This is the message ``E2EPipelineHarness.setUp()`` emits to isolate
        # tests; a §3.1/§3.2-conformant plugin drops it without this.
        detach_skill(self.bus, "sk", settle=0.0)
        self.assertEqual(self.captured[0].context["skill_id"], "sk")


class TestWaitHelpers(unittest.TestCase):
    def setUp(self):
        self.bus = FakeBus()

    def _emit_after(self, delay, msg):
        timer = threading.Timer(delay, lambda: self.bus.emit(msg))
        timer.daemon = True
        timer.start()
        return timer

    def test_wait_for_match_returns_message(self):
        self._emit_after(0.05, Message("sk:hello", {"x": 1}))
        msg = wait_for_match(self.bus, ["sk:hello"], timeout=1.0)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.msg_type, "sk:hello")

    def test_wait_for_match_returns_none_on_failure(self):
        self._emit_after(0.05, Message("complete_intent_failure", {}))
        msg = wait_for_match(self.bus, ["sk:hello"], timeout=1.0)
        self.assertIsNone(msg)

    def test_wait_for_match_returns_none_on_timeout(self):
        msg = wait_for_match(self.bus, ["sk:nope"], timeout=0.1)
        self.assertIsNone(msg)

    def test_wait_for_failure_true_when_event_fires(self):
        self._emit_after(0.05, Message("complete_intent_failure", {}))
        self.assertTrue(wait_for_failure(self.bus, timeout=1.0))

    def test_wait_for_failure_false_on_timeout(self):
        self.assertFalse(wait_for_failure(self.bus, timeout=0.1))


class TestHarnessClassValidation(unittest.TestCase):
    """Without spinning MiniCroft, ensure missing PIPELINE_ID/CONFIG_KEY skips
    rather than crashing.  This protects against accidental abstract instantiation.
    """

    def test_missing_ids_raises_skip(self):
        from ovoscope.e2e import E2EPipelineHarness

        class _Bad(E2EPipelineHarness):
            pass

        with self.assertRaises(unittest.SkipTest):
            _Bad.setUpClass()


if __name__ == "__main__":
    unittest.main()
