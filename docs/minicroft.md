# MiniCroft
`MiniCroft` is a minimal, in-process OVOS Core that loads real skill plugins and runs the full intent pipeline on a `FakeBus`. It is the execution engine behind every OvoScope test.
## Class: `MiniCroft` (`ovoscope/__init__.py`)
```python
from ovoscope import MiniCroft
```
Subclass of `ovos_core.skill_manager.SkillManager`.
`get_minicroft()` factory (`ovoscope/__init__.py`) replaces the real WebSocket bus with `FakeBus`, disables components not needed for testing, and only loads the skills you specify.
### Constructor
```python
MiniCroft(
    skill_ids: list[str],
    enable_installer: bool = False,
    enable_intent_service: bool = True,
    enable_event_scheduler: bool = False,
    enable_file_watcher: bool = False,
    enable_skill_api: bool = True,
    extra_skills: dict[str, OVOSSkill] | None = None,
    isolate_config: bool = True,
    default_pipeline: list[str] | None = LEAN_DEFAULT_PIPELINE,
    extra_pipelines: list[str] | None = None,
    lang: str | None = None,
    secondary_langs: list[str] | None = None,
    pipeline_config: dict[str, dict] | None = None,
    modernize: bool = True,
    emit_legacy: bool = True,
    *args, **kwargs,
)
```
| Parameter | Default | Description |
|---|---|---|
| `skill_ids` | required | Skill plugin IDs to load (from installed entry points) |
| `enable_installer` | `False` | Enable the runtime pip installer service |
| `enable_intent_service` | `True` | Enable intent matching pipeline |
| `enable_event_scheduler` | `False` | Enable scheduled event service |
| `enable_file_watcher` | `False` | Enable settings file watcher |
| `enable_skill_api` | `True` | Enable skill API exposure |
| `extra_skills` | `None` | Inject skill instances directly (useful for testing a skill class before packaging) |
| `isolate_config` | `True` | Clear user XDG configs so tests are reproducible |
| `default_pipeline` | `LEAN_DEFAULT_PIPELINE` | Full override of the session pipeline. Replaces the lean default entirely — see "Lean Default Pipeline" below. |
| `extra_pipelines` | `None` | Matcher ids to append on top of whichever pipeline was chosen above (the lean default, or a `default_pipeline=` override), without having to restate the whole list — e.g. `extra_pipelines=M2V_PIPELINE` to add M2V to the lean default. |
| `lang` | `None` | Override the system default language (`Configuration()["lang"]`). Patched before Adapt/Padatious init so vocab is registered for this language. |
| `secondary_langs` | `None` | Set `Configuration()["secondary_langs"]`. Adapt and Padatious create per-language engines for each language in this list, enabling multilingual intent matching. |
| `pipeline_config` | `None` | Per-pipeline plugin config overrides. A `dict` keyed by the plugin's config key under `Configuration()["intents"]` (e.g. `"ovos_m2v_pipeline"`). Patched before `super().__init__()` so pipeline plugins read overridden values during their `__init__`. Restored in `stop()`. |
| `modernize` | `True` | Forwarded to the harness `FakeBus`. When `True`, emitting a legacy bus topic also emits its `ovos.*` spec counterpart (legacy producer → spec listener). |
| `emit_legacy` | `True` | Forwarded to the harness `FakeBus`. When `True`, emitting an `ovos.*` spec topic also emits the matching legacy topic (spec producer → legacy listener). Set both `modernize` and `emit_legacy` to `False` to isolate a single namespace and assert no cross-namespace bridging occurs. |
### Key attributes
| Attribute | Type | Description |
|---|---|---|
| `bus` | `FakeBus` | The in-process message bus |
| `boot_messages` | `list[Message]` | All messages captured during startup |
| `status` | `ProcessState` | Current lifecycle state |
### `MiniCroft.run()`
Loads plugins and marks the runtime as ready. Called internally by `start()`. Does not block: returns after all skills are loaded.
### `MiniCroft.stop()`
Shuts down skills and closes the bus.
---
## Lean Default Pipeline
By default, MiniCroft boots only `LEAN_DEFAULT_PIPELINE`: the Stop, Converse,
Adapt, Padatious, Padacioso and Fallback matchers, high and medium
confidence tiers. This is the set the ovoscope test suite (and every
skill-fixture suite built on it) actually exercises — Stop is included
unconditionally because skills assert stop behavior fleet-wide and the
default config always runs it.

Every OTHER installed pipeline plugin — `ovos-m2v-pipeline`,
`ovos-m2v-prototype-pipeline`, `ovos-persona-pipeline-plugin`,
`ovos-common-query-pipeline-plugin`, the OCP pipeline plugins, the `-low`
confidence tier, and any third-party `opm.pipeline` plugin — is never
instantiated by a lean-default boot. This matters beyond intent-matching
determinism: some pipeline plugins do expensive or even blocking work at
init or on common bus events (e.g. `ovos-m2v-pipeline`'s intent-sync handler
sleeps synchronously), and instantiating every installed plugin regardless
of what a test actually needs can push MiniCroft's `READY` wait well past
what a CI job budgets for.

Listing a matcher in `intents.pipeline` alone does **not** stop
`IntentService` from loading the other installed plugins — only
`intents.blacklisted_pipelines` does. MiniCroft computes and sets that
blacklist for you: it is "every installed pipeline plugin not covered by
the chosen pipeline", patched before `IntentService` is constructed, and
restored in `stop()`.

To keep the lean set but add one heavy matcher for a suite that needs it,
use `extra_pipelines` instead of restating the whole lean list:

```python
from ovoscope import get_minicroft, M2V_PIPELINE

croft = get_minicroft(["my-skill.openvoiceos"], extra_pipelines=M2V_PIPELINE)
```

To replace the pipeline entirely (e.g. to test Adapt in isolation, or to
run the heavier `DEFAULT_TEST_PIPELINE`/`PERSONA_PIPELINE`/`M2V_PIPELINE`
combinations documented elsewhere in this file), pass `default_pipeline=`
as a full override — it is not merged with the lean default.

If any matcher id in the resulting pipeline — lean default, `extra_pipelines`
addition, or `default_pipeline` override — fails to load (the plugin isn't
installed, or raises during construction), `get_minicroft()` raises
`RuntimeError` naming the missing stage(s) once `READY` is reached, rather
than silently running with fewer matchers than the test expects.
---
## Factory: `get_minicroft()`
```python
from ovoscope import get_minicroft
croft = get_minicroft(
    skill_ids: list[str] | str,
    max_wait: float = 60,
    wait_for_trained: bool = True,
    **kwargs  # forwarded to MiniCroft constructor
)
```
Creates, starts, and waits for a `MiniCroft` to reach `READY` state. Returns the ready instance.
```python
croft = get_minicroft(["skill-weather.openvoiceos", "skill-timer.openvoiceos"])
# croft.status.state == ProcessState.READY
```

Loading skills only registers their intents; a pipeline plugin still has to
compile them before matching is reliable, and `mycroft.skills.trained` is
that plugin's own private readiness signal — it is not a spec topic.
Padatious, Padacioso, and Nebulento subscribe to `mycroft.skills.train` and
reply with `mycroft.skills.trained` once compiling settles. Adapt and m2v
register intents synchronously and never subscribe to
`mycroft.skills.train`, so they never send that reply.

Once `READY`, `get_minicroft()` waits for `mycroft.skills.trained` to go
quiet — no new event for a short window — before returning, but only when
both hold: a loaded skill actually registered an intent, and some pipeline
plugin on the bus subscribes to `mycroft.skills.train`. Skills with nothing
to train (pure event-handler skills, an empty `skill_ids`) skip the wait,
and so does a boot whose pipeline has no such subscriber — for example
`default_pipeline=M2V_PIPELINE` or an adapt-only pipeline — since nothing
will ever report training done. If an intent was registered, a subscriber
is present, and training never completes within `OVOSCOPE_TRAINED_TIMEOUT`
seconds (default: 180s when the `CI` environment variable is set, 5s
otherwise), `get_minicroft()` raises `RuntimeError` naming only the
skill(s) that registered an intent and never got a `mycroft.skills.trained`
reply — a stuck trainer in one skill never blames an unrelated, intentless
skill loaded alongside it. Pass `wait_for_trained=False` to opt out.

`get_minicroft()` also raises `RuntimeError` if any matcher id in the
configured pipeline (see "Lean Default Pipeline" above) failed to load —
naming the missing stage(s) — so a plugin that's absent or errors during
init is never silently dropped from the boot.

Only `ovos-core[lgpl,plugins]` (or the specific pipeline plugin packages)
ship Adapt/Padatious/Padacioso matchers. Adapt and Padacioso live in
`[plugins]`; Padatious is LGPL-licensed and lives in `[lgpl]` instead —
`[plugins]` alone leaves Padatious missing. Installing bare `ovos-core` in
a test environment leaves only Padacioso available, so every
Adapt-registered intent silently fails to match — install
`ovos-core[lgpl,plugins]` (or the plugins your skills under test actually
need) alongside ovoscope.
---
## Injecting Skills Under Test
To test a skill class that isn't installed as a plugin, inject it directly via `extra_skills`:
```python
from my_skill import MySkill
croft = get_minicroft(
    skill_ids=[],
    extra_skills={"my-skill.test": MySkill},
)
```
The skill ID key must match what the skill would normally register under.
---
## Multilingual Testing
By default, Adapt and Padatious only register vocab/intents for the system's configured default language. To test skills in other languages, pass `secondary_langs`:
```python
croft = get_minicroft(
    ["my-skill.openvoiceos"],
    secondary_langs=["pt-PT", "de-DE", "es-ES"],
)
```
This patches `Configuration()["secondary_langs"]` before `IntentService` initializes, so Adapt creates per-language engines and registers vocab from all locale directories.
To also change the primary language:
```python
croft = get_minicroft(
    ["my-skill.openvoiceos"],
    lang="pt-PT",
    secondary_langs=["en-US", "de-DE"],
)
```

When testing with N secondary languages, training overhead scales with the number of per-language containers — for example, a 17-locale suite may require 129 seconds for unconstrained training (on a system without resource limits). `max_wait` bounds only the wait for `READY`; it has no effect on the training wait that follows. The training wait is bounded by `OVOSCOPE_TRAINED_TIMEOUT`, and its default (180s under `CI`, 5s otherwise) is tuned for single-language loads. For multilingual suites, set `OVOSCOPE_TRAINED_TIMEOUT` large enough to accommodate all language engines, and raise `max_wait` too if reaching `READY` itself is slow with that many engines starting up:

```python
import os
os.environ["OVOSCOPE_TRAINED_TIMEOUT"] = "300"  # per-language training can be slow
croft = get_minicroft(
    ["my-skill.openvoiceos"],
    secondary_langs=["en-US", "pt-PT", "de-DE", "es-ES", "fr-FR"],
    max_wait=300,  # covers a slow READY, not training
)
```

The test suite itself can then settle to a quiet window using the same pattern `ovos-skill-alerts` uses for its multilingual fixtures: set `OVOSCOPE_TRAINED_TIMEOUT` with margin so all per-language training completes before assertions run.

---
## pytest-timeout Convention

Any test suite using `get_minicroft` must set `pytest-timeout` comfortably above the `get_minicroft` trained-wait ceiling. On slow 2-core CI runners, a pytest-timeout that equals or falls below the trained-wait ceiling can kill the `setUpClass` or `setUp` mid-training-wait before `get_minicroft` returns, and the failure masquerades as a boot failure rather than a timeout — diagnosis is difficult without knowing to check the framework timeout separately. Set `pytest-timeout` to at least `trained-ceiling + 120s` with margin: for single-language suites with the 180-second CI default, use 300 seconds or more; for multilingual suites, set `pytest-timeout ≥ max_wait + 120s` to ensure the wait completes before the framework timeout fires.

---
## Pipeline Plugin Config Overrides
Use `pipeline_config` to override per-plugin configuration under `Configuration()["intents"]` before pipeline plugins initialize. This ensures tests are reproducible regardless of the user's local `mycroft.conf`.

The key must match the plugin's config key (the key it reads under `Configuration()["intents"]`):

```python
# Force M2V to use the multilingual model regardless of mycroft.conf
croft = get_minicroft(
    ["my-skill.openvoiceos"],
    default_pipeline=M2V_PIPELINE,
    pipeline_config={
        "ovos_m2v_pipeline": {
            "model": "Jarbas/ovos-model2vec-intents-distiluse-base-multilingual-cased-v2",
        }
    },
)
```

All overrides are restored to their original values in `MiniCroft.stop()`.

---
## Boot Sequence
On startup, MiniCroft captures all messages emitted during skill loading into `boot_messages`. These can be asserted in `End2EndTest.expected_boot_sequence`. The typical boot sequence includes:
1. `mycroft.skills.train`: intent pipeline training request
2. `mycroft.skills.initialized`: skills initialized
3. `mycroft.skills.ready`: skills service ready
4. `mycroft.ready`: all core services ready
Skills that participate in `converse` or `fallback` registration also emit messages during boot (e.g. `ovos.skills.fallback.register`).

---
[← CI Integration](ci-integration.md) · [Home](../README.md) · [Capture Session →](capture-session.md)
