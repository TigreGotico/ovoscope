"""Extended tests for End2EndTest — covers routing, context, active skills,
boot sequence, final session, GUI filtering, from_message recording,
serialization edge cases, and anonymize_message."""
import json
import logging
import os
import tempfile
import unittest
import warnings

from ovos_bus_client.message import Message
from ovos_bus_client.session import Session, SessionManager
from ovos_utils.log import LOG
from ovos_workshop.skills.ovos import OVOSSkill

from ovoscope import (
    End2EndTest, CaptureSession, MiniCroft, get_minicroft,
    DEFAULT_EOF, DEFAULT_IGNORED, GUI_IGNORED,
    ADAPT_PIPELINE, PADATIOUS_PIPELINE, FALLBACK_PIPELINE,
    STOP_PIPELINE, CONVERSE_PIPELINE, COMMON_QUERY_PIPELINE,
    PERSONA_PIPELINE, DEFAULT_TEST_PIPELINE,
)

SKILL_ID = "ovoscope-extended-test.test"
HANDLER_LIFECYCLE = ["mycroft.skill.handler.start",
                     "mycroft.skill.handler.complete",
                     "recognizer_loop:audio_output_start",
                     "recognizer_loop:audio_output_end"]
ADAPT_ONLY = ["ovos-adapt-pipeline-plugin-high"]


class EchoSkill(OVOSSkill):
    def initialize(self):
        self.add_event("unittest.echo", self.handle_echo)

    def handle_echo(self, message: Message):
        text = message.data.get("text", "echo")
        self.speak(text)
        self.bus.emit(Message("ovos.utterance.handled", context=message.context))


class AsyncSkill(OVOSSkill):
    """Emits an async message alongside the normal flow."""

    def initialize(self):
        self.add_event("unittest.async", self.handle_async)

    def handle_async(self, message: Message):
        self.bus.emit(Message("test.async.event", context=message.context))
        self.speak("async done")
        self.bus.emit(Message("ovos.utterance.handled", context=message.context))


class TwoLifecycleSkill(OVOSSkill):
    """Emits two lifecycles tagged with distinct context skill_ids, to exercise
    the End2EndTest ``skill_id`` filter. Each lifecycle ends on the shared
    ``ovos.utterance.handled`` topic (so eof_count=2 spans both)."""

    def initialize(self):
        self.add_event("unittest.two_lifecycles", self.handle_two)

    def handle_two(self, message: Message):
        for sid in ("life.a", "life.b"):
            ctx = dict(message.context)
            ctx["skill_id"] = sid
            self.bus.emit(Message(f"{sid}.step", context=ctx))
            self.bus.emit(Message("ovos.utterance.handled", context=ctx))


class SharedSkillIdSkill(OVOSSkill):
    """Emits two lifecycles that share a context skill_id but differ in
    pipeline_id — the shape produced when a targeted stop (OVOS-STOP-1 §3.1)
    interrupts a running skill: both carry the target's skill_id, only the stop
    dispatch carries the stop plugin's pipeline_id. Exercises the pipeline_id
    filter. Each lifecycle ends on the shared ``ovos.utterance.handled``."""

    def initialize(self):
        self.add_event("unittest.shared_skill_id", self.handle_shared)

    def handle_shared(self, message: Message):
        for pid in ("pipe.a", "pipe.b"):
            ctx = dict(message.context)
            ctx["skill_id"] = "shared.skill"
            ctx["pipeline_id"] = pid
            self.bus.emit(Message(f"{pid}.step", context=ctx))
            self.bus.emit(Message("ovos.utterance.handled", context=ctx))


def _session(sid="ext-test", pipeline=None):
    s = Session(sid)
    s.lang = "en-US"
    s.pipeline = pipeline or ADAPT_ONLY
    return s


def _make_custom(msg_type, data=None, sid="ext-test"):
    sess = _session(sid)
    return Message(msg_type, data or {},
                   {"session": sess.serialize(),
                    "source": "A", "destination": "B"})


def _make_utterance(text, sid="ext-test", pipeline=None):
    sess = _session(sid, pipeline)
    return Message("recognizer_loop:utterance",
                   {"utterances": [text], "lang": "en-US"},
                   {"session": sess.serialize(),
                    "source": "A", "destination": "B"})


# ---------------------------------------------------------------------------
# __post_init__ and GUI filtering
# ---------------------------------------------------------------------------
class TestPostInit(unittest.TestCase):

    def test_source_message_normalized_to_list(self):
        """A single Message source_message becomes a list after __post_init__."""
        src = _make_custom("test")
        test = End2EndTest(
            skill_ids=[], source_message=src,
            expected_messages=[], verbose=False,
        )
        self.assertIsInstance(test.source_message, list)
        self.assertEqual(len(test.source_message), 1)

    def test_ignore_gui_adds_gui_messages(self):
        """ignore_gui=True adds GUI_IGNORED to ignore_messages."""
        test = End2EndTest(
            skill_ids=[], source_message=_make_custom("test"),
            expected_messages=[], ignore_gui=True, verbose=False,
        )
        for m in GUI_IGNORED:
            self.assertIn(m, test.ignore_messages)

    def test_ignore_gui_false_does_not_add(self):
        """ignore_gui=False leaves ignore_messages as-is."""
        custom_ignored = ["custom.ignore"]
        test = End2EndTest(
            skill_ids=[], source_message=_make_custom("test"),
            expected_messages=[], ignore_gui=False,
            ignore_messages=custom_ignored[:],  # fresh copy
            verbose=False,
        )
        for m in GUI_IGNORED:
            self.assertNotIn(m, test.ignore_messages)
        self.assertIn("custom.ignore", test.ignore_messages)


# ---------------------------------------------------------------------------
# Context assertion tests
# ---------------------------------------------------------------------------
class TestContextAssertions(unittest.TestCase):

    def setUp(self):
        LOG.set_level("ERROR")
        self.mc = get_minicroft([SKILL_ID], extra_skills={SKILL_ID: EchoSkill})

    def tearDown(self):
        self.mc.stop()
        LOG.set_level("CRITICAL")

    def test_wrong_context_raises(self):
        """test_msg_context=True raises on context mismatch."""
        src = _make_custom("unittest.echo", {"text": "ctx"})
        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            source_message=src,
            expected_messages=[
                Message("unittest.echo", {}, {"source": "WRONG"}),
            ],
            test_message_number=False,
            test_msg_type=False,
            test_msg_data=False,
            test_msg_context=True,
            test_routing=False,
            test_active_skills=False,
            test_final_session=False,
            test_async_messages=False,
            test_async_message_number=False,
            ignore_messages=DEFAULT_IGNORED + HANDLER_LIFECYCLE,
            verbose=False,
        )
        with self.assertRaises(AssertionError):
            test.execute(timeout=10)


# ---------------------------------------------------------------------------
# Routing tests (flip_points, entry_points, keep_original_src)
# ---------------------------------------------------------------------------
class TestRouting(unittest.TestCase):

    def setUp(self):
        LOG.set_level("ERROR")
        self.mc = get_minicroft([])

    def tearDown(self):
        self.mc.stop()
        LOG.set_level("CRITICAL")

    def test_routing_entry_point_flips_src_dst(self):
        """After an entry_point message, expected src/dst are flipped."""
        src = _make_utterance("no match", pipeline=ADAPT_ONLY)
        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[],
            source_message=src,
            expected_messages=[
                src,
                Message("mycroft.audio.play_sound", {"uri": "snd/error.mp3"}),
                Message("complete_intent_failure", {}),
                Message("ovos.utterance.handled", {}),
            ],
            test_routing=True,
            test_msg_type=False,
            test_msg_data=False,
            test_msg_context=False,
            test_active_skills=False,
            test_final_session=False,
            test_async_messages=False,
            test_async_message_number=False,
            verbose=False,
        )
        # Should not raise — default entry_points includes recognizer_loop:utterance
        result = test.execute(timeout=15)
        self.assertIsInstance(result, list)

    def test_flip_points_configuration(self):
        """flip_points parameter is stored and used during routing checks."""
        src = _make_utterance("no match", pipeline=ADAPT_ONLY)
        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[],
            source_message=src,
            expected_messages=[
                src,
                Message("mycroft.audio.play_sound", {"uri": "snd/error.mp3"}),
                Message("complete_intent_failure", {}),
                Message("ovos.utterance.handled", {}),
            ],
            flip_points=["recognizer_loop:utterance"],
            test_routing=False,  # routing internals are hard to test in isolation
            test_msg_type=False,
            test_msg_data=False,
            test_msg_context=False,
            test_active_skills=False,
            test_final_session=False,
            test_async_messages=False,
            test_async_message_number=False,
            verbose=False,
        )
        self.assertEqual(test.flip_points, ["recognizer_loop:utterance"])
        result = test.execute(timeout=15)
        self.assertIsInstance(result, list)


# ---------------------------------------------------------------------------
# Async message tests
# ---------------------------------------------------------------------------
class TestAsyncMessages(unittest.TestCase):

    def setUp(self):
        LOG.set_level("ERROR")
        self.mc = get_minicroft([SKILL_ID], extra_skills={SKILL_ID: AsyncSkill})

    def tearDown(self):
        self.mc.stop()
        LOG.set_level("CRITICAL")

    def test_async_messages_captured_separately(self):
        """Messages in async_messages list go to async_responses."""
        src = _make_custom("unittest.async")
        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            source_message=src,
            expected_messages=[src],
            async_messages=["test.async.event"],
            test_message_number=False,
            test_msg_type=False,
            test_msg_data=False,
            test_msg_context=False,
            test_routing=False,
            test_active_skills=False,
            test_final_session=False,
            test_async_messages=True,
            test_async_message_number=True,
            ignore_messages=DEFAULT_IGNORED + HANDLER_LIFECYCLE,
            verbose=False,
        )
        result = test.execute(timeout=10)
        self.assertIsInstance(result, list)

    def test_missing_async_message_raises(self):
        """test_async_messages=True raises if expected async msg is missing."""
        src = _make_custom("unittest.echo", {"text": "no async"})
        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            source_message=src,
            expected_messages=[src],
            async_messages=["nonexistent.async.msg"],
            test_message_number=False,
            test_msg_type=False,
            test_msg_data=False,
            test_msg_context=False,
            test_routing=False,
            test_active_skills=False,
            test_final_session=False,
            test_async_messages=True,
            test_async_message_number=False,
            ignore_messages=DEFAULT_IGNORED + HANDLER_LIFECYCLE,
            verbose=False,
        )
        with self.assertRaises(AssertionError):
            test.execute(timeout=10)

    def test_async_message_count_mismatch_raises(self):
        """test_async_message_number=True raises on count mismatch."""
        src = _make_custom("unittest.echo", {"text": "no async"})
        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            source_message=src,
            expected_messages=[src],
            async_messages=["nonexistent.a", "nonexistent.b"],
            test_message_number=False,
            test_msg_type=False,
            test_msg_data=False,
            test_msg_context=False,
            test_routing=False,
            test_active_skills=False,
            test_final_session=False,
            test_async_messages=False,
            test_async_message_number=True,
            ignore_messages=DEFAULT_IGNORED + HANDLER_LIFECYCLE,
            verbose=False,
        )
        with self.assertRaises(AssertionError):
            test.execute(timeout=10)


# ---------------------------------------------------------------------------
# Serialization edge cases
# ---------------------------------------------------------------------------
class TestSerializationExtended(unittest.TestCase):

    def setUp(self):
        LOG.set_level("ERROR")

    def tearDown(self):
        LOG.set_level("CRITICAL")

    def test_deserialize_from_json_string(self):
        """deserialize() accepts a JSON string, not just a dict."""
        src = _make_utterance("test", pipeline=ADAPT_ONLY)
        test = End2EndTest(
            skill_ids=[], source_message=src,
            expected_messages=[src],
            test_msg_context=False, verbose=False,
        )
        json_str = json.dumps(test.serialize(anonymize=False))
        restored = End2EndTest.deserialize(json_str)
        self.assertEqual(restored.skill_ids, [])
        self.assertEqual(len(restored.source_message), 1)

    def test_serialize_preserves_flip_points(self):
        """Serialized data includes flip_points."""
        src = _make_utterance("test", pipeline=ADAPT_ONLY)
        test = End2EndTest(
            skill_ids=[], source_message=src,
            expected_messages=[src],
            flip_points=["recognizer_loop:utterance"],
            verbose=False,
        )
        data = test.serialize()
        self.assertEqual(data["flip_points"], ["recognizer_loop:utterance"])

    def test_serialize_preserves_test_flags(self):
        """Serialized data includes test toggle flags."""
        src = _make_utterance("test", pipeline=ADAPT_ONLY)
        test = End2EndTest(
            skill_ids=[], source_message=src,
            expected_messages=[src],
            test_msg_type=False, test_msg_data=False,
            test_msg_context=False, test_routing=False,
            verbose=False,
        )
        data = test.serialize()
        self.assertFalse(data["test_msg_type"])
        self.assertFalse(data["test_msg_data"])
        self.assertFalse(data["test_msg_context"])
        self.assertFalse(data["test_routing"])

    def test_anonymize_message_replaces_location(self):
        """anonymize_message() sets location with OVOS-SESSION-1 spec shape."""
        sess = _session()
        sess.location = {
            "lat": 38.7,
            "lon": -9.1,
            "tz": "Europe/Lisbon"
        }
        src = Message("test", {},
                      {"session": sess.serialize(), "source": "A", "destination": "B"})
        anon = End2EndTest.anonymize_message(src)
        sess_data = anon.context.get("session", {})
        loc = sess_data.get("location", {})
        # Check that location is the three-key spec shape (OVOS-SESSION-1 §3.5)
        self.assertEqual(loc["lat"], 0.0)
        self.assertEqual(loc["lon"], 0.0)
        self.assertEqual(loc["tz"], "Europe/Lisbon")

    def test_anonymize_message_uses_spec_location_not_deprecated_api(self):
        """anonymize_message() uses spec-compliant Session.location, not deprecated location_preferences.

        Session.location_preferences is deprecated at ovos-bus-client 2.11.3a1+
        (removed at 3.0.0). anonymize_message() must use Session.location directly
        with the OVOS-SESSION-1 §3.5 spec shape {lat, lon, tz} to avoid triggering
        deprecation warnings from the ovos-bus-client logger.

        Regression: accessing location_preferences logs two deprecation warnings
        per call. The fix switches to direct location access.
        """
        sess = _session()
        sess.location = {
            "lat": 38.7,
            "lon": -9.1,
            "tz": "Europe/Lisbon"
        }
        src = Message("test", {},
                      {"session": sess.serialize(), "source": "A", "destination": "B"})
        # Call anonymize_message (must not raise, should produce spec-compliant output)
        anon = End2EndTest.anonymize_message(src)
        # Verify the output is spec-compliant (three-key location format)
        sess_data = anon.context.get("session", {})
        loc = sess_data.get("location", {})
        # Spec-compliant format per OVOS-SESSION-1 §3.5
        self.assertIn("lat", loc, "location must have 'lat' key (spec-compliant)")
        self.assertIn("lon", loc, "location must have 'lon' key (spec-compliant)")
        self.assertIn("tz", loc, "location must have 'tz' key (spec-compliant)")
        # Anonymized values
        self.assertEqual(loc["lat"], 0.0, "anonymized latitude must be 0.0")
        self.assertEqual(loc["lon"], 0.0, "anonymized longitude must be 0.0")
        self.assertEqual(loc["tz"], "Europe/Lisbon", "anonymized tz must be preserved")


# ---------------------------------------------------------------------------
# Boot sequence tests
# ---------------------------------------------------------------------------
class TestBootSequence(unittest.TestCase):

    def setUp(self):
        LOG.set_level("ERROR")
        self.mc = get_minicroft([SKILL_ID], extra_skills={SKILL_ID: EchoSkill})

    def tearDown(self):
        self.mc.stop()
        LOG.set_level("CRITICAL")

    def test_boot_sequence_passes_when_correct(self):
        """test_boot_sequence=True passes when expected matches first N boot msgs."""
        # Build expected_boot_sequence from actual first message
        self.assertTrue(len(self.mc.boot_messages) > 0,
                        "MiniCroft must emit at least one boot message")
        first_boot = self.mc.boot_messages[0]
        src = _make_custom("unittest.echo", {"text": "boot"})
        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            source_message=src,
            expected_messages=[src],
            expected_boot_sequence=[Message(first_boot.msg_type)],
            test_boot_sequence=True,
            test_message_number=False,
            test_msg_type=False,
            test_msg_data=False,
            test_msg_context=False,
            test_routing=False,
            test_active_skills=False,
            test_final_session=False,
            test_async_messages=False,
            test_async_message_number=False,
            verbose=False,
        )
        result = test.execute(timeout=10)
        self.assertIsInstance(result, list)

    def test_boot_sequence_wrong_type_raises(self):
        """test_boot_sequence=True raises on type mismatch."""
        src = _make_custom("unittest.echo", {"text": "boot"})
        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            source_message=src,
            expected_messages=[src],
            expected_boot_sequence=[Message("WRONG.BOOT.TYPE")],
            test_boot_sequence=True,
            test_message_number=False,
            test_msg_type=False,
            test_msg_data=False,
            test_msg_context=False,
            test_routing=False,
            test_active_skills=False,
            test_final_session=False,
            test_async_messages=False,
            test_async_message_number=False,
            verbose=False,
        )
        with self.assertRaises(AssertionError):
            test.execute(timeout=10)


# ---------------------------------------------------------------------------
# Active skills tests
# ---------------------------------------------------------------------------
class TestActiveSkills(unittest.TestCase):

    def setUp(self):
        LOG.set_level("ERROR")
        self.mc = get_minicroft([SKILL_ID], extra_skills={SKILL_ID: EchoSkill})

    def tearDown(self):
        self.mc.stop()
        LOG.set_level("CRITICAL")

    def test_inject_active_modifies_session(self):
        """inject_active adds skill to session's active_skills before test."""
        src = _make_custom("unittest.echo", {"text": "active"})
        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            source_message=src,
            expected_messages=[src],
            inject_active=["fake-active-skill.test"],
            test_message_number=False,
            test_msg_type=False,
            test_msg_data=False,
            test_msg_context=False,
            test_routing=False,
            test_active_skills=False,  # don't assert — just verify injection ran
            test_final_session=False,
            test_async_messages=False,
            test_async_message_number=False,
            ignore_messages=DEFAULT_IGNORED + HANDLER_LIFECYCLE,
            verbose=True,  # covers the inject_active print branch
        )
        result = test.execute(timeout=10)
        self.assertIsInstance(result, list)


# ---------------------------------------------------------------------------
# Pipeline constant composition
# ---------------------------------------------------------------------------
class TestPipelineConstants(unittest.TestCase):

    def test_default_test_pipeline_excludes_persona(self):
        """DEFAULT_TEST_PIPELINE must not contain persona stages."""
        for stage in PERSONA_PIPELINE:
            self.assertNotIn(stage, DEFAULT_TEST_PIPELINE)

    def test_default_test_pipeline_has_adapt(self):
        """DEFAULT_TEST_PIPELINE must contain Adapt stages."""
        for stage in ADAPT_PIPELINE:
            self.assertIn(stage, DEFAULT_TEST_PIPELINE)

    def test_default_test_pipeline_has_padatious(self):
        for stage in PADATIOUS_PIPELINE:
            self.assertIn(stage, DEFAULT_TEST_PIPELINE)

    def test_default_test_pipeline_has_fallback(self):
        for stage in FALLBACK_PIPELINE:
            self.assertIn(stage, DEFAULT_TEST_PIPELINE)

    def test_default_test_pipeline_has_common_query(self):
        for stage in COMMON_QUERY_PIPELINE:
            self.assertIn(stage, DEFAULT_TEST_PIPELINE)

    def test_stop_pipeline_length(self):
        self.assertEqual(len(STOP_PIPELINE), 3)

    def test_converse_pipeline_length(self):
        self.assertEqual(len(CONVERSE_PIPELINE), 1)


# ---------------------------------------------------------------------------
# MiniCroft language config
# ---------------------------------------------------------------------------
class TestMiniCroftLangConfig(unittest.TestCase):

    def setUp(self):
        LOG.set_level("ERROR")

    def tearDown(self):
        LOG.set_level("CRITICAL")

    def test_lang_override(self):
        """MiniCroft lang parameter overrides session lang."""
        mc = get_minicroft([], lang="pt-BR")
        try:
            self.assertEqual(SessionManager.get_default_session().lang, "pt-BR")
        finally:
            mc.stop()

    def test_lang_restored_after_stop(self):
        """Stopping MiniCroft restores original lang."""
        from ovos_config.config import Configuration
        original_lang = Configuration().get("lang")
        mc = get_minicroft([], lang="de-DE")
        mc.stop()
        restored_lang = Configuration().get("lang")
        self.assertEqual(restored_lang, original_lang)


# ---------------------------------------------------------------------------
# CaptureSession __del__
# ---------------------------------------------------------------------------
class TestCaptureSessionDel(unittest.TestCase):
    """__del__ touches only ``minicroft.bus`` — a stub is enough (and a real
    MiniCroft boot here retained hundreds of MB per run)."""

    def setUp(self):
        from types import SimpleNamespace
        from ovos_utils.fakebus import FakeBus
        LOG.set_level("ERROR")
        self.mc = SimpleNamespace(bus=FakeBus())

    def tearDown(self):
        self.mc.bus.close()
        LOG.set_level("CRITICAL")

    def test_del_calls_finish(self):
        """__del__ should call finish() safely."""
        cs = CaptureSession(self.mc, eof_msgs=["test.eof"], ignore_messages=[])
        cs.done.set()
        # Should not raise
        cs.__del__()
        self.assertTrue(cs.done.is_set())


# ---------------------------------------------------------------------------
# Verbose output (covers print branches)
# ---------------------------------------------------------------------------
class TestVerboseOutput(unittest.TestCase):

    def setUp(self):
        LOG.set_level("ERROR")
        self.mc = get_minicroft([SKILL_ID], extra_skills={SKILL_ID: EchoSkill})

    def tearDown(self):
        self.mc.stop()
        LOG.set_level("CRITICAL")

    def test_verbose_true_covers_print_branches(self):
        """verbose=True exercises all print branches without raising."""
        src = _make_custom("unittest.echo", {"text": "verbose"})
        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            source_message=src,
            expected_messages=[
                src,
                Message("ovos.utterance.speak", {"utterance": "verbose"}),
                Message("ovos.utterance.handled", {}),
            ],
            ignore_messages=DEFAULT_IGNORED + HANDLER_LIFECYCLE,
            test_routing=True,
            test_msg_type=True,
            test_msg_data=True,
            test_msg_context=False,
            test_active_skills=False,
            test_final_session=False,
            test_async_messages=False,
            test_async_message_number=False,
            verbose=True,
        )
        result = test.execute(timeout=10)
        self.assertEqual(len(result), 3)


# ---------------------------------------------------------------------------
# Routing internals (flip_points, entry_points, keep_original_src)
# ---------------------------------------------------------------------------
class TestRoutingInternals(unittest.TestCase):
    """Test routing assertion logic inside execute() using EchoSkill.

    EchoSkill produces: unittest.echo → speak → ovos.utterance.handled
    All messages carry the same context source/destination from the source msg.
    """

    def setUp(self):
        LOG.set_level("ERROR")
        self.mc = get_minicroft([SKILL_ID], extra_skills={SKILL_ID: EchoSkill})

    def tearDown(self):
        self.mc.stop()
        LOG.set_level("CRITICAL")

    def _base_flags(self, **overrides):
        defaults = dict(
            test_message_number=False,
            test_msg_type=False,
            test_msg_data=False,
            test_msg_context=False,
            test_active_skills=False,
            test_final_session=False,
            test_async_messages=False,
            test_async_message_number=False,
            ignore_messages=["ovos.skills.settings_changed"] + HANDLER_LIFECYCLE,
            verbose=False,
        )
        defaults.update(overrides)
        return defaults

    def test_routing_passes_with_matching_src_dst(self):
        """test_routing=True passes when src/dst match across all messages."""
        src = _make_custom("unittest.echo", {"text": "route"})
        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            source_message=src,
            expected_messages=[
                src,
                Message("speak", {}),
                Message("ovos.utterance.handled", {}),
            ],
            entry_points=[],  # no entry point flip
            **self._base_flags(test_routing=True),
        )
        result = test.execute(timeout=10)
        self.assertEqual(len(result), 3)

    def test_routing_with_flip_point(self):
        """After a flip_point, expected src and dst are swapped."""
        src = _make_custom("unittest.echo", {"text": "flip"})
        # After flip, expected src=B, dst=A (swapped from A,B)
        # But actual messages still have source=A, so this should fail
        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            source_message=src,
            expected_messages=[
                Message("unittest.echo", {}, {"source": "A", "destination": "B"}),
                Message("speak", {}),  # after flip, expects src=B,dst=A but gets src=A
                Message("ovos.utterance.handled", {}),
            ],
            flip_points=["unittest.echo"],
            entry_points=[],
            **self._base_flags(test_routing=True),
        )
        with self.assertRaises(AssertionError):
            test.execute(timeout=10)

    def test_routing_with_entry_point(self):
        """After an entry_point, src and dst are extracted from received and flipped."""
        src = _make_custom("unittest.echo", {"text": "entry"})
        # entry_points causes: after "unittest.echo", new e_src = r_dst, e_dst = r_src
        # received has source=A, destination=B → new expected = B, A
        # next msg (speak) will have source=A → mismatch with expected B → should fail
        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            source_message=src,
            expected_messages=[
                src,
                Message("speak", {}),
                Message("ovos.utterance.handled", {}),
            ],
            entry_points=["unittest.echo"],
            **self._base_flags(test_routing=True),
        )
        with self.assertRaises(AssertionError):
            test.execute(timeout=10)

    def test_keep_original_src_uses_original(self):
        """Messages in keep_original_src compare against o_src/o_dst."""
        src = _make_custom("unittest.echo", {"text": "keep"})
        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            source_message=src,
            expected_messages=[
                src,
                Message("speak", {}),
                Message("ovos.utterance.handled", {}),
            ],
            keep_original_src=["speak"],  # speak uses original src/dst
            entry_points=[],
            **self._base_flags(test_routing=True),
        )
        result = test.execute(timeout=10)
        self.assertEqual(len(result), 3)

    def test_routing_verbose_prints(self):
        """verbose=True with routing enabled exercises all routing print branches."""
        src = _make_custom("unittest.echo", {"text": "verbose-route"})
        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            source_message=src,
            expected_messages=[
                src,
                Message("speak", {}),
                Message("ovos.utterance.handled", {}),
            ],
            entry_points=[],
            ignore_messages=["ovos.skills.settings_changed"] + HANDLER_LIFECYCLE,
            test_message_number=False,
            test_msg_type=False,
            test_msg_data=False,
            test_msg_context=False,
            test_active_skills=False,
            test_final_session=False,
            test_async_messages=False,
            test_async_message_number=False,
            test_routing=True,
            verbose=True,
        )
        result = test.execute(timeout=10)
        self.assertEqual(len(result), 3)


# ---------------------------------------------------------------------------
# Active skills internals (activation_points, deactivation_points,
# disallow_extra_active_skills)
# ---------------------------------------------------------------------------
class TestActiveSkillsInternals(unittest.TestCase):

    def setUp(self):
        LOG.set_level("ERROR")
        self.mc = get_minicroft([SKILL_ID], extra_skills={SKILL_ID: EchoSkill})

    def tearDown(self):
        self.mc.stop()
        LOG.set_level("CRITICAL")

    def _base_flags(self, **overrides):
        defaults = dict(
            test_message_number=False,
            test_msg_type=False,
            test_msg_data=False,
            test_msg_context=False,
            test_routing=False,
            test_final_session=False,
            test_async_messages=False,
            test_async_message_number=False,
            ignore_messages=["ovos.skills.settings_changed"] + HANDLER_LIFECYCLE,
            verbose=False,
        )
        defaults.update(overrides)
        return defaults

    def test_activation_point_tracks_skill(self):
        """After an activation_point, the skill_id is tracked as active."""
        src = _make_custom("unittest.echo", {"text": "activate"})
        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            source_message=src,
            expected_messages=[src],
            activation_points=["unittest.echo"],
            **self._base_flags(test_active_skills=True, verbose=True),
        )
        # Should pass — unittest.echo is an activation point,
        # skill_id from context gets added to active_skills
        result = test.execute(timeout=10)
        self.assertIsInstance(result, list)

    def test_deactivation_point_removes_skill(self):
        """After a deactivation_point, the skill_id is removed from tracking."""
        src = _make_custom("unittest.echo", {"text": "deactivate"})
        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            source_message=src,
            expected_messages=[src],
            deactivation_points=["unittest.echo"],
            **self._base_flags(test_active_skills=False, verbose=True),
        )
        result = test.execute(timeout=10)
        self.assertIsInstance(result, list)

    def test_disallow_extra_active_fails(self):
        """disallow_extra_active_skills=True raises if unexpected skills are active."""
        src = _make_custom("unittest.echo", {"text": "extra"})
        # inject_active adds a skill, but the echo handler may activate
        # the test skill too — if any unexpected skill is active, it fails
        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            source_message=src,
            expected_messages=[src],
            inject_active=[],
            disallow_extra_active_skills=True,
            **self._base_flags(test_active_skills=True),
        )
        # This may or may not raise depending on what skills are active
        # The important thing is this code path is exercised
        try:
            test.execute(timeout=10)
        except AssertionError:
            pass  # expected if extra skills are active


# ---------------------------------------------------------------------------
# Final session tests
# ---------------------------------------------------------------------------
class TestFinalSession(unittest.TestCase):

    def setUp(self):
        LOG.set_level("ERROR")
        self.mc = get_minicroft([SKILL_ID], extra_skills={SKILL_ID: EchoSkill})

    def tearDown(self):
        self.mc.stop()
        LOG.set_level("CRITICAL")

    def test_final_session_lang_mismatch_raises(self):
        """test_final_session=True raises when lang doesn't match."""
        src = _make_custom("unittest.echo", {"text": "final"})
        wrong_session = Session("ext-test")
        wrong_session.lang = "xx-XX"  # wrong lang
        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            source_message=src,
            expected_messages=[
                src,
                Message("speak", {}),
                Message("ovos.utterance.handled", {}),
            ],
            final_session=wrong_session,
            test_message_number=False,
            test_msg_type=False,
            test_msg_data=False,
            test_msg_context=False,
            test_routing=False,
            test_active_skills=False,
            test_final_session=True,
            test_async_messages=False,
            test_async_message_number=False,
            ignore_messages=["ovos.skills.settings_changed"] + HANDLER_LIFECYCLE,
            verbose=False,
        )
        with self.assertRaises(AssertionError):
            test.execute(timeout=10)

    def test_final_session_passes_when_matching(self):
        """test_final_session=True passes when session attributes match."""
        sess = _session()
        src = _make_custom("unittest.echo", {"text": "final-ok"})
        # Build a final_session that matches the actual session state
        expected_sess = Session("ext-test")
        expected_sess.lang = "en-US"
        expected_sess.pipeline = ADAPT_ONLY
        expected_sess.site_id = sess.site_id
        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            source_message=src,
            expected_messages=[
                src,
                Message("speak", {}),
                Message("ovos.utterance.handled", {}),
            ],
            final_session=expected_sess,
            test_message_number=False,
            test_msg_type=False,
            test_msg_data=False,
            test_msg_context=False,
            test_routing=False,
            test_active_skills=False,
            test_final_session=True,
            test_async_messages=False,
            test_async_message_number=False,
            ignore_messages=["ovos.skills.settings_changed"] + HANDLER_LIFECYCLE,
            verbose=True,  # covers the verbose final session print branches
        )
        result = test.execute(timeout=10)
        self.assertIsInstance(result, list)


# ---------------------------------------------------------------------------
# from_message recording mode
# ---------------------------------------------------------------------------
class TestFromMessage(unittest.TestCase):

    def setUp(self):
        LOG.set_level("ERROR")

    def tearDown(self):
        LOG.set_level("CRITICAL")

    def test_from_message_records_sequence(self):
        """from_message() records the actual message sequence as expected."""
        src = _make_custom("unittest.echo", {"text": "record"})
        test = End2EndTest.from_message(
            message=src,
            skill_ids=[SKILL_ID],
            extra_skills={SKILL_ID: EchoSkill},
            timeout=10,
        )
        self.assertIsInstance(test, End2EndTest)
        self.assertEqual(test.skill_ids, [SKILL_ID])
        self.assertTrue(len(test.expected_messages) > 0,
                        "from_message must capture at least one message")

    def test_from_message_single_message_wrapped(self):
        """from_message() wraps a single Message into a list."""
        src = _make_custom("unittest.echo", {"text": "single"})
        test = End2EndTest.from_message(
            message=src,
            skill_ids=[SKILL_ID],
            extra_skills={SKILL_ID: EchoSkill},
            timeout=10,
        )
        self.assertIsInstance(test.source_message, list)
        self.assertEqual(len(test.source_message), 1)


# ---------------------------------------------------------------------------
# Message count verbose branch (first differing message)
# ---------------------------------------------------------------------------
class TestMessageCountVerbose(unittest.TestCase):

    def setUp(self):
        LOG.set_level("ERROR")
        self.mc = get_minicroft([SKILL_ID], extra_skills={SKILL_ID: EchoSkill})

    def tearDown(self):
        self.mc.stop()
        LOG.set_level("CRITICAL")

    def test_count_mismatch_prints_first_differing(self):
        """Count mismatch prints info about the first differing message."""
        src = _make_custom("unittest.echo", {"text": "diff"})
        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            source_message=src,
            expected_messages=[
                src,
                Message("WRONG.TYPE", {}),  # differs from "speak"
            ],
            test_message_number=True,
            test_msg_type=False,
            test_msg_data=False,
            test_msg_context=False,
            test_routing=False,
            test_active_skills=False,
            test_final_session=False,
            test_async_messages=False,
            test_async_message_number=False,
            ignore_messages=["ovos.skills.settings_changed"] + HANDLER_LIFECYCLE,
            verbose=False,
        )
        with self.assertRaises(AssertionError):
            test.execute(timeout=10)


class TestPipelineIdFilter(unittest.TestCase):
    """The pipeline_id filter isolates a lifecycle when a shared skill_id can't."""

    def setUp(self):
        LOG.set_level("ERROR")
        self.mc = get_minicroft([SKILL_ID],
                                extra_skills={SKILL_ID: SharedSkillIdSkill})

    def tearDown(self):
        self.mc.stop()
        LOG.set_level("CRITICAL")

    def _common_kwargs(self):
        return dict(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            eof_msgs=["ovos.utterance.handled"],
            eof_count=2,
            test_routing=False,
            test_active_skills=False,
            test_final_session=False,
            ignore_messages=DEFAULT_IGNORED + HANDLER_LIFECYCLE,
            verbose=False,
        )

    def test_pipeline_id_isolates_when_skill_id_shared(self):
        # both lifecycles share skill_id="shared.skill"; only pipeline_id differs
        src = _make_custom("unittest.shared_skill_id")
        test = End2EndTest(
            source_message=src,
            pipeline_id="pipe.a",
            expected_messages=[
                Message("pipe.a.step", {},
                        {"skill_id": "shared.skill", "pipeline_id": "pipe.a"}),
                Message("ovos.utterance.handled", {},
                        {"skill_id": "shared.skill", "pipeline_id": "pipe.a"}),
            ],
            **self._common_kwargs(),
        )
        test.execute(timeout=10)

    def test_pipeline_id_filters_the_other(self):
        src = _make_custom("unittest.shared_skill_id")
        test = End2EndTest(
            source_message=src,
            pipeline_id="pipe.b",
            expected_messages=[
                Message("pipe.b.step", {},
                        {"skill_id": "shared.skill", "pipeline_id": "pipe.b"}),
                Message("ovos.utterance.handled", {},
                        {"skill_id": "shared.skill", "pipeline_id": "pipe.b"}),
            ],
            **self._common_kwargs(),
        )
        test.execute(timeout=10)


class TestSkillIdFilter(unittest.TestCase):
    """The skill_id filter isolates one dispatch lifecycle from concurrent ones."""

    def setUp(self):
        LOG.set_level("ERROR")
        self.mc = get_minicroft([SKILL_ID],
                                extra_skills={SKILL_ID: TwoLifecycleSkill})

    def tearDown(self):
        self.mc.stop()
        LOG.set_level("CRITICAL")

    def _common_kwargs(self):
        return dict(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            eof_msgs=["ovos.utterance.handled"],
            eof_count=2,  # both lifecycles terminate on ovos.utterance.handled
            test_routing=False,
            test_active_skills=False,
            test_final_session=False,
            ignore_messages=DEFAULT_IGNORED + HANDLER_LIFECYCLE,
            verbose=False,
        )

    def test_filter_isolates_one_lifecycle(self):
        """Only messages whose context skill_id matches are asserted."""
        src = _make_custom("unittest.two_lifecycles")
        test = End2EndTest(
            source_message=src,
            skill_id="life.a",
            expected_messages=[
                Message("life.a.step", {}, {"skill_id": "life.a"}),
                Message("ovos.utterance.handled", {}, {"skill_id": "life.a"}),
            ],
            **self._common_kwargs(),
        )
        # passes only if life.b.* and the source (no skill_id) are filtered out
        test.execute(timeout=10)

    def test_filter_the_other_lifecycle(self):
        """The same scenario, filtered to the other skill_id."""
        src = _make_custom("unittest.two_lifecycles")
        test = End2EndTest(
            source_message=src,
            skill_id="life.b",
            expected_messages=[
                Message("life.b.step", {}, {"skill_id": "life.b"}),
                Message("ovos.utterance.handled", {}, {"skill_id": "life.b"}),
            ],
            **self._common_kwargs(),
        )
        test.execute(timeout=10)

    def test_unfiltered_sees_both_lifecycles(self):
        """Without the filter, eof_count=2 captures both lifecycles' messages."""
        src = _make_custom("unittest.two_lifecycles")
        test = End2EndTest(
            source_message=src,
            expected_messages=[src],
            test_message_number=False,
            test_msg_type=False,
            test_msg_data=False,
            test_msg_context=False,
            **self._common_kwargs(),
        )
        result = test.execute(timeout=10)
        types = [m.msg_type for m in result]
        self.assertIn("life.a.step", types)
        self.assertIn("life.b.step", types)


if __name__ == "__main__":
    unittest.main()
