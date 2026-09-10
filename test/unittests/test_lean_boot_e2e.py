"""Regression test for the lean default pipeline boot time.

Before the lean default + blacklisted_pipelines fix, MiniCroft instantiated
EVERY installed pipeline plugin at boot — including ovos-m2v-pipeline, whose
handle_sync_intents does a synchronous sleep(3) in a bus event handler. On a
fleet with several heavy skill repos this routinely pushed MiniCroft's
60s READY cap and made setUpClass ERROR out. This test boots a MiniCroft with
a real skill that registers a real intent and asserts it comes up fast, with
none of the heavy pipeline plugins instantiated.
"""
import time
import unittest

from ovos_bus_client.message import Message
from ovos_utils.log import LOG
from ovos_workshop.decorators import intent_handler
from ovos_workshop.intents import IntentBuilder
from ovos_workshop.skills.ovos import OVOSSkill

from ovoscope import get_minicroft, is_pipeline_available, LEAN_DEFAULT_PIPELINE

SKILL_ID = "ovoscope-unittest-lean-boot.test"

# Comfortably under the old 60s READY cap this regression used to blow past
# (a single m2v handler's synchronous sleep(3) alone eats a large chunk of
# that budget once several heavy plugins are in the mix) — proves the boot
# is actually lean, not just "still passes because CI is fast today".
BOOT_TIME_CEILING = 20.0

HEAVY_PIPELINE_PLUGINS = (
    "ovos-m2v-pipeline",
    "ovos-m2v-prototype-pipeline",
    "ovos-persona-pipeline-plugin",
    "ovos-common-query-pipeline-plugin",
    "ovos-ocp-pipeline-plugin",
    "ovos-ocp-pipeline-plugin-legacy",
)


class RealIntentSkill(OVOSSkill):
    """A minimal skill with a real Adapt intent, standing in for the
    "heavy skill repos" whose setUpClass used to time out."""

    @intent_handler(IntentBuilder("HelloIntent").require("hello"))
    def handle_hello(self, message: Message):
        self.speak("hello yourself", wait=False)


class TestLeanBootIsFast(unittest.TestCase):

    def setUp(self):
        LOG.set_level("ERROR")

    def tearDown(self):
        LOG.set_level("CRITICAL")

    def test_minicroft_with_real_skill_boots_fast_and_lean(self):
        if not is_pipeline_available(LEAN_DEFAULT_PIPELINE):
            raise unittest.SkipTest("lean pipeline plugins not installed")
        t0 = time.time()
        mc = get_minicroft([SKILL_ID], extra_skills={SKILL_ID: RealIntentSkill})
        try:
            elapsed = time.time() - t0
            self.assertLess(
                elapsed, BOOT_TIME_CEILING,
                f"MiniCroft took {elapsed:.1f}s to reach READY+trained — "
                f"a lean-default boot with a real skill must stay well "
                f"under the old 60s READY cap"
            )
            loaded = set(mc.intents.pipeline_plugins.keys())
            for heavy in HEAVY_PIPELINE_PLUGINS:
                self.assertNotIn(
                    heavy, loaded,
                    f"{heavy} was instantiated by a lean-default MiniCroft "
                    f"boot — this is exactly the regression that blew the "
                    f"60s READY cap fleet-wide"
                )
        finally:
            mc.stop()
