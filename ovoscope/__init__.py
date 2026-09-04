import inspect
import dataclasses
import gc
import json
import os
import threading
from copy import deepcopy
from time import sleep, time
from typing import Union, List, Dict, Any, Optional

from ovos_bus_client.message import Message
from ovos_bus_client.session import SessionManager, Session
from ovos_config.config import Configuration
from ovos_config.models import LocalConf
from ovos_core.intent_services import IntentService
from ovos_core.skill_manager import SkillManager
from ovos_plugin_manager.skills import find_skill_plugins
from ovos_utils.fakebus import FakeBus
from ovos_utils.log import LOG
from ovos_utils.process_utils import ProcessState
from ovos_spec_tools import SpecMessage
from ovos_spec_tools.messages import MIGRATION_MAP, SPEC_TO_LEGACY
from ovos_workshop.skills.api import SkillApi
from ovos_workshop.skills.ovos import OVOSSkill

SerializedMessage = Dict[str, Union[str, Dict[str, Any]]]
SerializedTest = Dict[str, Union[str, bool, List[str], SerializedMessage]]

# Bus orchestration noise that is not part of any scenario's own message
# sequence: it fires from MiniCroft's boot/training machinery (see
# get_minicroft's trained-quiet-window wait) rather than from a skill or
# pipeline reacting to a captured utterance, and can legitimately interleave
# with a capture window (e.g. a pipeline plugin re-training for a
# secondary_langs pass). Sequence comparisons filter this set out and then
# stay strictly exact on what remains — never subsequence-matched, or a
# lost/duplicated real message would slip through undetected.
TRAINING_NOISE = ["mycroft.skills.trained"]

DEFAULT_IGNORED = ["ovos.skills.settings_changed"] + TRAINING_NOISE
GUI_IGNORED = ["gui.clear.namespace",
               "gui.value.set",
               "mycroft.gui.screen.close",
               "gui.page.show"]
DEFAULT_EOF = ["ovos.utterance.handled"]
# The pipeline's own terminal marker: an utterance's lifecycle is over the
# instant this lands, matched or not. ``ovos.utterance.handled`` is the
# universal §9.5 end-marker — it fires exactly once, always, after every
# other signal on the utterance's path (matched, unmatched, or fallback) —
# so it alone is safe to use to end capture early without dropping trailing
# messages. Pre-terminal signals such as ``ovos.intent.unmatched`` and its
# legacy bridge ``complete_intent_failure`` fire *before* this marker and
# must not be added here. See CaptureSession.capture's ``terminal_signals``
# kwarg.
TERMINAL_SIGNALS = ["ovos.utterance.handled"]
DEFAULT_ENTRY_POINTS = ["recognizer_loop:utterance"]
DEFAULT_FLIP_POINTS = []
DEFAULT_KEEP_SRC = ["ovos.skills.fallback.ping"]
DEFAULT_ACTIVATION = []
DEFAULT_DEACTIVATION = ["intent.service.skills.deactivate"]

# ---------------------------------------------------------------------------
# Pipeline stage groups — combine as needed for test scenarios
# ---------------------------------------------------------------------------
STOP_PIPELINE = [
    "ovos-stop-pipeline-plugin-high",
    "ovos-stop-pipeline-plugin-medium",
    "ovos-stop-pipeline-plugin-low",
]
CONVERSE_PIPELINE = ["ovos-converse-pipeline-plugin"]
ADAPT_PIPELINE = [
    "ovos-adapt-pipeline-plugin-high",
    "ovos-adapt-pipeline-plugin-medium",
    "ovos-adapt-pipeline-plugin-low",
]
PADATIOUS_PIPELINE = [
    "ovos-padatious-pipeline-plugin-high",
    "ovos-padatious-pipeline-plugin-medium",
    "ovos-padatious-pipeline-plugin-low",
]
# Padacioso is a pure-Python Padatious-compatible engine (no swig/C required).
# It ships as a dependency of ovos-workshop so is always available in any OVOS
# skill test environment.  Use this in place of PADATIOUS_PIPELINE when you
# don't want a swig build dep in CI or when ovos-padatious is not installed.
PADACIOSO_PIPELINE = [
    "ovos-padacioso-pipeline-plugin-high",
    "ovos-padacioso-pipeline-plugin-medium",
    "ovos-padacioso-pipeline-plugin-low",
]
FALLBACK_PIPELINE = [
    "ovos-fallback-pipeline-plugin-high",
    "ovos-fallback-pipeline-plugin-medium",
    "ovos-fallback-pipeline-plugin-low",
]
COMMON_QUERY_PIPELINE = ["ovos-common-query-pipeline-plugin"]
PERSONA_PIPELINE = [
    "ovos-persona-pipeline-plugin-high",
    "ovos-persona-pipeline-plugin-low",
]
M2V_PIPELINE = [
    "ovos-m2v-pipeline-high",
    "ovos-m2v-pipeline-medium",
    "ovos-m2v-pipeline-low",
]
# Nebulento — fuzzy intent matching (ConfidenceMatcherPipeline). Single OPM
# entry point; the pipeline manager handles confidence-tier routing.
NEBULENTO_PIPELINE = ["ovos-nebulento-pipeline-plugin"]
# Palavreado — keyword/slot intent parser (ConfidenceMatcherPipeline).
PALAVREADO_PIPELINE = ["palavreado"]

# Lean default pipeline — the matcher families the ovoscope test suite (and
# every skill-fixture/harness suite built on it) actually asserts against:
# stop (mandatory — skills assert stop behavior fleet-wide and the default
# config always runs it), converse, adapt, padatious/padacioso and fallback,
# high+medium tiers only. Nothing here references the `-low` tier or
# common_query — grep the ovoscope test suite (test/unittests/*.py) turns up
# no assertion on either, so they are left out of the boot-by-default set.
# Anything heavier (m2v, persona, OCP, common_query, `-low` tiers, ...) is
# opt-in via `extra_pipelines=` or a full `default_pipeline=` override — see
# docs/minicroft.md. This is what MiniCroft/get_minicroft boot by default;
# it also drives `intents.blacklisted_pipelines` so the *other* installed
# pipeline plugins (e.g. ovos-m2v-pipeline, whose handler does a synchronous
# sleep(3) on init) are never instantiated in the first place — merely
# leaving a plugin out of `intents.pipeline` does NOT stop IntentService from
# loading it; only the blacklist does.
LEAN_DEFAULT_PIPELINE = [
    "ovos-stop-pipeline-plugin-high",
    "ovos-stop-pipeline-plugin-medium",
    "ovos-converse-pipeline-plugin",
    "ovos-adapt-pipeline-plugin-high",
    "ovos-adapt-pipeline-plugin-medium",
    "ovos-padatious-pipeline-plugin-high",
    "ovos-padatious-pipeline-plugin-medium",
    "ovos-padacioso-pipeline-plugin-high",
    "ovos-padacioso-pipeline-plugin-medium",
    "ovos-fallback-pipeline-plugin-high",
    "ovos-fallback-pipeline-plugin-medium",
]

# Standard test pipeline — all standard built-in stages.
# This requires ovos-adapt-pipeline-plugin and ovos-padatious-pipeline-plugin.
# If these are not installed, use LIGHT_TEST_PIPELINE instead.
DEFAULT_TEST_PIPELINE = [
    "ovos-stop-pipeline-plugin-high",
    "ovos-converse-pipeline-plugin",
    "ovos-adapt-pipeline-plugin-high",
    "ovos-padatious-pipeline-plugin-high",
    "ovos-padacioso-pipeline-plugin-high",
    "ovos-adapt-pipeline-plugin-medium",
    "ovos-padatious-pipeline-plugin-medium",
    "ovos-padacioso-pipeline-plugin-medium",
    "ovos-common-query-pipeline-plugin",
    "ovos-adapt-pipeline-plugin-low",
    "ovos-padatious-pipeline-plugin-low",
    "ovos-padacioso-pipeline-plugin-low",
    "ovos-fallback-pipeline-plugin-high",
    "ovos-fallback-pipeline-plugin-medium",
    "ovos-fallback-pipeline-plugin-low",
    "ovos-stop-pipeline-plugin-medium",
]

# ---------------------------------------------------------------------------
# Global bus-coverage state (managed by pytest plugin or CLI)
# ---------------------------------------------------------------------------
GLOBAL_BUS_COVERAGE: bool = False
GLOBAL_BUS_COVERAGE_FILE: Optional[str] = None


class GlobalBusCoverageCollector:
    """Accumulates bus events globally across all FakeBus instances."""
    def __init__(self):
        # msg_type -> count
        self.invocations: Dict[str, int] = {}
        # msg_type -> count (total times .on was called for this type)
        self.registrations: Dict[str, int] = {}
        # skill_id -> {msg_type -> count}
        self.skill_registrations: Dict[str, Dict[str, int]] = {}

    def record_invocation(self, msg_type: str):
        self.invocations[msg_type] = self.invocations.get(msg_type, 0) + 1

    def record_registration(self, msg_type: str):
        self.registrations[msg_type] = self.registrations.get(msg_type, 0) + 1

    def record_skill_registration(self, skill_id: str, msg_type: str):
        if not skill_id:
            return
        if skill_id not in self.skill_registrations:
            self.skill_registrations[skill_id] = {}
        self.skill_registrations[skill_id][msg_type] = (
            self.skill_registrations[skill_id].get(msg_type, 0) + 1
        )

    def rename_skill(self, old_id: str, new_id: str):
        """Merge registrations from old_id into new_id."""
        if not old_id or not new_id or old_id == new_id:
            return
        if old_id in self.skill_registrations:
            old_data = self.skill_registrations.pop(old_id)
            if new_id not in self.skill_registrations:
                self.skill_registrations[new_id] = {}
            for mt, count in old_data.items():
                self.skill_registrations[new_id][mt] = (
                    self.skill_registrations[new_id].get(mt, 0) + count
                )


GLOBAL_BUS_COVERAGE_COLLECTOR: Optional[GlobalBusCoverageCollector] = None


def _patch_fakebus():
    """Monkey-patch FakeBus and ovos-workshop classes to track global coverage."""
    from ovos_utils.fakebus import FakeBus
    
    original_on = FakeBus.on
    original_once = getattr(FakeBus, "once", None)
    original_emit = FakeBus.emit

    def patched_on(self, event, handler):
        if GLOBAL_BUS_COVERAGE and GLOBAL_BUS_COVERAGE_COLLECTOR:
            GLOBAL_BUS_COVERAGE_COLLECTOR.record_registration(event)
        return original_on(self, event, handler)

    def patched_once(self, event, handler):
        if GLOBAL_BUS_COVERAGE and GLOBAL_BUS_COVERAGE_COLLECTOR:
            GLOBAL_BUS_COVERAGE_COLLECTOR.record_registration(event)
        if original_once:
            return original_once(self, event, handler)
        return original_on(self, event, handler)

    def patched_emit(self, message):
        if GLOBAL_BUS_COVERAGE and GLOBAL_BUS_COVERAGE_COLLECTOR:
            msg_type = getattr(message, "msg_type", None) or getattr(message, "type", None)
            if msg_type:
                GLOBAL_BUS_COVERAGE_COLLECTOR.record_invocation(msg_type)
        return original_emit(self, message)

    FakeBus.on = patched_on
    FakeBus.once = patched_once
    FakeBus.emit = patched_emit

    # --- Patch ovos-workshop for better attribution ---
    try:
        from ovos_workshop.skills.ovos import OVOSSkill
        original_add_event = OVOSSkill.add_event
        original_bind = OVOSSkill.bind

        def patched_add_event(self, name, handler, *args, **kwargs):
            if GLOBAL_BUS_COVERAGE and GLOBAL_BUS_COVERAGE_COLLECTOR:
                # Fallback to .name if skill_id is not yet set
                sid = getattr(self, "skill_id", None) or getattr(self, "name", None)
                if sid:
                    GLOBAL_BUS_COVERAGE_COLLECTOR.record_skill_registration(sid, name)
            return original_add_event(self, name, handler, *args, **kwargs)

        def patched_bind(self, bus):
            if GLOBAL_BUS_COVERAGE and GLOBAL_BUS_COVERAGE_COLLECTOR:
                old_id = getattr(self, "skill_id", None) or getattr(self, "name", None)
                res = original_bind(self, bus)
                new_id = getattr(self, "skill_id", None)
                if old_id and new_id and old_id != new_id:
                    GLOBAL_BUS_COVERAGE_COLLECTOR.rename_skill(old_id, new_id)
                return res
            return original_bind(self, bus)

        OVOSSkill.add_event = patched_add_event
        OVOSSkill.bind = patched_bind
    except ImportError:
        pass

    try:
        from ovos_utils.events import EventContainer
        original_container_add = EventContainer.add

        def patched_container_add(self, name, handler, once=False):
            if GLOBAL_BUS_COVERAGE and GLOBAL_BUS_COVERAGE_COLLECTOR:
                # EventContainer usually belongs to a skill, but we don't have easy
                # access to skill_id here without more complex patching.
                # However, many skills call self.add_event which we already patched.
                pass
            return original_container_add(self, name, handler, once)
        # EventContainer.add = patched_container_add
    except ImportError:
        pass


# Apply the patch immediately when ovoscope is imported
_patch_fakebus()

# Lightweight test pipeline — no C extensions (swig) required.
# Uses only pure-Python stages that are dependencies of ovos-core/workshop.
# Use this when you want fast CI without building Padatious or Adapt.
LIGHT_TEST_PIPELINE = [
    "ovos-stop-pipeline-plugin-high",
    "ovos-converse-pipeline-plugin",
    "ovos-padacioso-pipeline-plugin-high",
    "ovos-padacioso-pipeline-plugin-medium",
    "ovos-padacioso-pipeline-plugin-low",
    "ovos-fallback-pipeline-plugin-high",
    "ovos-fallback-pipeline-plugin-medium",
    "ovos-fallback-pipeline-plugin-low",
    "ovos-stop-pipeline-plugin-medium",
]

DEFAULT_PIPELINE_UNSET = object()


def is_pipeline_available(pipeline: List[str]) -> bool:
    """Return True if all pipeline stages in *pipeline* are currently installed.

    Uses ``importlib.metadata`` to check entry points — no FakeBus or IntentService
    is created, so this is cheap to call at class setup time.

    Example::

        import unittest
        from ovoscope import is_pipeline_available, M2V_PIPELINE

        class TestM2V(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                if not is_pipeline_available(M2V_PIPELINE):
                    raise unittest.SkipTest("ovos-m2v-pipeline not installed")
    """
    import importlib.metadata
    installed_bases: set = set()
    try:
        # Python 3.10+ entry_points API
        for ep in importlib.metadata.entry_points(group="opm.pipeline"):
            installed_bases.add(ep.name)
    except TypeError:
        # Fallback for older metadata API if needed
        for ep in importlib.metadata.entry_points().get("opm.pipeline", []):
            installed_bases.add(ep.name)

    for stage in pipeline:
        base = stage
        for suffix in ("-high", "-medium", "-low"):
            if stage.endswith(suffix):
                base = stage[: -len(suffix)]
                break
        if base not in installed_bases:
            return False
    return True


def _pipeline_base_id(stage: str) -> str:
    """Strip a confidence-tier suffix (``-high``/``-medium``/``-low``) off a
    pipeline matcher id, returning the installed OPM plugin id it maps to.
    """
    for suffix in ("-high", "-medium", "-low"):
        if stage.endswith(suffix):
            return stage[: -len(suffix)]
    return stage


def _compute_pipeline_blacklist(pipeline: List[str]) -> List[str]:
    """Return the installed ``opm.pipeline`` plugin ids NOT covered by
    *pipeline*.

    IntentService loads every installed pipeline plugin except the ones
    listed in ``intents.blacklisted_pipelines`` — ``intents.pipeline`` alone
    only orders/selects among already-loaded matchers, it does not stop the
    others from being instantiated. So keeping a boot lean requires setting
    the blacklist to "everything installed that this pipeline doesn't need".
    """
    import importlib.metadata

    try:
        installed = {ep.name for ep in
                     importlib.metadata.entry_points(group="opm.pipeline")}
    except TypeError:
        installed = {ep.name for ep in
                     importlib.metadata.entry_points().get("opm.pipeline", [])}
    wanted = {_pipeline_base_id(stage) for stage in pipeline}
    return sorted(installed - wanted)


class MiniCroft(SkillManager):
    def __init__(self, skill_ids,
                 enable_installer=False,
                 enable_intent_service=True,
                 enable_event_scheduler=False,
                 enable_file_watcher=False,
                 enable_skill_api=True,
                 extra_skills: Optional[Dict[str, OVOSSkill]] = None,
                 isolate_config: bool = True,
                 default_pipeline: Optional[List[str]] = DEFAULT_PIPELINE_UNSET,
                 extra_pipelines: Optional[List[str]] = None,
                 lang: Optional[str] = None,
                 secondary_langs: Optional[List[str]] = None,
                 pipeline_config: Optional[Dict[str, Dict]] = None,
                 modernize: bool = True,
                 emit_legacy: bool = True,
                 get_response_timeout: float = 2.0,
                 *args, **kwargs):
        # Namespace-migration flags forwarded to the harness FakeBus so callers
        # can choose which bus namespace(s) to exercise:
        #   modernize=True   emitting a legacy topic ALSO emits the ovos.* spec
        #                    topic (legacy producer -> spec listener)
        #   emit_legacy=True emitting an ovos.* spec topic ALSO emits the legacy
        #                    topic (spec producer -> legacy listener)
        # Both default on (mirrors MessageBusClient). Set BOTH False to isolate a
        # single namespace and assert no cross-namespace bridging occurs.
        self._modernize = modernize
        self._emit_legacy = emit_legacy
        self._isolated_config = isolate_config
        self._original_xdg_configs: Optional[List[LocalConf]] = None

        # SessionManager.bus is a process-wide class attribute. IntentService
        # (constructed below via super().__init__()) calls
        # SessionManager.connect_to_bus(self.bus) in its own __init__,
        # clobbering it with this instance's FakeBus. If left in place after
        # stop(), SessionManager.wait_while_speaking()'s `if not cls.bus`
        # guard sees a stale, truthy, dead bus and blocks/registers listeners
        # on it instead of whatever bus a later test expects — polluting
        # every subsequent test in the process. Snapshot it before booting so
        # stop() can restore it.
        self._original_sm_bus = SessionManager.bus

        # SkillApi.bus is another process-wide class attribute. SkillManager
        # calls SkillApi.connect_bus(self.bus) during boot, which pins this
        # instance's FakeBus — and, through the bus handlers, the whole
        # MiniCroft object graph (~633MB per stopped instance measured on the
        # ovoscope suite). Snapshot it so stop() can put it back and let the
        # instance be collected.
        self._original_skill_api_bus = SkillApi.bus

        # SessionManager.default_session is a process-wide singleton. Booting a
        # MiniCroft (and running a test through it) mutates it in several ways:
        # run() overrides pipeline/lang, End2EndTest.execute() calls
        # activate_skill() for `inject_active`, and any message that carries a
        # session with id "default" folds its wire values onto the live object.
        # Snapshot the whole thing so stop() can put it back exactly as found —
        # otherwise every later test in the process inherits the mutation.
        self._default_session_obj = SessionManager.default_session
        try:
            # ovos-bus-client 2.x names these to_dict/from_dict; 1.x uses
            # serialize/deserialize. Support both — a silent None here would
            # quietly disable the whole restore.
            sess_obj = self._default_session_obj
            dump = getattr(sess_obj, "to_dict", None)
            if dump is not None:
                # ovos-bus-client 2.x
                self._session_api = "dict"
            else:
                # ovos-bus-client 1.x
                dump = sess_obj.serialize
                self._session_api = "legacy"
            self._default_session_state = deepcopy(dump())
        except Exception:  # pragma: no cover - defensive, session_cls may vary
            LOG.warning("ovoscope: could not snapshot the default session; "
                        "state mutated by tests will NOT be restored")
            self._default_session_state = None
            self._session_api = None
        # active_skills as found at boot. Restored even when the full snapshot
        # failed, so a skill activated during the test never survives teardown.
        try:
            self._default_active_skills = deepcopy(
                self._default_session_obj.active_skills)
        except Exception:  # pragma: no cover - defensive
            self._default_active_skills = None

        # Orphaned TTS timers (see _mock_tts below) would fire on a closed bus
        # after stop() and corrupt the global SessionManager during a LATER
        # test. Track them so stop() can cancel them.
        self._tts_timers: List[threading.Timer] = []
        self._tts_timers_lock = threading.Lock()

        # get_response()/ask_yesno() spawn a killable background thread
        # (OVOSSkill._real_wait_response) and the calling thread busy-polls
        # for its result. With `num_retries=-1` (the OVOSSkill default) that
        # thread re-prompts and waits forever if nothing ever answers it —
        # there is no ceiling on the calling thread's wait either. On a real
        # voice satellite this eventually gets a `mycroft.skills.abort_question`
        # from the listener/GUI on user silence or session teardown; the
        # synchronous FakeBus never generates one. Track the pending
        # "wait for an answer" timers here so stop() can cancel them the same
        # way it cancels the mock-TTS ones.
        # Keyed by (skill_id, session_id) — the SAME two-part scope upstream's
        # own @killable_event("mycroft.skills.abort_question",
        # check_skill_id=True) uses to decide which stalled thread an abort
        # is actually for (session_id match + optional skill_id match). A
        # flat list here would let one skill's `.disable` cancel a DIFFERENT
        # skill's (or a different concurrent session's) still-pending
        # watchdog, leaving it to hang forever again in a multi-skill
        # MiniCroft where two get_response() calls are in flight at once.
        self._get_response_timeout = get_response_timeout
        self._get_response_timers: Dict[tuple, threading.Timer] = {}
        self._get_response_timers_lock = threading.Lock()
        # Guards the `_stopped` flag against the mock-TTS emits. A plain
        # `if not self._stopped: bus.emit(...)` is a TOCTOU: stop() can flip the
        # flag and close the bus between the check and the emit, so the emit
        # lands on a dead bus and folds a stale session onto the global
        # SessionManager. Holding this lock across BOTH the check+emit and the
        # flag flip makes the two mutually exclusive. Re-entrant because an emit
        # can re-enter the mock-TTS handler on the same thread.
        self._stop_lock = threading.RLock()
        self._stopped = False

        if default_pipeline is DEFAULT_PIPELINE_UNSET:
            if is_pipeline_available(LEAN_DEFAULT_PIPELINE):
                self._default_pipeline = list(LEAN_DEFAULT_PIPELINE)
            elif is_pipeline_available(DEFAULT_TEST_PIPELINE):
                self._default_pipeline = list(DEFAULT_TEST_PIPELINE)
            else:
                self._default_pipeline = list(LIGHT_TEST_PIPELINE)
        else:
            self._default_pipeline = default_pipeline

        # extra_pipelines appends matcher ids on top of whatever base was
        # chosen above (the lean default, or a caller's `default_pipeline=`
        # override) — a heavyweight suite that needs e.g. m2v says
        # `extra_pipelines=M2V_PIPELINE` instead of restating the whole lean
        # list. Deduped, order-preserving; a bare `default_pipeline=None`
        # (explicitly "leave the pipeline alone") is left untouched.
        if extra_pipelines and self._default_pipeline is not None:
            merged = list(self._default_pipeline)
            for stage in extra_pipelines:
                if stage not in merged:
                    merged.append(stage)
            self._default_pipeline = merged

        self._original_pipeline: Optional[List[str]] = None
        self._original_cfg_pipeline: Optional[List[str]] = None
        self._had_cfg_pipeline: bool = False
        self._original_blacklisted_skills: Optional[List[str]] = None
        self._had_blacklisted_skills: bool = False
        self._original_blacklisted_intents: Optional[List[str]] = None
        self._had_blacklisted_intents: bool = False
        self._lang = lang
        self._secondary_langs = secondary_langs
        self._original_lang: Optional[str] = None
        self._original_cfg_lang: Optional[str] = None
        self._had_lang: bool = False
        self._original_secondary_langs: Optional[List[str]] = None
        self._had_secondary_langs: bool = False
        self._pipeline_config: Optional[Dict[str, Dict]] = pipeline_config
        self._original_pipeline_configs: Dict[str, Optional[Dict]] = {}
        self._had_pipeline_configs: Dict[str, bool] = {}

        if isolate_config:
            # Replace user XDG configs (e.g. ~/.config/mycroft/mycroft.conf) with
            # an empty list so the user's installed pipeline, locale, and other
            # preferences do not affect test results.  System config
            # (/etc/mycroft/mycroft.conf) and built-in defaults are still used.
            # Note: LocalConf(None) cannot be used here — its reload() calls
            # os.stat(None) and raises TypeError.  An empty list is safe.
            self._original_xdg_configs = Configuration.xdg_configs[:]
            Configuration.xdg_configs = []
            Configuration.reload()
            LOG.debug("ovoscope: user config isolated (xdg_configs cleared)")

        # Patch lang / secondary_langs BEFORE super().__init__() because
        # IntentService (and thus Adapt/Padatious) reads Configuration()
        # during construction and creates per-language engines at that time.
        if self._lang is not None or self._secondary_langs is not None:
            cfg = Configuration()
            if self._lang is not None:
                self._had_lang = "lang" in cfg
                self._original_cfg_lang = cfg.get("lang")
                cfg["lang"] = self._lang
                LOG.debug(f"ovoscope: lang set to '{self._lang}' "
                          f"(was '{self._original_cfg_lang}')")
            if self._secondary_langs is not None:
                self._had_secondary_langs = "secondary_langs" in cfg
                self._original_secondary_langs = cfg.get("secondary_langs")
                cfg["secondary_langs"] = self._secondary_langs
                LOG.debug(f"ovoscope: secondary_langs set to "
                          f"{self._secondary_langs}")

        # Patch per-pipeline config BEFORE super().__init__() so that pipeline
        # plugins read the overridden values during their __init__.
        # pipeline_config is a dict keyed by pipeline plugin config key
        # (the key used under Configuration()["intents"]), e.g.:
        #   {"ovos_m2v_pipeline": {"model": "Jarbas/ovos-model2vec-..."}}
        if self._pipeline_config:
            cfg = Configuration()
            intents_cfg = cfg.setdefault("intents", {})
            for plugin_key, plugin_cfg in self._pipeline_config.items():
                self._had_pipeline_configs[plugin_key] = plugin_key in intents_cfg
                self._original_pipeline_configs[plugin_key] = intents_cfg.get(plugin_key)
                intents_cfg[plugin_key] = plugin_cfg
                LOG.debug(f"ovoscope: pipeline_config patched '{plugin_key}'")

        # Blacklist every installed pipeline plugin the chosen pipeline
        # doesn't need, BEFORE super().__init__() constructs IntentService.
        # `intents.pipeline` (patched later, in run()) only orders/selects
        # among matchers IntentService already instantiated at construction
        # time — it does NOT stop the others from loading. Without this,
        # a lean `default_pipeline`/LEAN_DEFAULT_PIPELINE still boots every
        # OTHER installed pipeline plugin (e.g. ovos-m2v-pipeline, whose
        # handle_sync_intents does a synchronous sleep(3) on init) and gains
        # nothing.
        self._original_blacklisted_pipelines: Optional[List[str]] = None
        self._had_blacklisted_pipelines: bool = False
        if self._default_pipeline is not None:
            cfg = Configuration()
            intents_cfg = cfg.setdefault("intents", {})
            self._had_blacklisted_pipelines = "blacklisted_pipelines" in intents_cfg
            self._original_blacklisted_pipelines = intents_cfg.get("blacklisted_pipelines")
            intents_cfg["blacklisted_pipelines"] = _compute_pipeline_blacklist(
                self._default_pipeline)
            LOG.debug(f"ovoscope: blacklisted_pipelines set to "
                      f"{intents_cfg['blacklisted_pipelines']}")

        self.boot_messages: List[Message] = []
        bus = FakeBus(modernize=self._modernize,
                      emit_legacy=self._emit_legacy)
        bus.on("message", self.handle_boot_message)

        # TTS mock: speak_dialog(…, wait=True) blocks in wait_while_speaking on
        # recognizer_loop:audio_output_end. With no real TTS that event never
        # arrives, so the handler stalls until the dispatcher's §8.3 timeout. We
        # emit audio_output_start synchronously (duck) and schedule a short-delay
        # audio_output_end (unduck) to simulate the full TTS playback lifecycle.
        def _mock_tts(message):
            with self._stop_lock:
                if self._stopped:
                    return
                # TTS playback begins — duck immediately.
                # message.forward copies source/destination/session from the
                # speak, matching what the real audio service would do.
                bus.emit(message.forward("recognizer_loop:audio_output_start"))

            def _unduck():
                # stop() may have run while the timer was pending — emitting on
                # a closed bus here would fold a stale session onto the global
                # SessionManager and poison the next test. The lock makes the
                # check and the emit atomic against stop().
                with self._stop_lock:
                    if self._stopped:
                        return
                    bus.emit(message.forward("recognizer_loop:audio_output_end"))

            # TTS playback ends after a short delay — unduck.
            # Daemon + tracked so stop() can cancel it and the interpreter can
            # exit even if one is still pending.
            timer = threading.Timer(0.1, _unduck)
            timer.daemon = True
            with self._tts_timers_lock:
                self._tts_timers = [t for t in self._tts_timers if t.is_alive()]
                self._tts_timers.append(timer)
            timer.start()

        bus.on(SpecMessage.SPEAK, _mock_tts)

        # get_response()/ask_yesno() mock: OVOSSkill.get_response() emits
        # "skill.converse.get_response.enable" and then blocks the calling
        # thread until a "<skill_id>.converse.get_response" answer arrives or
        # the killable thread is aborted via "mycroft.skills.abort_question".
        # If a test doesn't inject a follow-up utterance, nothing ever answers
        # it and (with the OVOSSkill default `num_retries=-1`) the skill
        # re-prompts and waits forever. Arm a short watchdog on `.enable`
        # that fires the SAME "mycroft.skills.abort_question" a real listener
        # would send on user silence — this is existing bus-protocol, not a
        # new message type. "`.disable" (emitted once get_response() actually
        # returns, whether answered or cancelled) cancels the watchdog.
        def _arm_get_response_watchdog(message):
            with self._stop_lock:
                if self._stopped:
                    return
                skill_id = message.data.get("skill_id")
                session_id = SessionManager.get(message).session_id
                key = (skill_id, session_id)

                def _abort():
                    with self._stop_lock:
                        if self._stopped:
                            return
                        with self._get_response_timers_lock:
                            # Already disarmed (answered/cancelled) between
                            # the Timer firing and this lock — nothing to do.
                            if self._get_response_timers.get(key) is not timer:
                                return
                            del self._get_response_timers[key]
                        bus.emit(message.forward("mycroft.skills.abort_question",
                                                 {"skill_id": skill_id}))

                timer = threading.Timer(self._get_response_timeout, _abort)
                timer.daemon = True
                with self._get_response_timers_lock:
                    old = self._get_response_timers.get(key)
                    if old is not None and old.is_alive():
                        old.cancel()
                    self._get_response_timers[key] = timer
                timer.start()

        def _disarm_get_response_watchdog(message):
            # This ONE get_response() call returned (answered, cancelled,
            # retries exhausted, or already aborted by our own watchdog) —
            # cancel only ITS entry. Other (skill_id, session_id) pairs with
            # their own in-flight get_response() must keep their watchdog
            # armed (this is exactly what the flat-list version got wrong:
            # any skill's `.disable` cancelled every pending watchdog).
            skill_id = message.data.get("skill_id")
            session_id = SessionManager.get(message).session_id
            key = (skill_id, session_id)
            with self._get_response_timers_lock:
                timer = self._get_response_timers.pop(key, None)
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:
                    pass

        bus.on("skill.converse.get_response.enable", _arm_get_response_watchdog)
        bus.on("skill.converse.get_response.disable", _disarm_get_response_watchdog)

        self.skill_ids = skill_ids
        self.extra_skills = extra_skills or {}

        # Older ovos-core SkillManager releases (e.g. the stable release
        # channel's 1.3.x) predate some of these keyword arguments. Passing an
        # unknown kwarg to them raises TypeError and MiniCroft cannot boot,
        # which makes the latest ovoscope unusable against an older core (the
        # conformance harness exercises exactly this against pinned stable /
        # testing stacks). Forward only the enable_* flags the *installed*
        # SkillManager actually accepts, so one ovoscope boots on every core.
        _enable_flags = {
            "enable_installer": enable_installer,
            "enable_skill_api": enable_skill_api,
            "enable_file_watcher": enable_file_watcher,
            "enable_intent_service": enable_intent_service,
            "enable_event_scheduler": enable_event_scheduler,
        }
        try:
            _accepted = inspect.signature(SkillManager.__init__).parameters
        except (ValueError, TypeError):
            _accepted = {}
        _has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD
                          for p in _accepted.values())
        _supported = {k: v for k, v in _enable_flags.items()
                      if _has_var_kw or k in _accepted}
        _dropped = [k for k in _enable_flags if k not in _supported]
        if _dropped:
            LOG.debug(f"installed SkillManager does not accept {_dropped}; "
                      f"omitting for backwards compatibility")

        try:
            super().__init__(bus, *args, **_supported, **kwargs)
        except Exception:
            # If super().__init__ fails (e.g. plugin construction error),
            # ensure global Configuration() is restored.
            self.stop()
            raise

        # Track training readiness from the moment the bus exists — NOT after
        # load_plugin_skills() emits "mycroft.skills.train" in run() (which
        # runs on a separate thread started by start()). Subscribing late
        # risks missing the very first "mycroft.skills.trained" reply if a
        # pipeline plugin (e.g. padatious) answers before get_minicroft()
        # gets around to waiting for it.
        self._trained_times: List[float] = []
        # Populated with the skill_id of every register_intent /
        # padatious:register_intent event observed, so a stuck-trainer
        # timeout can name only the skill(s) that actually asked to be
        # trained instead of every skill_id passed to get_minicroft (a
        # 5-skill load with one hung trainer must not blame the other 4).
        self._registered_skill_ids: set = set()
        self._training_lock = threading.Lock()
        self.bus.on("mycroft.skills.trained", self._on_skills_trained)
        self.bus.on("register_intent", self._on_intent_registered)
        self.bus.on("padatious:register_intent", self._on_intent_registered)

    def _on_skills_trained(self, message: Message):
        with self._training_lock:
            self._trained_times.append(time())

    def _on_intent_registered(self, message: Message):
        # register_intent/padatious:register_intent always carry skill_id,
        # either in context (set by OVOSSkill.bus on every outgoing message)
        # or, for padatious, in data as a fallback (ovos_padatious.opm
        # defaults it to "anonymous_skill" if genuinely absent).
        skill_id = message.data.get("skill_id") or (message.context or {}).get("skill_id")
        if not skill_id:
            skill_id = "anonymous_skill"
        with self._training_lock:
            self._registered_skill_ids.add(skill_id)

    @property
    def pipeline(self) -> List[str]:
        """Return the current active pipeline stages for this instance.

        Returns:
            List of stage IDs.
        """
        return self._default_pipeline

    def handle_boot_message(self, message: str):
        self.boot_messages.append(Message.deserialize(message))

    def load_metadata_transformers(self, cfg):
        self.intent_service.metadata_plugins.config = cfg
        self.intent_service.metadata_plugins.load_plugins()

    def load_plugin_skills(self):
        LOG.info("loading skill plugins")
        plugins = find_skill_plugins()
        for skill_id, plug in plugins.items():
            LOG.debug(f"Found skill: {skill_id}")
            if skill_id not in self.skill_ids:
                continue
            if skill_id not in self.plugin_skills:
                self._load_plugin_skill(skill_id, plug)
                LOG.info(f"Loaded skill: {skill_id}")

        for skill_id, plug in self.extra_skills.items():
            LOG.debug(f"Injected test skill: {skill_id}")
            if skill_id not in self.plugin_skills:
                self._load_plugin_skill(skill_id, plug)
                LOG.info(f"Loaded test skill: {skill_id}")

        self.bus.emit(Message("mycroft.skills.train"))  # tell any pipeline plugins to train loaded intents

    def _check_pipeline_available(self, pipeline: List[str]) -> bool:
        """Check if all stages in *pipeline* can be served by IntentService.

        Returns:
            bool: True if all stages are available, False if any are missing.
        """
        available = set(self.intents.pipeline_plugins.keys())
        missing = []
        for stage in pipeline:
            # Strip priority suffix to get the base plugin ID
            base = stage
            for suffix in ("-high", "-medium", "-low"):
                if stage.endswith(suffix):
                    base = stage[: -len(suffix)]
                    break
            if base not in available:
                missing.append(stage)
        if missing:
            LOG.warning(
                f"ovoscope: Pipeline stage(s) not installed: {missing}\n"
                f"Installed pipeline plugins: {sorted(available)}\n"
                f"Missing package(s) suggested:\n"
                f"  ADAPT_PIPELINE  → pip install ovos-adapt-pipeline-plugin\n"
                f"  PADATIOUS_PIPELINE → pip install ovos-padatious-pipeline-plugin\n"
                f"  PADACIOSO_PIPELINE → already bundled with ovos-workshop\n"
            )
            return False
        return True

    def run(self):
        """Load skills and mark core as ready to start tests"""
        self.status.set_alive()
        self.load_plugin_skills()
        if self._default_pipeline is not None:
            if not self._check_pipeline_available(self._default_pipeline):
                if self._default_pipeline in (LEAN_DEFAULT_PIPELINE, DEFAULT_TEST_PIPELINE):
                    LOG.info("ovoscope: falling back to LIGHT_TEST_PIPELINE")
                    self._default_pipeline = LIGHT_TEST_PIPELINE
                else:
                    LOG.error("ovoscope: specified pipeline is missing stages; "
                              "test results may be unreliable")

            # Two-pronged pipeline override:
            #
            # 1. SessionManager.default_session — controls sessions created from
            #    messages that carry NO explicit session context (e.g. bare
            #    `Message("recognizer_loop:utterance", ...)` without session).
            #
            # 2. Configuration()["intents"]["pipeline"] — controls sessions
            #    created via `Session()` constructor, which reads
            #    `Configuration().get('intents', {}).get('pipeline')`.
            #    Note: Configuration.reload() does NOT invalidate the dict cache
            #    in-place, so we must patch the live singleton directly.
            self._original_pipeline = SessionManager.default_session.pipeline[:]
            SessionManager.default_session.pipeline = self._default_pipeline
            cfg = Configuration()
            intents_cfg = cfg.get("intents", {})
            self._had_cfg_pipeline = "pipeline" in intents_cfg
            self._original_cfg_pipeline = intents_cfg.get("pipeline")
            if "intents" not in cfg:
                cfg["intents"] = {}
            cfg["intents"]["pipeline"] = self._default_pipeline
            LOG.debug(f"ovoscope: default session pipeline set "
                      f"({len(self._default_pipeline)} stages, "
                      f"was {len(self._original_pipeline)})")
        if self._lang is not None:
            self._original_lang = SessionManager.default_session.lang
            SessionManager.default_session.lang = self._lang
        if self._isolated_config:
            # Session.__init__ reads Configuration()["skills"]["blacklisted_skills"]
            # and Configuration()["intents"]["blacklisted_intents"] from the live
            # singleton dict cache (not invalidated by reload()), so we must patch
            # the cache directly — same pattern as the pipeline patch above.
            cfg = Configuration()
            skills_cfg = cfg.setdefault("skills", {})
            intents_cfg = cfg.setdefault("intents", {})
            self._had_blacklisted_skills = "blacklisted_skills" in skills_cfg
            self._original_blacklisted_skills = skills_cfg.get("blacklisted_skills")
            self._had_blacklisted_intents = "blacklisted_intents" in intents_cfg
            self._original_blacklisted_intents = intents_cfg.get("blacklisted_intents")
            skills_cfg["blacklisted_skills"] = []
            intents_cfg["blacklisted_intents"] = []
            LOG.debug("ovoscope: blacklisted_skills and blacklisted_intents cleared")
        LOG.info("Skills all loaded!")
        self.status.set_ready()
        self.bus.remove("message", self.handle_boot_message)

    def inject_message(self, msg: Message) -> None:
        """Emit an arbitrary message onto the FakeBus during a test.

        Use this to trigger non-utterance skill handlers — e.g., timer events,
        GUI events, or skill API calls — without going through the utterance pipeline.
        """
        self.bus.emit(msg)

    def stop(self):
        # Flip the flag and take the pending timers under `_stop_lock`, so a
        # mock-TTS emit that is already past its `_stopped` check finishes on a
        # live bus before teardown starts, and none can start afterwards.
        with self._stop_lock:
            self._stopped = True
            # Cancel any pending mock-TTS unduck timers BEFORE closing the bus,
            # so none of them can emit onto a dead bus (and fold a stale
            # "default" session onto the process-wide SessionManager).
            with self._tts_timers_lock:
                timers, self._tts_timers = self._tts_timers, []
            with self._get_response_timers_lock:
                gr_timers = list(self._get_response_timers.values())
                self._get_response_timers = {}
        timers = timers + gr_timers
        for t in timers:
            try:
                t.cancel()
            except Exception:
                pass
        for t in timers:
            try:
                t.join(timeout=1.0)
            except Exception:
                pass
        try:
            super().stop()
        except Exception:
            pass
        if hasattr(self, "bus") and self.bus:
            try:
                # pyee's EventEmitter.remove_all_listeners() holds its internal,
                # non-reentrant lock while dropping self._events. The "defence
                # in depth" block near the end of this method (below) calls
                # exactly that, on this same bus's emitter, to release
                # listener references. If dropping the last reference to a
                # bound-method listener there runs that listener owner's
                # __del__, and that __del__ calls bus.remove()/
                # remove_listener(), the __del__ runs synchronously inside
                # the locked block and deadlocks trying to re-acquire the
                # same lock (30-minute CI hangs). Drain the listener dict
                # ourselves here, outside the lock, and force any pending
                # __del__ to run now instead, before that later call ever
                # takes the lock -- by the time it runs, _events is already
                # empty, so it is a safe no-op. Same workaround as the
                # minicroft fixture in ovos-skill-application-launcher's
                # test/end2end/test_intents_en_us.py (PR #108).
                ee = getattr(self.bus, "ee", None)
                if ee is not None:
                    events = getattr(ee, "_events", None)
                    if events is not None:
                        for key in list(events.keys()):
                            events.pop(key, None)
                        gc.collect()
            except Exception:
                pass
            try:
                self.bus.close()
            except Exception:
                pass
        if self._default_pipeline is not None and self._original_pipeline is not None:
            SessionManager.default_session.pipeline = self._original_pipeline
            cfg = Configuration()
            if "intents" in cfg:
                if self._had_cfg_pipeline:
                    cfg["intents"]["pipeline"] = self._original_cfg_pipeline
                else:
                    cfg["intents"].pop("pipeline", None)
            LOG.debug("ovoscope: default session pipeline restored")
        if self._default_pipeline is not None:
            cfg = Configuration()
            intents_cfg = cfg.setdefault("intents", {})
            if self._had_blacklisted_pipelines:
                intents_cfg["blacklisted_pipelines"] = self._original_blacklisted_pipelines
            else:
                intents_cfg.pop("blacklisted_pipelines", None)
            LOG.debug("ovoscope: blacklisted_pipelines restored")
        if self._isolated_config:
            cfg = Configuration()
            skills_cfg = cfg.get("skills", {})
            intents_cfg = cfg.get("intents", {})
            if self._had_blacklisted_skills:
                skills_cfg["blacklisted_skills"] = self._original_blacklisted_skills
            else:
                skills_cfg.pop("blacklisted_skills", None)
            if self._had_blacklisted_intents:
                intents_cfg["blacklisted_intents"] = self._original_blacklisted_intents
            else:
                intents_cfg.pop("blacklisted_intents", None)
            LOG.debug("ovoscope: blacklisted_skills and blacklisted_intents restored")
        if self._lang is not None:
            cfg = Configuration()
            if self._had_lang:
                cfg["lang"] = self._original_cfg_lang
            else:
                cfg.pop("lang", None)
            SessionManager.default_session.lang = self._original_lang
            LOG.debug(f"ovoscope: lang restored to '{self._original_lang}'")
        if self._secondary_langs is not None:
            cfg = Configuration()
            if self._had_secondary_langs:
                cfg["secondary_langs"] = self._original_secondary_langs
            else:
                cfg.pop("secondary_langs", None)
            LOG.debug("ovoscope: secondary_langs restored")
        if self._pipeline_config:
            cfg = Configuration()
            intents_cfg = cfg.get("intents", {})
            for plugin_key in self._pipeline_config:
                if self._had_pipeline_configs.get(plugin_key):
                    intents_cfg[plugin_key] = self._original_pipeline_configs[plugin_key]
                else:
                    intents_cfg.pop(plugin_key, None)
            LOG.debug("ovoscope: pipeline_config restored")
        if self._isolated_config and self._original_xdg_configs is not None:
            Configuration.xdg_configs = self._original_xdg_configs
            Configuration.reload()
            LOG.debug("ovoscope: user config restored")
        SessionManager.bus = self._original_sm_bus
        LOG.debug("ovoscope: SessionManager.bus restored")
        SkillApi.bus = self._original_skill_api_bus
        LOG.debug("ovoscope: SkillApi.bus restored")
        # Defence in depth: drop every handler still registered on this
        # instance's bus. Handlers are bound methods of the skills and of this
        # MiniCroft, so anything that still holds the bus would otherwise keep
        # the whole object graph alive.
        bus = getattr(self, "bus", None)
        if bus is not None:
            ee = getattr(bus, "ee", None)
            if ee is not None:
                try:
                    ee.remove_all_listeners()
                except Exception:
                    pass
            for attr in ("_handler_guards", "_dedup_registrations"):
                container = getattr(bus, attr, None)
                if container is not None:
                    try:
                        container.clear()
                    except Exception:
                        pass
        self._restore_default_session()

    def _restore_default_session(self):
        """Put the process-wide default Session back as it was before boot.

        The explicit pipeline / lang restores above only cover what run()
        changed. Tests also mutate the default session through
        ``activate_skill`` (``End2EndTest.inject_active``) and through any
        message carrying a ``"default"`` session, which folds its wire values
        onto the singleton. Restoring the full snapshot keeps that mutation
        inside the test that caused it.
        """
        state = getattr(self, "_default_session_state", None)
        if state is None:
            # The snapshot failed. Do not degrade to a total no-op: skills
            # activated during the run are the mutation that leaks hardest,
            # so put active_skills back explicitly.
            sess = SessionManager.default_session
            active = getattr(self, "_default_active_skills", None)
            if sess is not None and active is not None:
                try:
                    sess.active_skills = deepcopy(active)
                    LOG.debug("ovoscope: default session active_skills restored "
                              "(snapshot unavailable)")
                except Exception:
                    LOG.warning("ovoscope: could not restore active_skills")
            return
        # Restore onto whatever object is the default session NOW: boot can
        # replace the singleton (SessionManager.reset_default_session), and
        # mutations after the swap land on the new object — bailing out on an
        # identity mismatch would leak exactly the state this exists to scrub.
        sess = SessionManager.default_session
        if sess is None:
            return
        # Rebuild a pristine Session from the snapshot and copy every field
        # onto the live object. Copying only the snapshot keys is not enough:
        # to_dict() OMITS empty fields, so a skill activated during the test
        # would have no key to restore and would survive teardown.
        # Load with the SAME API family that produced the snapshot. to_dict()
        # and serialize() do not share a wire format on every version, so
        # pairing to_dict() output with deserialize() (or the reverse) silently
        # rebuilds a wrong session.
        try:
            if self._session_api == "dict":
                load = type(sess).from_dict
            else:
                load = type(sess).deserialize
            fresh = load(deepcopy(state))
        except Exception:
            LOG.warning("ovoscope: could not rebuild the default session from "
                        "its snapshot; leaked state will NOT be restored")
            return
        # Copy every instance attribute, including underscore-prefixed ones:
        # some ovos-bus-client versions back public fields (active_skills,
        # utterance_states, ...) with private storage, and skipping those
        # would leave the mutation in place.
        for key, value in vars(fresh).items():
            try:
                setattr(sess, key, value)
            except Exception:
                # read-only / computed field — skip it
                continue
        LOG.debug("ovoscope: default session state restored")


# How long a quiet window (no new "mycroft.skills.trained" event) must hold
# before we consider training settled. Kept short and non-tunable: it only
# absorbs the gap between successive per-language training passes, it is not
# meant to paper over a slow trainer (that's what OVOSCOPE_TRAINED_TIMEOUT is
# for).
TRAINED_QUIET_WINDOW = 0.5

# The overall bound on the trained-wait is env-tunable so CI (slower, cold
# caches, contended runners) gets a generous default while local runs stay
# tight. Presence of the CI env var (not its value) selects the default.
# CI default is 180s: worst-case uninstrumented on taskset-2 was 16.8s, but
# fleet CI jobs run under coverage instrumentation on throttled 2-core shared
# VMs where a large single-skill intent set exceeded 60s in the field (weather:
# 262 trained-timeout failures at 60s; the alerts multilang fixture
# independently documents "under coverage instrumentation, booting reliably
# needs more than 60s"). 180s serves the real condition, costs nothing on
# healthy boots (quiet-window return), and the loud never-trained guard still
# fires.
_DEFAULT_TRAINED_TIMEOUT = 180.0 if os.environ.get("CI") else 5.0


def get_minicroft(skill_ids: Union[List[str], str], *args,
                  max_wait: float = 60, wait_for_trained: bool = True,
                  **kwargs) -> MiniCroft:
    """Create a MiniCroft, start it, and block until it reaches READY state.

    Once READY, and unless ``wait_for_trained=False``, this also waits for
    "mycroft.skills.trained" to go quiet (no new event for
    ``TRAINED_QUIET_WINDOW`` seconds) before returning — but only if a loaded
    skill actually registered an intent (``register_intent`` /
    ``padatious:register_intent``), mirroring padatious' own
    ``needs_compile`` gate: nothing to train means nothing to wait for.

    Timeout behavior: On timeout waiting for training, get_minicroft's
    exception handler calls croft.stop(), which stops the MiniCroft process and
    kills background training threads. A timeout guard that is too tight can
    mask a slow trainer: the except block's stop() kills the thread before
    training completes, making a slow-but-successful event look like it never
    arrived. Testing shows training can arrive 3.5–4.0 seconds after READY
    when MiniCroft is kept alive; this is why the default timeout is
    conservative and suites with many secondary languages should pass their own
    larger max_wait (see "Multilingual Testing" in docs/minicroft.md).
    Callers' pytest-timeout must exceed this wait's ceiling with margin; see
    "pytest-timeout Convention" in docs/minicroft.md.

    Args:
        skill_ids: One or more skill plugin IDs to load.
        max_wait: Maximum seconds to wait for READY before raising TimeoutError.
        wait_for_trained: Wait for the trained-quiet-window after READY.
            Set False to opt out (e.g. skills with no intents to train).

    Raises:
        TimeoutError: If MiniCroft does not reach READY within ``max_wait`` seconds.
        RuntimeError: If a loaded skill registered intents but
            "mycroft.skills.trained" never arrives within
            ``OVOSCOPE_TRAINED_TIMEOUT`` seconds, OR if any pipeline id in
            the configured pipeline (the lean default, an
            ``extra_pipelines=`` addition, or a full ``default_pipeline=``
            override) failed to load — a missing/erroring plugin is never
            silently dropped.
    """
    if isinstance(skill_ids, str):
        skill_ids = [skill_ids]
    assert isinstance(skill_ids, list)
    croft = MiniCroft(skill_ids, *args, **kwargs)
    try:
        croft.start()
        deadline = time() + max_wait
        while croft.status.state != ProcessState.READY:
            if time() > deadline:
                raise TimeoutError(
                    f"MiniCroft did not reach READY in {max_wait}s — "
                    f"check skill startup logs (skill_ids={skill_ids})"
                )
            sleep(0.1)

        if croft._default_pipeline is not None:
            loaded = set(croft.intents.pipeline_plugins.keys())
            missing = sorted({
                stage for stage in croft._default_pipeline
                if _pipeline_base_id(stage) not in loaded
            })
            if missing:
                raise RuntimeError(
                    "MiniCroft: configured pipeline stage(s) failed to "
                    f"load: {missing} — plugin absent or errored during "
                    "init (see logs above for the load failure); a "
                    "configured-but-unloadable matcher is never silently "
                    "skipped"
                )

        with croft._training_lock:
            registered = set(croft._registered_skill_ids)
        if wait_for_trained and registered:
            timeout = float(os.environ.get("OVOSCOPE_TRAINED_TIMEOUT",
                                            _DEFAULT_TRAINED_TIMEOUT))
            trained_deadline = time() + timeout
            while True:
                with croft._training_lock:
                    times = list(croft._trained_times)
                now = time()
                if not times:
                    if now > trained_deadline:
                        # "mycroft.skills.trained" carries no skill_id — it
                        # reports a pipeline plugin's container(s), not a
                        # single skill — so it can't attribute which of
                        # several registered skills is the one still stuck.
                        # Name the full registered set (never the untouched
                        # skill_ids param: an intentless skill in the same
                        # load must not be blamed).
                        raise RuntimeError(
                            "MiniCroft: skill(s) registered intents but "
                            f"'mycroft.skills.trained' never arrived within "
                            f"{timeout}s (untrained skill_ids="
                            f"{sorted(registered)}) — the pipeline plugin's "
                            "intent container(s) never finished training"
                        )
                elif now - max(times) >= TRAINED_QUIET_WINDOW:
                    break
                elif now > trained_deadline:
                    LOG.warning(
                        "MiniCroft: 'mycroft.skills.trained' kept firing "
                        f"past the {timeout}s bound (skill_ids="
                        f"{sorted(registered)}); proceeding without "
                        "reaching a quiet window"
                    )
                    break
                sleep(0.05)
        return croft
    except BaseException:
        # pytest-timeout's Failed and KeyboardInterrupt derive from
        # BaseException, not Exception; catching only Exception here let
        # them skip cleanup and leak the started MiniCroft process.
        croft.stop()
        raise


@dataclasses.dataclass()
class CaptureSession:
    minicroft: MiniCroft
    responses: List[Message] = dataclasses.field(default_factory=list)
    async_responses: List[Message] = dataclasses.field(default_factory=list)

    eof_msgs: List[str] = dataclasses.field(default_factory=lambda: DEFAULT_EOF)
    # end capture only after an eof message has been seen this many times. Use >1
    # when the scenario produces N concurrent lifecycles that each terminate on the
    # same eof topic (e.g. two ovos.utterance.handled — one per utterance — when a
    # stop interrupts a running skill), so capture spans all of them.
    eof_count: int = 1
    # Merge the pipeline's own TERMINAL_SIGNALS into eof_msgs so capture ends
    # the moment an utterance's lifecycle is over, matched or not, instead of
    # only on whatever topic the caller happened to list (a caller chasing a
    # different deadlock, e.g. get_response(), commonly narrows eof_msgs down
    # to a single mid-pipeline topic that an unmatched/misrouted utterance
    # never reaches, paying the full timeout every time). Left off when
    # eof_count > 1: that knob means the caller is counting occurrences of ONE
    # topic across several concurrent lifecycles, and an unmatched utterance
    # firing two terminal topics (unmatched + handled) would inflate the count
    # and end capture before every lifecycle actually finished.
    terminal_signals: bool = True
    ignore_messages: List[str] = dataclasses.field(default_factory=lambda: DEFAULT_IGNORED)
    async_messages: List[str] = dataclasses.field(default_factory=list) # these come from an external thread and might come in any order
    done: threading.Event = dataclasses.field(default_factory=lambda: threading.Event())
    _eof_lock: threading.Lock = dataclasses.field(default_factory=lambda: threading.Lock())
    _eof_seen: int = 0
    # Handlers are registered in __post_init__, long before the first capture()
    # and again between captures. An eof arriving outside a capture window (a
    # late message from a previous scenario, or a skill emitting the eof topic
    # on its own) must not count towards the next capture, or the next capture
    # returns immediately with an empty message list and the test passes
    # vacuously. Only an ARMED session counts eofs, and only for the generation
    # that armed it.
    _armed: bool = False
    _generation: int = 0
    _done_generation: int = -1
    # set by capture() when the eof condition was never reached
    timed_out: bool = False
    timeout_seconds: Optional[float] = None

    def handle_message(self, msg: str):
        if self.done.is_set():
            return
        msg = Message.deserialize(msg)
        if msg.msg_type in self.async_messages:
            self.async_responses.append(msg)
        elif msg.msg_type not in self.ignore_messages:
            self.responses.append(msg)

    def handle_end_of_test(self, msg: Message):
        with self._eof_lock:
            if not self._armed:
                return
            self._eof_seen += 1
            if self._eof_seen >= self.eof_count:
                self._armed = False
                self._done_generation = self._generation
                self.done.set()

    def _effective_eof_msgs(self) -> List[str]:
        topics = list(self.eof_msgs)
        if self.terminal_signals and self.eof_count == 1:
            for sig in TERMINAL_SIGNALS:
                if sig not in topics:
                    topics.append(sig)
        return topics

    def __post_init__(self):
        self.minicroft.bus.on("message", self.handle_message)
        for m in self._effective_eof_msgs():
            self.minicroft.bus.on(m, self.handle_end_of_test)

    def capture(self, source_message: Message, timeout=20) -> bool:
        """Emit *source_message* and block until an eof message or *timeout*.

        Returns:
            True if the eof condition was reached, False on timeout. The same
            value is recorded on :attr:`timed_out` (inverted) so callers that
            ignore the return value can still tell a timeout from a genuine
            message-count mismatch.
        """
        test_message = deepcopy(source_message)  # ensure object not mutated by ovos-core
        # Reset the done flag and the eof counter ATOMICALLY: a handler running
        # between the two would otherwise have its increment thrown away (or set
        # done for the previous capture's counter).
        with self._eof_lock:
            self.done.clear()
            self._eof_seen = 0
            self._generation += 1
            generation = self._generation
            self._armed = True
        self.minicroft.bus.emit(test_message)
        completed = self.done.wait(timeout)
        if completed and self._done_generation != generation:
            # `done` was set by something other than this capture's eof run
            # (finish(), or a previous generation). Treat it as a timeout
            # rather than reporting a completion this capture never saw.
            completed = False
        if not completed:
            self.timed_out = True
            self.timeout_seconds = timeout
        return completed

    def finish(self) -> List[Message]:
        with self._eof_lock:
            self._armed = False
        self.done.set()
        self.minicroft.bus.remove("message", self.handle_message)
        for m in self._effective_eof_msgs():
            self.minicroft.bus.remove(m, self.handle_end_of_test)
        # Return a snapshot: the live list is still owned by this session (and
        # __del__ calls finish() again), so handing it out invites surprise
        # mutation from a late handler.
        return list(self.responses)

    def __del__(self):
        # At interpreter shutdown, or when construction failed part-way, the
        # MiniCroft may have no bus (or be gone entirely). finish() would then
        # raise inside __del__, which Python can only print and swallow.
        if getattr(getattr(self, "minicroft", None), "bus", None) is None:
            return
        try:
            self.finish()
        except Exception:
            pass


def _topic_matches(msg_type: str, name: str) -> bool:
    """True if ``msg_type`` is ``name`` under either its legacy or canonical spelling.

    Producers emit canonical ``ovos.*`` spec topics since workshop#425, but
    ovoscope's ``execute()`` captures every message via the bus catch-all
    (faithfully to the real wire), so a pre-spec producer vintage in the
    captured stream can still carry the legacy name instead. Assertions that
    filter the captured stream by ``msg_type`` must therefore accept both
    spellings.

    The legacy<->canonical pairing is not hand-rolled here: it reuses the
    same static maps ``ovos-bus-client``'s ``MessageBusClient`` and
    ``ovos-utils``' ``FakeBus`` use for their dual-emit bridging (see
    ``ovos_spec_tools.messages.NamespaceTranslator`` /
    ``MIGRATION_MAP`` / ``SPEC_TO_LEGACY``), so the pairing can't drift out
    of sync with the real bus behaviour.
    """
    if msg_type == name:
        return True
    canonical = MIGRATION_MAP.get(name)
    if canonical is not None and msg_type == canonical.value:
        return True
    legacy = SPEC_TO_LEGACY.get(name)
    if legacy is not None and msg_type == legacy:
        return True
    return False


def _describe_messages(messages: List[Message]) -> str:
    """Render a captured message list as a compact, numbered one-line-per-message
    summary for use inside assertion text.

    pytest-xdist runs each worker's stdout out-of-band, so anything only
    ``print()``-ed during a captured e2e scenario is lost from the CI job log
    on failure. The assertion text is the only diagnostic guaranteed to
    survive, so it must carry the message list itself.
    """
    lines = []
    for i, m in enumerate(messages):
        ctx = m.context or {}
        trimmed_ctx = {k: ctx[k] for k in ("session_id", "pipeline_id", "skill_id") if k in ctx}
        lines.append(f"\t{i}: {m.msg_type} data={m.data} context={trimmed_ctx}")
    return "\n".join(lines)


@dataclasses.dataclass()
class End2EndTest:
    skill_ids: List[str]  # skill_ids to load during the test (from skill plugins)

    ##############################
    # message content test params
    ##############################
    source_message: Union[Message, List[Message]]  # to be emitted, sequentially if a list
    expected_messages: List[Message]  # tests are performed against message list
    expected_boot_sequence: List[Message] = dataclasses.field(default_factory=list)  # check before any tests are run

    ##############################
    # message type runtime modifiers
    ##############################
    eof_msgs: List[str] = dataclasses.field(default_factory=lambda: DEFAULT_EOF) # if received, end message capture
    eof_count: int = 1 # end capture only after an eof message has been seen this many times (one per concurrent lifecycle terminating on the same topic)
    ignore_messages: List[str] = dataclasses.field(default_factory=lambda: DEFAULT_IGNORED) # pretend any message in this list was not emitted for testing purposes
    # Assert only the messages belonging to a single dispatch lifecycle, identified
    # by message.context["skill_id"]. When set, captured messages whose skill_id does
    # not match are dropped before assertion. This isolates one lifecycle's §8 trio +
    # §9 terminals from a CONCURRENT lifecycle whose messages interleave
    # non-deterministically (e.g. stopping a skill that is mid-dispatch: the stop
    # dispatch and the interrupted skill's own completion race). Run the same
    # scenario once per skill_id to assert each lifecycle deterministically.
    skill_id: Optional[str] = None
    # Like skill_id, but isolates a lifecycle by its producing pipeline_id
    # (OVOS-PIPELINE-1 §3.1, stamped on the dispatch context). Use this when the
    # dispatch and a concurrent lifecycle share a skill_id — e.g. a targeted stop
    # whose Match.skill_id IS the interrupted skill (OVOS-STOP-1 §3.1): both carry
    # that skill_id, but only the stop dispatch carries the stop plugin's
    # pipeline_id. Applied together with skill_id when both are set.
    pipeline_id: Optional[str] = None
    ignore_gui: bool = True # ignore the gui namespace bus messages, usually unwanted unless explicitly testing gui integration
    async_messages: List[str] = dataclasses.field(default_factory=list) # these come from an external thread and might come in any order, validate they are received outside the main test

    ##############################
    # message routing test params
    ##############################
    # for all messages received AFTER a flip_point, expected source and destination flip in the message.context
    flip_points: List[str] = dataclasses.field(default_factory=lambda: DEFAULT_FLIP_POINTS)
    # for all messages in entry_points list, new expected source and destination are extracted from message.context (flipped)
    entry_points: List[str] = dataclasses.field(default_factory=lambda: DEFAULT_ENTRY_POINTS)
    # for all messages in keep_original_src, expected source and destination are always compared against source_message[0]  (ignores rolling check via flip_points)
    keep_original_src: List[str] = dataclasses.field(default_factory=lambda: DEFAULT_KEEP_SRC)

    ###########################
    # active skill test params
    ###########################
    inject_active: List[str] = dataclasses.field(default_factory=list) # these skill_ids will be made active before the test runs (modifies Session from source_message[0])
    disallow_extra_active_skills: bool = False # if enabled any unexpected skill_ids that are active will fail the test
    activation_points: List[str] = dataclasses.field(default_factory=lambda: DEFAULT_ACTIVATION) # skill_id (message.context) must be active AFTER any message in activation_points
    deactivation_points:List[str] = dataclasses.field(default_factory=lambda: DEFAULT_DEACTIVATION) # skill_id (message.context) must NOT be active AFTER any message in deactivation_points
    final_session: Optional[Session] = None  # if provided, extra checks will be made against Session from last received message

    ###########################
    # sub-test configuration
    ###########################
    test_message_number: bool = True
    test_async_messages: bool = True
    test_async_message_number: bool = True
    test_boot_sequence: bool = True
    test_msg_type: bool = True
    test_msg_data: bool = True
    test_msg_context: bool = True
    test_active_skills: bool = True
    test_routing: bool = True
    test_final_session: bool = True

    ###########################
    # bus coverage
    ###########################
    track_bus_coverage: bool = False  # enable BusCoverageTracker for this test
    print_bus_coverage: bool = False  # print inline summary after execute()
    bus_coverage_report: Optional["BusCoverageReport"] = dataclasses.field(default=None, init=False, repr=False)

    ###########################
    # test runner internals
    ###########################
    verbose: bool = True
    minicroft: Optional[MiniCroft] = None
    managed: bool = False

    def __post_init__(self):
        # global coverage opt-in
        if GLOBAL_BUS_COVERAGE:
            self.track_bus_coverage = True

        # standardize to be a list. Use "not a list" rather than an
        # isinstance(Message) check: depending on installed versions the
        # message class may come from ovos_bus_client / ovos_spec_tools /
        # ovos_utils.fakebus, and a cross-class isinstance can be False — which
        # would leave a single (non-iterable) Message and break later iteration.
        if not isinstance(self.source_message, list):
            self.source_message = [self.source_message]

        # expected_messages (and expected_boot_sequence) must be Message
        # objects: execute() reads .msg_type / .data / .context off every
        # entry while walking the captured stream. A bare topic string (e.g.
        # expected_messages=["speak"]) used to fail deep in that loop with an
        # obscure AttributeError ('str' object has no attribute 'msg_type'/
        # 'serialize') instead of naming the actual mistake at construction
        # time. Message objects were the only shape this dataclass ever
        # accepted (unchanged since the first commit) — fail fast and clearly
        # instead.
        for _field_name in ("expected_messages", "expected_boot_sequence"):
            for _i, _m in enumerate(getattr(self, _field_name)):
                if not isinstance(_m, Message):
                    raise TypeError(
                        f"❌ {_field_name}[{_i}] must be a Message instance, "
                        f"got {type(_m).__name__}: {_m!r}. Bare topic strings "
                        f"(e.g. expected_messages=[\"speak\"]) are not "
                        f"supported — build a Message(topic, data, context) "
                        f"for each expected entry."
                    )

        if self.ignore_gui:
            # ensure we don't mutate a shared default list
            self.ignore_messages = list(self.ignore_messages)
            for m in GUI_IGNORED:
                if m not in self.ignore_messages:
                    self.ignore_messages.append(m)

    def execute(self, timeout: int = 30) -> List[Message]:
        if self.minicroft is None:
            self.minicroft = get_minicroft(self.skill_ids)
            self.managed = True
        # Teardown MUST run even when an assertion below fails: MiniCroft
        # patches process-wide globals (SessionManager.bus / default_session,
        # Configuration) that only stop() restores. Skipping it poisons every
        # later test in the process.
        try:
            return self._execute(timeout)
        finally:
            if self.managed and self.minicroft is not None:
                self.minicroft.stop()
                self.minicroft = None

    def _execute(self, timeout: int = 30) -> List[Message]:
        if self.test_boot_sequence and self.expected_boot_sequence:
            for expected, received in zip(self.expected_boot_sequence, self.minicroft.boot_messages):
                assert expected.msg_type == received.msg_type, f"❌ expected boot message_type '{expected.msg_type}' | got '{received.msg_type}'"
                if self.verbose:
                    print(f"✅ boot message type match: '{expected.msg_type}'")
                for k, v in expected.data.items():
                    assert received.data[k] == v, f"❌ boot message data mismatch for key '{k}' - expected '{v}' | got '{received.data[k]}'"
                    if self.verbose:
                        print(f"✅ boot message data match: '{k}' -> '{v}'")
                for k, v in expected.context.items():
                    assert received.context[k] == v, f"❌ boot message context mismatch for key '{k}' - expected '{v}' | got '{received.data[k]}'"
                    if self.verbose:
                        print(f"✅ boot message context match: '{k}' -> '{v}'")

        sess = SessionManager.get(self.source_message[0])
        for s in self.inject_active:
            if self.verbose:
                print(f"💡 activating skill pre-test: {s}")
            sess.activate_skill(s)
        active_skills = [s[0] for s in sess.active_skills]

        # track initial source/destination for use in routing tests
        e_src = o_src = self.source_message[0].context.get("source")
        e_dst = o_dst = self.source_message[0].context.get("destination")
        if self.verbose:
            print(f"💡 original message.context source: '{o_src}'")
            print(f"💡 original message.context destination: '{o_dst}'")

        # bus coverage tracking (optional)
        _bus_tracker = None
        if self.track_bus_coverage:
            from ovoscope.bus_coverage import BusCoverageTracker
            _bus_tracker = BusCoverageTracker(self.minicroft.bus, self.minicroft)
            _bus_tracker.snapshot_listeners()
            _bus_tracker.start_tracking()

        # the capture session will store all messages until capture.finish()
        #  even if multiple messages are emitted
        capture = CaptureSession(self.minicroft, eof_msgs=self.eof_msgs,
                                 eof_count=self.eof_count,
                                 ignore_messages=self.ignore_messages,
                                 async_messages=self.async_messages)
        # start_tracking() wraps bus.emit. Anything that raises between here and
        # stop_tracking() would leave the wrapper installed for the rest of the
        # process, and every later test would stack one more wrapper on top.
        try:
            for idx, source_message in enumerate(self.source_message):
                if "session" not in source_message.context and len(capture.responses):
                    # propagate session updates as a client would do
                    prev_ctx = capture.responses[-1].context or {}
                    if "session" not in prev_ctx:
                        raise AssertionError(
                            f"❌ cannot chain source_message #{idx}: the last "
                            f"captured response "
                            f"('{capture.responses[-1].msg_type}') carries no "
                            f"session in its context, so there is nothing to "
                            f"propagate. Give this source_message an explicit "
                            f"session."
                        )
                    source_message.context["session"] = prev_ctx["session"]
                capture.capture(source_message, timeout)

            # final message list
            messages = capture.finish()

            # isolate a single dispatch lifecycle by skill_id — drop messages
            # from a concurrent (interleaving) lifecycle so the assertion is
            # deterministic.
            if self.skill_id is not None:
                messages = [m for m in messages
                            if (m.context or {}).get("skill_id") == self.skill_id]
                if self.verbose:
                    print(f"💡 filtered to skill_id='{self.skill_id}': {len(messages)} messages")
            if self.pipeline_id is not None:
                messages = [m for m in messages
                            if (m.context or {}).get("pipeline_id") == self.pipeline_id]
                if self.verbose:
                    print(f"💡 filtered to pipeline_id='{self.pipeline_id}': {len(messages)} messages")
        finally:
            if _bus_tracker is not None:
                _bus_tracker.stop_tracking()

        if _bus_tracker is not None:
            all_responses = messages + list(getattr(capture, "async_responses", []))
            _bus_tracker.record_session(all_responses, self.expected_messages)
            self.bus_coverage_report = _bus_tracker.build_report()

        # A capture timeout means the scenario never terminated. Say so plainly
        # — otherwise it surfaces as a baffling message-count mismatch.
        assert not capture.timed_out, (
            f"❌ capture timed out after {capture.timeout_seconds}s waiting for "
            f"eof_msgs {self.eof_msgs} (needed {self.eof_count}, "
            f"got {capture._eof_seen}) — captured {len(messages)} messages: "
            f"{[m.msg_type for m in messages]}"
        )

        if self.test_message_number:
            n1 = len(self.expected_messages)
            n2 = len(messages)
            if n1 != n2:
                first_bad = None
                for i, n in enumerate(messages):
                    if i < len(self.expected_messages):
                        e = self.expected_messages[i]
                        if e.msg_type != n.msg_type and first_bad is None:
                            first_bad = n
                            print("⚠️ first differing message:", f"{n.msg_type} (received)", f"{e.msg_type} (expected)")
                    print("\t", i, n.serialize())
            assert n1 == n2, (
                f"❌ got {n2} messages, expected {n1}\n"
                + _describe_messages(messages)
            )
            if self.verbose:
                print(f"✅ got {n1} messages as expected")

        if self.test_async_message_number:
            n1 = len(self.async_messages)
            n2 = len(capture.async_responses)
            assert n1 == n2, f"❌ got {n2} async messages, expected {n1}"
            if self.verbose:
                print(f"✅ got {n1} async messages as expected")

        if self.test_async_messages:
            async_types = [m.msg_type for m in capture.async_responses]
            for m in self.async_messages:
                assert m in async_types, f"❌ missing async message: {m}"
                if self.verbose:
                    print(f"✅ got async message '{m}' as expected")

        for expected, received in zip(self.expected_messages, messages):
            if self.verbose:
                print(f"💡 Received message: {received.serialize()}")
                print(f"> Expected message: {expected.serialize()}")

            skill_id = received.context.get("skill_id")
            # track expected active skills
            if received.msg_type in self.activation_points and "skill_id" in received.context:
                if self.verbose:
                    print(f"💡 reached activation point: '{expected.msg_type}'")
                    print(f"💡 skill MUST be active from now on: '{skill_id}'")
                active_skills.append(skill_id)
            if received.msg_type in self.deactivation_points and "skill_id" in received.context:
                if self.verbose:
                    print(f"💡 reached deactivation point: '{expected.msg_type}'")
                    print(f"💡 skill must NOT be active from now on: '{skill_id}'")
                if skill_id in active_skills:
                    active_skills.remove(skill_id)

            if expected.msg_type in self.flip_points:
                e_src = expected.context.get("source")
                e_dst = expected.context.get("destination")

            if self.test_msg_type:
                assert expected.msg_type == received.msg_type, f"❌ expected message_type '{expected.msg_type}' | got '{received.msg_type}'"
                if self.verbose:
                    print(f"✅ got expected message_type: '{expected.msg_type}'")
            if self.test_msg_data:
                for k, v in expected.data.items():
                    assert received.data[k] == v, f"❌ message data mismatch for key '{k}' - expected '{v}' | got '{received.data[k]}'"
                    if self.verbose:
                        print(f"✅ got expected message data '{k}: '{v}'")
            if self.test_msg_context:
                for k, v in expected.context.items():
                    assert received.context[k] == v, f"❌ message context mismatch for key '{k}' - expected '{v}' | got '{received.context[k]}'"
                    if self.verbose:
                        print(f"✅ got expected message context '{k}: '{v}'")
            if self.test_routing and self.skill_id is None and self.pipeline_id is None:
                r_src = received.context.get("source")
                r_dst = received.context.get("destination")
                if expected.msg_type in self.keep_original_src:
                    assert o_src == r_src, f"❌ source doesnt match! expected '{o_src}' got '{r_src}'"
                    assert o_dst == r_dst, f"❌ destination doesnt match! expected '{o_dst}' got '{r_dst}'"
                else:
                    assert e_src == r_src, f"❌ source doesnt match! expected '{e_src}' got '{r_src}'"
                    assert e_dst == r_dst, f"❌ destination doesnt match! expected '{e_dst}' got '{r_dst}'"
                if self.verbose:
                    # print(f"💡 source/destination flip point: '{expected.msg_type}'")
                    print(f"✅ message source matches: {r_src}")
                    print(f"✅ message destination matches: {r_dst}")

                if expected.msg_type in self.entry_points:
                    e_src, e_dst = r_dst, r_src
                    if self.verbose:
                        print(f"💡 source/destination entry point: '{expected.msg_type}'")
                        print(f"💡 new expected message.context source: '{e_src}'")
                        print(f"💡 new expected message.context destination: '{e_dst}'")
                elif expected.msg_type in self.flip_points:
                    e_src, e_dst = e_dst, e_src
                    if self.verbose:
                        print(f"💡 source/destination flip point: '{expected.msg_type}'")
                        print(f"💡 new expected message.context source: '{e_src}'")
                        print(f"💡 new expected message.context destination: '{e_dst}'")

            if self.test_active_skills and active_skills:
                sess = SessionManager.get(received)
                skills = [s[0] for s in sess.active_skills]
                for s in active_skills:
                    assert s in skills, f"❌ '{s}' missing from active skills list"
                    if self.verbose:
                        print(f"✅ skill active as expected: '{s}'")
                if self.disallow_extra_active_skills:
                    for s in skills:
                        assert s in active_skills, f"❌ '{s}' extra skill in active skills list"


        if self.test_final_session and self.final_session:
            last_sess = SessionManager.get(messages[-1])
            expected_sess = self.final_session
            if self.verbose:
                print(f"💡 final session: {last_sess.serialize()}")
                print(f"> expected: {expected_sess.serialize()}")
            assert {s[0] for s in last_sess.active_skills} == {s[0] for s in expected_sess.active_skills}, f"❌ final session active_skills doesn't match"
            assert sess.lang == expected_sess.lang, f"❌ final session lang doesn't match"
            assert sess.pipeline == expected_sess.pipeline, f"❌ final session pipeline doesn't match"
            assert sess.system_unit == expected_sess.system_unit, f"❌ final session system_unit doesn't match"
            assert sess.date_format == expected_sess.date_format, f"❌ final session date_format doesn't match"
            assert sess.time_format == expected_sess.time_format, f"❌ final session time_format doesn't match"
            assert sess.site_id == expected_sess.site_id, f"❌ final session site_id doesn't match"
            assert sess.session_id == expected_sess.session_id, f"❌ final session session_id doesn't match"
            assert set(sess.blacklisted_skills or []) == set(expected_sess.blacklisted_skills or []), f"❌ final session blacklisted_skills doesn't match"
            assert set(sess.blacklisted_intents or []) == set(expected_sess.blacklisted_intents or []), f"❌ final session blacklisted_intents doesn't match"
            if self.verbose:
                print(f"✅ final session matches: {expected_sess.serialize()}")

        if self.print_bus_coverage and self.bus_coverage_report is not None:
            print(self.bus_coverage_report.summary_line())

        return messages

    @staticmethod
    def anonymize_message(message: Message) -> Message:
        msg = Message(message.msg_type, message.data, message.context)
        sess = SessionManager.get(message)
        sess.location = {
            "lat": 0.0,
            "lon": 0.0,
            "tz": "Europe/Lisbon"
        }
        msg.context["session"] = sess.serialize()
        return msg

    def serialize(self, anonymize=True) -> SerializedTest:
        src = [self.anonymize_message(m) if anonymize else m
               for m in self.source_message]
        expected = [self.anonymize_message(m) if anonymize else m
                    for m in self.expected_messages]
        data = {
            "skill_ids": self.skill_ids,
            "source_message": [json.loads(m.serialize()) for m in src],
            "expected_messages": [json.loads(m.serialize()) for m in expected],
            "eof_msgs": self.eof_msgs,
            "ignore_messages": self.ignore_messages,
            "ignore_gui": self.ignore_gui,
            "flip_points": self.flip_points,
            "test_msg_type": self.test_msg_type,
            "test_msg_data": self.test_msg_data,
            "test_msg_context": self.test_msg_context,
            "test_routing": self.test_routing
        }
        return data

    @staticmethod
    def deserialize(data: Union[str, SerializedTest]) -> 'End2EndTest':
        if isinstance(data, str):
            data = json.loads(data)
        kwargs = data
        kwargs["source_message"] = [Message.deserialize(m) for m in data["source_message"]]
        kwargs["expected_messages"] = [Message.deserialize(m) for m in data["expected_messages"]]
        return End2EndTest(**kwargs)

    @classmethod
    def from_message(cls, message: Union[Message, List[Message]],
                     skill_ids: List[str],
                     eof_msgs: Optional[List[str]] = None,
                     flip_points: Optional[List[str]] = None,
                     ignore_messages: Optional[List[str]] = None,
                     ignore_gui: bool = True,
                     async_messages: Optional[List[str]] = None,
                     timeout=20, *args, **kwargs) -> 'End2EndTest':
        if not isinstance(message, list):
            message = [message]
        if eof_msgs is None:
            eof_msgs = DEFAULT_EOF
        if flip_points is None:
            flip_points = DEFAULT_FLIP_POINTS
        if ignore_messages is None:
            ignore_messages = list(DEFAULT_IGNORED)
        else:
            ignore_messages = list(ignore_messages)

        if ignore_gui:
            for m in GUI_IGNORED:
                if m not in ignore_messages:
                    ignore_messages.append(m)

        if async_messages is None:
            async_messages = []

        minicroft = get_minicroft(skill_ids, *args, **kwargs)
        capture = CaptureSession(minicroft,
                                 eof_msgs=eof_msgs,
                                 ignore_messages=ignore_messages,
                                 async_messages=async_messages)

        # stop() restores process-wide globals — it must run even if a capture
        # raises.
        try:
            for idx, source_message in enumerate(message):
                if "session" not in source_message.context and len(capture.responses):
                    # propagate session updates as a client would do
                    source_message.context["session"] = capture.responses[-1].context["session"]
                capture.capture(source_message, timeout)
        finally:
            minicroft.stop()
        expected_messages = capture.finish()
        return End2EndTest(
            skill_ids=skill_ids,
            source_message=message,
            expected_messages=expected_messages,
            flip_points=flip_points,
            ignore_messages=ignore_messages,
            ignore_gui=ignore_gui,
            eof_msgs=eof_msgs,
            async_messages=async_messages,
        )

    @staticmethod
    def from_path(path: str) -> 'End2EndTest':
        with open(path) as f:
            return End2EndTest.deserialize(f.read())

    def save(self, path: str, anonymize: bool = True) -> None:
        with open(path, "w") as f:
            json.dump(self.serialize(anonymize=anonymize), f, ensure_ascii=False, indent=2)

    def assert_spoke(self, text: str, lang: str = "en-US", timeout: int = 30) -> None:

        """Run the test and assert that a ``speak`` message with the given utterance was emitted.

        Sugar for simple speak-assertion tests that don't need to check the full message sequence.
        Internally calls ``execute()`` and scans the returned messages for a matching ``speak``.

        Args:
            text: The exact ``utterance`` string expected in a ``speak`` message.
            lang: The ``lang`` field expected in the ``speak`` message data.
            timeout: Forwarded to ``execute()``.

        Raises:
            AssertionError: If no ``speak`` message with the given text (and lang) was emitted.
        """
        messages = self.execute(timeout=timeout)
        speak_utterances = [
            m.data.get("utterance")
            for m in messages
            if _topic_matches(m.msg_type, "speak") and m.data.get("lang") == lang
        ]
        assert text in speak_utterances, (
            f"❌ speak '{text}' (lang={lang}) not found. "
            f"Received speak utterances: {speak_utterances}"
        )


try:
    from ovoscope.audio import (  # noqa: F401
        MockAudioBackend,
        AudioServiceHarness,
        MockTTS,
        PlaybackServiceHarness,
        AudioCaptureSession,
    )
except ImportError as e:
    # Only silence if it's missing the optional dependency itself.
    # If ovoscope.audio has a logic error (e.g. broken import of a present lib), re-raise.
    if isinstance(e, ModuleNotFoundError) and e.name in ("ovos_audio", "ovos_audio.audio"):
        pass
    else:
        raise

try:
    from ovoscope.media import (  # noqa: F401
        MockOCPBackend,
        OCPPlayerHarness,
        OCPCaptureSession,
    )
except ImportError as e:
    # Optional [media] extra (ovos-media). Silence only when the missing module
    # is ovos-media itself; a logic error in a present lib must re-raise.
    if isinstance(e, ModuleNotFoundError) and e.name in ("ovos_media", "ovos_media.player"):
        pass
    else:
        raise

# MediaProvider (catalog/search) harness — duck-typed, stdlib-only at import time,
# so it needs no optional dependency guard (the provider package + mediavocab are
# only needed by the *test* that uses it, not by ovoscope itself).
from ovoscope.media_provider import MediaProviderHarness  # noqa: F401,E402

# MediaBackend v2 plugin harness. ovos_plugin_manager.templates.media is a
# new module that only exists on the unreleased feat/media-backend-v2
# branch — a released ovos-plugin-manager does not have it yet. Unlike the
# [media]-extra imports above, ovoscope.media_backend guards its own
# templates.media import internally and only raises (a clear ImportError)
# when MediaBackendHarness is actually constructed, not at module import
# time — so this import is always safe and needs no try/except here.
# TEMPORARY: once ovos-plugin-manager>=3.0.0a1 releases with the v2
# template, it becomes an explicit dependency and this comment can go.
from ovoscope.media_backend import MediaBackendHarness  # noqa: F401,E402

try:
    from ovoscope.tts_intelligibility import (  # noqa: F401
        TTSIntelligibilityHarness,
        IntelligibilityReport,
        UtteranceScore,
        score_tts_intelligibility,
    )
except ImportError as e:
    # Optional [tts] extra. Silence only when the missing module is one of the
    # optional TTS-scoring deps; a logic error in a present lib must re-raise.
    _TTS_OPTIONAL_MODULES = (
        "jiwer",
        "ovos_audio", "ovos_audio.audio",
        "ovos_utterance_normalizer",
        "ovos_stt_plugin_fasterwhisper",
        "faster_whisper",
    )
    if isinstance(e, ModuleNotFoundError) and e.name in _TTS_OPTIONAL_MODULES:
        pass
    else:
        raise

try:
    from ovoscope.listener import (  # noqa: F401
        MiniListener,
        get_mini_listener,
        ListenerTest,
    )
except ImportError as e:
    if isinstance(e, ModuleNotFoundError) and e.name in ("ovos_dinkum_listener", "ovos_dinkum_listener.transformers"):
        pass
    else:
        raise

from ovoscope.voice_loop import (  # noqa: F401
    ListenerHarness,
    MiniVoiceLoop,
    MiniHotwordContainer,
    MockFileMicrophone,
    MockStreamingSTT,
    get_mini_voice_loop,
    VoiceLoopTest,
)
from ovoscope.simple_listener import (  # noqa: F401
    MiniSimpleListener,
    get_mini_simple_listener,
)
from ovoscope.classic_listener import (  # noqa: F401
    MiniClassicListener,
    bridge_recognizer_loop_to_bus,
    classic_listener_available,
)


@dataclasses.dataclass
class GUICaptureSession:
    """Capture ``gui.*`` bus messages emitted during a skill interaction.

    Unlike :class:`CaptureSession` (which filters out ``gui.*`` messages by
    default), this session records *only* GUI-related messages so tests can
    assert page navigation, namespace values, and namespace teardown without
    cluttering the main message capture.

    Args:
        bus: The :class:`FakeBus` to subscribe to.
        prefixes: List of message-type prefixes to capture.
            Defaults to ``["gui.", "mycroft.gui."]``.

    Example::

        from ovoscope import get_minicroft, GUICaptureSession
        from ovos_utils.messagebus import Message

        mc = get_minicroft(["ovos-skill-hello-world.openvoiceos"])
        with GUICaptureSession(mc.bus) as gui:
            mc.bus.emit(Message("recognizer_loop:utterance",
                                data={"utterances": ["hello"], "lang": "en-US"}))
            import time; time.sleep(2)
            gui.assert_page_shown("helloworldskill", "hello.qml")
        mc.stop()
    """

    bus: Any
    prefixes: List[str] = dataclasses.field(
        default_factory=lambda: ["gui.", "mycroft.gui."]
    )
    messages: List[Message] = dataclasses.field(default_factory=list)

    def _on_message(self, raw: Any) -> None:
        """Capture GUI-prefixed messages from the bus.

        Args:
            raw: Raw message string or :class:`Message` object.
        """
        if isinstance(raw, str):
            try:
                msg = Message.deserialize(raw)
            except Exception:
                return
        else:
            msg = raw
        if any(msg.msg_type.startswith(p) for p in self.prefixes):
            self.messages.append(msg)

    def start(self) -> None:
        """Subscribe to the bus and begin capturing."""
        self.bus.on("message", self._on_message)

    def stop(self) -> None:
        """Unsubscribe from the bus and stop capturing."""
        self.bus.remove("message", self._on_message)

    def __enter__(self) -> "GUICaptureSession":
        """Start capturing on context-manager entry."""
        self.start()
        return self

    def __exit__(self, *_: Any) -> None:
        """Stop capturing on context-manager exit."""
        self.stop()

    @staticmethod
    def _ns_matches(expected: str, actual: str, exact: bool) -> bool:
        """Compare a GUI namespace, exactly by default.

        Substring matching cannot fail on a near-match: asserting namespace
        ``"skill-weather"`` would pass on ``"skill-weather-extended"``, and
        asserting ``"weather"`` would pass on any namespace containing it. Use
        ``exact=False`` only when you deliberately want prefix behaviour.
        """
        if exact:
            return expected == actual
        return actual.startswith(expected)

    def assert_page_shown(self, namespace: str, page: str, timeout: float = 2.0,
                          exact: bool = True) -> None:
        """Assert that a GUI page was shown in the given namespace.

        Polls the captured messages for up to *timeout* seconds.

        Args:
            namespace: GUI namespace (typically the skill ID slug).
            page: QML page filename (e.g. ``"hello.qml"``). Compared against
                the basename of each shown page, so a directory prefix in the
                message does not affect the result.
            timeout: Maximum seconds to wait.
            exact: Compare namespace and page basename by equality (default).
                Set ``False`` for prefix matching on the namespace and
                substring matching on the page.

        Raises:
            AssertionError: If no matching ``gui.page.show`` message is found.
        """
        import os
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for msg in self.messages:
                if "page.show" in msg.msg_type:
                    data_ns = (msg.data.get("namespace", "")
                              or msg.data.get("__from", "")
                              or msg.context.get("skill_id", ""))
                    pages = (msg.data.get("pages", [])
                             or msg.data.get("page_names", [])
                             or [msg.data.get("page", "")])
                    if not self._ns_matches(namespace, data_ns, exact):
                        continue
                    if exact:
                        hit = any(os.path.basename(str(p)) == page for p in pages)
                    else:
                        hit = any(page in str(p) for p in pages)
                    if hit:
                        return
            time.sleep(0.05)
        captured = [(m.msg_type, m.data) for m in self.messages]
        raise AssertionError(
            f"Expected page {page!r} in namespace {namespace!r} to be shown, "
            f"but no matching gui.page.show message was captured.\nGot: {captured}"
        )

    def assert_template_shown(self, namespace: str, template: str,
                              values: Optional[Dict[str, Any]] = None,
                              timeout: float = 2.0,
                              exact: bool = True) -> None:
        """Assert that a built-in ``SYSTEM_*`` template was shown.

        Ergonomic helper for the template-based GUI: a skill calling a typed
        method such as ``self.gui.show_weather(...)`` emits a
        ``gui.page.show`` for the ``SYSTEM_weather`` template plus
        ``gui.value.set`` for its data keys. This asserts both in one call.

        Args:
            namespace: GUI namespace (typically the skill ID).
            exact: Compare namespace and page name by equality (default).
            template: Template name, with or without the ``SYSTEM_`` prefix
                (``"weather"`` and ``"SYSTEM_weather"`` are equivalent).
            values: Optional mapping of session-data keys to expected values;
                each is checked via :meth:`assert_namespace_value`.
            timeout: Maximum seconds to wait for the page-show message.

        Raises:
            AssertionError: If the template was not shown, or a listed value
                was not set.
        """
        name = template if template.startswith("SYSTEM_") else f"SYSTEM_{template}"
        self.assert_page_shown(namespace, name, timeout=timeout, exact=exact)
        for key, value in (values or {}).items():
            self.assert_namespace_value(namespace, key, value, exact=exact)

    def assert_namespace_value(self, namespace: str, key: str, value: Any,
                               exact: bool = True) -> None:
        """Assert that a namespace key was set to a specific value.

        Args:
            namespace: GUI namespace to check.
            key: Data key within the namespace.
            value: Expected value.
            exact: Compare the namespace by equality (default).

        Raises:
            AssertionError: If no matching ``gui.value.set`` message is found.
        """
        for msg in self.messages:
            if "value.set" in msg.msg_type or "namespace.update" in msg.msg_type:
                data_ns = (msg.data.get("namespace", "")
                              or msg.data.get("__from", "")
                              or msg.context.get("skill_id", ""))
                if self._ns_matches(namespace, data_ns, exact):
                    data = msg.data.get("data", msg.data)
                    if data.get(key) == value:
                        return
        raise AssertionError(
            f"Expected namespace {namespace!r} key {key!r}={value!r} not found.\n"
            f"Captured GUI messages: {[m.msg_type for m in self.messages]}"
        )

    def assert_namespace_has_key(self, namespace: str, key: str,
                                 exact: bool = True) -> None:
        """Assert that a key was set in a namespace, regardless of value.

        Useful for dynamic data (e.g. weather API responses, timestamps)
        where the exact value is unpredictable but the key must exist.

        Args:
            namespace: GUI namespace to check.
            key: Data key that should exist within the namespace.
            exact: Compare the namespace by equality (default).

        Raises:
            AssertionError: If no matching message with the key is found.
        """
        for msg in self.messages:
            if "value.set" in msg.msg_type or "namespace.update" in msg.msg_type:
                data_ns = (msg.data.get("namespace", "")
                              or msg.data.get("__from", "")
                              or msg.context.get("skill_id", ""))
                if self._ns_matches(namespace, data_ns, exact):
                    data = msg.data.get("data", msg.data)
                    if key in data:
                        return
        raise AssertionError(
            f"Expected namespace {namespace!r} to contain key {key!r}, "
            f"but it was never set.\n"
            f"Captured GUI messages: {[m.msg_type for m in self.messages]}"
        )

    def assert_namespace_cleared(self, namespace: str,
                                 exact: bool = True) -> None:
        """Assert that a namespace was cleared/removed.

        Args:
            namespace: GUI namespace that should have been cleared.
            exact: Compare the namespace by equality (default).

        Raises:
            AssertionError: If no matching namespace-clear message is found.
        """
        # `gui.clear.namespace` is the topic the GUI service actually emits.
        # Matching only "namespace.clear" / "namespace.remove" made this
        # assertion impossible to satisfy on the real wire format.
        clear_types = ("namespace.remove", "namespace.clear", "clear.namespace")
        for msg in self.messages:
            if any(t in msg.msg_type for t in clear_types):
                data_ns = (msg.data.get("namespace", "")
                              or msg.data.get("__from", "")
                              or msg.context.get("skill_id", ""))
                if self._ns_matches(namespace, data_ns, exact):
                    return
        raise AssertionError(
            f"Expected namespace {namespace!r} to be cleared, "
            f"but no matching message was captured."
        )


# ---------------------------------------------------------------------------
# Public re-exports — see ovoscope/e2e.py for full docs
# ---------------------------------------------------------------------------
from ovoscope.intent_cases import (  # noqa: E402,F401
    DEFAULT_IGNORE_MESSAGES,
    DEFAULT_PIPELINE_FAMILIES,
    IntentCase,
    assert_intent_case,
    load_intent_cases,
    register_intent_case_tests,
)
from ovoscope.e2e import (  # noqa: E402,F401
    E2EPipelineHarness,
    detach_intent,
    detach_skill,
    make_session,
    make_utterance_message,
    register_adapt_intent,
    register_adapt_vocab,
    register_padatious_entity,
    register_padatious_intent,
    wait_for_failure,
    wait_for_match,
)
