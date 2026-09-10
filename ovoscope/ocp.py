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
"""OCP (OpenVoiceOS Common Play) test harness for ovoscope.

OCP skills respond to ``ovos.common_play.query`` with a
``ovos.common_play.query.response`` message containing a list of
:class:`MediaEntry` candidates.  This harness drives that flow in-process
using a :class:`MiniCroft` instance and optional HTTP mocking.

OCP message flow::

    recognizer_loop:utterance
      → ovos.common_play.query                  (broadcast to OCP skills)
      → ovos.common_play.query.response         (skill replies with MediaEntry list)
      → ovos.common_play.start                  (selected track)

Example::

    from ovoscope.ocp import OCPTest

    result = OCPTest(
        skill_ids=["ovos-skill-youtube.openvoiceos"],
        utterance="play lofi hip hop",
        mock_responses={"youtube.com": {"items": [...]}},
        expected_media=[{"title": "Lofi Hip Hop Radio"}],
    ).execute()
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

from ovos_utils.fakebus import FakeBus
from ovos_utils.messagebus import Message


@dataclass
class OCPTest:
    """Declarative OCP skill test.

    Fields:
        skill_ids: OPM entry-point IDs of the OCP skills under test.
        utterance: The user utterance to fire.
        mock_responses: URL-pattern → response body mapping for HTTP mocking.
            Keys are matched as substrings against the request URL.
        expected_media: List of partial dicts that must appear in the
            ``ovos.common_play.query.response`` ``media_list`` field.
            Subset-matching is used: only the keys present in each dict
            are checked against the corresponding item.
        expected_stream_url: Substring that must appear in the ``uri``
            field of the ``ovos.common_play.start`` message.
        lang: Language tag (default ``"en-US"``).
        timeout: Maximum seconds to wait for responses (default 20.0).
        patch_targets: Additional ``requests``-like module paths to patch
            (e.g. ``["my_skill.http_client.requests"]``).  The default
            target is ``"requests.Session.get"``.
        modernize: Forwarded to the harness ``MiniCroft`` / ``FakeBus``. When
            on (default), emitting a LEGACY topic also dispatches its ovos.*
            spec counterpart (legacy producer -> spec listener). The OCP flow
            is driven by ``recognizer_loop:utterance``; bridging lets it be
            observed on / driven from ``ovos.utterance.handle`` too.
        emit_legacy: Forwarded to the harness. When on (default), emitting an
            ovos.* spec topic also dispatches the legacy one (spec producer ->
            legacy listener). Set BOTH False to exercise a single namespace
            with no cross-namespace bridging.

    Example::

        result = OCPTest(
            skill_ids=["ovos-skill-bandcamp.openvoiceos"],
            utterance="play some jazz",
            mock_responses={"bandcamp.com": {"tracks": [{"title": "Blue Note", "url": "..."}]}},
            expected_media=[{"title": "Blue Note"}],
        ).execute()
    """

    skill_ids: List[str]
    utterance: str
    mock_responses: Dict[str, Any] = field(default_factory=dict)
    expected_media: List[Dict[str, Any]] = field(default_factory=list)
    expected_stream_url: Optional[str] = None
    lang: str = "en-US"
    timeout: float = 20.0
    patch_targets: List[str] = field(default_factory=list)
    modernize: bool = True
    emit_legacy: bool = True

    def execute(self) -> List[Message]:
        """Run the OCP test with optional HTTP mocking.

        Returns:
            All messages captured during the test run.

        Raises:
            AssertionError: If expected media or stream URL assertions fail.
        """
        from ovoscope import get_minicroft  # avoid circular at module level

        captured: List[Message] = []
        got_response = threading.Event()

        mc = get_minicroft(self.skill_ids, lang=self.lang, max_wait=60,
                           modernize=self.modernize,
                           emit_legacy=self.emit_legacy)

        def _capture(m: Any) -> None:
            captured.append(Message.deserialize(m) if isinstance(m, str) else m)

        def _on_query_response(_m: Any) -> None:
            got_response.set()

        mc.bus.on("message", _capture)
        mc.bus.on("ovos.common_play.query.response", _on_query_response)

        src_msg = Message(
            "recognizer_loop:utterance",
            data={"utterances": [self.utterance], "lang": self.lang},
        )

        # MiniCroft.stop() restores process-wide globals — it must run even if
        # emit or a patch raises.
        try:
            patches = self._build_patches()
            with _apply_patches(patches):
                mc.bus.emit(src_msg)
                # Wait for the OCP skills to answer instead of sleeping a fixed
                # fraction of the timeout: fast skills no longer pay the full
                # wait, slow ones are no longer cut off early.
                if got_response.wait(self.timeout):
                    # Several OCP skills answer the same query; give the
                    # stragglers a short grace period after the first reply.
                    time.sleep(min(0.5, self.timeout * 0.1))
        finally:
            mc.bus.remove("ovos.common_play.query.response", _on_query_response)
            mc.bus.remove("message", _capture)
            mc.stop()

        assert_ocp_query_response(
            captured,
            expected_media=self.expected_media,
            expected_stream_url=self.expected_stream_url,
        )
        return captured

    def _build_patches(self) -> List[Any]:
        """Build a list of ``unittest.mock.patch`` context managers for HTTP mocking.

        Returns:
            List of patch context managers ready for use in ``_apply_patches``.
        """
        patches = []
        if not self.mock_responses:
            return patches

        targets = list(self.patch_targets) + ["requests.Session.get", "requests.get"]
        for target in targets:
            # The side effect must live on the patched GET — that is what
            # receives the URL. Attaching it to the response mock could never
            # see a URL, so no configured body ever matched and json() always
            # returned {}.
            patches.append(patch(target,
                                 side_effect=_build_get_side_effect(
                                     self.mock_responses)))
        return patches


def _build_get_side_effect(mock_responses: Dict[str, Any]):
    """Build a side effect for a patched ``requests`` GET.

    The returned callable receives the request URL (the first positional
    argument of ``requests.get`` / ``requests.Session.get``) and answers with a
    response mock whose ``json()`` returns the body configured for the first
    *mock_responses* key found in that URL.

    Args:
        mock_responses: URL-substring → response body mapping.

    Returns:
        A callable suitable for ``patch(..., side_effect=...)``.
    """

    def _get(*args: Any, **kwargs: Any) -> MagicMock:
        url = ""
        for candidate in args:
            if isinstance(candidate, str):
                url = candidate
                break
        else:
            url = str(kwargs.get("url", ""))

        body: Any = {}
        for key, value in mock_responses.items():
            if key in url:
                body = value
                break

        response = MagicMock()
        response.url = url
        response.json.return_value = body
        response.text = json.dumps(body) if isinstance(body, (dict, list)) else str(body)
        response.status_code = 200
        response.ok = True
        return response

    return _get


def _build_mock_response(mock_responses: Dict[str, Any]) -> MagicMock:
    """Create a mock ``requests.Response`` that returns configured JSON bodies.

    When the mock is called with a URL, it checks if any key from
    *mock_responses* appears as a substring of the URL and returns the
    corresponding value.  Falls back to an empty dict.

    Args:
        mock_responses: URL-substring → response body mapping.

    Returns:
        A :class:`unittest.mock.MagicMock` mimicking ``requests.Response``.
    """
    mock = MagicMock()

    def json_side_effect(*_args: Any, **_kwargs: Any) -> Any:
        # Match URL against mock_responses keys (as substrings)
        url = str(mock.url) if hasattr(mock, 'url') else ""
        for key, value in mock_responses.items():
            if key in url:
                return value
        return {}

    mock.json.side_effect = json_side_effect
    mock.status_code = 200
    mock.ok = True
    return mock


class _apply_patches:
    """Context manager that enters all provided patches simultaneously.

    Args:
        patches: List of :func:`unittest.mock.patch` context managers.
    """

    def __init__(self, patches: List[Any]) -> None:
        self._patches = patches
        self._mocks: List[Any] = []

    def __enter__(self) -> "_apply_patches":
        for p in self._patches:
            self._mocks.append(p.__enter__())
        return self

    def __exit__(self, *args: Any) -> None:
        for p in reversed(self._patches):
            p.__exit__(*args)


def assert_ocp_query_response(
    messages: List[Message],
    *,
    min_results: int = 0,
    media_type: Optional[str] = None,
    expected_media: Optional[List[Dict[str, Any]]] = None,
    stream_url_contains: Optional[str] = None,
    expected_stream_url: Optional[str] = None,
) -> None:
    """Assert properties of captured OCP messages.

    Args:
        messages: Messages captured during an OCP test run.
        min_results: Minimum number of ``media_list`` entries expected.
        media_type: If provided, all ``media_list`` items must have this type.
        expected_media: List of partial dicts; each must match at least one
            item in the ``media_list`` (subset matching).
        stream_url_contains: Substring that must appear in the
            ``ovos.common_play.start`` message URI.
        expected_stream_url: Alias for *stream_url_contains*.

    Raises:
        AssertionError: If any assertion fails.
    """
    stream_check = stream_url_contains or expected_stream_url

    query_responses = [m for m in messages if m.msg_type == "ovos.common_play.query.response"]

    if min_results > 0 or expected_media:
        all_items: List[Dict[str, Any]] = []
        for resp in query_responses:
            all_items.extend(resp.data.get("media_list", []) or resp.data.get("results", []))

        if min_results > 0:
            assert len(all_items) >= min_results, (
                f"Expected at least {min_results} OCP results, got {len(all_items)}"
            )

        if expected_media:
            for expected in expected_media:
                match = any(
                    all(item.get(k) == v for k, v in expected.items())
                    for item in all_items
                )
                assert match, (
                    f"Expected media item {expected!r} not found in OCP results.\n"
                    f"Got: {all_items[:5]}..."
                )

        if media_type:
            wrong = [i for i in all_items if i.get("media_type") != media_type]
            assert not wrong, f"Found items with unexpected media_type: {wrong[:3]}"

    if stream_check:
        start_msgs = [m for m in messages if m.msg_type == "ovos.common_play.start"]
        assert start_msgs, "No 'ovos.common_play.start' message was captured."
        uri = start_msgs[0].data.get("uri", "") or start_msgs[0].data.get("url", "")
        assert stream_check in uri, (
            f"Expected stream URL to contain {stream_check!r}, got {uri!r}"
        )
