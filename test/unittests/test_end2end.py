"""Unit tests for End2EndTest — execute(), assertions, serialization, helpers."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from ovos_bus_client.message import Message
from ovos_bus_client.session import Session
from ovos_utils.log import LOG
from ovos_workshop.skills.ovos import OVOSSkill

from ovoscope import End2EndTest, get_minicroft

# ---------------------------------------------------------------------------
# Shared inline skill
# ---------------------------------------------------------------------------
SKILL_ID = "ovoscope-unittest-e2e.test"

# Handler lifecycle messages emitted by add_event() wrappers.
# Tests that don't care about lifecycle use this to filter noise.
HANDLER_LIFECYCLE = ["mycroft.skill.handler.start",
                     "mycroft.skill.handler.complete",
                     "recognizer_loop:audio_output_start",
                     "recognizer_loop:audio_output_end"]

# Minimal pipeline: only Adapt-high so we get predictable no-match / match
ADAPT_ONLY = ["ovos-adapt-pipeline-plugin-high"]


class EchoSkill(OVOSSkill):
    """Handles 'unittest.echo': speaks the 'text' field, then emits EOF."""

    def initialize(self):
        self.add_event("unittest.echo", self.handle_echo)

    def handle_echo(self, message: Message):
        text = message.data.get("text", "echo")
        self.speak(text)
        self.bus.emit(Message("ovos.utterance.handled", context=message.context))


class SilentSkill(OVOSSkill):
    """Handles 'unittest.silent': emits only EOF — no speak."""

    def initialize(self):
        self.add_event("unittest.silent", self.handle_silent)

    def handle_silent(self, message: Message):
        self.bus.emit(Message("ovos.utterance.handled", context=message.context))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _session(session_id: str = "unit-test-session",
             pipeline=None) -> Session:
    s = Session(session_id)
    s.lang = "en-US"   # explicit lang so tests don't depend on system locale
    s.pipeline = pipeline or ADAPT_ONLY
    return s


def _make_utterance(text: str = "hello world",
                    session_id: str = "unit-test-session",
                    pipeline=None) -> Message:
    """Build a recognizer_loop:utterance message with an explicit pipeline."""
    sess = _session(session_id, pipeline)
    return Message("recognizer_loop:utterance",
                   {"utterances": [text], "lang": "en-US"},
                   {"session": sess.serialize(),
                    "source": "A", "destination": "B"})


def _make_custom(msg_type: str, data=None,
                 session_id: str = "unit-test-session") -> Message:
    """Build a custom event message (bypasses utterance pipeline)."""
    sess = _session(session_id)
    return Message(msg_type, data or {},
                   {"session": sess.serialize(),
                    "source": "A", "destination": "B"})


# Expected 4-message failure sequence for any utterance that matches nothing
_FAILURE_SEQ = [
    # message itself is index 0 (caller provides it)
    Message("mycroft.audio.play_sound", {"uri": "snd/error.mp3"}),
    Message("ovos.intent.unmatched", {}),
    Message("ovos.utterance.handled", {}),
]


# ---------------------------------------------------------------------------
# Tests: execute() return value
# ---------------------------------------------------------------------------
class TestExecuteReturnValue(unittest.TestCase):

    def setUp(self):
        LOG.set_level("ERROR")
        self.mc = get_minicroft([SKILL_ID], extra_skills={SKILL_ID: EchoSkill})

    def tearDown(self):
        self.mc.stop()
        LOG.set_level("CRITICAL")

    def test_execute_returns_list(self):
        """execute() must return a list (not None)."""
        src = _make_custom("unittest.echo", {"text": "hello"})
        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            source_message=src,
            expected_messages=[src],  # exact list doesn't matter here
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
        self.assertIsNotNone(result, "execute() must not return None")
        self.assertIsInstance(result, list)

    def test_execute_returns_captured_messages(self):
        """The returned list contains the actual Message objects."""
        src = _make_custom("unittest.echo", {"text": "world"})
        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            source_message=src,
            expected_messages=[src],
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
        types = [m.msg_type for m in result]
        self.assertIn("unittest.echo", types)
        self.assertIn("ovos.utterance.speak", types)
        self.assertIn("ovos.utterance.handled", types)

    def test_execute_result_length_matches_expected(self):
        """The returned list has exactly N messages when all are present.

        EchoSkill sequence (filtering handler lifecycle):
          1. unittest.echo (the source message)
          2. speak
          3. ovos.utterance.handled
        """
        src = _make_custom("unittest.echo", {"text": "count test"})
        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            source_message=src,
            expected_messages=[
                src,
                Message("ovos.utterance.speak", {"utterance": "count test"}),
                Message("ovos.utterance.handled", {}),
            ],
            # filter out handler.start / handler.complete so count is 3
            ignore_messages=["ovos.skills.settings_changed"] + HANDLER_LIFECYCLE,
            test_routing=False,
            test_active_skills=False,
            test_final_session=False,
            test_async_messages=False,
            test_async_message_number=False,
            verbose=False,
        )
        result = test.execute(timeout=10)
        self.assertEqual(len(result), 3)


# ---------------------------------------------------------------------------
# Tests: assertion failures raise AssertionError
# ---------------------------------------------------------------------------
class TestAssertions(unittest.TestCase):

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
            test_active_skills=False,
            test_final_session=False,
            test_async_messages=False,
            test_async_message_number=False,
            ignore_messages=["ovos.skills.settings_changed"] + HANDLER_LIFECYCLE,
            verbose=False,
        )
        defaults.update(overrides)
        return defaults

    def test_expected_messages_bare_string_raises_clear_type_error(self):
        """A bare topic string in expected_messages (e.g. ["speak"]) must
        raise a clear TypeError naming the mistake at construction time,
        never an obscure AttributeError deep inside execute()'s assertion
        loop. Message objects are, and have always been, the only accepted
        shape for this field."""
        src = _make_custom("unittest.echo", {"text": "string test"})
        with self.assertRaises(TypeError):
            End2EndTest(
                minicroft=self.mc,
                skill_ids=[SKILL_ID],
                source_message=src,
                expected_messages=["speak"],
                **self._base_flags(),
            )

    def test_expected_boot_sequence_bare_string_raises_clear_type_error(self):
        """Same guard applies to expected_boot_sequence."""
        src = _make_custom("unittest.echo", {"text": "boot string test"})
        with self.assertRaises(TypeError):
            End2EndTest(
                minicroft=self.mc,
                skill_ids=[SKILL_ID],
                source_message=src,
                expected_messages=[],
                expected_boot_sequence=["mycroft.ready"],
                **self._base_flags(),
            )

    def test_wrong_message_count_raises(self):
        """test_message_number=True raises AssertionError on count mismatch,
        and the exception text itself must list the captured messages —
        under pytest-xdist a worker's stdout doesn't reach the CI job log,
        so the printed diagnostic loop is not enough (ovos-core#918)."""
        src = _make_custom("unittest.echo", {"text": "count"})
        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            source_message=src,
            expected_messages=[src],        # only 1 but 3 will be captured
            **self._base_flags(test_message_number=True),
        )
        with self.assertRaises(AssertionError) as ctx:
            test.execute(timeout=10)
        msg = str(ctx.exception)
        self.assertIn("got 3 messages, expected 1", msg)
        # the extra messages beyond the expected one must be named in the
        # assertion text, not just printed to a stdout no one can see
        self.assertIn("ovos.utterance.speak", msg)
        self.assertIn("ovos.utterance.handled", msg)

    def test_wrong_message_type_raises(self):
        """test_msg_type=True raises AssertionError when msg_type doesn't match."""
        src = _make_custom("unittest.echo", {"text": "type test"})
        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            source_message=src,
            expected_messages=[
                src,
                Message("WRONG.TYPE", {}),        # actual is "speak"
                Message("ovos.utterance.handled", {}),
            ],
            **self._base_flags(test_msg_type=True),
        )
        with self.assertRaises(AssertionError):
            test.execute(timeout=10)

    def test_wrong_message_data_raises(self):
        """test_msg_data=True raises AssertionError on data key mismatch."""
        src = _make_custom("unittest.echo", {"text": "data test"})
        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            source_message=src,
            expected_messages=[
                src,
                Message("speak", {"utterance": "WRONG TEXT"}),
                Message("ovos.utterance.handled", {}),
            ],
            **self._base_flags(test_msg_data=True),
        )
        with self.assertRaises(AssertionError):
            test.execute(timeout=10)

    def test_disable_data_assertion_ignores_wrong_data(self):
        """test_msg_data=False passes even with wrong expected data values."""
        src = _make_custom("unittest.echo", {"text": "data off"})
        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            source_message=src,
            expected_messages=[
                src,
                Message("speak", {"utterance": "WRONG TEXT"}),
                Message("ovos.utterance.handled", {}),
            ],
            **self._base_flags(test_msg_data=False),
        )
        # must not raise
        test.execute(timeout=10)

    def test_disable_count_assertion_allows_count_mismatch(self):
        """test_message_number=False allows more or fewer messages than expected."""
        src = _make_custom("unittest.echo", {"text": "count off"})
        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            source_message=src,
            expected_messages=[src],   # only 1; 3+ will be received
            **self._base_flags(test_message_number=False),
        )
        test.execute(timeout=10)

    def test_ignore_messages_excluded_from_captured_list(self):
        """Messages in ignore_messages do not appear in the captured sequence."""
        src = _make_custom("unittest.echo", {"text": "filter"})
        # Add "ovos.utterance.speak" to ignored — only 2 messages remain: src + eof
        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            source_message=src,
            expected_messages=[
                src,
                Message("ovos.utterance.handled", {}),
            ],
            ignore_messages=["ovos.skills.settings_changed", "ovos.utterance.speak"]
                            + HANDLER_LIFECYCLE,
            test_routing=False,
            test_active_skills=False,
            test_final_session=False,
            test_async_messages=False,
            test_async_message_number=False,
            verbose=False,
        )
        test.execute(timeout=10)


# ---------------------------------------------------------------------------
# Tests: managed lifecycle (no minicroft pre-supplied)
# ---------------------------------------------------------------------------
class TestManagedLifecycle(unittest.TestCase):

    def setUp(self):
        LOG.set_level("ERROR")

    def tearDown(self):
        LOG.set_level("CRITICAL")

    def test_execute_creates_and_stops_minicroft_when_unmanaged(self):
        """If minicroft is not supplied, execute() creates and stops one."""
        # Explicit pipeline so persona/OCP/fallback don't interfere
        message = _make_utterance("hello world", pipeline=ADAPT_ONLY)
        test = End2EndTest(
            skill_ids=[],
            source_message=message,
            expected_messages=[
                message,
                Message("mycroft.audio.play_sound", {"uri": "snd/error.mp3"}),
                Message("ovos.intent.unmatched", {}),
                Message("ovos.utterance.handled", {}),
            ],
            flip_points=["recognizer_loop:utterance"],
            test_routing=False,
            test_active_skills=False,
            test_final_session=False,
            test_async_messages=False,
            test_async_message_number=False,
            verbose=False,
        )
        result = test.execute(timeout=15)
        self.assertIsInstance(result, list)
        self.assertIsNone(test.minicroft,
                          "managed minicroft must be cleaned up after execute()")


# ---------------------------------------------------------------------------
# Tests: assert_spoke() sugar method
# ---------------------------------------------------------------------------
class TestAssertSpoke(unittest.TestCase):

    def setUp(self):
        LOG.set_level("ERROR")
        self.mc = get_minicroft([SKILL_ID], extra_skills={SKILL_ID: EchoSkill})

    def tearDown(self):
        self.mc.stop()
        LOG.set_level("CRITICAL")

    def _spoke_test(self, text: str) -> End2EndTest:
        """Build an End2EndTest configured for assert_spoke (all checks disabled)."""
        src = _make_custom("unittest.echo", {"text": text})
        return End2EndTest(
            minicroft=self.mc,
            skill_ids=[SKILL_ID],
            source_message=src,
            expected_messages=[src],   # doesn't matter — all checks off
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

    def test_assert_spoke_passes_correct_utterance(self):
        """assert_spoke does not raise when the speak utterance matches."""
        self._spoke_test("correct text").assert_spoke("correct text", timeout=10)

    def test_assert_spoke_fails_wrong_utterance(self):
        """assert_spoke raises AssertionError when the speak utterance is absent."""
        with self.assertRaises(AssertionError):
            self._spoke_test("correct text").assert_spoke("WRONG TEXT", timeout=10)

    def test_assert_spoke_accepts_canonical_only_captured_stream(self):
        """assert_spoke matches a captured stream that only carries the
        canonical "ovos.utterance.speak" topic (post-workshop#425 producers).
        """
        test = self._spoke_test("canonical text")
        captured = [
            Message("ovos.utterance.speak",
                    {"utterance": "canonical text", "lang": "en-US"}),
        ]
        with patch.object(End2EndTest, "execute", return_value=captured):
            test.assert_spoke("canonical text", timeout=10)

    def test_assert_spoke_accepts_legacy_only_captured_stream(self):
        """assert_spoke matches a captured stream that only carries the
        legacy "speak" topic (pre-spec producer vintage on the wire).
        """
        test = self._spoke_test("legacy text")
        captured = [
            Message("speak", {"utterance": "legacy text", "lang": "en-US"}),
        ]
        with patch.object(End2EndTest, "execute", return_value=captured):
            test.assert_spoke("legacy text", timeout=10)


# ---------------------------------------------------------------------------
# Tests: serialization round-trip (serialize / deserialize / save / from_path)
# ---------------------------------------------------------------------------
class TestSerialization(unittest.TestCase):

    def setUp(self):
        LOG.set_level("ERROR")

    def tearDown(self):
        LOG.set_level("CRITICAL")

    def _make_simple_test(self) -> End2EndTest:
        """A deterministic no-match test with an explicit Adapt-only pipeline.

        test_msg_context=False: anonymize_message() replaces session context in
        expected_messages with the default SessionManager session, which would
        cause context mismatches on replay.  Disabling context checks avoids that.
        """
        src = _make_utterance("hello world", session_id="serial-session",
                              pipeline=ADAPT_ONLY)
        return End2EndTest(
            skill_ids=[],
            source_message=src,
            expected_messages=[
                src,
                Message("mycroft.audio.play_sound", {"uri": "snd/error.mp3"}),
                Message("ovos.intent.unmatched", {}),
                Message("ovos.utterance.handled", {}),
            ],
            flip_points=["recognizer_loop:utterance"],
            test_msg_context=False,  # see docstring
        )

    def test_serialize_returns_dict(self):
        data = self._make_simple_test().serialize()
        self.assertIsInstance(data, dict)
        self.assertIn("skill_ids", data)
        self.assertIn("source_message", data)
        self.assertIn("expected_messages", data)

    def test_deserialize_round_trip(self):
        original = self._make_simple_test()
        data = original.serialize(anonymize=False)
        restored = End2EndTest.deserialize(data)

        self.assertEqual(restored.skill_ids, original.skill_ids)
        self.assertEqual(len(restored.source_message), len(original.source_message))
        for e, r in zip(original.expected_messages, restored.expected_messages):
            self.assertEqual(e.msg_type, r.msg_type)

    def test_save_and_from_path(self):
        """save() then from_path() produces an executable End2EndTest."""
        original = self._make_simple_test()
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            original.save(path, anonymize=True)
            with open(path) as f:
                data = json.load(f)
            self.assertIn("skill_ids", data)

            restored = End2EndTest.from_path(path)
            result = restored.execute(timeout=15)
            self.assertIsInstance(result, list)
            self.assertEqual(len(result), 4)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Tests: multi-turn (list of source messages)
# ---------------------------------------------------------------------------
class TestMultiTurn(unittest.TestCase):

    def setUp(self):
        LOG.set_level("ERROR")
        self.mc = get_minicroft([])

    def tearDown(self):
        self.mc.stop()
        LOG.set_level("CRITICAL")

    def test_multi_turn_two_failures(self):
        """Two turns that both fail produce 8 messages total."""
        # Explicit Adapt-only pipeline so persona doesn't intercept
        sess = _session("multi-turn", pipeline=ADAPT_ONLY)
        turn1 = Message("recognizer_loop:utterance",
                        {"utterances": ["turn one"], "lang": "en-US"},
                        {"session": sess.serialize(),
                         "source": "A", "destination": "B"})
        # turn2 has no session — ovoscope propagates from last received message
        turn2 = Message("recognizer_loop:utterance",
                        {"utterances": ["turn two"], "lang": "en-US"},
                        {"source": "A", "destination": "B"})

        test = End2EndTest(
            minicroft=self.mc,
            skill_ids=[],
            source_message=[turn1, turn2],
            expected_messages=[
                turn1,
                Message("mycroft.audio.play_sound", {"uri": "snd/error.mp3"}),
                Message("ovos.intent.unmatched", {}),
                Message("ovos.utterance.handled", {}),
                turn2,
                Message("mycroft.audio.play_sound", {"uri": "snd/error.mp3"}),
                Message("ovos.intent.unmatched", {}),
                Message("ovos.utterance.handled", {}),
            ],
            flip_points=["recognizer_loop:utterance"],
            test_routing=False,
            test_active_skills=False,
            test_final_session=False,
            test_async_messages=False,
            test_async_message_number=False,
            verbose=False,
        )
        result = test.execute(timeout=20)
        self.assertEqual(len(result), 8)


if __name__ == "__main__":
    unittest.main()
