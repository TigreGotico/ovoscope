# Multi-Engine Golden Utterances

`ovoscope.golden` evaluates a skill's golden-utterance corpus against
several intent-matching pipeline plugins at once, without booting a
MiniCroft or a message bus. It resolves each engine by its real OPM
(OVOS Plugin Manager) entry point, registers the skill's own intents
straight through that plugin's own `register_intent` method, and fires
every golden row through the plugin's own `match_<tier>`/`calc_intent`
— the exact call path the real intent service uses. A fighter's score
is the real shipping pipeline's score, never a reimplementation.

```python
from ovoscope.golden import (build_engine_intents, load_golden_rows,
                              run_golden_suite, assert_gates,
                              write_predictions)
```

## Resolution is generic, not four hardcoded engines

A **fighter** is a pipeline entry-point id (the same ids `mycroft.conf`'s
`intents.pipeline` lists, and the ones `ovos-plugin-arena` names
competitors after) plus a confidence tier and the plugin config that
tier implies:

```python
FighterSpec(
    competitor_id="padatious-medium",
    entry_point="ovos-padatious-pipeline-plugin",
    tier="medium",
    config={"conf_high": 0.95, "conf_med": 0.8, "conf_low": 0.5,
            "domain_engine": False, "cast_to_ascii": False,
            "stem": False, "disable_padaos": False,
            "instant_train": False},
    plugin_id="ovos-padatious",
)
```

`FIGHTERS` (the default registry) holds one entry per template engine —
`padatious-medium`, `padacioso-medium`, `nebulento-medium` — each gated
at their tier's `conf_med` (0.8). `GenericOPMAdapter` drives ANY
installed plugin that implements the standard
`register_intent(message)` / `calc_intent(utterances, lang, message)` /
`match_<tier>(utterances, lang, message)` trio
(`ovos_plugin_manager.templates.pipeline.ConfidenceMatcherPipeline`'s
own shape). A newly-registered fusion engine that ships the same
interface is runnable by adding one `FighterSpec` — no new adapter
class, no ovoscope change. `EngineAdapter` exists only as an escape
hatch for a plugin whose registration or training genuinely doesn't fit
that generic shape.

`resolve_pipeline_plugin(entry_point_id)` resolves an installed
plugin's class by id (via `ovos_plugin_manager.pipeline.
find_pipeline_plugins()`), returning `None` when it isn't installed —
this is the entire lookup mechanism; there is no separate hardcoded
adapter registry to keep in sync with what's actually installed.

## The paradigm split

Padatious, padacioso and nebulento are all *template* engines: given a
finite set of example phrasings per intent, they either match a query
to one of them or they don't. A skill that ships a golden-utterance
corpus is making a claim about its own template coverage, so these
three fighters are **gating** by default — a skill's golden suite is
expected to hit 100% at their tier's confidence gate, and any miss is a
template gap in the skill itself, not a matcher shortcoming. Nebulento
is a fuzzy matcher and its raw `calc_intent` always returns *some*
best-effort guess, including on totally unrelated input — the arena's
own 0.8 confidence gate on `match_medium` is what turns that into a
real match/no-match decision here, same as the other two.

`m2v-prototype` (Model2Vec's zero-shot pipeline) is scored differently,
and is **CI-display-only**: `ovos-m2v-pipeline` does not yet expose a
standalone, bus-free scoring path for its real algorithm (which keeps
every registered sample as its own vector and scores max-per-label
cosine, plus bounded template/entity expansion before embedding). This
module's `M2VPrototypeAdapter` approximates it with a per-intent
centroid average instead — a materially different, generally weaker
algorithm — so its score is reported in the scoreboard as an indicative
percentage but is **never gating by default** and is **excluded from
published prediction rows** (see below). Once `ovos-m2v-pipeline` ships
a standalone `PrototypeScorer` using the real per-sample vector store,
the adapter switches to it and stops being an approximation.

An engine that is not installed, or (for `m2v-prototype`) has no
reachable embedding model, is never silently dropped from the
scoreboard: it gets an explicit `unavailable` entry with a **stable**
reason id — `not-installed`, `no-model`, or `init-error` — never free
text, so a downstream consumer (including arena rows) can key off it.
An unavailable *gating* engine fails its own gate: there is no way to
tell whether it would have matched, so it cannot count as a pass.

## Corpus schema

One JSON object per line:

```json
{"utterance": "what's the weather", "lang": "en-us", "skill_id": "ovos-skill-weather.openvoiceos", "expected_intent": "CurrentWeather", "core": true}
```

`expected_intent` may also be spelled `intent_label` (the original
golden-utterance convention this schema comes from). `expected_intent:
null` marks an out-of-scope sample — the correct behaviour for every
engine is *no match*. Any other field (`source_repo`, `source_file`,
`intent_type`, `required_vocab`, ...) is kept as provenance and never
required.

`core: true` marks a must-match row — usually the canonical phrasing
for an intent. Core rows are required to match on **every gating
engine** regardless of that engine's aggregate percentage: a corpus
can only pass its gate by hitting 100% on the ordinary rows *and* never
missing a core row, so a single broken canonical phrasing fails loud
even if everything else still clears the bar.

## Running the suite

```python
rows = load_golden_rows("test/end2end/golden_utterances.jsonl")
groups = {
    ("ovos-skill-weather.openvoiceos", "en-us"):
        build_engine_intents("locale", "en-us"),
}
scoreboard, predictions = run_golden_suite(rows, groups)
assert_gates(scoreboard)  # raises AssertionError, lists every failing row
```

`scoreboard` is `{competitor_id: {total, matched, pct, unavailable,
reason, core_total, core_matched, gate_passed, gate_reason, failures}}`
— `failures` lists every missed row's utterance, language, expected and
actual intent, so a red gate points straight at the missing template.

## One corpus, two consumers

`run_golden_suite` also returns a list of prediction rows shaped like
`ovos-plugin-arena`'s prediction contract
(`docs/SPECIFICATION.md` §3.2 on `OpenVoiceOS/ovos-plugin-arena`, mirrored
here as `PredictionRow` without a hard dependency on that repo): one row
per `(engine, golden row)` that actually ran, carrying `competitor_id`,
`confidence`, `latency_ms`, `exact_match` and the rest of that contract.
`write_predictions` lays them out at
`predictions/<lang>/<competitor_id>.jsonl`, the exact layout the arena's
own benchmark assembler expects — so a skill's own CI run of its golden
corpus can be uploaded straight into the arena's leaderboard pipeline
without a second, separate evaluation of the same phrasings.
`m2v-prototype` rows are excluded from this output for the reason above
— it stays in the returned `predictions` list and the scoreboard, just
not in what `write_predictions` writes.

`competitor_id` values are the arena registry's existing fighter ids
(`padatious-medium`, `padacioso-medium`, `nebulento-medium`,
`m2v-prototype`) — this module never invents a new one.

## Extras

Padacioso ships with bare `ovos-core` (via ovos-workshop). Nebulento,
`ovos-m2v-pipeline` and Adapt are separate installs:

```
pip install 'ovoscope[engines]'
```

Padatious — archived, LGPL-licensed — needs `ovoscope[padatious]` instead.

## Wiring into a skill's CI

This module is the runner and its own test only. Wiring it into a
skill's `test/end2end/` suite the way `ovoscope.intent_cases` is wired
today is a follow-up once the gating model above is settled.
