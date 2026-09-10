"""Unit tests for ovoscope.pytest_plugin — the ``minicroft`` fixture.

Since pytest fixtures can't be called directly, we test the underlying
logic by importing the module and inspecting/mocking its internals.
"""
import unittest
from unittest.mock import MagicMock, patch

import ovoscope.pytest_plugin as plugin_mod


class TestMinicroftFixtureLogic(unittest.TestCase):
    """Tests for the fixture's skill_ids extraction and lifecycle."""

    def test_module_has_minicroft_fixture(self):
        """The module exposes a 'minicroft' callable."""
        self.assertTrue(hasattr(plugin_mod, "minicroft"))
        self.assertTrue(callable(plugin_mod.minicroft))

    @patch.object(plugin_mod, "get_minicroft")
    def test_skill_ids_read_from_class(self, mock_get):
        """The fixture function reads skill_ids from request.cls."""
        mock_mc = MagicMock()
        mock_get.return_value = mock_mc

        request = MagicMock()
        request.cls = type("FakeTest", (), {"skill_ids": ["skill-a.test"]})

        # Call the underlying generator function directly (bypassing pytest's
        # fixture decorator which blocks direct calls in newer pytest versions)
        gen = plugin_mod.minicroft.__wrapped__(request)
        mc = next(gen)

        mock_get.assert_called_once_with(["skill-a.test"])
        self.assertIs(mc, mock_mc)

        try:
            next(gen)
        except StopIteration:
            pass
        mock_mc.stop.assert_called_once()

    @patch.object(plugin_mod, "get_minicroft")
    def test_string_skill_ids_normalized(self, mock_get):
        """A single string skill_ids is wrapped into a list."""
        mock_mc = MagicMock()
        mock_get.return_value = mock_mc

        request = MagicMock()
        request.cls = type("FakeTest", (), {"skill_ids": "single.test"})

        gen = plugin_mod.minicroft.__wrapped__(request)
        next(gen)
        mock_get.assert_called_once_with(["single.test"])

        try:
            next(gen)
        except StopIteration:
            pass

    @patch.object(plugin_mod, "get_minicroft")
    def test_missing_skill_ids_defaults_empty(self, mock_get):
        """If the test class has no skill_ids, default to []."""
        mock_mc = MagicMock()
        mock_get.return_value = mock_mc

        request = MagicMock()
        request.cls = type("FakeTest", (), {})

        gen = plugin_mod.minicroft.__wrapped__(request)
        next(gen)
        mock_get.assert_called_once_with([])

        try:
            next(gen)
        except StopIteration:
            pass

    @patch.object(plugin_mod, "get_minicroft")
    def test_stop_called_on_exception(self, mock_get):
        """mc.stop() is called even if the test body raises."""
        mock_mc = MagicMock()
        mock_get.return_value = mock_mc

        request = MagicMock()
        request.cls = type("FakeTest", (), {"skill_ids": []})

        gen = plugin_mod.minicroft.__wrapped__(request)
        next(gen)

        try:
            gen.throw(RuntimeError("test failure"))
        except RuntimeError:
            pass
        mock_mc.stop.assert_called_once()

    @patch.object(plugin_mod, "get_minicroft", side_effect=TimeoutError("boom"))
    def test_get_minicroft_failure_no_name_error(self, mock_get):
        """If get_minicroft raises, teardown must not raise NameError."""
        request = MagicMock()
        request.cls = type("FakeTest", (), {"skill_ids": []})

        gen = plugin_mod.minicroft.__wrapped__(request)
        with self.assertRaises(TimeoutError):
            next(gen)


if __name__ == "__main__":
    unittest.main()


class TestModuleLevelSkipDoesNotAbortCollection:
    """Regression test: a module-level ``pytest.importorskip()``/``pytest.skip()``
    used to abort the *entire* collection session instead of skipping just
    that one module.

    ``pytest.skip.Exception`` subclasses ``BaseException`` (via
    ``_pytest.outcomes.OutcomeException``), not ``Exception``. The ovoscope
    ``pytest_pycollect_makemodule`` hook wrapper imports every collected
    module eagerly (to look for the ``ovoscope_intent_cases`` shim marker)
    guarded only by ``except Exception``. That guard never sees the skip
    exception, so it escapes the hook wrapper uncaught and pytest reports
    "found no collectors" / exit code 5 for the whole run, even though only
    one of the test files actually wanted to be skipped.
    """

    def test_importorskip_module_does_not_nuke_collection(self, pytester):
        pytester.makepyfile(
            test_skips_at_import="""
            import pytest
            pytest.importorskip("this_module_does_not_exist_xyz")

            def test_never_runs():
                assert False
            """
        )
        pytester.makepyfile(
            test_plain="""
            def test_ok():
                assert True
            """
        )

        result = pytester.runpytest()

        result.assert_outcomes(passed=1, skipped=1)
        assert result.ret == 0
