"""ovoscope.pydantic_helpers — optional bridge between ovos-bus-client and ovos-pydantic-models.

This module is only usable when the optional ``pydantic`` extras are installed::

    pip install ovoscope[pydantic]

Three public utilities are provided:

* :func:`to_bus_message` — convert a typed pydantic model to a raw
  :class:`ovos_bus_client.message.Message` for use with :class:`~ovoscope.End2EndTest`.
* :func:`from_bus_message` — parse a captured :class:`Message` into a typed pydantic
  model for richer post-test assertions.
* :func:`validate_fixture` — load and validate a JSON fixture file produced by
  :meth:`~ovoscope.End2EndTest.save` before handing it to
  :meth:`~ovoscope.End2EndTest.deserialize`, surfacing malformed messages as clear
  :class:`ValueError` rather than cryptic :class:`KeyError` or :class:`TypeError`.

Example::

    from ovoscope.pydantic_helpers import to_bus_message, from_bus_message, validate_fixture
    from ovoscope import End2EndTest
    from ovos_pydantic_models import RecognizerLoopUtteranceMessage, RecognizerLoopUtteranceData

    # Build a typed source message — validated at construction time
    utterance = to_bus_message(RecognizerLoopUtteranceMessage(
        data=RecognizerLoopUtteranceData(utterances=["hello"], lang="en-us")
    ))

    # Load and validate a saved fixture
    test = End2EndTest.deserialize(validate_fixture("test/fixtures/hello_world.json"))
    test.execute()
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Type, Union

try:
    from ovos_pydantic_models.message import OpenVoiceOSMessage
    from pydantic import ValidationError
    _PYDANTIC_AVAILABLE = True
except ImportError:
    _PYDANTIC_AVAILABLE = False

from ovos_bus_client.message import Message

if TYPE_CHECKING:
    # Only imported at type-check time so mypy/pyright see full types
    # without requiring ovos-pydantic-models at runtime.
    from ovos_pydantic_models.message import OpenVoiceOSMessage  # noqa: F811
    from ovoscope import SerializedTest


def _require_pydantic() -> None:
    """Raise :class:`ImportError` if ovos-pydantic-models is not installed."""
    if not _PYDANTIC_AVAILABLE:
        raise ImportError(
            "ovos-pydantic-models is required for this feature. "
            "Install it with: pip install ovoscope[pydantic]"
        )


def to_bus_message(pydantic_msg: "OpenVoiceOSMessage") -> Message:
    """Convert a typed pydantic model to an :class:`ovos_bus_client.message.Message`.

    The returned :class:`Message` is suitable for use as ``source_message`` or
    an entry in ``expected_messages`` in :class:`~ovoscope.End2EndTest`.

    Args:
        pydantic_msg: Any :class:`ovos_pydantic_models.message.OpenVoiceOSMessage`
            subclass instance.

    Returns:
        A :class:`~ovos_bus_client.message.Message` with ``msg_type``, ``data``,
        and ``context`` populated from the model.

    Raises:
        ImportError: If ``ovos-pydantic-models`` is not installed.

    Example::

        from ovoscope.pydantic_helpers import to_bus_message
        from ovos_pydantic_models import SpeakMessage, SpeakData

        bus_msg = to_bus_message(SpeakMessage(data=SpeakData(utterance="Hello!")))
        # Accepts both the legacy "speak" and canonical "ovos.utterance.speak"
        # spellings (producers emit canonical since workshop#425; captured
        # streams from pre-spec producer vintages can still carry the legacy
        # name).
        assert bus_msg.msg_type in {"speak", "ovos.utterance.speak"}
        assert bus_msg.data["utterance"] == "Hello!"
    """
    _require_pydantic()
    d: Dict[str, Any] = pydantic_msg.model_dump()
    return Message(
        d["message_type"],
        d.get("data") or {},
        d.get("context") or {},
    )


def from_bus_message(
    bus_msg: Message,
    model: "Type[OpenVoiceOSMessage]",
) -> "OpenVoiceOSMessage":
    """Parse a received :class:`Message` into a typed pydantic model.

    Useful for asserting rich constraints on captured messages after
    :meth:`~ovoscope.End2EndTest.execute` — e.g. checking that a ``speak``
    message has a specific ``lang`` or ``expect_response`` value.

    Raises :class:`pydantic.ValidationError` if the message does not conform
    to ``model``'s schema, which catches malformed skill messages early.

    Args:
        bus_msg: A captured bus message, e.g. an element of the list returned
            by :meth:`~ovoscope.End2EndTest.execute`.
        model: The :class:`~ovos_pydantic_models.message.OpenVoiceOSMessage`
            subclass to parse into (e.g. ``SpeakMessage``).

    Returns:
        The validated ``model`` instance.

    Raises:
        pydantic.ValidationError: If ``bus_msg`` does not match ``model``'s schema.
        ImportError: If ``ovos-pydantic-models`` is not installed.

    Example::

        from ovoscope.pydantic_helpers import from_bus_message
        from ovos_pydantic_models import SpeakMessage

        messages = test.execute()
        speak = from_bus_message(messages[0], SpeakMessage)
        assert speak.data.expect_response is False
    """
    _require_pydantic()
    return model.model_validate({
        "message_type": bus_msg.msg_type,
        "data": bus_msg.data,
        "context": bus_msg.context,
    })


def validate_fixture(path: Union[str, Path]) -> "SerializedTest":
    """Load and validate a JSON fixture file produced by :meth:`~ovoscope.End2EndTest.save`.

    Each message in the ``source_message`` and ``expected_messages`` arrays is
    validated against :class:`~ovos_pydantic_models.message.OpenVoiceOSMessage`.
    If any message is malformed, a :class:`ValueError` is raised with a clear
    description pointing to the offending section and index — instead of the
    cryptic :class:`KeyError` or :class:`TypeError` that would surface inside
    :meth:`~ovoscope.End2EndTest.deserialize`.

    Args:
        path: Path to the JSON fixture file written by
            :meth:`~ovoscope.End2EndTest.save`.

    Returns:
        The raw fixture :class:`dict`, ready to pass directly to
        :meth:`~ovoscope.End2EndTest.deserialize`.

    Raises:
        ValueError: If any message in the fixture fails schema validation.
            The original :class:`pydantic.ValidationError` is chained as the cause.
        FileNotFoundError: If ``path`` does not exist.
        ImportError: If ``ovos-pydantic-models`` is not installed.

    Example::

        from ovoscope.pydantic_helpers import validate_fixture
        from ovoscope import End2EndTest

        test = End2EndTest.deserialize(validate_fixture("test/fixtures/hello.json"))
        test.execute()
    """
    _require_pydantic()
    path = Path(path)
    with path.open() as f:
        data: "SerializedTest" = json.load(f)

    for section in ("source_message", "expected_messages"):
        msgs = data.get(section, [])  # type: ignore[union-attr]
        if isinstance(msgs, dict):
            # legacy fixtures stored a single message object here; the
            # current schema (End2EndTest.serialize) always writes a list
            msgs = [msgs]
        for i, raw in enumerate(msgs):
            # Fixtures use the Message.serialize() "type" key; pydantic models
            # expect "message_type".  Accept either form.  Use None (not "")
            # as the fallback so that pydantic rejects messages where both
            # keys are absent — an empty string would silently pass.
            normalised: Dict[str, Any] = {
                "message_type": raw.get("type") or raw.get("message_type"),
                "data": raw.get("data", {}),
                "context": raw.get("context", {}),
            }
            try:
                OpenVoiceOSMessage.model_validate(normalised)
            except ValidationError as exc:  # type: ignore[possibly-undefined]
                raise ValueError(
                    f"Fixture validation failed in '{section}[{i}]' "
                    f"(type={normalised['message_type']!r}) in {path}"
                ) from exc

    return data
