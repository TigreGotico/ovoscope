"""Adversarial regression tests for the Han audit round 1 fixes.

Each test here reproduces a defect that leaked state, hung, or reported a
false verdict. They are written to FAIL against the pre-fix code.
"""
import time
import unittest
from unittest.mock import MagicMock, patch

import pytest

from ovos_bus_client.message import Message
from ovos_bus_client.session import SessionManager
from ovos_utils.fakebus import FakeBus
from ovos_utils.log import LOG

import ovoscope
from ovoscope import CaptureSession, End2EndTest, get_minicroft
from ovoscope.bus_coverage import BusCoverageTracker
from ovoscope.pipeline import MatchResult


# ---------------------------------------------------------------------------
# A. teardown must run even when an assertion fails
# ---------------------------------------------------------------------------

@pytest.mark.timeout(600)  # boots a real MiniCroft (slow shutdown)
class TestTeardownOnFailure(unittest.TestCase):
    """execute() must stop a managed MiniCroft on the failure path too."""

    def setUp(self):
        LOG.set_level("ERROR")

    def tearDown(self):
        LOG.set_level("CRITICAL")

    def test_managed_minicroft_stopped_when_assertion_fails(self):
        original_bus = SessionManager.bus
        original_pipeline = SessionManager.get_default_session().pipeline[:]

        test = End2EndTest(
            skill_ids=[],
            source_message=Message("ovoscope.audit.never.answered"),
            # deliberately wrong: forces the message-count assertion to fail
            expected_messages=[Message("ovoscope.audit.does.not.happen")],
            eof_msgs=["ovoscope.audit.eof.never.emitted"],
            verbose=False,
        )
        with self.assertRaises(AssertionError):
            test.execute(timeout=2)

        # The whole point: globals are back even though execute() raised.
        self.assertIs(SessionManager.bus, original_bus)
        self.assertEqual(SessionManager.get_default_session().pipeline, original_pipeline)
        self.assertIsNone(test.minicroft)

    def test_capture_timeout_is_reported_as_a_timeout(self):
        """A timeout must say so, not masquerade as a count mismatch."""
        test = End2EndTest(
            skill_ids=[],
            source_message=Message("ovoscope.audit.never.answered"),
            expected_messages=[],
            eof_msgs=["ovoscope.audit.eof.never.emitted"],
            test_message_number=False,
            verbose=False,
        )
        with self.assertRaises(AssertionError) as ctx:
            test.execute(timeout=2)
        self.assertIn("capture timed out", str(ctx.exception))
        self.assertIn("ovoscope.audit.eof.never.emitted", str(ctx.exception))


# ---------------------------------------------------------------------------
# B. the process-wide default session must survive a test unchanged
# ---------------------------------------------------------------------------

@pytest.mark.timeout(600)  # boots a real MiniCroft (slow shutdown)
class TestDefaultSessionIsolation(unittest.TestCase):

    def setUp(self):
        LOG.set_level("ERROR")

    def tearDown(self):
        LOG.set_level("CRITICAL")

    def test_inject_active_does_not_leak_into_default_session(self):
        before = {s[0] for s in SessionManager.get_default_session().active_skills}

        test = End2EndTest(
            skill_ids=[],
            source_message=Message("ovoscope.audit.never.answered"),
            expected_messages=[],
            eof_msgs=["ovoscope.audit.eof.never.emitted"],
            inject_active=["ovoscope-audit-ghost.test"],
            test_message_number=False,
            verbose=False,
        )
        with self.assertRaises(AssertionError):
            test.execute(timeout=2)

        after = {s[0] for s in SessionManager.get_default_session().active_skills}
        self.assertEqual(before, after,
                         "inject_active leaked into the default session")

    def test_stop_restores_default_session_lang_and_pipeline(self):
        sess = SessionManager.get_default_session()
        original_lang = sess.lang
        original_pipeline = sess.pipeline[:]

        mc = get_minicroft([], lang="pt-PT")
        try:
            self.assertEqual(SessionManager.get_default_session().lang, "pt-PT")
        finally:
            mc.stop()

        self.assertEqual(SessionManager.get_default_session().lang, original_lang)
        self.assertEqual(SessionManager.get_default_session().pipeline, original_pipeline)

    def test_default_session_mutated_mid_test_is_restored(self):
        """Even a mutation MiniCroft never made itself must be undone."""
        mc = get_minicroft([])
        try:
            SessionManager.get_default_session().activate_skill("ovoscope-audit-x.test")
        finally:
            mc.stop()
        actives = {s[0] for s in SessionManager.get_default_session().active_skills}
        self.assertNotIn("ovoscope-audit-x.test", actives)


# ---------------------------------------------------------------------------
# C. mock-TTS timers must not outlive the MiniCroft
# ---------------------------------------------------------------------------

@pytest.mark.timeout(600)  # boots a real MiniCroft (slow shutdown)
class TestTTSTimerLifecycle(unittest.TestCase):

    def setUp(self):
        LOG.set_level("ERROR")

    def tearDown(self):
        LOG.set_level("CRITICAL")

    def test_timers_tracked_cancelled_and_silent_after_stop(self):
        # One boot covers the whole lifecycle: MiniCroft boots retain several
        # hundred MB each even after stop(), so the two assertions share it.
        mc = get_minicroft([])
        late = []
        mc.bus.on("recognizer_loop:audio_output_end", lambda m: late.append(m))
        mc.bus.emit(Message("speak", {"utterance": "hello"}))
        with mc._tts_timers_lock:
            timers = list(mc._tts_timers)
        self.assertTrue(timers, "no TTS timer was tracked")
        self.assertTrue(all(t.daemon for t in timers),
                        "TTS timers must be daemon threads")
        late.clear()  # drop anything emitted before stop() — only post-stop
        mc.stop()

        # stop() must have cancelled/joined every one of them.
        with mc._tts_timers_lock:
            self.assertEqual(mc._tts_timers, [])
        self.assertFalse(any(t.is_alive() for t in timers))
        time.sleep(0.4)  # well past the 0.1s timer
        self.assertEqual(late, [],
                         "an orphaned TTS timer fired after stop()")


# ---------------------------------------------------------------------------
# D. bus-coverage must not inherit earlier tests' invocation counts
# ---------------------------------------------------------------------------

class TestBusCoverageDelta:

    def setup_method(self, _):
        self._prev_flag = ovoscope.GLOBAL_BUS_COVERAGE
        self._prev_collector = ovoscope.GLOBAL_BUS_COVERAGE_COLLECTOR

    def teardown_method(self, _):
        ovoscope.GLOBAL_BUS_COVERAGE = self._prev_flag
        ovoscope.GLOBAL_BUS_COVERAGE_COLLECTOR = self._prev_collector

    def _tracker(self):
        bus = FakeBus()
        minicroft = MagicMock()
        minicroft.plugin_skills = {}
        return bus, BusCoverageTracker(bus, minicroft)

    def test_earlier_tests_invocations_are_excluded(self):
        """Counts from BEFORE the tracker existed must not inflate the report."""
        ovoscope.GLOBAL_BUS_COVERAGE = True
        ovoscope.GLOBAL_BUS_COVERAGE_COLLECTOR = ovoscope.GlobalBusCoverageCollector()
        collector = ovoscope.GLOBAL_BUS_COVERAGE_COLLECTOR
        # pretend 5 earlier tests each fired this event
        for _ in range(5):
            collector.record_invocation("shared.event")

        bus, tracker = self._tracker()
        tracker.start_tracking()
        bus.emit(Message("shared.event"))  # 1x during THIS test
        tracker.stop_tracking()
        tracker._registered = {"__core__": {"shared.event": 1}}

        report = tracker.build_report()
        skill = next(s for s in report.skills if s.skill_id == "__core__")
        handler = next(h for h in skill.listeners if h.msg_type == "shared.event")
        assert handler.invocation_count == 1, (
            "coverage inherited invocations from earlier tests"
        )

    def test_own_boot_is_counted_when_tracker_precedes_boot(self):
        ovoscope.GLOBAL_BUS_COVERAGE = True
        ovoscope.GLOBAL_BUS_COVERAGE_COLLECTOR = ovoscope.GlobalBusCoverageCollector()
        collector = ovoscope.GLOBAL_BUS_COVERAGE_COLLECTOR
        collector.record_invocation("boot.event")  # an EARLIER test

        bus, tracker = self._tracker()
        collector.record_invocation("boot.event")  # THIS test's boot
        tracker.start_tracking()
        tracker.stop_tracking()
        tracker._registered = {"__core__": {"boot.event": 1}}

        report = tracker.build_report()
        skill = next(s for s in report.skills if s.skill_id == "__core__")
        handler = next(h for h in skill.listeners if h.msg_type == "boot.event")
        assert handler.invocation_count == 1

    def test_tracking_window_is_not_double_counted(self):
        ovoscope.GLOBAL_BUS_COVERAGE = True
        ovoscope.GLOBAL_BUS_COVERAGE_COLLECTOR = ovoscope.GlobalBusCoverageCollector()

        bus, tracker = self._tracker()
        tracker.start_tracking()
        for _ in range(3):
            bus.emit(Message("dup.event"))
        tracker.stop_tracking()
        tracker._registered = {"__core__": {"dup.event": 1}}

        report = tracker.build_report()
        skill = next(s for s in report.skills if s.skill_id == "__core__")
        handler = next(h for h in skill.listeners if h.msg_type == "dup.event")
        assert handler.invocation_count == 3


# ---------------------------------------------------------------------------
# E. CaptureSession races
# ---------------------------------------------------------------------------

class TestCaptureSessionRaces(unittest.TestCase):
    """CaptureSession only ever touches ``minicroft.bus`` — a FakeBus-backed
    stub keeps these race tests fast and saves a full (memory-hungry) boot."""

    def setUp(self):
        LOG.set_level("ERROR")
        from types import SimpleNamespace
        from ovos_utils.fakebus import FakeBus
        self.mc = SimpleNamespace(bus=FakeBus())

    def tearDown(self):
        LOG.set_level("CRITICAL")

    def test_capture_reports_timeout(self):
        cap = CaptureSession(self.mc, eof_msgs=["ovoscope.audit.no.such.eof"])
        completed = cap.capture(Message("ovoscope.audit.ping"), timeout=1)
        cap.finish()
        self.assertFalse(completed)
        self.assertTrue(cap.timed_out)
        self.assertEqual(cap.timeout_seconds, 1)

    def test_finish_returns_a_copy(self):
        cap = CaptureSession(self.mc, eof_msgs=["ovoscope.audit.eof"])
        cap.capture(Message("ovoscope.audit.eof"), timeout=5)
        got = cap.finish()
        self.assertIsNot(got, cap.responses,
                         "finish() handed out the live response list")
        got.append(Message("mutated.by.caller"))
        self.assertNotIn("mutated.by.caller",
                         [m.msg_type for m in cap.responses])

    def test_eof_counter_reset_is_atomic(self):
        """A late eof handler must never leak into the NEXT capture."""
        cap = CaptureSession(self.mc, eof_msgs=["ovoscope.audit.eof"],
                             eof_count=2)
        # one eof is not enough — capture must time out
        completed = cap.capture(Message("ovoscope.audit.eof"), timeout=1)
        self.assertFalse(completed)
        self.assertEqual(cap._eof_seen, 1)
        # a second capture starts from a clean counter
        completed = cap.capture(Message("ovoscope.audit.eof"), timeout=1)
        cap.finish()
        self.assertFalse(completed,
                         "eof counter was not reset between captures")


# ---------------------------------------------------------------------------
# G. PipelineHarness.match() verdicts
# ---------------------------------------------------------------------------

class TestMatchResult:

    def test_handler_start_does_not_suppress_a_match(self):
        """`mycroft.skill.handler.start` fires on SUCCESS, never on failure.

        Treating it as a failure signal made every successful match report
        "no match".
        """
        from ovoscope.pipeline import PipelineHarness

        bus = FakeBus()
        matched = Message("intent.service.skills.activated",
                          {"skill_id": "audit.skill"})

        def _answer(_m):
            # exactly what a successful dispatch looks like on the wire
            bus.emit(Message("mycroft.skill.handler.start"))
            bus.emit(matched)

        bus.on("recognizer_loop:utterance", _answer)

        harness = PipelineHarness()
        harness._mc = MagicMock()
        harness._mc.bus = bus

        result = harness.match_result("turn on the lights", timeout=3.0)
        assert result.outcome == "matched", result.outcome
        assert result.message.msg_type == "intent.service.skills.activated"

    def test_match_result_discriminates_outcomes(self):
        assert MatchResult("matched", Message("x")).matched is True
        assert MatchResult("timeout").timed_out is True
        assert MatchResult("no_match").matched is False
        assert MatchResult("no_match").timed_out is False

    def test_assert_no_match_fails_on_timeout(self):
        """Silence is a broken harness, not proof of absence."""
        from ovoscope.pipeline import PipelineHarness

        harness = PipelineHarness.__new__(PipelineHarness)
        harness.match_result = lambda utt, timeout=2.0: MatchResult("timeout")
        with pytest.raises(AssertionError, match="no verdict"):
            PipelineHarness.assert_no_match(harness, "nonsense")

    def test_assert_no_match_passes_on_explicit_failure(self):
        from ovoscope.pipeline import PipelineHarness

        harness = PipelineHarness.__new__(PipelineHarness)
        harness.match_result = lambda utt, timeout=2.0: MatchResult("no_match")
        PipelineHarness.assert_no_match(harness, "nonsense")

    def test_assert_matches_fails_loudly_on_timeout(self):
        from ovoscope.pipeline import PipelineHarness

        harness = PipelineHarness.__new__(PipelineHarness)
        harness.match_result = lambda utt, timeout=5.0: MatchResult("timeout")
        with pytest.raises(AssertionError, match="no verdict"):
            PipelineHarness.assert_matches(harness, "turn on the lights")

    def test_match_returns_the_message_of_a_match_result(self):
        from ovoscope.pipeline import PipelineHarness

        msg = Message("intent.service.skills.activated")
        harness = PipelineHarness.__new__(PipelineHarness)
        harness.match_result = lambda utt, timeout=5.0: MatchResult("matched", msg)
        assert PipelineHarness.match(harness, "hi") is msg


# ---------------------------------------------------------------------------
# H. wait_for_match emit= parameter and match/fail race
# ---------------------------------------------------------------------------

class TestWaitForMatch:

    def test_emit_happens_after_subscription(self):
        """A synchronous FakeBus reply must not be missed."""
        from ovoscope.e2e import wait_for_match

        bus = FakeBus()
        # answers synchronously, inside emit()
        bus.on("audit.q", lambda m: bus.emit(Message("audit.a")))

        got = wait_for_match(bus, ["audit.a"], timeout=2.0,
                             emit=Message("audit.q"))
        assert got is not None
        assert got.msg_type == "audit.a"

    def test_match_wins_over_concurrent_failure(self):
        """A real match must not be dropped because a failure raced it."""
        from ovoscope.e2e import wait_for_match

        bus = FakeBus()

        def _answer(_m):
            bus.emit(Message("complete_intent_failure"))
            bus.emit(Message("audit.match"))

        bus.on("audit.q2", _answer)
        got = wait_for_match(bus, ["audit.match"], timeout=2.0,
                             emit=Message("audit.q2"))
        assert got is not None and got.msg_type == "audit.match"

    def test_docstring_no_longer_tells_callers_to_emit_after(self):
        from ovoscope.e2e import wait_for_match

        assert "emit" in (wait_for_match.__doc__ or "")
        assert "BLOCKS" in (wait_for_match.__doc__ or "")


# ---------------------------------------------------------------------------
# I. OCP HTTP mock must see the URL
# ---------------------------------------------------------------------------

class TestOCPHttpMock:

    def test_configured_url_body_is_returned(self):
        from ovoscope.ocp import _build_get_side_effect

        get = _build_get_side_effect({"bandcamp.com": {"tracks": ["Blue Note"]}})
        resp = get("https://bandcamp.com/api/search?q=jazz")
        assert resp.json() == {"tracks": ["Blue Note"]}

    def test_unconfigured_url_falls_back_to_empty(self):
        from ovoscope.ocp import _build_get_side_effect

        get = _build_get_side_effect({"bandcamp.com": {"tracks": []}})
        assert get("https://example.com/other").json() == {}

    def test_url_passed_as_keyword_is_matched(self):
        from ovoscope.ocp import _build_get_side_effect

        get = _build_get_side_effect({"youtube.com": {"items": [1]}})
        assert get(url="https://youtube.com/results").json() == {"items": [1]}

    def test_patch_targets_receive_the_side_effect(self):
        from ovoscope.ocp import OCPTest

        test = OCPTest(skill_ids=[], utterance="play jazz",
                       mock_responses={"bandcamp.com": {"ok": True}})
        patches = test._build_patches()
        assert patches, "no patches were built"
        import requests
        for p in patches:
            p.__enter__()
        try:
            assert requests.get("https://bandcamp.com/x").json() == {"ok": True}
        finally:
            for p in reversed(patches):
                p.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# J / K. harness teardown robustness
# ---------------------------------------------------------------------------

class TestHarnessTeardown:

    def test_audio_harness_closes_bus_when_shutdown_raises(self):
        from ovoscope.audio import AudioServiceHarness

        harness = AudioServiceHarness.__new__(AudioServiceHarness)
        harness.service = MagicMock()
        harness.service.shutdown.side_effect = RuntimeError("boom")
        harness.bus = MagicMock()

        with pytest.raises(RuntimeError):
            harness.__exit__(None, None, None)
        harness.bus.close.assert_called_once()

    def test_two_playback_harnesses_are_refused(self):
        from ovoscope.audio import PlaybackServiceHarness

        first = PlaybackServiceHarness.__new__(PlaybackServiceHarness)
        PlaybackServiceHarness._active = first
        try:
            with pytest.raises(RuntimeError, match="already active"):
                with PlaybackServiceHarness():
                    pass
        finally:
            PlaybackServiceHarness._active = None

    def test_playback_harness_restores_previous_tts_queue(self):
        from queue import Queue
        from ovos_plugin_manager.templates.tts import TTS
        from ovoscope.audio import PlaybackServiceHarness

        sentinel = Queue()
        TTS.queue = sentinel
        try:
            with PlaybackServiceHarness():
                assert TTS.queue is not sentinel
            assert TTS.queue is sentinel, "previous TTS.queue was not restored"
            assert PlaybackServiceHarness._active is None
        finally:
            TTS.queue = sentinel

    def test_miniphal_detaches_capture_handler(self):
        from ovoscope.phal import MiniPHAL

        phal = MiniPHAL()
        bus = phal._bus
        with phal:
            pass
        before = len(phal._captured)
        bus.emit(Message("audit.after.exit"))
        assert len(phal._captured) == before, (
            "MiniPHAL kept capturing after __exit__"
        )

    def test_listener_harness_detaches_capture_handler(self):
        from ovoscope.voice_loop import ListenerHarness

        bus = FakeBus()
        harness = ListenerHarness(bus=bus)
        with harness:
            bus.emit(Message("audit.during"))
        assert any(m.msg_type == "audit.during" for m in harness._messages)
        before = len(harness._messages)
        bus.emit(Message("audit.after"))
        assert len(harness._messages) == before, (
            "capture handler survived __exit__ on a shared bus"
        )


# ---------------------------------------------------------------------------
# N. PHAL load failures
# ---------------------------------------------------------------------------

class TestPHALLoadErrors:

    def test_load_failure_raises_by_default(self):
        from ovoscope.phal import MiniPHAL

        def _boom(_bus):
            raise ValueError("no hardware")

        with pytest.raises(RuntimeError, match="failed to load"):
            with MiniPHAL(plugin_ids=["audit.plugin"],
                          plugin_factories={"audit.plugin": _boom}):
                pass

    def test_tolerated_failure_is_recorded_and_quoted(self):
        from ovoscope.phal import MiniPHAL

        def _boom(_bus):
            raise ValueError("no hardware")

        with MiniPHAL(plugin_ids=["audit.plugin"],
                      plugin_factories={"audit.plugin": _boom},
                      tolerate_load_errors=True) as phal:
            assert phal.load_errors
            with pytest.raises(AssertionError) as ctx:
                phal.assert_emitted("audit.never", timeout=0.1)
            assert "failed to load" in str(ctx.value)


# ---------------------------------------------------------------------------
# O. coverage.py must not swallow a malformed manifest
# ---------------------------------------------------------------------------

class TestCoverageParseErrors:

    def test_malformed_pyproject_is_recorded(self, tmp_path):
        from ovoscope.coverage import _parse_entry_points

        bad = tmp_path / "pyproject.toml"
        bad.write_text('[project\nname = "oops"\n')
        errors = []
        _parse_entry_points(str(bad), errors)
        assert errors, "a malformed pyproject.toml was silently ignored"
        assert "invalid TOML" in errors[0][1]

    def test_unreadable_setup_py_is_recorded(self, tmp_path):
        from ovoscope.coverage import _parse_setup_py_entry_points

        errors = []
        _parse_setup_py_entry_points(str(tmp_path / "missing_setup.py"), errors)
        assert errors and "unreadable" in errors[0][1]

    def test_report_exposes_parse_errors(self):
        from ovoscope.coverage import EcosystemCoverageReport

        assert EcosystemCoverageReport().parse_errors == []

    def test_valid_pyproject_records_no_error(self, tmp_path):
        from ovoscope.coverage import _parse_entry_points

        good = tmp_path / "pyproject.toml"
        good.write_text(
            '[project]\nname = "x"\n\n'
            '[project.entry-points."opm.skill"]\n'
            '"my-skill.author" = "my_skill:create"\n'
        )
        errors = []
        eps = _parse_entry_points(str(good), errors)
        assert errors == []
        assert eps["opm.skill"] == ["my-skill.author"]


# ---------------------------------------------------------------------------
# P. RemoteRecorder must not leak a reconnecting client
# ---------------------------------------------------------------------------

class TestRemoteRecorderConnectLeak:

    def test_client_is_closed_on_connect_timeout(self):
        from ovoscope.remote_recorder import RemoteRecorder

        rec = RemoteRecorder(bus_url="ws://127.0.0.1:1/core")
        fake_client = MagicMock()
        fake_client.connected_event.is_set.return_value = False

        with patch("ovos_bus_client.client.MessageBusClient",
                   return_value=fake_client), \
                patch("time.monotonic", side_effect=[0.0, 100.0, 200.0]), \
                patch("time.sleep"):
            with pytest.raises(ConnectionError):
                rec.connect()

        fake_client.close.assert_called_once()
        assert rec._client is None, "a dead client was left reconnecting forever"


# ---------------------------------------------------------------------------
# F. cmd_run must not boot a second MiniCroft
# ---------------------------------------------------------------------------

class TestCliRunSingleMiniCroft:

    def test_cmd_run_hands_its_minicroft_to_the_test(self, tmp_path):
        import argparse
        from ovoscope import cli

        fixture = tmp_path / "f.json"
        fixture.write_text("{}")

        mc = MagicMock()
        test = MagicMock()
        test.skill_ids = []

        with patch("ovoscope.End2EndTest.from_path", return_value=test), \
                patch("ovoscope.get_minicroft", return_value=mc):
            cli.cmd_run(argparse.Namespace(fixture=str(fixture), timeout=5,
                                           verbose=False))

        assert test.minicroft is mc, "cmd_run did not reuse its MiniCroft"
        assert test.managed is False, "execute() would boot a second MiniCroft"
        mc.stop.assert_called_once()


# ---------------------------------------------------------------------------
# L. shared MiniCroft lifecycle
# ---------------------------------------------------------------------------

class TestSharedMiniCroftLifecycle:

    def test_only_one_instance_stays_live(self):
        from ovoscope import intent_cases

        key = intent_cases._SHARED_MINICROFT_KEY
        cache = vars(intent_cases).setdefault(key, {})
        cache.clear()

        old = MagicMock()
        cache[("old-skill", ("en-US",))] = old

        new = MagicMock()
        with patch.object(intent_cases, "get_minicroft", return_value=new):
            got = intent_cases._shared_minicroft("new-skill", ["en-US"], 0)

        assert got is new
        old.stop.assert_called_once()
        assert len(cache) == 1, "two shared MiniCrofts were kept alive"
        cache.clear()

    def test_stop_shared_minicrofts_empties_the_cache(self):
        from ovoscope import intent_cases

        key = intent_cases._SHARED_MINICROFT_KEY
        cache = vars(intent_cases).setdefault(key, {})
        cache.clear()
        mc = MagicMock()
        cache[("s", ("en-US",))] = mc

        intent_cases.stop_shared_minicrofts()
        mc.stop.assert_called_once()
        assert cache == {}


# ---------------------------------------------------------------------------
# M. MiniSimpleListener must not reuse a wedged listener thread
# ---------------------------------------------------------------------------

class TestSimpleListenerWedgedThread:

    def test_wedged_listener_is_replaced(self):
        pytest.importorskip("ovos_simple_listener")
        from ovoscope.simple_listener import MiniSimpleListener

        # SimpleListener eagerly builds a real microphone in __init__, so the
        # harness can only be constructed where a microphone plugin exists.
        # Construct the harness itself under the guard: earlier tests can
        # change the loaded Configuration, so probing the factory separately
        # is order-dependent — the harness construction is the real gate.
        try:
            harness = MiniSimpleListener()
        except Exception as e:
            pytest.skip(f"no usable microphone plugin: {e}")
        first = harness.listener
        try:
            # pretend the thread refuses to die
            with patch.object(type(first), "is_alive", return_value=True), \
                    patch.object(type(first), "start"), \
                    patch.object(type(first), "stop"), \
                    patch.object(type(first), "join"):
                harness.feed_file(b"\x00" * 4096, timeout=0.2)
            assert harness.listener is not first, (
                "a wedged listener was reused for the next run"
            )
        finally:
            harness.detach_capture()


if __name__ == "__main__":
    unittest.main()
