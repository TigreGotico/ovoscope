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
"""Fixture diffing utilities for ovoscope.

Compares two serialized End2EndTest fixture JSON files and reports
type mismatches, data diffs, missing messages, and extra messages.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


class _Missing:
    """Sentinel for "key absent", distinct from a stored ``None``."""

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "<missing>"


_MISSING = _Missing()


@dataclass
class MessageDiff:
    """Diff result for a single message pair at a given index.

    Attributes:
        index: Position in the message sequence (0-based).
        expected_type: Message type from the expected fixture, or None if extra.
        actual_type: Message type from the actual fixture, or None if missing.
        data_diffs: Mapping of key → (expected_value, actual_value) for mismatched data fields.
        context_diffs: Mapping of key → (expected_value, actual_value) for mismatched context fields.
        status: One of "match", "type_mismatch", "data_mismatch", "missing", "extra".
    """

    index: int
    expected_type: Optional[str]
    actual_type: Optional[str]
    data_diffs: Dict[str, Tuple[Any, Any]] = field(default_factory=dict)
    context_diffs: Dict[str, Tuple[Any, Any]] = field(default_factory=dict)
    status: str = "match"  # match | type_mismatch | data_mismatch | missing | extra


@dataclass
class FixtureDiffResult:
    """Result of comparing two fixture files.

    Attributes:
        diffs: Per-message diff entries.
        is_identical: True only if every message matches exactly.
        expected_path: Path to the expected fixture file.
        actual_path: Path to the actual fixture file.
    """

    diffs: List[MessageDiff] = field(default_factory=list)
    is_identical: bool = True
    expected_path: str = ""
    actual_path: str = ""

    def print_report(self, color: bool = True) -> None:
        """Print a human-readable diff report to stdout.

        Args:
            color: Whether to use ANSI color codes in output.
        """
        _GREEN = "\033[92m" if color else ""
        _RED = "\033[91m" if color else ""
        _YELLOW = "\033[93m" if color else ""
        _CYAN = "\033[96m" if color else ""
        _RESET = "\033[0m" if color else ""

        print(f"\n{_CYAN}=== Fixture Diff Report ==={_RESET}")
        print(f"  Expected : {self.expected_path}")
        print(f"  Actual   : {self.actual_path}")
        print(f"  Identical: {_GREEN if self.is_identical else _RED}{self.is_identical}{_RESET}\n")

        for d in self.diffs:
            if d.status == "match":
                print(f"  [{_GREEN}OK{_RESET}] [{d.index}] {d.expected_type}")
            elif d.status == "missing":
                print(f"  [{_RED}MISS{_RESET}] [{d.index}] expected={d.expected_type} but got nothing")
            elif d.status == "extra":
                print(f"  [{_YELLOW}EXTRA{_RESET}] [{d.index}] unexpected message: {d.actual_type}")
            elif d.status == "type_mismatch":
                print(f"  [{_RED}TYPE{_RESET}] [{d.index}] expected={d.expected_type} actual={d.actual_type}")
            elif d.status == "data_mismatch":
                print(f"  [{_YELLOW}DATA{_RESET}] [{d.index}] {d.expected_type}")
                for k, (exp, act) in d.data_diffs.items():
                    print(f"    data[{k!r}]: expected={exp!r}  actual={act!r}")
                for k, (exp, act) in d.context_diffs.items():
                    print(f"    ctx[{k!r}]: expected={exp!r}  actual={act!r}")

    def to_json(self) -> Dict[str, Any]:
        """Serialise the diff result to a JSON-compatible dict.

        Returns:
            Dictionary with keys ``is_identical``, ``expected_path``,
            ``actual_path``, and ``diffs``.
        """
        return {
            "is_identical": self.is_identical,
            "expected_path": self.expected_path,
            "actual_path": self.actual_path,
            "diffs": [
                {
                    "index": d.index,
                    "expected_type": d.expected_type,
                    "actual_type": d.actual_type,
                    "data_diffs": {k: list(v) for k, v in d.data_diffs.items()},
                    "context_diffs": {k: list(v) for k, v in d.context_diffs.items()},
                    "status": d.status,
                }
                for d in self.diffs
            ],
        }


def _dict_diff(
    expected: Dict[str, Any],
    actual: Dict[str, Any],
    strict: bool = False,
) -> Dict[str, Tuple[Any, Any]]:
    """Return keys whose values differ between *expected* and *actual*.

    By default only keys present in *expected* are checked (subset comparison).
    When *strict* is ``True``, keys present in *actual* but absent from
    *expected* are also flagged as unexpected extras.

    Args:
        expected: Reference dict.
        actual: Dict to compare against.
        strict: When ``True``, flag extra keys in *actual* not in *expected*.
            Default ``False`` preserves the original subset-comparison behaviour.

    Returns:
        Mapping of key → (expected_value, actual_value) for differing keys.
        For extra keys (strict mode only) the expected_value is ``None``.
    """
    diffs: Dict[str, Tuple[Any, Any]] = {}
    for k, exp_v in expected.items():
        # _MISSING, not None: an expected value of None must still differ from
        # an ABSENT key, otherwise `{"a": None}` vs `{}` compares equal and the
        # diff reports a match that is not there.
        act_v = actual.get(k, _MISSING)
        if act_v != exp_v:
            diffs[k] = (exp_v, None if act_v is _MISSING else act_v)
    if strict:
        for k, act_v in actual.items():
            if k not in expected:
                diffs[k] = (None, act_v)
    return diffs


def _load_messages(path: str) -> List[Dict[str, Any]]:
    """Load the ``expected_messages`` list from a serialised fixture file.

    Args:
        path: Path to a JSON fixture file produced by ``End2EndTest.save()``.

    Returns:
        List of message dicts, each with at least ``type``, ``data``, and ``context``.

    Raises:
        FileNotFoundError: If *path* does not exist.
        ValueError: If the file is not an ovoscope fixture (no
            ``expected_messages`` key). Defaulting to an empty list here made
            two unrelated JSON files compare as "Identical" and exit 0.
    """
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict) or "expected_messages" not in payload:
        raise ValueError(
            f"not an ovoscope fixture: {path} has no 'expected_messages' key. "
            f"Fixture files are produced by End2EndTest.save()."
        )
    return payload["expected_messages"]


def diff_fixtures(
    expected_path: str,
    actual_path: str,
    *,
    ignore_context: bool = True,
    strict: bool = False,
) -> FixtureDiffResult:
    """Compare two fixture JSON files and return a structured diff.

    Algorithm:
    1. Load ``expected_messages`` from both files.
    2. Align by index (messages must match in order).
    3. For each pair: compare type, then data, then (optionally) context.
    4. Flag trailing messages in the longer list as missing/extra.

    Args:
        expected_path: Path to the reference fixture file.
        actual_path: Path to the fixture file being validated.
        ignore_context: Skip context comparison (default True).
        strict: When ``True``, flag extra keys in *actual* data/context dicts
            not present in *expected*.  Default ``False`` (subset comparison).

    Returns:
        A :class:`FixtureDiffResult` describing all differences found.
    """
    expected_msgs = _load_messages(expected_path)
    actual_msgs = _load_messages(actual_path)

    result = FixtureDiffResult(
        expected_path=expected_path,
        actual_path=actual_path,
        is_identical=True,
    )

    max_len = max(len(expected_msgs), len(actual_msgs))

    for i in range(max_len):
        if i >= len(expected_msgs):
            diff = MessageDiff(
                index=i,
                expected_type=None,
                actual_type=actual_msgs[i].get("type"),
                status="extra",
            )
            result.diffs.append(diff)
            result.is_identical = False
            continue

        if i >= len(actual_msgs):
            diff = MessageDiff(
                index=i,
                expected_type=expected_msgs[i].get("type"),
                actual_type=None,
                status="missing",
            )
            result.diffs.append(diff)
            result.is_identical = False
            continue

        exp_msg = expected_msgs[i]
        act_msg = actual_msgs[i]
        exp_type = exp_msg.get("type", "")
        act_type = act_msg.get("type", "")

        if exp_type != act_type:
            diff = MessageDiff(
                index=i,
                expected_type=exp_type,
                actual_type=act_type,
                status="type_mismatch",
            )
            result.diffs.append(diff)
            result.is_identical = False
            continue

        # Same type — compare data and context
        data_diffs = _dict_diff(exp_msg.get("data", {}), act_msg.get("data", {}), strict=strict)
        ctx_diffs: Dict[str, Tuple[Any, Any]] = {}
        if not ignore_context:
            ctx_diffs = _dict_diff(exp_msg.get("context", {}), act_msg.get("context", {}), strict=strict)

        if data_diffs or ctx_diffs:
            diff = MessageDiff(
                index=i,
                expected_type=exp_type,
                actual_type=act_type,
                data_diffs=data_diffs,
                context_diffs=ctx_diffs,
                status="data_mismatch",
            )
            result.diffs.append(diff)
            result.is_identical = False
        else:
            result.diffs.append(
                MessageDiff(index=i, expected_type=exp_type, actual_type=act_type, status="match")
            )

    return result
