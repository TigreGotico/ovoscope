"""Unit tests for GUICaptureSession assertion methods."""
import unittest
from unittest.mock import MagicMock

from ovos_bus_client.message import Message
from ovoscope import GUICaptureSession


class TestGUICaptureSessionAssertions(unittest.TestCase):
    """Test GUICaptureSession assertion methods with synthetic messages."""

    def _make_session(self) -> GUICaptureSession:
        """Create a GUICaptureSession with a mock bus (not started)."""
        session = GUICaptureSession(bus=MagicMock())
        return session

    def _value_set_msg(self, namespace: str, data: dict) -> Message:
        """Build a gui.value.set message."""
        return Message(
            "gui.value.set",
            {"namespace": namespace, "data": data},
        )

    def _page_show_msg(self, namespace: str, page: str) -> Message:
        """Build a gui.page.show message."""
        return Message(
            "gui.page.show",
            {"namespace": namespace, "pages": [page]},
        )

    # -- assert_namespace_has_key --

    def test_assert_namespace_has_key_found(self) -> None:
        """Key present in namespace data should pass."""
        session = self._make_session()
        session.messages = [self._value_set_msg("weatherskill", {"current_temp": 22})]
        session.assert_namespace_has_key("weatherskill", "current_temp")

    def test_assert_namespace_has_key_missing(self) -> None:
        """Missing key should raise AssertionError."""
        session = self._make_session()
        session.messages = [self._value_set_msg("weatherskill", {"current_temp": 22})]
        with self.assertRaises(AssertionError):
            session.assert_namespace_has_key("weatherskill", "location")

    def test_assert_namespace_has_key_wrong_namespace(self) -> None:
        """Key in different namespace should not match."""
        session = self._make_session()
        session.messages = [self._value_set_msg("otherskill", {"current_temp": 22})]
        with self.assertRaises(AssertionError):
            session.assert_namespace_has_key("weatherskill", "current_temp")

    def test_assert_namespace_has_key_none_value(self) -> None:
        """Key with None value should still pass (key exists)."""
        session = self._make_session()
        session.messages = [self._value_set_msg("skill", {"key": None})]
        session.assert_namespace_has_key("skill", "key")

    # -- assert_namespace_value --

    def test_assert_namespace_value_match(self) -> None:
        """Exact value match should pass."""
        session = self._make_session()
        session.messages = [self._value_set_msg("skill", {"greeting": "Hello!"})]
        session.assert_namespace_value("skill", "greeting", "Hello!")

    def test_assert_namespace_value_mismatch(self) -> None:
        """Wrong value should raise AssertionError."""
        session = self._make_session()
        session.messages = [self._value_set_msg("skill", {"greeting": "Hello!"})]
        with self.assertRaises(AssertionError):
            session.assert_namespace_value("skill", "greeting", "Goodbye!")

    # -- assert_page_shown --

    def test_assert_page_shown_match(self) -> None:
        """Page in namespace should pass."""
        session = self._make_session()
        session.messages = [self._page_show_msg("helloworldskill", "hello.qml")]
        session.assert_page_shown("helloworldskill", "hello.qml")

    def test_assert_page_shown_missing(self) -> None:
        """Missing page should raise AssertionError."""
        session = self._make_session()
        session.messages = [self._page_show_msg("helloworldskill", "hello.qml")]
        with self.assertRaises(AssertionError):
            session.assert_page_shown("helloworldskill", "goodbye.qml")

    # -- __from and page_names wire format --

    def test_assert_page_shown_from_field(self) -> None:
        """Page show using __from and page_names (real wire format)."""
        session = self._make_session()
        session.messages = [Message(
            "gui.page.show",
            {"page_names": ["SYSTEM_clock"], "__from": "ovos-skill-date-time.openvoiceos"},
        )]
        session.assert_page_shown("ovos-skill-date-time.openvoiceos",
                                  "SYSTEM_clock")

    def test_assert_namespace_has_key_from_field(self) -> None:
        """Value set using __from (real wire format)."""
        session = self._make_session()
        session.messages = [Message(
            "gui.value.set",
            {"__from": "ovos-skill-weather.openvoiceos", "current_temp": 22},
        )]
        session.assert_namespace_has_key("ovos-skill-weather.openvoiceos",
                                         "current_temp")

    # -- assert_template_shown (SYSTEM_* template model) --

    def test_assert_template_shown_with_prefix(self) -> None:
        """Full SYSTEM_ name should match the shown template."""
        session = self._make_session()
        session.messages = [self._page_show_msg("weatherskill", "SYSTEM_weather")]
        session.assert_template_shown("weatherskill", "SYSTEM_weather")

    def test_assert_template_shown_without_prefix(self) -> None:
        """Short name is normalized to the SYSTEM_ prefix."""
        session = self._make_session()
        session.messages = [self._page_show_msg("weatherskill", "SYSTEM_weather")]
        session.assert_template_shown("weatherskill", "weather")

    def test_assert_template_shown_with_values(self) -> None:
        """Template + accompanying session-data values both asserted."""
        session = self._make_session()
        session.messages = [
            self._page_show_msg("weatherskill", "SYSTEM_weather"),
            self._value_set_msg("weatherskill", {"current_temp": 22,
                                                 "condition": "Sunny"}),
        ]
        session.assert_template_shown("weatherskill", "weather",
                                      values={"current_temp": 22,
                                              "condition": "Sunny"})

    def test_assert_template_shown_missing_template(self) -> None:
        """Template never shown should raise."""
        session = self._make_session()
        session.messages = [self._page_show_msg("weatherskill", "SYSTEM_text")]
        with self.assertRaises(AssertionError):
            session.assert_template_shown("weatherskill", "weather", timeout=0.1)

    def test_assert_template_shown_wrong_value(self) -> None:
        """Template shown but a listed value differs should raise."""
        session = self._make_session()
        session.messages = [
            self._page_show_msg("weatherskill", "SYSTEM_weather"),
            self._value_set_msg("weatherskill", {"current_temp": 22}),
        ]
        with self.assertRaises(AssertionError):
            session.assert_template_shown("weatherskill", "weather",
                                          values={"current_temp": 99})

    # -- assert_namespace_cleared --

    def test_assert_namespace_cleared_match(self) -> None:
        """Namespace clear message should pass."""
        session = self._make_session()
        session.messages = [Message("gui.namespace.remove", {"namespace": "skill"})]
        session.assert_namespace_cleared("skill")

    def test_assert_namespace_cleared_missing(self) -> None:
        """No clear message should raise AssertionError."""
        session = self._make_session()
        session.messages = []
        with self.assertRaises(AssertionError):
            session.assert_namespace_cleared("skill")


if __name__ == "__main__":
    unittest.main()

    # -- near-match assertions must FAIL (round-2 audit) --

    def test_page_shown_rejects_near_match_namespace(self) -> None:
        """A namespace that merely CONTAINS the expected one must not pass."""
        session = self._make_session()
        session.messages = [self._page_show_msg("weatherskill-extended",
                                                "hello.qml")]
        with self.assertRaises(AssertionError):
            session.assert_page_shown("weatherskill", "hello.qml", timeout=0.1)

    def test_page_shown_rejects_near_match_page(self) -> None:
        """A page name that merely CONTAINS the expected one must not pass."""
        session = self._make_session()
        session.messages = [self._page_show_msg("weatherskill",
                                                "hello_world.qml")]
        with self.assertRaises(AssertionError):
            session.assert_page_shown("weatherskill", "hello.qml", timeout=0.1)

    def test_page_shown_matches_page_basename(self) -> None:
        """A directory prefix on the shown page does not change the result."""
        session = self._make_session()
        session.messages = [self._page_show_msg("weatherskill",
                                                "ui/hello.qml")]
        session.assert_page_shown("weatherskill", "hello.qml")

    def test_page_shown_opt_in_prefix_matching(self) -> None:
        """exact=False restores the old prefix/substring behaviour."""
        session = self._make_session()
        session.messages = [self._page_show_msg("weatherskill-extended",
                                                "hello_world.qml")]
        session.assert_page_shown("weatherskill", "hello", exact=False)

    def test_namespace_value_rejects_near_match_namespace(self) -> None:
        """assert_namespace_value must not pass on a containing namespace."""
        session = self._make_session()
        session.messages = [self._value_set_msg("skill-extended",
                                                {"greeting": "Hello!"})]
        with self.assertRaises(AssertionError):
            session.assert_namespace_value("skill", "greeting", "Hello!")

    def test_namespace_has_key_rejects_near_match_namespace(self) -> None:
        """assert_namespace_has_key must not pass on a containing namespace."""
        session = self._make_session()
        session.messages = [self._value_set_msg("skill-extended", {"key": 1})]
        with self.assertRaises(AssertionError):
            session.assert_namespace_has_key("skill", "key")

    def test_namespace_cleared_rejects_near_match_namespace(self) -> None:
        """assert_namespace_cleared must not pass on a containing namespace."""
        session = self._make_session()
        session.messages = [Message("gui.clear.namespace",
                                    {"namespace": "skill-extended"})]
        with self.assertRaises(AssertionError):
            session.assert_namespace_cleared("skill")

    def test_namespace_cleared_exact(self) -> None:
        """The matching namespace still passes."""
        session = self._make_session()
        session.messages = [Message("gui.clear.namespace",
                                    {"namespace": "skill"})]
        session.assert_namespace_cleared("skill")
