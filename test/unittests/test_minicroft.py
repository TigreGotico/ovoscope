"""Unit tests for MiniCroft and get_minicroft()."""
import os
import threading
import unittest
from unittest.mock import patch

import pytest

from ovos_bus_client.message import Message
from ovos_spec_tools import SpecMessage
from ovos_utils.log import LOG
from ovos_workshop.skills.ovos import OVOSSkill

from ovos_bus_client.session import SessionManager

from ovoscope import (MiniCroft, get_minicroft, DEFAULT_TEST_PIPELINE,
                      LIGHT_TEST_PIPELINE, ADAPT_PIPELINE, LEAN_DEFAULT_PIPELINE,
                      M2V_PIPELINE, PERSONA_PIPELINE, is_pipeline_available)

LEGACY_UTTERANCE = "recognizer_loop:utterance"
SPEC_UTTERANCE = str(SpecMessage.UTTERANCE)  # ovos.utterance.handle


# ---------------------------------------------------------------------------
# Minimal inline skill used across tests
# ---------------------------------------------------------------------------
SKILL_ID = "ovoscope-unittest-minicroft.test"


class PingSkill(OVOSSkill):
    """Emits 'unittest.pong' in response to 'unittest.ping'."""

    def initialize(self):
        self.add_event("unittest.ping", self.handle_ping)

    def handle_ping(self, message: Message):
        self.bus.emit(Message("unittest.pong",
                              data={"reply": "pong"},
                              context=message.context))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestGetMiniCroft(unittest.TestCase):

    def setUp(self):
        LOG.set_level("ERROR")

    def tearDown(self):
        LOG.set_level("CRITICAL")

    def test_empty_skills_reaches_ready(self):
        """get_minicroft([]) should start successfully with no skills."""
        mc = get_minicroft([])
        try:
            from ovos_utils.process_utils import ProcessState
            self.assertEqual(mc.status.state, ProcessState.READY)
        finally:
            mc.stop()

    def test_extra_skill_is_loaded(self):
        """An inline skill passed via extra_skills must appear in plugin_skills."""
        mc = get_minicroft([SKILL_ID], extra_skills={SKILL_ID: PingSkill})
        try:
            self.assertIn(SKILL_ID, mc.plugin_skills,
                          "extra skill should be registered in plugin_skills")
        finally:
            mc.stop()

    def test_max_wait_timeout_raises(self):
        """max_wait=0 should raise TimeoutError because MiniCroft can't be
        instantaneous — the underlying thread needs time to start."""
        with self.assertRaises(TimeoutError):
            get_minicroft([], max_wait=0)

    def test_skill_ids_string_normalised_to_list(self):
        """Passing a bare string is equivalent to passing a single-element list."""
        mc = get_minicroft(SKILL_ID, extra_skills={SKILL_ID: PingSkill})
        try:
            self.assertIn(SKILL_ID, mc.plugin_skills)
        finally:
            mc.stop()

    def test_returns_minicroft_instance(self):
        mc = get_minicroft([])
        try:
            self.assertIsInstance(mc, MiniCroft)
        finally:
            mc.stop()

    def test_boot_and_stop_without_default_session_class_attribute(self):
        """ovos-spec-tools>=1.10.5a2 removed SessionManager.default_session as a
        class attribute; get_minicroft() must only ever go through
        get_default_session()/the sessions registry, never that mirror."""
        had_attr = "default_session" in vars(SessionManager)
        original = vars(SessionManager).get("default_session")
        if had_attr:
            delattr(SessionManager, "default_session")
        try:
            mc = get_minicroft([])
            try:
                self.assertIsInstance(mc, MiniCroft)
            finally:
                mc.stop()
        finally:
            if had_attr:
                SessionManager.default_session = original

    def test_basedexception_during_boot_still_stops_croft(self):
        """A BaseException (e.g. pytest-timeout's Failed, or a real
        KeyboardInterrupt) raised while waiting for READY must still trigger
        croft.stop() before propagating. Regression test for get_minicroft's
        cleanup handler only catching `Exception`, which let BaseException
        subclasses skip cleanup and leak the started MiniCroft process."""
        with patch.object(MiniCroft, "start", side_effect=KeyboardInterrupt), \
             patch.object(MiniCroft, "stop") as mock_stop:
            with self.assertRaises(KeyboardInterrupt):
                get_minicroft([])
            mock_stop.assert_called_once()


class TestMiniCroftSessionManagerBusRestore(unittest.TestCase):
    """MiniCroft must not leak its FakeBus into the process-wide
    SessionManager.bus class attribute after stop().

    IntentService.__init__ calls SessionManager.connect_to_bus(self.bus),
    clobbering SessionManager.bus with MiniCroft's FakeBus. If stop() doesn't
    restore it, later tests in the same process — e.g. ones using a plain
    FakeBus and calling SessionManager.wait_while_speaking() — hit the
    `if not cls.bus` guard with a stale, truthy, dead bus and block/register
    listeners on the wrong bus.
    """

    def setUp(self):
        LOG.set_level("ERROR")

    def tearDown(self):
        LOG.set_level("CRITICAL")

    def test_sessionmanager_bus_restored_after_stop(self):
        sentinel = object()
        SessionManager.bus = sentinel
        try:
            mc = get_minicroft([])
            # while running, MiniCroft's own FakeBus has taken over
            self.assertIs(SessionManager.bus, mc.bus)
            mc.stop()
            self.assertIs(SessionManager.bus, sentinel,
                          "SessionManager.bus must be restored to its "
                          "pre-boot value after MiniCroft.stop()")
        finally:
            SessionManager.bus = None


class TestMiniCroftPipelineIsolation(unittest.TestCase):
    """Tests for MiniCroft default_pipeline override."""

    def setUp(self):
        LOG.set_level("ERROR")

    def tearDown(self):
        LOG.set_level("CRITICAL")

    def test_default_pipeline_overrides_default_session(self):
        """default_pipeline is applied to SessionManager.get_default_session()."""
        mc = get_minicroft([], default_pipeline=ADAPT_PIPELINE)
        try:
            self.assertEqual(SessionManager.get_default_session().pipeline, ADAPT_PIPELINE)
        finally:
            mc.stop()

    def test_default_pipeline_restored_after_stop(self):
        """After stop(), default_session.pipeline is restored to its previous value."""
        original = SessionManager.get_default_session().pipeline[:]
        mc = get_minicroft([], default_pipeline=ADAPT_PIPELINE)
        mc.stop()
        self.assertEqual(SessionManager.get_default_session().pipeline, original)

    def test_isolate_config_uses_lean_default_pipeline(self):
        """isolate_config=True with no explicit default_pipeline uses LEAN_DEFAULT_PIPELINE or fallback."""
        mc = get_minicroft([])
        try:
            # If all plugins are installed, it uses LEAN_DEFAULT_PIPELINE.
            # Otherwise it falls back to DEFAULT_TEST_PIPELINE/LIGHT_TEST_PIPELINE.
            # All three are valid outcomes of the "isolation + default" logic.
            self.assertIn(mc.pipeline,
                          [LEAN_DEFAULT_PIPELINE, DEFAULT_TEST_PIPELINE, LIGHT_TEST_PIPELINE])
            self.assertEqual(SessionManager.get_default_session().pipeline, mc.pipeline)
        finally:
            mc.stop()

    def test_persona_stages_absent_from_default_test_pipeline(self):
        """DEFAULT_TEST_PIPELINE must not contain any persona or LLM pipeline stage."""
        for stage in DEFAULT_TEST_PIPELINE:
            self.assertNotIn("persona", stage, f"persona stage found: {stage}")
            self.assertNotIn("ollama", stage, f"ollama stage found: {stage}")
            self.assertNotIn("m2v", stage, f"m2v stage found: {stage}")

    def test_no_pipeline_override_when_none(self):
        """default_pipeline=None must not alter the existing default session pipeline."""
        before = SessionManager.get_default_session().pipeline[:]
        mc = get_minicroft([], isolate_config=False, default_pipeline=None)
        try:
            self.assertEqual(SessionManager.get_default_session().pipeline, before)
        finally:
            mc.stop()


class TestMiniCroftInjectMessage(unittest.TestCase):
    """Tests for MiniCroft.inject_message()."""

    def setUp(self):
        LOG.set_level("ERROR")
        self.skill_id = SKILL_ID
        self.mc = get_minicroft([self.skill_id],
                                extra_skills={self.skill_id: PingSkill})

    def tearDown(self):
        self.mc.stop()
        LOG.set_level("CRITICAL")

    def test_inject_message_delivers_to_skill(self):
        """inject_message() should cause the skill handler to fire."""
        received = threading.Event()
        pong_msgs = []

        # Named message handlers on FakeBus receive Message objects directly;
        # only the generic "message" wildcard receives raw serialised strings.
        def on_pong(msg: Message):
            pong_msgs.append(msg)
            received.set()

        self.mc.bus.on("unittest.pong", on_pong)
        try:
            self.mc.inject_message(Message("unittest.ping",
                                           context={"source": "A", "destination": "B"}))
            received.wait(timeout=5)
            self.assertTrue(received.is_set(), "pong not received within 5s")
            self.assertEqual(len(pong_msgs), 1)
            self.assertEqual(pong_msgs[0].data["reply"], "pong")
        finally:
            self.mc.bus.remove("unittest.pong", on_pong)

    def test_inject_message_does_not_go_through_utterance_pipeline(self):
        """inject_message() bypasses the intent pipeline — no utterance.handled."""
        handled = threading.Event()

        def on_handled(msg: Message):
            handled.set()

        self.mc.bus.on("ovos.utterance.handled", on_handled)
        try:
            self.mc.inject_message(
                Message("some.arbitrary.event", context={"source": "A"}))
            # give it a moment
            handled.wait(timeout=1)
            self.assertFalse(handled.is_set(),
                             "inject_message should NOT trigger utterance pipeline")
        finally:
            self.mc.bus.remove("ovos.utterance.handled", on_handled)

    def test_inject_message_with_data(self):
        """Data and context passed to inject_message are forwarded intact."""
        received = threading.Event()
        captured = []

        def on_ping(raw: str):
            captured.append(Message.deserialize(raw))
            received.set()

        self.mc.bus.on("message", on_ping)
        try:
            self.mc.inject_message(
                Message("test.data.check",
                        data={"key": "value"},
                        context={"ctx": "test"}))
            received.wait(timeout=2)
            # find our message
            match = [m for m in captured
                     if m.msg_type == "test.data.check"]
            self.assertTrue(match, "injected message not found on bus")
            self.assertEqual(match[0].data["key"], "value")
            self.assertEqual(match[0].context["ctx"], "test")
        finally:
            self.mc.bus.remove("message", on_ping)


class TestMiniCroftPipelineConfig(unittest.TestCase):
    """Tests for MiniCroft pipeline_config parameter."""

    def setUp(self):
        LOG.set_level("ERROR")

    def tearDown(self):
        LOG.set_level("CRITICAL")

    def test_pipeline_config_patches_intents_config(self):
        """pipeline_config must be visible under Configuration()['intents'] while running."""
        from ovos_config.config import Configuration

        captured_config = {}

        # We can only inspect the live config after MiniCroft starts.
        mc = get_minicroft(
            [],
            pipeline_config={"test_plugin": {"key": "patched_value"}},
        )
        try:
            cfg = Configuration()
            captured_config = cfg.get("intents", {}).get("test_plugin", {})
        finally:
            mc.stop()

        self.assertEqual(captured_config.get("key"), "patched_value",
                         "pipeline_config entry should appear in Configuration()['intents']")

    def test_pipeline_config_restored_after_stop(self):
        """After stop(), Configuration()['intents']['test_plugin'] is removed."""
        from ovos_config.config import Configuration

        mc = get_minicroft(
            [],
            pipeline_config={"test_plugin_restore": {"key": "value"}},
        )
        mc.stop()

        cfg = Configuration()
        remaining = cfg.get("intents", {}).get("test_plugin_restore")
        self.assertIsNone(remaining,
                          "pipeline_config entry should be removed after stop()")

    def test_pipeline_config_preserves_existing_key(self):
        """If the plugin key already existed, it is restored (not deleted) after stop()."""
        from ovos_config.config import Configuration

        # Pre-seed the intents config with an existing entry
        cfg = Configuration()
        intents_cfg = cfg.setdefault("intents", {})
        intents_cfg["pre_existing_plugin"] = {"key": "original"}

        try:
            mc = get_minicroft(
                [],
                pipeline_config={"pre_existing_plugin": {"key": "overridden"}},
            )
            # Confirm override is active
            live_val = Configuration().get("intents", {}).get("pre_existing_plugin", {})
            self.assertEqual(live_val.get("key"), "overridden")
            mc.stop()
            # Confirm original value is restored
            restored_val = Configuration().get("intents", {}).get("pre_existing_plugin", {})
            self.assertEqual(restored_val.get("key"), "original",
                             "original pipeline config should be restored after stop()")
        finally:
            # Clean up the pre-seeded key so it doesn't leak into other tests
            cfg.get("intents", {}).pop("pre_existing_plugin", None)

    def test_pipeline_config_none_does_not_patch(self):
        """pipeline_config=None must not modify Configuration()['intents']."""
        from ovos_config.config import Configuration

        before = dict(Configuration().get("intents", {}))
        mc = get_minicroft([], pipeline_config=None)
        try:
            after = dict(Configuration().get("intents", {}))
            # The intents dict may differ (pipeline key is patched by default_pipeline
            # logic), but no new arbitrary keys should appear from pipeline_config=None
            # Verify no unexpected keys were introduced compared to before
            new_keys = set(after.keys()) - set(before.keys())
            pipeline_keys = {"pipeline", "blacklisted_intents"}
            unexpected = new_keys - pipeline_keys
            self.assertEqual(unexpected, set(),
                             f"pipeline_config=None introduced unexpected keys: {unexpected}")
        finally:
            mc.stop()

    def test_pipeline_config_multiple_keys(self):
        """Multiple pipeline_config entries are all patched and all restored."""
        from ovos_config.config import Configuration

        mc = get_minicroft(
            [],
            pipeline_config={
                "plugin_a": {"model": "a"},
                "plugin_b": {"threshold": 0.5},
            },
        )
        try:
            cfg = Configuration().get("intents", {})
            self.assertEqual(cfg.get("plugin_a", {}).get("model"), "a")
            self.assertEqual(cfg.get("plugin_b", {}).get("threshold"), 0.5)
        finally:
            mc.stop()

        cfg_after = Configuration().get("intents", {})
        self.assertIsNone(cfg_after.get("plugin_a"))
        self.assertIsNone(cfg_after.get("plugin_b"))


class TestMiniCroftNamespaceBridging(unittest.TestCase):
    """MiniCroft is the e2e harness bus. Utterances are injected on the LEGACY
    topic (``recognizer_loop:utterance``); these tests pin that MiniCroft's
    FakeBus bridges that to/from the ovos.* SPEC topic
    (``ovos.utterance.handle``) so an e2e test can drive EITHER namespace, and
    that disabling the bridge isolates a single namespace.
    """

    def setUp(self):
        LOG.set_level("ERROR")

    def tearDown(self):
        LOG.set_level("CRITICAL")

    def _emit_and_collect(self, mc, emit_topic, watch_topic, *, timeout=3.0):
        seen = []
        got = threading.Event()

        def _on(msg):
            if isinstance(msg, str):
                msg = Message.deserialize(msg)
            seen.append(msg)
            got.set()

        mc.bus.on(watch_topic, _on)
        try:
            mc.bus.emit(Message(
                emit_topic,
                data={"utterances": ["hello world"], "lang": "en-US"},
            ))
            got.wait(timeout)
        finally:
            mc.bus.remove(watch_topic, _on)
        return seen

    def test_legacy_utterance_observed_on_spec_topic(self):
        """Default (bridging on): a legacy recognizer_loop:utterance injected
        into the e2e harness is observed on ovos.utterance.handle (modernize)."""
        mc = get_minicroft([])  # modernize/emit_legacy default on
        try:
            seen = self._emit_and_collect(mc, LEGACY_UTTERANCE, SPEC_UTTERANCE)
            self.assertTrue(seen, "legacy utterance was not bridged to the spec topic")
            self.assertEqual(seen[0].data["utterances"], ["hello world"])
        finally:
            mc.stop()

    def test_spec_utterance_reaches_legacy_listener(self):
        """An utterance injected on the SPEC topic reaches a LEGACY listener
        (emit_legacy) — the intent pipeline keys off the legacy topic."""
        mc = get_minicroft([])
        try:
            seen = self._emit_and_collect(mc, SPEC_UTTERANCE, LEGACY_UTTERANCE)
            self.assertTrue(seen, "spec utterance was not bridged to the legacy topic")
            self.assertEqual(seen[0].data["utterances"], ["hello world"])
        finally:
            mc.stop()

    def test_no_bridging_isolates_legacy_from_spec(self):
        """With bridging OFF, a legacy emit does NOT reach a spec-only
        subscriber — the e2e harness exercises a single isolated namespace."""
        mc = get_minicroft([], modernize=False, emit_legacy=False)
        try:
            seen = self._emit_and_collect(mc, LEGACY_UTTERANCE, SPEC_UTTERANCE,
                                          timeout=0.5)
            self.assertEqual(seen, [],
                             "legacy emit must not reach the spec topic when bridging is off")
        finally:
            mc.stop()


class TestSkillManagerKwargCompat(unittest.TestCase):
    """The latest ovoscope must boot against older ovos-core SkillManager
    releases that predate newer ``enable_*`` keyword arguments (the stable
    release channel ships ovos-core 1.3.x, whose SkillManager has no
    ``enable_installer``). MiniCroft must forward only the flags the installed
    SkillManager actually accepts instead of raising TypeError and failing to
    boot."""

    def test_unsupported_enable_kwargs_are_dropped(self):
        import ovoscope
        from unittest.mock import patch

        captured = {}

        class _Reached(Exception):
            """Raised from the fake old __init__ once kwarg filtering passed."""

        # Simulate an OLD SkillManager whose signature lacks enable_installer,
        # enable_file_watcher, enable_intent_service and enable_event_scheduler.
        def old_init(self, bus=None, enable_skill_api=True):
            captured["bus"] = bus
            captured["enable_skill_api"] = enable_skill_api
            raise _Reached

        with patch.object(ovoscope.SkillManager, "__init__", old_init):
            # If filtering failed, old_init would get enable_installer=... and
            # raise TypeError *before its body* -> captured stays empty.
            try:
                MiniCroft([SKILL_ID])
            except Exception:
                pass

        self.assertTrue(
            captured,
            "SkillManager.__init__ was never reached: an unsupported kwarg "
            "was forwarded, so the latest ovoscope cannot boot on older core")
        self.assertTrue(captured["enable_skill_api"],
                        "a supported enable_* flag must still be forwarded")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Trained-quiet-window wait (get_minicroft)
# ---------------------------------------------------------------------------
class RegistersIntentSkill(OVOSSkill):
    """Emits 'register_intent' with context['skill_id'] set, exactly as
    _AdaptIntentApi.register_intent (ovos_workshop.intents) always stamps
    it before emitting, but never fires 'mycroft.skills.trained' itself —
    used to simulate a pipeline plugin that never reports training done."""

    def initialize(self):
        self.bus.emit(Message("register_intent", {"name": "unittest.stub"},
                              {"skill_id": self.skill_id}))


class RegistersAndTrainsSkill(OVOSSkill):
    """Emits 'register_intent' (with context['skill_id'] stamped, as adapt
    does) then 'mycroft.skills.trained', like a real pipeline plugin
    finishing a training pass."""

    def initialize(self):
        self.bus.emit(Message("register_intent", {"name": "unittest.stub"},
                              {"skill_id": self.skill_id}))
        self.bus.emit(Message("mycroft.skills.trained"))


class StuckTrainerSkill(OVOSSkill):
    """Subscribes a no-op handler to 'mycroft.skills.train' (so a trainer
    is present on the bus) then registers an intent, exactly like
    RegistersIntentSkill, but never emits 'mycroft.skills.trained' — used
    to simulate a pipeline plugin that starts a training pass and never
    finishes it."""

    def initialize(self):
        self.bus.on("mycroft.skills.train", lambda message: None)
        self.bus.emit(Message("register_intent", {"name": "unittest.stub"},
                              {"skill_id": self.skill_id}))


class TestTrainedQuietWindow(unittest.TestCase):

    def setUp(self):
        LOG.set_level("ERROR")

    def tearDown(self):
        LOG.set_level("CRITICAL")

    def test_no_intents_registered_skips_wait(self):
        """Nothing registered an intent -> get_minicroft must not wait on
        'mycroft.skills.trained' at all (mirrors padatious' needs_compile:
        nothing to train, nothing to wait for)."""
        skill_id = "ovoscope-unittest-no-intents.test"
        mc = get_minicroft([skill_id], extra_skills={skill_id: PingSkill})
        try:
            self.assertEqual(mc._registered_skill_ids, set())
            self.assertEqual(mc._trained_times, [])
        finally:
            mc.stop()

    def test_trained_event_lets_quiet_window_elapse_and_return(self):
        """An intent was registered and 'mycroft.skills.trained' arrived ->
        get_minicroft must record it and return once the quiet window
        elapses, without raising."""
        skill_id = "ovoscope-unittest-trains.test"
        mc = get_minicroft([skill_id],
                           extra_skills={skill_id: RegistersAndTrainsSkill})
        try:
            self.assertEqual(mc._registered_skill_ids, {skill_id})
            self.assertEqual(len(mc._trained_times), 1)
        finally:
            mc.stop()

    @patch.dict("os.environ", {"OVOSCOPE_TRAINED_TIMEOUT": "0.3"})
    def test_never_trained_raises_naming_skill(self):
        """A trainer is subscribed to 'mycroft.skills.train' (StuckTrainerSkill)
        and an intent was registered, but 'mycroft.skills.trained' never
        arrives within the bound -> get_minicroft must raise loudly, never
        proceed silently as if it were READY and trained. Booted on the
        adapt pipeline so no padatious-family plugin can emit the reply
        behind the test's back."""
        skill_id = "ovoscope-unittest-stuck.test"
        with self.assertRaises(RuntimeError) as ctx:
            get_minicroft([skill_id],
                          extra_skills={skill_id: StuckTrainerSkill},
                          default_pipeline=ADAPT_PIPELINE)
        self.assertIn(skill_id, str(ctx.exception))
        self.assertIn("mycroft.skills.trained", str(ctx.exception))

    @patch.dict("os.environ", {"OVOSCOPE_TRAINED_TIMEOUT": "0.3"})
    def test_never_trained_raises_naming_only_stuck_skill(self):
        """A mixed load: one skill subscribes a trainer to
        'mycroft.skills.train', registers an intent, and never gets
        trained; another skill registers no intent at all. The raised
        error must name ONLY the stuck skill — an intentless skill loaded
        alongside a hung trainer must never be blamed. Booted on the adapt
        pipeline so no padatious-family plugin can emit the reply behind
        the test's back."""
        stuck_id = "ovoscope-unittest-stuck-mixed.test"
        intentless_id = "ovoscope-unittest-intentless-mixed.test"
        with self.assertRaises(RuntimeError) as ctx:
            get_minicroft([stuck_id, intentless_id],
                          extra_skills={stuck_id: StuckTrainerSkill,
                                        intentless_id: PingSkill},
                          default_pipeline=ADAPT_PIPELINE)
        message = str(ctx.exception)
        self.assertIn(stuck_id, message)
        self.assertNotIn(intentless_id, message)

    def test_wait_for_trained_false_opts_out(self):
        """wait_for_trained=False must skip the wait/raise entirely even
        when an intent was registered and never trained."""
        skill_id = "ovoscope-unittest-optout.test"
        mc = get_minicroft([skill_id],
                           extra_skills={skill_id: RegistersIntentSkill},
                           wait_for_trained=False)
        try:
            self.assertEqual(mc._registered_skill_ids, {skill_id})
            self.assertEqual(mc._trained_times, [])
        finally:
            mc.stop()

    @patch.dict("os.environ", {"OVOSCOPE_TRAINED_TIMEOUT": "0.3"})
    def test_no_trainer_subscribed_skips_wait(self):
        """m2v registers intents but no plugin on the bus ever subscribes to
        'mycroft.skills.train', so nothing will ever emit
        'mycroft.skills.trained' -> get_minicroft must return at READY
        instead of waiting out the bound and raising (OpenVoiceOS/ovoscope#179)."""
        if not is_pipeline_available(M2V_PIPELINE):
            raise unittest.SkipTest("ovos-m2v-pipeline plugin not installed")
        skill_id = "ovoscope-unittest-m2v-no-trainer.test"
        mc = get_minicroft([skill_id],
                           extra_skills={skill_id: RegistersIntentSkill},
                           default_pipeline=M2V_PIPELINE)
        try:
            self.assertEqual(mc._registered_skill_ids, {skill_id})
            self.assertEqual(mc._trained_times, [])
            self.assertEqual(mc.bus.ee.listeners("mycroft.skills.train"), [])
        finally:
            mc.stop()

    @patch.dict("os.environ", {"OVOSCOPE_TRAINED_TIMEOUT": "0.3"})
    def test_no_trainer_subscribed_skips_wait_adapt_only(self):
        """Same as above with adapt, which is always installed, so this
        regression is exercised in every run of the suite, never skipped."""
        skill_id = "ovoscope-unittest-adapt-no-trainer.test"
        mc = get_minicroft([skill_id],
                           extra_skills={skill_id: RegistersIntentSkill},
                           default_pipeline=ADAPT_PIPELINE)
        try:
            self.assertEqual(mc._registered_skill_ids, {skill_id})
            self.assertEqual(mc._trained_times, [])
            self.assertEqual(mc.bus.ee.listeners("mycroft.skills.train"), [])
        finally:
            mc.stop()

    @pytest.mark.padatious
    def test_lean_default_pipeline_has_a_trainer(self):
        """Detection premise: the lean default (padacioso/padatious) does
        subscribe to 'mycroft.skills.train'. If this ever stops holding,
        the wait-skip logic above would start skipping every boot's wait
        silently, so this must never regress unnoticed. LIGHT_TEST_PIPELINE
        (padacioso only) has no trainer at all, so it cannot stand in for
        the premise here — this needs ovoscope[padatious] (see the
        dedicated CI lane) rather than falling back to the wrong
        pipeline."""
        skill_id = "ovoscope-unittest-lean-has-trainer.test"
        mc = get_minicroft([skill_id], extra_skills={skill_id: PingSkill},
                           default_pipeline=LEAN_DEFAULT_PIPELINE, wait_for_trained=False)
        try:
            self.assertTrue(mc.bus.ee.listeners("mycroft.skills.train"))
        finally:
            mc.stop()


class TestMiniCroftLeanBootDefault(unittest.TestCase):
    """The lean default pipeline must boot ONLY the matcher families the
    ovoscope suite (and skill-fixture suites built on it) actually assert
    against — heavier installed pipeline plugins (m2v, persona, common_query,
    OCP, ...) must never be instantiated by default, and must stay opt-in via
    `extra_pipelines=`/`default_pipeline=`.
    """

    def setUp(self):
        LOG.set_level("ERROR")

    def tearDown(self):
        LOG.set_level("CRITICAL")

    def test_lean_default_excludes_heavy_pipelines(self):
        """LEAN_DEFAULT_PIPELINE must not reference m2v/persona/common_query/OCP."""
        for stage in LEAN_DEFAULT_PIPELINE:
            self.assertNotIn("m2v", stage, f"m2v stage found: {stage}")
            self.assertNotIn("persona", stage, f"persona stage found: {stage}")
            self.assertNotIn("common-query", stage, f"common_query stage found: {stage}")
            self.assertNotIn("ocp", stage, f"OCP stage found: {stage}")
            self.assertNotIn("-low", stage, f"-low tier stage found: {stage}")

    def test_lean_default_boots_only_lean_plugins(self):
        """A lean-default MiniCroft must not instantiate heavy pipeline
        plugins that ARE installed but not part of the lean set — this is
        the actual fix: `intents.pipeline` alone does not stop IntentService
        from loading every installed plugin, only `blacklisted_pipelines`
        does.
        """
        if not is_pipeline_available(LEAN_DEFAULT_PIPELINE):
            raise unittest.SkipTest("lean pipeline plugins not installed")
        mc = get_minicroft([], wait_for_trained=False)
        try:
            loaded = set(mc.intents.pipeline_plugins.keys())
            for heavy in ("ovos-m2v-pipeline", "ovos-m2v-prototype-pipeline",
                         "ovos-persona-pipeline-plugin",
                         "ovos-common-query-pipeline-plugin",
                         "ovos-ocp-pipeline-plugin",
                         "ovos-ocp-pipeline-plugin-legacy"):
                self.assertNotIn(heavy, loaded,
                                 f"{heavy} was instantiated by a lean-default MiniCroft")
            self.assertIn("ovos-adapt-pipeline-plugin", loaded)
            self.assertIn("ovos-stop-pipeline-plugin", loaded)
        finally:
            mc.stop()

    def test_extra_pipelines_appends_to_lean_default(self):
        """extra_pipelines= appends stages on top of the lean default,
        without the caller having to restate the whole lean list."""
        if not is_pipeline_available(LEAN_DEFAULT_PIPELINE + M2V_PIPELINE):
            raise unittest.SkipTest("lean + m2v pipeline plugins not installed")
        mc = get_minicroft([], extra_pipelines=M2V_PIPELINE, wait_for_trained=False)
        try:
            for stage in LEAN_DEFAULT_PIPELINE:
                self.assertIn(stage, mc.pipeline)
            for stage in M2V_PIPELINE:
                self.assertIn(stage, mc.pipeline)
            loaded = set(mc.intents.pipeline_plugins.keys())
            self.assertIn("ovos-m2v-pipeline", loaded)
        finally:
            mc.stop()

    def test_default_pipeline_full_override_still_works(self):
        """default_pipeline= remains a full override — it replaces the lean
        default entirely rather than extending it."""
        mc = get_minicroft([], default_pipeline=ADAPT_PIPELINE, wait_for_trained=False)
        try:
            self.assertEqual(mc.pipeline, ADAPT_PIPELINE)
            for stage in LEAN_DEFAULT_PIPELINE:
                if stage not in ADAPT_PIPELINE:
                    self.assertNotIn(stage, mc.pipeline)
        finally:
            mc.stop()

    def test_bogus_pipeline_id_raises_naming_it(self):
        """A configured-but-unloadable pipeline id must raise, naming it —
        never silently vanish the way the m2v/adapt hole did."""
        bogus = "ovos-definitely-not-a-real-pipeline-plugin-high"
        with self.assertRaises(RuntimeError) as ctx:
            get_minicroft([], default_pipeline=[bogus], wait_for_trained=False)
        self.assertIn(bogus, str(ctx.exception))



class TestTrainedTimeoutDefaults(unittest.TestCase):
    """Verify that the OVOSCOPE_TRAINED_TIMEOUT default is 60s in CI and 5s locally.

    This is a regression test ensuring the timeout scales appropriately: CI
    (slower, cold caches) gets a generous default, while local runs stay tight.
    """

    def setUp(self):
        LOG.set_level("ERROR")
        import os as os_module
        self.os_module = os_module

    def tearDown(self):
        LOG.set_level("CRITICAL")

    def test_ci_default_timeout_is_180_seconds(self):
        """When CI=true, the default computed timeout must be 180s."""
        # Test the logic: when CI env var is present, default should be 180s
        with patch.dict("os.environ", {"CI": "true"}):
            timeout = 180.0 if self.os_module.environ.get("CI") else 5.0
            self.assertEqual(timeout, 180.0,
                             "CI default timeout must be 180s to accommodate cold caches, "
                             "coverage instrumentation, and contended runners")

    def test_local_default_timeout_is_5_seconds(self):
        """When CI is not set, the default computed timeout must be 5s."""
        # Test the logic: when CI is absent, default should be 5s
        with patch.dict("os.environ", {}, clear=True):
            timeout = 60.0 if self.os_module.environ.get("CI") else 5.0
            self.assertEqual(timeout, 5.0,
                             "Local default timeout must be 5s for fast iteration")

    def test_ovoscope_trained_timeout_honors_env_var(self):
        """The OVOSCOPE_TRAINED_TIMEOUT env var is honored over the computed default."""
        with patch.dict("os.environ", {"OVOSCOPE_TRAINED_TIMEOUT": "120"}):
            timeout_str = self.os_module.environ.get("OVOSCOPE_TRAINED_TIMEOUT")
            timeout = float(timeout_str) if timeout_str else None
            self.assertEqual(timeout, 120.0,
                             "OVOSCOPE_TRAINED_TIMEOUT env var should be respected")
