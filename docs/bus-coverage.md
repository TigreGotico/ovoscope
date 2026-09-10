# Bus Coverage

Bus coverage measures how thoroughly an end-to-end test exercises a skill's
MessageBus interface.  Unlike line coverage (which measures code paths), bus
coverage answers:

> *Which message handlers did my tests actually trigger?  Which messages did
> the skill emit, and which of those did I explicitly assert?*

## Summary

| Dimension | What it measures |
|-----------|-----------------|
| **Listener coverage** | Which message types the skill is listening for and if they were invoked. |
| **Emitter coverage** | Which message types the skill emitted and if they were asserted in the test. |

---

## Enabling Coverage Tracking

There are three ways to enable bus coverage:

### 1. Global (Recommended for Pytest)
Pass the `--ovoscope-bus-cov` flag to pytest. This automatically enables tracking for all `End2EndTests` in the session and captures the full boot sequence.

```bash
# Basic report
pytest test/end2end/ --ovoscope-bus-cov

# Verbose report (shows exact message types)
pytest test/end2end/ --ovoscope-bus-cov --ovoscope-bus-cov-verbose

# Filter by skill_id regex
pytest test/end2end/ --ovoscope-bus-cov --ovoscope-bus-cov-include="my-skill"
pytest test/end2end/ --ovoscope-bus-cov --ovoscope-bus-cov-exclude="^Thread-|^__core__$"

# Save to JSON for CI
pytest test/end2end/ --ovoscope-bus-cov --ovoscope-bus-cov-file=bus-cov.json
```

### 2. Manual (Per Test)
Add `track_bus_coverage=True` to an `End2EndTest` instance.

```python
test = End2EndTest(..., track_bus_coverage=True)
test.execute()
report = test.bus_coverage_report
```

### 3. CLI Subcommand
Run coverage against a directory of JSON fixtures.

```bash
ovoscope bus-coverage Skills/ovos-skill-hello-world/test/end2end/ --verbose
```

---

## Thread Component Merging

OVOS components that inherit from `Thread` (such as `SkillManager`, `PlaybackService`, 
`OVOSDinkumVoiceService`, and `MediaService`) are automatically resolved to their 
actual class names using Python's Method Resolution Order (MRO).

**As of ovoscope 0.14.0**, the `_get_component_name()` method walks the MRO chain 
and skips the `Thread` class, returning the first non-Thread class name. This means:

| Component Class | Inherits From | Reported As |
|----------------|---------------|-------------|
| `MiniCroft` (test harness) | `SkillManager` → `Thread` | `SkillManager` |
| `SkillManager` | `Thread` | `SkillManager` |
| `PlaybackService` | `Thread` | `PlaybackService` |
| `OVOSDinkumVoiceService` | `Thread` | `OVOSDinkumVoiceService` |
| `MediaService` | `Thread` | `MediaService` |
| `IntentService` | (no Thread) | `IntentService` |

Note: `MiniCroft` (the test harness) is automatically renamed to `SkillManager` in 
reports for clarity, since MiniCroft is just a test wrapper around SkillManager.

This approach is automatic and requires no manual maintenance of message type patterns.
Any future Thread-based components will be correctly attributed without code changes.

---

## How it Works

Ovoscope uses a multi-layered approach to capture 100% of bus activity, including events that happen before the tests officially start.

### Implementation Details

1.  **Global Monkey-Patching**: When enabled, `ovoscope` monkey-patches `ovos_utils.fakebus.FakeBus.on`, `.once`, and `.emit`. This ensures that even "boot sequence" activity (like vocab registration or internal service setup) is captured from the moment the process starts.
2.  **Skill Attribution**: To accurately link message handlers to specific skills, `ovoscope` patches `ovos_workshop.skills.ovos.OVOSSkill.add_event` and `.bind`.
    *   This allows capturing registrations that happen during skill `__init__`.
    *   It handles skill renames (where a skill starts with a generic name and is later assigned a unique `skill_id` by the loader).
3.  **Instance Introspection**: After `MiniCroft` is READY, `ovoscope` performs a final sweep by introspecting `skill.events.events` and walking the bus's internal handler map.

### Data Attribution Logic

Messages and handlers are attributed in this order of precedence:
1.  **Direct Skill ID**: If the handler was registered via a patched `OVOSSkill` method.
2.  **Closure Introspection**: If the handler closure contains a reference to a skill instance.
3.  **Component Name**: If the handler belongs to a core component (e.g., `IntentService`, `AdaptPipeline`).
4.  **`__core__` Bucket**: A fallback for any message type registered or emitted that cannot be linked to a specific skill or component.

---

## Reading the Report

The report table displays three main columns:

```
Skill                              Listeners      Observed  Asserted
──────────────────────────────────────────────────────────────────────
my-skill.author                    8/12   66.7%   10/15     6/15
IntentService                      2/4    50.0%    0/0      0/0
──────────────────────────────────────────────────────────────────────
TOTAL                             10/16   62.5%   10/15     6/15
```

### 1. Listeners (The "What can it hear?" metric)
*   **Formula**: `(Invoked Message Types) / (Registered Message Types)`
*   **Registered**: Total unique message types the skill called `.on()` or `.add_event()` for.
*   **Invoked**: How many of those message types were actually emitted on the bus during the session.
*   **Meaning**: High percentage means your tests are triggering most of the skill's logic paths.

### 2. Observed Emitters (The "What did it say?" metric)
*   **Formula**: `(Emitted Message Types) / (Known Message Types)`
*   **Known**: The set of message types that were *either* emitted by this skill during this session *or* were listed in the test's `expected_messages` for this skill.
*   **Observed**: How many of those message types actually appeared on the bus.
*   **Meaning**: Usually 100% unless you have conditional emissions that didn't fire.

### 3. Asserted Emitters (The "Did I check it?" metric)
*   **Formula**: `(Asserted Message Types) / (Known Message Types)`
*   **Asserted**: How many of the observed message types were explicitly listed in `End2EndTest.expected_messages`.
*   **Meaning**: High percentage means your test suite is strictly validating the skill's output, not just letting it happen.

---

## Verbose Breakdown

In verbose mode (`--ovoscope-bus-cov-verbose`), `ovoscope` lists every message type:

```
LISTENERS: my-skill.author
  [x] my-intent.intent                                 2 invocation(s)
  [ ] some-unused-event                                NOT TESTED

EMITTERS: my-skill.author
  [x] speak                                            observed 1x  [x] asserted
  [x] my-skill.done                                    observed 1x  [ ] not asserted
```

*   **[x] (Checked)**: The listener was triggered or the emitter was asserted.
*   **[ ] (Not checked)**: The listener was never triggered or the emitter was seen but not checked in the test.

---

## Filtering and Tuning

By default, the bus report can be noisy because core services register many internal handlers. Use filtering to focus on your code:

*   **Include**: Only show skills matching a regex.
    *   `--ovoscope-bus-cov-include="my-skill"`
*   **Exclude**: Hide matches. The standard CI/CD workflow excludes threads and internal metadata helpers by default.
    *   `--ovoscope-bus-cov-exclude="^Thread-|^intents$|^skills$"`

---

## Calculations API

If you are building custom tooling, you can access these values via `SkillBusCoverage` properties:

*   `listener_coverage_pct`: `(covered_listeners / total_listeners) * 100`
*   `observed_emitter_pct`: `(observed_emitters / total_emitters) * 100`
*   `asserted_emitter_pct`: `(asserted_emitters / total_emitters) * 100`

Source: `SkillBusCoverage` (`ovoscope/bus_coverage.py`)

---
[← GUI Testing](gui-testing.md) · [Home](../README.md)
