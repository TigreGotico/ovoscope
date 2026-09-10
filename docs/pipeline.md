# Pipeline Plugin Testing

`ovoscope.pipeline` provides `PipelineHarness` for testing intent / pipeline
plugins in isolation: no skill is needed.

## What Is Tested

Pipeline plugins (Adapt, Padatious, Padacioso, OCP, etc.) match utterances to
intents. `PipelineHarness` loads the specified stages on a `MiniCroft` that
has no skills, so only the pipeline matching logic is exercised.

## `_SinkSkill`: Internal Catch-all

`_SinkSkill`: `ovoscope/pipeline.py`

When `PipelineHarness` creates a `MiniCroft`, it injects an internal
`__ovoscope_sink__` skill as a routing target for matched intents.  This is
necessary because OVOS routes intent matches to a skill handler; without a
skill present the match is discarded.  `_SinkSkill` simply records the matched
intent message and signals the waiting `match()` call.

Users never interact with `_SinkSkill` directly.

## `PipelineHarness`: Context Manager

`PipelineHarness`: `ovoscope/pipeline.py`

```python
from ovoscope.pipeline import PipelineHarness

with PipelineHarness(
    pipeline=["ovos-adapt-pipeline-plugin.openvoiceos"],
    lang="en-US",
) as harness:
    msg = harness.assert_matches("turn on the kitchen lights")
    harness.assert_no_match("garbled nonsense xyz 123")
```

### Constructor Arguments

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `pipeline` | `List[str]` | `[]` | OPM pipeline stage IDs to load. |
| `pipeline_config` | `Dict[str, Dict]` | `{}` | Per-stage config overrides keyed by stage ID. |
| `lang` | `str` | `"en-US"` | Language tag. |

### Methods

| Method | Source | Returns | Description |
|--------|--------|---------|-------------|
| `match(utterance, timeout=5.0)` | `ovoscope/pipeline.py` | `Optional[Message]` | Send utterance; return matched `Message` or `None` on timeout/failure. |
| `assert_matches(utterance, intent_type=None, timeout=5.0)` | `ovoscope/pipeline.py` | `Message` | Assert at least one stage matches. Raises `AssertionError` if no match. `intent_type` is a substring check on `msg_type`. |
| `assert_no_match(utterance, timeout=2.0)` | `ovoscope/pipeline.py` | `None` | Assert no stage matches. Raises `AssertionError` if a match is found. |

### Pipeline Stage Ordering and Success vs Failure

OVOS evaluates pipeline stages in the order listed in `pipeline`.  The first
stage that returns a non-empty match list wins; remaining stages are skipped.

**Success signal**: `intent.service.skills.activated` bus message: emitted
when a stage commits to handling the utterance.

**Failure signal**: `intent_failure` or `mycroft.skill.handler.start` bus
messages: emitted when no stage matched after all stages have been consulted.

`match()`: `ovoscope/pipeline.py`: uses separate `threading.Event`
objects for success and failure so that an `intent_failure` arriving first
does not mask a subsequent late success match.  On timeout or failure the
method returns `None`; on success it returns the captured `Message`.

## Examples

### Testing Adapt Pipeline Matching

```python
from ovoscope.pipeline import PipelineHarness

with PipelineHarness(
    pipeline=["ovos-adapt-pipeline-plugin.openvoiceos"],
    lang="en-US",
) as harness:
    # Adapt must have registered an intent containing "LightsOnIntent"
    msg = harness.assert_matches(
        "turn on the kitchen lights",
        intent_type="LightsOnIntent",
    )
    print(msg.data)  # {"LightsOnKeyword": "lights", ...}

    # Unrecognised utterance must not match
    harness.assert_no_match("garbled xyz 123")
```

### Testing Padatious Entity Extraction

```python
from ovoscope.pipeline import PipelineHarness

with PipelineHarness(
    pipeline=["ovos-padatious-pipeline-plugin.openvoiceos"],
    lang="en-US",
) as harness:
    # Padatious must have a trained intent that matches this utterance
    msg = harness.assert_matches(
        "set a timer for 5 minutes",
        intent_type="timer.intent",
    )
    # Entity extraction is in msg.data
    assert msg.data.get("duration") == "5 minutes"
```

### `assert_matches(intent_type=...)` semantics

`intent_type` is a **substring** check on the matched message's `msg_type`
— `ovoscope/pipeline.py`:

```python
# Pass: msg_type "padatious:0.95:LightsOnIntent" contains "LightsOnIntent"
msg = harness.assert_matches("turn on the lights", intent_type="LightsOnIntent")

# Pass: no intent_type check: any match accepted
msg = harness.assert_matches("turn on the lights")

# Fail: "LightsOffIntent" not in "padatious:0.95:LightsOnIntent"
msg = harness.assert_matches("turn on the lights", intent_type="LightsOffIntent")
# → AssertionError: Expected intent type to contain 'LightsOffIntent', got '...'
```

## Implementation Notes

`PipelineHarness.__enter__`: `ovoscope/pipeline.py`: creates a
`MiniCroft` with `skill_ids=[]` and the specified pipeline.

`PipelineHarness.match()`: `ovoscope/pipeline.py`: subscribes to
`intent.service.skills.activated` (success) and `intent_failure` /
`mycroft.skill.handler.start` (failure) before emitting the utterance,
then waits on a `threading.Event` with the given timeout.  Bus handlers are
removed after the wait completes to avoid cross-test leakage.

---
[← End2EndTest](end2end-test.md) · [Home](../README.md) · [Pydantic Integration →](pydantic-integration.md)
