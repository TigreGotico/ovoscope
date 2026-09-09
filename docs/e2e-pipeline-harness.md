# E2E Pipeline Harness
`ovoscope.e2e` provides scaffolding for end-to-end tests of a single
`ConfidenceMatcherPipeline` plugin (Adapt, Padatious, Padacioso, Nebulento,
Palavreado, and similar engine families). Most such plugins need the same
shape of test: patch the plugin's config, boot a `MiniCroft` pinned to that
one pipeline, drive the bus with utterances, and assert on the dispatched
intent message (or the `complete_intent_failure` fallback). `ovoscope.e2e`
factors that shape out so a plugin only has to subclass
`E2EPipelineHarness` and set a handful of class attributes.

It also exposes standalone bus helpers and engine-family registration shims
for callers that prefer pytest-style tests over `unittest.TestCase`.

```python
from ovoscope.e2e import E2EPipelineHarness
```

## Class: `E2EPipelineHarness` (`ovoscope/e2e.py`)

Subclass of `unittest.TestCase`. Set these class attributes:

| Attribute | Default | Description |
|---|---|---|
| `PIPELINE_ID` | `""` | OPM `opm.pipeline` entry-point name to pin `MiniCroft` to (e.g. `"ovos-nebulento-pipeline-plugin"`). `setUpClass` skips the test class if unset. |
| `CONFIG_KEY` | `""` | Key under `Configuration()["intents"]` for the plugin's config. `setUpClass` skips the test class if unset. |
| `PLUGIN_CONFIG` | `{}` | Dict merged into `Configuration()["intents"][CONFIG_KEY]` before `MiniCroft` starts. Restored on `tearDownClass`. |
| `SKILL_ID` | `"test_skill_ovoscope"` | Skill id used by the helper methods when registering intents. Detached in `setUp` to keep tests isolated. |
| `DEFAULT_LANG` | `"en-US"` | Language used by `make_utterance()` / `send_and_capture()` when no explicit `session` is given. |
| `STARTUP_MAX_WAIT` | `60.0` | Seconds to wait for `MiniCroft` to reach `READY`. |
| `MODERNIZE` | `True` | Forwarded to `MiniCroft`/`FakeBus` — legacy emit also dispatches its `ovos.*` spec counterpart. |
| `EMIT_LEGACY` | `True` | Forwarded to `MiniCroft`/`FakeBus` — spec emit also dispatches the legacy topic. Set both `MODERNIZE` and `EMIT_LEGACY` to `False` to drive a single isolated namespace. |

`setUpClass` boots one shared `MiniCroft` for the whole class (pinned to
`[PIPELINE_ID]`) and stores the pipeline plugin instance on `cls.pipeline`.
`tearDownClass` stops it and restores the original `Configuration()["intents"]`
entry.

### Instance attributes and helpers

| Member | Description |
|---|---|
| `self.mc` | The running `MiniCroft` (class-scoped). |
| `self.bus` | Shortcut for `self.mc.bus`. |
| `self.pipeline` | The loaded pipeline plugin instance. |
| `make_utterance(utterance, *, session=None)` | Build a `recognizer_loop:utterance` `Message` using `DEFAULT_LANG`. |
| `send_and_capture(utterance, expected_types, *, timeout=5.0, session=None)` | Emit `utterance` and return the first message whose type is in `expected_types`, or `None` on `complete_intent_failure`/timeout. |
| `expect_no_match(utterance, *, timeout=2.0, session=None)` | Assert emitting `utterance` produces `complete_intent_failure`. |

```python
from ovoscope.e2e import E2EPipelineHarness

class TestNebulento(E2EPipelineHarness):
    PIPELINE_ID = "ovos-nebulento-pipeline-plugin"
    CONFIG_KEY = "ovos-nebulento-pipeline-plugin"

    def test_match(self):
        register_adapt_vocab(self.bus, "greeting", ["hello", "hi"], skill_id=self.SKILL_ID)
        msg = self.send_and_capture("hello", ["greeting_intent"])
        assert msg is not None
```

---

## Standalone bus helpers

These work with any bus that implements `.on()` / `.remove()` / `.emit()`
(`FakeBus` or `MessageBusClient`) and do not require `E2EPipelineHarness`.

### `make_session(session_id="ovoscope-test", *, pipeline=None, blacklisted_intents=None, blacklisted_skills=None, lang="en-US") -> Session`
Build a `Session` with the most common overrides preset.

### `make_utterance_message(utterance, *, lang="en-US", session=None) -> Message`
Build a `recognizer_loop:utterance` `Message`. If `session` is given, its
serialized form is placed in the message context under `"session"`.

### `wait_for_match(bus, expected_types, *, timeout=5.0, emit=None) -> Optional[Message]`
Subscribe to `expected_types` and `complete_intent_failure`, then wait for
the first match. Returns the matching `Message`, or `None` on failure or
timeout.

This helper **blocks**, so a single-threaded caller cannot emit the
utterance and then call `wait_for_match` — the reply may arrive (and be
missed) before the caller resumes. Pass the message to emit as `emit=`
instead: `wait_for_match` subscribes its handlers first, then emits `emit`,
so no reply can be missed.

```python
from ovoscope.e2e import wait_for_match, make_utterance_message

msg = wait_for_match(
    bus,
    ["greeting_intent"],
    timeout=5.0,
    emit=make_utterance_message("hello"),
)
assert msg is not None
```

Only emit the message yourself beforehand (rather than via `emit=`) when
the reply is guaranteed to be asynchronous relative to the emit call.

### `wait_for_failure(bus, *, timeout=2.0) -> bool`
Wait for a `complete_intent_failure` message; return whether one fired.

---

## Intent-registration shims

Emit the bus event a given pipeline engine family expects, so tests can
register intents/vocab without constructing the underlying plugin objects
directly.

| Function | Engine family | Emits |
|---|---|---|
| `register_padatious_intent(bus, name, samples, *, skill_id, lang="en-US", settle=0.1)` | Padatious, Padacioso, Nebulento | `padatious:register_intent` |
| `register_padatious_entity(bus, name, samples, *, skill_id, lang="en-US", settle=0.1)` | Padatious, Padacioso, Nebulento | `padatious:register_entity` |
| `register_adapt_vocab(bus, entity_type, words, *, skill_id, lang="en-US", settle=0.1)` | Adapt, Palavreado | `register_vocab` (one per word) |
| `register_adapt_intent(bus, builder, *, skill_id, lang="en-US", settle=0.1)` | Adapt, Palavreado | `register_intent`. `builder` may be an `IntentBuilder` (`.build()`-ed automatically) or an already-built intent object. |
| `detach_intent(bus, intent_name, *, skill_id, settle=0.1)` | any | `detach_intent` |
| `detach_skill(bus, skill_id, *, settle=0.1)` | any | `detach_skill` |

Pass `skill_id` on every call. `detach_skill` already takes it as its
positional argument. Each shim stamps `Message.context["skill_id"]` so
OVOS-INTENT-4 §3.1/§3.2-conformant plugins accept the registration and
`detach_skill` can find it again. No fallback is derived from `name` or
`entity_type`. Adapt vocab and intent names are conventionally unscoped, so
guessing a `skill_id` from them would be wrong more often than not.

A call that omits it emits the message unattributed and logs one warning
naming the release the argument becomes required in. Unattributed is a poor
state to register in, because the plugin cannot deregister by skill and a
conformance suite cannot tell one caller's intents from another's. It is
also the state a suite written against an earlier ovoscope already had, and
breaking that suite tells its author nothing they can act on.

Every shim sleeps `settle` seconds after emitting (default `0.1`) to give
the pipeline plugin time to process the registration before the caller
proceeds — pass `settle=0` to skip the wait when the caller does its own
synchronization.

---

## Cross-References
- [minicroft.md](minicroft.md) — `MiniCroft` / `get_minicroft()`, the runtime this harness pins to a single pipeline.
- [end2end-test.md](end2end-test.md) — `End2EndTest`, the full declarative multi-message test runner (used by `intent-cases.md` on top of raw `MiniCroft`).
- [intent-cases.md](intent-cases.md) — file-based intent test cases, a higher-level alternative built on `End2EndTest` rather than this harness's bus helpers.
