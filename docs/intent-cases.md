# Intent Cases
`ovoscope.intent_cases` lets skill authors describe expected intent routing
as plain-text files instead of Python. Adding a phrase, an intent, or a
whole new language is a pure text edit — no test code required.

```python
from ovoscope.intent_cases import register_intent_case_tests
```

## Layout

```
test/end2end/cases/
    <lang>/
        <IntentName>.intent.test     # one utterance per line, expected
                                      # to match <IntentName>
        no_match.test                # utterances expected to match
                                      # NO intent of this skill
```

`#` comments and blank lines are ignored in `.test` files.

## Usage

One call, in a test module owned by the skill:

```python
# test/end2end/test_intents.py
from pathlib import Path
from ovoscope.intent_cases import register_intent_case_tests

register_intent_case_tests(
    globals(),
    skill_id="ovos-skill-personal.openvoiceos",
    handlers={
        "WhoAreYou": "PersonalSkill.handle_who_are_you_intent",
        "WhatAreYou": "PersonalSkill.handle_what_are_you_intent",
    },
    cases_dir=Path(__file__).parent / "cases",
)
```

The call creates one `unittest.TestCase` class per pipeline family in the
caller's module — `TestPadatious`, `TestPadacioso`, `TestM2V`, and
`TestDefaultPipeline` by default — each containing one `test_*` method per
`(lang, utterance)` pair found under `cases_dir`. A test passes if its
pipeline family routes the utterance to the expected intent, matching
realistic production cascade behaviour. Pass `pipelines={...}` to override
the generated set with a subset, or with custom pipeline stage lists.

---

## `IntentCase` (`ovoscope/intent_cases.py`)
Frozen dataclass: a single expectation — `utterance` in `lang` should match
`intent`.

| Field | Type | Description |
|---|---|---|
| `lang` | `str` | Language directory the case came from. |
| `utterance` | `str` | The utterance text. |
| `intent` | `Optional[str]` | Expected canonical intent name (`"<IntentName>"`, the `.intent` suffix is folded off), or `None` to assert the utterance falls through to `complete_intent_failure`. |
| `source` | `Path` | The `.test` file the case was read from. |

## `load_intent_cases(cases_dir, known_intents=None) -> List[IntentCase]`
Discover every `IntentCase` under `cases_dir`. Returns `[]` if `cases_dir`
does not exist. If `known_intents` is given, every discovered intent name (suffix folded)
filename found is validated against it — a typo raises `AssertionError`
instead of being silently skipped.

## `assert_intent_case(minicroft, skill_id, handlers, case, pipeline, *, ignore_messages=None, timeout=30) -> None`
Fire `case.utterance` through `pipeline` on a running `minicroft` and assert
routing, using `End2EndTest` under the hood.

- `case.intent is None` — asserts the full `source_message` →
  `complete_intent_failure` → `ovos.utterance.handled` sequence.
- Otherwise — asserts `source_message` → `<skill_id>.activate` →
  `<skill_id>:<intent>` → `mycroft.skill.handler.start` →
  `mycroft.skill.handler.complete` → `ovos.utterance.handled`, using
  `handlers[case.intent]` as the expected handler name. Raises
  `AssertionError` up front if `case.intent` has no entry in `handlers`.

`ignore_messages` defaults to `DEFAULT_IGNORE_MESSAGES` (`"speak"`,
`"mycroft.audio.play_sound"`, `"ovos.common_play.stop.response"`) — message
types that are noisy or non-deterministic and should not be asserted on.

## `register_intent_case_tests(target_globals, *, skill_id, handlers, cases_dir, pipelines=None, ignore_messages=None, timeout=30, m2v_warmup=10.0) -> Dict[str, type]`
Create per-pipeline `TestCase` classes in `target_globals` (pass `globals()`
from the calling test module so pytest collects them).

| Parameter | Default | Description |
|---|---|---|
| `target_globals` | required | `globals()` of the caller's test module. |
| `skill_id` | required | Full skill plugin id, e.g. `"my-skill.author"`. |
| `handlers` | required | `{"<IntentName>.intent": "<HandlerMethodName>"}`, covering every intent referenced by case files. |
| `cases_dir` | required | Directory containing `<lang>/<Intent>.intent.test` and optional `<lang>/no_match.test` files. |
| `pipelines` | `None` | `{class_suffix: pipeline_stage_list}` to override the default per-family classes (`DEFAULT_PIPELINE_FAMILIES`: Padatious, Padacioso, M2V, DefaultPipeline). |
| `ignore_messages` | `None` | Extra message types to filter out of comparison, added to `DEFAULT_IGNORE_MESSAGES`. |
| `timeout` | `30` | Per-case execution timeout, in seconds. |
| `m2v_warmup` | `10.0` | Seconds to wait (upper bound) after booting `MiniCroft` for the m2v pipeline to finish syncing its label index. Set to `0` if not running M2V cases. |

Returns `{}` with no classes created if `cases_dir` has no case files —
this lets a freshly-copied template pass collection before any `.test`
files are added.

All generated test classes share one `MiniCroft` instance per
`(skill_id, langs)` key, booted lazily on first use and cached at module
scope. It is registered with `atexit` (`stop_shared_minicrofts()`) so it
does not leak process-wide `SessionManager`/`Configuration` patches past
the test run. At most one shared instance is kept alive at a time —
requesting a different `(skill_id, langs)` key stops the cached instance
first, since two live `MiniCroft`s fight over the same globals.

## `autodiscover_from_conftest(conftest_dir, target_globals) -> Dict[str, type]`
Zero-boilerplate alternative to calling `register_intent_case_tests`
directly: looks for an `ovoscope_intent_cases` dict in the conftest
namespace and calls `register_intent_case_tests` with it. A skill opts in
by adding a `conftest.py` next to its `cases/` directory:

```python
ovoscope_intent_cases = dict(
    skill_id="my-skill.author",
    handlers={"DoX.intent": "MySkill.handle_do_x"},
    # optional: cases_dir, pipelines, ignore_messages, timeout, m2v_warmup
)
```

The ovoscope pytest plugin's `pytest_collect_directory` hook discovers this
conftest, walks `<dir>/cases/`, and generates the same `TestCase` classes
`register_intent_case_tests` would have created. Returns `{}` if the
conftest has no `ovoscope_intent_cases` or the cases directory does not
exist.

---

## Cross-References
- [end2end-test.md](end2end-test.md) — `End2EndTest`, used internally by `assert_intent_case`.
- [minicroft.md](minicroft.md) — `MiniCroft` / `get_minicroft()`, the runtime the shared instance wraps.
- [e2e-pipeline-harness.md](e2e-pipeline-harness.md) — a lower-level harness for testing a single pipeline plugin directly against raw bus messages, rather than via `.test` case files.
