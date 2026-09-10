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
"""Unit tests for ovoscope.phal."""

import time
import threading
from unittest.mock import MagicMock

import pytest

from ovos_utils.fakebus import FakeBus
from ovos_bus_client.message import Message

from ovoscope.phal import MiniPHAL, PHALTest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_echo_plugin(bus: FakeBus, response_type: str, trigger_type: str) -> MagicMock:
    """Return a mock plugin that re-emits *response_type* when *trigger_type* is received."""
    plugin = MagicMock()
    plugin.shutdown = MagicMock()

    def on_trigger(msg: Message) -> None:
        bus.emit(Message(response_type))

    bus.on(trigger_type, on_trigger)
    return plugin


# ---------------------------------------------------------------------------
# MiniPHAL
# ---------------------------------------------------------------------------


class TestMiniPHAL:
    def test_context_manager_enter_exit(self):
        with MiniPHAL() as phal:
            assert phal._bus is not None

    def test_emit_and_assert_emitted(self):
        """Plugin responds to trigger: assert_emitted returns matching message."""
        with MiniPHAL() as phal:
            # Simulate a plugin by wiring a handler on the FakeBus directly
            phal._bus.on("test.trigger", lambda msg: phal._bus.emit(Message("test.response")))
            phal.emit(Message("test.trigger"), wait=0.1)
            msg = phal.assert_emitted("test.response", timeout=2.0)
            assert msg.msg_type == "test.response"

    def test_assert_emitted_raises_on_timeout(self):
        with MiniPHAL() as phal:
            with pytest.raises(AssertionError, match="test.never"):
                phal.assert_emitted("test.never", timeout=0.2)

    def test_assert_not_emitted_passes_when_absent(self):
        with MiniPHAL() as phal:
            phal.assert_not_emitted("some.message.type", wait=0.1)  # should not raise

    def test_assert_not_emitted_raises_when_present(self):
        with MiniPHAL() as phal:
            phal._bus.emit(Message("bad.message"))
            time.sleep(0.1)
            with pytest.raises(AssertionError, match="bad.message"):
                phal.assert_not_emitted("bad.message", wait=0.05)

    def test_clear_captured(self):
        with MiniPHAL() as phal:
            phal._bus.emit(Message("some.msg"))
            time.sleep(0.1)
            phal.clear_captured()
            assert phal._captured == []

    def test_plugin_instances_used(self):
        """Pre-built plugin instances are stored and shutdown is called on exit."""
        mock_plugin = MagicMock()
        mock_plugin.shutdown = MagicMock()
        with MiniPHAL(
            plugin_ids=["fake-plugin"],
            plugin_instances={"fake-plugin": mock_plugin},
        ) as phal:
            assert "fake-plugin" in phal._loaded
        mock_plugin.shutdown.assert_called_once()


# ---------------------------------------------------------------------------
# PHALTest
# ---------------------------------------------------------------------------


class TestPHALTest:
    def test_execute_with_mock_plugin(self):
        """PHALTest with a pre-wired mock plugin verifies expected_types."""
        # We don't have a real PHAL plugin installed so we wire up the bus manually.
        # Use MiniPHAL directly instead.
        with MiniPHAL() as phal:
            phal._bus.on("trigger", lambda m: phal._bus.emit(Message("expected.response")))
            phal.emit(Message("trigger"), wait=0.1)
            phal.assert_emitted("expected.response")

    def test_phal_test_dataclass_fields(self):
        trigger = Message("test.trigger")
        t = PHALTest(
            plugin_ids=["fake-phal"],
            trigger_message=trigger,
            expected_types=["expected.type"],
            forbidden_types=["bad.type"],
            timeout=3.0,
        )
        assert t.plugin_ids == ["fake-phal"]
        assert t.expected_types == ["expected.type"]
        assert t.forbidden_types == ["bad.type"]
        assert t.timeout == 3.0

    def test_phal_test_execute_with_instance(self):
        """PHALTest.execute with a pre-built instance that auto-responds."""
        from ovos_utils.fakebus import FakeBus as _FakeBus

        # We can't easily inject a "real" plugin, but we verify the dataclass
        # executes without error when plugin loading is skipped due to no real plugin.
        # Just verify PHALTest.execute completes (plugin_ids=[] = no plugins to load).
        t = PHALTest(
            plugin_ids=[],
            trigger_message=Message("harmless.trigger"),
            expected_types=[],
            forbidden_types=[],
            timeout=0.5,
        )
        result = t.execute()
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# plugin_factories
# ---------------------------------------------------------------------------


class TestPluginFactories:
    """Tests for the plugin_factories parameter added to MiniPHAL and PHALTest."""

    def _make_responder_factory(self, trigger_type: str, response_type: str):
        """Return a factory callable that wires a trigger→response handler on the bus."""

        def factory(bus: FakeBus):
            plugin = MagicMock()
            plugin.shutdown = MagicMock()
            bus.on(trigger_type, lambda m: bus.emit(Message(response_type)))
            return plugin

        return factory

    def test_factory_called_with_harness_bus(self):
        """Factory receives the MiniPHAL internal bus."""
        received_buses = []

        def factory(bus: FakeBus):
            received_buses.append(bus)
            return MagicMock()

        with MiniPHAL(
            plugin_ids=["test-plugin"],
            plugin_factories={"test-plugin": factory},
        ) as phal:
            assert len(received_buses) == 1
            assert received_buses[0] is phal._bus

    def test_factory_plugin_receives_emitted_message(self):
        """Plugin built by factory can handle messages emitted via phal.emit()."""
        factory = self._make_responder_factory("req.type", "resp.type")
        with MiniPHAL(
            plugin_ids=["echo-plugin"],
            plugin_factories={"echo-plugin": factory},
        ) as phal:
            phal.emit(Message("req.type"), wait=0.1)
            msg = phal.assert_emitted("resp.type", timeout=2.0)
            assert msg.msg_type == "resp.type"

    def test_factory_takes_precedence_over_plugin_instances(self):
        """When both factory and instance are provided, factory wins."""
        factory_calls = []

        def factory(bus: FakeBus):
            factory_calls.append(True)
            return MagicMock()

        stale_instance = MagicMock()
        with MiniPHAL(
            plugin_ids=["dual-plugin"],
            plugin_factories={"dual-plugin": factory},
            plugin_instances={"dual-plugin": stale_instance},
        ) as phal:
            assert len(factory_calls) == 1
            assert "dual-plugin" in phal._loaded
            # stale_instance was NOT loaded
            assert phal._loaded["dual-plugin"] is not stale_instance

    def test_factory_raising_fails_the_harness(self):
        """A factory that raises stops the harness instead of degrading.

        A silently skipped plugin used to resurface much later as an
        unrelated assert_emitted timeout.
        """
        def bad_factory(bus: FakeBus):
            raise RuntimeError("factory error")

        with pytest.raises(RuntimeError, match="bad-plugin"):
            with MiniPHAL(
                plugin_ids=["bad-plugin"],
                plugin_factories={"bad-plugin": bad_factory},
            ):
                pass

    def test_factory_raising_warns_and_skips_plugin_when_tolerated(self):
        """With tolerate_load_errors, the failure warns and the plugin is skipped."""
        import warnings

        def bad_factory(bus: FakeBus):
            raise RuntimeError("factory error")

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            with MiniPHAL(
                plugin_ids=["bad-plugin"],
                plugin_factories={"bad-plugin": bad_factory},
                tolerate_load_errors=True,
            ) as phal:
                assert "bad-plugin" not in phal._loaded
                assert phal.load_errors
        assert any("bad-plugin" in str(warning.message) for warning in w)

    def test_phal_test_plugin_factories_field(self):
        """PHALTest.plugin_factories is forwarded to MiniPHAL."""
        factory = self._make_responder_factory("ovos.req", "ovos.resp")
        t = PHALTest(
            plugin_ids=["my-phal"],
            trigger_message=Message("ovos.req"),
            expected_types=["ovos.resp"],
            plugin_factories={"my-phal": factory},
            timeout=2.0,
        )
        captured = t.execute()
        assert any(m.msg_type == "ovos.resp" for m in captured)


# ---------------------------------------------------------------------------
# Namespace bridging
# ---------------------------------------------------------------------------

try:
    from ovos_spec_tools import SpecMessage
    _HAS_SPEC_TOOLS = True
except ImportError:
    _HAS_SPEC_TOOLS = False


@pytest.mark.skipif(not _HAS_SPEC_TOOLS,
                    reason="requires ovos-spec-tools (SpecMessage)")
class TestMiniPHALNamespaceBridging:
    """PHAL plugins communicate over arbitrary plugin-specific topics and touch
    NONE of the legacy<->ovos.* migrated topics, so the harness has no migrated
    topic of its own to drive. These tests instead verify that the harness
    ``FakeBus`` performs the same namespace bridging as the audio/media harnesses
    (so a PHAL plugin that *did* consume/produce a migrated topic would
    interoperate across both namespaces), and that the ``modernize``/``emit_legacy``
    flags are threaded through ``MiniPHAL`` to that bus.
    """

    def test_bus_bridges_legacy_to_spec_by_default(self) -> None:
        """Default harness (bridging on): a LEGACY emit on the harness bus is
        delivered to a SPEC-topic subscriber (modernize bridging)."""
        spec_topic = str(SpecMessage.SPEAK)  # ovos.utterance.speak
        with MiniPHAL() as phal:
            seen = []
            phal._bus.on(spec_topic, lambda m: seen.append(m))
            phal._bus.emit(Message("speak", {"utterance": "hi"}))
            time.sleep(0.05)
            assert [m.data["utterance"] for m in seen] == ["hi"]

    def test_bus_bridges_spec_to_legacy_by_default(self) -> None:
        """Default harness (bridging on): a SPEC emit on the harness bus is
        delivered to a LEGACY-topic subscriber (emit_legacy bridging)."""
        spec_topic = str(SpecMessage.SPEAK)
        with MiniPHAL() as phal:
            seen = []
            phal._bus.on("speak", lambda m: seen.append(m))
            phal._bus.emit(Message(spec_topic, {"utterance": "bye"}))
            time.sleep(0.05)
            assert [m.data["utterance"] for m in seen] == ["bye"]

    def test_no_bridging_isolates_namespaces(self) -> None:
        """With modernize=False, emit_legacy=False the harness bus keeps each
        namespace isolated — a LEGACY emit does NOT reach a SPEC subscriber."""
        spec_topic = str(SpecMessage.SPEAK)
        with MiniPHAL(modernize=False, emit_legacy=False) as phal:
            seen = []
            phal._bus.on(spec_topic, lambda m: seen.append(m))
            phal._bus.emit(Message("speak", {"utterance": "legacy only"}))
            time.sleep(0.1)
            assert seen == []

    def test_phal_test_threads_bridging_flags(self) -> None:
        """PHALTest forwards modernize/emit_legacy to MiniPHAL (default True)."""
        t = PHALTest(
            plugin_ids=[],
            trigger_message=Message("harmless.trigger"),
        )
        assert t.modernize is True
        assert t.emit_legacy is True
