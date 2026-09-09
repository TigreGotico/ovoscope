"""The source/destination rule holds under either spelling of a topic.

A migrating topic reaches the bus under a legacy or a canonical name depending
on the deployment, and `keep_original_src` decides whether a message's source
and destination are checked against the original source message or against the
rolling flip. A rule naming one spelling silently selects the other comparison
when the deployment uses the other name, so a capture stays green while
asserting something different.
"""
import unittest

from ovos_bus_client.message import Message
from ovos_bus_client.session import Session
from ovos_utils.log import LOG
from ovos_workshop.skills.ovos import OVOSSkill

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

    The unit tests above pin what `both_spellings` returns. This one pins the
    outcome: a rule naming one spelling accepts the reply under the other,
    which a comparison reading the caller's list verbatim does not.
    """

    SKILL_ID = "ovoscope-unittest-keepsrc.test"

    def setUp(self):
        LOG.set_level("ERROR")
        self.mc = get_minicroft([self.SKILL_ID],
                                extra_skills={self.SKILL_ID: EchoSkill})

    def tearDown(self):
        self.mc.stop()
        LOG.set_level("CRITICAL")

    def _run(self, rule):
        """One echo round with an entry point on the source message, so the
        rolling expectation flips to (B, A) while every later message still
        carries the original (A, B). The skill's reply reaches the bus under
        the canonical spelling, so only the keep-src branch accepts it."""
        src = Message("unittest.echo", {"text": "hi"},
                      {"session": Session("keepsrc-session").serialize(),
                       "source": "A", "destination": "B"})
        End2EndTest(
            minicroft=self.mc, skill_ids=[self.SKILL_ID],
            source_message=src,
            expected_messages=[src, Message("ovos.utterance.speak")],
            entry_points=["unittest.echo"],
            keep_original_src=rule,
            test_message_number=False, test_msg_type=True,
            test_msg_data=False, test_msg_context=False,
            test_routing=True, test_active_skills=False,
            test_final_session=False, test_async_messages=False,
            test_async_message_number=False, verbose=False,
        ).execute(timeout=10)

    def test_the_callers_rule_decides_the_comparison_under_the_other_spelling(self):
        # the scenario discriminates: with no rule the rolling branch is
        # taken and the reply's original routing fails it
        with self.assertRaises(AssertionError):
            self._run([])
        # naming the spelling that actually arrives passes either way
        self._run(["ovos.utterance.speak"])
        # naming only the legacy spelling passes solely because the rule is
        # expanded before the membership test; a comparison that reads the
        # caller's list verbatim takes the rolling branch and fails
        self._run(["speak"])
