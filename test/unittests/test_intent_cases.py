# Copyright 2024 OpenVoiceOS
# Licensed under the Apache License, Version 2.0
"""Unit tests for ovoscope.intent_cases helpers and the accuracy reporter.

Tests focus on the parts that don't need a live MiniCroft: the file-layout
loader, the JSON summary roll-ups, the structural baseline diff, and the
Markdown report renderer. The end-to-end execution path is covered by
the consumer skill's e2e suite.
"""
from pathlib import Path
from unittest import TestCase

from ovoscope.intent_cases import (IntentCase, _read_lines, load_intent_cases)
from ovoscope.pytest_plugin import (_accuracy_markdown, _accuracy_summary,
                                     _baseline_diff)


class TestLoadIntentCases(TestCase):
    """Discovery of <lang>/<Intent>.intent.test and no_match.test files."""

    def _write(self, tmp, lang, name, body):
        d = tmp / "cases" / lang
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(body, encoding="utf-8")

    def test_loads_intent_and_no_match_files(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._write(tmp, "en-US", "WhoAreYou.intent.test",
                        "# header\nwho are you\nwhat is your name\n")
            self._write(tmp, "en-US", "no_match.test", "what time is it\n")
            cases = load_intent_cases(tmp / "cases",
                                      known_intents=["WhoAreYou.intent"])
            self.assertEqual(len(cases), 3)
            self.assertEqual(sum(1 for c in cases if c.intent is None), 1)
            self.assertTrue(all(c.lang == "en-US" for c in cases))
            # case ids are the OVOS-INTENT-4 canonical (suffixless) form the
            # matcher plugins actually register, regardless of whether the
            # caller's known_intents entries carry the legacy suffix
            self.assertEqual({c.intent for c in cases if c.intent},
                             {"WhoAreYou"})

    def test_suffixless_known_intents_accepted(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._write(tmp, "en-US", "WhoAreYou.intent.test", "who are you\n")
            cases = load_intent_cases(tmp / "cases",
                                      known_intents=["WhoAreYou"])
            self.assertEqual(cases[0].intent, "WhoAreYou")

    def test_unknown_intent_raises(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._write(tmp, "en-US", "Mystery.intent.test", "x\n")
            with self.assertRaises(AssertionError):
                load_intent_cases(tmp / "cases",
                                  known_intents=["WhoAreYou.intent"])

    def test_skips_comments_and_blank_lines(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._write(tmp, "en-US", "WhoAreYou.intent.test",
                        "# a\n\n  \nfoo\n#bar\n")
            cases = load_intent_cases(tmp / "cases",
                                      known_intents=["WhoAreYou.intent"])
            self.assertEqual([c.utterance for c in cases], ["foo"])


class TestAccuracySummary(TestCase):
    """Aggregate roll-ups feeding the markdown / terminal pivots."""

    def _r(self, **kw):
        base = {"pipeline": "TestX", "lang": "en-US",
                "intent": "WhoAreYou.intent", "utterance": "u",
                "passed": True, "source": ""}
        base.update(kw)
        return base

    def test_overall_and_pivots(self):
        results = [
            self._r(),
            self._r(passed=False),
            self._r(pipeline="TestY"),
        ]
        s = _accuracy_summary(results)
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["passed"], 2)
        self.assertAlmostEqual(s["overall_accuracy"], 2 / 3)
        # Per-pipeline bucket exists for both pipelines.
        self.assertEqual(s["by_pipeline"]["TestX"]["total"], 2)
        self.assertEqual(s["by_pipeline"]["TestY"]["pass"], 1)
        # by_utterance carries failing_pipelines for diagnosis.
        by_utt = {(u["lang"], u["intent"], u["utterance"]): u
                  for u in s["by_utterance"]}
        u = by_utt[("en-US", "WhoAreYou.intent", "u")]
        self.assertIn("TestX", u["failing_pipelines"])


class TestBaselineDiff(TestCase):
    """Structural baseline diff: regressed vs recovered."""

    def _r(self, pipeline, passed, utterance="u"):
        return {"pipeline": pipeline, "lang": "en-US",
                "intent": "WhoAreYou.intent", "utterance": utterance,
                "passed": passed}

    def test_diff_classifies_flips(self):
        baseline = [self._r("TestX", True), self._r("TestY", False)]
        current = [self._r("TestX", False), self._r("TestY", True),
                   self._r("TestZ", True)]
        d = _baseline_diff(baseline, current)
        self.assertEqual(len(d["regressed"]), 1)
        self.assertEqual(d["regressed"][0]["pipeline"], "TestX")
        self.assertEqual(len(d["recovered"]), 1)
        self.assertEqual(d["recovered"][0]["pipeline"], "TestY")
        self.assertEqual(len(d["added"]), 1)
        self.assertEqual(d["added"][0]["pipeline"], "TestZ")
        self.assertEqual(d["baseline_total"], 2)


class TestAccuracyMarkdown(TestCase):
    """Markdown report includes every section we need for PR comments."""

    def test_markdown_contains_expected_sections(self):
        results = [
            {"pipeline": "TestA", "lang": "en-US", "intent": "I.intent",
             "utterance": "ok phrase", "passed": True},
            {"pipeline": "TestA", "lang": "en-US", "intent": "I.intent",
             "utterance": "bad phrase", "passed": False},
        ]
        s = _accuracy_summary(results)
        md = _accuracy_markdown(s, results)
        self.assertIn("intent-case checks passed", md)
        self.assertIn("| Pipeline | Pass / Total | Accuracy |", md)
        self.assertIn("Per-pipeline × language", md)
        self.assertIn("Per-pipeline × intent", md)
        self.assertIn("Hardest utterances", md)
        # The failing utterance must be quoted in the hardest list.
        self.assertIn("bad phrase", md)

    def test_baseline_diff_section_when_supplied(self):
        baseline = [{"pipeline": "TestA", "lang": "en-US",
                     "intent": "I.intent", "utterance": "phrase",
                     "passed": True}]
        current = [{"pipeline": "TestA", "lang": "en-US",
                    "intent": "I.intent", "utterance": "phrase",
                    "passed": False}]
        s = _accuracy_summary(current)
        d = _baseline_diff(baseline, current)
        md = _accuracy_markdown(s, current, baseline_diff=d)
        self.assertIn("Baseline diff", md)
        self.assertIn("Regressed (1)", md)
