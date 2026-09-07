"""Regression tests for the round-2 audit findings.

Each test here fails on the pre-fix code. Grouped by audit section:

A. memory retention (SkillApi.bus pinned every stopped MiniCroft)
B. the dead ``--ovoscope-accuracy-min`` CI gate
C. silent state corruption / false-green assertions
D. race windows found by adversarial validation of the round-1 fixes
E. resilience sweep items
F. test-suite quality (direct coverage of previously untested helpers)

Tests that do not need a running assistant use a
``SimpleNamespace(bus=FakeBus())`` stub: booting a MiniCroft costs seconds and
hundreds of MB of retained memory per boot.
"""
import gc
import json
import os
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from ovos_bus_client.message import Message
from ovos_utils.fakebus import FakeBus
from ovos_utils.log import LOG

from ovoscope import CaptureSession


# ---------------------------------------------------------------------------
# A. Memory retention — SkillApi.bus
# ---------------------------------------------------------------------------

class TestSkillApiBusRestore(unittest.TestCase):
    """``SkillApi.bus`` is a process-wide class attribute set during boot.

    Left pointing at a stopped MiniCroft's FakeBus, it pins the whole object
    graph (~633MB per stopped instance measured on this suite).
    """

    def test_stop_restores_skill_api_bus(self):
        from ovos_workshop.skills.api import SkillApi
        from ovoscope import get_minicroft

        original = SkillApi.bus
        LOG.set_level("ERROR")
        try:
            croft = get_minicroft([])
            try:
                self.assertIsNot(
                    SkillApi.bus, original,
                    "boot did not connect SkillApi to the harness bus — this "
                    "test no longer proves anything")
            finally:
                croft.stop()
            self.assertIs(SkillApi.bus, original,
                          "SkillApi.bus was not restored by stop()")
        finally:
            LOG.set_level("CRITICAL")
            SkillApi.bus = original

    @pytest.mark.timeout(900)
    def test_stopped_minicrofts_are_collectable(self):
        """Boot+stop two MiniCrofts; the second must be collectable.

        The FIRST boot leaves a known residue (module-level singletons built
        lazily on first use), so one live instance is allowed. A second live
        instance means every stopped harness is still pinned.
        """
        from ovoscope import MiniCroft, get_minicroft

        LOG.set_level("ERROR")
        try:
            for _ in range(2):
                croft = get_minicroft([])
                croft.stop()
                del croft
                gc.collect()
            gc.collect()
            alive = [o for o in gc.get_objects()
                     if type(o) is MiniCroft]
            self.assertLessEqual(
                len(alive), 1,
                f"{len(alive)} stopped MiniCroft instances are still alive — "
                f"a process-wide reference is pinning them")
        finally:
            LOG.set_level("CRITICAL")


# ---------------------------------------------------------------------------
# B. The accuracy gate must change the exit status
# ---------------------------------------------------------------------------

class TestAccuracyGateExitStatus(unittest.TestCase):
    """The gate was computed in pytest_terminal_summary, which runs AFTER
    pytest_sessionfinish — so the flag was always read too late and the exit
    status stayed 0 no matter how bad the accuracy was."""

    _CONFTEST = """
def pytest_sessionstart(session):
    from ovoscope import pytest_plugin
    pytest_plugin.pytest_runtest_logreport._accum = {
        "meta": {},
        "results": [{"nodeid": "x", "skill_id": "s", "pipeline": "p",
                     "lang": "en-US", "intent": "i", "utterance": "u",
                     "source": "src", "passed": PASSED}],
    }
"""

    def _run(self, passed, extra):
        """Run pytest in a subprocess with a pre-seeded accuracy result."""
        import subprocess

        tmpdir = tempfile.mkdtemp(prefix="ovoscope-gate-")
        with open(os.path.join(tmpdir, "conftest.py"), "w",
                  encoding="utf-8") as fh:
            fh.write(self._CONFTEST.replace("PASSED", str(passed)))
        with open(os.path.join(tmpdir, "test_gate_case.py"), "w",
                  encoding="utf-8") as fh:
            fh.write("def test_ok():\n    assert True\n")
        # Put the code under test ahead of any installed copy of ovoscope.
        repo_root = os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [repo_root] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", tmpdir, "-q",
             "-p", "no:cacheprovider"] + extra,
            capture_output=True, text=True, cwd=tmpdir, timeout=300, env=env)
        return proc

    def test_failing_gate_yields_nonzero_exit_status(self):
        proc = self._run(False, ["--ovoscope-accuracy-min", "0.99"])
        self.assertNotEqual(
            proc.returncode, 0,
            f"a failed accuracy gate did not change the exit status\n"
            f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("accuracy gate FAILED", proc.stdout)

    def test_passing_gate_keeps_exit_status_zero(self):
        proc = self._run(True, ["--ovoscope-accuracy-min", "0.99"])
        self.assertEqual(proc.returncode, 0,
                         f"{proc.stdout}\n{proc.stderr}")

    def test_no_gate_option_keeps_exit_status_zero(self):
        proc = self._run(False, [])
        self.assertEqual(proc.returncode, 0,
                         f"{proc.stdout}\n{proc.stderr}")


# ---------------------------------------------------------------------------
# C. False-green / silent corruption
# ---------------------------------------------------------------------------

class TestDiffFixtureValidation(unittest.TestCase):
    """A file with no ``expected_messages`` compared []-vs-[] as identical."""

    def _write(self, payload):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return path

    def test_non_fixture_file_raises(self):
        from ovoscope.diff import diff_fixtures

        a = self._write({"not": "a fixture"})
        b = self._write({"also": "not one"})
        with self.assertRaises(ValueError):
            diff_fixtures(expected_path=a, actual_path=b)

    def test_real_fixture_still_diffs(self):
        from ovoscope.diff import diff_fixtures

        a = self._write({"expected_messages": [
            {"type": "a", "data": {}, "context": {}}]})
        b = self._write({"expected_messages": [
            {"type": "b", "data": {}, "context": {}}]})
        result = diff_fixtures(expected_path=a, actual_path=b)
        self.assertFalse(result.is_identical)

    def test_expected_none_differs_from_absent_key(self):
        from ovoscope.diff import _dict_diff

        diffs = _dict_diff({"a": None}, {})
        self.assertIn("a", diffs,
                      "an expected None matched an ABSENT key")

    def test_present_none_matches_expected_none(self):
        from ovoscope.diff import _dict_diff

        self.assertEqual(_dict_diff({"a": None}, {"a": None}), {})

    def test_nested_dict_value_mismatch(self):
        from ovoscope.diff import _dict_diff

        diffs = _dict_diff({"a": {"b": 1}}, {"a": {"b": 2}})
        self.assertIn("a", diffs)

    def test_list_of_dicts_mismatch(self):
        from ovoscope.diff import _dict_diff

        diffs = _dict_diff({"a": [{"b": 1}]}, {"a": [{"b": 2}]})
        self.assertIn("a", diffs)

    def test_list_of_dicts_match(self):
        from ovoscope.diff import _dict_diff

        self.assertEqual(_dict_diff({"a": [{"b": 1}]}, {"a": [{"b": 1}]}), {})


class TestBusCoverageTrackerUnwrap(unittest.TestCase):
    """A raise between start_tracking() and stop_tracking() left bus.emit
    wrapped forever, stacking one wrapper per test."""

    def test_stop_tracking_restores_emit(self):
        from ovoscope.bus_coverage import BusCoverageTracker

        bus = FakeBus()
        mc = SimpleNamespace(bus=bus)
        tracker = BusCoverageTracker(bus, mc)
        tracker.snapshot_listeners()
        tracker.start_tracking()
        bus.emit(Message("ovoscope.audit.r2.counted"))
        self.assertEqual(tracker._invocations.get("ovoscope.audit.r2.counted"),
                         1, "the wrapper did not count")
        tracker.stop_tracking()
        bus.emit(Message("ovoscope.audit.r2.counted"))
        self.assertEqual(
            tracker._invocations.get("ovoscope.audit.r2.counted"), 1,
            "bus.emit was still wrapped after stop_tracking()")
        bus.close()

    def test_stop_tracking_leaves_a_foreign_wrapper_alone(self):
        from ovoscope.bus_coverage import BusCoverageTracker

        bus = FakeBus()
        mc = SimpleNamespace(bus=bus)
        tracker = BusCoverageTracker(bus, mc)
        tracker.snapshot_listeners()
        tracker.start_tracking()

        inner = bus.emit

        def _foreign(message):
            inner(message)

        bus.emit = _foreign
        tracker.stop_tracking()
        self.assertIs(bus.emit, _foreign,
                      "stop_tracking clobbered another tracker's wrapper")
        bus.close()

    def test_tracker_is_unwrapped_when_execute_raises(self):
        """End2EndTest._execute must unwrap on the failure path too."""
        import ovoscope.bus_coverage as bc
        from ovoscope import End2EndTest

        bus = FakeBus()
        mc = SimpleNamespace(bus=bus, boot_messages=[])
        built = []
        real_cls = bc.BusCoverageTracker

        class _Recording(real_cls):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                built.append(self)

        test = End2EndTest(
            skill_ids=[],
            source_message=Message("ovoscope.audit.r2.ping"),
            expected_messages=[Message("ovoscope.audit.r2.never")],
            eof_msgs=["ovoscope.audit.r2.no.such.eof"],
            track_bus_coverage=True,
            verbose=False,
            minicroft=mc,
        )
        with patch.object(bc, "BusCoverageTracker", _Recording):
            with self.assertRaises(AssertionError):
                test._execute(timeout=1)
        self.assertEqual(len(built), 1)
        tracker = built[0]
        before = dict(tracker._invocations)
        bus.emit(Message("ovoscope.audit.r2.after.failure"))
        self.assertEqual(
            dict(tracker._invocations), before,
            "bus.emit was left wrapped after a failing test")
        bus.close()


# ---------------------------------------------------------------------------
# D. Race windows
# ---------------------------------------------------------------------------

class TestCaptureSessionArming(unittest.TestCase):
    """An eof arriving OUTSIDE a capture window must not count."""

    def setUp(self):
        LOG.set_level("ERROR")
        self.mc = SimpleNamespace(bus=FakeBus())

    def tearDown(self):
        self.mc.bus.close()
        LOG.set_level("CRITICAL")

    def test_eof_before_capture_does_not_complete_it(self):
        cap = CaptureSession(self.mc, eof_msgs=["ovoscope.audit.r2.eof"])
        # eof fires before capture() is ever called
        self.mc.bus.emit(Message("ovoscope.audit.r2.eof"))
        completed = cap.capture(Message("ovoscope.audit.r2.ping"), timeout=1)
        cap.finish()
        self.assertFalse(
            completed,
            "an eof emitted before capture() completed the next capture")
        self.assertTrue(cap.timed_out)

    def test_eof_after_finish_does_not_leak_into_next_capture(self):
        cap = CaptureSession(self.mc, eof_msgs=["ovoscope.audit.r2.eof"])
        cap.capture(Message("ovoscope.audit.r2.eof"), timeout=5)
        cap.finish()
        self.mc.bus.emit(Message("ovoscope.audit.r2.eof"))

        cap2 = CaptureSession(self.mc, eof_msgs=["ovoscope.audit.r2.eof"])
        self.mc.bus.emit(Message("ovoscope.audit.r2.eof"))
        completed = cap2.capture(Message("ovoscope.audit.r2.ping"), timeout=1)
        cap2.finish()
        self.assertFalse(completed)

    def test_normal_capture_still_completes(self):
        cap = CaptureSession(self.mc, eof_msgs=["ovoscope.audit.r2.eof"])
        completed = cap.capture(Message("ovoscope.audit.r2.eof"), timeout=5)
        cap.finish()
        self.assertTrue(completed)

    def test_del_without_a_bus_is_silent(self):
        cap = CaptureSession(SimpleNamespace(bus=FakeBus()),
                             eof_msgs=["ovoscope.audit.r2.eof"])
        cap.minicroft = SimpleNamespace(bus=None)
        cap.__del__()  # must not raise


class TestMockTTSStopRace(unittest.TestCase):
    """The mock-TTS unduck emit and stop() must be mutually exclusive."""

    @pytest.mark.timeout(900)
    def test_unduck_never_emits_after_stop(self):
        from ovoscope import get_minicroft

        LOG.set_level("ERROR")
        try:
            croft = get_minicroft([])
            seen = []
            after_stop = threading.Event()

            def _watch(msg):
                if croft._stopped:
                    seen.append(msg)
                    after_stop.set()

            croft.bus.on("recognizer_loop:audio_output_end", _watch)
            for i in range(20):
                croft.bus.emit(Message("speak", {"utterance": f"u{i}"}))
            croft.stop()
            self.assertFalse(
                after_stop.wait(0.5),
                f"the mock TTS emitted after stop(): {seen}")
        finally:
            LOG.set_level("CRITICAL")


class TestDefaultSessionRestoreFallbacks(unittest.TestCase):
    """The snapshot/restore must use ONE bus-client API family, and must not
    degrade to a total no-op when the snapshot failed."""

    def test_restore_uses_the_api_family_that_snapshotted(self):
        """The snapshot API and the load API must be the same family."""
        from ovos_bus_client.session import Session, SessionManager
        from ovoscope import MiniCroft

        croft = MiniCroft.__new__(MiniCroft)
        sess = SessionManager.get_default_session()
        if hasattr(sess, "to_dict"):
            croft._session_api = "dict"
            croft._default_session_state = sess.to_dict()
            used, forbidden = "from_dict", "deserialize"
        else:
            croft._session_api = "legacy"
            croft._default_session_state = sess.serialize()
            used, forbidden = "deserialize", "from_dict"
        croft._default_active_skills = list(sess.active_skills)

        calls = []
        real = getattr(Session, used)

        def _spy(data):
            calls.append(used)
            return real(data)

        def _forbidden(*_a, **_kw):
            self.fail(f"a {croft._session_api} snapshot was loaded with "
                      f"{forbidden}()")

        patches = [patch.object(Session, used, staticmethod(_spy))]
        if hasattr(Session, forbidden):
            patches.append(
                patch.object(Session, forbidden, staticmethod(_forbidden)))
        for p in patches:
            p.start()
        try:
            croft._restore_default_session()
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertEqual(calls, [used])

    def test_active_skills_restored_without_a_snapshot(self):
        from ovos_bus_client.session import SessionManager
        from ovoscope import MiniCroft

        croft = MiniCroft.__new__(MiniCroft)
        croft._default_session_state = None
        croft._default_active_skills = []
        sess = SessionManager.get_default_session()
        original = list(sess.active_skills)
        try:
            sess.activate_skill("ovoscope.audit.r2.leaked")
            self.assertTrue(sess.active_skills)
            croft._restore_default_session()
            self.assertEqual(
                sess.active_skills, [],
                "a skill activated during the run survived teardown")
        finally:
            sess.active_skills = original


# ---------------------------------------------------------------------------
# E. Resilience sweep
# ---------------------------------------------------------------------------

class TestHotwordCompatIsScoped(unittest.TestCase):
    def test_patch_is_reverted(self):
        from ovos_plugin_manager.templates import hotwords as hw
        from ovoscope.wakeword_probe import hotword_compat

        original = hw.HotWordEngine.__init__
        with hotword_compat():
            pass
        self.assertIs(hw.HotWordEngine.__init__, original,
                      "hotword compat patch outlived its block")

    def test_patch_is_reverted_on_error(self):
        from ovos_plugin_manager.templates import hotwords as hw
        from ovoscope.wakeword_probe import hotword_compat

        original = hw.HotWordEngine.__init__
        with self.assertRaises(RuntimeError):
            with hotword_compat():
                raise RuntimeError("boom")
        self.assertIs(hw.HotWordEngine.__init__, original)


class TestWakeWordDestructiveReads(unittest.TestCase):
    """``any(e.found_wake_word() ...)`` short-circuited, leaving later engines
    latched — the NEXT call then reported a stale detection."""

    class _Latch:
        def __init__(self, value):
            self.value = value
            self.reads = 0

        def update(self, chunk):
            pass

        def found_wake_word(self, *args):
            self.reads += 1
            got, self.value = self.value, False
            return got

    def test_every_engine_latch_is_read(self):
        from ovoscope.listener import MiniListener

        listener = MiniListener.__new__(MiniListener)
        a = self._Latch(True)
        b = self._Latch(True)
        listener._ww = {"a": a, "b": b}
        self.assertTrue(listener.detect_wakeword(b"\x00" * 32))
        self.assertEqual(b.reads, 1,
                         "the second engine's latch was never read")
        # both latches were consumed, so a second call reports nothing
        self.assertFalse(listener.detect_wakeword(b"\x00" * 32))

    def test_scan_reads_every_engine_latch(self):
        from ovoscope.listener import MiniListener

        listener = MiniListener.__new__(MiniListener)
        a = self._Latch(True)
        b = self._Latch(True)
        listener._ww = {"a": a, "b": b}
        found, idx = listener.scan_for_wakeword([b"\x00" * 32])
        self.assertTrue(found)
        self.assertEqual(idx, 0)
        self.assertEqual(b.reads, 1,
                         "the second engine's latch was never read")


class TestMediaProviderTimeout(unittest.TestCase):
    def test_hanging_provider_raises_timeout(self):
        from ovoscope.media_provider import MediaProviderHarness

        block = threading.Event()

        class _Hanging:
            def is_available(self_inner):
                block.wait(30)
                return True

        harness = MediaProviderHarness(_Hanging(), call_timeout=0.2)
        try:
            with self.assertRaises(TimeoutError):
                harness.is_available()
        finally:
            block.set()

    def test_fast_provider_returns_normally(self):
        from ovoscope.media_provider import MediaProviderHarness

        class _Fast:
            def is_available(self_inner):
                return True

        self.assertTrue(MediaProviderHarness(_Fast()).is_available())

    def test_timeout_can_be_disabled(self):
        from ovoscope.media_provider import MediaProviderHarness

        class _Fast:
            def is_available(self_inner):
                return True

        harness = MediaProviderHarness(_Fast(), call_timeout=None)
        self.assertTrue(harness.is_available())


class TestTtsIntelligibilityMarkers(unittest.TestCase):
    def setUp(self):
        pytest.importorskip("jiwer")

    def test_utterance_slug_is_stable_and_distinct(self):
        from ovoscope.tts_intelligibility import _utt_slug

        self.assertEqual(_utt_slug("hello"), _utt_slug("hello"))
        self.assertNotEqual(_utt_slug("hello"), _utt_slug("hello!"))
        self.assertEqual(len(_utt_slug("hello")), 16)

    def test_score_reports_a_reference_stt_failure(self):
        from ovoscope.tts_intelligibility import UtteranceScore

        score = UtteranceScore(utterance="hi", transcript=None, wer=1.0,
                               cer=1.0, transcribe_failed=True,
                               transcribe_error="RuntimeError: no model")
        payload = score.to_dict()
        self.assertTrue(payload["transcribe_failed"])
        self.assertIsNone(payload["transcript"])
        self.assertIn("no model", payload["transcribe_error"])


class TestSetupSkillFetchFailure(unittest.TestCase):
    def test_failed_skill_md_download_reports_an_error(self):
        from ovoscope import setup_skill

        with tempfile.TemporaryDirectory() as tmp:
            from pathlib import Path
            with patch.object(setup_skill, "_fetch", return_value=False):
                ok = setup_skill._install_skill(
                    Path(tmp) / "ovoscope", "claude",
                    fetch_docs=False, verbose=False)
        self.assertFalse(ok, "a failed SKILL.md download reported success")

    def test_main_exits_nonzero_when_install_fails(self):
        from ovoscope import setup_skill

        argv = ["ovoscope-setup-skill", "--claude", "--no-docs"]
        with patch.object(sys, "argv", argv), \
                patch.object(setup_skill, "install_claude", return_value=False):
            self.assertEqual(setup_skill.main(), 1)


class TestListenerImportGuardIsNarrow(unittest.TestCase):
    """A constructor failure must propagate, not masquerade as a missing
    ovos-dinkum-listener install."""

    def test_constructor_failure_propagates(self):
        import ovoscope.listener as listener_mod

        try:
            from ovos_dinkum_listener import transformers as tmod
        except ImportError:
            self.skipTest("ovos-dinkum-listener is not installed")

        with patch.object(tmod, "AudioTransformersService",
                          side_effect=RuntimeError("constructor boom")):
            with self.assertRaises(RuntimeError):
                listener_mod.MiniListener(
                    {"listener": {"audio_transformers": {}}})


class TestVoiceLoopShutdownDetaches(unittest.TestCase):
    def test_shutdown_removes_the_capture_handler(self):
        from ovoscope.voice_loop import MiniVoiceLoop

        harness = MiniVoiceLoop.__new__(MiniVoiceLoop)
        harness.hotwords = None
        harness.bus = FakeBus()
        calls = []
        harness._capture = lambda msg: calls.append(msg)
        harness.bus.on("message", harness._capture)
        harness.shutdown()
        harness.bus.emit(Message("ovoscope.audit.r2.after.shutdown"))
        self.assertEqual(calls, [],
                         "shutdown() left the capture handler attached")
        harness.bus.close()


# ---------------------------------------------------------------------------
# F. Direct coverage of previously untested helpers
# ---------------------------------------------------------------------------

class TestE2EPipelineHarnessConfigRestore(unittest.TestCase):
    """setUpClass/tearDownClass patch a process-wide config key. Two
    subclasses running back-to-back must not leak it."""

    def _make_subclass(self, config_key, plugin_config):
        from ovoscope.e2e import E2EPipelineHarness

        class _Sub(E2EPipelineHarness):
            PIPELINE_ID = "ovoscope-audit-r2-pipeline"
            CONFIG_KEY = config_key
            PLUGIN_CONFIG = plugin_config

        return _Sub

    def _fake_minicroft(self):
        mc = MagicMock()
        mc.intents.pipeline_plugins = {
            "ovoscope-audit-r2-pipeline": MagicMock()}
        return mc

    def test_absent_key_is_removed_again(self):
        from ovos_config.config import Configuration
        import ovoscope

        cfg = Configuration()
        intents = cfg.setdefault("intents", {})
        key = "ovoscope_audit_r2_absent"
        intents.pop(key, None)

        sub = self._make_subclass(key, {"a": 1})
        with patch.object(ovoscope, "get_minicroft",
                          return_value=self._fake_minicroft()):
            sub.setUpClass()
            self.assertEqual(intents[key], {"a": 1})
            sub.tearDownClass()
        self.assertNotIn(key, intents,
                         "a config key the harness ADDED survived teardown")

    def test_existing_key_is_restored(self):
        from ovos_config.config import Configuration
        import ovoscope

        cfg = Configuration()
        intents = cfg.setdefault("intents", {})
        key = "ovoscope_audit_r2_existing"
        intents[key] = {"original": True}
        try:
            sub = self._make_subclass(key, {"patched": True})
            with patch.object(ovoscope, "get_minicroft",
                              return_value=self._fake_minicroft()):
                sub.setUpClass()
                self.assertEqual(intents[key], {"patched": True})
                sub.tearDownClass()
            self.assertEqual(intents[key], {"original": True})
        finally:
            intents.pop(key, None)

    def test_two_subclasses_back_to_back_do_not_leak(self):
        from ovos_config.config import Configuration
        import ovoscope

        cfg = Configuration()
        intents = cfg.setdefault("intents", {})
        for key in ("ovoscope_audit_r2_first", "ovoscope_audit_r2_second"):
            intents.pop(key, None)

        with patch.object(ovoscope, "get_minicroft",
                          return_value=self._fake_minicroft()):
            for key in ("ovoscope_audit_r2_first",
                        "ovoscope_audit_r2_second"):
                sub = self._make_subclass(key, {"k": key})
                sub.setUpClass()
                sub.tearDownClass()

        for key in ("ovoscope_audit_r2_first", "ovoscope_audit_r2_second"):
            self.assertNotIn(key, intents)


class TestE2EHelpersOnAFakeBus(unittest.TestCase):
    """The registration shims are pure bus emits — assert the wire payload."""

    def setUp(self):
        self.bus = FakeBus()
        self.seen = []
        self.bus.on("message", lambda raw: self.seen.append(
            Message.deserialize(raw)))

    def tearDown(self):
        self.bus.close()

    def _types(self):
        return [m.msg_type for m in self.seen]

    def test_wait_for_failure_returns_false_on_timeout(self):
        from ovoscope.e2e import wait_for_failure

        self.assertFalse(wait_for_failure(self.bus, timeout=0.2))

    def test_wait_for_failure_returns_true_when_it_fires(self):
        from ovoscope.e2e import wait_for_failure

        threading.Timer(
            0.05,
            lambda: self.bus.emit(Message("complete_intent_failure"))).start()
        self.assertTrue(wait_for_failure(self.bus, timeout=3))

    def test_wait_for_failure_unsubscribes(self):
        from ovoscope.e2e import wait_for_failure

        wait_for_failure(self.bus, timeout=0.05)
        before = len(self.seen)
        self.bus.emit(Message("complete_intent_failure"))
        # the helper's own handler is gone; only the wildcard capture fires
        self.assertEqual(len(self.seen), before + 1)

    def test_register_adapt_vocab_emits_one_message_per_word(self):
        from ovoscope.e2e import register_adapt_vocab

        register_adapt_vocab(
            self.bus, "Fruit", ["apple", "pear"], skill_id="sk", settle=0
        )
        vocab = [m for m in self.seen if m.msg_type == "register_vocab"]
        self.assertEqual(len(vocab), 2)
        self.assertEqual(
            [m.data["entity_value"] for m in vocab], ["apple", "pear"])
        self.assertTrue(all(m.data["entity_type"] == "Fruit" for m in vocab))

    def test_register_adapt_vocab_requires_skill_id(self):
        # Unscoped Adapt vocab names ("Fruit") carry no skill_id to derive —
        # omitting the keyword must fail loud, not register ownerless.
        from ovoscope.e2e import register_adapt_vocab

        with self.assertRaises(TypeError):
            register_adapt_vocab(self.bus, "Fruit", ["apple"], settle=0)

    def test_register_adapt_intent_round_trip(self):
        pytest.importorskip("adapt")
        from adapt.intent import IntentBuilder

        from ovoscope.e2e import detach_intent, register_adapt_intent

        builder = IntentBuilder("R2TestIntent").require("Fruit")
        register_adapt_intent(
            self.bus, builder, lang="en-US", skill_id="sk", settle=0
        )
        registered = [m for m in self.seen
                      if m.msg_type == "register_intent"]
        self.assertEqual(len(registered), 1)
        self.assertEqual(registered[0].context["lang"], "en-US")

        detach_intent(self.bus, "R2TestIntent", skill_id="sk", settle=0)
        detached = [m for m in self.seen if m.msg_type == "detach_intent"]
        self.assertEqual(len(detached), 1)
        self.assertEqual(detached[0].data["intent_name"], "R2TestIntent")

    def test_register_adapt_intent_accepts_a_built_intent(self):
        pytest.importorskip("adapt")
        from adapt.intent import IntentBuilder

        from ovoscope.e2e import register_adapt_intent

        intent = IntentBuilder("R2Built").require("Fruit").build()
        register_adapt_intent(self.bus, intent, skill_id="sk", settle=0)
        self.assertIn("register_intent", self._types())

    def test_register_adapt_intent_stamps_context_skill_id(self):
        # OVOS-INTENT-4 §3.1/§3.2: explicit skill_id is stamped on context
        # since Adapt intent names are not "<skill_id>:<name>"-scoped.
        pytest.importorskip("adapt")
        from adapt.intent import IntentBuilder

        from ovoscope.e2e import register_adapt_intent

        builder = IntentBuilder("R2TestIntent").require("Fruit")
        register_adapt_intent(
            self.bus, builder, lang="en-US", skill_id="sk", settle=0
        )
        registered = [m for m in self.seen if m.msg_type == "register_intent"]
        self.assertEqual(registered[0].context["skill_id"], "sk")


class TestCliErrors(unittest.TestCase):
    def _run_cli(self, argv):
        from ovoscope import cli

        with patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit) as ctx:
                cli.main()
        return ctx.exception.code

    def test_run_with_a_missing_fixture_exits_nonzero(self):
        code = self._run_cli(
            ["ovoscope", "run", "/nonexistent/ovoscope-audit-r2.json"])
        self.assertNotEqual(code, 0)

    def test_run_with_a_failing_fixture_exits_nonzero(self):
        import ovoscope
        from ovoscope import End2EndTest

        test = End2EndTest(
            skill_ids=[],
            source_message=Message("ovoscope.audit.r2.eof"),
            expected_messages=[Message("ovoscope.audit.r2.never.emitted")],
            eof_msgs=["ovoscope.audit.r2.eof"],
            verbose=False,
        )
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        test.save(path)

        bus = FakeBus()
        mc = SimpleNamespace(bus=bus, boot_messages=[],
                             stop=lambda: bus.close())
        with patch.object(ovoscope, "get_minicroft", return_value=mc):
            code = self._run_cli(["ovoscope", "run", path])
        self.assertNotEqual(code, 0,
                            "a failing fixture exited 0")

    def test_diff_with_a_non_fixture_file_exits_nonzero(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"nope": 1}, fh)
        code = self._run_cli(["ovoscope", "diff", path, path])
        self.assertNotEqual(code, 0)


class TestSetupPyEntryPointParsing(unittest.TestCase):
    def _write_setup_py(self, source):
        tmp = tempfile.mkdtemp(prefix="ovoscope-r2-repo-")
        path = os.path.join(tmp, "setup.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(source)
        return path

    def test_happy_path(self):
        from ovoscope.coverage import _parse_setup_py_entry_points

        path = self._write_setup_py(
            "from setuptools import setup\n"
            "setup(name='x',\n"
            "      entry_points={'ovos.plugin.skill': "
            "['my.skill=my.mod:Skill']})\n")
        eps = _parse_setup_py_entry_points(path)
        self.assertIn("ovos.plugin.skill", eps)

    def test_malformed_setup_py_is_recorded_not_raised(self):
        from ovoscope.coverage import _parse_setup_py_entry_points

        path = self._write_setup_py("this is ( not python\n")
        errors = []
        eps = _parse_setup_py_entry_points(path, errors)
        self.assertIsInstance(eps, dict)

    def test_unreadable_setup_py_is_recorded(self):
        from ovoscope.coverage import _parse_setup_py_entry_points

        errors = []
        eps = _parse_setup_py_entry_points(
            "/nonexistent/ovoscope-audit-r2/setup.py", errors)
        self.assertEqual(eps, {})
        self.assertTrue(errors, "an unreadable setup.py recorded no error")


class TestPipelineHarnessState(unittest.TestCase):
    """match -> failure -> match must not leave a stale verdict behind."""

    def test_failure_clears_the_previous_match(self):
        from ovoscope.pipeline import _SinkSkill

        bus = FakeBus()
        sink = _SinkSkill(bus=bus)
        bus.emit(Message("intent.service.skills.activated",
                         {"skill_id": "a"}))
        self.assertIsNotNone(sink._last_match)
        bus.emit(Message("intent_failure"))
        self.assertIsNone(sink._last_match,
                          "a stale match survived an explicit intent failure")
        bus.emit(Message("intent.service.skills.activated",
                         {"skill_id": "b"}))
        self.assertEqual(sink._last_match.data["skill_id"], "b")
        bus.close()

    def test_match_result_clears_state_before_sending(self):
        from ovoscope.pipeline import PipelineHarness, _SinkSkill

        bus = FakeBus()
        harness = PipelineHarness()
        harness._mc = SimpleNamespace(bus=bus)
        harness._sink = _SinkSkill(bus=bus)
        harness._sink._last_match = Message("stale.match")
        result = harness.match_result("nothing will answer", timeout=0.2)
        self.assertTrue(result.timed_out)
        self.assertIsNone(harness._sink._last_match,
                          "match_result did not reset the stale verdict")
        bus.close()

    def test_enter_stops_minicroft_when_wiring_fails(self):
        import ovoscope
        from ovoscope.pipeline import PipelineHarness

        mc = MagicMock()
        # rebinding the sink bus raises -> __enter__ must still stop the boot
        type(mc).bus = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("wiring boom")))
        with patch.object(ovoscope, "get_minicroft", return_value=mc):
            harness = PipelineHarness()
            with self.assertRaises(RuntimeError):
                harness.__enter__()
        mc.stop.assert_called_once()


class TestExecuteIsNotVacuous(unittest.TestCase):
    """Anti-vacuity guard: if an injected failure did NOT surface, every
    End2EndTest-based test in this repo would be worthless."""

    def test_injected_failure_propagates(self):
        from ovoscope import End2EndTest

        bus = FakeBus()
        mc = SimpleNamespace(bus=bus, boot_messages=[])
        test = End2EndTest(
            skill_ids=[],
            source_message=Message("ovoscope.audit.r2.eof"),
            expected_messages=[Message("ovoscope.audit.r2.never.emitted"),
                               Message("ovoscope.audit.r2.also.never")],
            eof_msgs=["ovoscope.audit.r2.eof"],
            verbose=False,
            minicroft=mc,
        )
        with self.assertRaises(AssertionError):
            test._execute(timeout=5)
        bus.close()


if __name__ == "__main__":
    unittest.main()
