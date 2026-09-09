"""The source/destination rule holds under either spelling of a topic.

A migrating topic reaches the bus under a legacy or a canonical name depending
on the deployment, and `keep_original_src` decides whether a message's source
and destination are checked against the original source message or against the
rolling flip. A rule naming one spelling silently selects the other comparison
when the deployment uses the other name, so a capture stays green while
asserting something different.
"""
import unittest
from unittest.mock import patch

from ovos_bus_client.message import Message
from ovos_bus_client.session import Session
from ovos_utils.log import LOG
from ovos_workshop.skills.ovos import OVOSSkill

import ovoscope
from ovoscope import (DEFAULT_KEEP_SRC, End2EndTest, both_spellings,
                      get_minicroft)


class EchoSkill(OVOSSkill):
    """Speaks the 'text' field, then signals the round is over."""

    def initialize(self):
        self.add_event("unittest.echo", self.handle_echo)

    def handle_echo(self, message: Message):
        self.speak(message.data.get("text", "echo"))
        self.bus.emit(Message("ovos.utterance.handled", context=message.context))


class TestBothSpellings(unittest.TestCase):
    def test_default_covers_the_fallback_poll_under_either_name(self):
        self.assertIn("ovos.skills.fallback.ping", DEFAULT_KEEP_SRC)
        self.assertIn("ovos.fallback.ping", DEFAULT_KEEP_SRC)

    def test_the_fallback_poll_resolves_through_the_map(self):
        """The default names both spellings by hand, so it stays correct even
        where the migration map has no fallback entry. A caller's own rule has
        no such spelling out and depends entirely on the map, so the poll has
        to resolve through `both_spellings` itself. This is what holds the
        declared ovos-spec-tools floor: below the release that carries the
        OVOS-FALLBACK-1 renames the expansion returns the topic unchanged."""
        self.assertEqual(both_spellings(["ovos.skills.fallback.ping"]),
                         ["ovos.skills.fallback.ping", "ovos.fallback.ping"])
        self.assertEqual(both_spellings(["ovos.fallback.pong"]),
                         ["ovos.fallback.pong", "ovos.skills.fallback.pong"])

    def test_expansion_is_idempotent_and_order_preserving(self):
        once = both_spellings(DEFAULT_KEEP_SRC)
        self.assertEqual(once, both_spellings(once))
        self.assertEqual(once[0], DEFAULT_KEEP_SRC[0])
        self.assertEqual(len(once), len(set(once)))

    def test_a_migrating_topic_gains_its_counterpart(self):
        # "speak" is a payload-compatible rename carried by the migration map
        # since its first release, so this holds without the fallback entries.
        expanded = both_spellings(["speak"])
        self.assertEqual(expanded[0], "speak")
        self.assertEqual(len(expanded), 2)
        self.assertTrue(expanded[1].startswith("ovos."))

    def test_a_topic_outside_the_map_expands_to_itself(self):
        self.assertEqual(both_spellings(["not.a.migrating.topic"]),
                         ["not.a.migrating.topic"])

    def test_an_empty_rule_stays_empty(self):
        self.assertEqual(both_spellings([]), [])


class TestTheRuleReachesTheComparison(unittest.TestCase):
    """The expansion is only worth anything if the comparison loop reads it.

    The unit tests above pin what `both_spellings` returns. This one pins that
    `End2EndTest` runs the caller's own `keep_original_src` through it rather
    than using the list verbatim, which is the wiring a refactor can drop
    without any of them noticing.
    """

    SKILL_ID = "ovoscope-unittest-keepsrc.test"

    def setUp(self):
        LOG.set_level("ERROR")
        self.mc = get_minicroft([self.SKILL_ID],
                                extra_skills={self.SKILL_ID: EchoSkill})

    def tearDown(self):
        self.mc.stop()
        LOG.set_level("CRITICAL")

    def test_the_callers_rule_is_expanded_before_it_is_compared(self):
        seen = []
        real = ovoscope.both_spellings

        def spy(topics):
            result = real(topics)
            seen.append((list(topics), list(result)))
            return result

        rule = ["ovos.skills.fallback.ping"]
        src = Message("unittest.echo", {"text": "hi"},
                      {"session": Session("keepsrc-session").serialize(),
                       "source": "A", "destination": "B"})
        test = End2EndTest(
            minicroft=self.mc, skill_ids=[self.SKILL_ID],
            source_message=src, expected_messages=[src],
            keep_original_src=rule,
            test_message_number=False, test_msg_type=False,
            test_msg_data=False, test_msg_context=False,
            test_routing=True, test_active_skills=False,
            test_final_session=False, test_async_messages=False,
            test_async_message_number=False, verbose=False)
        with patch.object(ovoscope, "both_spellings", spy):
            test.execute(timeout=10)

        self.assertIn(
            (rule, ["ovos.skills.fallback.ping", "ovos.fallback.ping"]), seen,
            "the comparison never saw the expanded rule")
