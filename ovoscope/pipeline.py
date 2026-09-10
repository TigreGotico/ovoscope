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
"""Pipeline plugin test harness for ovoscope.

Tests intent / pipeline plugins in isolation — no full skill is needed.
The harness loads the specified pipeline stages on a :class:`MiniCroft`
that has a single internal sink skill to absorb matched intents.

Example::

    from ovoscope.pipeline import PipelineHarness

    with PipelineHarness(pipeline=["ovos-adapt-pipeline-plugin.openvoiceos"]) as harness:
        msg = harness.assert_matches("turn on the lights", intent_type="LightsOnIntent")
        harness.assert_no_match("garbled nonsense xyz 123")
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ovos_utils.messagebus import Message


@dataclass
class MatchResult:
    """Discriminated outcome of a single :meth:`PipelineHarness.match_result`.

    Attributes:
        outcome: One of ``"matched"``, ``"no_match"`` or ``"timeout"``.
            ``"no_match"`` means the pipeline explicitly reported an intent
            failure. ``"timeout"`` means nothing came back at all — the
            pipeline gave no verdict, which is a harness problem and must not
            be read as "no match".
        message: The matched :class:`Message`, or ``None``.
    """

    outcome: str
    message: Optional[Message] = None

    @property
    def matched(self) -> bool:
        """True only when the pipeline produced a match."""
        return self.outcome == "matched"

    @property
    def timed_out(self) -> bool:
        """True when the pipeline gave no verdict within the timeout."""
        return self.outcome == "timeout"


class _SinkSkill:
    """Internal catch-all fallback skill so matched intents have somewhere to route.

    This is injected into MiniCroft as ``__ovoscope_sink__`` and registers a
    handler that captures the incoming message so ``PipelineHarness`` can
    return it from :meth:`match`.
    """

    def __init__(self, bus: Optional[Any] = None, skill_id: str = "__ovoscope_sink__") -> None:
        from ovos_utils.fakebus import FakeBus

        self.skill_id = skill_id
        self._last_match: Optional[Message] = None
        self._bus: Any = bus if bus is not None else FakeBus()
        self._bus.on("intent.service.skills.activated", self._handle)
        self._bus.on("intent_failure", self._handle_failure)

    @property
    def bus(self) -> Any:
        return self._bus

    @bus.setter
    def bus(self, new_bus: Any) -> None:
        if new_bus is None:
            raise ValueError("_SinkSkill.bus cannot be None; pass a real bus or omit to default to FakeBus.")
        # Detach handlers from the previous bus before rebinding.
        try:
            self._bus.remove("intent.service.skills.activated", self._handle)
            self._bus.remove("intent_failure", self._handle_failure)
        except Exception:
            pass
        self._bus = new_bus
        new_bus.on("intent.service.skills.activated", self._handle)
        new_bus.on("intent_failure", self._handle_failure)

    def _handle(self, message: Any) -> None:
        """Capture matched intent messages."""
        if isinstance(message, str):
            try:
                message = Message.deserialize(message)
            except Exception:
                return
        self._last_match = message

    def _handle_failure(self, message: Any) -> None:
        """Record explicit intent failures."""
        if isinstance(message, str):
            try:
                message = Message.deserialize(message)
            except Exception:
                return
        # An explicit failure CLEARS the previous match. Without this, a
        # match -> failure -> ... sequence leaves the first match visible on
        # `_last_match`, so anything reading it sees a verdict from an earlier
        # utterance.
        self._last_match = None


class PipelineHarness:
    """Load pipeline plugins and assert utterance matching without skills.

    Args:
        pipeline: List of OPM pipeline stage IDs to load.
        pipeline_config: Per-stage config overrides keyed by stage ID.
        lang: Language tag (default ``"en-US"``).
        modernize: Forwarded to the harness ``MiniCroft`` / ``FakeBus``. When
            on (default), emitting a LEGACY topic also dispatches its ovos.*
            spec counterpart (legacy producer -> spec listener). Utterances are
            injected via ``recognizer_loop:utterance``; bridging lets them also
            drive / be observed on ``ovos.utterance.handle``.
        emit_legacy: Forwarded to the harness. When on (default), emitting an
            ovos.* spec topic also dispatches the legacy one (spec producer ->
            legacy listener). Set BOTH False to exercise a single namespace
            with no cross-namespace bridging.

    Example::

        with PipelineHarness(
            pipeline=["ovos-padatious-pipeline-plugin.openvoiceos"],
            lang="en-US",
        ) as harness:
            msg = harness.assert_matches("what time is it")
            harness.assert_no_match("frobble zorp snork")
    """

    def __init__(
        self,
        pipeline: Optional[List[str]] = None,
        pipeline_config: Optional[Dict[str, Dict[str, Any]]] = None,
        lang: str = "en-US",
        modernize: bool = True,
        emit_legacy: bool = True,
    ) -> None:
        self.pipeline: List[str] = pipeline or []
        self.pipeline_config: Dict[str, Dict[str, Any]] = pipeline_config or {}
        self.lang: str = lang
        self.modernize: bool = modernize
        self.emit_legacy: bool = emit_legacy
        self._mc: Any = None
        self._sink: Any = None

    # ------------------------------------------------------------------
    # Context manager interface
    # ------------------------------------------------------------------

    def __enter__(self) -> "PipelineHarness":
        """Start MiniCroft with the specified pipeline and no skills."""
        from ovoscope import get_minicroft

        # Inject internal sink skill to capture matched intents.
        # Constructed with a default FakeBus; rebound to MiniCroft's real bus below.
        sink_skill = _SinkSkill()

        self._mc = get_minicroft(
            skill_ids=[],
            lang=self.lang,
            default_pipeline=self.pipeline or None,
            extra_skills={"__ovoscope_sink__": sink_skill},
            max_wait=60,
            modernize=self.modernize,
            emit_legacy=self.emit_legacy,
        )

        # MiniCroft patches process-wide globals that only stop() restores, so
        # anything that raises after a successful boot must still shut it down.
        try:
            # Update sink skill's bus reference now that MiniCroft is created
            if self._mc is not None:
                sink_skill.bus = self._mc.bus
            self._sink = sink_skill
        except BaseException:
            self.__exit__(None, None, None)
            raise

        return self

    def __exit__(self, *_: Any) -> None:
        """Shut down MiniCroft."""
        if self._mc is not None:
            self._mc.stop()
            self._mc = None
        self._sink = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def match_result(self, utterance: str, timeout: float = 5.0) -> "MatchResult":
        """Send *utterance* through the pipeline and report what happened.

        Unlike :meth:`match`, this distinguishes the three outcomes a caller
        must tell apart: a match, an explicit intent failure, and a timeout
        (nothing at all came back — usually a broken harness, not a genuine
        "no match").

        Args:
            utterance: Text utterance to send.
            timeout: Seconds to wait for a verdict (default 5.0).

        Returns:
            A :class:`MatchResult`.
        """
        if self._mc is None:
            raise RuntimeError("PipelineHarness must be used as a context manager.")

        # Clear the previous utterance's verdict so a stale match can never be
        # read as this utterance's result.
        if self._sink is not None:
            self._sink._last_match = None

        import threading

        captured: List[Message] = []
        lock = threading.Lock()
        done = threading.Event()
        _failed = threading.Event()

        success_type = "intent.service.skills.activated"
        # NOTE: `mycroft.skill.handler.start` is NOT a failure — it fires on a
        # SUCCESSFUL match, right before the skill handler runs. Treating it as
        # one made every successful match report "no match".
        failure_types = ["intent_failure", "complete_intent_failure"]

        def _on_success(msg: Any) -> None:
            if isinstance(msg, str):
                try:
                    msg = Message.deserialize(msg)
                except Exception:
                    return
            with lock:
                captured.append(msg)
            done.set()

        def _on_failure(msg: Any) -> None:
            _failed.set()
            done.set()

        self._mc.bus.on(success_type, _on_success)
        for et in failure_types:
            self._mc.bus.on(et, _on_failure)

        try:
            src = Message(
                "recognizer_loop:utterance",
                data={"utterances": [utterance], "lang": self.lang},
            )
            self._mc.bus.emit(src)
            # Wait directly on the shared event — no watcher thread. The old
            # watcher polled at 20Hz forever after a timeout, because the
            # handlers were already removed so its events could never be set.
            completed = done.wait(timeout=timeout)
        finally:
            self._mc.bus.remove(success_type, _on_success)
            for et in failure_types:
                self._mc.bus.remove(et, _on_failure)

        with lock:
            got = captured[0] if captured else None
        if got is not None:
            # A real match wins over a concurrent failure signal.
            return MatchResult(outcome="matched", message=got)
        if _failed.is_set():
            return MatchResult(outcome="no_match", message=None)
        if not completed:
            return MatchResult(outcome="timeout", message=None)
        return MatchResult(outcome="no_match", message=None)

    def match(self, utterance: str, timeout: float = 5.0) -> Optional[Message]:
        """Send *utterance* through the pipeline and return the matched message.

        Args:
            utterance: Text utterance to send.
            timeout: Seconds to wait for a match (default 5.0).

        Returns:
            The ``recognizer_loop:utterance`` response message if a match occurs,
            otherwise ``None``.
        """
        return self.match_result(utterance, timeout=timeout).message

    def assert_matches(
        self,
        utterance: str,
        intent_type: Optional[str] = None,
        timeout: float = 5.0,
    ) -> Message:
        """Assert that *utterance* is matched by the pipeline.

        Args:
            utterance: Text utterance to test.
            intent_type: If provided, the matched intent ``type`` must contain
                this substring.
            timeout: Seconds to wait for a match.

        Returns:
            The matched :class:`Message`.

        Raises:
            AssertionError: If no match is found or the intent type is wrong.
        """
        result = self.match_result(utterance, timeout=timeout)
        assert result.outcome != "timeout", (
            f"Pipeline gave no verdict for utterance {utterance!r} within "
            f"{timeout}s — neither a match nor an intent failure was emitted."
        )
        msg = result.message
        assert msg is not None, (
            f"Expected utterance {utterance!r} to be matched by the pipeline, but no match occurred."
        )
        if intent_type is not None:
            assert intent_type in (msg.msg_type or ""), (
                f"Expected intent type to contain {intent_type!r}, got {msg.msg_type!r}"
            )
        return msg

    def assert_no_match(self, utterance: str, timeout: float = 2.0) -> None:
        """Assert that *utterance* is NOT matched by the pipeline.

        Args:
            utterance: Text utterance that should produce no match.
            timeout: Seconds to observe before asserting absence (default 2.0).

        Raises:
            AssertionError: If a match is unexpectedly found, or if the
                pipeline gave no verdict at all within *timeout* (silence is
                a broken harness, not proof of absence).
        """
        result = self.match_result(utterance, timeout=timeout)
        if result.outcome == "timeout":
            raise AssertionError(
                f"Pipeline gave no verdict for utterance {utterance!r} within "
                f"{timeout}s — no match AND no intent failure was emitted. "
                f"Absence of a match cannot be asserted from silence; check "
                f"that the harness is wired and the pipeline is loaded."
            )
        msg = result.message
        if msg is not None and msg.msg_type != "intent_failure":
            raise AssertionError(
                f"Utterance {utterance!r} was unexpectedly matched: {msg.msg_type!r}"
            )
