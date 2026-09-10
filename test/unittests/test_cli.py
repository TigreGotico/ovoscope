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
"""Unit tests for ovoscope.cli."""

import json
import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from ovoscope.cli import (
    _build_parser,
    cmd_validate,
    cmd_diff,
    cmd_coverage,
    _basic_validate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_fixture(path: str, msgs: list) -> None:
    with open(path, "w") as f:
        json.dump({
            "source_message": {"type": "recognizer_loop:utterance", "data": {}, "context": {}},
            "expected_messages": msgs,
        }, f)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


class TestBuildParser:
    def test_record_subcommand(self):
        parser = _build_parser()
        args = parser.parse_args([
            "record", "--utterance", "hello", "--output", "out.json"
        ])
        assert args.command == "record"
        assert args.utterance == "hello"
        assert args.output == "out.json"
        assert args.live is False

    def test_record_live_flag(self):
        parser = _build_parser()
        args = parser.parse_args([
            "record", "--live", "--utterance", "hi", "--output", "out.json"
        ])
        assert args.live is True

    def test_run_subcommand(self):
        parser = _build_parser()
        args = parser.parse_args(["run", "fixture.json"])
        assert args.command == "run"
        assert args.fixture == "fixture.json"
        assert args.verbose is False

    def test_diff_subcommand(self):
        parser = _build_parser()
        args = parser.parse_args(["diff", "a.json", "b.json"])
        assert args.command == "diff"
        assert args.expected == "a.json"
        assert args.actual == "b.json"

    def test_validate_subcommand(self):
        parser = _build_parser()
        args = parser.parse_args(["validate", "a.json", "b.json"])
        assert args.command == "validate"
        assert args.fixtures == ["a.json", "b.json"]

    def test_coverage_subcommand(self):
        parser = _build_parser()
        args = parser.parse_args(["coverage", "/some/path"])
        assert args.command == "coverage"
        assert args.workspace == "/some/path"
        assert args.format == "table"

    def test_no_subcommand_exits(self):
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])


# ---------------------------------------------------------------------------
# _basic_validate
# ---------------------------------------------------------------------------


class TestBasicValidate:
    def test_valid_fixture_passes(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({
                "source_message": {"type": "x", "data": {}, "context": {}},
                "expected_messages": [],
            }, f)
            path = f.name
        try:
            _basic_validate(path)  # should not raise
        finally:
            os.unlink(path)

    def test_missing_key_raises(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"source_message": {}}, f)
            path = f.name
        try:
            with pytest.raises(ValueError, match="expected_messages"):
                _basic_validate(path)
        finally:
            os.unlink(path)

    def test_wrong_type_raises(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"source_message": {}, "expected_messages": "not-a-list"}, f)
            path = f.name
        try:
            with pytest.raises(ValueError, match="list"):
                _basic_validate(path)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# cmd_validate
# ---------------------------------------------------------------------------


class TestCmdValidate:
    def test_valid_fixture_returns_0(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({
                "source_message": [{"type": "x", "data": {}, "context": {}}],
                "expected_messages": [],
            }, f)
            path = f.name
        try:
            parser = _build_parser()
            args = parser.parse_args(["validate", path])
            code = cmd_validate(args)
            assert code == 0
        finally:
            os.unlink(path)

    def test_invalid_fixture_returns_1(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"only_source": {}}, f)
            path = f.name
        try:
            parser = _build_parser()
            args = parser.parse_args(["validate", path])
            code = cmd_validate(args)
            assert code == 1
        finally:
            os.unlink(path)

    def test_uses_validate_fixture_when_pydantic_available(self):
        """cmd_validate layers pydantic_helpers.validate_fixture on top of the
        structural checks when the pydantic extra is importable."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({
                "source_message": [{"type": "x", "data": {}, "context": {}}],
                "expected_messages": [],
            }, f)
            path = f.name
        try:
            parser = _build_parser()
            args = parser.parse_args(["validate", path])
            mock_validate_fixture = MagicMock(return_value={})
            with patch("ovoscope.pydantic_helpers._PYDANTIC_AVAILABLE", True), \
                 patch("ovoscope.pydantic_helpers.validate_fixture", mock_validate_fixture), \
                 patch("ovoscope.cli._basic_validate") as mock_basic:
                code = cmd_validate(args)
            assert code == 0
            mock_validate_fixture.assert_called_once_with(path)
            mock_basic.assert_called_once_with(path)
        finally:
            os.unlink(path)

    def test_falls_back_to_basic_validate_without_pydantic(self):
        """cmd_validate must use _basic_validate when ovoscope.pydantic_helpers
        (or ovos-pydantic-models underneath it) is not importable."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({
                "source_message": {"type": "x", "data": {}, "context": {}},
                "expected_messages": [],
            }, f)
            path = f.name
        try:
            parser = _build_parser()
            args = parser.parse_args(["validate", path])
            with patch.dict(sys.modules, {"ovoscope.pydantic_helpers": None}):
                code = cmd_validate(args)
            assert code == 0
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# cmd_diff
# ---------------------------------------------------------------------------


class TestCmdDiff:
    def test_identical_fixtures_returns_0(self):
        msgs = [{"type": "speak", "data": {}, "context": {}}]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f1:
            _write_fixture(f1.name, msgs)
            p1 = f1.name
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f2:
            _write_fixture(f2.name, msgs)
            p2 = f2.name
        try:
            parser = _build_parser()
            args = parser.parse_args(["diff", p1, p2, "--no-color"])
            code = cmd_diff(args)
            assert code == 0
        finally:
            os.unlink(p1)
            os.unlink(p2)

    def test_different_fixtures_returns_1(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f1:
            _write_fixture(f1.name, [{"type": "speak", "data": {}, "context": {}}])
            p1 = f1.name
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f2:
            _write_fixture(f2.name, [{"type": "stop", "data": {}, "context": {}}])
            p2 = f2.name
        try:
            parser = _build_parser()
            args = parser.parse_args(["diff", p1, p2, "--no-color"])
            code = cmd_diff(args)
            assert code == 1
        finally:
            os.unlink(p1)
            os.unlink(p2)


# ---------------------------------------------------------------------------
# cmd_coverage
# ---------------------------------------------------------------------------


class TestCmdCoverage:
    def test_coverage_table_returns_0(self, tmp_path):
        parser = _build_parser()
        args = parser.parse_args(["coverage", str(tmp_path)])
        code = cmd_coverage(args)
        assert code == 0

    def test_coverage_json_returns_0(self, tmp_path):
        parser = _build_parser()
        args = parser.parse_args(["coverage", str(tmp_path), "--format", "json"])
        code = cmd_coverage(args)
        assert code == 0
