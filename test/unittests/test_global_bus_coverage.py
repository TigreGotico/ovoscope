# Copyright 2024 Jarbas AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Unit tests for global bus coverage tracking."""

from unittest.mock import MagicMock
import pytest
from ovos_bus_client.message import Message
from ovos_utils.fakebus import FakeBus

import ovoscope
from ovoscope.bus_coverage import BusCoverageTracker


class TestGlobalBusCoverage:
    @pytest.fixture(autouse=True)
    def setup_globals(self):
        """Reset global state before and after each test."""
        orig_enabled = ovoscope.GLOBAL_BUS_COVERAGE
        orig_collector = ovoscope.GLOBAL_BUS_COVERAGE_COLLECTOR
        
        ovoscope.GLOBAL_BUS_COVERAGE = False
        ovoscope.GLOBAL_BUS_COVERAGE_COLLECTOR = None
        
        yield
        
        ovoscope.GLOBAL_BUS_COVERAGE = orig_enabled
        ovoscope.GLOBAL_BUS_COVERAGE_COLLECTOR = orig_collector

    def test_collector_records_activity(self):
        """GlobalBusCoverageCollector should store registration and invocation counts."""
        collector = ovoscope.GlobalBusCoverageCollector()
        collector.record_registration("test.on")
        collector.record_registration("test.on")
        collector.record_invocation("test.emit")
        
        assert collector.registrations["test.on"] == 2
        assert collector.invocations["test.emit"] == 1

    def test_fakebus_patches_work(self):
        """FakeBus.on and .emit should update the global collector when enabled."""
        ovoscope.GLOBAL_BUS_COVERAGE = True
        ovoscope.GLOBAL_BUS_COVERAGE_COLLECTOR = ovoscope.GlobalBusCoverageCollector()
        
        bus = FakeBus()
        bus.on("global.on", lambda m: None)
        bus.emit(Message("global.emit"))
        
        assert ovoscope.GLOBAL_BUS_COVERAGE_COLLECTOR.registrations["global.on"] == 1
        assert ovoscope.GLOBAL_BUS_COVERAGE_COLLECTOR.invocations["global.emit"] == 1

    def test_tracker_snapshots_global_state(self):
        """BusCoverageTracker snapshots the collector as a BASELINE at __init__.

        Invocations that happened before the tracker existed belong to earlier
        tests and must be subtracted, not inherited.
        """
        ovoscope.GLOBAL_BUS_COVERAGE = True
        ovoscope.GLOBAL_BUS_COVERAGE_COLLECTOR = ovoscope.GlobalBusCoverageCollector()
        ovoscope.GLOBAL_BUS_COVERAGE_COLLECTOR.record_invocation("boot.event")
        ovoscope.GLOBAL_BUS_COVERAGE_COLLECTOR.record_registration("boot.handler")

        bus = FakeBus()
        minicroft = MagicMock()
        minicroft.plugin_skills = {}

        tracker = BusCoverageTracker(bus, minicroft)

        # baseline holds the earlier count …
        assert tracker._collector_baseline["boot.event"] == 1
        # … and the delta over that baseline is zero.
        assert tracker._collector_delta() == {}
        assert tracker._global_registrations["boot.handler"] == 1

    def test_tracker_merges_global_registrations(self):
        """snapshot_listeners should merge global registrations into __core__ if unclaimed."""
        ovoscope.GLOBAL_BUS_COVERAGE = True
        ovoscope.GLOBAL_BUS_COVERAGE_COLLECTOR = ovoscope.GlobalBusCoverageCollector()
        ovoscope.GLOBAL_BUS_COVERAGE_COLLECTOR.record_registration("boot.unclaimed")
        
        bus = FakeBus()
        minicroft = MagicMock()
        minicroft.plugin_skills = {}
        
        tracker = BusCoverageTracker(bus, minicroft)
        tracker.snapshot_listeners()
        
        assert "__core__" in tracker._registered
        assert "boot.unclaimed" in tracker._registered["__core__"]

    def test_tracker_merges_global_invocations(self):
        """build_report sums THIS test's boot delta and its local invocations."""
        ovoscope.GLOBAL_BUS_COVERAGE = True
        ovoscope.GLOBAL_BUS_COVERAGE_COLLECTOR = ovoscope.GlobalBusCoverageCollector()

        bus = FakeBus()
        minicroft = MagicMock()
        minicroft.plugin_skills = {}

        tracker = BusCoverageTracker(bus, minicroft)
        # boot happens AFTER the tracker exists, so it belongs to this test
        ovoscope.GLOBAL_BUS_COVERAGE_COLLECTOR.record_invocation("shared.event")
        tracker.start_tracking()
        bus.emit(Message("shared.event"))  # 1x during test
        tracker.stop_tracking()

        # Manually register the listener so it shows up in report
        tracker._registered = {"__core__": {"shared.event": 1}}

        report = tracker.build_report()
        skill = next(s for s in report.skills if s.skill_id == "__core__")
        handler = next(h for h in skill.listeners if h.msg_type == "shared.event")

        # 1 (this test's boot) + 1 (test) = 2
        assert handler.invocation_count == 2
        assert handler.covered is True
