# Copyright 2026 OpenVoiceOS
# Licensed under the Apache License, Version 2.0
"""Unit tests for ovoscope.golden: multi-engine golden-utterance evaluation.

Uses a toy 2-intent (+ a deliberately thin third intent) skill so the
suite runs fully offline and fast: no network, no MiniCroft/message-bus
boot. Engines are resolved via their real OPM pipeline-plugin entry
points and driven through their own register_intent/calc_intent/
match_<tier> methods (see ovoscope.golden.GenericOPMAdapter) — these
tests exercise the actual shipping Padatious/Padacioso/Nebulento
pipelines, not a reimplementation. m2v-prototype has no cached/
reachable embedding model in this test (a bogus model name is forced
for determinism), so its `unavailable`/`no-model` path is asserted
directly rather than skipped (never skip on a missing/uncached
optional dependency).
"""
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from ovoscope.golden import (DEFAULT_ENGINES, DEFAULT_GATING_ENGINES,
                              FIGHTERS, REASON_INIT_ERROR,
                              REASON_NOT_INSTALLED, GenericOPMAdapter,
                              GoldenRow, M2VPrototypeAdapter,
                              PredictionRow, _make_default_engines,
                              assert_gates, build_engine_entities,
                              build_engine_intents, load_golden_rows,
                              resolve_pipeline_plugin, run_golden_suite,
                              write_predictions)

SKILL_ID = "toy-skill.test"
LANG = "en-us"

# A well-covered skill: two intents with several phrasings each.
GOOD_INTENTS = {
    "Hello": ["hello", "hi there", "hey", "good morning"],
    "Goodbye": ["bye", "goodbye", "see you later", "farewell"],
}

# Same two intents, plus a THIRD intent with a single, too-narrow
# template — deliberately thin, so a golden row phrased differently
# should miss it on the template engines.
THIN_INTENTS = dict(GOOD_INTENTS, Weather=["what is the weather"])

GOLDEN_ROWS = [
    GoldenRow(utterance="hello", lang=LANG, skill_id=SKILL_ID,
              expected_intent="Hello", core=True),
    GoldenRow(utterance="hi there", lang=LANG, skill_id=SKILL_ID,
              expected_intent="Hello", core=False),
    GoldenRow(utterance="goodbye", lang=LANG, skill_id=SKILL_ID,
              expected_intent="Goodbye", core=True),
    GoldenRow(utterance="see you later", lang=LANG, skill_id=SKILL_ID,
              expected_intent="Goodbye", core=False),
]

# A row that only the thin corpus is asked to match — different enough
# phrasing from its one template that padatious/padacioso/nebulento all
# fall below the 0.8 confidence gate, which should fail the template
# gate.
THIN_ROW = GoldenRow(utterance="will it rain tomorrow", lang=LANG,
                     skill_id=SKILL_ID, expected_intent="Weather", core=False)


class _StubAvailableM2V(M2VPrototypeAdapter):
    """A deterministic, network-free ``m2v-prototype`` stand-in that IS
    available and actually produces prediction rows (unlike
    ``_bogus_m2v``, which is deliberately unavailable and produces
    none) — needed to exercise the write_predictions() m2v exclusion:
    deleting that exclusion must turn a test using this adapter red.
    """
    def __init__(self):
        super().__init__(model_name="stub")

    def available(self):
        return True, None

    def build(self, intents, *, skill_id, lang, entities=None):
        return {name: object() for name, lines in intents.items() if lines}

    def match(self, container, utterance, lang):
        # Always "matches" the first registered intent name at a fixed
        # confidence — deterministic and network-free.
        name = next(iter(container), None) if container else None
        return name, 0.9, 0.0, None


def _bogus_m2v():
    """A deterministic, network-free m2v-prototype adapter: a model name
    that cannot resolve, so `available()` reliably reports `no-model`
    without depending on whatever happens to be HF-cached."""
    return M2VPrototypeAdapter(model_name="no-such/model-xyz")


def _fresh_engines():
    """Fresh per-test adapter instances (never the shared DEFAULT_ENGINES
    objects) — a `dict(DEFAULT_ENGINES)` shallow copy still shares every
    adapter, so a test mutating one (e.g. registering intents into its
    ``_cls``/container state) would bleed into every other test and into
    :data:`DEFAULT_ENGINES` itself. Built the same way
    ``run_golden_suite(engines=None)`` builds its own per-run engines."""
    return _make_default_engines()


class TestLoadGoldenRows(TestCase):
    def test_loads_jsonl_with_legacy_intent_label_alias(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "golden_utterances.jsonl"
            path.write_text(
                '{"utterance": "hello", "lang": "en-us", '
                '"skill_id": "toy-skill.test", "intent_label": "Hello", '
                '"core": true, "source_repo": "toy"}\n'
                '# a comment line\n'
                '\n'
                '{"utterance": "bye", "lang": "en-us", '
                '"skill_id": "toy-skill.test", "expected_intent": "Goodbye"}\n',
                encoding="utf-8")
            rows = load_golden_rows(path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].expected_intent, "Hello")
        self.assertTrue(rows[0].core)
        self.assertEqual(rows[0].provenance.get("source_repo"), "toy")
        self.assertEqual(rows[1].expected_intent, "Goodbye")
        self.assertFalse(rows[1].core)


class TestBuildEngineIntents(TestCase):
    def test_expands_intent_and_voc_files(self):
        with tempfile.TemporaryDirectory() as td:
            lang_dir = Path(td) / LANG
            lang_dir.mkdir(parents=True)
            (lang_dir / "greeting.voc").write_text("hi\nhello\n", encoding="utf-8")
            (lang_dir / "Hello.intent").write_text("<greeting> there\n", encoding="utf-8")
            intents = build_engine_intents(Path(td), LANG)
        self.assertIn("Hello", intents)
        self.assertIn("hi there", intents["Hello"])
        self.assertIn("hello there", intents["Hello"])


class TestBuildEngineEntities(TestCase):
    def test_loads_entity_files(self):
        with tempfile.TemporaryDirectory() as td:
            lang_dir = Path(td) / LANG
            lang_dir.mkdir(parents=True)
            (lang_dir / "city.entity").write_text("london\nparis\n", encoding="utf-8")
            entities = build_engine_entities(Path(td), LANG)
        self.assertEqual(entities, {"city": ["london", "paris"]})

    def test_registered_entity_changes_slot_scoring_and_extraction(self):
        """A registered ``{city}`` entity must actually reach the
        plugin: padacioso (``fuzz: False``, the runner's own fighter
        config) scores a slot value from the registered entity as an
        EXACT match (1.0) and a value outside it as a fuzzy fallback
        (< 1.0) — a runner that never calls ``register_entity`` (the
        pre-fix behaviour) leaves ``{city}`` an unconstrained capture
        with no registered set to score exactly against at all.
        ``matches`` must also carry the extracted slot value either
        way, proving :func:`build_engine_entities` output actually
        reaches the plugin's registration path."""
        with tempfile.TemporaryDirectory() as td:
            lang_dir = Path(td) / LANG
            lang_dir.mkdir(parents=True)
            (lang_dir / "city.entity").write_text("london\nparis\n", encoding="utf-8")
            (lang_dir / "Weather.intent").write_text(
                "weather in {city}\n", encoding="utf-8")
            intents = build_engine_intents(Path(td), LANG)
            entities = build_engine_entities(Path(td), LANG)
        self.assertEqual(entities, {"city": ["london", "paris"]})

        adapter = GenericOPMAdapter(FIGHTERS["padacioso-medium"])
        ok, reason = adapter.available()
        self.assertTrue(ok, reason)
        container = adapter.build(intents, skill_id=SKILL_ID, lang=LANG, entities=entities)

        name, conf_known, _, slots_known = adapter.match(container, "weather in london", LANG)
        self.assertEqual(name, "Weather")
        self.assertEqual(slots_known, {"city": "london"})

        name, conf_unknown, _, slots_unknown = adapter.match(container, "weather in tokyo", LANG)
        self.assertEqual(name, "Weather")
        self.assertEqual(slots_unknown, {"city": "tokyo"})

        self.assertEqual(conf_known, 1.0)
        self.assertLess(conf_unknown, conf_known,
                         "registering the city entity had no measurable effect on scoring")


class TestEntryPointResolution(TestCase):
    """The registry (FIGHTERS) is data; resolution goes through the
    installed OPM pipeline-plugin entry points, not hardcoded classes."""

    def test_every_default_fighter_resolves_to_an_installed_plugin(self):
        for competitor_id, spec in FIGHTERS.items():
            cls = resolve_pipeline_plugin(spec.entry_point)
            self.assertIsNotNone(cls, f"{competitor_id}: {spec.entry_point} not installed")

    def test_unknown_entry_point_resolves_to_none(self):
        self.assertIsNone(resolve_pipeline_plugin("no-such-pipeline-plugin"))

    def test_a_new_fighter_needs_no_new_adapter_class(self):
        """Adding a fighter for an OPM-conformant plugin ovoscope has
        never seen is one FighterSpec entry, driven by the SAME
        GenericOPMAdapter — this is the point of entry-point
        resolution: no per-engine subclass required."""
        from ovoscope.golden import FighterSpec
        spec = FighterSpec(
            competitor_id="padacioso-medium-clone",
            entry_point="ovos-padacioso-pipeline-plugin",
            tier="medium",
            config={"conf_high": 0.95, "conf_med": 0.8, "conf_low": 0.5, "fuzz": False},
            plugin_id="padacioso",
        )
        adapter = GenericOPMAdapter(spec)
        ok, reason = adapter.available()
        self.assertTrue(ok, reason)


class TestRunGoldenSuite(TestCase):
    """Template engines hit 100% on the well-covered corpus (via their
    real OPM pipeline plugins), m2v-prototype reports (or explicitly
    abstains as) unavailable without failing the gate, the core-flag is
    enforced, and a thin template fails the gate."""

    def test_template_engines_full_coverage_and_m2v_informational(self):
        groups = {(SKILL_ID, LANG): GOOD_INTENTS}
        engines = _fresh_engines()
        engines["m2v-prototype"] = _bogus_m2v()
        scoreboard, predictions = run_golden_suite(GOLDEN_ROWS, groups, engines=engines)

        for engine_id in DEFAULT_GATING_ENGINES:
            entry = scoreboard[engine_id]
            self.assertFalse(entry["unavailable"], f"{engine_id}: {entry.get('reason')}")
            self.assertEqual(entry["matched"], entry["total"], f"{engine_id} failures: {entry['failures']}")
            self.assertEqual(entry["pct"], 1.0)
            self.assertTrue(entry["gate_passed"])

        # m2v-prototype: informational by default, and CI-display-only —
        # no reachable embedding model here, so it must report the STABLE
        # `no-model` reason id rather than being silently absent from the
        # scoreboard or counted as a pass by omission, and that must NOT
        # fail the gate (m2v_gating defaults to False).
        m2v = scoreboard["m2v-prototype"]
        self.assertTrue(m2v["unavailable"])
        self.assertEqual(m2v["reason"], "no-model")
        self.assertTrue(m2v["gate_passed"])

        gating_predictions = [p for p in predictions if p.competitor_id != "m2v-prototype"]
        self.assertEqual(len(gating_predictions), len(GOLDEN_ROWS) * len(DEFAULT_GATING_ENGINES))
        self.assertFalse(any(p.competitor_id == "m2v-prototype" for p in predictions))
        sample = gating_predictions[0]
        self.assertIsInstance(sample, PredictionRow)
        self.assertEqual(sample.schema_version, 2)
        self.assertEqual(sample.modality, "intent")
        self.assertEqual(sample.dataset_id, "golden-utterances")
        self.assertIsNone(sample.bucket)

    def test_matched_predictions_carry_a_real_confidence(self):
        """calc_intent's MatchData/PadaciosoIntent/NebulentoIntent expose
        name/conf as plain attributes, not via a dict-style ``.get`` —
        every matched row's published confidence must be a real,
        plausible score (clearing the engine's own 0.8 medium-tier
        gate), never the 0.0 a `.get("conf")` misread would silently
        produce."""
        groups = {(SKILL_ID, LANG): GOOD_INTENTS}
        engines = _fresh_engines()
        engines["m2v-prototype"] = _bogus_m2v()
        _, predictions = run_golden_suite(GOLDEN_ROWS, groups, engines=engines)

        matched = [p for p in predictions
                   if p.competitor_id in DEFAULT_GATING_ENGINES and p.exact_match]
        self.assertTrue(matched)
        for p in matched:
            self.assertGreaterEqual(
                p.confidence, 0.8,
                f"{p.competitor_id} {p.utterance!r}: implausible confidence {p.confidence}")

    def test_core_flag_enforced_on_gating_engines(self):
        """A core row that fails must fail the gate even though every
        non-core row still matches: the CORE utterance's own template
        ("hello") is deliberately removed here."""
        broken_intents = {
            "Hello": ["hi there", "hey", "good morning"],  # "hello" itself removed
            "Goodbye": ["bye", "goodbye", "see you later", "farewell"],
        }
        groups = {(SKILL_ID, LANG): broken_intents}
        engines = _fresh_engines()
        engines["m2v-prototype"] = _bogus_m2v()  # offline: no HF hub hit
        scoreboard, _ = run_golden_suite(GOLDEN_ROWS, groups, engines=engines)

        padatious = scoreboard["padatious-medium"]
        self.assertEqual(padatious["core_total"], 2)
        self.assertLess(padatious["core_matched"], padatious["core_total"])
        self.assertFalse(padatious["gate_passed"])

        with self.assertRaises(AssertionError):
            assert_gates(scoreboard)

    def test_thin_template_fails_template_engine_gate(self):
        rows = GOLDEN_ROWS + [THIN_ROW]
        groups = {(SKILL_ID, LANG): THIN_INTENTS}
        engines = _fresh_engines()
        engines["m2v-prototype"] = _bogus_m2v()  # offline: no HF hub hit
        scoreboard, _ = run_golden_suite(rows, groups, engines=engines)

        for engine_id in DEFAULT_GATING_ENGINES:
            entry = scoreboard[engine_id]
            self.assertFalse(entry["gate_passed"], f"{engine_id} unexpectedly passed")
            self.assertTrue(any(f["utterance"] == THIN_ROW.utterance
                                for f in entry["failures"]),
                            f"{engine_id}: {entry['failures']}")
        with self.assertRaises(AssertionError) as ctx:
            assert_gates(scoreboard)
        self.assertIn("will it rain tomorrow", str(ctx.exception))

    def test_unavailable_gating_engine_reports_stable_reason_and_fails_gate(self):
        class AlwaysUnavailable(GenericOPMAdapter):
            def available(self):
                return False, REASON_NOT_INSTALLED

        engines = _fresh_engines()
        engines["padatious-medium"] = AlwaysUnavailable(FIGHTERS["padatious-medium"])
        engines["m2v-prototype"] = _bogus_m2v()  # offline: no HF hub hit
        groups = {(SKILL_ID, LANG): GOOD_INTENTS}
        scoreboard, predictions = run_golden_suite(GOLDEN_ROWS, groups, engines=engines)

        entry = scoreboard["padatious-medium"]
        self.assertTrue(entry["unavailable"])
        self.assertEqual(entry["reason"], REASON_NOT_INSTALLED)
        self.assertFalse(entry["gate_passed"])
        self.assertFalse(any(p.competitor_id == "padatious-medium" for p in predictions))
        with self.assertRaises(AssertionError):
            assert_gates(scoreboard)


class TestPerRunAdapters(TestCase):
    """``run_golden_suite(engines=None)`` (the default every skill's CI
    actually uses) must build fresh adapter instances, never reuse or
    mutate the shared :data:`DEFAULT_ENGINES` — otherwise a per-call
    knob like ``m2v_threshold`` permanently reconfigures the process-
    wide defaults for every later, unrelated call."""

    def test_default_engines_survive_a_run_untouched(self):
        baseline_threshold = DEFAULT_ENGINES["m2v-prototype"].threshold
        baseline_cls = {cid: DEFAULT_ENGINES[cid]._cls for cid in FIGHTERS}
        # DEFAULT_ENGINES itself is never exercised by any other test in
        # this file (they all go through _fresh_engines()) — if this
        # fails, something reused the shared instances.
        for cid, cls in baseline_cls.items():
            self.assertIsNone(cls, f"{cid}: DEFAULT_ENGINES was already touched before this test ran")

        groups = {(SKILL_ID, LANG): GOOD_INTENTS}
        # m2v-prototype's own available() would otherwise try to reach
        # the HF hub for the real default model; stub it out so this
        # stays fully offline while still exercising the m2v_threshold
        # code path that used to mutate the shared instance's attribute.
        with patch.object(M2VPrototypeAdapter, "available", lambda self: (False, "no-model")):
            run_golden_suite(GOLDEN_ROWS, groups, m2v_threshold=0.99)

        self.assertEqual(
            DEFAULT_ENGINES["m2v-prototype"].threshold, baseline_threshold,
            "m2v_threshold leaked into the shared DEFAULT_ENGINES['m2v-prototype'] instance")
        for cid in FIGHTERS:
            self.assertIsNone(
                DEFAULT_ENGINES[cid]._cls,
                f"{cid}: a run resolved/cached _cls on the shared DEFAULT_ENGINES adapter")


class TestWritePredictions(TestCase):
    def test_writes_per_lang_per_competitor_jsonl_excluding_m2v(self):
        groups = {(SKILL_ID, LANG): GOOD_INTENTS}
        engines = _fresh_engines()
        # An AVAILABLE m2v stand-in, so it actually produces rows: with
        # an unavailable one (_bogus_m2v) this test passes 10/10 even
        # if the write_predictions() m2v exclusion is deleted, because
        # there would be no m2v rows to exclude in the first place.
        engines["m2v-prototype"] = _StubAvailableM2V()
        _, predictions = run_golden_suite(GOLDEN_ROWS, groups, engines=engines)
        self.assertTrue(any(p.competitor_id == "m2v-prototype" for p in predictions),
                         "test setup bug: stub m2v produced no rows to exclude")
        with tempfile.TemporaryDirectory() as td:
            paths = write_predictions(predictions, td)
            self.assertTrue(paths)
            written_competitors = {p.stem for p in paths}
            self.assertNotIn("m2v-prototype", written_competitors)
            for path in paths:
                self.assertTrue(path.exists())
                self.assertIn(str(Path("predictions") / LANG), str(path))
                lines = path.read_text(encoding="utf-8").strip().splitlines()
                self.assertEqual(len(lines), len(GOLDEN_ROWS))
