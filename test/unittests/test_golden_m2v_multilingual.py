"""Golden EFFECT test for the m2v-multilingual boot recipe.

Boots ovos-skill-personal through the candidate default intent engine — the
model2vec multilingual classifier — via ``get_m2v_minicroft`` and asserts, for
each utterance, the whole round trip: the correct intent routed AND the
rendered spoken dialog carries the real answer words (or the slot value), not
the dialog file name. One utterance is Spanish, proving the multilingual model
routes localized input.

The skill is loaded under its real registered id
(``ovos-skill-personal.openvoiceos``) through the normal skill-plugin install:
the classifier's trained labels carry the author-qualified skill id, so the
model's classes intersect the labels the skill registers at runtime and route
directly. ``test_registered_skill_id_labels_route`` pins that the model's class
set carries the ``.openvoiceos`` labels and that they intersect the loaded
skill's registered intents.

This is a live test: the ~512MB model downloads to the shared HuggingFace cache
on first run. It is skipped unless OVOSCOPE_LIVE=1 and the m2v pipeline is
installed, mirroring ovos-m2v-pipeline's own live fixture.
"""
import os
import shutil
import tempfile
import unittest

from ovos_bus_client.message import Message
from ovos_bus_client.session import Session

from ovoscope import (get_m2v_minicroft, is_pipeline_available,
                      M2V_PIPELINE, M2V_MULTILINGUAL_MODEL)

# The canonical author-qualified id the installed skill registers under, and the
# id the multilingual model's labels are trained on.
SKILL_ID = "ovos-skill-personal.openvoiceos"

LIVE = os.environ.get("OVOSCOPE_LIVE") == "1"

_MC = None
_PIPE = None
_XDG = None
_ORIG_XDG = None


def setUpModule():
    global _MC, _PIPE, _XDG, _ORIG_XDG
    if not LIVE or not is_pipeline_available(M2V_PIPELINE):
        return

    _ORIG_XDG = os.environ.get("XDG_DATA_HOME")
    _XDG = tempfile.mkdtemp(prefix="ovoscope-m2v-personal-xdg-")
    os.environ["XDG_DATA_HOME"] = _XDG

    # Normal skill-plugin install: the skill registers its intents under its
    # canonical author-qualified id, which the model's labels are trained on.
    _MC = get_m2v_minicroft(
        skill_ids=[SKILL_ID],
        lang="en-US",
        secondary_langs=["es-ES"],
    )
    _PIPE = _MC.intents.pipeline_plugins["ovos-m2v-pipeline"]
    # The model loads lazily on first match; force it now so the first real
    # utterance is not silently skipped while the model is still warming up.
    _PIPE._ensure_model(background_ok=False)


def tearDownModule():
    global _MC, _XDG, _ORIG_XDG
    if _MC is not None:
        _MC.stop()
        _MC = None
    if _ORIG_XDG is None:
        os.environ.pop("XDG_DATA_HOME", None)
    else:
        os.environ["XDG_DATA_HOME"] = _ORIG_XDG
    if _XDG is not None:
        shutil.rmtree(_XDG, ignore_errors=True)
        _XDG = None


@unittest.skipUnless(LIVE, "live m2v model test; set OVOSCOPE_LIVE=1 to enable")
class TestM2VMultilingualGoldenEffect(unittest.TestCase):
    """Effect assertions: utterance in -> correct intent -> real words out."""

    def _run(self, utterance: str, lang: str, timeout: float = 15.0):
        """Fire one utterance and return (dialog_name, rendered_text, failed).

        dialog_name/rendered_text come from the captured ``speak``;
        ``failed`` is True if a ``complete_intent_failure`` arrived instead.
        """
        speaks: list = []
        failures: list = []

        def _on_speak(msg):
            speaks.append(msg)

        def _on_fail(msg):
            failures.append(msg)

        self._MC = _MC
        _MC.bus.on("speak", _on_speak)
        _MC.bus.on("complete_intent_failure", _on_fail)
        sess = Session(session_id=f"m2v-golden-{hash(utterance)}",
                       pipeline=M2V_PIPELINE)
        sess.lang = lang
        try:
            _MC.bus.emit(Message(
                "recognizer_loop:utterance",
                data={"utterances": [utterance], "lang": lang},
                context={"session": sess.serialize(), "lang": lang},
            ))
            import time as _t
            deadline = _t.time() + timeout
            while _t.time() < deadline and not speaks and not failures:
                _t.sleep(0.05)
        finally:
            _MC.bus.remove("speak", _on_speak)
            _MC.bus.remove("complete_intent_failure", _on_fail)

        if not speaks:
            return None, None, bool(failures)
        data = speaks[0].data
        meta = data.get("meta", {}) or {}
        return meta.get("dialog"), (data.get("utterance") or ""), False

    def _assert_effect(self, utterance, lang, expected_dialog, expected_substrings):
        dialog, text, failed = self._run(utterance, lang)
        self.assertFalse(
            failed,
            f"{utterance!r} ({lang}) did not route: complete_intent_failure")
        self.assertIsNotNone(
            text, f"{utterance!r} ({lang}) produced no spoken output")
        low = text.lower()
        # Routing: the intent-specific dialog was rendered.
        self.assertEqual(
            dialog, expected_dialog,
            f"{utterance!r} ({lang}) routed to dialog {dialog!r}, "
            f"expected {expected_dialog!r} (rendered: {text!r})")
        # Effect: real words / slot values, never the raw dialog file name.
        self.assertNotIn(
            expected_dialog, low,
            f"{utterance!r} ({lang}) spoke the dialog NAME, not rendered text: {text!r}")
        for sub in expected_substrings:
            self.assertIn(
                sub.lower(), low,
                f"{utterance!r} ({lang}) rendered {text!r}; missing {sub!r}")

    # The who_am_i dialog has two variants and its {name} slot depends on the
    # configured wake word, so assert on the variant-invariant words.
    def test_who_are_you_en_effect(self):
        dialog, text, failed = self._run("who are you", "en-US")
        self.assertFalse(failed, "who are you did not route")
        self.assertEqual(dialog, "who_am_i")
        low = text.lower()
        self.assertTrue(
            "open-source" in low or "intelligent personal assistant" in low,
            f"unexpected who_am_i rendering: {text!r}")
        self.assertNotIn("who_am_i", low)

    def test_what_are_you_en(self):
        dialog, text, failed = self._run("what are you", "en-US")
        self.assertFalse(failed, "what are you did not route")
        self.assertEqual(dialog, "what_am_i")
        low = text.lower()
        self.assertTrue(
            "artificial intelligence" in low or "software" in low,
            f"unexpected what_am_i rendering: {text!r}")
        self.assertNotIn("what_am_i", low)

    def test_who_made_you_en(self):
        # who_made_me renders the {creator} slot (default OpenVoiceOS).
        self._assert_effect(
            "who made you", "en-US", "who_made_me",
            ["openvoiceos", "community"])

    def test_when_were_you_born_en(self):
        # when_was_i_born renders the {year} slot (default 2015).
        self._assert_effect(
            "when were you born", "en-US", "when_was_i_born", ["2015"])

    def test_where_were_you_born_en(self):
        # where_was_i_born renders the {location} slot (default Lawrence Kansas).
        self._assert_effect(
            "where were you born", "en-US", "where_was_i_born", ["lawrence kansas"])

    def test_who_made_you_es(self):
        # Non-en-US: Spanish routes to the same intent; the es-ES who_made_me
        # dialog renders "...comunidad y el equipo de OpenVoiceOS".
        self._assert_effect(
            "quién te creó", "es-ES", "who_made_me", ["openvoiceos", "comunidad"])


@unittest.skipUnless(LIVE, "live m2v model test; set OVOSCOPE_LIVE=1 to enable")
class TestM2VRegisteredLabelRouting(unittest.TestCase):
    """The model's labels carry the canonical skill id and route it."""

    def test_registered_skill_id_labels_route(self):
        classes = {str(c) for c in _PIPE.model.classes_}
        # The model is trained on author-qualified labels...
        self.assertIn(f"{SKILL_ID}:who_are_you", classes)
        # ...and carries no bare-package-name labels for this skill.
        self.assertNotIn("ovos-skill-personal:who_are_you", classes)
        # The skill registers under its canonical id, so its runtime labels
        # intersect the model's trained classes -> it can route.
        registered = set(_PIPE.intents)
        self.assertIn(f"{SKILL_ID}:who_are_you", registered)
        self.assertTrue(
            registered & classes,
            "registered intent labels do not intersect the model's classes; "
            "the skill would route nothing through this model")
